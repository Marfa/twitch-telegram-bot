"""Group/chat commands: next slot (/when) and day/week schedule listing."""
from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime, time, timedelta, timezone
from typing import Literal

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
    start: datetime,
    category: str,
) -> str:
    local = start.astimezone(SCHEDULE_TZ)
    when = local.strftime("%d.%m.%Y %H:%M MSK")
    category_label = (category or "").strip() or "—"
    return t("when_stream", lang, when=when, category=category_label)


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
    slots: list[tuple[datetime, str]],
    escape_game: bool = False,
) -> str:
    return "\n".join(
        format_range_schedule_line(
            lang=lang,
            start=start,
            category=category,
            escape_game=escape_game,
        )
        for start, category in slots
    )


def format_schedule_with_streamer_link(
    *,
    username: str,
    body: str,
) -> str:
    return f"{twitch_login_link_html(username)}:\n{body}"


def schedule_streamer_keyboard(
    mode: ScheduleMode,
    streamers: list[tuple[str, str]],
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for uid, username in streamers:
        label = (username or uid)[:64]
        row.append(
            InlineKeyboardButton(label, callback_data=f"schq:{mode}:{uid}")
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def schedule_detail_keyboard(mode: ScheduleMode, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("btn_wizard_back", lang),
                    callback_data=f"schq:list:{mode}",
                )
            ]
        ]
    )


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


def _empty_text(mode: ScheduleMode, lang: str) -> str:
    if mode == "day":
        return t("schedule_range_empty_day", lang)
    if mode == "week":
        return t("schedule_range_empty_week", lang)
    return t("when_stream_no_schedule", lang)


def _segment_category(segment: dict) -> str:
    cat = segment.get("category") or {}
    return str(cat.get("name") or "") if isinstance(cat, dict) else ""


async def _fetch_schedule(
    twitch: TwitchClient,
    uid: str,
    *,
    mode: ScheduleMode,
    now: datetime,
) -> dict:
    window_end = (
        schedule_window_end(mode, now=now) if mode in ("day", "week") else None
    )
    stop_before = (
        window_end.strftime("%Y-%m-%dT%H:%M:%SZ") if window_end is not None else None
    )
    return await asyncio.to_thread(
        twitch.get_channel_schedule,
        uid,
        stop_before=stop_before,
    )


def _format_streamer_schedule(
    *,
    mode: ScheduleMode,
    lang: str,
    schedule: dict,
    now: datetime,
    username: str | None = None,
) -> str | None:
    linked = bool((username or "").strip())
    if mode == "when":
        segment = next_upcoming_segment(schedule, now=now)
        if segment is None:
            return None
        start = _parse_segment_start(segment)
        if start is None:
            return None
        raw_cat = (_segment_category(segment) or "").strip() or "—"
        body = format_when_stream_line(
            lang=lang,
            start=start,
            category=html.escape(raw_cat) if linked else raw_cat,
        )
    else:
        window_end = schedule_window_end(mode, now=now)
        slots = segments_in_window(schedule, window_start=now, window_end=window_end)
        if not slots:
            return None
        body = format_range_streamer_block(
            lang=lang, slots=slots, escape_game=linked
        )
    if linked:
        return format_schedule_with_streamer_link(
            username=username or "", body=body
        )
    return body


async def _discover_hits(
    *,
    twitch: TwitchClient,
    streamers: list[tuple[str, str]],
    mode: ScheduleMode,
    lang: str,
    now: datetime,
) -> tuple[list[tuple[str, str, str]], bool]:
    """Return ([(uid, username, text), …], any successful fetch)."""
    hits: list[tuple[str, str, str]] = []
    fetched_ok = False
    for uid, username in streamers:
        try:
            schedule = await _fetch_schedule(twitch, uid, mode=mode, now=now)
        except Exception:
            logger.exception("%s: schedule fetch failed for %s", mode, uid)
            continue
        fetched_ok = True
        text = _format_streamer_schedule(
            mode=mode, lang=lang, schedule=schedule, now=now
        )
        if text is None:
            continue
        hits.append((uid, username, text))
    return hits, fetched_ok


async def _reply_schedule_message(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    *,
    reply_markup=None,
    as_html: bool = False,
) -> None:
    send_kw: dict = {}
    if reply_markup is not None:
        send_kw["reply_markup"] = reply_markup
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


async def _edit_schedule_message(
    query,
    text: str,
    *,
    reply_markup=None,
    as_html: bool = False,
) -> None:
    edit_kw: dict = {}
    if reply_markup is not None:
        edit_kw["reply_markup"] = reply_markup
    if as_html:
        edit_kw["parse_mode"] = ParseMode.HTML
        edit_kw["disable_web_page_preview"] = True
    try:
        await query.edit_message_text(text, **edit_kw)
    except BadRequest:
        pass


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

    streamers = unique_streamers_for_chat(
        db.get_enabled_subscriptions_by_chat_id(chat_id)
    )
    if not streamers:
        return

    now = datetime.now(timezone.utc)
    hits, fetched_ok = await _discover_hits(
        twitch=twitch, streamers=streamers, mode=mode, lang=lang, now=now
    )
    if not hits:
        if not fetched_ok:
            return
        _mark_cooldown(chat_id)
        await _reply_schedule_message(
            message, context, chat_id, _empty_text(mode, lang)
        )
        return

    _mark_cooldown(chat_id)
    if len(hits) == 1:
        await _reply_schedule_message(message, context, chat_id, hits[0][2])
        return

    await _reply_schedule_message(
        message,
        context,
        chat_id,
        t("schedule_found_streamers", lang),
        reply_markup=schedule_streamer_keyboard(
            mode, [(uid, name) for uid, name, _text in hits]
        ),
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


async def on_schedule_query_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    raw = query.data
    user = update.effective_user
    if user is None:
        await query.answer()
        return

    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    lang = _user_lang(context, user.id)
    now = datetime.now(timezone.utc)

    list_m = re.fullmatch(r"schq:list:(when|day|week)", raw)
    pick_m = re.fullmatch(r"schq:(when|day|week):(\d+)", raw)
    if list_m:
        mode: ScheduleMode = list_m.group(1)  # type: ignore[assignment]
        chat = update.effective_chat
        if chat is not None:
            chat_id = int(chat.id)
        elif query.message is not None:
            chat_id = int(query.message.chat_id)
        else:
            await query.answer()
            return
        streamers = unique_streamers_for_chat(
            db.get_enabled_subscriptions_by_chat_id(chat_id)
        )
        hits, fetched_ok = await _discover_hits(
            twitch=twitch,
            streamers=streamers,
            mode=mode,
            lang=lang,
            now=now,
        )
        await query.answer()
        if not hits:
            text = (
                _empty_text(mode, lang)
                if fetched_ok
                else t("schedule_found_streamers", lang)
            )
            await _edit_schedule_message(query, text)
            return
        if len(hits) == 1:
            uid, username, _plain = hits[0]
            try:
                schedule = await _fetch_schedule(twitch, uid, mode=mode, now=now)
                text = (
                    _format_streamer_schedule(
                        mode=mode,
                        lang=lang,
                        schedule=schedule,
                        now=now,
                        username=username,
                    )
                    or _empty_text(mode, lang)
                )
            except Exception:
                logger.exception("%s: schedule re-fetch failed for %s", mode, uid)
                return
            await _edit_schedule_message(query, text, as_html=True)
            return
        await _edit_schedule_message(
            query,
            t("schedule_found_streamers", lang),
            reply_markup=schedule_streamer_keyboard(
                mode, [(uid, name) for uid, name, _text in hits]
            ),
        )
        return

    if not pick_m:
        await query.answer()
        return

    mode = pick_m.group(1)  # type: ignore[assignment]
    uid = pick_m.group(2)
    chat = update.effective_chat
    if chat is not None:
        chat_id = int(chat.id)
    elif query.message is not None:
        chat_id = int(query.message.chat_id)
    else:
        await query.answer()
        return
    name_by_id = dict(
        unique_streamers_for_chat(db.get_enabled_subscriptions_by_chat_id(chat_id))
    )
    username = name_by_id.get(uid, uid)
    try:
        schedule = await _fetch_schedule(twitch, uid, mode=mode, now=now)
        text = _format_streamer_schedule(
            mode=mode,
            lang=lang,
            schedule=schedule,
            now=now,
            username=username,
        )
    except Exception:
        logger.exception("%s: schedule pick failed for %s", mode, uid)
        await query.answer()
        return
    await query.answer()
    if not text:
        text = _empty_text(mode, lang)
        await _edit_schedule_message(
            query,
            text,
            reply_markup=schedule_detail_keyboard(mode, lang),
        )
        return
    await _edit_schedule_message(
        query,
        text,
        reply_markup=schedule_detail_keyboard(mode, lang),
        as_html=True,
    )
