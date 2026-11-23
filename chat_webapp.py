"""Twitch stream chat Mini App: auth, online list, resolve, send."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import random
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, WebAppInfo

from config import PUBLIC_BASE_URL, TELEGRAM_BOT_TOKEN
from i18n import SUPPORTED_LOCALES
from premium import get_status, list_promo_channel_logins

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
    locale = lang if lang in SUPPORTED_LOCALES else ""
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
        locale = locale_raw if locale_raw in SUPPORTED_LOCALES else None
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
    if lang in SUPPORTED_LOCALES:
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
    if lang in SUPPORTED_LOCALES:
        params["lang"] = lang
    if user_id is not None:
        params["t"] = make_webapp_token(int(user_id), lang=lang)
    return f"{base}?{urlencode(params)}"


def stream_chat_open_markup(lang: str, url: str, *, private: bool) -> InlineKeyboardMarkup:
    """Inline open-chat button. web_app only works in private chats with the bot."""
    from i18n import dm_main_menu_button, t

    label = t("menu_btn_chat", lang)
    if private:
        rows = [
            [InlineKeyboardButton(label, web_app=WebAppInfo(url=url))],
            [dm_main_menu_button(lang)],
        ]
    else:
        rows = [[InlineKeyboardButton(label, url=url)]]
    return InlineKeyboardMarkup(rows)


def stream_chat_reply_button(lang: str) -> KeyboardButton:
    """Reply-keyboard Chat: WebApp opens Mini App with no bot message; else plain text."""
    from i18n import btn

    label = btn("chat", lang)
    url = chat_webapp_url(lang=lang if lang in SUPPORTED_LOCALES else None)
    if url:
        return KeyboardButton(label, web_app=WebAppInfo(url=url))
    return KeyboardButton(label)


def stream_chat_menu_inline_button(
    lang: str, *, fallback_callback: str = "new_sub_other:chat"
) -> InlineKeyboardButton:
    """Inline Chat for menus: WebApp when URL known, else callback fallback."""
    from i18n import btn

    label = btn("chat", lang)
    url = chat_webapp_url(lang=lang if lang in SUPPORTED_LOCALES else None)
    if url:
        return InlineKeyboardButton(label, web_app=WebAppInfo(url=url))
    return InlineKeyboardButton(label, callback_data=fallback_callback)


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
        profiles = _cached_users_by_login(logins)
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



def _err(code: int, error: str) -> tuple[int, dict[str, Any]]:
    return code, {"ok": False, "error": error}


# One Mini App visit fetches /app/chat/api/online more than once: app.js calls it
# on boot(), again on every return to the app (visibilitychange), and once more
# when the player closes (closeChat()). Each call spends up to 5 Helix points
# from the app-token bucket that stream polling also uses, so a read reuses the
# answer of the previous one inside this window. Stream polling keeps its own
# uncached calls.
_READ_CACHE_TTL_SEC = 20.0
_READ_CACHE_MAX_ENTRIES = 512
_users_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_live_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}


def _cache_get(cache: dict[str, Any], key: str) -> tuple[bool, Any]:
    hit = cache.get(key)
    if hit is None:
        return False, None
    if time.monotonic() >= hit[0]:
        # Handlers run in parallel threads, so another read can drop the entry
        # between the lookup above and this removal.
        cache.pop(key, None)
        return False, None
    return True, hit[1]


def _cache_put(cache: dict[str, Any], key: str, value: Any) -> None:
    # A Mini App query is caller-controlled, so the cache keeps a fixed size.
    if len(cache) >= _READ_CACHE_MAX_ENTRIES:
        cache.clear()
    cache[key] = (time.monotonic() + _READ_CACHE_TTL_SEC, value)


def _cached_users_by_login(logins: list[str]) -> dict[str, dict[str, Any]]:
    """`get_users_by_login` for a Mini App read, reused inside the cache window."""
    wanted: list[str] = []
    seen: set[str] = set()
    for raw in logins:
        login = (raw or "").strip().lower()
        if login and login not in seen:
            seen.add(login)
            wanted.append(login)
    if not wanted or _twitch is None:
        return {}
    out: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for login in wanted:
        found, profile = _cache_get(_users_cache, login)
        if found:
            out[login] = dict(profile)
        else:
            missing.append(login)
    if missing:
        fetched = _twitch.get_users_by_login(missing)
        for login in missing:
            profile = fetched.get(login)
            if not profile:
                continue
            out[login] = dict(profile)
            _cache_put(_users_cache, login, dict(profile))
    return out


def _cached_live_streams(user_ids: list[str]) -> dict[str, dict[str, Any]]:
    """`get_live_streams` for a Mini App read; an offline answer is reused too."""
    wanted: list[str] = []
    seen: set[str] = set()
    for raw in user_ids:
        uid = str(raw or "").strip()
        if uid and uid not in seen:
            seen.add(uid)
            wanted.append(uid)
    if not wanted or _twitch is None:
        return {}
    out: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for uid in wanted:
        found, stream = _cache_get(_live_cache, uid)
        if not found:
            missing.append(uid)
        elif stream is not None:
            out[uid] = dict(stream)
    if missing:
        live = _twitch.get_live_streams(missing)
        for uid in missing:
            stream = live.get(uid)
            if stream is not None:
                out[uid] = dict(stream)
            _cache_put(_live_cache, uid, dict(stream) if stream is not None else None)
    return out


# Twitch allows one account 20 chat messages per 30 s and restricts the account
# for an hour above that. The relay keeps the same window, so one client loop
# cannot push the linked account past Twitch's own limit or spend the shared
# app-token bucket that stream polling uses.
_CHAT_SEND_LIMIT = 20
_CHAT_SEND_WINDOW_SEC = 30.0


def _chat_send_allowed(user_id: int) -> bool:
    """Record one relay send for the Telegram user; False when over the window."""
    from health import _http_rate_limit_allow

    return _http_rate_limit_allow(
        f"chat_send:{int(user_id)}",
        limit=_CHAT_SEND_LIMIT,
        window_sec=_CHAT_SEND_WINDOW_SEC,
    )


# The read endpoints spend Helix points from the shared app-token bucket that
# stream polling also uses: api_online costs 5 without the full plan (2 with
# it), api_resolve costs 2. Bound them per Telegram user and across all Mini App
# users, so one client loop cannot stall alert polling. Both shed before any
# Helix call.
_CHAT_READ_LIMIT = 20
_CHAT_READ_WINDOW_SEC = 60.0
_CHAT_READ_GLOBAL_LIMIT = 60
_CHAT_READ_GLOBAL_WINDOW_SEC = 60.0


def _chat_read_allowed(user_id: int, *, path: str) -> bool:
    """Record one Mini App read; False when its user or the shared window is full."""
    from health import _http_rate_limits_admit, _note_http_rate_limited

    rejected = _http_rate_limits_admit(
        [
            (f"chat_read:{int(user_id)}", _CHAT_READ_LIMIT, _CHAT_READ_WINDOW_SEC),
            (
                "chat_read:shared",
                _CHAT_READ_GLOBAL_LIMIT,
                _CHAT_READ_GLOBAL_WINDOW_SEC,
            ),
        ]
    )
    if rejected is None:
        return True
    if rejected == "chat_read:shared":
        _note_http_rate_limited(
            "chat_read_shared", path, _CHAT_READ_GLOBAL_WINDOW_SEC
        )
    else:
        _note_http_rate_limited("chat_read", path, _CHAT_READ_WINDOW_SEC)
    return False


def _profile_image_url(profile: dict[str, Any] | None) -> str:
    if not profile:
        return ""
    return str(profile.get("profile_image_url") or "").strip()


def _stream_cards_from_live(
    live: dict[str, dict[str, Any]],
    *,
    by_uid: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    streams: list[dict[str, Any]] = []
    logins: list[str] = []
    for uid, stream in live.items():
        sub = (by_uid or {}).get(str(uid))
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
    profiles = _cached_users_by_login(logins) if logins else {}
    for item in streams:
        profile = profiles.get(str(item.get("login") or "").lower())
        item["profile_image_url"] = _profile_image_url(profile)
    streams.sort(key=lambda s: (-int(s["viewer_count"]), str(s["login"])))
    return streams


_OTHER_PROMO_STREAMS_N = 5


def _other_promo_streams(user_id: int, *, sub_logins: set[str]) -> list[dict[str, Any]]:
    """Up to 5 random live Premium channels not in the user's subscriptions (no full plan)."""
    if _twitch is None or _db is None:
        return []
    if get_status(_db, user_id).has_full_plan:
        return []
    candidates = [
        login
        for login in list_promo_channel_logins(_db)
        if login and login not in sub_logins
    ]
    if not candidates:
        return []
    try:
        profiles = _cached_users_by_login(candidates)
    except Exception:
        logger.exception("chat other promo resolve failed user=%s", user_id)
        return []
    uids = [str(p["id"]) for p in profiles.values() if p.get("id")]
    if not uids:
        return []
    try:
        live = _cached_live_streams(uids)
    except Exception:
        logger.exception("chat other promo live failed user=%s", user_id)
        return []
    if not live:
        return []
    picked_uids = random.sample(list(live.keys()), min(_OTHER_PROMO_STREAMS_N, len(live)))
    return _stream_cards_from_live({uid: live[uid] for uid in picked_uids})


def api_session(*, init_data: str = "", token: str = "") -> tuple[int, dict[str, Any]]:
    user, err, token_lang = _require_user(init_data=init_data, token=token)
    if err or user is None:
        code = 401 if (err or "").startswith("unauthorized") else 403
        return _err(code, err or "unauthorized")
    user_id = int(user["id"])
    lang = token_lang or _db.get_user_locale(user_id) or "en"
    auth = _db.get_chat_auth(user_id)
    return 200, {
        "ok": True,
        "user_id": user_id,
        "lang": lang,
        "twitch_linked": bool(auth and auth.twitch_user_id),
        "twitch_login": (auth.twitch_login if auth else "") or "",
        "unlimited": True,
        "daily_limit": None,
        "sent_today": 0,
        "remaining": None,
        "prefer_embed": True,
        "show_other_streams": not get_status(_db, user_id).has_full_plan,
        "promo_channels": list_promo_channel_logins(_db),
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
    if not _chat_read_allowed(user_id, path="/app/chat/api/online"):
        code, payload = _err(429, "rate_limited")
        payload["retry_after"] = int(_CHAT_READ_WINDOW_SEC)
        return code, payload
    import demo_mode

    try:
        demo = demo_mode.is_active(user_id)
        owner_subs = [
            s
            for s in _db.get_subscriptions_by_owner(user_id)
            if bool(s.is_demo) == demo and s.twitch_username
        ]
        sub_logins = {str(s.twitch_username).lower() for s in owner_subs}
        subs = [s for s in owner_subs if s.enabled]
        streams: list[dict[str, Any]] = []
        subscribed = 0
        if subs:
            by_uid = _subscription_uids(subs)
            subscribed = len(by_uid)
            if by_uid:
                live = _cached_live_streams(list(by_uid.keys()))
                streams = _stream_cards_from_live(live, by_uid=by_uid)
        other_streams = _other_promo_streams(user_id, sub_logins=sub_logins)
        return 200, {
            "ok": True,
            "streams": streams,
            "other_streams": other_streams,
            "subscribed": subscribed,
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
    if not _chat_read_allowed(int(user["id"]), path="/app/chat/api/resolve"):
        code, payload = _err(429, "rate_limited")
        payload["retry_after"] = int(_CHAT_READ_WINDOW_SEC)
        return code, payload
    login = _twitch.parse_username(query or "")
    if not login:
        return _err(400, "bad_query")
    try:
        profile = _cached_users_by_login([login]).get(login.lower())
        if not profile:
            return _err(404, "not_found")
        uid = str(profile["id"])
        live_map = _cached_live_streams([uid])
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


def api_oauth_url(*, init_data: str = "", token: str = "") -> tuple[int, dict[str, Any]]:
    from config import twitch_oauth_redirect_uri
    from health import create_pending_login_state

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
    state = create_pending_login_state(user_id, lang, purpose="chat")
    url = _twitch.build_authorize_url(
        redirect_uri=redirect, state=state, force_verify=True
    )
    from bot_helpers import oauth_legal_suffix

    return 200, {
        "ok": True,
        "url": url,
        "privacy_notice": oauth_legal_suffix(lang).strip(),
    }


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
    if not _chat_send_allowed(user_id):
        code, payload = _err(429, "rate_limited")
        payload["retry_after"] = int(_CHAT_SEND_WINDOW_SEC)
        from health import _http_rate_limit_allow

        if _http_rate_limit_allow(
            f"chat_send_log:{user_id}",
            limit=1,
            window_sec=_CHAT_SEND_WINDOW_SEC,
        ):
            logger.warning(
                "chat send rate limited user=%s channel=%s", user_id, login
            )
        return code, payload
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
    return 200, {
        "ok": True,
        "sent_today": 0,
        "remaining": None,
        "unlimited": True,
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
