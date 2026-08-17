"""Twitch EventSub webhook helpers (whisper received)."""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from twitch import USERNAME_RE

logger = logging.getLogger(__name__)

WHISPER_TYPE = "user.whisper.message"
_MAX_AGE = timedelta(minutes=10)
_FUTURE_SKEW = timedelta(minutes=1)
_SEEN_MAX = 500
_seen_ids: deque[str] = deque()
_seen_set: set[str] = set()
_TELEGRAM_TEXT_MAX = 3500


@dataclass(frozen=True)
class WhisperEvent:
    from_user_id: str
    from_user_login: str
    from_user_name: str
    to_user_id: str
    to_user_login: str
    text: str
    whisper_id: str
    subscription_id: str


@dataclass(frozen=True)
class EventSubResult:
    status: int
    body: bytes
    content_type: str
    whisper: WhisperEvent | None = None
    revoked_user_id: str = ""


def eventsub_secret() -> str:
    raw = (os.getenv("TWITCH_EVENTSUB_SECRET") or "").strip()
    if raw:
        return raw[:100]
    from config import TELEGRAM_BOT_TOKEN

    material = f"{TELEGRAM_BOT_TOKEN}:eventsub-v1".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:32]


def eventsub_callback_url() -> str:
    from config import PUBLIC_BASE_URL

    if not PUBLIC_BASE_URL:
        return ""
    return f"{PUBLIC_BASE_URL}/hooks/eventsub"


def verify_signature(
    *,
    secret: str,
    message_id: str,
    timestamp: str,
    body: bytes,
    signature: str,
) -> bool:
    if not secret or not message_id or not timestamp or not signature:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        (message_id + timestamp).encode("utf-8") + body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def parse_rfc3339(ts: str) -> datetime | None:
    raw = (ts or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    if "." in raw:
        main, rest = raw.split(".", 1)
        digits = ""
        tz = ""
        for ch in rest:
            if ch.isdigit():
                digits += ch
            else:
                tz += ch
        digits = (digits + "000000")[:6]
        raw = f"{main}.{digits}{tz}"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def timestamp_fresh(ts: str, *, now: datetime | None = None) -> bool:
    dt = parse_rfc3339(ts)
    if dt is None:
        return False
    current = now or datetime.now(timezone.utc)
    if dt > current + _FUTURE_SKEW:
        return False
    return current - dt <= _MAX_AGE


def remember_message_id(message_id: str) -> bool:
    """True if this id was not seen before (should process)."""
    if not message_id:
        return True
    if message_id in _seen_set:
        return False
    _seen_ids.append(message_id)
    _seen_set.add(message_id)
    while len(_seen_ids) > _SEEN_MAX:
        old = _seen_ids.popleft()
        _seen_set.discard(old)
    return True


def whisper_conversation_url(*, to_login: str = "", from_login: str = "") -> str:
    to_ok = bool(to_login) and bool(USERNAME_RE.match(to_login))
    if to_ok:
        return f"https://www.twitch.tv/popout/{to_login}/whisper"
    from_ok = bool(from_login) and bool(USERNAME_RE.match(from_login))
    if from_ok:
        return f"https://www.twitch.tv/{from_login}"
    return "https://www.twitch.tv/inbox"


def parse_whisper_event(payload: dict[str, Any]) -> WhisperEvent | None:
    event = payload.get("event")
    if not isinstance(event, dict):
        return None
    whisper = event.get("whisper")
    text = ""
    if isinstance(whisper, dict):
        text = str(whisper.get("text") or "")
    else:
        text = str(whisper or "")
    from_login = str(event.get("from_user_login") or "").strip()
    to_id = str(event.get("to_user_id") or "").strip()
    if not to_id:
        return None
    sub = payload.get("subscription")
    sub_id = ""
    if isinstance(sub, dict):
        sub_id = str(sub.get("id") or "")
    return WhisperEvent(
        from_user_id=str(event.get("from_user_id") or ""),
        from_user_login=from_login,
        from_user_name=str(event.get("from_user_name") or from_login),
        to_user_id=to_id,
        to_user_login=str(event.get("to_user_login") or "").strip(),
        text=text,
        whisper_id=str(event.get("whisper_id") or ""),
        subscription_id=sub_id,
    )


def format_whisper_alert(
    lang: str,
    event: WhisperEvent,
    *,
    url: str,
) -> str:
    from i18n import t

    name = (event.from_user_name or event.from_user_login or "?").strip()
    login = (event.from_user_login or "").strip()
    body = event.text.strip()
    if len(body) > _TELEGRAM_TEXT_MAX:
        body = body[: _TELEGRAM_TEXT_MAX - 1] + "…"
    return t(
        "whisper_alert_message",
        lang,
        name=html.escape(name),
        login=html.escape(login or "—"),
        text=html.escape(body) or "—",
        url=html.escape(url, quote=True),
    )


def handle_eventsub_post(
    *,
    headers: Any,
    body: bytes,
    secret: str,
) -> EventSubResult:
    message_id = str(headers.get("Twitch-Eventsub-Message-Id") or "")
    timestamp = str(headers.get("Twitch-Eventsub-Message-Timestamp") or "")
    signature = str(headers.get("Twitch-Eventsub-Message-Signature") or "")
    msg_type = str(headers.get("Twitch-Eventsub-Message-Type") or "")
    if not verify_signature(
        secret=secret,
        message_id=message_id,
        timestamp=timestamp,
        body=body,
        signature=signature,
    ):
        return EventSubResult(403, b"invalid signature", "text/plain")
    if not timestamp_fresh(timestamp):
        return EventSubResult(403, b"stale timestamp", "text/plain")
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return EventSubResult(400, b"invalid json", "text/plain")
    if not isinstance(payload, dict):
        return EventSubResult(400, b"expected object", "text/plain")
    if msg_type == "webhook_callback_verification":
        challenge = str(payload.get("challenge") or "")
        if not challenge:
            return EventSubResult(400, b"missing challenge", "text/plain")
        return EventSubResult(200, challenge.encode("utf-8"), "text/plain")
    if not remember_message_id(message_id):
        return EventSubResult(204, b"", "text/plain")
    if msg_type == "revocation":
        sub = payload.get("subscription")
        user_id = ""
        if isinstance(sub, dict):
            cond = sub.get("condition")
            if isinstance(cond, dict):
                user_id = str(cond.get("user_id") or "")
        return EventSubResult(
            204, b"", "text/plain", revoked_user_id=user_id
        )
    if msg_type != "notification":
        return EventSubResult(204, b"", "text/plain")
    sub = payload.get("subscription")
    sub_type = ""
    if isinstance(sub, dict):
        sub_type = str(sub.get("type") or "")
    if sub_type and sub_type != WHISPER_TYPE:
        return EventSubResult(204, b"", "text/plain")
    event = parse_whisper_event(payload)
    if event is None:
        return EventSubResult(204, b"", "text/plain")
    return EventSubResult(204, b"", "text/plain", whisper=event)
