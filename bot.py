from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import date, datetime, timedelta, timezone

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MessageOriginChannel,
    MessageOriginChat,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, Conflict, Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from db import BotStats, Database, Subscription, is_on_notify_cooldown
from i18n import (
    DEFAULT_LOCALE,
    SCHEDULE_TZ,
    SUPPORTED_LOCALES,
    admin_menu,
    admin_type_keyboard,
    admin_wizard_menu,
    all_btn_texts,
    all_menu_buttons,
    all_wizard_nav_buttons,
    broadcast_menu,
    btn,
    delete_old_keyboard,
    delete_fail_notify_keyboard,
    dest_keyboard,
    dest_label,
    delay_keyboard,
    edit_bool_keyboard,
    edit_options_keyboard,
    ignore_keywords_keyboard,
    language_keyboard,
    link_preview_keyboard,
    main_menu,
    repeat_keyboard,
    schedule_keyboard,
    scheduled_edit_keyboard,
    scheduled_list_keyboard,
    settings_menu,
    stream_schedule_confirm_keyboard,
    stream_schedule_day_keyboard,
    format_stream_schedule_prompt_date,
    format_stream_schedule_result,
    subscriptions_menu,
    sys_notifications_keyboard,
    t,
    wizard_menu,
)
from links import TelegramTopicLink, chat_ref_to_id, parse_telegram_topic_link
from render_status import (
    StatusItem,
    fetch_render_status,
    is_aiven_outage,
    is_planned_maintenance,
)
from twitch import (
    TwitchClient,
    normalize_ignore_keywords,
    render_template,
    should_ignore_stream,
)
from translate import build_translations

logger = logging.getLogger(__name__)

GITHUB_ISSUES_URL = "https://github.com/Marfa/twitch-telegram-bot/issues"

(
    LANG_SELECT,
    CHANNEL,
    TEMPLATE,
    IGNORE_KEYWORDS,
    LINK_PREVIEW,
    DELAY_SEND,
    DELAY_MINUTES,
    REPEAT_ALLOW,
    REPEAT_MUTE_MINUTES,
    DEST_TYPE,
    DEST_CHAT,
    DELETE_OLD,
    DELETE_FAIL_NOTIFY,
    EDIT_TEMPLATE,
    EDIT_IGNORE_KEYWORDS,
    EDIT_DELAY,
    EDIT_REPEAT,
    ADMIN_MSG_TYPE,
    ADMIN_MSG_TEXT,
    ADMIN_MSG_SCHEDULE,
    ADMIN_SB_EDIT_TEXT,
    ADMIN_SB_EDIT_SCHEDULE,
    STREAM_SCHEDULE_CONFIRM,
    STREAM_SCHEDULE_GAME,
    STREAM_SCHEDULE_TIME,
) = range(25)

_STREAM_TIME_PATTERN = re.compile(r"^(\d{1,2}):(\d{2})$")


def _delay_current_label(minutes: int, lang: str) -> str:
    if minutes <= 0:
        return t("edit_delay_current_none", lang)
    return t("edit_delay_current", lang, minutes=minutes)


def _repeat_current_label(minutes: int, lang: str) -> str:
    if minutes <= 0:
        return t("edit_repeat_current_allow", lang)
    return t("edit_repeat_current_mute", lang, minutes=minutes)


def _ignore_keywords_current_label(keywords: str, lang: str) -> str:
    if not keywords.strip():
        return t("ignore_keywords_current_none", lang)
    return keywords


def _broadcast_type_label(msg_type: str, lang: str) -> str:
    if msg_type == "bot_update":
        return t("broadcast_type_bot_update", lang)
    if msg_type == "availability":
        return t("broadcast_type_availability", lang)
    return msg_type


def _format_scheduled_at_label(scheduled_at: str) -> str:
    dt = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(SCHEDULE_TZ).strftime("%d.%m.%Y %H:%M MSK")


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


def _broadcast_job_name(broadcast_id: int) -> str:
    return f"broadcast_{broadcast_id}"


def _cancel_broadcast_job(job_queue, broadcast_id: int) -> None:
    for job in job_queue.get_jobs_by_name(_broadcast_job_name(broadcast_id)):
        job.schedule_removal()


def _schedule_broadcast_job(job_queue, broadcast_id: int, scheduled_at: str) -> None:
    _cancel_broadcast_job(job_queue, broadcast_id)
    due = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    when = (due - datetime.now(timezone.utc)).total_seconds()
    if when > 0:
        job_queue.run_once(
            _run_scheduled_broadcast,
            when=when,
            data={"broadcast_id": broadcast_id},
            name=_broadcast_job_name(broadcast_id),
        )


def _scheduled_text_preview(text: str, limit: int = 120) -> str:
    plain = re.sub(r"<[^>]+>", "", text).strip()
    if len(plain) <= limit:
        return plain
    return plain[: limit - 1] + "…"


def _owner_sub_number(db: Database, owner_id: int, sub_id: int) -> int:
    for index, sub in enumerate(db.get_subscriptions_by_owner(owner_id), 1):
        if sub.id == sub_id:
            return index
    return sub_id


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


def _init_stream_schedule(context: ContextTypes.DEFAULT_TYPE) -> None:
    today = datetime.now(SCHEDULE_TZ).date()
    context.user_data["stream_schedule_dates"] = _next_week_dates(today)
    context.user_data["stream_schedule_index"] = 0
    context.user_data["stream_schedule_entries"] = []


async def _prompt_stream_schedule_game(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
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
            update.effective_user.id,
            message,
            reply_markup=keyboard,
        )
    else:
        await update.effective_message.reply_text(message, reply_markup=keyboard)
    return STREAM_SCHEDULE_GAME


async def _prompt_stream_schedule_time(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    keyboard = stream_schedule_day_keyboard(
        lang,
        show_finish=_stream_schedule_show_finish(context),
        show_skip=False,
    )
    await update.effective_message.reply_text(
        t("stream_schedule_time_prompt", lang),
        reply_markup=keyboard,
    )
    return STREAM_SCHEDULE_TIME


async def _finish_stream_schedule(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    user_id = update.effective_user.id
    entries: list[dict] = context.user_data.get("stream_schedule_entries", [])
    context.user_data.clear()
    text = format_stream_schedule_result(entries, lang) if entries else "—"
    if update.callback_query:
        await update.callback_query.edit_message_text("✓")
        await context.bot.send_message(
            user_id,
            text,
            reply_markup=_menu(lang, user_id),
        )
    else:
        await update.effective_message.reply_text(
            text,
            reply_markup=_menu(lang, user_id),
        )
    return ConversationHandler.END


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


def _menu(lang: str, user_id: int) -> ReplyKeyboardMarkup:
    return main_menu(lang, is_admin=_is_admin(user_id))


def _wizard(lang: str, *, back: bool = True) -> ReplyKeyboardMarkup:
    return wizard_menu(lang, back=back)


async def _prompt_repeat_step(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str, *, edit: bool = False
) -> int:
    if update.callback_query:
        await update.callback_query.edit_message_text(
            t("repeat_prompt", lang),
            reply_markup=repeat_keyboard(lang),
        )
    else:
        await update.effective_message.reply_text(
            t("repeat_prompt", lang),
            reply_markup=repeat_keyboard(lang),
        )
    _set_wizard_back(context, REPEAT_ALLOW)
    return REPEAT_ALLOW


async def _prompt_dest_step(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str, *, edit: bool = False
) -> int:
    if update.callback_query:
        await update.callback_query.edit_message_text(
            t("dest_prompt", lang),
            reply_markup=dest_keyboard(lang),
        )
    else:
        await update.effective_message.reply_text(
            t("dest_prompt", lang),
            reply_markup=dest_keyboard(lang),
        )
    _set_wizard_back(context, DEST_TYPE)
    return DEST_TYPE


async def _go_channel_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> int:
    await update.effective_message.reply_text(
        t("new_sub_prompt", lang),
        reply_markup=_wizard(lang, back=False),
    )
    _set_wizard_back(context, CHANNEL)
    return CHANNEL


async def _go_template_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> int:
    await update.effective_message.reply_text(
        t("channel_found", lang, display_name=html.escape(context.user_data.get("twitch_username", ""))),
        parse_mode=ParseMode.HTML,
        reply_markup=_wizard(lang),
    )
    _set_wizard_back(context, TEMPLATE)
    return TEMPLATE


async def _go_ignore_keywords_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> int:
    await update.effective_message.reply_text(
        t("ignore_keywords_prompt", lang),
        parse_mode=ParseMode.HTML,
        reply_markup=ignore_keywords_keyboard(lang),
    )
    _set_wizard_back(context, IGNORE_KEYWORDS)
    return IGNORE_KEYWORDS


async def _go_link_preview_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> int:
    await update.effective_message.reply_text(
        t("link_preview_prompt", lang),
        reply_markup=link_preview_keyboard(lang),
    )
    _set_wizard_back(context, LINK_PREVIEW)
    return LINK_PREVIEW


async def _go_delay_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> int:
    await update.effective_message.reply_text(
        t("delay_prompt", lang),
        reply_markup=delay_keyboard(lang),
    )
    _set_wizard_back(context, DELAY_SEND)
    return DELAY_SEND


async def _go_delay_minutes_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> int:
    await update.effective_message.reply_text(
        t("delay_minutes_prompt", lang),
        reply_markup=_wizard(lang),
    )
    _set_wizard_back(context, DELAY_MINUTES)
    return DELAY_MINUTES


async def _go_repeat_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> int:
    return await _prompt_repeat_step(update, context, lang)


async def _go_dest_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> int:
    return await _prompt_dest_step(update, context, lang)


async def wizard_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = _user_lang(context, update.effective_user.id)
    state = context.user_data.get("wizard_back_state")
    if state == TEMPLATE:
        return await _go_channel_prompt(update, context, lang)
    if state == IGNORE_KEYWORDS:
        return await _go_template_prompt(update, context, lang)
    if state == LINK_PREVIEW:
        return await _go_ignore_keywords_prompt(update, context, lang)
    if state == DELAY_SEND:
        return await _go_link_preview_prompt(update, context, lang)
    if state == DELAY_MINUTES:
        return await _go_delay_prompt(update, context, lang)
    if state == REPEAT_ALLOW:
        after = context.user_data.get("after_delay_state", DELAY_SEND)
        if after == DELAY_MINUTES:
            return await _go_delay_minutes_prompt(update, context, lang)
        return await _go_delay_prompt(update, context, lang)
    if state == REPEAT_MUTE_MINUTES:
        return await _go_repeat_prompt(update, context, lang)
    if state == DEST_TYPE:
        return await _go_repeat_prompt(update, context, lang)
    if state == DEST_CHAT:
        return await _go_dest_prompt(update, context, lang)
    if state == DELETE_OLD:
        dest_type = context.user_data.get("dest_type")
        if dest_type == "dm":
            return await _go_dest_prompt(update, context, lang)
        setup_key = "channel_setup" if dest_type == "channel" else "group_setup"
        await update.effective_message.reply_text(
            t(setup_key, lang),
            reply_markup=_wizard(lang),
        )
        _set_wizard_back(context, DEST_CHAT)
        return DEST_CHAT
    if state == DELETE_FAIL_NOTIFY:
        await update.effective_message.reply_text(
            t("delete_old_text", lang),
            reply_markup=delete_old_keyboard(lang),
        )
        _set_wizard_back(context, DELETE_OLD)
        return DELETE_OLD
    if state == ADMIN_MSG_TEXT:
        user_id = update.effective_user.id
        await update.effective_message.reply_text(
            t("broadcast_prompt", lang),
            reply_markup=admin_type_keyboard(lang),
        )
        _set_wizard_back(context, ADMIN_MSG_TYPE)
        return ADMIN_MSG_TYPE
    if state == ADMIN_MSG_SCHEDULE:
        await update.effective_message.reply_text(
            t("broadcast_text_prompt", lang),
            reply_markup=admin_wizard_menu(lang),
        )
        _set_wizard_back(context, ADMIN_MSG_TEXT)
        return ADMIN_MSG_TEXT
    if state == ADMIN_SB_EDIT_SCHEDULE:
        broadcast_id = context.user_data.get("sb_edit_id")
        if broadcast_id:
            return await _go_sb_edit_time_prompt(update, context, lang, int(broadcast_id))
    if state == ADMIN_SB_EDIT_TEXT:
        broadcast_id = context.user_data.get("sb_edit_id")
        if broadcast_id:
            return await _go_sb_edit_text_prompt(update, context, lang, int(broadcast_id))
    return ConversationHandler.END


def _set_wizard_back(context: ContextTypes.DEFAULT_TYPE, state: int) -> None:
    context.user_data["wizard_back_state"] = state


def _btn_filter(key: str) -> filters.Regex:
    texts = "|".join(re.escape(btn(key, loc)) for loc in SUPPORTED_LOCALES)
    return filters.Regex(f"^({texts})$")


def _is_admin(user_id: int) -> bool:
    from config import ADMIN_USER_IDS

    return user_id in ADMIN_USER_IDS


def _user_lang(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str:
    db: Database = context.application.bot_data["db"]
    return db.get_user_locale(user_id) or DEFAULT_LOCALE


def _help_text(lang: str) -> str:
    return t(
        "help",
        lang,
        btn_new=btn("new", lang),
        btn_manage=btn("manage", lang),
        btn_feedback=btn("feedback", lang),
    )


def _is_link_preview_disabled(message) -> bool:
    opts = message.link_preview_options
    return bool(opts and opts.is_disabled)


def _format_sub_line(
    sub: Subscription,
    lang: str,
    sub_num: int,
    *,
    chat_display: str | None = None,
    thread_display: str | None = None,
) -> str:
    status = "✅" if sub.enabled else "⏸"
    chat_label = chat_display if chat_display is not None else str(sub.chat_id)
    settings: list[str] = []
    if sub.ignore_keywords.strip():
        settings.append(
            t("sub_list_ignore_yes", lang, keywords=sub.ignore_keywords)
        )
    else:
        settings.append(t("sub_list_ignore_no", lang))
    settings.append(
        t("sub_list_preview_off", lang)
        if sub.disable_link_preview
        else t("sub_list_preview_on", lang)
    )
    settings.append(
        t("sub_list_delay", lang, minutes=sub.delay_minutes)
        if sub.delay_minutes > 0
        else t("sub_list_delay_none", lang)
    )
    settings.append(
        t("sub_list_repeat_mute", lang, minutes=sub.suppress_repeat_minutes)
        if sub.suppress_repeat_minutes > 0
        else t("sub_list_repeat_allow", lang)
    )
    settings.append(
        t(
            "sub_list_dest",
            lang,
            dest=dest_label(sub.dest_type, lang),
            chat_id=chat_label,
        )
    )
    if sub.thread_id:
        thread_label = thread_display if thread_display is not None else str(sub.thread_id)
        settings.append(t("sub_list_thread", lang, thread_id=thread_label))
    if sub.dest_type != "dm":
        settings.append(
            t("sub_list_delete_yes", lang)
            if sub.delete_previous
            else t("sub_list_delete_no", lang)
        )
        if sub.delete_previous and sub.notify_delete_fail:
            settings.append(t("sub_list_delete_fail", lang))
    return (
        f"{status} #{sub_num} — {sub.twitch_username}\n"
        + "\n".join(f"   {line}" for line in settings)
    )


async def _resolve_chat_display_name(bot, sub: Subscription) -> str:
    try:
        chat = await bot.get_chat(sub.chat_id)
        if sub.dest_type == "dm":
            parts = [chat.first_name or "", chat.last_name or ""]
            name = " ".join(part for part in parts if part).strip()
            if name:
                return name
        elif chat.title:
            return chat.title
        if chat.username:
            return f"@{chat.username}"
    except (BadRequest, Forbidden) as exc:
        logger.debug("Cannot resolve chat name for %s: %s", sub.chat_id, exc)
    return str(sub.chat_id)


async def _resolve_thread_display_name(bot, chat_id: int, thread_id: int) -> str:
    try:
        # PTB 21.x has no getForumTopic wrapper; call the Bot API directly.
        result = await bot._post(
            "getForumTopic",
            {"chat_id": chat_id, "message_thread_id": thread_id},
        )
        if isinstance(result, dict):
            name = result.get("name")
            if name:
                return str(name)
    except (BadRequest, Forbidden) as exc:
        logger.debug(
            "Cannot resolve topic name for %s/%s: %s", chat_id, thread_id, exc
        )
    except Exception:
        logger.exception(
            "Unexpected error resolving topic name for %s/%s", chat_id, thread_id
        )
    return str(thread_id)


def _message_link(chat_id: int, message_id: int, thread_id: int | None = None) -> str:
    s = str(chat_id)
    if s.startswith("-100"):
        internal = s[4:]
    else:
        internal = str(abs(chat_id))
    if thread_id:
        return f"https://t.me/c/{internal}/{thread_id}/{message_id}"
    return f"https://t.me/c/{internal}/{message_id}"


async def _send_notification(
    bot,
    db: Database,
    sub: Subscription,
    text: str,
) -> None:
    if sub.delete_previous and sub.last_message_id and sub.dest_type != "dm":
        try:
            await bot.delete_message(chat_id=sub.chat_id, message_id=sub.last_message_id)
        except (BadRequest, Forbidden) as exc:
            logger.warning(
                "Cannot delete message %s in %s: %s",
                sub.last_message_id,
                sub.chat_id,
                exc,
            )
            if sub.notify_delete_fail:
                lang = db.get_user_locale(sub.owner_id) or DEFAULT_LOCALE
                link = _message_link(sub.chat_id, sub.last_message_id, sub.thread_id)
                try:
                    await bot.send_message(
                        sub.owner_id,
                        t("delete_fail_notice", lang, link=link),
                    )
                except (BadRequest, Forbidden) as notify_exc:
                    logger.warning(
                        "Cannot notify owner %s about delete failure: %s",
                        sub.owner_id,
                        notify_exc,
                    )

    kwargs: dict = {"chat_id": sub.chat_id, "text": text}
    if sub.thread_id:
        kwargs["message_thread_id"] = sub.thread_id
    if sub.disable_link_preview:
        kwargs["disable_web_page_preview"] = True
    try:
        msg = await bot.send_message(**kwargs)
        if sub.delete_previous and sub.dest_type != "dm":
            db.set_last_message_id(sub.id, msg.message_id)
        if sub.suppress_repeat_minutes > 0:
            db.set_notify_cooldown(sub.id, sub.suppress_repeat_minutes)
    except (BadRequest, Forbidden) as exc:
        logger.warning("Cannot send to %s: %s", sub.chat_id, exc)


async def _send_test(bot, chat_id: int, thread_id: int | None, text: str) -> bool:
    kwargs: dict = {"chat_id": chat_id, "text": text}
    if thread_id:
        kwargs["message_thread_id"] = thread_id
    try:
        await bot.send_message(**kwargs)
        return True
    except (BadRequest, Forbidden) as exc:
        logger.warning("Cannot send to %s: %s", chat_id, exc)
        return False


async def _user_can_manage_chat(bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except (BadRequest, Forbidden) as exc:
        logger.warning(
            "Cannot verify membership of %s in %s: %s", user_id, chat_id, exc
        )
        return False
    return member.status in (
        ChatMemberStatus.OWNER,
        ChatMemberStatus.ADMINISTRATOR,
    )


async def _prompt_language(update: Update) -> int:
    await update.effective_message.reply_text(
        t("lang_pick", DEFAULT_LOCALE),
        reply_markup=language_keyboard(),
    )
    return LANG_SELECT


async def _begin_setup_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    user_id = update.effective_user.id
    db: Database = context.application.bot_data["db"]
    from config import MAX_SUBSCRIPTIONS_PER_OWNER

    if len(db.get_subscriptions_by_owner(user_id)) >= MAX_SUBSCRIPTIONS_PER_OWNER:
        await update.effective_message.reply_text(
            t("sub_limit", lang, limit=MAX_SUBSCRIPTIONS_PER_OWNER),
            reply_markup=_menu(lang, user_id),
        )
        return ConversationHandler.END
    await update.effective_message.reply_text(
        t("start_welcome", lang),
        reply_markup=_wizard(lang, back=False),
    )
    _set_wizard_back(context, CHANNEL)
    return CHANNEL


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    db: Database = context.application.bot_data["db"]
    user_id = update.effective_user.id
    db.upsert_user(user_id)
    lang = db.get_user_locale(user_id)
    if not lang:
        context.user_data["after_lang"] = "setup"
        return await _prompt_language(update)
    return await _begin_setup_message(update, context, lang)


async def receive_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = query.data.split(":", 1)[1]
    if lang not in SUPPORTED_LOCALES:
        lang = DEFAULT_LOCALE
    db: Database = context.application.bot_data["db"]
    db.set_user_locale(query.from_user.id, lang)
    await query.edit_message_text(t("lang_set", lang))
    after = context.user_data.pop("after_lang", "setup")
    if after == "help":
        await context.bot.send_message(
            query.from_user.id,
            _help_text(lang),
            reply_markup=_menu(lang, query.from_user.id),
        )
        return ConversationHandler.END
    if after == "settings":
        await context.bot.send_message(
            query.from_user.id,
            t("menu_settings", lang),
            reply_markup=settings_menu(lang),
        )
        return ConversationHandler.END
    from config import MAX_SUBSCRIPTIONS_PER_OWNER

    if len(db.get_subscriptions_by_owner(query.from_user.id)) >= MAX_SUBSCRIPTIONS_PER_OWNER:
        await context.bot.send_message(
            query.from_user.id,
            t("sub_limit", lang, limit=MAX_SUBSCRIPTIONS_PER_OWNER),
            reply_markup=_menu(lang, query.from_user.id),
        )
        return ConversationHandler.END
    await context.bot.send_message(
        query.from_user.id,
        t("start_welcome", lang),
        reply_markup=_wizard(lang, back=False),
    )
    _set_wizard_back(context, CHANNEL)
    return CHANNEL


async def start_new_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    from config import MAX_SUBSCRIPTIONS_PER_OWNER

    if len(db.get_subscriptions_by_owner(user_id)) >= MAX_SUBSCRIPTIONS_PER_OWNER:
        await update.effective_message.reply_text(
            t("sub_limit", lang, limit=MAX_SUBSCRIPTIONS_PER_OWNER),
            reply_markup=_menu(lang, user_id),
        )
        return ConversationHandler.END
    await update.effective_message.reply_text(
        t("new_sub_prompt", lang),
        reply_markup=_wizard(lang, back=False),
    )
    _set_wizard_back(context, CHANNEL)
    return CHANNEL


async def receive_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = _user_lang(context, update.effective_user.id)
    text = update.effective_message.text or ""
    if text in all_menu_buttons():
        await update.effective_message.reply_text(t("finish_setup_first", lang))
        return CHANNEL

    twitch: TwitchClient = context.application.bot_data["twitch"]
    username = twitch.parse_username(text)
    if not username:
        await update.effective_message.reply_text(t("channel_not_parsed", lang))
        return CHANNEL

    user = await asyncio.to_thread(twitch.get_user, username)
    if not user:
        await update.effective_message.reply_text(
            t("channel_not_found", lang, username=username)
        )
        return CHANNEL

    context.user_data["twitch_username"] = user["login"]
    context.user_data["twitch_user_id"] = user["id"]
    await update.effective_message.reply_text(
        t("channel_found", lang, display_name=html.escape(user["display_name"])),
        parse_mode=ParseMode.HTML,
        reply_markup=_wizard(lang),
    )
    _set_wizard_back(context, TEMPLATE)
    return TEMPLATE


async def receive_template(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = _user_lang(context, update.effective_user.id)
    template = (update.effective_message.text or "").strip()
    if template in all_menu_buttons():
        await update.effective_message.reply_text(t("finish_setup_first", lang))
        return TEMPLATE
    if not template:
        await update.effective_message.reply_text(t("template_empty", lang))
        return TEMPLATE

    context.user_data["message_template"] = template
    return await _go_ignore_keywords_prompt(update, context, lang)


async def receive_ignore_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = _user_lang(context, update.effective_user.id)
    text = (update.effective_message.text or "").strip()
    if text in all_menu_buttons():
        await update.effective_message.reply_text(t("finish_setup_first", lang))
        return IGNORE_KEYWORDS

    context.user_data["ignore_keywords"] = normalize_ignore_keywords(text)
    await update.effective_message.reply_text(
        t("link_preview_prompt", lang),
        reply_markup=link_preview_keyboard(lang),
    )
    _set_wizard_back(context, LINK_PREVIEW)
    return LINK_PREVIEW


async def receive_ignore_keywords_skip(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    context.user_data["ignore_keywords"] = ""
    await query.edit_message_text("✓")
    await context.bot.send_message(
        query.from_user.id,
        t("link_preview_prompt", lang),
        reply_markup=link_preview_keyboard(lang),
    )
    _set_wizard_back(context, LINK_PREVIEW)
    return LINK_PREVIEW


async def receive_link_preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    context.user_data["disable_link_preview"] = query.data.endswith(":1")
    await query.edit_message_text(
        t("delay_prompt", lang),
        reply_markup=delay_keyboard(lang),
    )
    _set_wizard_back(context, DELAY_SEND)
    return DELAY_SEND


async def receive_delay_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    if query.data.endswith(":1"):
        context.user_data["after_delay_state"] = DELAY_MINUTES
        await query.edit_message_text("✓")
        await context.bot.send_message(
            query.from_user.id,
            t("delay_minutes_prompt", lang),
            reply_markup=_wizard(lang),
        )
        _set_wizard_back(context, DELAY_MINUTES)
        return DELAY_MINUTES
    context.user_data["delay_minutes"] = 0
    context.user_data["after_delay_state"] = DELAY_SEND
    _set_wizard_back(context, REPEAT_ALLOW)
    return await _prompt_repeat_step(update, context, lang)


async def receive_delay_minutes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = _user_lang(context, update.effective_user.id)
    raw = (update.effective_message.text or "").strip()
    if raw in all_menu_buttons():
        await update.effective_message.reply_text(t("finish_setup_first", lang))
        return DELAY_MINUTES
    if not raw.isdigit() or int(raw) < 1:
        await update.effective_message.reply_text(t("delay_minutes_invalid", lang))
        return DELAY_MINUTES
    context.user_data["delay_minutes"] = int(raw)
    context.user_data["after_delay_state"] = DELAY_MINUTES
    _set_wizard_back(context, REPEAT_ALLOW)
    return await _prompt_repeat_step(update, context, lang)


async def receive_repeat_allow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    if query.data.endswith(":1"):
        context.user_data["suppress_repeat_minutes"] = 0
        _set_wizard_back(context, DEST_TYPE)
        return await _prompt_dest_step(update, context, lang)
    await query.edit_message_text("✓")
    await context.bot.send_message(
        query.from_user.id,
        t("repeat_mute_prompt", lang),
        reply_markup=_wizard(lang),
    )
    _set_wizard_back(context, REPEAT_MUTE_MINUTES)
    return REPEAT_MUTE_MINUTES


async def receive_repeat_mute_minutes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = _user_lang(context, update.effective_user.id)
    raw = (update.effective_message.text or "").strip()
    if raw in all_menu_buttons():
        await update.effective_message.reply_text(t("finish_setup_first", lang))
        return REPEAT_MUTE_MINUTES
    if not raw.isdigit() or int(raw) < 1:
        await update.effective_message.reply_text(t("repeat_mute_invalid", lang))
        return REPEAT_MUTE_MINUTES
    context.user_data["suppress_repeat_minutes"] = int(raw)
    _set_wizard_back(context, DEST_TYPE)
    return await _prompt_dest_step(update, context, lang)


async def start_edit_delay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    sub_id = int(query.data.split(":")[1])
    db: Database = context.application.bot_data["db"]
    sub = db.get_subscription(sub_id, query.from_user.id)
    if not sub:
        await query.edit_message_text(t("sub_not_found", lang))
        return ConversationHandler.END
    context.user_data["edit_sub_id"] = sub_id
    context.user_data["wizard_edit"] = True
    current = _delay_current_label(sub.delay_minutes, lang)
    sub_num = _owner_sub_number(db, query.from_user.id, sub_id)
    await query.edit_message_text("✓")
    await context.bot.send_message(
        query.from_user.id,
        t("edit_delay_prompt", lang, sub_id=sub_num, current=current),
        reply_markup=_wizard(lang, back=False),
    )
    return EDIT_DELAY


async def start_edit_repeat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    sub_id = int(query.data.split(":")[1])
    db: Database = context.application.bot_data["db"]
    sub = db.get_subscription(sub_id, query.from_user.id)
    if not sub:
        await query.edit_message_text(t("sub_not_found", lang))
        return ConversationHandler.END
    await query.edit_message_text(
        t("edit_repeat_menu", lang),
        reply_markup=edit_bool_keyboard(sub_id, "repeat", lang),
    )
    return ConversationHandler.END


async def receive_edit_delay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = _user_lang(context, update.effective_user.id)
    sub_id = context.user_data.get("edit_sub_id")
    if not sub_id:
        return ConversationHandler.END

    raw = (update.effective_message.text or "").strip()
    if raw in all_menu_buttons():
        await update.effective_message.reply_text(t("finish_setup_first", lang))
        return EDIT_DELAY
    if not raw.isdigit():
        await update.effective_message.reply_text(t("edit_delay_invalid", lang))
        return EDIT_DELAY

    delay_minutes = int(raw)
    db: Database = context.application.bot_data["db"]
    owner_id = update.effective_user.id
    sub_num = _owner_sub_number(db, owner_id, sub_id)
    if not db.update_subscription(sub_id, owner_id, delay_minutes=delay_minutes):
        await update.effective_message.reply_text(t("sub_not_found", lang))
    else:
        await update.effective_message.reply_text(
            t("edit_updated", lang, sub_id=sub_num),
            reply_markup=_menu(lang, owner_id),
        )
    context.user_data.clear()
    return ConversationHandler.END


async def receive_edit_template(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = _user_lang(context, update.effective_user.id)
    sub_id = context.user_data.get("edit_sub_id")
    if not sub_id:
        return ConversationHandler.END

    template = (update.effective_message.text or "").strip()
    if template in all_menu_buttons():
        await update.effective_message.reply_text(t("finish_setup_first", lang))
        return EDIT_TEMPLATE
    if not template:
        await update.effective_message.reply_text(t("template_empty", lang))
        return EDIT_TEMPLATE

    db: Database = context.application.bot_data["db"]
    owner_id = update.effective_user.id
    sub_num = _owner_sub_number(db, owner_id, sub_id)
    if not db.update_subscription(
        sub_id,
        owner_id,
        message_template=template,
        disable_link_preview=_is_link_preview_disabled(update.effective_message),
    ):
        await update.effective_message.reply_text(t("sub_not_found", lang))
    else:
        await update.effective_message.reply_text(
            t("edit_updated", lang, sub_id=sub_num),
            reply_markup=_menu(lang, owner_id),
        )
    context.user_data.clear()
    return ConversationHandler.END


async def receive_edit_repeat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = _user_lang(context, update.effective_user.id)
    sub_id = context.user_data.get("edit_sub_id")
    if not sub_id:
        return ConversationHandler.END

    raw = (update.effective_message.text or "").strip()
    if raw in all_menu_buttons():
        await update.effective_message.reply_text(t("finish_setup_first", lang))
        return EDIT_REPEAT
    if not raw.isdigit():
        await update.effective_message.reply_text(t("edit_repeat_invalid", lang))
        return EDIT_REPEAT

    minutes = int(raw)
    db: Database = context.application.bot_data["db"]
    owner_id = update.effective_user.id
    sub_num = _owner_sub_number(db, owner_id, sub_id)
    if not db.update_subscription(sub_id, owner_id, suppress_repeat_minutes=minutes):
        await update.effective_message.reply_text(t("sub_not_found", lang))
    else:
        await update.effective_message.reply_text(
            t("edit_updated", lang, sub_id=sub_num),
            reply_markup=_menu(lang, owner_id),
        )
    context.user_data.clear()
    return ConversationHandler.END


async def receive_dest_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    dest_type = query.data.split(":", 1)[1]
    context.user_data["dest_type"] = dest_type

    if dest_type == "dm":
        context.user_data["pending_chat_id"] = query.from_user.id
        context.user_data["pending_thread_id"] = None
        context.user_data["delete_previous"] = False
        context.user_data["notify_delete_fail"] = False
        return await _finish_subscription(
            update,
            context,
            query.from_user.id,
            query.from_user.id,
            None,
        )

    setup_key = "channel_setup" if dest_type == "channel" else "group_setup"
    await query.edit_message_text("✓")
    await context.bot.send_message(
        query.from_user.id,
        t(setup_key, lang),
        reply_markup=_wizard(lang),
    )
    _set_wizard_back(context, DEST_CHAT)
    return DEST_CHAT


async def _resolve_chat_ref(bot, ref: str) -> int:
    numeric = chat_ref_to_id(ref)
    if numeric is not None:
        return numeric
    chat = await bot.get_chat(f"@{ref.lstrip('@')}")
    return chat.id


async def _resolve_from_topic_link(bot, link: TelegramTopicLink) -> tuple[int, int]:
    chat_id = await _resolve_chat_ref(bot, link.chat_ref)
    return chat_id, link.thread_id


def _extract_forward_chat(message) -> tuple[int | None, int | None]:
    origin = message.forward_origin
    if isinstance(origin, MessageOriginChannel):
        return origin.chat.id, None
    if isinstance(origin, MessageOriginChat):
        return origin.sender_chat.id, message.message_thread_id or None
    if message.forward_from_chat:
        return message.forward_from_chat.id, message.message_thread_id or None
    return None, None


async def _parse_dest_input(
    bot,
    message,
    dest_type: str,
    lang: str,
) -> tuple[int | None, int | None, str | None]:
    text = (message.text or message.caption or "").strip()

    topic = parse_telegram_topic_link(text)
    if topic:
        try:
            chat_id, thread_id = await _resolve_from_topic_link(bot, topic)
            return chat_id, thread_id, None
        except BadRequest:
            return None, None, t("group_not_found", lang)

    fwd_chat, fwd_thread = _extract_forward_chat(message)
    if fwd_chat is not None:
        return fwd_chat, fwd_thread, None

    if text.startswith("@"):
        try:
            chat = await bot.get_chat(text)
            return chat.id, None, None
        except BadRequest:
            key = "dest_not_found_channel" if dest_type == "channel" else "dest_not_found_group"
            return None, None, t(key, lang)

    if text and re.fullmatch(r"-?\d+", text):
        return int(text), None, None

    if message.forward_origin or message.forward_from_chat:
        return None, None, t("fwd_from_dm", lang)

    hint_key = "dest_hint_group" if dest_type == "group" else "dest_hint_channel"
    return None, None, t(hint_key, lang)


async def receive_dest_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    lang = _user_lang(context, update.effective_user.id)
    dest_type = context.user_data.get("dest_type", "")

    chat_id, thread_id, error = await _parse_dest_input(
        context.bot, message, dest_type, lang
    )
    if error:
        await message.reply_text(error)
        return DEST_CHAT
    if chat_id is None:
        await message.reply_text(t("chat_not_determined", lang))
        return DEST_CHAT

    if dest_type == "channel":
        try:
            chat = await context.bot.get_chat(chat_id)
            if chat.type != ChatType.CHANNEL:
                await message.reply_text(t("not_a_channel", lang))
                return DEST_CHAT
        except BadRequest:
            await message.reply_text(t("bot_no_channel", lang))
            return DEST_CHAT

    if dest_type == "group":
        try:
            chat = await context.bot.get_chat(chat_id)
            if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
                await message.reply_text(t("not_a_group", lang))
                return DEST_CHAT
        except BadRequest:
            await message.reply_text(t("bot_no_group", lang))
            return DEST_CHAT

    if not await _user_can_manage_chat(
        context.bot, chat_id, update.effective_user.id
    ):
        await message.reply_text(t("dest_not_admin", lang))
        return DEST_CHAT

    ok = await _send_test(context.bot, chat_id, thread_id, t("test_ok", lang))
    if not ok:
        await message.reply_text(t("test_failed", lang))
        return DEST_CHAT

    context.user_data["pending_chat_id"] = chat_id
    context.user_data["pending_thread_id"] = thread_id
    await message.reply_text(
        t("delete_old_text", lang),
        reply_markup=delete_old_keyboard(lang),
    )
    _set_wizard_back(context, DELETE_OLD)
    return DELETE_OLD


async def receive_delete_old(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    delete_previous = query.data.endswith(":1")
    context.user_data["delete_previous"] = delete_previous
    if not delete_previous:
        context.user_data["notify_delete_fail"] = False
        chat_id = context.user_data["pending_chat_id"]
        thread_id = context.user_data.get("pending_thread_id")
        return await _finish_subscription(
            update, context, query.from_user.id, chat_id, thread_id
        )
    await query.edit_message_text(
        t("delete_fail_notify_text", lang),
        reply_markup=delete_fail_notify_keyboard(lang),
    )
    _set_wizard_back(context, DELETE_FAIL_NOTIFY)
    return DELETE_FAIL_NOTIFY


async def receive_delete_fail_notify(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["notify_delete_fail"] = query.data.endswith(":1")
    chat_id = context.user_data["pending_chat_id"]
    thread_id = context.user_data.get("pending_thread_id")
    return await _finish_subscription(
        update, context, query.from_user.id, chat_id, thread_id
    )


async def _finish_subscription(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_id: int,
    chat_id: int,
    thread_id: int | None,
) -> int:
    db: Database = context.application.bot_data["db"]
    lang = _user_lang(context, owner_id)
    data = dict(context.user_data)
    edit_sub_id = data.get("edit_sub_id")
    dest_type = data["dest_type"]
    delete_previous = bool(data.get("delete_previous", False)) and dest_type != "dm"
    notify_delete_fail = bool(data.get("notify_delete_fail", False)) and delete_previous

    try:
        if edit_sub_id:
            ok = db.update_subscription(
                edit_sub_id,
                owner_id,
                dest_type=dest_type,
                chat_id=chat_id,
                thread_id=thread_id,
                delete_previous=delete_previous,
                notify_delete_fail=notify_delete_fail,
            )
            if not ok:
                await context.bot.send_message(
                    owner_id,
                    t("sub_not_found", lang),
                    reply_markup=_menu(lang, owner_id),
                )
                context.user_data.clear()
                return ConversationHandler.END
            sub_id = edit_sub_id
        else:
            from config import MAX_SUBSCRIPTIONS_PER_OWNER

            if len(db.get_subscriptions_by_owner(owner_id)) >= MAX_SUBSCRIPTIONS_PER_OWNER:
                await context.bot.send_message(
                    owner_id,
                    t("sub_limit", lang, limit=MAX_SUBSCRIPTIONS_PER_OWNER),
                    reply_markup=_menu(lang, owner_id),
                )
                context.user_data.clear()
                return ConversationHandler.END
            sub_id = db.add_subscription(
                owner_id=owner_id,
                twitch_username=data["twitch_username"],
                twitch_user_id=data["twitch_user_id"],
                message_template=data["message_template"],
                dest_type=dest_type,
                chat_id=chat_id,
                thread_id=thread_id,
                delete_previous=delete_previous,
                notify_delete_fail=notify_delete_fail,
                disable_link_preview=bool(data.get("disable_link_preview", False)),
                delay_minutes=int(data.get("delay_minutes", 0)),
                suppress_repeat_minutes=int(data.get("suppress_repeat_minutes", 0)),
                ignore_keywords=str(data.get("ignore_keywords", "")),
            )
    except Exception:
        logger.exception("Failed to save subscription for owner %s", owner_id)
        await context.bot.send_message(
            owner_id,
            t("save_failed", lang),
            reply_markup=_menu(lang, owner_id),
        )
        context.user_data.clear()
        return ConversationHandler.END

    db.upsert_user(owner_id)
    context.user_data.clear()

    if edit_sub_id:
        text = t("edit_updated", lang, sub_id=sub_id)
    else:
        preview = render_template(
            data["message_template"],
            data["twitch_username"],
            "Just Chatting",
            t("preview_stream", lang),
        )
        thread_note = (
            t("thread_note", lang, thread_id=thread_id) if thread_id else ""
        )
        delete_note = (
            t("delete_yes", lang)
            if delete_previous
            else t("delete_no", lang)
        )
        delete_fail_note = ""
        if delete_previous:
            delete_fail_note = (
                "\n"
                + (
                    t("delete_fail_yes_note", lang)
                    if notify_delete_fail
                    else t("delete_fail_no_note", lang)
                )
            )
        preview_note = (
            t("preview_off", lang)
            if data.get("disable_link_preview")
            else t("preview_on", lang)
        )
        ignore_keywords = str(data.get("ignore_keywords", ""))
        ignore_keywords_note = (
            t("ignore_keywords_yes_note", lang, keywords=ignore_keywords)
            if ignore_keywords
            else t("ignore_keywords_no_note", lang)
        )
        delay_minutes = int(data.get("delay_minutes", 0))
        delay_note = (
            t("delay_yes_note", lang, minutes=delay_minutes)
            if delay_minutes > 0
            else t("delay_no_note", lang)
        )
        suppress = int(data.get("suppress_repeat_minutes", 0))
        repeat_note = (
            t("repeat_no_note", lang, minutes=suppress)
            if suppress > 0
            else t("repeat_yes_note", lang)
        )
        user_sub_num = _owner_sub_number(db, owner_id, sub_id)
        text = t(
            "setup_done",
            lang,
            sub_id=user_sub_num,
            twitch_username=data["twitch_username"],
            dest=dest_label(dest_type, lang),
            thread_note=thread_note,
            delete_note=delete_note,
            delete_fail_note=delete_fail_note,
            preview_note=preview_note,
            ignore_keywords_note=ignore_keywords_note,
            delay_note=delay_note,
            repeat_note=repeat_note,
            preview=preview,
        )

    if update.callback_query:
        try:
            msg_key = "sub_created_short" if not edit_sub_id else "edit_updated"
            await update.callback_query.edit_message_text(
                t(msg_key, lang, sub_id=_owner_sub_number(db, owner_id, sub_id))
            )
        except BadRequest:
            pass

    await context.bot.send_message(owner_id, text, reply_markup=_menu(lang, owner_id))
    return ConversationHandler.END


async def start_stream_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    context.user_data.clear()
    lang = _user_lang(context, user_id)
    await update.effective_message.reply_text(
        t("stream_schedule_intro", lang),
        parse_mode=ParseMode.HTML,
    )
    await update.effective_message.reply_text(
        t("stream_schedule_confirm", lang),
        reply_markup=stream_schedule_confirm_keyboard(lang),
    )
    return STREAM_SCHEDULE_CONFIRM


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
            query.from_user.id,
            t("menu_main", lang),
            reply_markup=_menu(lang, query.from_user.id),
        )
        return ConversationHandler.END
    _init_stream_schedule(context)
    return await _prompt_stream_schedule_game(update, context, lang)


async def stream_schedule_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = _user_lang(context, update.effective_user.id)
    text = (update.effective_message.text or "").strip()
    if text in all_menu_buttons() or text in all_wizard_nav_buttons():
        await update.effective_message.reply_text(t("finish_setup_first", lang))
        return STREAM_SCHEDULE_GAME
    if not text:
        await update.effective_message.reply_text(t("stream_schedule_game_empty", lang))
        return STREAM_SCHEDULE_GAME
    context.user_data["stream_schedule_game"] = text
    return await _prompt_stream_schedule_time(update, context, lang)


async def stream_schedule_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = _user_lang(context, update.effective_user.id)
    text = (update.effective_message.text or "").strip()
    if text in all_menu_buttons() or text in all_wizard_nav_buttons():
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
    return await _advance_stream_schedule_day(update, context, lang)


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


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(t("cancelled", lang))
        except BadRequest:
            pass
        await context.bot.send_message(
            user_id, t("menu_main", lang), reply_markup=_menu(lang, user_id)
        )
    else:
        await update.effective_message.reply_text(
            t("cancelled", lang),
            reply_markup=_menu(lang, user_id),
        )
    return ConversationHandler.END


async def report_problem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from config import BOT_VERSION

    db: Database = context.application.bot_data["db"]
    user_id = update.effective_user.id
    db.upsert_user(user_id)
    lang = _user_lang(context, user_id)
    version = BOT_VERSION[:7] if len(BOT_VERSION) >= 7 else BOT_VERSION
    await update.effective_message.reply_text(
        t("feedback", lang, github=GITHUB_ISSUES_URL, bot_version=version, user_id=user_id),
        reply_markup=_menu(lang, user_id),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db: Database = context.application.bot_data["db"]
    user_id = update.effective_user.id
    db.upsert_user(user_id)
    lang = db.get_user_locale(user_id)
    if not lang:
        context.user_data["after_lang"] = "help"
        return await _prompt_language(update)
    await update.effective_message.reply_text(
        _help_text(lang),
        reply_markup=_menu(lang, user_id),
    )
    return ConversationHandler.END


async def open_subscriptions_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    await update.effective_message.reply_text(
        t("menu_subs", lang),
        reply_markup=subscriptions_menu(lang),
    )


async def open_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        return
    lang = _user_lang(context, user_id)
    await update.effective_message.reply_text(
        t("menu_admin", lang),
        reply_markup=admin_menu(lang),
    )


async def open_broadcast_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        return
    context.user_data.clear()
    lang = _user_lang(context, user_id)
    await update.effective_message.reply_text(
        t("menu_broadcast", lang),
        reply_markup=broadcast_menu(lang),
    )


async def back_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    await update.effective_message.reply_text(
        t("menu_main", lang),
        reply_markup=_menu(lang, user_id),
    )


async def list_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    subs = db.get_subscriptions_by_owner(user_id)
    if not subs:
        await update.effective_message.reply_text(
            t("no_subs", lang),
            reply_markup=subscriptions_menu(lang),
        )
        return

    lines: list[str] = []
    for i, sub in enumerate(subs, 1):
        chat_display = await _resolve_chat_display_name(context.bot, sub)
        thread_display = None
        if sub.thread_id:
            thread_display = await _resolve_thread_display_name(
                context.bot, sub.chat_id, sub.thread_id
            )
        lines.append(
            _format_sub_line(
                sub,
                lang,
                i,
                chat_display=chat_display,
                thread_display=thread_display,
            )
        )
    keyboard = [
        [
            InlineKeyboardButton(
                f"{t('toggle_off', lang) if s.enabled else t('toggle_on', lang)} "
                f"#{i} {s.twitch_username}",
                callback_data=f"toggle:{s.id}",
            )
        ]
        for i, s in enumerate(subs, 1)
    ]
    await update.effective_message.reply_text(
        t("subs_list", lang) + "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    await update.effective_message.reply_text(
        t("menu_subs", lang),
        reply_markup=subscriptions_menu(lang),
    )


async def edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    subs = db.get_subscriptions_by_owner(user_id)
    if not subs:
        await update.effective_message.reply_text(
            t("no_subs_short", lang),
            reply_markup=subscriptions_menu(lang),
        )
        return

    keyboard = [
        [
            InlineKeyboardButton(
                f"✏️ #{i} {s.twitch_username}",
                callback_data=f"edit:{s.id}",
            )
        ]
        for i, s in enumerate(subs, 1)
    ]
    await update.effective_message.reply_text(
        t("edit_pick", lang),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    await update.effective_message.reply_text(
        t("menu_subs", lang),
        reply_markup=subscriptions_menu(lang),
    )


async def on_edit_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    sub_id = int(query.data.split(":", 1)[1])
    db: Database = context.application.bot_data["db"]
    sub = db.get_subscription(sub_id, query.from_user.id)
    if not sub:
        await query.edit_message_text(t("sub_not_found", lang))
        return
    sub_num = _owner_sub_number(db, query.from_user.id, sub_id)
    await query.edit_message_text(
        t("edit_menu", lang, sub_id=sub_num, username=sub.twitch_username),
        reply_markup=edit_options_keyboard(
            sub_id,
            lang,
            dest_type=sub.dest_type,
            delete_previous=sub.delete_previous,
        ),
    )


async def start_edit_template(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    sub_id = int(query.data.split(":")[1])
    db: Database = context.application.bot_data["db"]
    if not db.get_subscription(sub_id, query.from_user.id):
        await query.edit_message_text(t("sub_not_found", lang))
        return ConversationHandler.END
    sub_num = _owner_sub_number(db, query.from_user.id, sub_id)
    context.user_data["edit_sub_id"] = sub_id
    context.user_data["wizard_edit"] = True
    await query.edit_message_text("✓")
    await context.bot.send_message(
        query.from_user.id,
        t("edit_template_prompt", lang, sub_id=sub_num),
        parse_mode=ParseMode.HTML,
        reply_markup=_wizard(lang, back=False),
    )
    return EDIT_TEMPLATE


async def start_edit_ignore_keywords(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    sub_id = int(query.data.split(":")[1])
    db: Database = context.application.bot_data["db"]
    sub = db.get_subscription(sub_id, query.from_user.id)
    if not sub:
        await query.edit_message_text(t("sub_not_found", lang))
        return ConversationHandler.END
    context.user_data["edit_sub_id"] = sub_id
    context.user_data["wizard_edit"] = True
    current = _ignore_keywords_current_label(sub.ignore_keywords, lang)
    sub_num = _owner_sub_number(db, query.from_user.id, sub_id)
    await query.edit_message_text("✓")
    await context.bot.send_message(
        query.from_user.id,
        t("edit_ignore_keywords_prompt", lang, sub_id=sub_num, current=current),
        reply_markup=ignore_keywords_keyboard(lang),
    )
    return EDIT_IGNORE_KEYWORDS


async def receive_edit_ignore_keywords(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    lang = _user_lang(context, update.effective_user.id)
    sub_id = context.user_data.get("edit_sub_id")
    if not sub_id:
        return ConversationHandler.END

    text = (update.effective_message.text or "").strip()
    if text in all_menu_buttons():
        await update.effective_message.reply_text(t("finish_setup_first", lang))
        return EDIT_IGNORE_KEYWORDS

    keywords = normalize_ignore_keywords(text)
    db: Database = context.application.bot_data["db"]
    owner_id = update.effective_user.id
    sub_num = _owner_sub_number(db, owner_id, sub_id)
    if not db.update_subscription(sub_id, owner_id, ignore_keywords=keywords):
        await update.effective_message.reply_text(t("sub_not_found", lang))
    else:
        await update.effective_message.reply_text(
            t("edit_updated", lang, sub_id=sub_num),
            reply_markup=_menu(lang, owner_id),
        )
    context.user_data.clear()
    return ConversationHandler.END


async def receive_edit_ignore_keywords_skip(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    sub_id = context.user_data.get("edit_sub_id")
    if not sub_id:
        return ConversationHandler.END

    db: Database = context.application.bot_data["db"]
    owner_id = query.from_user.id
    sub_num = _owner_sub_number(db, owner_id, sub_id)
    if not db.update_subscription(sub_id, owner_id, ignore_keywords=""):
        await query.edit_message_text(t("sub_not_found", lang))
    else:
        await query.edit_message_text("✓")
        await context.bot.send_message(
            owner_id,
            t("edit_updated", lang, sub_id=sub_num),
            reply_markup=_menu(lang, owner_id),
        )
    context.user_data.clear()
    return ConversationHandler.END


async def start_edit_repeat_mute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    sub_id = int(query.data.split(":")[1])
    db: Database = context.application.bot_data["db"]
    sub = db.get_subscription(sub_id, query.from_user.id)
    if not sub:
        await query.edit_message_text(t("sub_not_found", lang))
        return ConversationHandler.END
    context.user_data["edit_sub_id"] = sub_id
    context.user_data["wizard_edit"] = True
    current = _repeat_current_label(sub.suppress_repeat_minutes, lang)
    sub_num = _owner_sub_number(db, query.from_user.id, sub_id)
    await query.edit_message_text("✓")
    await context.bot.send_message(
        query.from_user.id,
        t("edit_repeat_mute_prompt", lang, sub_id=sub_num, current=current),
        reply_markup=_wizard(lang, back=False),
    )
    return EDIT_REPEAT


async def start_edit_dest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    sub_id = int(query.data.split(":")[1])
    db: Database = context.application.bot_data["db"]
    sub = db.get_subscription(sub_id, query.from_user.id)
    if not sub:
        await query.edit_message_text(t("sub_not_found", lang))
        return ConversationHandler.END
    context.user_data["edit_sub_id"] = sub_id
    context.user_data["twitch_username"] = sub.twitch_username
    context.user_data["twitch_user_id"] = sub.twitch_user_id
    context.user_data["message_template"] = sub.message_template
    context.user_data["ignore_keywords"] = sub.ignore_keywords
    context.user_data["disable_link_preview"] = sub.disable_link_preview
    context.user_data["suppress_repeat_minutes"] = sub.suppress_repeat_minutes
    context.user_data["wizard_edit"] = True
    await query.edit_message_text(
        t("dest_prompt", lang),
        reply_markup=dest_keyboard(lang),
    )
    _set_wizard_back(context, DEST_TYPE)
    return DEST_TYPE


async def on_edit_bool_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    parts = query.data.split(":")
    sub_id = int(parts[1])
    field = parts[2]
    db: Database = context.application.bot_data["db"]
    sub = db.get_subscription(sub_id, query.from_user.id)
    if not sub:
        await query.edit_message_text(t("sub_not_found", lang))
        return
    if field in ("delete_old", "delete_fail") and sub.dest_type == "dm":
        await query.edit_message_text(
            t("edit_menu", lang, sub_id=_owner_sub_number(db, query.from_user.id, sub_id), username=sub.twitch_username),
            reply_markup=edit_options_keyboard(
                sub_id, lang, dest_type=sub.dest_type, delete_previous=sub.delete_previous
            ),
        )
        return
    menu_keys = {
        "delete_old": "edit_delete_old_menu",
        "delete_fail": "edit_delete_fail_menu",
        "preview": "edit_preview_menu",
        "repeat": "edit_repeat_menu",
    }
    await query.edit_message_text(
        t(menu_keys[field], lang),
        reply_markup=edit_bool_keyboard(sub_id, field, lang),
    )


async def on_edit_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    parts = query.data.split(":")
    sub_id = int(parts[1])
    field = parts[2]
    value = parts[3] == "1"
    db: Database = context.application.bot_data["db"]
    sub = db.get_subscription(sub_id, query.from_user.id)
    if not sub:
        await query.edit_message_text(t("sub_not_found", lang))
        return
    if field in ("delete_old", "delete_fail") and sub.dest_type == "dm":
        await query.edit_message_text(t("sub_not_found", lang))
        return
    if field == "delete_old":
        kwargs: dict = {"delete_previous": value}
        if not value:
            kwargs["notify_delete_fail"] = False
    elif field == "delete_fail":
        if not sub.delete_previous:
            await query.edit_message_text(t("sub_not_found", lang))
            return
        kwargs = {"notify_delete_fail": value}
    elif field == "preview":
        kwargs = {"disable_link_preview": value}
    elif field == "repeat":
        kwargs = {"suppress_repeat_minutes": 0} if value else None
        if kwargs is None:
            return
    else:
        return
    if db.update_subscription(sub_id, query.from_user.id, **kwargs):
        sub_num = _owner_sub_number(db, query.from_user.id, sub_id)
        await query.edit_message_text(t("edit_updated", lang, sub_id=sub_num))
    else:
        await query.edit_message_text(t("sub_not_found", lang))


async def delete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    subs = db.get_subscriptions_by_owner(user_id)
    if not subs:
        await update.effective_message.reply_text(
            t("no_subs_short", lang),
            reply_markup=subscriptions_menu(lang),
        )
        return

    keyboard = [
        [InlineKeyboardButton(f"🗑 #{i} {s.twitch_username}", callback_data=f"delete:{s.id}")]
        for i, s in enumerate(subs, 1)
    ]
    await update.effective_message.reply_text(
        t("delete_pick", lang),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    await update.effective_message.reply_text(
        t("menu_subs", lang),
        reply_markup=subscriptions_menu(lang),
    )


async def on_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    sub_id = int(query.data.split(":", 1)[1])
    db: Database = context.application.bot_data["db"]
    new_state = db.toggle_subscription(sub_id, query.from_user.id)
    if new_state is None:
        await query.edit_message_text(t("sub_not_found", lang))
        return
    key = "sub_enabled" if new_state else "sub_disabled"
    sub_num = _owner_sub_number(db, query.from_user.id, sub_id)
    await query.edit_message_text(t(key, lang, sub_id=sub_num))


async def on_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    sub_id = int(query.data.split(":", 1)[1])
    db: Database = context.application.bot_data["db"]
    sub_num = _owner_sub_number(db, query.from_user.id, sub_id)
    if db.delete_subscription(sub_id, query.from_user.id):
        await query.edit_message_text(t("sub_deleted", lang, sub_id=sub_num))
    else:
        await query.edit_message_text(t("sub_not_found", lang))


async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not _is_admin(user_id):
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
    query = update.callback_query
    await query.answer()
    if not _is_admin(query.from_user.id):
        return ConversationHandler.END
    lang = _user_lang(context, query.from_user.id)
    msg_type = query.data.split(":", 1)[1]
    context.user_data["admin_msg_type"] = msg_type
    await query.edit_message_text("✓")
    await context.bot.send_message(
        query.from_user.id,
        t("broadcast_text_prompt", lang),
        reply_markup=admin_wizard_menu(lang),
    )
    _set_wizard_back(context, ADMIN_MSG_TEXT)
    return ADMIN_MSG_TEXT


async def admin_receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        return ConversationHandler.END
    lang = _user_lang(context, user_id)
    msg = update.effective_message
    plain = (msg.text or "").strip()
    if plain in all_menu_buttons():
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


async def _send_admin_broadcast(
    context: ContextTypes.DEFAULT_TYPE,
    msg_type: str,
    text: str,
    *,
    source_lang: str | None = None,
) -> tuple[int, int, int, int]:
    db: Database = context.application.bot_data["db"]
    if msg_type == "bot_update":
        user_ids = db.get_bot_update_recipients()
    elif msg_type == "availability":
        user_ids = db.get_availability_recipients()
    else:
        user_ids = [
            uid for uid in db.get_notify_user_ids() if not db.is_bot_blocked(uid)
        ]
    source = source_lang or DEFAULT_LOCALE
    user_locales = {
        uid: db.get_user_locale(uid) or DEFAULT_LOCALE for uid in user_ids
    }
    translations = await asyncio.to_thread(
        build_translations,
        text,
        source,
        set(user_locales.values()),
    )
    sent = failed = blocked = 0
    for uid in user_ids:
        locale = user_locales[uid]
        message = translations.get(locale, text)
        try:
            try:
                await context.bot.send_message(
                    uid, message, parse_mode=ParseMode.HTML
                )
            except BadRequest:
                # Plain legacy text or translation broke tags — send without parse_mode.
                await context.bot.send_message(uid, message)
            sent += 1
        except Forbidden as exc:
            if "blocked" in str(exc).lower():
                db.set_bot_blocked(uid, True)
                blocked += 1
            else:
                failed += 1
                logger.warning("Broadcast to %s failed: %s", uid, exc)
        except BadRequest as exc:
            failed += 1
            logger.warning("Broadcast to %s failed: %s", uid, exc)
    return sent, failed, blocked, len(user_ids)


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    result = update.my_chat_member
    if result is None or result.chat.type != ChatType.PRIVATE:
        return
    db: Database = context.application.bot_data["db"]
    user_id = result.from_user.id
    status = result.new_chat_member.status
    if status == ChatMemberStatus.KICKED:
        db.set_bot_blocked(user_id, True)
    elif status in (ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED):
        db.set_bot_blocked(user_id, False)


async def admin_schedule_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
    if data == "sched:now":
        sent, failed, blocked, total = await _send_admin_broadcast(
            context, msg_type, text, source_lang=lang
        )
        context.user_data.clear()
        await query.edit_message_text(
            t(
                "broadcast_done",
                lang,
                sent=sent,
                failed=failed,
                blocked=blocked,
                total=total,
            )
        )
        await context.bot.send_message(user_id, t("menu_broadcast", lang), reply_markup=broadcast_menu(lang))
        return ConversationHandler.END

    if data == "sched:apply":
        if schedule.get("hour") is None or schedule.get("minute") is None:
            await query.answer(t("schedule_pick_minutes", lang), show_alert=True)
            return ADMIN_MSG_SCHEDULE
        hour = int(schedule["hour"])
        minute = int(schedule["minute"])
        db.set_saved_schedule(user_id, hour, minute)
        scheduled_at = _schedule_to_utc_iso(schedule)
        broadcast_id = db.add_scheduled_broadcast(msg_type, text, scheduled_at, user_id)
        when = (
            datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
            - datetime.now(timezone.utc)
        ).total_seconds()
        if when <= 0:
            sent, failed, blocked, total = await _send_admin_broadcast(
                context, msg_type, text, source_lang=lang
            )
            db.mark_scheduled_broadcast_sent(broadcast_id)
            context.user_data.clear()
            await query.edit_message_text(
                t(
                    "broadcast_done",
                    lang,
                    sent=sent,
                    failed=failed,
                    blocked=blocked,
                    total=total,
                )
            )
        else:
            context.job_queue.run_once(
                _run_scheduled_broadcast,
                when=when,
                data={"broadcast_id": broadcast_id},
                name=f"broadcast_{broadcast_id}",
            )
            when_local = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00")).astimezone(
                SCHEDULE_TZ
            )
            when_label = when_local.strftime("%d.%m.%Y %H:%M MSK")
            context.user_data.clear()
            await query.edit_message_text(
                t("broadcast_scheduled", lang, when=when_label)
            )
        await context.bot.send_message(user_id, t("menu_broadcast", lang), reply_markup=broadcast_menu(lang))
        return ConversationHandler.END

    return ADMIN_MSG_SCHEDULE


async def _run_scheduled_broadcast(context: ContextTypes.DEFAULT_TYPE) -> None:
    broadcast_id = context.job.data["broadcast_id"]
    db: Database = context.application.bot_data["db"]
    pending = db.get_pending_scheduled_broadcasts()
    item = next((b for b in pending if b.id == broadcast_id), None)
    if not item:
        unsent = db.get_unsent_scheduled_broadcasts()
        item = next((b for b in unsent if b.id == broadcast_id), None)
    if not item:
        return
    source_lang = db.get_user_locale(item.created_by) or DEFAULT_LOCALE
    await _send_admin_broadcast(
        context, item.msg_type, item.text, source_lang=source_lang
    )
    db.mark_scheduled_broadcast_sent(broadcast_id)


async def admin_scheduled_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not _is_admin(user_id):
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
        _cancel_broadcast_job(context.application.job_queue, broadcast_id)
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
) -> int:
    user_id = update.effective_user.id
    await context.bot.send_message(
        user_id,
        t("scheduled_edit_text_prompt", lang, id=broadcast_id),
        reply_markup=admin_wizard_menu(lang, back=False),
    )
    _set_wizard_back(context, ADMIN_SB_EDIT_TEXT)
    return ADMIN_SB_EDIT_TEXT


async def _go_sb_edit_time_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str, broadcast_id: int
) -> int:
    user_id = update.effective_user.id
    db: Database = context.application.bot_data["db"]
    item = db.get_scheduled_broadcast(broadcast_id)
    if not item:
        await context.bot.send_message(user_id, t("scheduled_not_found", lang))
        return ConversationHandler.END
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
    _set_wizard_back(context, ADMIN_SB_EDIT_SCHEDULE)
    return ADMIN_SB_EDIT_SCHEDULE


async def start_sb_edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not _is_admin(query.from_user.id):
        return ConversationHandler.END
    lang = _user_lang(context, query.from_user.id)
    broadcast_id = int(query.data.split(":")[2])
    db: Database = context.application.bot_data["db"]
    if not db.get_scheduled_broadcast(broadcast_id):
        await query.edit_message_text(t("scheduled_not_found", lang))
        return ConversationHandler.END
    context.user_data["sb_edit_id"] = broadcast_id
    await query.edit_message_text("✓")
    return await _go_sb_edit_text_prompt(update, context, lang, broadcast_id)


async def start_sb_edit_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not _is_admin(query.from_user.id):
        return ConversationHandler.END
    lang = _user_lang(context, query.from_user.id)
    broadcast_id = int(query.data.split(":")[2])
    db: Database = context.application.bot_data["db"]
    if not db.get_scheduled_broadcast(broadcast_id):
        await query.edit_message_text(t("scheduled_not_found", lang))
        return ConversationHandler.END
    context.user_data["sb_edit_id"] = broadcast_id
    await query.edit_message_text("✓")
    return await _go_sb_edit_time_prompt(update, context, lang, broadcast_id)


async def receive_sb_edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        return ConversationHandler.END
    lang = _user_lang(context, user_id)
    broadcast_id = context.user_data.get("sb_edit_id")
    if not broadcast_id:
        return ConversationHandler.END

    msg = update.effective_message
    plain = (msg.text or "").strip()
    if plain in all_menu_buttons():
        await update.effective_message.reply_text(t("finish_setup_first", lang))
        return ADMIN_SB_EDIT_TEXT
    if not plain:
        await update.effective_message.reply_text(t("broadcast_empty", lang))
        return ADMIN_SB_EDIT_TEXT

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
    return ConversationHandler.END


async def admin_sb_schedule_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
    if data == "sb_sched:toggle_min":
        schedule["show_minutes"] = True
        context.user_data["schedule"] = schedule
        await query.edit_message_text(
            t("scheduled_edit_time_title", lang, id=broadcast_id),
            reply_markup=schedule_keyboard(
                lang, schedule, prefix="sb_sched", show_send_now=False
            ),
        )
        return ADMIN_SB_EDIT_SCHEDULE
    if data == "sb_sched:date_next":
        schedule["date_page"] = int(schedule.get("date_page", 0)) + 1
        context.user_data["schedule"] = schedule
        await query.edit_message_text(
            t("scheduled_edit_time_title", lang, id=broadcast_id),
            reply_markup=schedule_keyboard(
                lang, schedule, prefix="sb_sched", show_send_now=False
            ),
        )
        return ADMIN_SB_EDIT_SCHEDULE
    if data.startswith("sb_sched:date:"):
        schedule["date_offset"] = int(data.split(":")[2])
        context.user_data["schedule"] = schedule
        await query.edit_message_text(
            t("scheduled_edit_time_title", lang, id=broadcast_id),
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
        when = (
            datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
            - datetime.now(timezone.utc)
        ).total_seconds()
        if when <= 0:
            source_lang = db.get_user_locale(item.created_by) or DEFAULT_LOCALE
            sent, failed, blocked, total = await _send_admin_broadcast(
                context, item.msg_type, item.text, source_lang=source_lang
            )
            db.mark_scheduled_broadcast_sent(int(broadcast_id))
            _cancel_broadcast_job(context.application.job_queue, int(broadcast_id))
            await query.edit_message_text(
                t(
                    "broadcast_done",
                    lang,
                    sent=sent,
                    failed=failed,
                    blocked=blocked,
                    total=total,
                )
            )
        elif db.update_scheduled_broadcast(int(broadcast_id), scheduled_at=scheduled_at):
            _schedule_broadcast_job(
                context.application.job_queue, int(broadcast_id), scheduled_at
            )
            when_label = _format_scheduled_at_label(scheduled_at)
            await query.edit_message_text(
                t("scheduled_updated", lang, id=broadcast_id) + f"\n{when_label}"
            )
        else:
            await query.edit_message_text(t("scheduled_not_found", lang))
        context.user_data.clear()
        await context.bot.send_message(
            user_id,
            t("menu_broadcast", lang),
            reply_markup=broadcast_menu(lang),
        )
        return ConversationHandler.END

    return ADMIN_SB_EDIT_SCHEDULE


async def open_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    await update.effective_message.reply_text(
        t("menu_settings", lang),
        reply_markup=settings_menu(lang),
    )


async def start_language_change(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    context.user_data["after_lang"] = "settings"
    await update.effective_message.reply_text(
        t("lang_pick", lang),
        reply_markup=language_keyboard(),
    )
    return LANG_SELECT


async def open_sys_notifications_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    await update.effective_message.reply_text(
        t("sys_notifications_menu", lang),
        reply_markup=sys_notifications_keyboard(
            lang,
            updates_enabled=db.get_receive_bot_updates(user_id),
            availability_enabled=db.get_receive_availability_updates(user_id),
        ),
    )


async def _refresh_sys_notifications_menu(
    query, context: ContextTypes.DEFAULT_TYPE, lang: str, user_id: int
) -> None:
    db: Database = context.application.bot_data["db"]
    await query.edit_message_text(
        t("sys_notifications_menu", lang),
        reply_markup=sys_notifications_keyboard(
            lang,
            updates_enabled=db.get_receive_bot_updates(user_id),
            availability_enabled=db.get_receive_availability_updates(user_id),
        ),
    )


async def on_sys_updates_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    db.set_receive_bot_updates(user_id, not db.get_receive_bot_updates(user_id))
    await _refresh_sys_notifications_menu(query, context, lang, user_id)


async def on_sys_availability_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    db.set_receive_availability_updates(
        user_id, not db.get_receive_availability_updates(user_id)
    )
    await _refresh_sys_notifications_menu(query, context, lang, user_id)


def _format_stats(stats: BotStats, lang: str) -> str:
    return t(
        "bot_stats",
        lang,
        users=stats.users,
        notify_users=stats.notify_users,
        subscriptions_total=stats.subscriptions_total,
        subscriptions_enabled=stats.subscriptions_enabled,
        subscriptions_disabled=stats.subscriptions_disabled,
        unique_owners=stats.unique_owners,
        unique_twitch_channels=stats.unique_twitch_channels,
        sys_updates=stats.sys_updates,
        sys_availability=stats.sys_availability,
        blocked_users=stats.blocked_users,
        locale_en=stats.locale_en,
        locale_ru=stats.locale_ru,
        locale_unset=stats.locale_unset,
    )


async def admin_show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        return
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    stats = db.get_bot_stats()
    await update.effective_message.reply_text(
        _format_stats(stats, lang),
        reply_markup=admin_menu(lang),
    )


async def _send_delayed_notification(context: ContextTypes.DEFAULT_TYPE) -> None:
    sub_id = context.job.data["sub_id"]
    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    sub = db.get_subscription_by_id(sub_id)
    if not sub or not sub.enabled:
        return

    try:
        live_streams = await asyncio.to_thread(
            twitch.get_live_streams, [sub.twitch_user_id]
        )
    except Exception:
        logger.exception("Twitch poll failed for delayed notification sub %s", sub_id)
        return

    lang = db.get_user_locale(sub.owner_id) or DEFAULT_LOCALE
    if sub.twitch_user_id not in live_streams:
        preview = render_template(
            sub.message_template,
            sub.twitch_username,
            "—",
            "—",
        )
        try:
            await context.bot.send_message(
                sub.owner_id,
                t("delayed_not_sent", lang, message=preview),
            )
        except (BadRequest, Forbidden) as exc:
            logger.warning("Cannot notify owner %s: %s", sub.owner_id, exc)
        return

    stream = live_streams[sub.twitch_user_id]
    if is_on_notify_cooldown(sub):
        return
    username = stream.get("user_login", stream.get("user_name", ""))
    game = stream.get("game_name", "")
    title = stream.get("title", "")
    if should_ignore_stream(sub.ignore_keywords, game, title):
        return
    text = render_template(sub.message_template, username, game, title)
    await _send_notification(context.bot, db, sub, text)


async def check_streams(context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    last_live: dict[str, bool] = context.application.bot_data.setdefault("last_live", {})

    user_ids = db.get_unique_twitch_user_ids()
    if not user_ids:
        return

    try:
        live_streams = await asyncio.to_thread(twitch.get_live_streams, user_ids)
    except Exception:
        logger.exception("Twitch poll failed")
        return

    for uid in user_ids:
        is_live = uid in live_streams
        was_live = last_live.get(uid, False)
        if is_live and not was_live:
            stream = live_streams[uid]
            username = stream.get("user_login", stream.get("user_name", ""))
            game = stream.get("game_name", "")
            title = stream.get("title", "")
            for sub in db.get_enabled_by_twitch_user_id(uid):
                if is_on_notify_cooldown(sub):
                    continue
                if should_ignore_stream(sub.ignore_keywords, game, title):
                    continue
                if sub.delay_minutes > 0:
                    context.job_queue.run_once(
                        _send_delayed_notification,
                        when=sub.delay_minutes * 60,
                        data={"sub_id": sub.id},
                        name=f"delay_{sub.id}",
                    )
                    continue
                text = render_template(sub.message_template, username, game, title)
                await _send_notification(context.bot, db, sub, text)
        last_live[uid] = is_live


async def _notify_status_items(
    context: ContextTypes.DEFAULT_TYPE,
    items: list[StatusItem],
    message_key: str,
) -> None:
    db: Database = context.application.bot_data["db"]
    user_ids = db.get_availability_recipients()
    if not user_ids:
        return

    for item in items:
        body = item.description[:600] + ("…" if len(item.description) > 600 else "")
        for user_id in user_ids:
            lang = db.get_user_locale(user_id) or DEFAULT_LOCALE
            text = t(
                message_key,
                lang,
                title=item.title,
                body=body,
                link=item.link,
            )
            try:
                await context.bot.send_message(
                    user_id,
                    text,
                    disable_web_page_preview=True,
                )
            except Forbidden as exc:
                if "blocked" in str(exc).lower():
                    db.set_bot_blocked(user_id, True)
                else:
                    logger.warning("Cannot notify user %s: %s", user_id, exc)
            except BadRequest as exc:
                logger.warning("Cannot notify user %s: %s", user_id, exc)


async def _check_status_rss(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    rss_url: str,
    seeded_key: str,
    matches,
    message_key: str,
    log_label: str,
) -> None:
    db: Database = context.application.bot_data["db"]
    seeded = context.application.bot_data.setdefault(seeded_key, False)

    try:
        items = await asyncio.to_thread(fetch_render_status, rss_url)
    except Exception:
        logger.exception("%s status RSS fetch failed", log_label)
        return

    new_items: list[StatusItem] = []
    for item in items:
        if db.is_status_seen(item.guid):
            continue
        db.mark_status_seen(item.guid)
        if not seeded:
            continue
        if matches(item):
            new_items.append(item)

    context.application.bot_data[seeded_key] = True
    if not new_items:
        return

    await _notify_status_items(context, new_items, message_key)


async def check_render_status(context: ContextTypes.DEFAULT_TYPE) -> None:
    from config import RENDER_STATUS_RSS

    await _check_status_rss(
        context,
        rss_url=RENDER_STATUS_RSS,
        seeded_key="render_status_seeded",
        matches=is_planned_maintenance,
        message_key="render_maintenance",
        log_label="Render",
    )


async def check_aiven_status(context: ContextTypes.DEFAULT_TYPE) -> None:
    from config import AIVEN_STATUS_RSS

    await _check_status_rss(
        context,
        rss_url=AIVEN_STATUS_RSS,
        seeded_key="aiven_status_seeded",
        matches=is_aiven_outage,
        message_key="aiven_outage",
        log_label="Aiven",
    )


async def process_scheduled_broadcasts(context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    for item in db.get_pending_scheduled_broadcasts():
        source_lang = db.get_user_locale(item.created_by) or DEFAULT_LOCALE
        await _send_admin_broadcast(
            context, item.msg_type, item.text, source_lang=source_lang
        )
        db.mark_scheduled_broadcast_sent(item.id)


def _seconds_until_next_weekly_report() -> float:
    now = datetime.now(SCHEDULE_TZ)
    # Next Monday 10:00 MSK
    days_ahead = (7 - now.weekday()) % 7
    target = now.replace(hour=10, minute=0, second=0, microsecond=0) + timedelta(
        days=days_ahead
    )
    if target <= now:
        target += timedelta(days=7)
    return (target - now).total_seconds()


async def weekly_new_users_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    from config import ADMIN_USER_IDS

    if not ADMIN_USER_IDS:
        return
    db: Database = context.application.bot_data["db"]
    since = datetime.now(timezone.utc) - timedelta(days=7)
    count = db.count_new_users_since(since)
    if count <= 0:
        return
    for admin_id in ADMIN_USER_IDS:
        lang = db.get_user_locale(admin_id) or DEFAULT_LOCALE
        try:
            await context.bot.send_message(
                admin_id,
                t("weekly_new_users", lang, count=count),
            )
        except (BadRequest, Forbidden) as exc:
            logger.warning("Cannot send weekly report to admin %s: %s", admin_id, exc)


async def _restore_broadcast_jobs(app: Application) -> None:
    db: Database = app.bot_data["db"]
    now = datetime.now(timezone.utc)
    for item in db.get_unsent_scheduled_broadcasts():
        try:
            due = datetime.fromisoformat(item.scheduled_at.replace("Z", "+00:00"))
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        delta = (due - now).total_seconds()
        if delta <= 0:
            continue
        app.job_queue.run_once(
            _run_scheduled_broadcast,
            when=delta,
            data={"broadcast_id": item.id},
            name=f"broadcast_{item.id}",
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, Conflict):
        logger.warning(t("conflict_polling", DEFAULT_LOCALE))
        return
    logger.exception(t("unhandled_error", DEFAULT_LOCALE, err=err))


def build_application(token: str, db: Database, twitch: TwitchClient) -> Application:
    async def post_init(application: Application) -> None:
        await _restore_broadcast_jobs(application)

    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .build()
    )
    app.bot_data["db"] = db
    app.bot_data["twitch"] = twitch
    app.bot_data["last_live"] = {}
    app.add_error_handler(error_handler)

    app.add_handler(
        MessageHandler(_btn_filter("feedback"), report_problem),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("manage"), open_subscriptions_menu),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("admin"), open_admin_menu),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("back"), back_to_main_menu),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("list"), list_subscriptions),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("edit"), edit_menu),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("delete"), delete_menu),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("stats"), admin_show_stats),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("broadcast"), open_broadcast_menu),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("scheduled_broadcasts"), admin_scheduled_list),
        group=0,
    )
    app.add_handler(CallbackQueryHandler(on_sb_edit_pick, pattern=r"^sb_edit:\d+$"), group=0)
    app.add_handler(CallbackQueryHandler(on_sb_delete, pattern=r"^sb_delete:\d+$"), group=0)
    app.add_handler(
        ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("settings"), open_settings_menu),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("sys_notifications"), open_sys_notifications_menu),
        group=0,
    )
    app.add_handler(CallbackQueryHandler(on_sys_updates_toggle, pattern=r"^sys_updates:toggle$"), group=0)
    app.add_handler(
        CallbackQueryHandler(on_sys_availability_toggle, pattern=r"^sys_availability:toggle$"),
        group=0,
    )
    app.add_handler(CallbackQueryHandler(on_toggle, pattern=r"^toggle:"), group=0)
    app.add_handler(CallbackQueryHandler(on_delete, pattern=r"^delete:"), group=0)
    app.add_handler(CallbackQueryHandler(on_edit_pick, pattern=r"^edit:\d+$"), group=0)
    app.add_handler(
        CallbackQueryHandler(
            on_edit_bool_menu,
            pattern=r"^edit_f:\d+:(delete_old|delete_fail|preview|repeat)$",
        ),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(
            on_edit_set,
            pattern=r"^edit_set:\d+:(delete_old|delete_fail|preview):[01]$|^edit_set:\d+:repeat:1$",
        ),
        group=0,
    )

    _wiz_cancel = MessageHandler(_btn_filter("wizard_cancel"), cancel)
    _wiz_back = MessageHandler(_btn_filter("wizard_back"), wizard_back)

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("help", help_command),
            MessageHandler(_btn_filter("new"), start_new_subscription),
            MessageHandler(_btn_filter("create_schedule"), start_stream_schedule),
            MessageHandler(_btn_filter("language"), start_language_change),
            MessageHandler(_btn_filter("broadcast_new"), admin_broadcast_start),
            CallbackQueryHandler(start_sb_edit_text, pattern=r"^sb_edit_f:\d+:text$"),
            CallbackQueryHandler(start_sb_edit_time, pattern=r"^sb_edit_f:\d+:time$"),
            CallbackQueryHandler(start_edit_template, pattern=r"^edit_f:\d+:template$"),
            CallbackQueryHandler(start_edit_ignore_keywords, pattern=r"^edit_f:\d+:ignore_keywords$"),
            CallbackQueryHandler(start_edit_dest, pattern=r"^edit_f:\d+:dest$"),
            CallbackQueryHandler(start_edit_delay, pattern=r"^edit_f:\d+:delay$"),
            CallbackQueryHandler(start_edit_repeat, pattern=r"^edit_f:\d+:repeat$"),
            CallbackQueryHandler(start_edit_repeat_mute, pattern=r"^edit_set:\d+:repeat:0$"),
        ],
        states={
            LANG_SELECT: [CallbackQueryHandler(receive_language, pattern=r"^lang:")],
            CHANNEL: [
                _wiz_cancel,
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_channel),
            ],
            TEMPLATE: [
                _wiz_cancel,
                _wiz_back,
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_template),
            ],
            IGNORE_KEYWORDS: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(
                    receive_ignore_keywords_skip, pattern=r"^ignore_keywords:skip$"
                ),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_ignore_keywords),
            ],
            LINK_PREVIEW: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(receive_link_preview, pattern=r"^link_preview:"),
            ],
            DELAY_SEND: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(receive_delay_send, pattern=r"^delay_send:"),
            ],
            DELAY_MINUTES: [
                _wiz_cancel,
                _wiz_back,
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_delay_minutes),
            ],
            REPEAT_ALLOW: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(receive_repeat_allow, pattern=r"^repeat:"),
            ],
            REPEAT_MUTE_MINUTES: [
                _wiz_cancel,
                _wiz_back,
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_repeat_mute_minutes),
            ],
            EDIT_TEMPLATE: [
                _wiz_cancel,
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_edit_template),
            ],
            EDIT_IGNORE_KEYWORDS: [
                _wiz_cancel,
                CallbackQueryHandler(
                    receive_edit_ignore_keywords_skip, pattern=r"^ignore_keywords:skip$"
                ),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_edit_ignore_keywords),
            ],
            EDIT_DELAY: [
                _wiz_cancel,
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_edit_delay),
            ],
            EDIT_REPEAT: [
                _wiz_cancel,
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_edit_repeat),
            ],
            DEST_TYPE: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(receive_dest_type, pattern=r"^dest:"),
            ],
            DEST_CHAT: [
                _wiz_cancel,
                _wiz_back,
                MessageHandler(filters.TEXT | filters.FORWARDED, receive_dest_chat),
            ],
            DELETE_OLD: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(receive_delete_old, pattern=r"^delete_old:"),
            ],
            DELETE_FAIL_NOTIFY: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(receive_delete_fail_notify, pattern=r"^delete_fail:"),
            ],
            ADMIN_MSG_TYPE: [
                _wiz_cancel,
                CallbackQueryHandler(admin_select_type, pattern=r"^admin_type:"),
            ],
            ADMIN_MSG_TEXT: [
                _wiz_cancel,
                _wiz_back,
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_receive_text),
            ],
            ADMIN_MSG_SCHEDULE: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(admin_schedule_callback, pattern=r"^sched:"),
            ],
            ADMIN_SB_EDIT_TEXT: [
                _wiz_cancel,
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_sb_edit_text),
            ],
            ADMIN_SB_EDIT_SCHEDULE: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(admin_sb_schedule_callback, pattern=r"^sb_sched:"),
            ],
            STREAM_SCHEDULE_CONFIRM: [
                _wiz_cancel,
                CallbackQueryHandler(
                    stream_schedule_confirm_callback, pattern=r"^stream_sched:confirm:"
                ),
            ],
            STREAM_SCHEDULE_GAME: [
                _wiz_cancel,
                CallbackQueryHandler(stream_schedule_skip_callback, pattern=r"^stream_sched:skip$"),
                CallbackQueryHandler(stream_schedule_finish_callback, pattern=r"^stream_sched:finish$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, stream_schedule_game),
            ],
            STREAM_SCHEDULE_TIME: [
                _wiz_cancel,
                CallbackQueryHandler(stream_schedule_skip_callback, pattern=r"^stream_sched:skip$"),
                CallbackQueryHandler(stream_schedule_finish_callback, pattern=r"^stream_sched:finish$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, stream_schedule_time),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("help", help_command),
        ],
        allow_reentry=True,
    )

    app.add_handler(conv, group=1)

    from config import CHECK_INTERVAL, DATABASE_URL, STATUS_CHECK_INTERVAL

    app.job_queue.run_repeating(check_streams, interval=CHECK_INTERVAL, first=10)
    app.job_queue.run_repeating(process_scheduled_broadcasts, interval=60, first=20)
    app.job_queue.run_repeating(
        check_render_status, interval=STATUS_CHECK_INTERVAL, first=30
    )
    app.job_queue.run_repeating(
        weekly_new_users_report,
        interval=7 * 24 * 3600,
        first=_seconds_until_next_weekly_report(),
    )
    if DATABASE_URL:
        app.job_queue.run_repeating(
            check_aiven_status, interval=STATUS_CHECK_INTERVAL, first=45
        )
    return app
