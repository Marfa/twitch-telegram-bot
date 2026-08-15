from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Awaitable, Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

# PostHog error-tracking Issue alerts → Telegram admins
PosthogIssueHandler = Callable[[dict[str, Any]], Awaitable[None]]
_posthog_issue_loop: asyncio.AbstractEventLoop | None = None
_posthog_issue_on_event: PosthogIssueHandler | None = None
_POSTHOG_BODY_MAX = 256_000


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


def register_posthog_issue_bridge(
    loop: asyncio.AbstractEventLoop,
    on_event: PosthogIssueHandler,
) -> None:
    global _posthog_issue_loop, _posthog_issue_on_event
    _posthog_issue_loop = loop
    _posthog_issue_on_event = on_event


def parse_posthog_issue_payload(raw: dict[str, Any]) -> dict[str, str] | None:
    """Normalize HogFunction webhook JSON into name/description/url/kind."""
    if not isinstance(raw, dict):
        return None
    kind = ""
    name = ""
    description = ""
    url = ""
    fingerprint = ""
    status = ""
    # Custom body from our HogFunction inputs.body
    if any(k in raw for k in ("name", "fingerprint", "kind", "description")) and not isinstance(
        raw.get("event"), dict
    ):
        kind = str(raw.get("kind") or raw.get("event") or "")
        name = str(raw.get("name") or "").strip()
        description = str(raw.get("description") or "").strip()
        url = str(raw.get("url") or "").strip()
        fingerprint = str(raw.get("fingerprint") or "").strip()
        status = str(raw.get("status") or "").strip()
    else:
        event = raw.get("event")
        if isinstance(event, dict):
            kind = str(event.get("event") or "")
            props = event.get("properties")
            if not isinstance(props, dict):
                props = {}
            name = str(props.get("name") or "").strip()
            description = str(props.get("description") or "").strip()
            fingerprint = str(props.get("fingerprint") or "").strip()
            status = str(props.get("status") or "").strip()
            url = str(raw.get("url") or props.get("url") or "").strip()
        else:
            kind = str(raw.get("kind") or raw.get("event") or "")
            name = str(raw.get("name") or "").strip()
            description = str(raw.get("description") or "").strip()
            url = str(raw.get("url") or "").strip()
            fingerprint = str(raw.get("fingerprint") or "").strip()
            status = str(raw.get("status") or "").strip()

    allowed = {
        "$error_tracking_issue_created",
        "$error_tracking_issue_reopened",
        "created",
        "reopened",
    }
    if kind and kind not in allowed:
        return None
    if not name and not description and not fingerprint:
        return None
    kind_key = (
        "reopened"
        if kind in ("$error_tracking_issue_reopened", "reopened")
        else "created"
    )
    return {
        "kind": kind_key,
        "name": name or fingerprint or "Issue",
        "description": description,
        "url": url,
        "fingerprint": fingerprint,
        "status": status,
    }


def _schedule_posthog_issue(payload: dict[str, str]) -> None:
    if _posthog_issue_loop is None or _posthog_issue_on_event is None:
        logger.error("PostHog issue webhook with no bridge registered")
        return
    fut = asyncio.run_coroutine_threadsafe(
        _posthog_issue_on_event(payload),
        _posthog_issue_loop,
    )

    def _done(f: Any) -> None:
        try:
            f.result()
        except Exception:
            logger.exception("PostHog issue notify failed")

    fut.add_done_callback(_done)


def _posthog_webhook_authorized(handler: BaseHTTPRequestHandler) -> bool:
    from config import POSTHOG_ISSUE_WEBHOOK_SECRET

    secret = POSTHOG_ISSUE_WEBHOOK_SECRET
    if not secret:
        return False
    auth = handler.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if secrets.compare_digest(token, secret):
            return True
    query = parse_qs(urlparse(handler.path).query)
    token = (query.get("token") or [""])[0]
    if token and secrets.compare_digest(token, secret):
        return True
    return False


def _handle_posthog_issue_post(
    handler: BaseHTTPRequestHandler,
) -> tuple[int, bytes]:
    if not _posthog_webhook_authorized(handler):
        return 401, b"unauthorized"
    length_raw = handler.headers.get("Content-Length") or "0"
    try:
        length = int(length_raw)
    except ValueError:
        return 400, b"bad content-length"
    if length < 0 or length > _POSTHOG_BODY_MAX:
        return 413, b"payload too large"
    try:
        body = handler.rfile.read(length)
        raw = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 400, b"invalid json"
    if not isinstance(raw, dict):
        return 400, b"expected object"
    parsed = parse_posthog_issue_payload(raw)
    if not parsed:
        return 204, b""
    _schedule_posthog_issue(parsed)
    return 202, b"accepted"


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


def _fmt_rub(amount: int) -> str:
    return f"{amount:,}".replace(",", "\u00a0")


def _oferta_page() -> bytes:
    """Public offer for paid Premium (Russian legal text; always ru)."""
    from config import (
        PREMIUM_FREE_ACTIVE_LIMIT,
        PREMIUM_STARS_AMOUNT,
        PREMIUM_STARS_FEATURE,
        PREMIUM_STARS_LIFETIME,
        PREMIUM_STARS_YEAR,
        PREMIUM_TRIAL_DAYS,
        PREMIUM_TWITCH_LOGIN,
    )
    from i18n import t

    # Orientative Stars→RUB for offer disclosure; Telegram sets the rate when buying Stars.
    rub_per_star = 2
    title = html.escape(t("oferta_page_title", "ru"))
    intro = html.escape(t("oferta_page_intro", "ru"))
    body = t(
        "oferta_page_body",
        "ru",
        free_limit=PREMIUM_FREE_ACTIVE_LIMIT,
        trial_days=PREMIUM_TRIAL_DAYS,
        channel=html.escape(PREMIUM_TWITCH_LOGIN),
        month_stars=PREMIUM_STARS_AMOUNT,
        month_rub=_fmt_rub(PREMIUM_STARS_AMOUNT * rub_per_star),
        year_stars=PREMIUM_STARS_YEAR,
        year_rub=_fmt_rub(PREMIUM_STARS_YEAR * rub_per_star),
        life_stars=PREMIUM_STARS_LIFETIME,
        life_rub=_fmt_rub(PREMIUM_STARS_LIFETIME * rub_per_star),
        feat_stars=PREMIUM_STARS_FEATURE,
        feat_rub=_fmt_rub(PREMIUM_STARS_FEATURE * rub_per_star),
        rub_per_star=rub_per_star,
    )
    return (
        "<!DOCTYPE html><html lang='ru'><head><meta charset='utf-8'>"
        f"<title>{title}</title>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "</head><body style='font-family:sans-serif;max-width:40rem;"
        "margin:2rem auto;padding:0 1rem;line-height:1.5'>"
        f"<h1>{title}</h1><p>{intro}</p>{body}</body></html>"
    ).encode("utf-8")


class _HealthHandler(BaseHTTPRequestHandler):
    timeout = 60  # half-open clients must not hold a worker forever

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
        if path == "/oferta":
            body = _oferta_page()
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

    def do_POST(self) -> None:
        path = self._path_only()
        if path == "/hooks/posthog-issues":
            status, body = _handle_posthog_issue_post(self)
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

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
    # Threading so OAuth Twitch calls cannot block /health.
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
        name="health-server",
    )
    thread.start()
