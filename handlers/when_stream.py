"""Group/chat commands: next slot (/when) and day/week schedule listing."""
from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime, time, timedelta, timezone
from typing import Literal

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from bot_helpers import _user_lang
from db import Database
from handlers.notifications import _parse_segment_start
from i18n import SCHEDULE_TZ, format_stream_schedule_date, t
from twitch import TwitchClient, twitch_login_link_html

logger = logging.getLogger(__name__)

ScheduleMode = Literal["when", "day", "week"]

WHEN_STREAM_COOLDOWN = timedelta(minutes=1)
_when_stream_last: dict[int, datetime] = {}

# Free-text triggers. Leading @bot is common under Telegram privacy mode.
WHEN_STREAM_TEXT_RE = re.compile(
    r"(?i)^\s*(?:@\w+\s+)*(?:когда\s+стрим|when(?:'s|\s+is)?(?:\s+the)?\s+stream)\s*\??\s*$"
)
DAY_SCHEDULE_TEXT_RE = re.compile(
    r"(?i)^\s*(?:@\w+\s+)*("
    r"расписание\s+на\s+день"
    r"|розклад\s+на\s+день"
    r"|schedule\s+for\s+(?:the\s+)?day"
    r"|day(?:'s)?\s+schedule"
    r"|today'?s?\s+schedule"
    r")\s*\??\s*$"
)
WEEK_SCHEDULE_TEXT_RE = re.compile(
    r"(?i)^\s*(?:@\w+\s+)*("
    r"расписание\s+на\s+неделю"
    r"|розклад\s+на\s+тиждень"
    r"|schedule\s+for\s+(?:the\s+)?week"
    r"|week(?:ly)?\s+schedule"
    r")\s*\??\s*$"
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


def schedule_window_end(
    mode: Literal["day", "week"],
    *,
    now: datetime | None = None,
) -> datetime:
    """Exclusive UTC end of the day/week window (MSK calendar)."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local = current.astimezone(SCHEDULE_TZ)
    days = 1 if mode == "day" else 7
    end_local = datetime.combine(
        local.date() + timedelta(days=days),
        time.min,
        tzinfo=SCHEDULE_TZ,
    )
    return end_local.astimezone(timezone.utc)


def segments_in_window(
    schedule: dict,
    *,
    window_start: datetime,
    window_end: datetime,
) -> list[tuple[datetime, str]]:
    """Non-canceled segments with start in [window_start, window_end), sorted."""
    if window_start.tzinfo is None:
        window_start = window_start.replace(tzinfo=timezone.utc)
    if window_end.tzinfo is None:
        window_end = window_end.replace(tzinfo=timezone.utc)
    window_start = window_start.astimezone(timezone.utc)
    window_end = window_end.astimezone(timezone.utc)
    if TwitchClient.vacation_active(schedule.get("vacation"), now=window_start):
        return []
    out: list[tuple[datetime, str]] = []
    for segment in schedule.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        if segment.get("canceled_until"):
            continue
        start = _parse_segment_start(segment)
        if start is None or start < window_start or start >= window_end:
            continue
        cat = segment.get("category") or {}
        category = (
            str(cat.get("name") or "")
            if isinstance(cat, dict)
            else ""
        )
        out.append((start, category))
    out.sort(key=lambda item: item[0])
    return out


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
    if not show_username:
        return t("when_stream", lang, when=when, category=category_label)
    return t(
        "when_stream_named",
        lang,
        username=twitch_login_link_html(username),
        when=when,
        category=html.escape(category_label),
    )


def format_range_schedule_line(
    *,
    lang: str,
    start: datetime,
    category: str,
    escape_game: bool = False,
) -> str:
    local = start.astimezone(SCHEDULE_TZ)
    game = (category or "").strip() or "—"
    if escape_game:
        game = html.escape(game)
    return t(
        "stream_schedule_line",
        lang,
        date=format_stream_schedule_date(local.date(), lang),
        time=local.strftime("%H:%M"),
        game=game,
    )


def format_range_streamer_block(
    *,
    lang: str,
    username: str,
    slots: list[tuple[datetime, str]],
    show_username: bool,
) -> str:
    lines = [
        format_range_schedule_line(
            lang=lang,
            start=start,
            category=category,
            escape_game=show_username,
        )
        for start, category in slots
    ]
    body = "\n".join(lines)
    if not show_username:
        return body
    return f"{twitch_login_link_html(username)}:\n{body}"


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


async def _reply_schedule_text(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    *,
    as_html: bool,
) -> None:
    send_kw: dict = {}
    if as_html:
        send_kw["parse_mode"] = ParseMode.HTML
        send_kw["disable_web_page_preview"] = True
    try:
        await message.reply_text(text, **send_kw)
    except BadRequest as exc:
        # Trigger deleted while we fetched schedule — still answer in the chat.
        if "message to be replied not found" not in str(exc).lower():
            raise
        await context.bot.send_message(chat_id, text, **send_kw)


async def _schedule_query_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    mode: ScheduleMode,
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

    show_name = len(streamers) > 1
    now = datetime.now(timezone.utc)
    window_end = (
        schedule_window_end(mode, now=now) if mode in ("day", "week") else None
    )
    stop_before = (
        window_end.strftime("%Y-%m-%dT%H:%M:%SZ") if window_end is not None else None
    )

    blocks: list[str] = []
    fetched_ok = False
    for uid, username in streamers:
        try:
            schedule = await asyncio.to_thread(
                twitch.get_channel_schedule,
                uid,
                stop_before=stop_before,
            )
        except Exception:
            logger.exception("%s: schedule fetch failed for %s", mode, uid)
            continue
        fetched_ok = True
        if mode == "when":
            segment = next_upcoming_segment(schedule, now=now)
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
            blocks.append(
                format_when_stream_line(
                    lang=lang,
                    username=username,
                    start=start,
                    category=category,
                    show_username=show_name,
                )
            )
            continue

        assert window_end is not None
        slots = segments_in_window(
            schedule, window_start=now, window_end=window_end
        )
        if not slots:
            continue
        blocks.append(
            format_range_streamer_block(
                lang=lang,
                username=username,
                slots=slots,
                show_username=show_name,
            )
        )

    if blocks:
        text = "\n\n".join(blocks) if mode != "when" else "\n".join(blocks)
    elif fetched_ok:
        if mode == "day":
            text = t("schedule_range_empty_day", lang)
        elif mode == "week":
            text = t("schedule_range_empty_week", lang)
        else:
            text = t("when_stream_no_schedule", lang)
    else:
        return

    _mark_cooldown(chat_id)
    await _reply_schedule_text(
        message, context, chat_id, text, as_html=show_name
    )


async def when_stream_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _schedule_query_command(update, context, mode="when")


async def day_schedule_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _schedule_query_command(update, context, mode="day")


async def week_schedule_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _schedule_query_command(update, context, mode="week")
