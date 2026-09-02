from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta, timezone

from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import Application, ContextTypes, ConversationHandler

import analytics
from bot_helpers import (
    _BROADCAST_SEND_PAUSE,
    _can_use_admin_tools,
    _is_admin,
    _menu,
    _pulse_reply_keyboard,
    _send_dm_html,
    _user_lang,
)
from db import Database
from db.models import BROADCAST_RETENTION_DAYS, ScheduledBroadcast
from i18n import (
    DEFAULT_LOCALE,
    SCHEDULE_TZ,
    admin_other_audience_keyboard,
    admin_type_keyboard,
    admin_wizard_menu,
    all_wizard_nav_buttons,
    broadcast_feedback_keyboard,
    broadcast_menu,
    format_schedule_month_label,
    is_menu_button,
    schedule_calendar_days_keyboard,
    schedule_keyboard,
    schedule_month_keyboard,
    scheduled_edit_keyboard,
    scheduled_list_keyboard,
    t,
)
from translate import build_translations

logger = logging.getLogger(__name__)


def _conv_states():
    """Lazy-load conversation state ints from bot (avoids circular import at load)."""
    from bot import (
        ADMIN_MSG_AUDIENCE,
        ADMIN_MSG_IDS,
        ADMIN_MSG_SCHEDULE,
        ADMIN_MSG_TEXT,
        ADMIN_MSG_TYPE,
        ADMIN_SB_EDIT_SCHEDULE,
        ADMIN_SB_EDIT_TEXT,
        _set_wizard_back,
    )

    return {
        "ADMIN_MSG_TYPE": ADMIN_MSG_TYPE,
        "ADMIN_MSG_TEXT": ADMIN_MSG_TEXT,
        "ADMIN_MSG_SCHEDULE": ADMIN_MSG_SCHEDULE,
        "ADMIN_MSG_AUDIENCE": ADMIN_MSG_AUDIENCE,
        "ADMIN_MSG_IDS": ADMIN_MSG_IDS,
        "ADMIN_SB_EDIT_TEXT": ADMIN_SB_EDIT_TEXT,
        "ADMIN_SB_EDIT_SCHEDULE": ADMIN_SB_EDIT_SCHEDULE,
        "_set_wizard_back": _set_wizard_back,
    }


def _broadcast_type_label(msg_type: str, lang: str) -> str:
    if msg_type == "bot_update":
        return t("broadcast_type_bot_update", lang)
    if msg_type == "availability":
        return t("broadcast_type_availability", lang)
    if msg_type == "other":
        return t("broadcast_type_other", lang)
    return msg_type


def _format_scheduled_at_label(scheduled_at: str) -> str:
    dt = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(SCHEDULE_TZ).strftime("%d.%m.%Y %H:%M MSK")


def _format_sent_at_label(sent_at: str) -> str:
    return _format_scheduled_at_label(sent_at)


def _utc_iso_to_schedule(scheduled_at: str) -> dict:
    dt = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(SCHEDULE_TZ)
    now = datetime.now(SCHEDULE_TZ)
    date_offset = max(0, (local.date() - now.date()).days)
    return {
        "date_offset": date_offset,
        "date_page": date_offset // 3,
        "hour": local.hour,
        "minute": local.minute,
        "show_minutes": True,
    }


def _scheduled_text_preview(text: str, limit: int = 120) -> str:
    plain = re.sub(r"<[^>]+>", "", text).strip()
    if len(plain) <= limit:
        return plain
    return plain[: limit - 1] + "…"


def _admin_sent_broadcast_message(
    db: Database, item: ScheduledBroadcast, lang: str
) -> tuple[str, InlineKeyboardMarkup]:
    when = _format_sent_at_label(item.sent_at or item.scheduled_at)
    type_label = _broadcast_type_label(item.msg_type, lang)
    header = t("sent_line", lang, id=item.id, when=when, type=type_label)
    footer = t("broadcast_footer", lang, type=type_label)
    message = f"{header}\n\n{item.text}\n\n{footer}"
    up_count, down_count = db.get_broadcast_feedback_counts(item.id)
    markup = broadcast_feedback_keyboard(item.id, up_count, down_count)
    return message, markup


async def _send_admin_preview_message(
    bot, chat_id: int, message: str, *, reply_markup
) -> None:
    try:
        await bot.send_message(
            chat_id,
            message,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
    except BadRequest:
        await bot.send_message(chat_id, message, reply_markup=reply_markup)


async def open_broadcast_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not _can_use_admin_tools(user_id):
        return
    context.user_data.clear()
    lang = _user_lang(context, user_id)
    await update.effective_message.reply_text(
        t("menu_broadcast", lang),
        reply_markup=broadcast_menu(lang),
    )


async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _st = _conv_states()
    ADMIN_MSG_TYPE = _st["ADMIN_MSG_TYPE"]
    ADMIN_MSG_TEXT = _st["ADMIN_MSG_TEXT"]
    ADMIN_MSG_SCHEDULE = _st["ADMIN_MSG_SCHEDULE"]
    ADMIN_MSG_AUDIENCE = _st["ADMIN_MSG_AUDIENCE"]
    ADMIN_MSG_IDS = _st["ADMIN_MSG_IDS"]
    ADMIN_SB_EDIT_SCHEDULE = _st["ADMIN_SB_EDIT_SCHEDULE"]
    ADMIN_SB_EDIT_TEXT = _st["ADMIN_SB_EDIT_TEXT"]
    _set_wizard_back = _st["_set_wizard_back"]

    user_id = update.effective_user.id
    if not _can_use_admin_tools(user_id):
        return ConversationHandler.END
    context.user_data.clear()
    lang = _user_lang(context, user_id)
    await update.effective_message.reply_text(
        t("broadcast_prompt", lang),
        reply_markup=admin_type_keyboard(lang),
    )
    _set_wizard_back(context, ADMIN_MSG_TYPE)
    return ADMIN_MSG_TYPE


async def admin_select_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _st = _conv_states()
    ADMIN_MSG_TYPE = _st["ADMIN_MSG_TYPE"]
    ADMIN_MSG_TEXT = _st["ADMIN_MSG_TEXT"]
    ADMIN_MSG_SCHEDULE = _st["ADMIN_MSG_SCHEDULE"]
    ADMIN_MSG_AUDIENCE = _st["ADMIN_MSG_AUDIENCE"]
    ADMIN_MSG_IDS = _st["ADMIN_MSG_IDS"]
    ADMIN_SB_EDIT_SCHEDULE = _st["ADMIN_SB_EDIT_SCHEDULE"]
    ADMIN_SB_EDIT_TEXT = _st["ADMIN_SB_EDIT_TEXT"]
    _set_wizard_back = _st["_set_wizard_back"]

    query = update.callback_query
    await query.answer()
    if not _is_admin(query.from_user.id):
        return ConversationHandler.END
    lang = _user_lang(context, query.from_user.id)
    msg_type = query.data.split(":", 1)[1]
    context.user_data["admin_msg_type"] = msg_type
    context.user_data.pop("admin_recipient_ids", None)
    await query.edit_message_text("✓")
    if msg_type == "other":
        await context.bot.send_message(
            query.from_user.id,
            t("broadcast_audience_prompt", lang),
            reply_markup=admin_other_audience_keyboard(lang),
        )
        _set_wizard_back(context, ADMIN_MSG_AUDIENCE)
        return ADMIN_MSG_AUDIENCE
    await context.bot.send_message(
        query.from_user.id,
        t("broadcast_text_prompt", lang),
        reply_markup=admin_wizard_menu(lang),
    )
    _set_wizard_back(context, ADMIN_MSG_TEXT)
    return ADMIN_MSG_TEXT


async def admin_audience_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _st = _conv_states()
    ADMIN_MSG_TYPE = _st["ADMIN_MSG_TYPE"]
    ADMIN_MSG_TEXT = _st["ADMIN_MSG_TEXT"]
    ADMIN_MSG_SCHEDULE = _st["ADMIN_MSG_SCHEDULE"]
    ADMIN_MSG_AUDIENCE = _st["ADMIN_MSG_AUDIENCE"]
    ADMIN_MSG_IDS = _st["ADMIN_MSG_IDS"]
    ADMIN_SB_EDIT_SCHEDULE = _st["ADMIN_SB_EDIT_SCHEDULE"]
    ADMIN_SB_EDIT_TEXT = _st["ADMIN_SB_EDIT_TEXT"]
    _set_wizard_back = _st["_set_wizard_back"]

    query = update.callback_query
    await query.answer()
    if not _is_admin(query.from_user.id):
        return ConversationHandler.END
    lang = _user_lang(context, query.from_user.id)
    action = query.data.split(":", 1)[1]
    await query.edit_message_text("✓")
    if action == "ids":
        context.user_data.pop("admin_recipient_ids", None)
        await context.bot.send_message(
            query.from_user.id,
            t("broadcast_ids_prompt", lang),
            reply_markup=admin_wizard_menu(lang),
        )
        _set_wizard_back(context, ADMIN_MSG_IDS)
        return ADMIN_MSG_IDS
    context.user_data.pop("admin_recipient_ids", None)
    await context.bot.send_message(
        query.from_user.id,
        t("broadcast_text_prompt", lang),
        reply_markup=admin_wizard_menu(lang),
    )
    _set_wizard_back(context, ADMIN_MSG_TEXT)
    return ADMIN_MSG_TEXT


def _parse_broadcast_recipient_ids(raw: str) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()
    for part in (raw or "").replace(";", ",").split(","):
        token = part.strip()
        if not token or not token.isdigit():
            continue
        uid = int(token)
        if uid <= 0 or uid in seen:
            continue
        seen.add(uid)
        ids.append(uid)
    return ids


def _dump_broadcast_recipient_ids(ids: object) -> str:
    if not isinstance(ids, (list, tuple)):
        return ""
    out: list[str] = []
    seen: set[int] = set()
    for item in ids:
        try:
            uid = int(item)
        except (TypeError, ValueError):
            continue
        if uid <= 0 or uid in seen:
            continue
        seen.add(uid)
        out.append(str(uid))
    return ",".join(out)


async def admin_receive_ids(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _st = _conv_states()
    ADMIN_MSG_TYPE = _st["ADMIN_MSG_TYPE"]
    ADMIN_MSG_TEXT = _st["ADMIN_MSG_TEXT"]
    ADMIN_MSG_SCHEDULE = _st["ADMIN_MSG_SCHEDULE"]
    ADMIN_MSG_AUDIENCE = _st["ADMIN_MSG_AUDIENCE"]
    ADMIN_MSG_IDS = _st["ADMIN_MSG_IDS"]
    ADMIN_SB_EDIT_SCHEDULE = _st["ADMIN_SB_EDIT_SCHEDULE"]
    ADMIN_SB_EDIT_TEXT = _st["ADMIN_SB_EDIT_TEXT"]
    _set_wizard_back = _st["_set_wizard_back"]

    user_id = update.effective_user.id
    if not _is_admin(user_id):
        return ConversationHandler.END
    lang = _user_lang(context, user_id)
    plain = (update.effective_message.text or "").strip()
    if is_menu_button(plain):
        await update.effective_message.reply_text(t("finish_setup_first", lang))
        return ADMIN_MSG_IDS
    ids = _parse_broadcast_recipient_ids(plain)
    if not ids:
        await update.effective_message.reply_text(t("broadcast_ids_invalid", lang))
        return ADMIN_MSG_IDS
    context.user_data["admin_recipient_ids"] = ids
    await update.effective_message.reply_text(
        t("broadcast_text_prompt", lang),
        reply_markup=admin_wizard_menu(lang),
    )
    _set_wizard_back(context, ADMIN_MSG_TEXT)
    return ADMIN_MSG_TEXT


async def admin_receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _st = _conv_states()
    ADMIN_MSG_TYPE = _st["ADMIN_MSG_TYPE"]
    ADMIN_MSG_TEXT = _st["ADMIN_MSG_TEXT"]
    ADMIN_MSG_SCHEDULE = _st["ADMIN_MSG_SCHEDULE"]
    ADMIN_MSG_AUDIENCE = _st["ADMIN_MSG_AUDIENCE"]
    ADMIN_MSG_IDS = _st["ADMIN_MSG_IDS"]
    ADMIN_SB_EDIT_SCHEDULE = _st["ADMIN_SB_EDIT_SCHEDULE"]
    ADMIN_SB_EDIT_TEXT = _st["ADMIN_SB_EDIT_TEXT"]
    _set_wizard_back = _st["_set_wizard_back"]

    user_id = update.effective_user.id
    if not _is_admin(user_id):
        return ConversationHandler.END
    lang = _user_lang(context, user_id)
    msg = update.effective_message
    plain = (msg.text or "").strip()
    if is_menu_button(plain):
        await update.effective_message.reply_text(t("finish_setup_first", lang))
        return ADMIN_MSG_TEXT
    if not plain:
        await update.effective_message.reply_text(t("broadcast_empty", lang))
        return ADMIN_MSG_TEXT

    # Keep Telegram client formatting (bold/italic/links/…) as HTML for send.
    text = (msg.text_html or plain).strip()
    context.user_data["admin_msg_text"] = text
    db: Database = context.application.bot_data["db"]
    hour, minute = db.get_saved_schedule(user_id)
    schedule = {
        "date_offset": 0,
        "date_page": 0,
        "hour": hour,
        "minute": minute,
        "show_minutes": minute is not None,
    }
    context.user_data["schedule"] = schedule
    await update.effective_message.reply_text(
        t("schedule_title", lang),
        reply_markup=schedule_keyboard(lang, schedule),
    )
    _set_wizard_back(context, ADMIN_MSG_SCHEDULE)
    return ADMIN_MSG_SCHEDULE


def _schedule_to_utc_iso(schedule: dict) -> str:
    now = datetime.now(SCHEDULE_TZ)
    d = now.date() + timedelta(days=int(schedule.get("date_offset", 0)))
    hour = int(schedule.get("hour", 0))
    minute = int(schedule.get("minute", 0))
    local_dt = datetime(d.year, d.month, d.day, hour, minute, tzinfo=SCHEDULE_TZ)
    return local_dt.astimezone(timezone.utc).isoformat()


def _calendar_message(
    data: str,
    *,
    prefix: str,
    lang: str,
    schedule: dict,
    time_title: str,
    show_send_now: bool,
) -> tuple[str, object] | None:
    """Return (text, markup) for calendar navigation callbacks, else None."""
    if data == f"{prefix}:calendar":
        return t("schedule_pick_month", lang), schedule_month_keyboard(
            lang, prefix=prefix
        )
    if data == f"{prefix}:time":
        return time_title, schedule_keyboard(
            lang, schedule, prefix=prefix, show_send_now=show_send_now
        )
    if data.startswith(f"{prefix}:month:"):
        raw = data.split(":", 2)[2]
        try:
            year_s, month_s = raw.split("-", 1)
            year, month = int(year_s), int(month_s)
        except ValueError:
            return None
        if not (1 <= month <= 12):
            return None
        month_label = format_schedule_month_label(year, month, lang)
        return (
            t("schedule_pick_day", lang, month=month_label),
            schedule_calendar_days_keyboard(
                lang, year, month, schedule, prefix=prefix
            ),
        )
    return None


def _default_broadcast_utc_offset() -> int:
    return int(SCHEDULE_TZ.utcoffset(None).total_seconds() // 60)


def _schedule_wall_clock(scheduled_at: str) -> tuple[date, int, int]:
    dt = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(SCHEDULE_TZ)
    return local.date(), local.hour, local.minute


def _utc_due_for_offset(
    day: date, hour: int, minute: int, utc_offset_minutes: int
) -> datetime:
    local_tz = timezone(timedelta(minutes=int(utc_offset_minutes)))
    local_dt = datetime(day.year, day.month, day.day, hour, minute, tzinfo=local_tz)
    return local_dt.astimezone(timezone.utc)


def _broadcast_recipient_ids(
    db: Database,
    msg_type: str,
    recipient_ids: list[int] | None,
) -> list[int]:
    if recipient_ids is not None:
        return list(recipient_ids)
    if msg_type == "bot_update":
        return db.get_bot_update_recipients()
    if msg_type == "availability":
        return db.get_availability_recipients()
    if msg_type == "other":
        return db.get_other_recipients()
    return db.get_notify_user_ids()


def _broadcast_offset_groups(
    db: Database, user_ids: list[int]
) -> dict[int, list[int]]:
    default_off = _default_broadcast_utc_offset()
    offsets = db.get_schedule_utc_offsets_for_users(user_ids)
    groups: dict[int, list[int]] = {}
    for uid in user_ids:
        off = offsets.get(uid)
        key = int(off) if off is not None else default_off
        groups.setdefault(key, []).append(uid)
    return groups


def _broadcast_waves(db: Database, item) -> list[tuple[int, datetime]]:
    user_ids = _broadcast_recipient_ids(
        db, item.msg_type, _parse_broadcast_recipient_ids(item.recipient_ids or "") or None
    )
    user_ids = [uid for uid in user_ids if not db.is_bot_blocked(uid)]
    day, hour, minute = _schedule_wall_clock(item.scheduled_at)
    waves: dict[int, datetime] = {}
    for offset in _broadcast_offset_groups(db, user_ids):
        waves[offset] = _utc_due_for_offset(day, hour, minute, offset)
    return sorted(waves.items(), key=lambda pair: pair[1])


def _broadcast_job_name(broadcast_id: int) -> str:
    return f"broadcast_{broadcast_id}"


def _broadcast_offset_job_name(broadcast_id: int, utc_offset_minutes: int) -> str:
    return f"broadcast_{broadcast_id}_tz_{int(utc_offset_minutes)}"


def _cancel_broadcast_job(job_queue, broadcast_id: int) -> None:
    for job in job_queue.get_jobs_by_name(_broadcast_job_name(broadcast_id)):
        job.schedule_removal()


def _cancel_broadcast_jobs(job_queue, db: Database, broadcast_id: int) -> None:
    _cancel_broadcast_job(job_queue, broadcast_id)
    item = db.get_scheduled_broadcast(broadcast_id)
    if not item:
        return
    for offset, _ in _broadcast_waves(db, item):
        name = _broadcast_offset_job_name(broadcast_id, offset)
        for job in job_queue.get_jobs_by_name(name):
            job.schedule_removal()


def _schedule_broadcast_job(job_queue, db: Database, broadcast_id: int) -> None:
    _schedule_broadcast_waves(job_queue, db, broadcast_id)


def _schedule_broadcast_waves(job_queue, db: Database, broadcast_id: int) -> None:
    _cancel_broadcast_jobs(job_queue, db, broadcast_id)
    item = db.get_scheduled_broadcast(broadcast_id)
    if not item:
        return
    now = datetime.now(timezone.utc)
    sent = db.get_broadcast_sent_offsets(broadcast_id)
    for offset, due_utc in _broadcast_waves(db, item):
        if offset in sent:
            continue
        when = max(0.0, (due_utc - now).total_seconds())
        job_queue.run_once(
            _run_scheduled_broadcast,
            when=when,
            data={"broadcast_id": broadcast_id, "utc_offset": offset},
            name=_broadcast_offset_job_name(broadcast_id, offset),
        )


async def _run_broadcast_offset_wave(
    context: ContextTypes.DEFAULT_TYPE, item, utc_offset: int
) -> None:
    db: Database = context.application.bot_data["db"]
    if utc_offset in db.get_broadcast_sent_offsets(item.id):
        return
    source_lang = db.get_user_locale(item.created_by) or DEFAULT_LOCALE
    recipients = _parse_broadcast_recipient_ids(item.recipient_ids or "")
    sent, _failed, _total = await _send_admin_broadcast(
        context,
        item.msg_type,
        item.text,
        broadcast_id=item.id,
        source_lang=source_lang,
        recipient_ids=recipients or None,
        utc_offset_filter=utc_offset,
    )
    db.record_broadcast_offset_sent(item.id, utc_offset, sent)
    await _maybe_finish_broadcast(context, item)


async def _maybe_finish_broadcast(
    context: ContextTypes.DEFAULT_TYPE, item
) -> None:
    db: Database = context.application.bot_data["db"]
    waves = _broadcast_waves(db, item)
    target_offsets = {offset for offset, _ in waves}
    if not target_offsets.issubset(db.get_broadcast_sent_offsets(item.id)):
        return
    recipients = _broadcast_recipient_ids(
        db,
        item.msg_type,
        _parse_broadcast_recipient_ids(item.recipient_ids or "") or None,
    )
    total = len([uid for uid in recipients if not db.is_bot_blocked(uid)])
    sent = db.get_broadcast_sent_count(item.id)
    await _report_broadcast_done(
        context,
        item.created_by,
        sent=sent,
        total=total,
    )
    db.mark_scheduled_broadcast_sent(item.id)


def _claim_broadcast_send(bot_data: dict, broadcast_id: int) -> bool:
    """Prevent double-send if a job and the pending poll race."""
    sending: set[int] = bot_data.setdefault("sending_broadcasts", set())
    if broadcast_id in sending:
        return False
    sending.add(broadcast_id)
    return True


def _release_broadcast_send(bot_data: dict, broadcast_id: int) -> None:
    sending = bot_data.get("sending_broadcasts")
    if isinstance(sending, set):
        sending.discard(broadcast_id)


def _claim_broadcast_wave_send(
    bot_data: dict, broadcast_id: int, utc_offset: int
) -> bool:
    sending: set[tuple[int, int]] = bot_data.setdefault("sending_broadcast_waves", set())
    key = (broadcast_id, int(utc_offset))
    if key in sending:
        return False
    sending.add(key)
    return True


def _release_broadcast_wave_send(
    bot_data: dict, broadcast_id: int, utc_offset: int
) -> None:
    sending = bot_data.get("sending_broadcast_waves")
    if isinstance(sending, set):
        sending.discard((broadcast_id, int(utc_offset)))


async def _send_admin_broadcast(
    context: ContextTypes.DEFAULT_TYPE,
    msg_type: str,
    text: str,
    *,
    broadcast_id: int,
    source_lang: str | None = None,
    recipient_ids: list[int] | None = None,
    utc_offset_filter: int | None = None,
) -> tuple[int, int, int]:
    db: Database = context.application.bot_data["db"]
    user_ids = _broadcast_recipient_ids(db, msg_type, recipient_ids)
    user_ids = [uid for uid in user_ids if not db.is_bot_blocked(uid)]
    if utc_offset_filter is not None:
        default_off = _default_broadcast_utc_offset()
        offsets = db.get_schedule_utc_offsets_for_users(user_ids)
        user_ids = [
            uid
            for uid in user_ids
            if (offsets.get(uid) if offsets.get(uid) is not None else default_off)
            == int(utc_offset_filter)
        ]
    source = source_lang or DEFAULT_LOCALE
    locale_rows = db.get_user_locales(user_ids)
    user_locales = {
        uid: locale_rows.get(uid) or DEFAULT_LOCALE for uid in user_ids
    }
    translations = await asyncio.to_thread(
        build_translations,
        text,
        source,
        set(user_locales.values()),
    )
    attach_menu = msg_type == "bot_update"
    up_count, down_count = db.get_broadcast_feedback_counts(broadcast_id)
    feedback_kb = broadcast_feedback_keyboard(broadcast_id, up_count, down_count)
    sent = failed = 0
    for uid in user_ids:
        locale = user_locales[uid]
        body = translations.get(locale, text)
        footer = t(
            "broadcast_footer",
            locale,
            type=_broadcast_type_label(msg_type, locale),
        )
        message = f"{body}\n\n{footer}"
        result = await _send_dm_html(
            context.bot,
            db,
            uid,
            message,
            reply_markup=feedback_kb,
            return_message_id=True,
        )
        status, message_id = result if isinstance(result, tuple) else (result, None)
        if status == "sent":
            sent += 1
            if message_id is not None:
                db.add_broadcast_delivery(broadcast_id, uid, message_id)
            if attach_menu:
                await _pulse_reply_keyboard(
                    context.bot, uid, _menu(locale, uid)
                )
        else:
            failed += 1
        await asyncio.sleep(_BROADCAST_SEND_PAUSE)
    return sent, failed, len(user_ids)


async def _refresh_broadcast_feedback_keyboards(
    context: ContextTypes.DEFAULT_TYPE,
    broadcast_id: int,
) -> None:
    db: Database = context.application.bot_data["db"]
    up_count, down_count = db.get_broadcast_feedback_counts(broadcast_id)
    markup = broadcast_feedback_keyboard(broadcast_id, up_count, down_count)
    for uid, message_id in db.get_broadcast_deliveries(broadcast_id):
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=uid,
                message_id=message_id,
                reply_markup=markup,
            )
        except (BadRequest, Forbidden):
            pass
        await asyncio.sleep(0.05)


async def refresh_broadcast_feedback_keyboards(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    db: Database = context.application.bot_data["db"]
    for item in db.get_sent_broadcasts(retention_days=BROADCAST_RETENTION_DAYS):
        if not db.get_broadcast_deliveries(item.id):
            continue
        await _refresh_broadcast_feedback_keyboards(context, item.id)


async def on_broadcast_feedback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not query:
        return
    try:
        await query.answer()
    except BadRequest:
        pass
    if not query.data:
        return
    parts = query.data.split(":")
    if len(parts) != 3 or parts[0] != "bcf":
        return
    vote_key, raw_id = parts[1], parts[2]
    if vote_key not in ("up", "down") or not raw_id.isdigit():
        return
    vote = 1 if vote_key == "up" else -1
    broadcast_id = int(raw_id)
    user_id = query.from_user.id
    db: Database = context.application.bot_data["db"]
    current = db.get_broadcast_feedback_vote(broadcast_id, user_id)
    if current == vote:
        db.clear_broadcast_feedback(broadcast_id, user_id)
    else:
        db.set_broadcast_feedback(broadcast_id, user_id, vote)
    up_count, down_count = db.get_broadcast_feedback_counts(broadcast_id)
    markup = broadcast_feedback_keyboard(broadcast_id, up_count, down_count)
    if query.message:
        try:
            await query.edit_message_reply_markup(reply_markup=markup)
        except (BadRequest, Forbidden):
            pass


async def _report_broadcast_done(
    context: ContextTypes.DEFAULT_TYPE,
    admin_id: int,
    *,
    sent: int,
    total: int,
) -> None:
    db: Database = context.application.bot_data["db"]
    lang = db.get_user_locale(admin_id) or DEFAULT_LOCALE
    blocked_users = db.get_bot_stats().blocked_users
    text = t(
        "broadcast_done",
        lang,
        sent=sent,
        blocked_users=blocked_users,
        total=total,
    )
    analytics.capture(
        admin_id,
        "broadcast_completed",
        {"sent": sent, "total": total, "blocked_users": blocked_users},
    )
    try:
        await context.bot.send_message(admin_id, text)
    except (BadRequest, Forbidden) as exc:
        logger.warning("Cannot send broadcast stats to admin %s: %s", admin_id, exc)


async def admin_schedule_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _st = _conv_states()
    ADMIN_MSG_TYPE = _st["ADMIN_MSG_TYPE"]
    ADMIN_MSG_TEXT = _st["ADMIN_MSG_TEXT"]
    ADMIN_MSG_SCHEDULE = _st["ADMIN_MSG_SCHEDULE"]
    ADMIN_MSG_AUDIENCE = _st["ADMIN_MSG_AUDIENCE"]
    ADMIN_MSG_IDS = _st["ADMIN_MSG_IDS"]
    ADMIN_SB_EDIT_SCHEDULE = _st["ADMIN_SB_EDIT_SCHEDULE"]
    ADMIN_SB_EDIT_TEXT = _st["ADMIN_SB_EDIT_TEXT"]
    _set_wizard_back = _st["_set_wizard_back"]

    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not _is_admin(user_id):
        return ConversationHandler.END
    lang = _user_lang(context, user_id)
    data = query.data
    schedule = dict(context.user_data.get("schedule") or {})
    db: Database = context.application.bot_data["db"]

    if data == "sched:noop":
        return ADMIN_MSG_SCHEDULE
    calendar_edit = _calendar_message(
        data,
        prefix="sched",
        lang=lang,
        schedule=schedule,
        time_title=t("schedule_title", lang),
        show_send_now=True,
    )
    if calendar_edit is not None:
        text, markup = calendar_edit
        await query.edit_message_text(text, reply_markup=markup)
        return ADMIN_MSG_SCHEDULE
    if data == "sched:toggle_min":
        schedule["show_minutes"] = True
        context.user_data["schedule"] = schedule
        await query.edit_message_text(
            t("schedule_title", lang),
            reply_markup=schedule_keyboard(lang, schedule),
        )
        return ADMIN_MSG_SCHEDULE
    if data == "sched:date_next":
        schedule["date_page"] = int(schedule.get("date_page", 0)) + 1
        context.user_data["schedule"] = schedule
        await query.edit_message_text(
            t("schedule_title", lang),
            reply_markup=schedule_keyboard(lang, schedule),
        )
        return ADMIN_MSG_SCHEDULE
    if data.startswith("sched:date:"):
        schedule["date_offset"] = int(data.split(":")[2])
        schedule["date_page"] = int(schedule["date_offset"]) // 3
        context.user_data["schedule"] = schedule
        await query.edit_message_text(
            t("schedule_title", lang),
            reply_markup=schedule_keyboard(lang, schedule),
        )
        return ADMIN_MSG_SCHEDULE
    if data.startswith("sched:hour:"):
        schedule["hour"] = int(data.split(":")[2])
        context.user_data["schedule"] = schedule
        await query.edit_message_text(
            t("schedule_title", lang),
            reply_markup=schedule_keyboard(lang, schedule),
        )
        return ADMIN_MSG_SCHEDULE
    if data.startswith("sched:min:"):
        schedule["minute"] = int(data.split(":")[2])
        schedule["show_minutes"] = True
        context.user_data["schedule"] = schedule
        await query.edit_message_text(
            t("schedule_title", lang),
            reply_markup=schedule_keyboard(lang, schedule),
        )
        return ADMIN_MSG_SCHEDULE
    if data == "sched:saved":
        hour, minute = db.get_saved_schedule(user_id)
        if hour is not None and minute is not None:
            schedule["hour"] = hour
            schedule["minute"] = minute
            schedule["show_minutes"] = True
            context.user_data["schedule"] = schedule
        await query.edit_message_text(
            t("schedule_title", lang),
            reply_markup=schedule_keyboard(lang, schedule),
        )
        return ADMIN_MSG_SCHEDULE

    msg_type = context.user_data.get("admin_msg_type", "bot_update")
    text = context.user_data.get("admin_msg_text", "")
    recipient_ids_raw = _dump_broadcast_recipient_ids(
        context.user_data.get("admin_recipient_ids")
    )
    if data == "sched:now":
        # Offload to job queue so the conversation handler returns immediately
        # and the bot keeps processing other updates while the mass send runs.
        scheduled_at = datetime.now(timezone.utc).isoformat()
        broadcast_id = db.add_scheduled_broadcast(
            msg_type, text, scheduled_at, user_id, recipient_ids=recipient_ids_raw
        )
        context.user_data.clear()
        try:
            await query.edit_message_text(t("broadcast_started", lang))
        except BadRequest:
            pass
        context.job_queue.run_once(
            _run_scheduled_broadcast,
            when=0,
            data={"broadcast_id": broadcast_id},
            name=_broadcast_job_name(broadcast_id),
        )
        await context.bot.send_message(
            user_id, t("menu_broadcast", lang), reply_markup=broadcast_menu(lang)
        )
        return ConversationHandler.END

    if data == "sched:apply":
        if schedule.get("hour") is None or schedule.get("minute") is None:
            await query.answer(t("schedule_pick_minutes", lang), show_alert=True)
            return ADMIN_MSG_SCHEDULE
        hour = int(schedule["hour"])
        minute = int(schedule["minute"])
        db.set_saved_schedule(user_id, hour, minute)
        scheduled_at = _schedule_to_utc_iso(schedule)
        broadcast_id = db.add_scheduled_broadcast(
            msg_type, text, scheduled_at, user_id, recipient_ids=recipient_ids_raw
        )
        context.user_data.clear()
        _schedule_broadcast_waves(context.job_queue, db, broadcast_id)
        when_local = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00")).astimezone(
            SCHEDULE_TZ
        )
        when_label = when_local.strftime("%d.%m.%Y %H:%M MSK")
        msk_due = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
        if msk_due.tzinfo is None:
            msk_due = msk_due.replace(tzinfo=timezone.utc)
        if msk_due <= datetime.now(timezone.utc):
            try:
                await query.edit_message_text(t("broadcast_started", lang))
            except BadRequest:
                pass
        else:
            await query.edit_message_text(
                t("broadcast_scheduled", lang, when=when_label)
            )
        await context.bot.send_message(user_id, t("menu_broadcast", lang), reply_markup=broadcast_menu(lang))
        return ConversationHandler.END

    return ADMIN_MSG_SCHEDULE


async def _run_scheduled_broadcast(context: ContextTypes.DEFAULT_TYPE) -> None:
    broadcast_id = int(context.job.data["broadcast_id"])
    utc_offset = context.job.data.get("utc_offset")
    bot_data = context.application.bot_data
    db: Database = bot_data["db"]

    if utc_offset is None:
        if not _claim_broadcast_send(bot_data, broadcast_id):
            return
        try:
            item = db.get_scheduled_broadcast(broadcast_id)
            if not item:
                return
            source_lang = db.get_user_locale(item.created_by) or DEFAULT_LOCALE
            recipients = _parse_broadcast_recipient_ids(item.recipient_ids or "")
            sent, _failed, total = await _send_admin_broadcast(
                context,
                item.msg_type,
                item.text,
                broadcast_id=broadcast_id,
                source_lang=source_lang,
                recipient_ids=recipients or None,
            )
            db.mark_scheduled_broadcast_sent(broadcast_id)
            await _report_broadcast_done(
                context,
                item.created_by,
                sent=sent,
                total=total,
            )
        finally:
            _release_broadcast_send(bot_data, broadcast_id)
        return

    offset = int(utc_offset)
    if not _claim_broadcast_wave_send(bot_data, broadcast_id, offset):
        return
    try:
        item = db.get_scheduled_broadcast(broadcast_id)
        if not item:
            return
        await _run_broadcast_offset_wave(context, item, offset)
    finally:
        _release_broadcast_wave_send(bot_data, broadcast_id, offset)


async def admin_sent_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not _can_use_admin_tools(user_id):
        return
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    items = db.get_sent_broadcasts(retention_days=BROADCAST_RETENTION_DAYS)
    if not items:
        await update.effective_message.reply_text(
            t("sent_empty", lang),
            reply_markup=broadcast_menu(lang),
        )
        return

    await update.effective_message.reply_text(
        t("sent_list_title", lang),
        reply_markup=broadcast_menu(lang),
    )
    for item in items:
        message, markup = _admin_sent_broadcast_message(db, item, lang)
        await _send_admin_preview_message(
            context.bot,
            user_id,
            message,
            reply_markup=markup,
        )


async def purge_old_broadcasts(context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    removed = db.purge_old_sent_broadcasts(retention_days=BROADCAST_RETENTION_DAYS)
    if removed:
        logger.info("Purged %s sent broadcast(s) older than %s days", removed, BROADCAST_RETENTION_DAYS)


async def admin_scheduled_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not _can_use_admin_tools(user_id):
        return
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    items = db.get_unsent_scheduled_broadcasts()
    if not items:
        await update.effective_message.reply_text(
            t("scheduled_empty", lang),
            reply_markup=broadcast_menu(lang),
        )
        return

    lines = [t("scheduled_list_title", lang)]
    ids: list[int] = []
    for item in items:
        ids.append(item.id)
        lines.append(
            t(
                "scheduled_line",
                lang,
                id=item.id,
                when=_format_scheduled_at_label(item.scheduled_at),
                type=_broadcast_type_label(item.msg_type, lang),
                preview=_scheduled_text_preview(item.text),
            )
        )
    await update.effective_message.reply_text(
        "\n\n".join(lines),
        reply_markup=scheduled_list_keyboard(ids, lang),
    )


async def on_sb_edit_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _is_admin(query.from_user.id):
        return
    lang = _user_lang(context, query.from_user.id)
    broadcast_id = int(query.data.split(":", 1)[1])
    db: Database = context.application.bot_data["db"]
    item = db.get_scheduled_broadcast(broadcast_id)
    if not item:
        await query.edit_message_text(t("scheduled_not_found", lang))
        return
    await query.edit_message_text(
        t("scheduled_edit_menu", lang, id=broadcast_id),
        reply_markup=scheduled_edit_keyboard(broadcast_id, lang),
    )


async def on_sb_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not _is_admin(user_id):
        return
    lang = _user_lang(context, user_id)
    broadcast_id = int(query.data.split(":", 1)[1])
    db: Database = context.application.bot_data["db"]
    if db.delete_scheduled_broadcast(broadcast_id):
        _cancel_broadcast_jobs(context.application.job_queue, db, broadcast_id)
        await query.edit_message_text(t("scheduled_deleted", lang, id=broadcast_id))
    else:
        await query.edit_message_text(t("scheduled_not_found", lang))
    await context.bot.send_message(
        user_id,
        t("menu_broadcast", lang),
        reply_markup=broadcast_menu(lang),
    )


async def _go_sb_edit_text_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str, broadcast_id: int
) -> None:
    user_id = update.effective_user.id
    db: Database = context.application.bot_data["db"]
    item = db.get_scheduled_broadcast(broadcast_id)
    if not item:
        await context.bot.send_message(user_id, t("scheduled_not_found", lang))
        context.user_data.pop("sb_edit_mode", None)
        context.user_data.pop("sb_edit_id", None)
        return
    current = item.text or "—"
    markup = admin_wizard_menu(lang, back=False)
    body = t("scheduled_edit_text_prompt", lang, id=broadcast_id, text=current)
    if len(body) <= 4096:
        try:
            await context.bot.send_message(
                user_id, body, parse_mode=ParseMode.HTML, reply_markup=markup
            )
            return
        except BadRequest:
            pass
    # Full text may exceed one Telegram message or break HTML — send separately.
    for i in range(0, max(len(current), 1), 4096):
        chunk = current[i : i + 4096]
        try:
            await context.bot.send_message(user_id, chunk, parse_mode=ParseMode.HTML)
        except BadRequest:
            await context.bot.send_message(user_id, chunk)
    await context.bot.send_message(
        user_id,
        t("scheduled_edit_text_ask", lang, id=broadcast_id),
        reply_markup=markup,
    )


async def _go_sb_edit_time_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str, broadcast_id: int
) -> None:
    user_id = update.effective_user.id
    db: Database = context.application.bot_data["db"]
    item = db.get_scheduled_broadcast(broadcast_id)
    if not item:
        await context.bot.send_message(user_id, t("scheduled_not_found", lang))
        context.user_data.pop("sb_edit_mode", None)
        return
    context.user_data["schedule"] = _utc_iso_to_schedule(item.scheduled_at)
    await context.bot.send_message(
        user_id,
        t("scheduled_edit_time_title", lang, id=broadcast_id),
        reply_markup=schedule_keyboard(
            lang,
            context.user_data["schedule"],
            prefix="sb_sched",
            show_send_now=False,
        ),
    )


def _clear_main_conversation(
    context: ContextTypes.DEFAULT_TYPE, update: Update
) -> None:
    conv = context.application.bot_data.get("main_conv")
    if conv is None:
        return
    try:
        key = conv._get_key(update)
    except Exception:
        return
    if key in conv._conversations:
        del conv._conversations[key]


def _parse_sb_edit_f_id(callback_data: str) -> int:
    # sb_edit_f:{id}:text|time — id is parts[1], not parts[2]
    return int(callback_data.split(":")[1])


async def on_sb_edit_text_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _is_admin(query.from_user.id):
        return
    lang = _user_lang(context, query.from_user.id)
    broadcast_id = _parse_sb_edit_f_id(query.data)
    db: Database = context.application.bot_data["db"]
    if not db.get_scheduled_broadcast(broadcast_id):
        await query.edit_message_text(t("scheduled_not_found", lang))
        return
    _clear_main_conversation(context, update)
    context.user_data.clear()
    context.user_data["sb_edit_id"] = broadcast_id
    context.user_data["sb_edit_mode"] = "text"
    try:
        await query.edit_message_text(t("scheduled_edit_text", lang))
    except BadRequest:
        pass
    await _go_sb_edit_text_prompt(update, context, lang, broadcast_id)


async def on_sb_edit_time_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _is_admin(query.from_user.id):
        return
    lang = _user_lang(context, query.from_user.id)
    broadcast_id = _parse_sb_edit_f_id(query.data)
    db: Database = context.application.bot_data["db"]
    if not db.get_scheduled_broadcast(broadcast_id):
        await query.edit_message_text(t("scheduled_not_found", lang))
        return
    _clear_main_conversation(context, update)
    context.user_data.clear()
    context.user_data["sb_edit_id"] = broadcast_id
    context.user_data["sb_edit_mode"] = "schedule"
    try:
        await query.edit_message_text(t("scheduled_edit_time", lang))
    except BadRequest:
        pass
    await _go_sb_edit_time_prompt(update, context, lang, broadcast_id)


async def receive_sb_edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("sb_edit_mode") != "text":
        return
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        return
    lang = _user_lang(context, user_id)
    broadcast_id = context.user_data.get("sb_edit_id")
    if not broadcast_id:
        context.user_data.pop("sb_edit_mode", None)
        return

    msg = update.effective_message
    plain = (msg.text or "").strip()
    if is_menu_button(plain) or plain in all_wizard_nav_buttons():
        context.user_data.clear()
        await update.effective_message.reply_text(
            t("menu_broadcast", lang),
            reply_markup=broadcast_menu(lang),
        )
        return
    if not plain:
        await update.effective_message.reply_text(t("broadcast_empty", lang))
        return

    text = (msg.text_html or plain).strip()
    db: Database = context.application.bot_data["db"]
    if not db.update_scheduled_broadcast(int(broadcast_id), text=text):
        await update.effective_message.reply_text(t("scheduled_not_found", lang))
    else:
        await update.effective_message.reply_text(
            t("scheduled_updated", lang, id=broadcast_id),
            reply_markup=broadcast_menu(lang),
        )
    context.user_data.clear()


async def on_sb_sched_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if context.user_data.get("sb_edit_mode") != "schedule":
        if query:
            await query.answer()
        return
    await admin_sb_schedule_callback(update, context)


async def admin_sb_schedule_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _st = _conv_states()
    ADMIN_MSG_TYPE = _st["ADMIN_MSG_TYPE"]
    ADMIN_MSG_TEXT = _st["ADMIN_MSG_TEXT"]
    ADMIN_MSG_SCHEDULE = _st["ADMIN_MSG_SCHEDULE"]
    ADMIN_MSG_AUDIENCE = _st["ADMIN_MSG_AUDIENCE"]
    ADMIN_MSG_IDS = _st["ADMIN_MSG_IDS"]
    ADMIN_SB_EDIT_SCHEDULE = _st["ADMIN_SB_EDIT_SCHEDULE"]
    ADMIN_SB_EDIT_TEXT = _st["ADMIN_SB_EDIT_TEXT"]
    _set_wizard_back = _st["_set_wizard_back"]

    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not _is_admin(user_id):
        return ConversationHandler.END
    lang = _user_lang(context, user_id)
    broadcast_id = context.user_data.get("sb_edit_id")
    if not broadcast_id:
        return ConversationHandler.END

    data = query.data
    schedule = dict(context.user_data.get("schedule") or {})
    db: Database = context.application.bot_data["db"]
    item = db.get_scheduled_broadcast(int(broadcast_id))
    if not item:
        await query.edit_message_text(t("scheduled_not_found", lang))
        context.user_data.clear()
        return ConversationHandler.END

    if data == "sb_sched:noop":
        return ADMIN_SB_EDIT_SCHEDULE
    time_title = t("scheduled_edit_time_title", lang, id=broadcast_id)
    calendar_edit = _calendar_message(
        data,
        prefix="sb_sched",
        lang=lang,
        schedule=schedule,
        time_title=time_title,
        show_send_now=False,
    )
    if calendar_edit is not None:
        text, markup = calendar_edit
        await query.edit_message_text(text, reply_markup=markup)
        return ADMIN_SB_EDIT_SCHEDULE
    if data == "sb_sched:toggle_min":
        schedule["show_minutes"] = True
        context.user_data["schedule"] = schedule
        await query.edit_message_text(
            time_title,
            reply_markup=schedule_keyboard(
                lang, schedule, prefix="sb_sched", show_send_now=False
            ),
        )
        return ADMIN_SB_EDIT_SCHEDULE
    if data == "sb_sched:date_next":
        schedule["date_page"] = int(schedule.get("date_page", 0)) + 1
        context.user_data["schedule"] = schedule
        await query.edit_message_text(
            time_title,
            reply_markup=schedule_keyboard(
                lang, schedule, prefix="sb_sched", show_send_now=False
            ),
        )
        return ADMIN_SB_EDIT_SCHEDULE
    if data.startswith("sb_sched:date:"):
        schedule["date_offset"] = int(data.split(":")[2])
        schedule["date_page"] = int(schedule["date_offset"]) // 3
        context.user_data["schedule"] = schedule
        await query.edit_message_text(
            time_title,
            reply_markup=schedule_keyboard(
                lang, schedule, prefix="sb_sched", show_send_now=False
            ),
        )
        return ADMIN_SB_EDIT_SCHEDULE
    if data.startswith("sb_sched:hour:"):
        schedule["hour"] = int(data.split(":")[2])
        context.user_data["schedule"] = schedule
        await query.edit_message_text(
            t("scheduled_edit_time_title", lang, id=broadcast_id),
            reply_markup=schedule_keyboard(
                lang, schedule, prefix="sb_sched", show_send_now=False
            ),
        )
        return ADMIN_SB_EDIT_SCHEDULE
    if data.startswith("sb_sched:min:"):
        schedule["minute"] = int(data.split(":")[2])
        schedule["show_minutes"] = True
        context.user_data["schedule"] = schedule
        await query.edit_message_text(
            t("scheduled_edit_time_title", lang, id=broadcast_id),
            reply_markup=schedule_keyboard(
                lang, schedule, prefix="sb_sched", show_send_now=False
            ),
        )
        return ADMIN_SB_EDIT_SCHEDULE
    if data == "sb_sched:saved":
        hour, minute = db.get_saved_schedule(user_id)
        if hour is not None and minute is not None:
            schedule["hour"] = hour
            schedule["minute"] = minute
            schedule["show_minutes"] = True
            context.user_data["schedule"] = schedule
        await query.edit_message_text(
            t("scheduled_edit_time_title", lang, id=broadcast_id),
            reply_markup=schedule_keyboard(
                lang, schedule, prefix="sb_sched", show_send_now=False
            ),
        )
        return ADMIN_SB_EDIT_SCHEDULE

    if data == "sb_sched:apply":
        if schedule.get("hour") is None or schedule.get("minute") is None:
            await query.answer(t("schedule_pick_minutes", lang), show_alert=True)
            return ADMIN_SB_EDIT_SCHEDULE
        hour = int(schedule["hour"])
        minute = int(schedule["minute"])
        db.set_saved_schedule(user_id, hour, minute)
        scheduled_at = _schedule_to_utc_iso(schedule)
        msk_due = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
        if msk_due.tzinfo is None:
            msk_due = msk_due.replace(tzinfo=timezone.utc)
        when = (msk_due - datetime.now(timezone.utc)).total_seconds()
        if not db.update_scheduled_broadcast(int(broadcast_id), scheduled_at=scheduled_at):
            await query.edit_message_text(t("scheduled_not_found", lang))
        else:
            db.reset_broadcast_send_progress(int(broadcast_id))
            _schedule_broadcast_waves(
                context.application.job_queue, db, int(broadcast_id)
            )
            if when <= 0:
                try:
                    await query.edit_message_text(t("broadcast_started", lang))
                except BadRequest:
                    pass
            else:
                when_label = _format_scheduled_at_label(scheduled_at)
                await query.edit_message_text(
                    t("scheduled_updated", lang, id=broadcast_id) + f"\n{when_label}"
                )
        context.user_data.clear()
        await context.bot.send_message(
            user_id,
            t("menu_broadcast", lang),
            reply_markup=broadcast_menu(lang),
        )
        return ConversationHandler.END

    return ADMIN_SB_EDIT_SCHEDULE


async def process_scheduled_broadcasts(context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    bot_data = context.application.bot_data
    now = datetime.now(timezone.utc)
    for item in db.get_unsent_scheduled_broadcasts():
        sent_offsets = db.get_broadcast_sent_offsets(item.id)
        for offset, due_utc in _broadcast_waves(db, item):
            if offset in sent_offsets or due_utc > now:
                continue
            if not _claim_broadcast_wave_send(bot_data, item.id, offset):
                continue
            try:
                await _run_broadcast_offset_wave(context, item, offset)
            finally:
                _release_broadcast_wave_send(bot_data, item.id, offset)


async def _restore_broadcast_jobs(app: Application) -> None:
    db: Database = app.bot_data["db"]
    for item in db.get_unsent_scheduled_broadcasts():
        _schedule_broadcast_waves(app.job_queue, db, item.id)
