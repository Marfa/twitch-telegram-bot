"""Thin MAX Messenger bot: updates webhook + outbound-alert ingest.

Phase 3: DM destinations, link codes, send via Max API.
Never log tokens, update secrets, or per-dest webhook secrets.
"""
from __future__ import annotations

import html
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
_TAG_RE = re.compile(r"<[^>]+>")
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)|_(.+?)_")
_MD_CODE_RE = re.compile(r"`([^`]+)`")

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


def bootstrap_max_subscription() -> bool:
    """Ensure Max delivers updates to our HTTPS endpoint. Returns True if ok/skipped."""
    from config import MAX_BOT_TOKEN

    if not (MAX_BOT_TOKEN or "").strip():
        logger.info("MAX_BOT_TOKEN unset — skip Max subscription bootstrap")
        return False
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
        if sub_url == url:
            logger.info("Max subscription already registered")
            return True
    try:
        max_api.subscribe(
            url,
            update_types=["bot_started", "message_created"],
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
    """Accept our outbound webhook contract and fan-out to Max DM."""
    db = _require_db()
    pid = (public_id or "").strip()
    if not _PUBLIC_ID_RE.fullmatch(pid):
        return 404, b"not found"
    dest = db.get_max_destination_by_public_id(pid)
    if dest is None or not dest.enabled or dest.kind != "dm":
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
        _deliver_alert_to_dm(dest.max_user_id, payload)
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
    if update_type == "message_created":
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        sender = message.get("sender") if isinstance(message.get("sender"), dict) else {}
        body = message.get("body") if isinstance(message.get("body"), dict) else {}
        text = str(body.get("text") or "").strip()
        if text.lower().startswith("/start"):
            locale = str(data.get("user_locale") or "").strip()
            _onboard_user(sender, locale=locale)
        return


def _onboard_user(user: dict[str, Any], *, locale: str = "") -> None:
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
    dest, plain_secret, is_new_secret = _ensure_dm_destination(db, max_user_id)
    code = _issue_link_code(db, max_user_id=max_user_id, dest_id=dest.id)
    url = ingest_url_for(dest.public_id)
    text = _welcome_text(
        lang=lang or "ru",
        code=code,
        url=url,
        secret=plain_secret if is_new_secret else None,
    )
    _throttled_send(user_id=max_user_id, text=text)


def _ensure_dm_destination(
    db: Database, max_user_id: int
) -> tuple[Any, str, bool]:
    existing = db.get_max_dm_destination(max_user_id)
    if existing is not None:
        plain = try_decrypt_secret(existing.webhook_secret) or ""
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
    # Last resort — longer code
    code = secrets.token_hex(8).upper()
    db.create_max_link_code(
        code=code,
        max_user_id=max_user_id,
        dest_id=dest_id,
        expires_at=expires,
    )
    return code


def _welcome_text(*, lang: str, code: str, url: str, secret: str | None) -> str:
    if lang.startswith("en"):
        lines = [
            "Twitch stream alerts → MAX (via Telegram bot).",
            "",
            f"Link code (for Telegram «Bind Max», coming soon): {code}",
            f"Valid ~{int(LINK_CODE_TTL.total_seconds() // 60)} minutes.",
            "",
            "Until then, in Telegram choose destination Webhook and paste:",
            url,
        ]
        if secret:
            lines.extend(
                [
                    "",
                    "Webhook secret (shown once — store it):",
                    secret,
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    "Webhook secret was shown when you first started this bot.",
                ]
            )
        return "\n".join(lines)
    lines = [
        "Оповещения о стримах Twitch → MAX (через Telegram-бота).",
        "",
        f"Код связи (для «Привязать Max» в Telegram — скоро): {code}",
        f"Действует ~{int(LINK_CODE_TTL.total_seconds() // 60)} мин.",
        "",
        "Пока можно вручную в Telegram выбрать назначение Webhook и вставить:",
        url,
    ]
    if secret:
        lines.extend(
            [
                "",
                "Секрет webhook (показывается один раз — сохраните):",
                secret,
            ]
        )
    else:
        lines.extend(
            [
                "",
                "Секрет webhook уже показывали при первом /start.",
            ]
        )
    return "\n".join(lines)


def _normalize_lang(locale: str) -> str:
    raw = (locale or "").strip().lower().replace("_", "-")
    if not raw:
        return "ru"
    primary = raw.split("-", 1)[0]
    if primary in ("en", "ru", "uk", "it"):
        return primary
    return "ru"


def _deliver_alert_to_dm(max_user_id: int, payload: dict[str, Any]) -> None:
    text = _payload_to_plain_text(payload)
    attachments = _payload_attachments(payload)
    _throttled_send(
        user_id=max_user_id,
        text=text[:4000] if text else " ",
        attachments=attachments or None,
    )


def _payload_to_plain_text(payload: dict[str, Any]) -> str:
    raw = str(payload.get("text") or "")
    fmt = str(payload.get("text_format") or "plain").strip().lower()
    if fmt == "html":
        # Decode entities after stripping tags.
        plain = _TAG_RE.sub("", raw)
        return html.unescape(plain).strip()
    if fmt == "markdown":
        plain = _MD_CODE_RE.sub(r"\1", raw)
        plain = _MD_BOLD_RE.sub(lambda m: m.group(1) or m.group(2) or "", plain)
        plain = _MD_ITALIC_RE.sub(lambda m: m.group(1) or m.group(2) or "", plain)
        return plain.strip()
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
