"""Twitch stream chat Mini App: auth, online list, resolve, send limits."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse

from config import PUBLIC_BASE_URL, TELEGRAM_BOT_TOKEN
from premium import CHAT_FREE_DAILY_SEND_LIMIT, chat_daily_send_limit

logger = logging.getLogger(__name__)

BETA_FEATURE_ID = "stream-chat"
WEBAPP_DIR = Path(__file__).resolve().parent / "webapp" / "chat"
_INIT_DATA_MAX_AGE_SEC = 86_400
_WEBAPP_TOKEN_TTL_SEC = 7 * 86_400
_STATIC_NAMES = frozenset({"index.html", "app.js", "style.css"})
# Pre-resolved paths only — never join user input onto WEBAPP_DIR at call time.
_STATIC_PATHS: dict[str, Path] = {
    name: (WEBAPP_DIR / name).resolve() for name in _STATIC_NAMES
}

_db: Any = None
_twitch: Any = None


def register_chat_webapp(*, db: Any, twitch: Any) -> None:
    global _db, _twitch
    _db = db
    _twitch = twitch


def make_webapp_token(
    user_id: int,
    *,
    lang: str | None = None,
    ttl_sec: int = _WEBAPP_TOKEN_TTL_SEC,
) -> str:
    """Short-lived HMAC token so the Mini App works even if initData is empty."""
    exp = int(time.time()) + max(60, int(ttl_sec))
    locale = lang if lang in ("en", "ru") else ""
    msg = f"{int(user_id)}:{exp}:{locale}" if locale else f"{int(user_id)}:{exp}"
    sig = hmac.new(
        TELEGRAM_BOT_TOKEN.encode("utf-8"),
        msg.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]
    return f"{msg}:{sig}"


def parse_webapp_token(token: str) -> tuple[int | None, str | None]:
    """Return (user_id, lang) from a signed Mini App token."""
    raw = (token or "").strip()
    if not raw or not TELEGRAM_BOT_TOKEN:
        return None, None
    parts = raw.split(":")
    if len(parts) not in (3, 4):
        return None, None
    uid_s, exp_s = parts[0], parts[1]
    if len(parts) == 3:
        sig = parts[2]
        locale = None
        msg = f"{uid_s}:{exp_s}"
    else:
        locale_raw, sig = parts[2], parts[3]
        locale = locale_raw if locale_raw in ("en", "ru") else None
        msg = f"{uid_s}:{exp_s}:{locale_raw}" if locale_raw else f"{uid_s}:{exp_s}"
    try:
        user_id = int(uid_s)
        exp = int(exp_s)
    except ValueError:
        return None, None
    if user_id <= 0 or exp < int(time.time()):
        return None, None
    expect = hmac.new(
        TELEGRAM_BOT_TOKEN.encode("utf-8"),
        msg.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]
    if not hmac.compare_digest(expect, sig):
        return None, None
    return user_id, locale


def validate_webapp_token(token: str) -> int | None:
    user_id, _ = parse_webapp_token(token)
    return user_id


def chat_webapp_url(*, lang: str | None = None, user_id: int | None = None) -> str:
    if not PUBLIC_BASE_URL:
        return ""
    base = f"{PUBLIC_BASE_URL}/app/chat/"
    params: dict[str, str] = {}
    if lang in ("en", "ru"):
        params["lang"] = lang
    if user_id is not None:
        params["t"] = make_webapp_token(int(user_id), lang=lang)
    if not params:
        return base
    return f"{base}?{urlencode(params)}"


def alert_chat_button_url(
    *, login: str, lang: str | None = None, user_id: int | None = None
) -> str:
    if not PUBLIC_BASE_URL:
        return ""
    login = (login or "").strip().lower()
    if not login:
        return ""
    base = f"{PUBLIC_BASE_URL}/app/chat/"
    params: dict[str, str] = {"login": login, "open": "1"}
    if lang in ("en", "ru"):
        params["lang"] = lang
    if user_id is not None:
        params["t"] = make_webapp_token(int(user_id), lang=lang)
    return f"{base}?{urlencode(params)}"


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
    # Newer Telegram clients also send signature; it is not part of the hash check.
    pairs.pop("signature", None)
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


def _require_user(
    *,
    init_data: str = "",
    token: str = "",
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    import beta as beta_features

    token_lang: str | None = None
    user_id: int | None = None
    if token:
        user_id, token_lang = parse_webapp_token(token)
    user: dict[str, Any] | None = None
    if user_id is None:
        user = validate_webapp_init_data(init_data)
        if user is None:
            if not (init_data or "").strip() and not (token or "").strip():
                logger.info("chat auth: empty initData and token")
                return None, "unauthorized_empty", None
            logger.info(
                "chat auth: rejected init_len=%s token_len=%s",
                len((init_data or "").strip()),
                len((token or "").strip()),
            )
            return None, "unauthorized", None
        user_id = int(user["id"])
    else:
        user = {"id": user_id}
    if _db is None:
        return None, "unavailable", None
    if not beta_features.is_enabled(_db, user_id, BETA_FEATURE_ID):
        return None, "beta_required", None
    if token_lang:
        user["lang"] = token_lang
    return user, None, token_lang


def _subscription_uids(subs: list[Any]) -> dict[str, Any]:
    """Map twitch user_id -> subscription, resolving missing ids by login."""
    by_uid: dict[str, Any] = {}
    missing: list[Any] = []
    for sub in subs:
        uid = str(getattr(sub, "twitch_user_id", "") or "").strip()
        if uid:
            by_uid[uid] = sub
        elif getattr(sub, "twitch_username", ""):
            missing.append(sub)
    if not missing or _twitch is None or _db is None:
        return by_uid
    logins = list({str(s.twitch_username).lower() for s in missing})
    try:
        profiles = _twitch.get_users_by_login(logins)
    except Exception:
        logger.exception("chat resolve subscription logins failed")
        return by_uid
    for sub in missing:
        profile = profiles.get(str(sub.twitch_username).lower())
        if not profile:
            continue
        uid = str(profile.get("id") or "")
        if not uid:
            continue
        by_uid[uid] = sub
        try:
            _db.update_subscription(int(sub.id), int(sub.owner_id), twitch_user_id=uid)
        except Exception:
            logger.debug("chat backfill twitch_user_id failed sub=%s", sub.id)
    return by_uid


def _utc_day() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _err(code: int, error: str) -> tuple[int, dict[str, Any]]:
    return code, {"ok": False, "error": error}


def _profile_image_url(profile: dict[str, Any] | None) -> str:
    if not profile:
        return ""
    return str(profile.get("profile_image_url") or "").strip()


def api_session(*, init_data: str = "", token: str = "") -> tuple[int, dict[str, Any]]:
    user, err, token_lang = _require_user(init_data=init_data, token=token)
    if err or user is None:
        code = 401 if (err or "").startswith("unauthorized") else 403
        return _err(code, err or "unauthorized")
    user_id = int(user["id"])
    lang = token_lang or _db.get_user_locale(user_id) or "en"
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


def api_online(*, init_data: str = "", token: str = "") -> tuple[int, dict[str, Any]]:
    user, err, _token_lang = _require_user(init_data=init_data, token=token)
    if err or user is None:
        code = 401 if (err or "").startswith("unauthorized") else 403
        return _err(code, err or "unauthorized")
    if _twitch is None:
        return _err(503, "unavailable")
    user_id = int(user["id"])
    import demo_mode

    try:
        demo = demo_mode.is_active(user_id)
        subs = [
            s
            for s in _db.get_subscriptions_by_owner(user_id)
            if s.enabled and bool(s.is_demo) == demo and s.twitch_username
        ]
        if not subs:
            return 200, {"ok": True, "streams": [], "subscribed": 0, "live": 0}
        by_uid = _subscription_uids(subs)
        if not by_uid:
            return 200, {"ok": True, "streams": [], "subscribed": 0, "live": 0}
        live = _twitch.get_live_streams(list(by_uid.keys()))
        streams: list[dict[str, Any]] = []
        logins: list[str] = []
        for uid, stream in live.items():
            sub = by_uid.get(str(uid))
            login = (
                stream.get("user_login") or (sub.twitch_username if sub else "") or ""
            ).lower()
            if login:
                logins.append(login)
            streams.append(
                {
                    "login": login,
                    "display_name": stream.get("user_name")
                    or (sub.twitch_username if sub else login),
                    "title": stream.get("title") or "",
                    "game_name": stream.get("game_name") or "",
                    "viewer_count": int(stream.get("viewer_count") or 0),
                    "twitch_user_id": str(uid),
                }
            )
        profiles = _twitch.get_users_by_login(logins) if logins else {}
        for item in streams:
            profile = profiles.get(str(item.get("login") or "").lower())
            item["profile_image_url"] = _profile_image_url(profile)
        streams.sort(key=lambda s: (-int(s["viewer_count"]), str(s["login"])))
        return 200, {
            "ok": True,
            "streams": streams,
            "subscribed": len(by_uid),
            "live": len(streams),
        }
    except Exception:
        logger.exception("chat api_online failed user=%s", user_id)
        return _err(502, "twitch_error")


def api_resolve(
    *, init_data: str = "", token: str = "", query: str = ""
) -> tuple[int, dict[str, Any]]:
    user, err, _token_lang = _require_user(init_data=init_data, token=token)
    if err or user is None:
        code = 401 if (err or "").startswith("unauthorized") else 403
        return _err(code, err or "unauthorized")
    if _twitch is None:
        return _err(503, "unavailable")
    login = _twitch.parse_username(query or "")
    if not login:
        return _err(400, "bad_query")
    try:
        profile = _twitch.get_user(login)
        if not profile:
            return _err(404, "not_found")
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
            "profile_image_url": _profile_image_url(profile),
        }
    except Exception:
        logger.exception("chat api_resolve failed login=%s", login)
        return _err(502, "twitch_error")


def api_info(
    *, init_data: str = "", token: str = "", query: str = ""
) -> tuple[int, dict[str, Any]]:
    user, err, _token_lang = _require_user(init_data=init_data, token=token)
    if err or user is None:
        code = 401 if (err or "").startswith("unauthorized") else 403
        return _err(code, err or "unauthorized")
    if _twitch is None:
        return _err(503, "unavailable")
    login = _twitch.parse_username(query or "")
    if not login:
        return _err(400, "bad_query")
    try:
        profile = _twitch.get_user(login)
        if not profile:
            return _err(404, "not_found")
        links = _twitch.get_channel_about_links(login)
        return 200, {
            "ok": True,
            "login": str(profile.get("login") or login).lower(),
            "display_name": profile.get("display_name") or profile.get("login") or login,
            "profile_image_url": _profile_image_url(profile),
            "links": links,
        }
    except Exception:
        logger.exception("chat api_info failed login=%s", login)
        return _err(502, "twitch_error")


def api_oauth_url(*, init_data: str = "", token: str = "") -> tuple[int, dict[str, Any]]:
    from config import twitch_oauth_redirect_uri
    from health import create_oauth_state
    from twitch import CHAT_OAUTH_SCOPES

    user, err, token_lang = _require_user(init_data=init_data, token=token)
    if err or user is None:
        code = 401 if (err or "").startswith("unauthorized") else 403
        return _err(code, err or "unauthorized")
    if _twitch is None:
        return _err(503, "unavailable")
    redirect = twitch_oauth_redirect_uri()
    if not redirect:
        return _err(503, "oauth_unavailable")
    user_id = int(user["id"])
    lang = token_lang or _db.get_user_locale(user_id) or "en"
    state = create_oauth_state(user_id, lang, purpose="chat")
    url = _twitch.build_authorize_url(
        redirect_uri=redirect, state=state, scopes=CHAT_OAUTH_SCOPES
    )
    return 200, {"ok": True, "url": url}


def api_send(
    *,
    init_data: str = "",
    token: str = "",
    broadcaster_login: str = "",
    message: str = "",
) -> tuple[int, dict[str, Any]]:
    user, err, _token_lang = _require_user(init_data=init_data, token=token)
    if err or user is None:
        code = 401 if (err or "").startswith("unauthorized") else 403
        return _err(code, err or "unauthorized")
    if _twitch is None:
        return _err(503, "unavailable")
    user_id = int(user["id"])
    text = (message or "").strip()
    if not text or len(text) > 500:
        return _err(400, "bad_message")
    login = _twitch.parse_username(broadcaster_login or "")
    if not login:
        return _err(400, "bad_channel")
    auth = _db.get_chat_auth(user_id)
    if not auth or not auth.refresh_token:
        return _err(401, "twitch_auth_required")
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
        return _err(404, "not_found")
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
        return _err(502, "send_failed")
    new_count = _db.increment_chat_send_count(user_id, day)
    remaining = None if limit is None else max(0, int(limit) - new_count)
    return 200, {
        "ok": True,
        "sent_today": new_count,
        "remaining": remaining,
        "unlimited": limit is None,
    }


def static_file(name: str) -> tuple[bytes, str] | None:
    path = _STATIC_PATHS.get(name)
    if path is None or not path.is_file():
        return None
    data = path.read_bytes()
    if name.endswith(".js"):
        ctype = "application/javascript; charset=utf-8"
    elif name.endswith(".css"):
        ctype = "text/css; charset=utf-8"
    else:
        ctype = "text/html; charset=utf-8"
    return data, ctype
