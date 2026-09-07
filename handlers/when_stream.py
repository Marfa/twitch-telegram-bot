"""Group/chat command: next Twitch schedule slot for streamers alerting this chat."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import ContextTypes

from bot_helpers import _user_lang
from db import Database
from handlers.notifications import _parse_segment_start
from i18n import SCHEDULE_TZ, t
from twitch import TwitchClient

logger = logging.getLogger(__name__)

WHEN_STREAM_COOLDOWN = timedelta(minutes=1)
_when_stream_last: dict[int, datetime] = {}

# Free-text triggers. Leading @bot is common under Telegram privacy mode.
WHEN_STREAM_TEXT_RE = re.compile(
    r"(?i)^\s*(?:@\w+\s+)*(?:когда\s+стрим|when(?:'s|\s+is)?(?:\s+the)?\s+stream)\s*\??\s*$"
)


def unique_streamers_for_chat(
    subs: list,
) -> list[tuple[str, str]]:
    """Return (twitch_user_id, twitch_username) in first-seen order."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for sub in subs:
        uid = str(getattr(sub, "twitch_user_id", "") or "").strip()
        if not uid or uid in seen:
            continue
        seen.add(uid)
        name = str(getattr(sub, "twitch_username", "") or "").strip() or uid
        out.append((uid, name))
    return out


def next_upcoming_segment(
    schedule: dict,
    *,
    now: datetime | None = None,
) -> dict | None:
    """Nearest future non-canceled segment, or None (empty/vacation/none)."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    if TwitchClient.vacation_active(schedule.get("vacation"), now=current):
        return None
    best: tuple[datetime, dict] | None = None
    for segment in schedule.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        if segment.get("canceled_until"):
            continue
        start = _parse_segment_start(segment)
        if start is None or start <= current:
            continue
        if best is None or start < best[0]:
            best = (start, segment)
    return best[1] if best else None


def format_when_stream_line(
    *,
    lang: str,
    username: str,
    start: datetime,
    category: str,
    show_username: bool,
) -> str:
    local = start.astimezone(SCHEDULE_TZ)
    when = local.strftime("%d.%m.%Y %H:%M MSK")
    category_label = (category or "").strip() or "—"
    key = "when_stream_named" if show_username else "when_stream"
    return t(key, lang, username=username, when=when, category=category_label)


def _cooldown_allows(chat_id: int, *, now: datetime | None = None) -> bool:
    """True if the command may reply; marks the chat on success path only via mark."""
    at = now or datetime.now(timezone.utc)
    last = _when_stream_last.get(chat_id)
    if last is not None and at - last < WHEN_STREAM_COOLDOWN:
        return False
    return True


def _mark_cooldown(chat_id: int, *, now: datetime | None = None) -> None:
    _when_stream_last[chat_id] = now or datetime.now(timezone.utc)


def reset_when_stream_cooldown() -> None:
    """Test helper."""
    _when_stream_last.clear()


async def when_stream_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or not user:
        return

    chat_id = int(chat.id)
    if not _cooldown_allows(chat_id):
        return

    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    lang = _user_lang(context, user.id)

    subs = db.get_enabled_subscriptions_by_chat_id(chat_id)
    streamers = unique_streamers_for_chat(subs)
    if not streamers:
        return

    lines: list[str] = []
    show_name = len(streamers) > 1
    fetched_ok = False
    for uid, username in streamers:
        try:
            schedule = await asyncio.to_thread(twitch.get_channel_schedule, uid)
        except Exception:
            logger.exception("when_stream: schedule fetch failed for %s", uid)
            continue
        fetched_ok = True
        segment = next_upcoming_segment(schedule)
        if segment is None:
            continue
        start = _parse_segment_start(segment)
        if start is None:
            continue
        cat = segment.get("category") or {}
        category = (
            str(cat.get("name") or "")
            if isinstance(cat, dict)
            else ""
        )
        lines.append(
            format_when_stream_line(
                lang=lang,
                username=username,
                start=start,
                category=category,
                show_username=show_name,
            )
        )

    if lines:
        text = "\n".join(lines)
    elif fetched_ok:
        text = t("when_stream_no_schedule", lang)
    else:
        return

    _mark_cooldown(chat_id)
    await message.reply_text(text)
