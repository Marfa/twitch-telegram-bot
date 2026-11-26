"""Thin MAX Messenger bot: updates webhook + outbound-alert ingest.

Phase 3–4: DM + group/channel destinations, link codes, unlink, send via Max API.
Never log tokens, update secrets, or per-dest webhook secrets.
"""
from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from db.protocol import Database
from outbound_webhook import (
    DELIVERY_ID_HEADER,
    SIGNATURE_HEADER,
    generate_secret,
    verify_signature,
)
from token_crypto import encrypt_secret, try_decrypt_secret

import max_api

logger = logging.getLogger(__name__)

MAX_UPDATES_PATH = "/hooks/max/updates"
MAX_INGEST_PREFIX = "/hooks/max/d/"
LINK_CODE_TTL = timedelta(minutes=30)
_MSG_MIN_INTERVAL = 0.55  # ≤2 msg/s per peer
_BODY_MAX = 256_000
_PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_CMD_RE = re.compile(r"^/([a-zA-Z_]+)(?:@\S+)?(?:\s+(.*))?$", re.DOTALL)

_UPDATE_TYPES = [
    "bot_started",
    "message_created",
    "message_callback",
    "bot_added",
    "bot_removed",
    "bot_admin_permissions_changed",
]

# Onboarding callback payloads (inline_keyboard type=callback).
_CB_MENU = "ob:menu"
_CB_HAVE = "ob:have"
_CB_NEW = "ob:new"
_CB_CODE = "ob:code"
_CB_LIST = "ob:list"
_CB_GROUP = "ob:group"

_db: Database | None = None
_throttle_lock = threading.Lock()
_last_send_at: dict[str, float] = {}


def configure(db: Database) -> None:
    global _db
    _db = db


def _require_db() -> Database:
    if _db is None:
        raise RuntimeError("max_bot.configure(db) not called")
    return _db


def updates_callback_url() -> str:
    from config import PUBLIC_BASE_URL

    base = (PUBLIC_BASE_URL or "").rstrip("/")
    if not base:
        return ""
    return f"{base}{MAX_UPDATES_PATH}"


def ingest_url_for(public_id: str) -> str:
    from config import PUBLIC_BASE_URL

    base = (PUBLIC_BASE_URL or "").rstrip("/")
    pid = (public_id or "").strip()
    if not base or not pid:
        return ""
    return f"{base}{MAX_INGEST_PREFIX}{pid}"


def updates_secret() -> str:
    from config import MAX_UPDATES_SECRET

    return (MAX_UPDATES_SECRET or "").strip()


def _bot_commands_payload(*, lang: str = "ru") -> list[dict[str, str]]:
    """Slash hints for Max ``/`` menu (API has no per-locale list — use ``lang``)."""
    from i18n import t

    return [
        {"name": name, "description": t(desc_key, lang)[:128]}
        for name, desc_key in (
            ("start", "max_cmd_start_desc"),
            ("link", "max_cmd_link_desc"),
            ("list", "max_cmd_list_desc"),
            ("unlink", "max_cmd_unlink_desc"),
            ("help", "max_cmd_help_desc"),
        )
    ]


def ensure_max_bot_commands() -> bool:
    """Register Max slash commands so ``/`` shows the hint list. Returns True if ok."""
    from config import MAX_BOT_TOKEN

    if not (MAX_BOT_TOKEN or "").strip():
        return False
    try:
        max_api.set_my_commands(_bot_commands_payload(lang="ru"))
        logger.info("Max bot commands registered")
        return True
    except max_api.MaxApiError:
        logger.exception("Max set_my_commands failed")
        return False


def bootstrap_max_subscription() -> bool:
    """Ensure Max delivers updates to our HTTPS endpoint. Returns True if ok/skipped."""
    from config import MAX_BOT_TOKEN

    if not (MAX_BOT_TOKEN or "").strip():
        logger.info("MAX_BOT_TOKEN unset — skip Max subscription bootstrap")
        return False
    # Slash menu is independent of webhook subscription — always refresh.
    ensure_max_bot_commands()
    secret = updates_secret()
    if not secret:
        logger.warning("MAX_UPDATES_SECRET unset — skip Max subscription bootstrap")
        return False
    url = updates_callback_url()
    if not url.startswith("https://"):
        logger.warning("PUBLIC_BASE_URL missing/invalid — skip Max subscription")
        return False
    try:
        existing = max_api.list_subscriptions()
    except max_api.MaxApiError:
        logger.exception("Max list_subscriptions failed")
        return False
    for sub in existing:
        sub_url = str(sub.get("url") or "").strip()
        if sub_url != url:
            continue
        types = sub.get("update_types")
        have = {str(t) for t in types} if isinstance(types, list) else set()
        if have and set(_UPDATE_TYPES).issubset(have):
            logger.info("Max subscription already registered")
            return True
        # Same URL but missing group events — replace.
        try:
            max_api.unsubscribe(url)
        except max_api.MaxApiError:
            logger.exception("Max unsubscribe before refresh failed")
            return False
        break
    try:
        max_api.subscribe(
            url,
            update_types=list(_UPDATE_TYPES),
            secret=secret,
        )
        logger.info("Max subscription registered")
        return True
    except max_api.MaxApiError:
        logger.exception("Max subscribe failed")
        return False


def handle_updates_post(
    *,
    headers: Any,
    body: bytes,
) -> tuple[int, bytes]:
    """Process Max platform Update webhook. Always return quickly."""
    secret = updates_secret()
    if not secret:
        return 503, b"not configured"
    got = ""
    try:
        got = str(headers.get("X-Max-Bot-Api-Secret") or "").strip()
    except Exception:
        got = ""
    if not got or got != secret:
        return 401, b"unauthorized"
    if len(body) > _BODY_MAX:
        return 413, b"payload too large"
    try:
        import json

        data = json.loads(body.decode("utf-8"))
    except Exception:
        return 400, b"bad json"
    if not isinstance(data, dict):
        return 400, b"bad json"
    try:
        _process_update(data)
    except Exception:
        logger.exception("Max update handler failed")
        # Still 200 so Max does not retry forever on app bugs.
    return 200, b"ok"


def handle_ingest_post(
    public_id: str,
    *,
    headers: Any,
    body: bytes,
) -> tuple[int, bytes]:
    """Accept our outbound webhook contract and fan-out to Max DM/group/channel."""
    db = _require_db()
    pid = (public_id or "").strip()
    if not _PUBLIC_ID_RE.fullmatch(pid):
        return 404, b"not found"
    dest = db.get_max_destination_by_public_id(pid)
    if dest is None or not dest.enabled:
        return 404, b"not found"
    if dest.kind not in ("dm", "chat", "channel"):
        return 404, b"not found"
    plain_secret = try_decrypt_secret(dest.webhook_secret)
    if plain_secret is None or plain_secret == "":
        return 500, b"secret error"
    sig = ""
    try:
        sig = str(headers.get(SIGNATURE_HEADER) or "").strip()
    except Exception:
        sig = ""
    if not verify_signature(body, plain_secret, sig):
        return 401, b"bad signature"
    if len(body) > _BODY_MAX:
        return 413, b"payload too large"
    try:
        import json

        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return 400, b"bad json"
    if not isinstance(payload, dict):
        return 400, b"bad json"
    delivery_id = str(payload.get("delivery_id") or "").strip()
    if not delivery_id:
        try:
            delivery_id = str(headers.get(DELIVERY_ID_HEADER) or "").strip()
        except Exception:
            delivery_id = ""
    if not delivery_id:
        return 400, b"missing delivery_id"
    if db.has_max_delivery(delivery_id):
        return 200, b"ok"
    if not db.mark_max_delivery_seen(delivery_id, dest.id):
        return 200, b"ok"
    try:
        deliver_alert_to_destination(dest, payload)
    except max_api.MaxApiError as exc:
        logger.warning("Max send failed status=%s", exc.status_code)
        if exc.status_code in (401, 403, 404, 410):
            return 502, b"send failed"
        if exc.status_code == 429:
            return 429, b"rate limited"
        return 502, b"send failed"
    except Exception:
        logger.exception("Max alert deliver failed")
        return 500, b"error"
    return 200, b"ok"


def _process_update(data: dict[str, Any]) -> None:
    update_type = str(data.get("update_type") or "").strip()
    if update_type == "bot_started":
        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        locale = str(data.get("user_locale") or "").strip()
        _onboard_user(user, locale=locale)
        return
    if update_type == "message_callback":
        _on_message_callback(data)
        return
    if update_type == "bot_added":
        _on_bot_added(data)
        return
    if update_type == "bot_removed":
        _on_bot_removed(data)
        return
    if update_type == "bot_admin_permissions_changed":
        _on_permissions_changed(data)
        return
    if update_type == "message_created":
        _on_message_created(data)
        return


def _on_bot_added(data: dict[str, Any]) -> None:
    try:
        chat_id = int(data.get("chat_id") or 0)
    except (TypeError, ValueError):
        chat_id = 0
    if chat_id == 0:
        return
    is_channel = bool(data.get("is_channel"))
    try:
        membership = max_api.get_chat_membership(chat_id)
    except max_api.MaxApiError as exc:
        logger.warning("Max members/me on bot_added failed status=%s", exc.status_code)
        membership = None
    if not max_api.bot_can_post(membership, is_channel=is_channel):
        logger.info("Max bot cannot post in chat_id=%s channel=%s", chat_id, is_channel)
        return
    text = (
        "Бот добавлен. Чтобы привязать оповещения из Telegram, напишите сюда /link\n"
        "(нужны права писать сообщения)."
        if not is_channel
        else "Бот добавлен в канал. Админ с правом писать посты: /link — код для Telegram."
    )
    try:
        _throttled_send(chat_id=chat_id, text=text)
    except max_api.MaxApiError as exc:
        logger.warning("Max bot_added welcome failed status=%s", exc.status_code)


def _on_bot_removed(data: dict[str, Any]) -> None:
    db = _require_db()
    try:
        chat_id = int(data.get("chat_id") or 0)
    except (TypeError, ValueError):
        chat_id = 0
    if chat_id == 0:
        return
    dest = db.get_max_destination_by_chat_id(chat_id)
    if dest is not None and dest.enabled:
        db.set_max_destination_enabled(dest.id, False)
        logger.info("Max dest disabled after bot_removed chat_id=%s", chat_id)


def _on_permissions_changed(data: dict[str, Any]) -> None:
    try:
        chat_id = int(data.get("chat_id") or 0)
    except (TypeError, ValueError):
        chat_id = 0
    if chat_id == 0:
        return
    is_channel = bool(data.get("is_channel"))
    db = _require_db()
    dest = db.get_max_destination_by_chat_id(chat_id)
    if dest is None:
        return
    try:
        membership = max_api.get_chat_membership(chat_id)
    except max_api.MaxApiError:
        return
    if not max_api.bot_can_post(membership, is_channel=is_channel):
        if dest.enabled:
            db.set_max_destination_enabled(dest.id, False)
            logger.info("Max dest paused — no write permission chat_id=%s", chat_id)


def _on_message_created(data: dict[str, Any]) -> None:
    message = data.get("message") if isinstance(data.get("message"), dict) else {}
    sender = message.get("sender") if isinstance(message.get("sender"), dict) else {}
    body = message.get("body") if isinstance(message.get("body"), dict) else {}
    recipient = (
        message.get("recipient") if isinstance(message.get("recipient"), dict) else {}
    )
    text = str(body.get("text") or "").strip()
    if not text:
        return
    chat_type = str(recipient.get("chat_type") or "").strip().lower()
    try:
        chat_id = int(recipient.get("chat_id") or 0)
    except (TypeError, ValueError):
        chat_id = 0
    locale = str(data.get("user_locale") or "").strip()

    if chat_type in ("", "dialog") or chat_id == 0:
        # DM with the bot
        if text.lower().startswith("/start"):
            _onboard_user(sender, locale=locale)
            return
        _handle_dm_command(sender, text, locale=locale)
        return

    # Group / channel
    cmd, arg = _parse_command(text)
    if cmd == "help":
        lang = _normalize_lang(locale)
        try:
            uid = int(sender.get("user_id") or 0)
        except (TypeError, ValueError):
            uid = 0
        text, attachments = _help_screen(lang, user_id=uid)
        _throttled_send(chat_id=chat_id, text=text, attachments=attachments)
        return
    if cmd in ("link", "start"):
        _link_chat_destination(
            sender,
            chat_id=chat_id,
            is_channel=(chat_type == "channel"),
            locale=locale,
        )
        return
    if cmd == "unlink":
        _unlink_chat_destination(sender, chat_id=chat_id, locale=locale)
        return


def _parse_command(text: str) -> tuple[str | None, str]:
    m = _CMD_RE.match((text or "").strip())
    if not m:
        return None, ""
    return (m.group(1) or "").lower(), (m.group(2) or "").strip()


def _onboard_user(user: dict[str, Any], *, locale: str = "") -> None:
    """/start and bot_started: choice menu — no link code until the user picks a path."""
    db = _require_db()
    try:
        max_user_id = int(user.get("user_id") or 0)
    except (TypeError, ValueError):
        max_user_id = 0
    if max_user_id <= 0:
        return
    username = str(user.get("username") or "").strip()
    lang = _normalize_lang(locale)
    db.upsert_max_user(max_user_id, username=username, lang=lang)
    text, attachments = _menu_start(lang)
    _throttled_send(user_id=max_user_id, text=text, attachments=attachments)


def _on_message_callback(data: dict[str, Any]) -> None:
    cb = data.get("callback") if isinstance(data.get("callback"), dict) else {}
    callback_id = str(cb.get("callback_id") or "").strip()
    payload = str(cb.get("payload") or "").strip()
    user = cb.get("user") if isinstance(cb.get("user"), dict) else {}
    locale = str(data.get("user_locale") or "").strip()
    try:
        max_user_id = int(user.get("user_id") or 0)
    except (TypeError, ValueError):
        max_user_id = 0
    if max_user_id <= 0 or not callback_id:
        return
    db = _require_db()
    lang = _normalize_lang(locale)
    db.upsert_max_user(
        max_user_id,
        username=str(user.get("username") or "").strip(),
        lang=lang,
    )
    text, attachments = _screen_for_payload(db, max_user_id, payload, lang=lang)
    _answer_callback(
        callback_id,
        text=text,
        attachments=attachments,
        user_id=max_user_id,
    )


def _screen_for_payload(
    db: Database,
    max_user_id: int,
    payload: str,
    *,
    lang: str,
) -> tuple[str, list[dict[str, Any]]]:
    if payload == _CB_NEW:
        return _menu_new(lang)
    if payload == _CB_GROUP:
        return _menu_group(lang)
    if payload == _CB_LIST:
        dests = db.list_max_destinations(max_user_id)
        return _menu_list(lang, dests)
    if payload in (_CB_HAVE, _CB_CODE):
        dest, _, _ = _ensure_dm_destination(db, max_user_id)
        code = _issue_link_code(db, max_user_id=max_user_id, dest_id=dest.id)
        return _menu_have(lang, code=code)
    # Default / unknown → start menu
    return _menu_start(lang)


def _answer_callback(
    callback_id: str,
    *,
    text: str,
    attachments: list[dict[str, Any]] | None,
    user_id: int,
) -> None:
    message: dict[str, Any] = {"text": text}
    if attachments is not None:
        message["attachments"] = attachments
    try:
        max_api.answer_callback(callback_id, message=message)
        return
    except max_api.MaxApiError as exc:
        logger.warning(
            "Max answer_callback failed status=%s — fallback send",
            exc.status_code,
        )
    try:
        _throttled_send(
            user_id=user_id,
            text=text,
            attachments=attachments,
        )
    except max_api.MaxApiError as exc:
        logger.warning("Max onboard fallback send failed status=%s", exc.status_code)


def _handle_dm_command(
    user: dict[str, Any], text: str, *, locale: str = ""
) -> None:
    db = _require_db()
    try:
        max_user_id = int(user.get("user_id") or 0)
    except (TypeError, ValueError):
        max_user_id = 0
    if max_user_id <= 0:
        return
    lang = _normalize_lang(locale)
    db.upsert_max_user(
        max_user_id,
        username=str(user.get("username") or "").strip(),
        lang=lang,
    )
    cmd, arg = _parse_command(text)
    if cmd in ("link", "code"):
        dest, _, _ = _ensure_dm_destination(db, max_user_id)
        code = _issue_link_code(db, max_user_id=max_user_id, dest_id=dest.id)
        _throttled_send(
            user_id=max_user_id,
            text=_dm_code_text(lang=lang, code=code, kind="dm"),
        )
        return
    if cmd in ("list", "destinations", "dests"):
        dests = db.list_max_destinations(max_user_id)
        _throttled_send(
            user_id=max_user_id,
            text=_list_dests_text(lang=lang, dests=dests),
        )
        return
    if cmd == "unlink":
        _unlink_from_dm(max_user_id, arg, lang=lang)
        return
    if cmd == "help":
        text, attachments = _help_screen(lang, user_id=max_user_id)
        _throttled_send(
            user_id=max_user_id, text=text, attachments=attachments
        )
        return


def _link_chat_destination(
    user: dict[str, Any],
    *,
    chat_id: int,
    is_channel: bool,
    locale: str = "",
) -> None:
    db = _require_db()
    try:
        max_user_id = int(user.get("user_id") or 0)
    except (TypeError, ValueError):
        max_user_id = 0
    if max_user_id <= 0 or chat_id == 0:
        return
    lang = _normalize_lang(locale)
    db.upsert_max_user(
        max_user_id,
        username=str(user.get("username") or "").strip(),
        lang=lang,
    )
    try:
        membership = max_api.get_chat_membership(chat_id)
    except max_api.MaxApiError as exc:
        _throttled_send(
            chat_id=chat_id,
            text=_err_membership(lang, status=exc.status_code),
        )
        return
    if not max_api.bot_can_post(membership, is_channel=is_channel):
        _throttled_send(chat_id=chat_id, text=_err_no_write(lang))
        return
    kind = "channel" if is_channel else "chat"
    dest = _ensure_chat_destination(
        db, max_user_id=max_user_id, chat_id=chat_id, kind=kind
    )
    code = _issue_link_code(db, max_user_id=max_user_id, dest_id=dest.id)
    msg = _dm_code_text(lang=lang, code=code, kind=kind)
    _throttled_send(chat_id=chat_id, text=msg)
    try:
        _throttled_send(user_id=max_user_id, text=msg)
    except max_api.MaxApiError:
        pass


def _unlink_chat_destination(
    user: dict[str, Any], *, chat_id: int, locale: str = ""
) -> None:
    db = _require_db()
    lang = _normalize_lang(locale)
    dest = db.get_max_destination_by_chat_id(chat_id)
    if dest is None or not dest.enabled:
        _throttled_send(chat_id=chat_id, text=_already_unlinked(lang))
        return
    try:
        max_user_id = int(user.get("user_id") or 0)
    except (TypeError, ValueError):
        max_user_id = 0
    # Only the linker (or anyone in chat) may unlink this chat dest.
    if max_user_id and dest.max_user_id and max_user_id != dest.max_user_id:
        # Still allow — chat admin asked to stop alerts in this room.
        pass
    db.set_max_destination_enabled(dest.id, False)
    _throttled_send(chat_id=chat_id, text=_unlinked_ok(lang, dest.public_id))


def _unlink_from_dm(max_user_id: int, arg: str, *, lang: str) -> None:
    db = _require_db()
    dests = db.list_max_destinations(max_user_id, enabled_only=True)
    if not dests:
        _throttled_send(user_id=max_user_id, text=_no_dests(lang))
        return
    target = None
    token = (arg or "").strip()
    if token.isdigit():
        idx = int(token) - 1
        if 0 <= idx < len(dests):
            target = dests[idx]
    elif token:
        upper = token.upper()
        for d in dests:
            if d.public_id == token or d.public_id.upper() == upper:
                target = d
                break
            if d.chat_id is not None and str(d.chat_id) == token:
                target = d
                break
    if target is None:
        _throttled_send(
            user_id=max_user_id,
            text=_list_dests_text(lang=lang, dests=dests)
            + "\n\n"
            + _unlink_usage(lang),
        )
        return
    db.set_max_destination_enabled(target.id, False)
    _throttled_send(
        user_id=max_user_id, text=_unlinked_ok(lang, target.public_id)
    )


def _ensure_dm_destination(
    db: Database, max_user_id: int
) -> tuple[Any, str, bool]:
    existing = db.get_max_dm_destination(max_user_id)
    if existing is not None:
        plain = try_decrypt_secret(existing.webhook_secret) or ""
        if not existing.enabled:
            db.set_max_destination_enabled(existing.id, True)
            existing = db.get_max_destination_by_id(existing.id) or existing
        return existing, plain, False
    public_id = secrets.token_urlsafe(18)
    plain = generate_secret()
    stored = encrypt_secret(plain)
    dest = db.create_max_destination(
        max_user_id,
        kind="dm",
        chat_id=None,
        public_id=public_id,
        webhook_secret=stored,
        enabled=True,
    )
    return dest, plain, True


def _ensure_chat_destination(
    db: Database,
    *,
    max_user_id: int,
    chat_id: int,
    kind: str,
) -> Any:
    existing = db.get_max_destination_by_chat_id(chat_id)
    if existing is not None:
        if not existing.enabled:
            db.set_max_destination_enabled(existing.id, True)
            existing = db.get_max_destination_by_id(existing.id) or existing
        return existing
    # Reuse DM secret style — each dest has its own ingest secret.
    public_id = secrets.token_urlsafe(18)
    plain = generate_secret()
    stored = encrypt_secret(plain)
    return db.create_max_destination(
        max_user_id,
        kind=kind if kind in ("chat", "channel") else "chat",
        chat_id=int(chat_id),
        public_id=public_id,
        webhook_secret=stored,
        enabled=True,
    )


def _issue_link_code(db: Database, *, max_user_id: int, dest_id: int) -> str:
    expires = (datetime.now(timezone.utc) + LINK_CODE_TTL).isoformat()
    for _ in range(8):
        code = secrets.token_hex(4).upper()
        try:
            db.create_max_link_code(
                code=code,
                max_user_id=max_user_id,
                dest_id=dest_id,
                expires_at=expires,
            )
            return code
        except Exception:
            continue
    code = secrets.token_hex(8).upper()
    db.create_max_link_code(
        code=code,
        max_user_id=max_user_id,
        dest_id=dest_id,
        expires_at=expires,
    )
    return code


def _tg_bot_link() -> str:
    from config import TELEGRAM_BOT_USERNAME

    name = (TELEGRAM_BOT_USERNAME or "twitchbot_game").lstrip("@")
    return f"https://t.me/{name}"


def _kb(*rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"type": "inline_keyboard", "payload": {"buttons": list(rows)}}]


def _btn_cb(text: str, payload: str) -> dict[str, Any]:
    return {"type": "callback", "text": text[:64], "payload": payload}


def _btn_link(text: str, url: str) -> dict[str, Any]:
    return {"type": "link", "text": text[:64], "url": url}


def _menu_start(lang: str) -> tuple[str, list[dict[str, Any]]]:
    en = lang.startswith("en")
    if en:
        text = (
            "Twitch stream alerts → MAX.\n"
            "Alerts are configured in Telegram; this bot only delivers them here.\n\n"
            "Pick what fits you:"
        )
        rows = [
            [_btn_cb("I already have an alert", _CB_HAVE)],
            [_btn_cb("Create an alert", _CB_NEW)],
        ]
    else:
        text = (
            "Оповещения о стримах Twitch → MAX.\n"
            "Настройка — в Telegram; этот бот только доставляет сюда.\n\n"
            "Выберите, что вам подходит:"
        )
        rows = [
            [_btn_cb("Есть настроенное оповещение", _CB_HAVE)],
            [_btn_cb("Создать оповещение", _CB_NEW)],
        ]
    return text, _kb(*rows)


def _menu_have(lang: str, *, code: str) -> tuple[str, list[dict[str, Any]]]:
    mins = int(LINK_CODE_TTL.total_seconds() // 60)
    en = lang.startswith("en")
    if en:
        text = (
            "You already set up alerts in Telegram — bind Max as the destination.\n\n"
            "1. In Telegram open the alert → Where to send → «To Max».\n"
            "2. Paste this DM link code:\n"
            f"{code}\n"
            f"Valid ~{mins} minutes.\n\n"
            "Group/channel: add this bot there and send /link — you get a code for that chat.\n"
            "Manage: /list · /unlink N"
        )
        rows = [
            [_btn_cb("New DM code", _CB_CODE)],
            [_btn_cb("Group / channel", _CB_GROUP)],
            [_btn_cb("My destinations", _CB_LIST)],
            [_btn_cb("« Back", _CB_MENU)],
        ]
    else:
        text = (
            "Оповещение уже есть в Telegram — привяжем Max как место доставки.\n\n"
            "1. В Telegram откройте оповещение → «Куда отправлять» → «В Max».\n"
            "2. Вставьте этот код связи (ЛС):\n"
            f"{code}\n"
            f"Действует ~{mins} мин.\n\n"
            "Группа/канал: добавьте бота туда и напишите /link — код для того чата.\n"
            "Управление: /list · /unlink N"
        )
        rows = [
            [_btn_cb("Новый код для ЛС", _CB_CODE)],
            [_btn_cb("Группа / канал", _CB_GROUP)],
            [_btn_cb("Мои назначения", _CB_LIST)],
            [_btn_cb("« Назад", _CB_MENU)],
        ]
    return text, _kb(*rows)


def _menu_new(lang: str) -> tuple[str, list[dict[str, Any]]]:
    tg = _tg_bot_link()
    en = lang.startswith("en")
    if en:
        text = (
            "New here — create the alert in Telegram first.\n\n"
            "1. Open the Telegram bot and tap Start / New.\n"
            "2. Choose the streamer and options.\n"
            "3. When asked where to send, pick «To Max» "
            "(or come back here for a link code).\n"
            "4. Return to Max and tap «I already have an alert».\n\n"
            f"Telegram bot: {tg}"
        )
        rows = [
            [_btn_link("Open Telegram bot", tg)],
            [_btn_cb("I already set it up", _CB_HAVE)],
            [_btn_cb("« Back", _CB_MENU)],
        ]
    else:
        text = (
            "Вы новый пользователь — сначала создайте оповещение в Telegram.\n\n"
            "1. Откройте Telegram-бота и нажмите «Старт» / «Новое».\n"
            "2. Выберите стримера и параметры.\n"
            "3. Когда спросит куда слать — «В Max» "
            "(или вернитесь сюда за кодом связи).\n"
            "4. Вернитесь в Max и нажмите «Есть настроенное оповещение».\n\n"
            f"Telegram-бот: {tg}"
        )
        rows = [
            [_btn_link("Открыть Telegram-бота", tg)],
            [_btn_cb("Уже настроил — взять код", _CB_HAVE)],
            [_btn_cb("« Назад", _CB_MENU)],
        ]
    return text, _kb(*rows)


def _menu_group(lang: str) -> tuple[str, list[dict[str, Any]]]:
    en = lang.startswith("en")
    if en:
        text = (
            "Deliver to a Max group or channel:\n\n"
            "1. Add this bot to the group/channel "
            "(channel: grant post permission).\n"
            "2. In that chat send /link — you get a link code.\n"
            "3. In Telegram: alert → Where to send → «To Max» → paste the code.\n\n"
            "DM code is only for private delivery; group/channel need /link there."
        )
        rows = [
            [_btn_cb("DM code instead", _CB_HAVE)],
            [_btn_cb("« Back", _CB_MENU)],
        ]
    else:
        text = (
            "Доставка в группу или канал Max:\n\n"
            "1. Добавьте этого бота в группу/канал "
            "(в канале — право писать посты).\n"
            "2. В том чате напишите /link — получите код связи.\n"
            "3. В Telegram: оповещение → «Куда отправлять» → «В Max» → вставьте код.\n\n"
            "Код из ЛС — только для лички; для группы/канала нужен /link там."
        )
        rows = [
            [_btn_cb("Код для ЛС", _CB_HAVE)],
            [_btn_cb("« Назад", _CB_MENU)],
        ]
    return text, _kb(*rows)


def _menu_list(
    lang: str, dests: list[Any]
) -> tuple[str, list[dict[str, Any]]]:
    body = _list_dests_text(lang=lang, dests=dests)
    en = lang.startswith("en")
    rows = [
        [_btn_cb("New DM code", _CB_CODE) if en else _btn_cb("Новый код для ЛС", _CB_CODE)],
        [_btn_cb("« Back", _CB_MENU) if en else _btn_cb("« Назад", _CB_MENU)],
    ]
    return body, _kb(*rows)


def _dm_code_text(*, lang: str, code: str, kind: str) -> str:
    label = {
        "dm": "ЛС" if not lang.startswith("en") else "DM",
        "chat": "группа" if not lang.startswith("en") else "group",
        "channel": "канал" if not lang.startswith("en") else "channel",
    }.get(kind, kind)
    if lang.startswith("en"):
        return (
            f"Link code ({label}) for Telegram → «To Max»:\n"
            f"{code}\n"
            f"Valid ~{int(LINK_CODE_TTL.total_seconds() // 60)} minutes."
        )
    return (
        f"Код связи ({label}) для Telegram → «В Max»:\n"
        f"{code}\n"
        f"Действует ~{int(LINK_CODE_TTL.total_seconds() // 60)} мин."
    )


def _list_dests_text(*, lang: str, dests: list[Any]) -> str:
    if not dests:
        return _no_dests(lang)
    lines = [
        "Your Max destinations:" if lang.startswith("en") else "Ваши назначения Max:"
    ]
    for i, d in enumerate(dests, start=1):
        mark = "" if d.enabled else (" [off]" if lang.startswith("en") else " [выкл]")
        peer = (
            f"user:{d.max_user_id}"
            if d.kind == "dm"
            else f"chat:{d.chat_id}"
        )
        lines.append(f"{i}. {d.kind} {peer} id={d.public_id}{mark}")
    return "\n".join(lines)


_DEV_TG_URL = "https://t.me/immarfa"


def _help_screen(
    lang: str, *, user_id: int = 0
) -> tuple[str, list[dict[str, Any]] | None]:
    """Same content as Telegram «❓ Помощь» / ``/feedback`` / ``/help``."""
    from i18n import btn, guide_url, t
    from translate import telegram_html_to_plain

    text = telegram_html_to_plain(
        t("feedback", lang, user_id=int(user_id) if user_id else 0)
    ).strip()
    rows: list[list[dict[str, Any]]] = [[_btn_link("@immarfa", _DEV_TG_URL)]]
    guide = guide_url(lang)
    if guide:
        rows.append([_btn_link(btn("guide", lang), guide)])
    return text, _kb(*rows)


def _no_dests(lang: str) -> str:
    return (
        "No destinations yet. /start or /link"
        if lang.startswith("en")
        else "Пока нет назначений. /start или /link"
    )


def _unlink_usage(lang: str) -> str:
    return (
        "Usage: /unlink 1  or  /unlink <public_id>"
        if lang.startswith("en")
        else "Формат: /unlink 1  или  /unlink <public_id>"
    )


def _unlinked_ok(lang: str, public_id: str) -> str:
    if lang.startswith("en"):
        return f"Destination disabled: {public_id}"
    return f"Назначение отключено: {public_id}"


def _already_unlinked(lang: str) -> str:
    return (
        "Nothing to unlink here."
        if lang.startswith("en")
        else "Здесь нечего отвязывать."
    )


def _err_membership(lang: str, *, status: int | None) -> str:
    if lang.startswith("en"):
        return f"Cannot read bot membership (HTTP {status})."
    return f"Не удалось проверить права бота (HTTP {status})."


def _err_no_write(lang: str) -> str:
    if lang.startswith("en"):
        return "Bot cannot post here. Grant write permission, then /link again."
    return "Бот не может писать сюда. Выдайте право писать и снова /link."


def _normalize_lang(locale: str) -> str:
    raw = (locale or "").strip().lower().replace("_", "-")
    if not raw:
        return "ru"
    primary = raw.split("-", 1)[0]
    if primary in ("en", "ru", "uk", "it"):
        return primary
    return "ru"


def peer_ids_for_destination(dest: Any) -> tuple[int | None, int | None]:
    """Return (user_id, chat_id) for Max send_message — exactly one set."""
    kind = str(getattr(dest, "kind", "") or "dm")
    if kind == "dm":
        return int(dest.max_user_id), None
    cid = getattr(dest, "chat_id", None)
    if cid is None:
        return None, None
    return None, int(cid)


def deliver_alert_to_destination(dest: Any, payload: dict[str, Any]) -> None:
    text = _payload_to_plain_text(payload)
    attachments = _payload_attachments(payload)
    user_id, chat_id = peer_ids_for_destination(dest)
    _throttled_send(
        user_id=user_id,
        chat_id=chat_id,
        text=text[:4000] if text else " ",
        attachments=attachments or None,
    )


def _deliver_alert_to_dm(max_user_id: int, payload: dict[str, Any]) -> None:
    text = _payload_to_plain_text(payload)
    attachments = _payload_attachments(payload)
    _throttled_send(
        user_id=max_user_id,
        text=text[:4000] if text else " ",
        attachments=attachments or None,
    )


def _unwrap_md_marker(text: str, marker: str) -> str:
    """Strip non-overlapping ``marker…marker`` wrappers in linear time (no ReDoS)."""
    mlen = len(marker)
    if mlen == 0 or not text:
        return text
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text.startswith(marker, i):
            j = text.find(marker, i + mlen)
            if j != -1:
                out.append(text[i + mlen : j])
                i = j + mlen
                continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _markdown_to_plain(text: str) -> str:
    """Best-effort markdown → plain without backtracking regexes."""
    plain = _unwrap_md_marker(text, "`")
    plain = _unwrap_md_marker(plain, "**")
    plain = _unwrap_md_marker(plain, "__")
    plain = _unwrap_md_marker(plain, "*")
    plain = _unwrap_md_marker(plain, "_")
    return plain


def _payload_to_plain_text(payload: dict[str, Any]) -> str:
    raw = str(payload.get("text") or "")
    fmt = str(payload.get("text_format") or "plain").strip().lower()
    if fmt == "html":
        # Linear tag strip (same approach as translate.telegram_html_to_plain).
        from translate import telegram_html_to_plain

        return telegram_html_to_plain(raw).strip()
    if fmt == "markdown":
        return _markdown_to_plain(raw).strip()
    return raw.strip()


def _payload_attachments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    photo = str(payload.get("photo_url") or "").strip()
    if photo.startswith("https://"):
        out.append({"type": "image", "payload": {"url": photo}})
    buttons = payload.get("buttons")
    rows: list[list[dict[str, Any]]] = []
    if isinstance(buttons, list):
        row: list[dict[str, Any]] = []
        for btn in buttons:
            if not isinstance(btn, dict):
                continue
            label = str(btn.get("text") or "").strip()[:64]
            href = str(btn.get("url") or "").strip()
            if not label or not href.startswith("https://"):
                continue
            row.append({"type": "link", "text": label, "url": href})
            if len(row) >= 3:
                rows.append(row)
                row = []
            if len(rows) >= 3:
                break
        if row and len(rows) < 3:
            rows.append(row)
    if rows:
        out.append({"type": "inline_keyboard", "payload": {"buttons": rows}})
    return out


def _throttled_send(
    *,
    user_id: int | None = None,
    chat_id: int | None = None,
    text: str,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    peer = f"u:{user_id}" if user_id is not None else f"c:{chat_id}"
    with _throttle_lock:
        now = time.monotonic()
        last = _last_send_at.get(peer, 0.0)
        wait = _MSG_MIN_INTERVAL - (now - last)
        if wait > 0:
            time.sleep(wait)
        result = max_api.send_message(
            text=text,
            user_id=user_id,
            chat_id=chat_id,
            attachments=attachments,
        )
        _last_send_at[peer] = time.monotonic()
        return result


def parse_ingest_public_id(path: str) -> str | None:
    """Return public_id from ``/hooks/max/d/{id}`` or None."""
    raw = (path or "").split("?", 1)[0]
    if not raw.startswith(MAX_INGEST_PREFIX):
        return None
    pid = raw[len(MAX_INGEST_PREFIX) :].strip("/")
    if "/" in pid or not _PUBLIC_ID_RE.fullmatch(pid):
        return None
    return pid


def our_ingest_public_id_from_url(url: str) -> str | None:
    """If ``url`` is this bot's Max ingest endpoint, return public_id."""
    from config import PUBLIC_BASE_URL
    from urllib.parse import urlparse

    text = (url or "").strip()
    if not text:
        return None
    base = (PUBLIC_BASE_URL or "").rstrip("/")
    if not base.startswith("https://"):
        return None
    try:
        parts = urlparse(text)
        base_parts = urlparse(base)
    except Exception:
        return None
    if (parts.scheme or "").lower() != "https":
        return None
    if (parts.hostname or "").lower() != (base_parts.hostname or "").lower():
        return None
    return parse_ingest_public_id(parts.path or "")


def resolve_link_code(
    db: Database, code: str
) -> tuple[Any | None, str | None]:
    """Validate a Max link code.

    Returns ``(destination, None)`` on success, or ``(None, i18n_key)`` on failure.
    Does not consume the code — caller should call ``consume_max_link_code``.
    """
    raw = (code or "").strip().upper()
    if not raw or len(raw) < 6 or len(raw) > 32:
        return None, "max_invalid_code"
    link = db.get_max_link_code(raw)
    if link is None:
        return None, "max_invalid_code"
    if link.consumed_at:
        return None, "max_code_used"
    try:
        expires = datetime.fromisoformat(
            str(link.expires_at or "").replace("Z", "+00:00")
        )
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < datetime.now(timezone.utc):
            return None, "max_code_expired"
    except Exception:
        return None, "max_code_expired"
    dest = db.get_max_destination_by_id(int(link.dest_id))
    if dest is None or not dest.enabled:
        return None, "max_invalid_code"
    if dest.kind not in ("dm", "chat", "channel"):
        return None, "max_invalid_code"
    if int(dest.max_user_id) != int(link.max_user_id):
        return None, "max_invalid_code"
    return dest, None


def send_plain_dm(max_user_id: int, text: str) -> None:
    """Send a plain text DM (wizard sample / direct delivery)."""
    _throttled_send(
        user_id=int(max_user_id),
        text=(text or " ")[:4000],
    )


def send_plain_to_destination(dest: Any, text: str) -> None:
    """Send plain text to a Max destination (DM or chat)."""
    user_id, chat_id = peer_ids_for_destination(dest)
    _throttled_send(
        user_id=user_id,
        chat_id=chat_id,
        text=(text or " ")[:4000],
    )


def deliver_alert_payload(max_user_id: int, payload: dict[str, Any]) -> None:
    """Fan-out an outbound-webhook-shaped payload to a Max DM (legacy helper)."""
    _deliver_alert_to_dm(int(max_user_id), payload)
