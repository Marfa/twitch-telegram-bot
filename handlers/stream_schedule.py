from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, ContextTypes, ConversationHandler

import beta as beta_features
import premium as prem
from bot_helpers import _menu, _pulse_wizard_keyboard, _user_lang, _wizard, reply_chat_id
from db import Database
from i18n import (
    DEFAULT_LOCALE,
    SCHEDULE_TZ,
    all_wizard_nav_buttons,
    format_stream_schedule_date,
    format_stream_schedule_prompt_date,
    format_stream_schedule_result,
    is_menu_button,
    stream_schedule_confirm_keyboard,
    stream_schedule_day_keyboard,
    stream_schedule_duration_keyboard,
    stream_schedule_fix_day_keyboard,
    stream_schedule_mode_keyboard,
    stream_schedule_more_keyboard,
    stream_schedule_occupied_keyboard,
    stream_schedule_publish_keyboard,
    t,
)
from twitch import TwitchClient

logger = logging.getLogger(__name__)


def _log_schedule_clear_failed(exc_type: str) -> None:
    logger.warning("Failed to clear Twitch schedule before publish (%s)", exc_type)


_STREAM_TIME_PATTERN = re.compile(r"^(\d{1,2}):(\d{2})$")
_UTC_OFFSET_PATTERN = re.compile(
    r"^\s*(?:UTC|GMT)?\s*([+-])?\s*(\d{1,2})(?:\s*:\s*(\d{2}))?\s*$",
    re.IGNORECASE,
)
_SCHEDULE_DEFAULT_DURATION_MIN = 120
_UTC_OFFSET_MIN = -12 * 60
_UTC_OFFSET_MAX = 14 * 60


def parse_utc_offset_text(raw: str) -> int | None:
    """Parse 'UTC+3', 'UTC-5', '+5:30', 'UTC' → offset minutes from UTC."""
    text = (raw or "").strip()
    if not text:
        return None
    if re.fullmatch(r"(?i)UTC|GMT", text):
        return 0
    match = _UTC_OFFSET_PATTERN.fullmatch(text)
    if not match:
        return None
    sign = -1 if match.group(1) == "-" else 1
    hours = int(match.group(2))
    minutes = int(match.group(3) or 0)
    if hours > 14 or minutes > 59:
        return None
    total = sign * (hours * 60 + minutes)
    if total < _UTC_OFFSET_MIN or total > _UTC_OFFSET_MAX:
        return None
    return total


def format_utc_offset(offset_minutes: int) -> str:
    sign = "+" if offset_minutes >= 0 else "-"
    abs_m = abs(int(offset_minutes))
    hours, minutes = divmod(abs_m, 60)
    if minutes:
        return f"UTC{sign}{hours}:{minutes:02d}"
    return f"UTC{sign}{hours}"


def offset_minutes_to_tzinfo(offset_minutes: int) -> timezone:
    return timezone(timedelta(minutes=int(offset_minutes)))


def offset_minutes_to_iana(offset_minutes: int) -> str:
    """Fixed-offset IANA name for Helix (Etc/GMT signs are inverted vs UTC)."""
    mins = int(offset_minutes)
    if mins % 60 != 0:
        return "Etc/UTC"
    hours = mins // 60
    if hours == 0:
        return "Etc/UTC"
    if hours > 0:
        return f"Etc/GMT-{hours}"
    return f"Etc/GMT+{-hours}"


def _user_schedule_tz(db: Database, user_id: int) -> timezone:
    offset = db.get_schedule_utc_offset_minutes(user_id)
    if offset is None:
        return SCHEDULE_TZ
    return offset_minutes_to_tzinfo(offset)


def _sched_states():
    from bot import (
        STREAM_SCHEDULE_CONFIRM,
        STREAM_SCHEDULE_DURATION,
        STREAM_SCHEDULE_FIX_DAY,
        STREAM_SCHEDULE_FIX_GAME,
        STREAM_SCHEDULE_FIX_SLOTS,
        STREAM_SCHEDULE_FIX_TIME,
        STREAM_SCHEDULE_GAME,
        STREAM_SCHEDULE_MODE,
        STREAM_SCHEDULE_MORE,
        STREAM_SCHEDULE_PUBLISH,
        STREAM_SCHEDULE_TIME,
        STREAM_SCHEDULE_TZ,
    )

    return {
        "STREAM_SCHEDULE_CONFIRM": STREAM_SCHEDULE_CONFIRM,
        "STREAM_SCHEDULE_DURATION": STREAM_SCHEDULE_DURATION,
        "STREAM_SCHEDULE_FIX_DAY": STREAM_SCHEDULE_FIX_DAY,
        "STREAM_SCHEDULE_FIX_GAME": STREAM_SCHEDULE_FIX_GAME,
        "STREAM_SCHEDULE_FIX_SLOTS": STREAM_SCHEDULE_FIX_SLOTS,
        "STREAM_SCHEDULE_FIX_TIME": STREAM_SCHEDULE_FIX_TIME,
        "STREAM_SCHEDULE_GAME": STREAM_SCHEDULE_GAME,
        "STREAM_SCHEDULE_MODE": STREAM_SCHEDULE_MODE,
        "STREAM_SCHEDULE_MORE": STREAM_SCHEDULE_MORE,
        "STREAM_SCHEDULE_PUBLISH": STREAM_SCHEDULE_PUBLISH,
        "STREAM_SCHEDULE_TIME": STREAM_SCHEDULE_TIME,
        "STREAM_SCHEDULE_TZ": STREAM_SCHEDULE_TZ,
    }


_SCHEDULE_FIX_DAY_BETA_ID = "schedule-fix-day"


def _next_week_dates(today: date) -> list[date]:
    days_until_monday = (7 - today.weekday()) % 7
    if days_until_monday == 0:
        days_until_monday = 7
    monday = today + timedelta(days=days_until_monday)
    return [monday + timedelta(days=i) for i in range(7)]


def _parse_stream_time(raw: str) -> str | None:
    match = _STREAM_TIME_PATTERN.match(raw.strip())
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def _stream_schedule_show_finish(context: ContextTypes.DEFAULT_TYPE) -> bool:
    dates = context.user_data.get("stream_schedule_dates", [])
    index = int(context.user_data.get("stream_schedule_index", 0))
    return index >= 1 and index < len(dates) - 1


def _init_stream_schedule(
    context: ContextTypes.DEFAULT_TYPE, *, local_tz: timezone | None = None
) -> None:
    tz = local_tz or SCHEDULE_TZ
    today = datetime.now(tz).date()
    context.user_data["stream_schedule_dates"] = _next_week_dates(today)
    context.user_data["stream_schedule_index"] = 0
    context.user_data["stream_schedule_entries"] = []


def _owner_schedule_broadcaster_id(db: Database, user_id: int) -> str:
    sync = db.get_twitch_sync(user_id)
    if sync and sync.twitch_user_id:
        return str(sync.twitch_user_id)
    status = db.get_premium_status(user_id)
    return str(getattr(status, "twitch_user_id", "") or "")


def _schedule_segment_game(seg: dict) -> str:
    title = str(seg.get("title") or "").strip()
    cat = seg.get("category")
    if isinstance(cat, dict):
        return title or str(cat.get("name") or "").strip()
    return title


def _slots_on_local_day(
    twitch: TwitchClient,
    broadcaster_id: str,
    day: date,
    *,
    local_tz: timezone | None = None,
) -> list[dict]:
    tz = local_tz or SCHEDULE_TZ
    day_start = datetime(day.year, day.month, day.day, 0, 0, tzinfo=tz)
    start_iso = day_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    segs = twitch.get_schedule_segments(
        broadcaster_id, first=25, start_time=start_iso
    )
    out: list[dict] = []
    seen: set[str] = set()
    for seg in segs:
        sid = str(seg.get("id") or "")
        if not sid or sid in seen:
            continue
        raw = seg.get("start_time") or ""
        try:
            local = twitch._parse_schedule_time(str(raw)).astimezone(tz)
        except Exception:
            continue
        if local.date() != day:
            continue
        seen.add(sid)
        out.append(
            {
                "id": sid,
                "date": day,
                "time": f"{local.hour:02d}:{local.minute:02d}",
                "game": _schedule_segment_game(seg),
            }
        )
    out.sort(key=lambda s: str(s.get("time") or ""))
    return out


def _day_slots_view(context: ContextTypes.DEFAULT_TYPE) -> list[dict]:
    existing = list(context.user_data.get("stream_schedule_existing") or [])
    by_id: dict[str, dict] = {}
    for slot in existing:
        sid = str(slot.get("id") or "")
        if sid:
            by_id[sid] = dict(slot)
    for upd in context.user_data.get("stream_schedule_updates") or []:
        sid = str(upd.get("id") or "")
        if sid:
            by_id[sid] = {**by_id.get(sid, {}), **upd}
    slots = sorted(by_id.values(), key=lambda s: str(s.get("time") or ""))
    day = context.user_data.get("stream_schedule_fix_date")
    for entry in context.user_data.get("stream_schedule_entries") or []:
        if day is not None and entry.get("date") != day:
            continue
        slots.append(dict(entry))
    return slots


def _schedule_publish_error_text(exc: BaseException, date_raw: str, lang: str) -> str:
    from twitch import TwitchClient

    pretty = str(date_raw or "").strip()
    try:
        y, m, d = (int(x) for x in pretty.split("-", 2))
        pretty = format_stream_schedule_date(date(y, m, d), lang)
    except (TypeError, ValueError):
        pass
    detail = TwitchClient._schedule_error_detail(exc)
    raw = str(exc).lower()
    if TwitchClient.is_recurring_start_forbidden(exc):
        key = "stream_schedule_err_recurring_time"
    elif TwitchClient.is_overlapping_schedule(exc):
        key = "stream_schedule_err_overlap"
    elif TwitchClient.is_one_off_schedule_forbidden(exc):
        key = "stream_schedule_err_one_off"
    elif "401" in raw or "unauthorized" in detail:
        key = "stream_schedule_err_auth"
    elif "404" in raw or "not found" in detail:
        key = "stream_schedule_err_not_found"
    else:
        key = "stream_schedule_err_generic"
    return t(key, lang, date=pretty)


def _pending_schedule_preview(context: ContextTypes.DEFAULT_TYPE) -> list[dict]:
    items = list(context.user_data.get("stream_schedule_entries") or [])
    for upd in context.user_data.get("stream_schedule_updates") or []:
        items.append(
            {
                "date": upd["date"],
                "time": upd["time"],
                "game": upd.get("game") or "",
            }
        )
    return items


def _pending_schedule_deletes(context: ContextTypes.DEFAULT_TYPE) -> list[str]:
    return list(context.user_data.get("stream_schedule_deletes") or [])


def _has_pending_schedule_changes(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return bool(
        _pending_schedule_preview(context) or _pending_schedule_deletes(context)
    )


def _format_schedule_finish_text(context: ContextTypes.DEFAULT_TYPE, lang: str) -> str:
    items = _pending_schedule_preview(context)
    deletes = _pending_schedule_deletes(context)
    parts: list[str] = []
    if items:
        parts.append(format_stream_schedule_result(items, lang))
    if deletes:
        parts.append(t("stream_schedule_deleted_slots", lang, count=len(deletes)))
    return "\n".join(parts) if parts else "—"


def _remove_day_slot(context: ContextTypes.DEFAULT_TYPE, slot_id: str) -> None:
    sid = str(slot_id or "")
    if not sid:
        return
    existing = context.user_data.get("stream_schedule_existing") or []
    context.user_data["stream_schedule_existing"] = [
        s for s in existing if str(s.get("id") or "") != sid
    ]
    updates = context.user_data.get("stream_schedule_updates") or []
    context.user_data["stream_schedule_updates"] = [
        u for u in updates if str(u.get("id") or "") != sid
    ]
    deletes: list[str] = context.user_data.setdefault("stream_schedule_deletes", [])
    if sid not in deletes:
        deletes.append(sid)


async def _prompt_stream_schedule_game(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    _st = _sched_states()
    STREAM_SCHEDULE_CONFIRM = _st["STREAM_SCHEDULE_CONFIRM"]
    STREAM_SCHEDULE_DURATION = _st["STREAM_SCHEDULE_DURATION"]
    STREAM_SCHEDULE_FIX_DAY = _st["STREAM_SCHEDULE_FIX_DAY"]
    STREAM_SCHEDULE_FIX_GAME = _st["STREAM_SCHEDULE_FIX_GAME"]
    STREAM_SCHEDULE_FIX_SLOTS = _st["STREAM_SCHEDULE_FIX_SLOTS"]
    STREAM_SCHEDULE_FIX_TIME = _st["STREAM_SCHEDULE_FIX_TIME"]
    STREAM_SCHEDULE_GAME = _st["STREAM_SCHEDULE_GAME"]
    STREAM_SCHEDULE_MODE = _st["STREAM_SCHEDULE_MODE"]
    STREAM_SCHEDULE_MORE = _st["STREAM_SCHEDULE_MORE"]
    STREAM_SCHEDULE_PUBLISH = _st["STREAM_SCHEDULE_PUBLISH"]
    STREAM_SCHEDULE_TIME = _st["STREAM_SCHEDULE_TIME"]

    dates: list[date] = context.user_data["stream_schedule_dates"]
    index = int(context.user_data["stream_schedule_index"])
    current_date = dates[index]
    context.user_data.pop("stream_schedule_game", None)
    message = t(
        "stream_schedule_game_prompt",
        lang,
        date=format_stream_schedule_prompt_date(current_date, lang),
    )
    keyboard = stream_schedule_day_keyboard(
        lang, show_finish=_stream_schedule_show_finish(context)
    )
    if update.callback_query:
        await update.callback_query.edit_message_text("✓")
        await context.bot.send_message(
            reply_chat_id(update),
            message,
            reply_markup=keyboard,
        )
    else:
        await update.effective_message.reply_text(message, reply_markup=keyboard)
    await _pulse_wizard_keyboard(
        context.bot, reply_chat_id(update), lang, back=False
    )
    return STREAM_SCHEDULE_GAME


async def _prompt_add_another_slot(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    _st = _sched_states()
    STREAM_SCHEDULE_CONFIRM = _st["STREAM_SCHEDULE_CONFIRM"]
    STREAM_SCHEDULE_DURATION = _st["STREAM_SCHEDULE_DURATION"]
    STREAM_SCHEDULE_FIX_DAY = _st["STREAM_SCHEDULE_FIX_DAY"]
    STREAM_SCHEDULE_FIX_GAME = _st["STREAM_SCHEDULE_FIX_GAME"]
    STREAM_SCHEDULE_FIX_SLOTS = _st["STREAM_SCHEDULE_FIX_SLOTS"]
    STREAM_SCHEDULE_FIX_TIME = _st["STREAM_SCHEDULE_FIX_TIME"]
    STREAM_SCHEDULE_GAME = _st["STREAM_SCHEDULE_GAME"]
    STREAM_SCHEDULE_MODE = _st["STREAM_SCHEDULE_MODE"]
    STREAM_SCHEDULE_MORE = _st["STREAM_SCHEDULE_MORE"]
    STREAM_SCHEDULE_PUBLISH = _st["STREAM_SCHEDULE_PUBLISH"]
    STREAM_SCHEDULE_TIME = _st["STREAM_SCHEDULE_TIME"]

    markup = stream_schedule_more_keyboard(lang)
    if update.callback_query:
        await update.callback_query.edit_message_text(
            t("stream_schedule_more_prompt", lang),
            reply_markup=markup,
        )
    else:
        await update.effective_message.reply_text(
            t("stream_schedule_more_prompt", lang),
            reply_markup=markup,
        )
    return STREAM_SCHEDULE_MORE


async def _prompt_stream_schedule_time(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    _st = _sched_states()
    STREAM_SCHEDULE_CONFIRM = _st["STREAM_SCHEDULE_CONFIRM"]
    STREAM_SCHEDULE_DURATION = _st["STREAM_SCHEDULE_DURATION"]
    STREAM_SCHEDULE_FIX_DAY = _st["STREAM_SCHEDULE_FIX_DAY"]
    STREAM_SCHEDULE_FIX_GAME = _st["STREAM_SCHEDULE_FIX_GAME"]
    STREAM_SCHEDULE_FIX_SLOTS = _st["STREAM_SCHEDULE_FIX_SLOTS"]
    STREAM_SCHEDULE_FIX_TIME = _st["STREAM_SCHEDULE_FIX_TIME"]
    STREAM_SCHEDULE_GAME = _st["STREAM_SCHEDULE_GAME"]
    STREAM_SCHEDULE_MODE = _st["STREAM_SCHEDULE_MODE"]
    STREAM_SCHEDULE_MORE = _st["STREAM_SCHEDULE_MORE"]
    STREAM_SCHEDULE_PUBLISH = _st["STREAM_SCHEDULE_PUBLISH"]
    STREAM_SCHEDULE_TIME = _st["STREAM_SCHEDULE_TIME"]

    keyboard = stream_schedule_day_keyboard(
        lang,
        show_finish=False,
        show_skip=False,
    )
    await update.effective_message.reply_text(
        t("stream_schedule_time_prompt", lang),
        reply_markup=keyboard,
    )
    await _pulse_wizard_keyboard(
        context.bot, reply_chat_id(update), lang, back=False
    )
    return STREAM_SCHEDULE_TIME


async def _prompt_publish_on_twitch(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    _st = _sched_states()
    STREAM_SCHEDULE_PUBLISH = _st["STREAM_SCHEDULE_PUBLISH"]
    await context.bot.send_message(
        reply_chat_id(update),
        t("stream_schedule_publish_prompt", lang),
        reply_markup=stream_schedule_publish_keyboard(lang),
    )
    return STREAM_SCHEDULE_PUBLISH


async def _prompt_schedule_tz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    lang: str,
    *,
    resume: str,
) -> int:
    _st = _sched_states()
    STREAM_SCHEDULE_TZ = _st["STREAM_SCHEDULE_TZ"]
    user_id = update.effective_user.id
    db: Database = context.application.bot_data["db"]
    context.user_data["stream_schedule_tz_resume"] = resume
    current = db.get_schedule_utc_offset_minutes(user_id)
    if current is None:
        text = t("stream_schedule_tz_prompt", lang)
    else:
        text = t(
            "stream_schedule_tz_prompt_current",
            lang,
            tz=format_utc_offset(current),
        )
    chat_id = reply_chat_id(update)
    query = update.callback_query
    if query:
        try:
            await query.edit_message_text(text)
        except BadRequest:
            await context.bot.send_message(chat_id, text)
    else:
        await context.bot.send_message(chat_id, text)
    await _pulse_wizard_keyboard(context.bot, chat_id, lang, back=False)
    return STREAM_SCHEDULE_TZ


async def _prompt_duration_after_publish_yes(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    _st = _sched_states()
    STREAM_SCHEDULE_DURATION = _st["STREAM_SCHEDULE_DURATION"]
    user_id = update.effective_user.id
    text = t(
        (
            "stream_schedule_duration_prompt_keep"
            if context.user_data.get("stream_schedule_clear_mode") == "overlap"
            else "stream_schedule_duration_prompt"
        ),
        lang,
    )
    markup = stream_schedule_duration_keyboard(lang)
    chat_id = reply_chat_id(update)
    query = update.callback_query
    if query:
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except BadRequest:
            await context.bot.send_message(chat_id, text, reply_markup=markup)
    else:
        await context.bot.send_message(chat_id, text, reply_markup=markup)
    return STREAM_SCHEDULE_DURATION


async def _finish_stream_schedule(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    user_id = update.effective_user.id
    text = _format_schedule_finish_text(context, lang)
    chat_id = reply_chat_id(update)
    if update.callback_query:
        await update.callback_query.edit_message_text("✓")
        await context.bot.send_message(chat_id, text)
    else:
        await update.effective_message.reply_text(text)
    if not _has_pending_schedule_changes(context):
        context.user_data.clear()
        await context.bot.send_message(
            chat_id, t("menu_main", lang), reply_markup=_menu(lang, user_id)
        )
        return ConversationHandler.END
    return await _prompt_publish_on_twitch(update, context, lang)


async def _advance_stream_schedule_day(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    dates: list[date] = context.user_data["stream_schedule_dates"]
    index = int(context.user_data["stream_schedule_index"]) + 1
    context.user_data["stream_schedule_index"] = index
    context.user_data.pop("stream_schedule_game", None)
    if index >= len(dates):
        return await _finish_stream_schedule(update, context, lang)
    return await _prompt_stream_schedule_game(update, context, lang)



async def start_stream_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _st = _sched_states()
    STREAM_SCHEDULE_CONFIRM = _st["STREAM_SCHEDULE_CONFIRM"]
    STREAM_SCHEDULE_MODE = _st["STREAM_SCHEDULE_MODE"]

    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    context.user_data.clear()
    db: Database = context.application.bot_data["db"]
    if not beta_features.is_enabled(db, user_id, _SCHEDULE_FIX_DAY_BETA_ID):
        await update.effective_message.reply_text(
            t("stream_schedule_intro", lang),
            parse_mode=ParseMode.HTML,
        )
        await update.effective_message.reply_text(
            t("stream_schedule_confirm", lang),
            reply_markup=stream_schedule_confirm_keyboard(lang),
        )
        return STREAM_SCHEDULE_CONFIRM
    await update.effective_message.reply_text(
        t("stream_schedule_mode_intro", lang),
        parse_mode=ParseMode.HTML,
        reply_markup=stream_schedule_mode_keyboard(lang),
    )
    return STREAM_SCHEDULE_MODE


async def stream_schedule_tz_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    resume = (query.data or "").rsplit(":", 1)[-1]
    if resume not in ("mode", "confirm"):
        resume = "mode"
    return await _prompt_schedule_tz(update, context, lang, resume=resume)


async def stream_schedule_tz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _st = _sched_states()
    STREAM_SCHEDULE_CONFIRM = _st["STREAM_SCHEDULE_CONFIRM"]
    STREAM_SCHEDULE_MODE = _st["STREAM_SCHEDULE_MODE"]
    STREAM_SCHEDULE_TZ = _st["STREAM_SCHEDULE_TZ"]

    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    raw = (update.effective_message.text or "").strip()
    if is_menu_button(raw) or raw in all_wizard_nav_buttons():
        context.user_data.clear()
        await update.effective_message.reply_text(
            t("menu_main", lang), reply_markup=_menu(lang, user_id)
        )
        return ConversationHandler.END
    offset = parse_utc_offset_text(raw)
    if offset is None:
        await update.effective_message.reply_text(t("stream_schedule_tz_invalid", lang))
        return STREAM_SCHEDULE_TZ

    db: Database = context.application.bot_data["db"]
    db.set_schedule_utc_offset_minutes(user_id, offset)
    label = format_utc_offset(offset)
    await update.effective_message.reply_text(
        t("stream_schedule_tz_saved", lang, tz=label)
    )
    resume = context.user_data.pop("stream_schedule_tz_resume", "mode")
    if resume == "publish_prompt":
        return await _prompt_publish_on_twitch(update, context, lang)
    if resume == "duration":
        return await _prompt_duration_after_publish_yes(update, context, lang)
    if resume == "confirm":
        await update.effective_message.reply_text(
            t("stream_schedule_confirm", lang),
            reply_markup=stream_schedule_confirm_keyboard(lang),
        )
        return STREAM_SCHEDULE_CONFIRM
    await update.effective_message.reply_text(
        t("stream_schedule_mode_intro", lang),
        parse_mode=ParseMode.HTML,
        reply_markup=stream_schedule_mode_keyboard(lang),
    )
    return STREAM_SCHEDULE_MODE


async def stream_schedule_mode_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    _st = _sched_states()
    STREAM_SCHEDULE_CONFIRM = _st["STREAM_SCHEDULE_CONFIRM"]
    STREAM_SCHEDULE_FIX_DAY = _st["STREAM_SCHEDULE_FIX_DAY"]

    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    mode = (query.data or "").split(":")[-1]
    if mode == "week":
        await query.edit_message_text(
            t("stream_schedule_intro", lang),
            parse_mode=ParseMode.HTML,
            reply_markup=stream_schedule_confirm_keyboard(lang),
        )
        return STREAM_SCHEDULE_CONFIRM

    # Day fix mode — remaining days of the current week (Mon–Sun), including today.
    db: Database = context.application.bot_data["db"]
    local_tz = _user_schedule_tz(db, query.from_user.id)
    today = datetime.now(local_tz).date()
    monday = today - timedelta(days=today.weekday())
    dates = [monday + timedelta(days=i) for i in range(7) if monday + timedelta(days=i) >= today]
    context.user_data["stream_schedule_fix_dates"] = dates
    await query.edit_message_text(
        t("stream_schedule_fix_day_prompt", lang),
        reply_markup=stream_schedule_fix_day_keyboard(lang, dates),
    )
    return STREAM_SCHEDULE_FIX_DAY


async def _occupied_slots_text(lang: str, day_date: date, slots: list[dict]) -> str:
    header_date = format_stream_schedule_prompt_date(day_date, lang)
    if not slots:
        return t("stream_schedule_occupied_empty", lang, date=header_date)
    lines = [t("stream_schedule_occupied_header", lang, date=header_date), ""]
    for slot in slots:
        lines.append(
            t(
                "stream_schedule_occupied_line",
                lang,
                time=slot.get("time") or "",
                game=slot.get("game") or "",
            )
        )
    return "\n".join(lines)


async def _show_day_slots(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    _st = _sched_states()
    STREAM_SCHEDULE_CONFIRM = _st["STREAM_SCHEDULE_CONFIRM"]
    STREAM_SCHEDULE_DURATION = _st["STREAM_SCHEDULE_DURATION"]
    STREAM_SCHEDULE_FIX_DAY = _st["STREAM_SCHEDULE_FIX_DAY"]
    STREAM_SCHEDULE_FIX_GAME = _st["STREAM_SCHEDULE_FIX_GAME"]
    STREAM_SCHEDULE_FIX_SLOTS = _st["STREAM_SCHEDULE_FIX_SLOTS"]
    STREAM_SCHEDULE_FIX_TIME = _st["STREAM_SCHEDULE_FIX_TIME"]
    STREAM_SCHEDULE_GAME = _st["STREAM_SCHEDULE_GAME"]
    STREAM_SCHEDULE_MODE = _st["STREAM_SCHEDULE_MODE"]
    STREAM_SCHEDULE_MORE = _st["STREAM_SCHEDULE_MORE"]
    STREAM_SCHEDULE_PUBLISH = _st["STREAM_SCHEDULE_PUBLISH"]
    STREAM_SCHEDULE_TIME = _st["STREAM_SCHEDULE_TIME"]

    user_id = update.effective_user.id
    day_date: date = context.user_data["stream_schedule_fix_date"]
    slots = _day_slots_view(context)
    text = await _occupied_slots_text(lang, day_date, slots)
    markup = stream_schedule_occupied_keyboard(lang, slots)
    chat_id = reply_chat_id(update)
    query = update.callback_query
    if query:
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except BadRequest:
            await context.bot.send_message(chat_id, text, reply_markup=markup)
    else:
        await context.bot.send_message(chat_id, text, reply_markup=markup)
    return STREAM_SCHEDULE_FIX_SLOTS


async def _prompt_stream_schedule_fix_game(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    _st = _sched_states()
    STREAM_SCHEDULE_CONFIRM = _st["STREAM_SCHEDULE_CONFIRM"]
    STREAM_SCHEDULE_DURATION = _st["STREAM_SCHEDULE_DURATION"]
    STREAM_SCHEDULE_FIX_DAY = _st["STREAM_SCHEDULE_FIX_DAY"]
    STREAM_SCHEDULE_FIX_GAME = _st["STREAM_SCHEDULE_FIX_GAME"]
    STREAM_SCHEDULE_FIX_SLOTS = _st["STREAM_SCHEDULE_FIX_SLOTS"]
    STREAM_SCHEDULE_FIX_TIME = _st["STREAM_SCHEDULE_FIX_TIME"]
    STREAM_SCHEDULE_GAME = _st["STREAM_SCHEDULE_GAME"]
    STREAM_SCHEDULE_MODE = _st["STREAM_SCHEDULE_MODE"]
    STREAM_SCHEDULE_MORE = _st["STREAM_SCHEDULE_MORE"]
    STREAM_SCHEDULE_PUBLISH = _st["STREAM_SCHEDULE_PUBLISH"]
    STREAM_SCHEDULE_TIME = _st["STREAM_SCHEDULE_TIME"]

    day_date: date = context.user_data["stream_schedule_fix_date"]
    context.user_data.pop("stream_schedule_fix_game", None)
    text = t(
        "stream_schedule_fix_game_prompt",
        lang,
        date=format_stream_schedule_prompt_date(day_date, lang),
    )
    query = update.callback_query
    if query:
        try:
            await query.edit_message_text("✓")
        except BadRequest:
            pass
    await context.bot.send_message(
        reply_chat_id(update),
        text,
        reply_markup=_wizard(lang, back=False),
    )
    return STREAM_SCHEDULE_FIX_GAME


async def stream_schedule_fix_day_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    _st = _sched_states()
    STREAM_SCHEDULE_CONFIRM = _st["STREAM_SCHEDULE_CONFIRM"]
    STREAM_SCHEDULE_DURATION = _st["STREAM_SCHEDULE_DURATION"]
    STREAM_SCHEDULE_FIX_DAY = _st["STREAM_SCHEDULE_FIX_DAY"]
    STREAM_SCHEDULE_FIX_GAME = _st["STREAM_SCHEDULE_FIX_GAME"]
    STREAM_SCHEDULE_FIX_SLOTS = _st["STREAM_SCHEDULE_FIX_SLOTS"]
    STREAM_SCHEDULE_FIX_TIME = _st["STREAM_SCHEDULE_FIX_TIME"]
    STREAM_SCHEDULE_GAME = _st["STREAM_SCHEDULE_GAME"]
    STREAM_SCHEDULE_MODE = _st["STREAM_SCHEDULE_MODE"]
    STREAM_SCHEDULE_MORE = _st["STREAM_SCHEDULE_MORE"]
    STREAM_SCHEDULE_PUBLISH = _st["STREAM_SCHEDULE_PUBLISH"]
    STREAM_SCHEDULE_TIME = _st["STREAM_SCHEDULE_TIME"]

    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    dates: list[date] = context.user_data.get("stream_schedule_fix_dates") or []
    try:
        idx = int((query.data or "").split(":")[-1])
    except ValueError:
        idx = -1
    if idx < 0 or idx >= len(dates):
        await query.edit_message_text(t("stream_schedule_fix_day_prompt", lang))
        return STREAM_SCHEDULE_FIX_DAY

    day_date = dates[idx]
    context.user_data["stream_schedule_fix_date"] = day_date
    context.user_data["stream_schedule_clear_mode"] = "overlap"
    context.user_data.setdefault("stream_schedule_entries", [])
    context.user_data.setdefault("stream_schedule_updates", [])
    context.user_data.setdefault("stream_schedule_deletes", [])
    context.user_data.pop("stream_schedule_edit_id", None)

    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    broadcaster_id = _owner_schedule_broadcaster_id(db, user_id)
    existing: list[dict] = []
    if broadcaster_id:
        try:
            existing = await asyncio.to_thread(
                _slots_on_local_day,
                twitch,
                broadcaster_id,
                day_date,
                local_tz=_user_schedule_tz(db, user_id),
            )
        except Exception:
            logger.exception("Failed to load Twitch schedule slots for user=%s", user_id)
    context.user_data["stream_schedule_existing"] = existing
    return await _show_day_slots(update, context, lang)


async def stream_schedule_fix_add_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    context.user_data.pop("stream_schedule_edit_id", None)
    return await _prompt_stream_schedule_fix_game(update, context, lang)


async def stream_schedule_fix_edit_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    slots = _day_slots_view(context)
    try:
        idx = int((query.data or "").split(":")[-1])
    except ValueError:
        idx = -1
    if idx < 0 or idx >= len(slots) or not slots[idx].get("id"):
        return await _show_day_slots(update, context, lang)
    context.user_data["stream_schedule_edit_id"] = slots[idx]["id"]
    return await _prompt_stream_schedule_fix_game(update, context, lang)


async def stream_schedule_fix_delete_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    slots = _day_slots_view(context)
    try:
        idx = int((query.data or "").split(":")[-1])
    except ValueError:
        idx = -1
    if idx < 0 or idx >= len(slots) or not slots[idx].get("id"):
        return await _show_day_slots(update, context, lang)
    _remove_day_slot(context, str(slots[idx]["id"]))
    context.user_data.pop("stream_schedule_edit_id", None)
    return await _show_day_slots(update, context, lang)


async def stream_schedule_fix_slots_done_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    return await _finish_stream_schedule(update, context, lang)


async def stream_schedule_noop_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    _st = _sched_states()
    STREAM_SCHEDULE_CONFIRM = _st["STREAM_SCHEDULE_CONFIRM"]
    STREAM_SCHEDULE_DURATION = _st["STREAM_SCHEDULE_DURATION"]
    STREAM_SCHEDULE_FIX_DAY = _st["STREAM_SCHEDULE_FIX_DAY"]
    STREAM_SCHEDULE_FIX_GAME = _st["STREAM_SCHEDULE_FIX_GAME"]
    STREAM_SCHEDULE_FIX_SLOTS = _st["STREAM_SCHEDULE_FIX_SLOTS"]
    STREAM_SCHEDULE_FIX_TIME = _st["STREAM_SCHEDULE_FIX_TIME"]
    STREAM_SCHEDULE_GAME = _st["STREAM_SCHEDULE_GAME"]
    STREAM_SCHEDULE_MODE = _st["STREAM_SCHEDULE_MODE"]
    STREAM_SCHEDULE_MORE = _st["STREAM_SCHEDULE_MORE"]
    STREAM_SCHEDULE_PUBLISH = _st["STREAM_SCHEDULE_PUBLISH"]
    STREAM_SCHEDULE_TIME = _st["STREAM_SCHEDULE_TIME"]

    await update.callback_query.answer()
    return STREAM_SCHEDULE_FIX_SLOTS


async def stream_schedule_more_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    more = (query.data or "").split(":")[-1] == "1"
    if more:
        if context.user_data.get("stream_schedule_fix_date"):
            context.user_data.pop("stream_schedule_edit_id", None)
            return await _prompt_stream_schedule_fix_game(update, context, lang)
        return await _prompt_stream_schedule_game(update, context, lang)
    if context.user_data.get("stream_schedule_fix_date"):
        return await _finish_stream_schedule(update, context, lang)
    return await _advance_stream_schedule_day(update, context, lang)


async def stream_schedule_confirm_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    confirmed = query.data.split(":")[-1] == "1"
    if not confirmed:
        context.user_data.clear()
        await query.edit_message_text(t("cancelled", lang))
        await context.bot.send_message(
            reply_chat_id(update),
            t("menu_main", lang),
            reply_markup=_menu(lang, query.from_user.id),
        )
        return ConversationHandler.END
    _init_stream_schedule(
        context,
        local_tz=_user_schedule_tz(
            context.application.bot_data["db"], query.from_user.id
        ),
    )
    return await _prompt_stream_schedule_game(update, context, lang)


async def stream_schedule_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _st = _sched_states()
    STREAM_SCHEDULE_CONFIRM = _st["STREAM_SCHEDULE_CONFIRM"]
    STREAM_SCHEDULE_DURATION = _st["STREAM_SCHEDULE_DURATION"]
    STREAM_SCHEDULE_FIX_DAY = _st["STREAM_SCHEDULE_FIX_DAY"]
    STREAM_SCHEDULE_FIX_GAME = _st["STREAM_SCHEDULE_FIX_GAME"]
    STREAM_SCHEDULE_FIX_SLOTS = _st["STREAM_SCHEDULE_FIX_SLOTS"]
    STREAM_SCHEDULE_FIX_TIME = _st["STREAM_SCHEDULE_FIX_TIME"]
    STREAM_SCHEDULE_GAME = _st["STREAM_SCHEDULE_GAME"]
    STREAM_SCHEDULE_MODE = _st["STREAM_SCHEDULE_MODE"]
    STREAM_SCHEDULE_MORE = _st["STREAM_SCHEDULE_MORE"]
    STREAM_SCHEDULE_PUBLISH = _st["STREAM_SCHEDULE_PUBLISH"]
    STREAM_SCHEDULE_TIME = _st["STREAM_SCHEDULE_TIME"]

    lang = _user_lang(context, update.effective_user.id)
    text = (update.effective_message.text or "").strip()
    if is_menu_button(text) or text in all_wizard_nav_buttons():
        await update.effective_message.reply_text(t("finish_setup_first", lang))
        return STREAM_SCHEDULE_GAME
    if not text:
        await update.effective_message.reply_text(t("stream_schedule_game_empty", lang))
        return STREAM_SCHEDULE_GAME
    context.user_data["stream_schedule_game"] = text
    return await _prompt_stream_schedule_time(update, context, lang)


async def stream_schedule_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _st = _sched_states()
    STREAM_SCHEDULE_CONFIRM = _st["STREAM_SCHEDULE_CONFIRM"]
    STREAM_SCHEDULE_DURATION = _st["STREAM_SCHEDULE_DURATION"]
    STREAM_SCHEDULE_FIX_DAY = _st["STREAM_SCHEDULE_FIX_DAY"]
    STREAM_SCHEDULE_FIX_GAME = _st["STREAM_SCHEDULE_FIX_GAME"]
    STREAM_SCHEDULE_FIX_SLOTS = _st["STREAM_SCHEDULE_FIX_SLOTS"]
    STREAM_SCHEDULE_FIX_TIME = _st["STREAM_SCHEDULE_FIX_TIME"]
    STREAM_SCHEDULE_GAME = _st["STREAM_SCHEDULE_GAME"]
    STREAM_SCHEDULE_MODE = _st["STREAM_SCHEDULE_MODE"]
    STREAM_SCHEDULE_MORE = _st["STREAM_SCHEDULE_MORE"]
    STREAM_SCHEDULE_PUBLISH = _st["STREAM_SCHEDULE_PUBLISH"]
    STREAM_SCHEDULE_TIME = _st["STREAM_SCHEDULE_TIME"]

    lang = _user_lang(context, update.effective_user.id)
    text = (update.effective_message.text or "").strip()
    if is_menu_button(text) or text in all_wizard_nav_buttons():
        await update.effective_message.reply_text(t("finish_setup_first", lang))
        return STREAM_SCHEDULE_TIME
    parsed_time = _parse_stream_time(text)
    if not parsed_time:
        await update.effective_message.reply_text(t("stream_schedule_time_invalid", lang))
        return STREAM_SCHEDULE_TIME
    dates: list[date] = context.user_data["stream_schedule_dates"]
    index = int(context.user_data["stream_schedule_index"])
    entries: list[dict] = context.user_data.setdefault("stream_schedule_entries", [])
    entries.append(
        {
            "date": dates[index],
            "time": parsed_time,
            "game": context.user_data.pop("stream_schedule_game", ""),
        }
    )
    return await _prompt_add_another_slot(update, context, lang)


async def stream_schedule_fix_game(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    _st = _sched_states()
    STREAM_SCHEDULE_CONFIRM = _st["STREAM_SCHEDULE_CONFIRM"]
    STREAM_SCHEDULE_DURATION = _st["STREAM_SCHEDULE_DURATION"]
    STREAM_SCHEDULE_FIX_DAY = _st["STREAM_SCHEDULE_FIX_DAY"]
    STREAM_SCHEDULE_FIX_GAME = _st["STREAM_SCHEDULE_FIX_GAME"]
    STREAM_SCHEDULE_FIX_SLOTS = _st["STREAM_SCHEDULE_FIX_SLOTS"]
    STREAM_SCHEDULE_FIX_TIME = _st["STREAM_SCHEDULE_FIX_TIME"]
    STREAM_SCHEDULE_GAME = _st["STREAM_SCHEDULE_GAME"]
    STREAM_SCHEDULE_MODE = _st["STREAM_SCHEDULE_MODE"]
    STREAM_SCHEDULE_MORE = _st["STREAM_SCHEDULE_MORE"]
    STREAM_SCHEDULE_PUBLISH = _st["STREAM_SCHEDULE_PUBLISH"]
    STREAM_SCHEDULE_TIME = _st["STREAM_SCHEDULE_TIME"]

    lang = _user_lang(context, update.effective_user.id)
    text = (update.effective_message.text or "").strip()
    if is_menu_button(text) or text in all_wizard_nav_buttons():
        await update.effective_message.reply_text(t("finish_setup_first", lang))
        return STREAM_SCHEDULE_FIX_GAME
    if not text:
        await update.effective_message.reply_text(t("stream_schedule_game_empty", lang))
        return STREAM_SCHEDULE_FIX_GAME
    context.user_data["stream_schedule_fix_game"] = text
    return await _prompt_stream_schedule_fix_time(update, context, lang)


async def _prompt_stream_schedule_fix_time(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    _st = _sched_states()
    STREAM_SCHEDULE_CONFIRM = _st["STREAM_SCHEDULE_CONFIRM"]
    STREAM_SCHEDULE_DURATION = _st["STREAM_SCHEDULE_DURATION"]
    STREAM_SCHEDULE_FIX_DAY = _st["STREAM_SCHEDULE_FIX_DAY"]
    STREAM_SCHEDULE_FIX_GAME = _st["STREAM_SCHEDULE_FIX_GAME"]
    STREAM_SCHEDULE_FIX_SLOTS = _st["STREAM_SCHEDULE_FIX_SLOTS"]
    STREAM_SCHEDULE_FIX_TIME = _st["STREAM_SCHEDULE_FIX_TIME"]
    STREAM_SCHEDULE_GAME = _st["STREAM_SCHEDULE_GAME"]
    STREAM_SCHEDULE_MODE = _st["STREAM_SCHEDULE_MODE"]
    STREAM_SCHEDULE_MORE = _st["STREAM_SCHEDULE_MORE"]
    STREAM_SCHEDULE_PUBLISH = _st["STREAM_SCHEDULE_PUBLISH"]
    STREAM_SCHEDULE_TIME = _st["STREAM_SCHEDULE_TIME"]

    await update.effective_message.reply_text(
        t("stream_schedule_fix_time_prompt", lang),
        reply_markup=_wizard(lang, back=False),
    )
    return STREAM_SCHEDULE_FIX_TIME


async def stream_schedule_fix_time(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    _st = _sched_states()
    STREAM_SCHEDULE_CONFIRM = _st["STREAM_SCHEDULE_CONFIRM"]
    STREAM_SCHEDULE_DURATION = _st["STREAM_SCHEDULE_DURATION"]
    STREAM_SCHEDULE_FIX_DAY = _st["STREAM_SCHEDULE_FIX_DAY"]
    STREAM_SCHEDULE_FIX_GAME = _st["STREAM_SCHEDULE_FIX_GAME"]
    STREAM_SCHEDULE_FIX_SLOTS = _st["STREAM_SCHEDULE_FIX_SLOTS"]
    STREAM_SCHEDULE_FIX_TIME = _st["STREAM_SCHEDULE_FIX_TIME"]
    STREAM_SCHEDULE_GAME = _st["STREAM_SCHEDULE_GAME"]
    STREAM_SCHEDULE_MODE = _st["STREAM_SCHEDULE_MODE"]
    STREAM_SCHEDULE_MORE = _st["STREAM_SCHEDULE_MORE"]
    STREAM_SCHEDULE_PUBLISH = _st["STREAM_SCHEDULE_PUBLISH"]
    STREAM_SCHEDULE_TIME = _st["STREAM_SCHEDULE_TIME"]

    lang = _user_lang(context, update.effective_user.id)
    text = (update.effective_message.text or "").strip()
    if is_menu_button(text) or text in all_wizard_nav_buttons():
        await update.effective_message.reply_text(t("finish_setup_first", lang))
        return STREAM_SCHEDULE_FIX_TIME
    parsed_time = _parse_stream_time(text)
    if not parsed_time:
        await update.effective_message.reply_text(
            t("stream_schedule_time_invalid", lang)
        )
        return STREAM_SCHEDULE_FIX_TIME
    day_date: date = context.user_data["stream_schedule_fix_date"]
    game_text = context.user_data.pop("stream_schedule_fix_game", "")
    edit_id = context.user_data.pop("stream_schedule_edit_id", None)
    if edit_id:
        updates: list[dict] = context.user_data.setdefault("stream_schedule_updates", [])
        found = False
        for upd in updates:
            if upd.get("id") == edit_id:
                upd["date"] = day_date
                upd["time"] = parsed_time
                upd["game"] = game_text
                found = True
                break
        if not found:
            updates.append(
                {
                    "id": edit_id,
                    "date": day_date,
                    "time": parsed_time,
                    "game": game_text,
                }
            )
        return await _show_day_slots(update, context, lang)
    entries: list[dict] = context.user_data.setdefault("stream_schedule_entries", [])
    entries.append({"date": day_date, "time": parsed_time, "game": game_text})
    return await _prompt_add_another_slot(update, context, lang)


async def stream_schedule_skip_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    return await _advance_stream_schedule_day(update, context, lang)


async def stream_schedule_finish_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    context.user_data.pop("stream_schedule_game", None)
    return await _finish_stream_schedule(update, context, lang)


def _pending_schedule_publishes(application: Application) -> dict[int, dict]:
    return application.bot_data.setdefault("pending_schedule_publishes", {})


async def stream_schedule_publish_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    _st = _sched_states()
    STREAM_SCHEDULE_CONFIRM = _st["STREAM_SCHEDULE_CONFIRM"]
    STREAM_SCHEDULE_DURATION = _st["STREAM_SCHEDULE_DURATION"]
    STREAM_SCHEDULE_FIX_DAY = _st["STREAM_SCHEDULE_FIX_DAY"]
    STREAM_SCHEDULE_FIX_GAME = _st["STREAM_SCHEDULE_FIX_GAME"]
    STREAM_SCHEDULE_FIX_SLOTS = _st["STREAM_SCHEDULE_FIX_SLOTS"]
    STREAM_SCHEDULE_FIX_TIME = _st["STREAM_SCHEDULE_FIX_TIME"]
    STREAM_SCHEDULE_GAME = _st["STREAM_SCHEDULE_GAME"]
    STREAM_SCHEDULE_MODE = _st["STREAM_SCHEDULE_MODE"]
    STREAM_SCHEDULE_MORE = _st["STREAM_SCHEDULE_MORE"]
    STREAM_SCHEDULE_PUBLISH = _st["STREAM_SCHEDULE_PUBLISH"]
    STREAM_SCHEDULE_TIME = _st["STREAM_SCHEDULE_TIME"]

    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    publish = query.data.split(":")[-1] == "1"
    entries = context.user_data.get("stream_schedule_entries", [])
    updates = context.user_data.get("stream_schedule_updates", [])
    deletes = context.user_data.get("stream_schedule_deletes", [])
    if not publish or (not entries and not updates and not deletes):
        context.user_data.clear()
        await query.edit_message_text(t("cancelled", lang) if not publish else "—")
        await context.bot.send_message(
            reply_chat_id(update), t("menu_main", lang), reply_markup=_menu(lang, user_id)
        )
        return ConversationHandler.END

    db: Database = context.application.bot_data["db"]
    if not await prem.has_feature(context.bot, db, user_id, "schedule_publish"):
        from premium_handlers import send_premium_screen

        context.user_data.clear()
        await query.edit_message_text(
            t("premium_gate", lang, action=t("premium_gate_action_cancel", lang))
        )
        await send_premium_screen(context.bot, user_id, lang, db, update=update)
        return ConversationHandler.END

    if db.get_schedule_utc_offset_minutes(user_id) is None:
        return await _prompt_schedule_tz(update, context, lang, resume="duration")
    return await _prompt_duration_after_publish_yes(update, context, lang)


async def stream_schedule_duration_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    raw = (query.data or "").rsplit(":", 1)[-1]
    try:
        hours = int(raw)
    except ValueError:
        hours = 0
    duration_min = (
        _SCHEDULE_DEFAULT_DURATION_MIN if hours <= 0 else max(1, hours) * 60
    )
    entries = context.user_data.get("stream_schedule_entries", [])
    updates = context.user_data.get("stream_schedule_updates", [])
    deletes = context.user_data.get("stream_schedule_deletes", [])
    if not entries and not updates and not deletes:
        context.user_data.clear()
        await query.edit_message_text(t("stream_schedule_publish_fail", lang, error="no data"))
        await context.bot.send_message(
            reply_chat_id(update), t("menu_main", lang), reply_markup=_menu(lang, user_id)
        )
        return ConversationHandler.END

    db: Database = context.application.bot_data["db"]
    clear_mode = context.user_data.get("stream_schedule_clear_mode", "all")
    offset_minutes = db.get_schedule_utc_offset_minutes(user_id)
    if offset_minutes is None:
        return await _prompt_schedule_tz(update, context, lang, resume="duration")

    def _iso_date(value: date | str) -> str:
        if isinstance(value, date):
            return value.isoformat()
        return str(value)

    _pending_schedule_publishes(context.application)[user_id] = {
        "entries": [
            {"date": _iso_date(e["date"]), "time": e["time"], "game": e["game"]}
            for e in entries
        ],
        "updates": [
            {
                "id": u["id"],
                "date": _iso_date(u["date"]),
                "time": u["time"],
                "game": u.get("game") or "",
            }
            for u in updates
            if u.get("id")
        ],
        "deletes": [str(d) for d in deletes if d],
        "duration": duration_min,
        "clear_mode": clear_mode,
        "utc_offset_minutes": int(offset_minutes),
    }
    context.user_data.clear()
    return await _start_schedule_publish_auth(update, context, user_id, lang)


async def _start_schedule_publish_auth(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    lang: str,
) -> int:
    from config import twitch_oauth_redirect_uri
    from health import create_oauth_state
    from twitch import SCHEDULE_OAUTH_SCOPES, SCHEDULE_SCOPE, TwitchClient

    query = update.callback_query
    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    sync = db.get_twitch_sync(user_id)
    if sync and sync.refresh_token:
        try:
            token_data = await asyncio.to_thread(
                twitch.refresh_user_token, sync.refresh_token
            )
            access = token_data.get("access_token") or ""
            refresh = token_data.get("refresh_token") or sync.refresh_token
            if access and await asyncio.to_thread(
                twitch.token_has_scope, access, SCHEDULE_SCOPE
            ):
                if refresh != sync.refresh_token:
                    db.update_twitch_sync_tokens(
                        user_id,
                        refresh,
                        last_sync_at=sync.last_sync_at
                        or datetime.now(timezone.utc).isoformat(),
                        next_sync_at=sync.next_sync_at,
                    )
                if query:
                    await query.edit_message_text(t("stream_schedule_publishing", lang))
                await _complete_schedule_publish(
                    context.application,
                    user_id,
                    None,
                    {
                        "access_token": access,
                        "refresh_token": "",
                        "twitch_user_id": sync.twitch_user_id,
                    },
                )
                return ConversationHandler.END
        except Exception:
            logger.exception(
                "Saved Twitch token unusable for schedule publish (user=%s)", user_id
            )

    chat_id = reply_chat_id(update)
    redirect_uri = twitch_oauth_redirect_uri()
    if not redirect_uri:
        text = t("stream_schedule_publish_auth_unavailable", lang)
        if query:
            await query.edit_message_text(text)
        else:
            await context.bot.send_message(chat_id, text)
        await context.bot.send_message(
            chat_id, t("menu_main", lang), reply_markup=_menu(lang, user_id)
        )
        return ConversationHandler.END

    state = create_oauth_state(user_id, lang, purpose="schedule")
    url = twitch.build_authorize_url(
        redirect_uri=redirect_uri, state=state, scopes=SCHEDULE_OAUTH_SCOPES
    )
    auth_text = t("stream_schedule_publish_auth", lang)
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton(t("stream_schedule_publish_auth_button", lang), url=url)]]
    )
    if query:
        await query.edit_message_text(auth_text, reply_markup=markup)
    else:
        await context.bot.send_message(chat_id, auth_text, reply_markup=markup)
    return ConversationHandler.END


async def _complete_schedule_publish(
    application: Application,
    owner_id: int,
    error: str | None,
    token_info: dict[str, str] | None,
) -> None:
    from twitch import TwitchClient

    db: Database = application.bot_data["db"]
    lang = db.get_user_locale(owner_id) or DEFAULT_LOCALE
    if error:
        await application.bot.send_message(
            owner_id,
            t("stream_schedule_publish_fail", lang, error=error),
            reply_markup=_menu(lang, owner_id),
        )
        return

    pending = _pending_schedule_publishes(application).pop(owner_id, None)
    if isinstance(pending, list):
        # Legacy in-memory shape from older deploys
        entries, duration_min = pending, _SCHEDULE_DEFAULT_DURATION_MIN
        clear_mode = "all"
        updates: list[dict] = []
        deletes: list[str] = []
        offset_minutes = None
    elif isinstance(pending, dict):
        entries = pending.get("entries") or []
        updates = pending.get("updates") or []
        deletes = pending.get("deletes") or []
        duration_min = int(pending.get("duration") or _SCHEDULE_DEFAULT_DURATION_MIN)
        clear_mode = pending.get("clear_mode") or "all"
        offset_minutes = pending.get("utc_offset_minutes")
    else:
        entries, duration_min = [], _SCHEDULE_DEFAULT_DURATION_MIN
        clear_mode = "all"
        updates = []
        deletes = []
        offset_minutes = None
    if offset_minutes is None:
        offset_minutes = db.get_schedule_utc_offset_minutes(owner_id)
    if offset_minutes is None:
        offset_minutes = int(SCHEDULE_TZ.utcoffset(None).total_seconds() // 60)
    local_tz = offset_minutes_to_tzinfo(int(offset_minutes))
    tz_name = offset_minutes_to_iana(int(offset_minutes))
    if (not entries and not updates and not deletes) or not token_info:
        await application.bot.send_message(
            owner_id,
            t("stream_schedule_publish_fail", lang, error="no data"),
            reply_markup=_menu(lang, owner_id),
        )
        return

    access = token_info.get("access_token", "")
    twitch_user_id = token_info.get("twitch_user_id", "")
    refresh = token_info.get("refresh_token", "")
    twitch: TwitchClient = application.bot_data["twitch"]

    if clear_mode not in ("overlap", "none"):
        try:
            unique_dates = {e.get("date") for e in entries if e.get("date")}
            if clear_mode == "day" and len(unique_dates) == 1:
                first = next(iter(unique_dates))
                y, m, d = (int(x) for x in str(first).split("-", 2))
                day_start_local = datetime(y, m, d, 0, 0, tzinfo=local_tz)
                day_start_iso = day_start_local.astimezone(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                await asyncio.to_thread(
                    twitch.clear_schedule_for_day,
                    access,
                    twitch_user_id,
                    start_time=day_start_iso,
                )
            else:
                await asyncio.to_thread(
                    twitch.clear_channel_schedule, access, twitch_user_id
                )
        except Exception as exc:
            _log_schedule_clear_failed(type(exc).__name__)

    def _start_and_category(item: dict) -> tuple[str, str, str]:
        hour, minute = (int(x) for x in item["time"].split(":", 1))
        y, m, d = (int(x) for x in str(item["date"]).split("-", 2))
        local_dt = datetime(y, m, d, hour, minute, tzinfo=local_tz)
        start_iso = local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        game_text = item.get("game", "")
        category_id = ""
        if game_text:
            try:
                cats = twitch.search_categories(game_text)
                if cats:
                    category_id = cats[0]["id"]
            except Exception:
                pass
        return start_iso, game_text, category_id

    ok_count = 0
    errors: list[str] = []
    prefer_recurring = False
    used_recurring_fallback = False
    for del_id in deletes:
        try:
            await asyncio.to_thread(
                twitch.delete_schedule_segment,
                access,
                twitch_user_id,
                str(del_id),
            )
            ok_count += 1
        except Exception as exc:
            errors.append(_schedule_publish_error_text(exc, "", lang))
    for upd in updates:
        start_iso, game_text, category_id = _start_and_category(upd)
        try:
            _, recurring = twitch.update_schedule_segment_with_overlap_replace(
                access,
                twitch_user_id,
                str(upd["id"]),
                start_time=start_iso,
                timezone=tz_name,
                duration=duration_min,
                title=game_text or "",
                category_id=category_id,
            )
            if recurring:
                used_recurring_fallback = True
            ok_count += 1
        except Exception as exc:
            errors.append(_schedule_publish_error_text(exc, str(upd.get("date") or ""), lang))
    for entry in entries:
        start_iso, game_text, category_id = _start_and_category(entry)
        try:
            # Partner/Affiliate → one-off; else Twitch 403 → weekly recurring fallback.
            _, recurring = twitch.create_schedule_segment_with_fallback(
                access,
                twitch_user_id,
                start_time=start_iso,
                timezone=tz_name,
                duration=duration_min,
                title=game_text or "",
                category_id=category_id,
                prefer_recurring=prefer_recurring,
            )
            if recurring:
                prefer_recurring = True
                used_recurring_fallback = True
            ok_count += 1
        except Exception as exc:
            errors.append(_schedule_publish_error_text(exc, str(entry.get("date") or ""), lang))

    total = len(entries) + len(updates) + len(deletes)
    if ok_count == total:
        key = (
            "stream_schedule_publish_ok_recurring"
            if used_recurring_fallback
            else "stream_schedule_publish_ok"
        )
        text = t(key, lang)
    elif ok_count > 0:
        text = t(
            "stream_schedule_publish_partial",
            lang,
            ok=ok_count,
            total=total,
            errors="\n".join(errors),
        )
    else:
        text = t("stream_schedule_publish_fail", lang, error="\n".join(errors))

    buttons = []
    if refresh:
        buttons.append([InlineKeyboardButton(
            t("stream_schedule_save_token", lang),
            callback_data=f"sched_save_token:{owner_id}",
        )])
        application.bot_data.setdefault("pending_schedule_tokens", {})[owner_id] = {
            "refresh_token": refresh,
            "twitch_user_id": twitch_user_id,
        }
    markup = InlineKeyboardMarkup(buttons) if buttons else _menu(lang, owner_id)
    await application.bot.send_message(owner_id, text, reply_markup=markup)


async def schedule_save_token_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    pending = context.application.bot_data.get("pending_schedule_tokens", {}).pop(user_id, None)
    if not pending:
        await query.edit_message_reply_markup(None)
        return
    db: Database = context.application.bot_data["db"]
    existing = db.get_twitch_sync(user_id)
    if existing and existing.period_days > 0:
        db.upsert_twitch_sync(
            owner_id=user_id,
            twitch_user_id=pending["twitch_user_id"],
            refresh_token=pending["refresh_token"],
            period_days=existing.period_days,
            next_sync_at=existing.next_sync_at,
            last_sync_at=existing.last_sync_at,
        )
    else:
        db.upsert_twitch_sync(
            owner_id=user_id,
            twitch_user_id=pending["twitch_user_id"],
            refresh_token=pending["refresh_token"],
            period_days=0,
            next_sync_at="9999-12-31T00:00:00+00:00",
        )
    await query.edit_message_text(
        query.message.text + "\n\n" + t("stream_schedule_token_saved", lang)
    )
    await context.bot.send_message(
        reply_chat_id(update), t("menu_main", lang), reply_markup=_menu(lang, user_id)
    )
