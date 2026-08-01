from __future__ import annotations

import asyncio
import html
import logging
import os
import secrets
import threading
import time
from collections.abc import Awaitable, Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

_ready = False
_ready_lock = threading.Lock()

_OAUTH_TTL_SEC = 600
# state -> (telegram_user_id, lang, expires_at)
_oauth_pending: dict[str, tuple[int, str, float]] = {}
_oauth_pending_lock = threading.Lock()

# on_complete(telegram_user_id, followed, error, token_info)
# token_info: access_token, refresh_token, twitch_user_id (only on success)
OAuthCompleteHandler = Callable[
    [int, Optional[list], Optional[str], Optional[dict[str, str]]],
    Awaitable[None],
]
_oauth_loop: asyncio.AbstractEventLoop | None = None
_oauth_on_complete: OAuthCompleteHandler | None = None
_oauth_twitch: Any = None
_oauth_redirect_uri: str = ""


def mark_ready() -> None:
    global _ready
    with _ready_lock:
        _ready = True


def is_ready() -> bool:
    with _ready_lock:
        return _ready


def create_oauth_state(
    telegram_user_id: int, lang: str = "en", *, purpose: str = "import"
) -> str:
    from i18n import DEFAULT_LOCALE, SUPPORTED_LOCALES

    locale = lang if lang in SUPPORTED_LOCALES else DEFAULT_LOCALE
    state = secrets.token_urlsafe(24)
    expires = time.time() + _OAUTH_TTL_SEC
    with _oauth_pending_lock:
        _oauth_pending[state] = (telegram_user_id, locale, expires, purpose)
        _purge_oauth_locked(time.time())
    return state


def pop_oauth_state(state: str) -> tuple[int, str, str] | None:
    from i18n import DEFAULT_LOCALE

    now = time.time()
    with _oauth_pending_lock:
        _purge_oauth_locked(now)
        item = _oauth_pending.pop(state, None)
    if not item:
        return None
    if len(item) == 3:
        user_id, lang, expires = item
        purpose = "import"
    else:
        user_id, lang, expires, purpose = item
    if expires < now:
        return None
    return user_id, lang or DEFAULT_LOCALE, purpose


def _purge_oauth_locked(now: float) -> None:
    expired = [k for k, v in _oauth_pending.items() if v[2] < now]
    for k in expired:
        del _oauth_pending[k]


def register_oauth_bridge(
    loop: asyncio.AbstractEventLoop,
    *,
    twitch: Any,
    redirect_uri: str,
    on_complete: OAuthCompleteHandler,
) -> None:
    global _oauth_loop, _oauth_on_complete, _oauth_twitch, _oauth_redirect_uri
    _oauth_loop = loop
    _oauth_on_complete = on_complete
    _oauth_twitch = twitch
    _oauth_redirect_uri = redirect_uri


def _schedule_oauth_complete(
    telegram_user_id: int,
    followed: list[dict[str, Any]] | None,
    error: str | None,
    token_info: dict[str, str] | None = None,
) -> None:
    if _oauth_loop is None or _oauth_on_complete is None:
        logger.error("OAuth complete with no bridge registered")
        return
    fut = asyncio.run_coroutine_threadsafe(
        _oauth_on_complete(telegram_user_id, followed, error, token_info),
        _oauth_loop,
    )

    def _done(f: Any) -> None:
        try:
            f.result()
        except Exception:
            logger.exception("OAuth on_complete failed for user %s", telegram_user_id)

    fut.add_done_callback(_done)


def _handle_twitch_oauth(query: dict[str, list[str]]) -> tuple[int, bytes, str]:
    from i18n import DEFAULT_LOCALE, t

    err = (query.get("error") or [""])[0]
    state = (query.get("state") or [""])[0]
    code = (query.get("code") or [""])[0]
    pending = pop_oauth_state(state) if state else None
    lang = DEFAULT_LOCALE
    telegram_user_id: int | None = None
    purpose = "import"
    if pending:
        telegram_user_id, lang, purpose = pending
    if telegram_user_id is None:
        body = _html_page(
            t("oauth_web_expired_title", lang),
            t("oauth_web_expired_body", lang),
        )
        return 400, body, "text/html; charset=utf-8"
    if err:
        _schedule_oauth_complete(telegram_user_id, None, err)
        body = _html_page(
            t("oauth_web_cancelled_title", lang),
            t("oauth_web_cancelled_body", lang),
        )
        return 200, body, "text/html; charset=utf-8"
    if not code or not _oauth_twitch or not _oauth_redirect_uri:
        _schedule_oauth_complete(telegram_user_id, None, "missing_code")
        body = _html_page(
            t("oauth_web_failed_title", lang),
            t("oauth_web_failed_body", lang),
        )
        return 400, body, "text/html; charset=utf-8"
    try:
        token_data = _oauth_twitch.exchange_code(code, redirect_uri=_oauth_redirect_uri)
        access = token_data.get("access_token") or ""
        refresh = token_data.get("refresh_token") or ""
        user = _oauth_twitch.get_token_user(access)
        if not user:
            raise RuntimeError("no_user")
        twitch_user_id = str(user["id"])
        if purpose == "schedule":
            followed = []
        elif purpose == "premium":
            followed = []
        else:
            followed = _oauth_twitch.get_followed_channels(access, twitch_user_id)
    except Exception as exc:
        logger.warning("Twitch OAuth failed (purpose=%s): %s", purpose, exc)
        _schedule_oauth_complete(telegram_user_id, None, "twitch_api")
        body = _html_page(
            t("oauth_web_failed_title", lang),
            t("oauth_web_failed_body", lang),
        )
        return 500, body, "text/html; charset=utf-8"
    token_info = {
        "access_token": access,
        "refresh_token": refresh,
        "twitch_user_id": twitch_user_id,
        "purpose": purpose,
    }
    if purpose == "premium":
        from config import PREMIUM_TWITCH_LOGIN

        try:
            broadcaster = _oauth_twitch.get_user(PREMIUM_TWITCH_LOGIN)
            active = False
            if broadcaster:
                active = _oauth_twitch.check_user_subscription(
                    access,
                    broadcaster_id=str(broadcaster["id"]),
                    user_id=twitch_user_id,
                )
            token_info["twitch_sub_active"] = "1" if active else "0"
        except Exception as exc:
            logger.warning("Premium Twitch sub check failed: %s", exc)
            token_info["twitch_sub_active"] = "0"
    _schedule_oauth_complete(telegram_user_id, followed, None, token_info)
    body = _html_page(
        t("oauth_web_done_title", lang),
        t("oauth_web_done_body", lang),
    )
    return 200, body, "text/html; charset=utf-8"


def _html_page(title: str, message: str) -> bytes:
    safe_title = html.escape(title)
    safe_msg = html.escape(message)
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{safe_title}</title></head>"
        "<body style='font-family:sans-serif;text-align:center;padding:3rem'>"
        f"<h1>{safe_title}</h1><p>{safe_msg}</p></body></html>"
    ).encode("utf-8")


def _placeholders_page(lang: str) -> bytes:
    from i18n import DEFAULT_LOCALE, SUPPORTED_LOCALES, t

    loc = lang if lang in SUPPORTED_LOCALES else DEFAULT_LOCALE
    title = html.escape(t("placeholders_page_title", loc))
    intro = html.escape(t("placeholders_page_intro", loc))
    # Body already contains safe HTML tags from i18n; tokens use literal braces.
    body = t("placeholders_page_body", loc)
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "</head><body style='font-family:sans-serif;max-width:40rem;"
        "margin:2rem auto;padding:0 1rem;line-height:1.5'>"
        f"<h1>{title}</h1><p>{intro}</p>{body}</body></html>"
    ).encode("utf-8")


class _HealthHandler(BaseHTTPRequestHandler):
    def _path_only(self) -> str:
        return urlparse(self.path).path

    def _health_paths(self) -> bool:
        return self._path_only() in ("/", "/health")

    def do_GET(self) -> None:
        path = self._path_only()
        if path == "/oauth/twitch/callback":
            query = parse_qs(urlparse(self.path).query)
            status, body, content_type = _handle_twitch_oauth(query)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/placeholders":
            query = parse_qs(urlparse(self.path).query)
            lang = (query.get("lang") or ["en"])[0]
            body = _placeholders_page(lang)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self._health_paths():
            self.send_response(404)
            self.end_headers()
            return
        if is_ready():
            body = b"ok"
            self.send_response(200)
        else:
            body = b"starting"
            self.send_response(503)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:
        if not self._health_paths():
            self.send_response(404)
            self.end_headers()
            return
        if is_ready():
            self.send_response(200)
            self.send_header("Content-Length", "2")
        else:
            self.send_response(503)
            self.send_header("Content-Length", "8")
        self.send_header("Content-Type", "text/plain")
        self.end_headers()

    def log_message(self, format: str, *args) -> None:
        pass


def start_health_server() -> None:
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
        name="health-server",
    )
    thread.start()
