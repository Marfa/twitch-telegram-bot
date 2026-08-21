"""Twitch stream chat Mini App: initData auth, online list, resolve, send limits."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse

from config import PUBLIC_BASE_URL, TELEGRAM_BOT_TOKEN
from premium import CHAT_FREE_DAILY_SEND_LIMIT, chat_daily_send_limit

logger = logging.getLogger(__name__)

BETA_FEATURE_ID = "stream-chat"
WEBAPP_DIR = Path(__file__).resolve().parent / "webapp" / "chat"
_INIT_DATA_MAX_AGE_SEC = 86_400
_STATIC_NAMES = frozenset({"index.html", "app.js", "style.css"})

_db: Any = None
_twitch: Any = None


def register_chat_webapp(*, db: Any, twitch: Any) -> None:
    global _db, _twitch
    _db = db
    _twitch = twitch


def chat_webapp_url() -> str:
    if not PUBLIC_BASE_URL:
        return ""
    return f"{PUBLIC_BASE_URL}/app/chat/"


def embed_parent_host() -> str:
    if not PUBLIC_BASE_URL:
        return ""
    host = urlparse(PUBLIC_BASE_URL).hostname or ""
    return host.lower()


def validate_webapp_init_data(init_data: str) -> dict[str, Any] | None:
    """Validate Telegram WebApp initData; returns parsed user dict or None."""
    raw = (init_data or "").strip()
    if not raw or not TELEGRAM_BOT_TOKEN:
        return None
    pairs = dict(parse_qsl(raw, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None
    check_list = [f"{k}={v}" for k, v in sorted(pairs.items())]
    data_check_string = "\n".join(check_list)
    secret_key = hmac.new(
        b"WebAppData", TELEGRAM_BOT_TOKEN.encode("utf-8"), hashlib.sha256
    ).digest()
    calculated = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        return None
    try:
        auth_date = int(pairs.get("auth_date") or "0")
    except ValueError:
        return None
    if auth_date <= 0 or abs(int(time.time()) - auth_date) > _INIT_DATA_MAX_AGE_SEC:
        return None
    user_raw = pairs.get("user") or ""
    try:
        user = json.loads(user_raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(user, dict) or "id" not in user:
        return None
    try:
        user["id"] = int(user["id"])
    except (TypeError, ValueError):
        return None
    return user


def _require_user(init_data: str) -> tuple[dict[str, Any] | None, str | None]:
    import beta as beta_features

    user = validate_webapp_init_data(init_data)
    if user is None:
        return None, "unauthorized"
    if _db is None:
        return None, "unavailable"
    user_id = int(user["id"])
    if not beta_features.is_enabled(_db, user_id, BETA_FEATURE_ID):
        return None, "beta_required"
    return user, None


def _utc_day() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def api_session(init_data: str) -> tuple[int, dict[str, Any]]:
    user, err = _require_user(init_data)
    if err or user is None:
        code = 401 if err == "unauthorized" else 403
        return code, {"ok": False, "error": err or "unauthorized"}
    user_id = int(user["id"])
    lang = _db.get_user_locale(user_id) or "en"
    auth = _db.get_chat_auth(user_id)
    limit = chat_daily_send_limit(_db, user_id)
    sent = _db.get_chat_send_count(user_id, _utc_day())
    unlimited = limit is None
    remaining = None if unlimited else max(0, int(limit) - sent)
    return 200, {
        "ok": True,
        "user_id": user_id,
        "lang": lang,
        "twitch_linked": bool(auth and auth.twitch_user_id),
        "twitch_login": (auth.twitch_login if auth else "") or "",
        "unlimited": unlimited,
        "daily_limit": limit if limit is not None else CHAT_FREE_DAILY_SEND_LIMIT,
        "sent_today": sent,
        "remaining": remaining,
        "prefer_embed": unlimited,
        "embed_parent": embed_parent_host(),
    }


def api_online(init_data: str) -> tuple[int, dict[str, Any]]:
    user, err = _require_user(init_data)
    if err or user is None:
        code = 401 if err == "unauthorized" else 403
        return code, {"ok": False, "error": err or "unauthorized"}
    if _twitch is None:
        return 503, {"ok": False, "error": "unavailable"}
    user_id = int(user["id"])
    import demo_mode

    demo = demo_mode.is_active(user_id)
    subs = [
        s
        for s in _db.get_subscriptions_by_owner(user_id)
        if s.enabled and bool(s.is_demo) == demo and s.twitch_user_id
    ]
    if not subs:
        return 200, {"ok": True, "streams": []}
    by_uid = {s.twitch_user_id: s for s in subs}
    live = _twitch.get_live_streams(list(by_uid.keys()))
    streams: list[dict[str, Any]] = []
    for uid, stream in live.items():
        sub = by_uid.get(uid)
        login = (stream.get("user_login") or (sub.twitch_username if sub else "") or "").lower()
        streams.append(
            {
                "login": login,
                "display_name": stream.get("user_name")
                or (sub.twitch_username if sub else login),
                "title": stream.get("title") or "",
                "game_name": stream.get("game_name") or "",
                "viewer_count": int(stream.get("viewer_count") or 0),
                "twitch_user_id": uid,
            }
        )
    streams.sort(key=lambda s: (-int(s["viewer_count"]), str(s["login"])))
    return 200, {"ok": True, "streams": streams}


def api_resolve(init_data: str, query: str) -> tuple[int, dict[str, Any]]:
    user, err = _require_user(init_data)
    if err or user is None:
        code = 401 if err == "unauthorized" else 403
        return code, {"ok": False, "error": err or "unauthorized"}
    if _twitch is None:
        return 503, {"ok": False, "error": "unavailable"}
    login = _twitch.parse_username(query or "")
    if not login:
        return 400, {"ok": False, "error": "bad_query"}
    profile = _twitch.get_user(login)
    if not profile:
        return 404, {"ok": False, "error": "not_found"}
    uid = str(profile["id"])
    live_map = _twitch.get_live_streams([uid])
    stream = live_map.get(uid)
    online = stream is not None
    return 200, {
        "ok": True,
        "login": str(profile.get("login") or login).lower(),
        "display_name": profile.get("display_name") or profile.get("login") or login,
        "twitch_user_id": uid,
        "online": online,
        "title": (stream or {}).get("title") or "",
        "game_name": (stream or {}).get("game_name") or "",
        "viewer_count": int((stream or {}).get("viewer_count") or 0),
    }


def api_oauth_url(init_data: str) -> tuple[int, dict[str, Any]]:
    from config import twitch_oauth_redirect_uri
    from health import create_oauth_state
    from twitch import CHAT_OAUTH_SCOPES

    user, err = _require_user(init_data)
    if err or user is None:
        code = 401 if err == "unauthorized" else 403
        return code, {"ok": False, "error": err or "unauthorized"}
    if _twitch is None:
        return 503, {"ok": False, "error": "unavailable"}
    redirect = twitch_oauth_redirect_uri()
    if not redirect:
        return 503, {"ok": False, "error": "oauth_unavailable"}
    user_id = int(user["id"])
    lang = _db.get_user_locale(user_id) or "en"
    state = create_oauth_state(user_id, lang, purpose="chat")
    url = _twitch.build_authorize_url(
        redirect_uri=redirect, state=state, scopes=CHAT_OAUTH_SCOPES
    )
    return 200, {"ok": True, "url": url}


def api_send(
    init_data: str, *, broadcaster_login: str, message: str
) -> tuple[int, dict[str, Any]]:
    user, err = _require_user(init_data)
    if err or user is None:
        code = 401 if err == "unauthorized" else 403
        return code, {"ok": False, "error": err or "unauthorized"}
    if _twitch is None:
        return 503, {"ok": False, "error": "unavailable"}
    user_id = int(user["id"])
    text = (message or "").strip()
    if not text or len(text) > 500:
        return 400, {"ok": False, "error": "bad_message"}
    login = _twitch.parse_username(broadcaster_login or "")
    if not login:
        return 400, {"ok": False, "error": "bad_channel"}
    auth = _db.get_chat_auth(user_id)
    if not auth or not auth.refresh_token:
        return 401, {"ok": False, "error": "twitch_auth_required"}
    limit = chat_daily_send_limit(_db, user_id)
    day = _utc_day()
    sent = _db.get_chat_send_count(user_id, day)
    if limit is not None and sent >= limit:
        return 429, {
            "ok": False,
            "error": "daily_limit",
            "daily_limit": limit,
            "sent_today": sent,
            "remaining": 0,
        }
    broadcaster = _twitch.get_user(login)
    if not broadcaster:
        return 404, {"ok": False, "error": "not_found"}
    try:
        token_data = _twitch.refresh_user_token(auth.refresh_token)
        access = token_data.get("access_token") or ""
        new_refresh = token_data.get("refresh_token") or auth.refresh_token
        if new_refresh and new_refresh != auth.refresh_token:
            _db.upsert_chat_auth(
                user_id,
                twitch_user_id=auth.twitch_user_id,
                twitch_login=auth.twitch_login,
                refresh_token=new_refresh,
            )
        if not access:
            raise RuntimeError("no_access")
        _twitch.send_chat_message(
            access,
            broadcaster_id=str(broadcaster["id"]),
            sender_id=auth.twitch_user_id,
            message=text,
        )
    except Exception as exc:
        logger.warning("chat send failed user=%s: %s", user_id, exc)
        return 502, {"ok": False, "error": "send_failed"}
    new_count = _db.increment_chat_send_count(user_id, day)
    remaining = None if limit is None else max(0, int(limit) - new_count)
    return 200, {
        "ok": True,
        "sent_today": new_count,
        "remaining": remaining,
        "unlimited": limit is None,
    }


def static_file(name: str) -> tuple[bytes, str] | None:
    if name not in _STATIC_NAMES:
        return None
    path = WEBAPP_DIR / name
    if not path.is_file():
        return None
    data = path.read_bytes()
    if name.endswith(".js"):
        ctype = "application/javascript; charset=utf-8"
    elif name.endswith(".css"):
        ctype = "text/css; charset=utf-8"
    else:
        ctype = "text/html; charset=utf-8"
    return data, ctype
