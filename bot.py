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
from telegram.error import BadRequest, Conflict, Forbidden, RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

import premium as prem
import demo_mode
from db import (
    BotStats,
    Database,
    Subscription,
    TwitchSync,
    WATCH_MAX_FILTERS,
    WatchPrefs,
    is_on_notify_cooldown,
)
from i18n import (
    DEFAULT_LOCALE,
    SCHEDULE_TZ,
    SCHEDULE_TZ_NAME,
    SUPPORTED_LOCALES,
    admin_menu,
    admin_type_keyboard,
    admin_wizard_menu,
    alert_type_keyboard,
    all_btn_texts,
    all_menu_buttons,
    all_wizard_nav_buttons,
    broadcast_menu,
    btn,
    channel_dup_keyboard,
    delete_old_keyboard,
    delete_fail_notify_keyboard,
    delete_sibling_keyboard,
    dest_keyboard,
    dest_label,
    delay_keyboard,
    edit_bool_keyboard,
    edit_options_keyboard,
    ignore_keywords_keyboard,
    ignored_words_keyboard,
    image_ask_keyboard,
    image_edit_keyboard,
    image_position_keyboard,
    import_mode_keyboard,
    language_keyboard,
    link_preview_keyboard,
    lucky_preview_keyboard,
    main_menu,
    placeholders_link_html,
    partner_menu,
    premium_gate_keyboard,
    repeat_keyboard,
    schedule_keyboard,
    schedule_reminder_keyboard,
    schedule_live_add_keyboard,
    scheduled_edit_keyboard,
    scheduled_list_keyboard,
    settings_menu,
    stream_schedule_confirm_keyboard,
    stream_schedule_day_keyboard,
    stream_schedule_duration_keyboard,
    stream_schedule_publish_keyboard,
    format_stream_schedule_prompt_date,
    format_stream_schedule_result,
    subscriptions_menu,
    sync_settings_keyboard,
    withdrawal_actions_keyboard,
    sys_notifications_keyboard,
    t,
    template_strip_keyboard,
    template_typo_keyboard,
    watch_cats_nav_keyboard,
    watch_cats_pick_keyboard,
    watch_lang_keyboard,
    watch_mature_keyboard,
    watch_pick_keyboard,
    watch_delete_pick_keyboard,
    watch_save_keyboard,
    watch_suggest_keyboard,
    watch_tags_keyboard,
    watch_viewers_keyboard,
    wizard_menu,
)
from links import TelegramTopicLink, chat_ref_to_id, parse_telegram_topic_link
from twitch import (
    TwitchClient,
    fetch_twitch_status_summary,
    filter_streams_for_watch,
    find_placeholder_typos,
    normalize_ignore_keywords,
    merge_ignore_keywords,
    normalize_watch_tags,
    pick_random_streams,
    preview_stream_title,
    render_template,
    should_ignore_stream,
    template_has_link,
    twitch_status_fingerprint,
)
from translate import build_translations
from hf_text import generate_alert_template

logger = logging.getLogger(__name__)

GITHUB_ISSUES_URL = "https://github.com/Marfa/twitch-telegram-bot/issues"
TWITCH_STATUS_PAGE_URL = "https://status.twitch.com/"
_TELEGRAM_CAPTION_LIMIT = 1024
# Soft pacing for mass DM sends — keeps under Telegram flood limits.
_BROADCAST_SEND_PAUSE = 0.05

_TWITCH_INDICATOR_KEYS = {
    "none": "twitch_indicator_none",
    "minor": "twitch_indicator_minor",
    "major": "twitch_indicator_major",
    "critical": "twitch_indicator_critical",
    "maintenance": "twitch_indicator_maintenance",
}
_TWITCH_COMPONENT_KEYS = {
    "operational": "twitch_comp_operational",
    "degraded_performance": "twitch_comp_degraded",
    "partial_outage": "twitch_comp_partial",
    "major_outage": "twitch_comp_major",
    "under_maintenance": "twitch_comp_maintenance",
}

(
    LANG_SELECT,
    ALERT_TYPE,
    CHANNEL,
    CHANNEL_DUP,
    TEMPLATE,
    TEMPLATE_TYPO_CONFIRM,
    IMAGE_ASK,
    IMAGE_UPLOAD,
    IMAGE_POSITION,
    LUCKY_PREVIEW,
    IGNORE_KEYWORDS,
    LINK_PREVIEW,
    DELAY_SEND,
    DELAY_MINUTES,
    REPEAT_ALLOW,
    REPEAT_MUTE_MINUTES,
    SCHEDULE_REMINDER_ASK,
    SCHEDULE_REMINDER_MINUTES,
    DEST_TYPE,
    DEST_CHAT,
    DELETE_OLD,
    DELETE_FAIL_NOTIFY,
    SCHEDULE_LIVE_ASK,
    EDIT_TEMPLATE,
    EDIT_IGNORE_KEYWORDS,
    EDIT_DELAY,
    EDIT_REPEAT,
    EDIT_SCHEDULE_REMINDER,
    ADMIN_MSG_TYPE,
    ADMIN_MSG_TEXT,
    ADMIN_MSG_SCHEDULE,
    ADMIN_SB_EDIT_TEXT,
    ADMIN_SB_EDIT_SCHEDULE,
    STREAM_SCHEDULE_CONFIRM,
    STREAM_SCHEDULE_GAME,
    STREAM_SCHEDULE_TIME,
    STREAM_SCHEDULE_PUBLISH,
    STREAM_SCHEDULE_DURATION,
    SYNC_DAYS,
    PREMIUM_GATE,
    WATCH_PICK,
    WATCH_DELETE,
    WATCH_CATEGORIES,
    WATCH_TAGS,
    WATCH_VIEWERS,
    WATCH_LANGUAGE,
    WATCH_MATURE,
    WATCH_SAVE,
    DELETE_SIBLING_ALERTS,
    GLOBAL_IGNORE_KEYWORDS,
) = range(50)

_PENDING_IMPORT_TTL_SEC = 1800
_SYNC_PERIOD_MIN = 1
_SYNC_PERIOD_MAX = 365
_WATCH_MAX_CATS = 5
_WATCH_SUGGEST_N = 5
_WATCH_MAX_TAGS = 10
_SCHEDULE_DEFAULT_DURATION_MIN = 120

_STREAM_TIME_PATTERN = re.compile(r"^(\d{1,2}):(\d{2})$")
_WATCH_VIEWERS_RE = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+))?\s*$")
_WATCH_LANG_RE = re.compile(r"^[a-zA-Z]{2}$")


def _delay_current_label(minutes: int, lang: str) -> str:
    if minutes <= 0:
        return t("edit_delay_current_none", lang)
    return t("edit_delay_current", lang, minutes=minutes)


def _repeat_current_label(minutes: int, lang: str) -> str:
    if minutes <= 0:
        return t("edit_repeat_current_allow", lang)
    return t("edit_repeat_current_mute", lang, minutes=minutes)


def _schedule_reminder_current_label(minutes: int, lang: str) -> str:
    if minutes <= 0:
        return t("edit_schedule_reminder_current_off", lang)
    return t("edit_schedule_reminder_current", lang, minutes=minutes)


def _ignore_keywords_current_label(keywords: str, lang: str) -> str:
    if not keywords.strip():
        return t("ignore_keywords_current_none", lang)
    return keywords


def _effective_ignore_keywords(sub: Subscription, db: Database) -> str:
    if not sub.use_global_ignore:
        return sub.ignore_keywords
    return merge_ignore_keywords(
        sub.ignore_keywords, db.get_global_ignore_keywords(sub.owner_id)
    )


def _ignore_keywords_note(keywords: str, use_global: bool, lang: str) -> str:
    if keywords.strip() and use_global:
        return t("ignore_keywords_yes_global_note", lang, keywords=keywords)
    if keywords.strip():
        return t("ignore_keywords_yes_note", lang, keywords=keywords)
    if use_global:
        return t("ignore_keywords_global_only_note", lang)
    return t("ignore_keywords_no_note", lang)


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
    for index, sub in enumerate(_subs_for_owner(db, owner_id), 1):
        if sub.id == sub_id:
            return index
    return sub_id


def _subs_for_owner(db: Database, owner_id: int) -> list[Subscription]:
    """Real subs normally; only demo rows while Demo mode is on."""
    demo = demo_mode.is_active(owner_id)
    return [s for s in db.get_subscriptions_by_owner(owner_id) if bool(s.is_demo) == demo]


def _sub_in_current_mode(sub: Subscription, owner_id: int) -> bool:
    return bool(sub.is_demo) == demo_mode.is_active(owner_id)


def import_followed_as_subscriptions(
    db: Database,
    owner_id: int,
    followed: list[dict],
    *,
    template: str,
    limit: int,
    prune_missing: bool = False,
    enabled: bool = False,
    is_demo: bool = False,
) -> tuple[int, int, int, int, list[Subscription]]:
    """Create DM subscriptions from Helix followed channels.

    Import path keeps enabled=False (paused). Periodic sync passes enabled=True.
    New rows are marked from_twitch_sync=True. When prune_missing=True, sync-origin
    subs absent from follows are deleted (manual subs never touched).
    Returns (imported, skipped, limited, removed, new_subs).
    """
    existing_subs = [
        s for s in db.get_subscriptions_by_owner(owner_id) if s.is_demo is is_demo
    ]
    existing = {s.twitch_user_id for s in existing_subs}
    count = len(existing)
    imported = skipped = limited = 0
    new_subs: list[Subscription] = []
    seen_follow: set[str] = set()
    follow_ids: set[str] = set()
    for channel in followed:
        twitch_user_id = str(channel.get("broadcaster_id") or "")
        login = str(channel.get("broadcaster_login") or "").strip().lower()
        if not twitch_user_id or not login:
            continue
        if twitch_user_id in seen_follow:
            continue
        seen_follow.add(twitch_user_id)
        follow_ids.add(twitch_user_id)
        if twitch_user_id in existing:
            skipped += 1
            continue
        if count >= limit:
            limited += 1
            continue
        sub_id = db.add_subscription(
            owner_id=owner_id,
            twitch_username=login,
            twitch_user_id=twitch_user_id,
            message_template=template,
            dest_type="dm",
            chat_id=owner_id,
            thread_id=None,
            disable_link_preview=True,
            enabled=enabled,
            from_twitch_sync=True,
            is_demo=is_demo,
        )
        sub = db.get_subscription(sub_id, owner_id)
        if sub:
            new_subs.append(sub)
        existing.add(twitch_user_id)
        count += 1
        imported += 1
    removed = 0
    if prune_missing:
        removed = db.delete_synced_subscriptions_missing(owner_id, follow_ids)
    return imported, skipped, limited, removed, new_subs


LEGACY_IMPORT_TEMPLATES = frozenset(
    {
        "Streamer {username} went live with {game}",
        "Стример {username} вышел в эфир с игрой {game}",
    }
)


def migrate_import_sync_subscriptions(
    db: Database, *, dry_run: bool = False
) -> tuple[int, int]:
    """Refresh import/sync subs still on the legacy default template; disable link preview.

    Returns (templates_updated, preview_updated).
    """
    templates_updated = preview_updated = 0
    for owner_id in db.get_all_owner_ids():
        locale = db.get_user_locale(owner_id) or DEFAULT_LOCALE
        lang = "ru" if str(locale).lower().startswith("ru") else "en"
        new_template = t("import_default_template", lang)
        for sub in db.get_subscriptions_by_owner(owner_id):
            if not sub.from_twitch_sync:
                continue
            fields: dict[str, object] = {}
            if sub.message_template in LEGACY_IMPORT_TEMPLATES:
                fields["message_template"] = new_template
            if not sub.disable_link_preview:
                fields["disable_link_preview"] = True
            if not fields:
                continue
            if dry_run:
                if "message_template" in fields:
                    templates_updated += 1
                if "disable_link_preview" in fields:
                    preview_updated += 1
                continue
            if db.update_subscription(sub.id, owner_id, **fields):
                if "message_template" in fields:
                    templates_updated += 1
                if "disable_link_preview" in fields:
                    preview_updated += 1
    return templates_updated, preview_updated


def _import_result_keyboard(
    lang: str, subs: list[Subscription]
) -> InlineKeyboardMarkup:
    """Compact post-import keyboard: Enable all + unique channels, 2 per row."""
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(t("enable_all", lang), callback_data="enable_all")]
    ]
    seen: set[str] = set()
    unique: list[Subscription] = []
    for sub in subs:
        if sub.twitch_user_id in seen:
            continue
        seen.add(sub.twitch_user_id)
        unique.append(sub)
    row: list[InlineKeyboardButton] = []
    for i, sub in enumerate(unique, 1):
        row.append(
            InlineKeyboardButton(
                f"✏️ #{i} {sub.twitch_username}",
                callback_data=f"edit:{sub.id}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


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
    await _pulse_wizard_keyboard(
        context.bot, update.effective_user.id, lang, back=False
    )
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
    await _pulse_wizard_keyboard(
        context.bot, update.effective_user.id, lang, back=False
    )
    return STREAM_SCHEDULE_TIME


async def _finish_stream_schedule(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    user_id = update.effective_user.id
    entries: list[dict] = context.user_data.get("stream_schedule_entries", [])
    text = format_stream_schedule_result(entries, lang) if entries else "—"
    if update.callback_query:
        await update.callback_query.edit_message_text("✓")
        await context.bot.send_message(user_id, text)
    else:
        await update.effective_message.reply_text(text)
    if not entries:
        context.user_data.clear()
        await context.bot.send_message(
            user_id, t("menu_main", lang), reply_markup=_menu(lang, user_id)
        )
        return ConversationHandler.END
    await context.bot.send_message(
        user_id,
        t("stream_schedule_publish_prompt", lang),
        reply_markup=stream_schedule_publish_keyboard(lang),
    )
    return STREAM_SCHEDULE_PUBLISH


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
    return main_menu(
        lang,
        is_admin=_is_admin(user_id) and not demo_mode.is_active(user_id),
        demo_active=demo_mode.is_active(user_id),
    )


def _wizard(lang: str, *, back: bool = True) -> ReplyKeyboardMarkup:
    return wizard_menu(lang, back=back)


async def _pulse_wizard_keyboard(bot, chat_id: int, lang: str, *, back: bool = True) -> None:
    """Refresh Cancel/Back reply keyboard; carrier message is deleted immediately."""
    try:
        msg = await bot.send_message(
            chat_id, "·", reply_markup=_wizard(lang, back=back)
        )
        await bot.delete_message(chat_id, msg.message_id)
    except BadRequest:
        pass


async def _send_prompt_with_wizard_inline(
    bot,
    chat_id: int,
    text: str,
    lang: str,
    *,
    inline_markup: InlineKeyboardMarkup,
    back: bool = True,
    parse_mode: str | None = ParseMode.HTML,
    disable_web_page_preview: bool = False,
) -> None:
    """Send prompt with inline actions (incl. Back/Cancel). No extra carrier message."""
    _ = (lang, back)
    kwargs: dict = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": inline_markup,
    }
    if parse_mode is not None:
        kwargs["parse_mode"] = parse_mode
    if disable_web_page_preview:
        kwargs["disable_web_page_preview"] = True
    await bot.send_message(**kwargs)


def _render_sub_template(
    sub: Subscription,
    username: str,
    game: str = "",
    name: str = "",
    *,
    twitch: TwitchClient | None = None,
    stream: dict | None = None,
    extra: dict[str, str] | None = None,
) -> str:
    return render_template(
        sub.message_template,
        username,
        game,
        name,
        stream=stream,
        extra=extra,
        strip_name_mentions=bool(sub.strip_name_mentions),
        twitch=twitch,
    )


async def _prompt_repeat_step(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str, *, edit: bool = False
) -> int:
    db: Database = context.application.bot_data["db"]
    user_id = update.effective_user.id
    if not await prem.has_feature(context.bot, db, user_id, "repeat"):
        return await _show_premium_gate(
            update, context, feature="repeat", first_step=False
        )
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


async def _show_premium_gate(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    feature: str,
    first_step: bool,
) -> int:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    context.user_data["premium_gate_feature"] = feature
    context.user_data["premium_gate_first"] = first_step
    action = t(
        "premium_gate_action_cancel" if first_step else "premium_gate_action_skip",
        lang,
    )
    text = t("premium_gate", lang, action=action)
    markup = premium_gate_keyboard(lang, first_step=first_step)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)
    return PREMIUM_GATE


async def on_premium_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from premium_handlers import send_premium_screen

    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    action = query.data.split(":", 1)[1]
    feature = context.user_data.get("premium_gate_feature", "")
    first = bool(context.user_data.get("premium_gate_first"))
    db: Database = context.application.bot_data["db"]

    if action == "get":
        await query.edit_message_text("✓")
        await send_premium_screen(context.bot, user_id, lang, db)
        await context.bot.send_message(
            user_id,
            t("menu_settings", lang),
            reply_markup=settings_menu(lang),
        )
        context.user_data.clear()
        return ConversationHandler.END

    if action == "cancel" or (first and action == "skip"):
        await query.edit_message_text("✓")
        return await cancel(update, context)

    await query.edit_message_text("✓")
    if feature == "ignore_keywords":
        context.user_data["ignore_keywords"] = ""
        context.user_data["use_global_ignore"] = False
        return await _go_after_ignore_keywords(update, context, lang)
    if feature == "delay":
        context.user_data["delay_minutes"] = 0
        return await _continue_after_delay(update, context, lang)
    if feature == "repeat":
        context.user_data["suppress_repeat_minutes"] = 0
        return await _go_after_repeat(update, context, lang)
    if feature == "delete_old":
        context.user_data["delete_previous"] = False
        context.user_data["notify_delete_fail"] = False
        context.user_data["delete_other_alerts"] = False
        chat_id = context.user_data.get("pending_chat_id", user_id)
        thread_id = context.user_data.get("pending_thread_id")
        return await _finish_subscription(update, context, user_id, chat_id, thread_id)
    if feature == "delete_fail":
        context.user_data["notify_delete_fail"] = False
        chat_id = context.user_data.get("pending_chat_id", user_id)
        thread_id = context.user_data.get("pending_thread_id")
        return await _finish_subscription(update, context, user_id, chat_id, thread_id)
    if feature in ("active_limit", "sync", "alert_type"):
        return await cancel(update, context)
    return await _prompt_dest_step(update, context, lang)


async def _continue_after_delay(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    """After delay step: skip repeat mute for upcoming / stream-end / category-change."""
    context.user_data.setdefault("suppress_repeat_minutes", 0)
    if context.user_data.get("alert_type") in ("upcoming", "end", "category"):
        return await _go_after_repeat(update, context, lang)
    return await _prompt_repeat_step(update, context, lang)


async def _prompt_schedule_reminder_ask(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    text = t("schedule_reminder_prompt", lang)
    markup = schedule_reminder_keyboard(lang)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)
    context.user_data["schedule_reminder_offered"] = True
    _set_wizard_back(context, SCHEDULE_REMINDER_ASK)
    return SCHEDULE_REMINDER_ASK


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
    has_alert_type = bool(context.user_data.get("alert_type"))
    await update.effective_message.reply_text(
        t("new_sub_prompt", lang),
        reply_markup=_wizard(lang, back=has_alert_type),
    )
    _set_wizard_back(context, CHANNEL)
    return CHANNEL


async def _go_alert_type_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    chat_id = update.effective_user.id
    text = t("alert_type_prompt", lang)
    markup = alert_type_keyboard(lang)
    if update.callback_query:
        await context.bot.send_message(chat_id, text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)
    _set_wizard_back(context, ALERT_TYPE)
    return ALERT_TYPE


async def _go_template_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> int:
    display = (
        context.user_data.get("twitch_display_name")
        or context.user_data.get("twitch_username", "")
    )
    chat_id = update.effective_user.id
    context.user_data.setdefault("strip_name_mentions", False)
    strip_on = bool(context.user_data.get("strip_name_mentions"))
    text = t(
        "channel_found",
        lang,
        display_name=html.escape(display),
        placeholders_link=placeholders_link_html(lang),
    )
    await _send_prompt_with_wizard_inline(
        context.bot,
        chat_id,
        text,
        lang,
        inline_markup=template_strip_keyboard(
            lang,
            enabled=strip_on,
            show_lucky=True,
            show_back=True,
            show_cancel=True,
        ),
        back=True,
        disable_web_page_preview=True,
    )
    _set_wizard_back(context, TEMPLATE)
    return TEMPLATE


async def _go_image_ask_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> int:
    target = update.effective_message or update.callback_query.message
    chat_id = update.effective_user.id
    has_image = bool(
        context.user_data.get("edit_sub_id") and context.user_data.get("edit_has_image")
    )
    prompt = t("edit_image_prompt", lang) if has_image else t("image_ask", lang)
    markup = image_edit_keyboard(lang, has_image=has_image)
    if update.callback_query:
        await context.bot.send_message(
            chat_id,
            prompt,
            reply_markup=markup,
        )
    else:
        await target.reply_text(
            prompt,
            reply_markup=markup,
        )
    _set_wizard_back(context, IMAGE_ASK)
    return IMAGE_ASK


async def _go_ignore_keywords_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> int:
    db: Database = context.application.bot_data["db"]
    if not await prem.has_feature(context.bot, db, update.effective_user.id, "ignore_keywords"):
        return await _show_premium_gate(
            update, context, feature="ignore_keywords", first_step=False
        )
    context.user_data.setdefault("use_global_ignore", False)
    context.user_data["ignore_keywords_as_cancel"] = False
    await update.effective_message.reply_text(
        t("ignore_keywords_prompt", lang),
        parse_mode=ParseMode.HTML,
        reply_markup=ignore_keywords_keyboard(
            lang,
            use_global=bool(context.user_data.get("use_global_ignore")),
        ),
    )
    await _pulse_wizard_keyboard(
        context.bot, update.effective_user.id, lang, back=True
    )
    _set_wizard_back(context, IGNORE_KEYWORDS)
    return IGNORE_KEYWORDS


async def _go_link_preview_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> int:
    chat_id = update.effective_user.id
    text = t("link_preview_prompt", lang)
    markup = link_preview_keyboard(lang)
    if update.callback_query:
        await context.bot.send_message(chat_id, text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)
    _set_wizard_back(context, LINK_PREVIEW)
    return LINK_PREVIEW


async def _go_delay_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> int:
    db: Database = context.application.bot_data["db"]
    if not await prem.has_feature(context.bot, db, update.effective_user.id, "delay"):
        return await _show_premium_gate(
            update, context, feature="delay", first_step=False
        )
    chat_id = update.effective_user.id
    text = t("delay_prompt", lang)
    markup = delay_keyboard(lang)
    if update.callback_query:
        await context.bot.send_message(chat_id, text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)
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


async def _go_schedule_reminder_ask(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    return await _prompt_schedule_reminder_ask(update, context, lang)


async def _go_schedule_reminder_minutes(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    await update.effective_message.reply_text(
        t("schedule_reminder_minutes_prompt", lang),
        reply_markup=_wizard(lang),
    )
    _set_wizard_back(context, SCHEDULE_REMINDER_MINUTES)
    return SCHEDULE_REMINDER_MINUTES


async def _go_after_repeat(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    """After repeat step: offer schedule reminders if Twitch schedule exists."""
    context.user_data.setdefault("schedule_reminder_minutes", 0)
    context.user_data.setdefault("schedule_reminder_configured", False)
    context.user_data.pop("schedule_reminder_offered", None)
    if context.user_data.get("skip_schedule_check"):
        _set_wizard_back(context, DEST_TYPE)
        return await _prompt_dest_step(update, context, lang)
    if context.user_data.get("alert_type") == "upcoming":
        context.user_data["notify_on_live"] = False
        context.user_data["notify_on_end"] = False
        return await _go_schedule_reminder_minutes(update, context, lang)
    twitch: TwitchClient = context.application.bot_data["twitch"]
    uid = str(context.user_data.get("twitch_user_id") or "")
    has_schedule = False
    if uid:
        try:
            has_schedule = await asyncio.to_thread(twitch.has_channel_schedule, uid)
        except Exception:
            logger.exception("Twitch schedule check failed for %s", uid)
    if not has_schedule:
        _set_wizard_back(context, DEST_TYPE)
        return await _prompt_dest_step(update, context, lang)
    return await _prompt_schedule_reminder_ask(update, context, lang)


async def _go_dest_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> int:
    return await _prompt_dest_step(update, context, lang)


async def wizard_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = _user_lang(context, update.effective_user.id)
    state = context.user_data.get("wizard_back_state")
    if state == CHANNEL:
        context.user_data.pop("twitch_username", None)
        context.user_data.pop("twitch_user_id", None)
        context.user_data.pop("twitch_display_name", None)
        context.user_data.pop("channel_input_was_url", None)
        return await _go_alert_type_prompt(update, context, lang)
    if state == TEMPLATE:
        context.user_data.pop("pending_template", None)
        context.user_data.pop("pending_template_preview_disabled", None)
        return await _go_channel_prompt(update, context, lang)
    if state == CHANNEL_DUP:
        return await _go_channel_prompt(update, context, lang)
    if state == IMAGE_ASK:
        if context.user_data.get("edit_sub_id"):
            owner_id = update.effective_user.id
            context.user_data.clear()
            await update.effective_message.reply_text(
                t("cancelled", lang),
                reply_markup=_menu(lang, owner_id),
            )
            return ConversationHandler.END
        return await _go_template_prompt(update, context, lang)
    if state == IMAGE_UPLOAD:
        return await _go_image_ask_prompt(update, context, lang)
    if state == IMAGE_POSITION:
        await update.effective_message.reply_text(
            t("image_send_prompt", lang),
            reply_markup=_wizard(lang, back=not bool(context.user_data.get("edit_sub_id"))),
        )
        _set_wizard_back(context, IMAGE_UPLOAD)
        return IMAGE_UPLOAD
    if state == LUCKY_PREVIEW:
        return await _go_template_prompt(update, context, lang)
    if state == IGNORE_KEYWORDS:
        return await _go_image_ask_prompt(update, context, lang)
    if state == LINK_PREVIEW:
        return await _go_ignore_keywords_prompt(update, context, lang)
    if state == DELAY_SEND:
        if context.user_data.get("image_file_id") or not template_has_link(
            str(context.user_data.get("message_template") or "")
        ):
            return await _go_ignore_keywords_prompt(update, context, lang)
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
    if state == SCHEDULE_REMINDER_ASK:
        return await _go_repeat_prompt(update, context, lang)
    if state == SCHEDULE_REMINDER_MINUTES:
        if context.user_data.get("alert_type") == "upcoming":
            # Upcoming skips delay/repeat; back to preview or ignore.
            if context.user_data.get("image_file_id") or not template_has_link(
                str(context.user_data.get("message_template") or "")
            ):
                return await _go_ignore_keywords_prompt(update, context, lang)
            return await _go_link_preview_prompt(update, context, lang)
        return await _go_schedule_reminder_ask(update, context, lang)
    if state == DEST_TYPE:
        if context.user_data.get("lucky_quick"):
            return await _show_lucky_preview(update, context, lang)
        if context.user_data.get("alert_type") == "upcoming":
            return await _go_schedule_reminder_minutes(update, context, lang)
        if context.user_data.get("alert_type") in ("end", "category"):
            after = context.user_data.get("after_delay_state", DELAY_SEND)
            if after == DELAY_MINUTES:
                return await _go_delay_minutes_prompt(update, context, lang)
            return await _go_delay_prompt(update, context, lang)
        if context.user_data.get("schedule_reminder_offered"):
            if int(context.user_data.get("schedule_reminder_minutes", 0)) > 0:
                return await _go_schedule_reminder_minutes(update, context, lang)
            return await _go_schedule_reminder_ask(update, context, lang)
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
    if state == DELETE_SIBLING_ALERTS:
        await update.effective_message.reply_text(
            _delete_old_prompt_text(context, lang),
            reply_markup=delete_old_keyboard(lang),
        )
        _set_wizard_back(context, DELETE_OLD)
        return DELETE_OLD
    if state == DELETE_FAIL_NOTIFY:
        if context.user_data.get("delete_sibling_asked"):
            await update.effective_message.reply_text(
                t("delete_sibling_text", lang),
                reply_markup=delete_sibling_keyboard(lang),
            )
            _set_wizard_back(context, DELETE_SIBLING_ALERTS)
            return DELETE_SIBLING_ALERTS
        await update.effective_message.reply_text(
            _delete_old_prompt_text(context, lang),
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
    if state == WATCH_DELETE:
        return await _go_watch_pick_prompt(update, context, lang)
    if state == WATCH_TAGS:
        return await _go_watch_categories_prompt(update, context, lang)
    if state == WATCH_VIEWERS:
        return await _go_watch_tags_prompt(update, context, lang)
    if state == WATCH_LANGUAGE:
        return await _go_watch_viewers_prompt(update, context, lang)
    if state == WATCH_MATURE:
        return await _go_watch_language_prompt(update, context, lang)
    if state == WATCH_SAVE:
        return await _go_watch_mature_prompt(update, context, lang)
    if context.user_data.get("sb_edit_mode") in ("text", "schedule"):
        context.user_data.clear()
        await update.effective_message.reply_text(
            t("menu_broadcast", lang),
            reply_markup=broadcast_menu(lang),
        )
        return ConversationHandler.END
    return ConversationHandler.END


def _set_wizard_back(context: ContextTypes.DEFAULT_TYPE, state: int) -> None:
    context.user_data["wizard_back_state"] = state


def _btn_filter(key: str) -> filters.Regex:
    texts = "|".join(re.escape(btn(key, loc)) for loc in SUPPORTED_LOCALES)
    return filters.Regex(f"^({texts})$")


def _is_admin(user_id: int) -> bool:
    from config import ADMIN_USER_IDS

    return user_id in ADMIN_USER_IDS


def _can_use_admin_tools(user_id: int) -> bool:
    return _is_admin(user_id) and not demo_mode.is_active(user_id)


def _user_lang(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str:
    db: Database = context.application.bot_data["db"]
    return db.get_user_locale(user_id) or DEFAULT_LOCALE


def _help_text(lang: str) -> str:
    return t(
        "help",
        lang,
        btn_new=btn("new", lang),
        btn_watch=btn("watch", lang),
        btn_import_twitch=btn("import_twitch", lang),
        btn_manage=btn("manage", lang),
        btn_feedback=btn("feedback", lang),
        btn_settings=btn("settings", lang),
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
    # Order matches create wizard: image → ignore → preview → delay → repeat
    # → schedule reminder → dest → delete.
    status = "✅" if sub.enabled else "⏸"
    chat_label = chat_display if chat_display is not None else str(sub.chat_id)
    settings: list[str] = []
    if sub.notify_on_end:
        settings.append(t("sub_list_alert_end", lang))
    elif sub.notify_on_category_change:
        settings.append(t("sub_list_alert_category", lang))
    elif sub.schedule_reminder_minutes > 0 and not sub.notify_on_live:
        settings.append(t("sub_list_alert_upcoming", lang))
    else:
        settings.append(t("sub_list_alert_live", lang))
    if sub.image_file_id:
        pos = (sub.image_position or "").strip()
        if pos == "after":
            settings.append(t("sub_list_image_after", lang))
        else:
            settings.append(t("sub_list_image_before", lang))
    else:
        settings.append(t("sub_list_image_no", lang))
    if sub.ignore_keywords.strip() and sub.use_global_ignore:
        settings.append(
            t("sub_list_ignore_yes_global", lang, keywords=sub.ignore_keywords)
        )
    elif sub.ignore_keywords.strip():
        settings.append(
            t("sub_list_ignore_yes", lang, keywords=sub.ignore_keywords)
        )
    elif sub.use_global_ignore:
        settings.append(t("sub_list_ignore_global_only", lang))
    else:
        settings.append(t("sub_list_ignore_no", lang))
    if sub.image_file_id or template_has_link(sub.message_template or ""):
        settings.append(
            t("sub_list_preview_off", lang)
            if sub.disable_link_preview or sub.image_file_id
            else t("sub_list_preview_on", lang)
        )
    is_upcoming = (
        sub.schedule_reminder_minutes > 0
        and not sub.notify_on_live
        and not sub.notify_on_end
        and not sub.notify_on_category_change
    )
    if not is_upcoming:
        settings.append(
            t("sub_list_delay", lang, minutes=sub.delay_minutes)
            if sub.delay_minutes > 0
            else t("sub_list_delay_none", lang)
        )
        if not sub.notify_on_category_change and not sub.notify_on_end:
            settings.append(
                t("sub_list_repeat_mute", lang, minutes=sub.suppress_repeat_minutes)
                if sub.suppress_repeat_minutes > 0
                else t("sub_list_repeat_allow", lang)
            )
    if sub.schedule_reminder_configured:
        settings.append(
            t("sub_list_schedule_reminder", lang, minutes=sub.schedule_reminder_minutes)
            if sub.schedule_reminder_minutes > 0
            else t("sub_list_schedule_reminder_none", lang)
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
        if sub.delete_previous and sub.notify_on_category_change:
            settings.append(
                t("sub_list_delete_other_yes", lang)
                if sub.delete_other_alerts
                else t("sub_list_delete_other_no", lang)
            )
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
    except Exception:
        logger.exception("Unexpected error resolving chat name for %s", sub.chat_id)
    return str(sub.chat_id)


def _inline_btn_label(text: str, *, limit: int = 64) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _split_telegram_text(text: str, *, limit: int = 4096) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while rest:
        if len(rest) <= limit:
            chunks.append(rest)
            break
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    return chunks


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


async def _deliver_alert_content(
    bot,
    *,
    chat_id: int,
    text: str,
    thread_id: int | None = None,
    image_file_id: str | None = None,
    image_position: str = "",
    disable_link_preview: bool = False,
):
    """Send alert text, optionally with image above/below. Returns the primary message."""
    thread_kwargs: dict = {}
    if thread_id:
        thread_kwargs["message_thread_id"] = thread_id

    file_id = image_file_id
    position = (image_position or "").strip()
    # Image posts always disable link preview (caption has no separate preview toggle).
    if file_id and position in ("before", "after"):
        disable_link_preview = True

    if file_id and position in ("before", "after") and len(text) <= _TELEGRAM_CAPTION_LIMIT:
        return await bot.send_photo(
            chat_id=chat_id,
            photo=file_id,
            caption=text,
            show_caption_above_media=(position == "after"),
            **thread_kwargs,
        )
    if file_id and position in ("before", "after"):
        if position == "before":
            await bot.send_photo(chat_id=chat_id, photo=file_id, **thread_kwargs)
            text_kwargs: dict = {"chat_id": chat_id, "text": text, **thread_kwargs}
            if disable_link_preview:
                text_kwargs["disable_web_page_preview"] = True
            return await bot.send_message(**text_kwargs)
        text_kwargs = {"chat_id": chat_id, "text": text, **thread_kwargs}
        if disable_link_preview:
            text_kwargs["disable_web_page_preview"] = True
        msg = await bot.send_message(**text_kwargs)
        await bot.send_photo(chat_id=chat_id, photo=file_id, **thread_kwargs)
        return msg

    kwargs: dict = {"chat_id": chat_id, "text": text, **thread_kwargs}
    if disable_link_preview:
        kwargs["disable_web_page_preview"] = True
    return await bot.send_message(**kwargs)


async def _send_notification(
    bot,
    db: Database,
    sub: Subscription,
    text: str,
    *,
    alert_type: str = "live",
) -> bool:
    if sub.delete_previous and sub.dest_type != "dm":
        to_delete: list[tuple[int, int]] = []
        seen_msg: set[int] = set()
        if sub.last_message_id and sub.last_message_id not in seen_msg:
            to_delete.append((sub.id, sub.last_message_id))
            seen_msg.add(sub.last_message_id)
        if sub.delete_other_alerts:
            for sibling in db.get_enabled_by_twitch_user_id(sub.twitch_user_id):
                if sibling.id == sub.id:
                    continue
                if sibling.owner_id != sub.owner_id:
                    continue
                if sibling.chat_id != sub.chat_id:
                    continue
                if (sibling.thread_id or None) != (sub.thread_id or None):
                    continue
                if not sibling.last_message_id or sibling.last_message_id in seen_msg:
                    continue
                to_delete.append((sibling.id, sibling.last_message_id))
                seen_msg.add(sibling.last_message_id)
        for owner_sub_id, message_id in to_delete:
            try:
                await bot.delete_message(chat_id=sub.chat_id, message_id=message_id)
                if owner_sub_id != sub.id:
                    db.set_last_message_id(owner_sub_id, None)
            except (BadRequest, Forbidden) as exc:
                logger.warning(
                    "Cannot delete message %s in %s: %s",
                    message_id,
                    sub.chat_id,
                    exc,
                )
                if sub.notify_delete_fail:
                    lang = db.get_user_locale(sub.owner_id) or DEFAULT_LOCALE
                    link = _message_link(sub.chat_id, message_id, sub.thread_id)
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

    try:
        msg = await _deliver_alert_content(
            bot,
            chat_id=sub.chat_id,
            text=text,
            thread_id=sub.thread_id,
            image_file_id=sub.image_file_id,
            image_position=sub.image_position,
            disable_link_preview=bool(sub.disable_link_preview) or bool(sub.image_file_id),
        )
    except RetryAfter as exc:
        await asyncio.sleep(float(exc.retry_after) + 0.5)
        try:
            msg = await _deliver_alert_content(
                bot,
                chat_id=sub.chat_id,
                text=text,
                thread_id=sub.thread_id,
                image_file_id=sub.image_file_id,
                image_position=sub.image_position,
                disable_link_preview=bool(sub.disable_link_preview)
                or bool(sub.image_file_id),
            )
        except (BadRequest, Forbidden, RetryAfter) as retry_exc:
            logger.warning("Cannot send to %s after RetryAfter: %s", sub.chat_id, retry_exc)
            return False
    except (BadRequest, Forbidden) as exc:
        logger.warning("Cannot send to %s: %s", sub.chat_id, exc)
        return False

    if msg and sub.delete_previous and sub.dest_type != "dm":
        db.set_last_message_id(sub.id, msg.message_id)
    if sub.suppress_repeat_minutes > 0:
        db.set_notify_cooldown(sub.id, sub.suppress_repeat_minutes)
    # History is for the user's DM inbox only — skip channel/group destinations.
    if sub.dest_type == "dm":
        try:
            db.add_alert_history(
                sub.owner_id,
                subscription_id=sub.id,
                twitch_username=sub.twitch_username,
                alert_type=alert_type,
                message_text=text,
            )
        except Exception:
            logger.exception("Failed to record alert history for sub %s", sub.id)
    return True


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


async def _send_welcome(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    user_id = update.effective_user.id
    await update.effective_message.reply_text(
        t("start_welcome", lang),
        reply_markup=_menu(lang, user_id),
    )
    return ConversationHandler.END


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    db: Database = context.application.bot_data["db"]
    user_id = update.effective_user.id
    db.upsert_user(user_id)
    _apply_referral_start_arg(db, user_id, context.args)
    lang = db.get_user_locale(user_id)
    if not lang:
        context.user_data["after_lang"] = "welcome"
        return await _prompt_language(update)
    return await _send_welcome(update, context, lang)


def _apply_referral_start_arg(db: Database, user_id: int, args: list[str] | None) -> None:
    if not args:
        return
    raw = (args[0] or "").strip()
    if not raw.startswith("ref_"):
        return
    try:
        referrer_id = int(raw[4:])
    except ValueError:
        return
    db.set_referred_by(user_id, referrer_id)


async def receive_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = query.data.split(":", 1)[1]
    if lang not in SUPPORTED_LOCALES:
        lang = DEFAULT_LOCALE
    db: Database = context.application.bot_data["db"]
    db.set_user_locale(query.from_user.id, lang)
    await query.edit_message_text(t("lang_set", lang))
    after = context.user_data.pop("after_lang", "welcome")
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
    await context.bot.send_message(
        query.from_user.id,
        t("start_welcome", lang),
        reply_markup=_menu(lang, query.from_user.id),
    )
    return ConversationHandler.END


async def start_new_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    from config import MAX_SUBSCRIPTIONS_PER_OWNER

    if len(_subs_for_owner(db, user_id)) >= MAX_SUBSCRIPTIONS_PER_OWNER:
        await update.effective_message.reply_text(
            t("sub_limit", lang, limit=MAX_SUBSCRIPTIONS_PER_OWNER),
            reply_markup=_menu(lang, user_id),
        )
        return ConversationHandler.END
    if not await prem.can_enable_more_async(context.bot, db, user_id):
        # Still allow creating paused alerts; warn via gate only when enabling.
        pass
    return await _go_alert_type_prompt(update, context, lang)


async def receive_alert_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    kind = query.data.split(":", 1)[1]
    if kind not in ("live", "category", "upcoming", "end"):
        return ALERT_TYPE
    db: Database = context.application.bot_data["db"]
    if kind != "live" and not await prem.has_feature(
        context.bot, db, query.from_user.id, "alert_types"
    ):
        context.user_data["alert_type"] = kind
        return await _show_premium_gate(
            update, context, feature="alert_type", first_step=True
        )
    context.user_data["alert_type"] = kind
    context.user_data["notify_on_end"] = kind == "end"
    context.user_data["notify_on_category_change"] = kind == "category"
    context.user_data["delete_other_alerts"] = False
    if kind in ("live", "end", "category"):
        context.user_data["skip_schedule_check"] = True
        context.user_data["notify_on_live"] = kind == "live"
        if kind in ("end", "category"):
            context.user_data["notify_on_live"] = False
    else:
        context.user_data.pop("skip_schedule_check", None)
        context.user_data["notify_on_live"] = False
        context.user_data["notify_on_end"] = False
    await query.edit_message_text("✓")
    return await _go_channel_prompt(update, context, lang)


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
    context.user_data["twitch_display_name"] = user.get("display_name") or user["login"]
    context.user_data["channel_input_was_url"] = twitch.is_twitch_url(text)

    if context.user_data.get("alert_type") == "upcoming":
        has_schedule = False
        try:
            has_schedule = await asyncio.to_thread(
                twitch.has_channel_schedule, user["id"]
            )
        except Exception:
            logger.exception("Twitch schedule check failed for %s", user["id"])
        if not has_schedule:
            await update.effective_message.reply_text(t("alert_type_no_schedule", lang))
            return CHANNEL

    db: Database = context.application.bot_data["db"]
    owner_id = update.effective_user.id
    existing = next(
        (
            s
            for s in _subs_for_owner(db, owner_id)
            if s.twitch_user_id == user["id"]
        ),
        None,
    )
    if existing:
        await update.effective_message.reply_text(
            t("channel_dup_prompt", lang),
            reply_markup=channel_dup_keyboard(lang, existing.id),
        )
        context.user_data["dup_sub_id"] = existing.id
        _set_wizard_back(context, CHANNEL_DUP)
        return CHANNEL_DUP

    return await _go_template_prompt(update, context, lang)


async def receive_channel_dup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    parts = query.data.split(":")
    if parts[1] == "edit":
        sub_id = int(parts[2])
        db: Database = context.application.bot_data["db"]
        sub = db.get_subscription(sub_id, query.from_user.id)
        context.user_data.clear()
        if not sub:
            await query.edit_message_text(t("sub_not_found", lang))
            return ConversationHandler.END
        sub_num = _owner_sub_number(db, query.from_user.id, sub_id)
        await query.edit_message_text(
            t("edit_menu", lang, sub_id=sub_num, username=sub.twitch_username),
            reply_markup=_edit_options_for_sub(sub, lang),
        )
        await context.bot.send_message(
            query.from_user.id,
            t("menu_subs", lang),
            reply_markup=_menu(lang, query.from_user.id),
        )
        return ConversationHandler.END

    await query.edit_message_text("✓")
    return await _go_template_prompt(update, context, lang)


async def receive_template(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = _user_lang(context, update.effective_user.id)
    template = (update.effective_message.text or "").strip()
    if template in all_menu_buttons():
        await update.effective_message.reply_text(t("finish_setup_first", lang))
        return TEMPLATE
    if not template:
        await update.effective_message.reply_text(t("template_empty", lang))
        return TEMPLATE

    if await _offer_template_typo_fix(update, context, lang, template):
        return TEMPLATE_TYPO_CONFIRM

    context.user_data["message_template"] = template
    context.user_data.pop("lucky_quick", None)
    return await _go_image_ask_prompt(update, context, lang)


async def _offer_template_typo_fix(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    lang: str,
    template: str,
) -> bool:
    typos = find_placeholder_typos(template)
    if not typos:
        return False
    lines = "\n".join(
        t(
            "template_typo_item",
            lang,
            found=html.escape(found),
            suggested=html.escape(suggested),
        )
        for found, suggested in typos
    )
    context.user_data["pending_template"] = template
    await update.effective_message.reply_text(
        t("template_typo_prompt", lang, typos=lines),
        parse_mode=ParseMode.HTML,
        reply_markup=template_typo_keyboard(lang),
    )
    return True


async def receive_template_typo_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    fix = query.data.endswith(":1")
    template = context.user_data.pop("pending_template", "")
    is_edit = bool(context.user_data.get("edit_sub_id"))

    if fix:
        await query.edit_message_text("✓")
        if is_edit:
            db: Database = context.application.bot_data["db"]
            sub = db.get_subscription(context.user_data["edit_sub_id"], query.from_user.id)
            if not sub:
                await context.bot.send_message(
                    query.from_user.id, t("sub_not_found", lang)
                )
                context.user_data.clear()
                return ConversationHandler.END
            sub_num = _owner_sub_number(db, query.from_user.id, sub.id)
            await _prompt_edit_template(
                bot=context.bot,
                user_id=query.from_user.id,
                lang=lang,
                sub=sub,
                sub_num=sub_num,
            )
            return EDIT_TEMPLATE
        await _send_prompt_with_wizard_inline(
            context.bot,
            query.from_user.id,
            t(
                "template_typo_resend",
                lang,
                placeholders_link=placeholders_link_html(lang),
            ),
            lang,
            inline_markup=template_strip_keyboard(
                lang,
                enabled=bool(context.user_data.get("strip_name_mentions")),
                show_lucky=True,
                show_back=True,
                show_cancel=True,
            ),
            back=True,
            disable_web_page_preview=True,
        )
        _set_wizard_back(context, TEMPLATE)
        return TEMPLATE

    await query.edit_message_text("✓")
    if is_edit:
        return await _save_edit_template(update, context, lang, template)

    context.user_data["message_template"] = template
    context.user_data.pop("lucky_quick", None)
    return await _go_image_ask_prompt(update, context, lang)


async def _show_lucky_preview(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    template = context.user_data.get("message_template") or ""
    username = context.user_data.get("twitch_username") or "username"
    twitch: TwitchClient = context.application.bot_data["twitch"]
    game = await asyncio.to_thread(twitch.random_igdb_game_name)
    stream_title = preview_stream_title(lang, game)
    preview = render_template(template, username, game, stream_title)
    text = t(
        "lucky_preview",
        lang,
        template=html.escape(template),
        preview=html.escape(preview),
    )
    markup = lucky_preview_keyboard(lang)
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=markup
            )
        except BadRequest:
            await context.bot.send_message(
                update.effective_user.id,
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
    else:
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup
        )
    _set_wizard_back(context, LUCKY_PREVIEW)
    return LUCKY_PREVIEW


async def lucky_generate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    await query.edit_message_text(t("lucky_generating", lang))
    channel = str(context.user_data.get("twitch_username") or "")
    db: Database = context.application.bot_data["db"]
    try:
        template = await asyncio.to_thread(
            generate_alert_template, locale=lang, channel=channel, store=db
        )
    except Exception:
        logger.exception("Lucky template generation failed")
        await _send_prompt_with_wizard_inline(
            context.bot,
            query.from_user.id,
            t("lucky_failed", lang),
            lang,
            inline_markup=template_strip_keyboard(
                lang,
                enabled=bool(context.user_data.get("strip_name_mentions")),
                show_lucky=True,
                show_back=True,
                show_cancel=True,
            ),
            back=True,
            parse_mode=None,
        )
        _set_wizard_back(context, TEMPLATE)
        return TEMPLATE

    context.user_data["message_template"] = template
    return await _show_lucky_preview(update, context, lang)


async def lucky_continue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    if not context.user_data.get("message_template"):
        await query.edit_message_text(t("template_empty", lang))
        return TEMPLATE
    context.user_data["lucky_quick"] = True
    context.user_data.setdefault("ignore_keywords", "")
    context.user_data.setdefault("use_global_ignore", False)
    # Link preview on only when the channel was entered as a Twitch URL.
    context.user_data["disable_link_preview"] = not bool(
        context.user_data.get("channel_input_was_url")
    )
    context.user_data.setdefault("delay_minutes", 0)
    context.user_data.setdefault("suppress_repeat_minutes", 0)
    context.user_data.setdefault("schedule_reminder_minutes", 0)
    context.user_data.setdefault("schedule_reminder_configured", False)
    context.user_data.pop("image_file_id", None)
    context.user_data["image_position"] = ""
    await query.edit_message_text("✓")
    if context.user_data.get("alert_type") == "upcoming":
        context.user_data["notify_on_live"] = False
        context.user_data["notify_on_end"] = False
        return await _go_schedule_reminder_minutes(update, context, lang)
    return await _prompt_dest_step(update, context, lang)


async def lucky_full_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    context.user_data.pop("lucky_quick", None)
    context.user_data.pop("message_template", None)
    context.user_data.pop("image_file_id", None)
    context.user_data["image_position"] = ""
    await query.edit_message_text("✓")
    return await _go_template_prompt(update, context, lang)


async def receive_image_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    action = query.data.split(":", 1)[1]
    is_edit = bool(context.user_data.get("edit_sub_id"))

    if action == "keep":
        await query.edit_message_text("✓")
        owner_id = query.from_user.id
        context.user_data.clear()
        await context.bot.send_message(
            owner_id,
            "✓",
            reply_markup=_menu(lang, owner_id),
        )
        return ConversationHandler.END

    if action == "delete":
        context.user_data["image_file_id"] = None
        context.user_data["image_position"] = ""
        await query.edit_message_text("✓")
        if is_edit:
            return await _save_edit_image(update, context, lang)
        return await _go_ignore_keywords_prompt(update, context, lang)

    if action == "skip":
        context.user_data["image_file_id"] = None
        context.user_data["image_position"] = ""
        await query.edit_message_text("✓")
        if is_edit:
            return await _save_edit_image(update, context, lang)
        return await _go_ignore_keywords_prompt(update, context, lang)

    await query.edit_message_text("✓")
    await context.bot.send_message(
        query.from_user.id,
        t("image_send_prompt", lang),
        reply_markup=_wizard(lang, back=not is_edit),
    )
    _set_wizard_back(context, IMAGE_UPLOAD)
    return IMAGE_UPLOAD


async def receive_image_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = _user_lang(context, update.effective_user.id)
    message = update.effective_message
    photo = message.photo
    if not photo:
        await message.reply_text(t("image_need_photo", lang))
        return IMAGE_UPLOAD
    context.user_data["image_file_id"] = photo[-1].file_id
    await message.reply_text(
        t("image_position_prompt", lang),
        reply_markup=image_position_keyboard(lang),
    )
    _set_wizard_back(context, IMAGE_POSITION)
    return IMAGE_POSITION


async def receive_image_position(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    position = query.data.split(":", 1)[1]
    if position not in ("before", "after"):
        return IMAGE_POSITION
    context.user_data["image_position"] = position
    await query.edit_message_text("✓")
    if context.user_data.get("edit_sub_id"):
        return await _save_edit_image(update, context, lang)
    return await _go_ignore_keywords_prompt(update, context, lang)


async def _save_edit_image(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    sub_id = context.user_data.get("edit_sub_id")
    if not sub_id:
        return ConversationHandler.END
    db: Database = context.application.bot_data["db"]
    owner_id = update.effective_user.id
    sub_num = _owner_sub_number(db, owner_id, sub_id)
    file_id = context.user_data.get("image_file_id") or None
    position = str(context.user_data.get("image_position") or "") if file_id else ""
    fields: dict = {
        "image_file_id": file_id,
        "image_position": position,
    }
    if file_id:
        fields["disable_link_preview"] = True
    if not db.update_subscription(sub_id, owner_id, **fields):
        await context.bot.send_message(owner_id, t("sub_not_found", lang))
    else:
        await context.bot.send_message(
            owner_id,
            t("edit_updated", lang, sub_id=sub_num),
            reply_markup=_menu(lang, owner_id),
        )
    context.user_data.clear()
    return ConversationHandler.END


async def start_edit_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    sub_id = int(query.data.split(":")[1])
    db: Database = context.application.bot_data["db"]
    sub = db.get_subscription(sub_id, query.from_user.id)
    if not sub:
        await query.edit_message_text(t("sub_not_found", lang))
        return ConversationHandler.END
    has_image = bool(sub.image_file_id)
    context.user_data["edit_sub_id"] = sub_id
    context.user_data["wizard_edit"] = True
    context.user_data["edit_has_image"] = has_image
    if has_image:
        context.user_data["image_file_id"] = sub.image_file_id
        context.user_data["image_position"] = sub.image_position or ""
    await query.edit_message_text("✓")
    await context.bot.send_message(
        query.from_user.id,
        t("edit_image_prompt", lang) if has_image else t("image_ask", lang),
        reply_markup=image_edit_keyboard(lang, has_image=has_image),
    )
    return IMAGE_ASK


async def delete_edit_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    sub_id = int(query.data.split(":")[1])
    db: Database = context.application.bot_data["db"]
    owner_id = query.from_user.id
    if not db.update_subscription(
        sub_id,
        owner_id,
        image_file_id=None,
        image_position="",
    ):
        await query.edit_message_text(t("sub_not_found", lang))
        return ConversationHandler.END
    sub_num = _owner_sub_number(db, owner_id, sub_id)
    await query.edit_message_text("✓")
    await context.bot.send_message(
        owner_id,
        t("edit_updated", lang, sub_id=sub_num),
        reply_markup=_menu(lang, owner_id),
    )
    context.user_data.clear()
    return ConversationHandler.END


async def _save_edit_template(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    lang: str,
    template: str,
) -> int:
    sub_id = context.user_data.get("edit_sub_id")
    if not sub_id:
        return ConversationHandler.END

    db: Database = context.application.bot_data["db"]
    owner_id = update.effective_user.id
    sub_num = _owner_sub_number(db, owner_id, sub_id)
    preview_disabled = context.user_data.pop("pending_template_preview_disabled", None)
    if preview_disabled is None and update.effective_message:
        preview_disabled = _is_link_preview_disabled(update.effective_message)
    if preview_disabled is None:
        preview_disabled = False
    fields: dict[str, object] = {
        "message_template": template,
        "disable_link_preview": bool(preview_disabled),
    }
    if "strip_name_mentions" in context.user_data:
        fields["strip_name_mentions"] = bool(context.user_data.get("strip_name_mentions"))
    if not db.update_subscription(sub_id, owner_id, **fields):
        await context.bot.send_message(owner_id, t("sub_not_found", lang))
    else:
        await context.bot.send_message(
            owner_id,
            t("edit_updated", lang, sub_id=sub_num),
            reply_markup=_menu(lang, owner_id),
        )
    context.user_data.clear()
    return ConversationHandler.END


async def _prompt_edit_template(
    *,
    bot,
    user_id: int,
    lang: str,
    sub: Subscription,
    sub_num: int,
    reply_markup=None,
    strip_name_mentions: bool | None = None,
) -> None:
    preview = render_template(
        sub.message_template,
        sub.twitch_username,
        "Just Chatting",
        t("preview_stream", lang),
    )
    strip_on = (
        bool(strip_name_mentions)
        if strip_name_mentions is not None
        else bool(sub.strip_name_mentions)
    )
    kb = (
        reply_markup
        if reply_markup is not None
        else template_strip_keyboard(
            lang, enabled=strip_on, show_cancel=True
        )
    )
    await _send_prompt_with_wizard_inline(
        bot,
        user_id,
        t(
            "edit_template_prompt",
            lang,
            sub_id=sub_num,
            placeholders_link=placeholders_link_html(lang),
            current=html.escape(sub.message_template or ""),
            preview=html.escape(preview),
        ),
        lang,
        inline_markup=kb,
        back=False,
        disable_web_page_preview=True,
    )


async def receive_strip_name_toggle(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    if query.data.endswith(":cancel"):
        return await cancel(update, context)
    if query.data.endswith(":back"):
        return await wizard_back(update, context)
    lang = _user_lang(context, query.from_user.id)
    enabled = not bool(context.user_data.get("strip_name_mentions"))
    context.user_data["strip_name_mentions"] = enabled
    editing = bool(context.user_data.get("edit_sub_id"))
    if editing:
        db: Database = context.application.bot_data["db"]
        owner_id = query.from_user.id
        sub_id = int(context.user_data["edit_sub_id"])
        db.update_subscription(sub_id, owner_id, strip_name_mentions=enabled)
    try:
        await query.edit_message_reply_markup(
            reply_markup=template_strip_keyboard(
                lang,
                enabled=enabled,
                show_lucky=not editing,
                show_back=not editing,
                show_cancel=True,
            )
        )
    except BadRequest:
        pass
    return EDIT_TEMPLATE if editing else TEMPLATE


async def _go_after_link_preview_step(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    """After preview choice (or skip): upcoming → reminder minutes; else → delay."""
    context.user_data.setdefault("delay_minutes", 0)
    context.user_data.setdefault("suppress_repeat_minutes", 0)
    if context.user_data.get("alert_type") == "upcoming":
        context.user_data["notify_on_live"] = False
        context.user_data["notify_on_end"] = False
        return await _go_schedule_reminder_minutes(update, context, lang)
    return await _go_delay_prompt(update, context, lang)


async def _go_after_ignore_keywords(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    if context.user_data.get("image_file_id"):
        context.user_data["disable_link_preview"] = True
        return await _go_after_link_preview_step(update, context, lang)
    template = str(context.user_data.get("message_template") or "")
    if not template_has_link(template):
        # No URL in template → nothing to preview; skip the ask.
        context.user_data["disable_link_preview"] = True
        return await _go_after_link_preview_step(update, context, lang)
    return await _go_link_preview_prompt(update, context, lang)


async def receive_ignore_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = _user_lang(context, update.effective_user.id)
    text = (update.effective_message.text or "").strip()
    if text in all_menu_buttons():
        await update.effective_message.reply_text(t("finish_setup_first", lang))
        return IGNORE_KEYWORDS

    context.user_data["ignore_keywords"] = normalize_ignore_keywords(text)
    return await _go_after_ignore_keywords(update, context, lang)


async def receive_ignore_keywords_skip(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    context.user_data["ignore_keywords"] = ""
    await query.edit_message_text("✓")
    return await _go_after_ignore_keywords(update, context, lang)


async def receive_ignore_keywords_global_toggle(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    new_val = not bool(context.user_data.get("use_global_ignore"))
    context.user_data["use_global_ignore"] = new_val
    editing = bool(
        context.user_data.get("wizard_edit") and context.user_data.get("edit_sub_id")
    )
    if not new_val:
        await query.edit_message_reply_markup(
            reply_markup=ignore_keywords_keyboard(
                lang,
                as_cancel=bool(context.user_data.get("ignore_keywords_as_cancel")),
                use_global=False,
            )
        )
        return EDIT_IGNORE_KEYWORDS if editing else IGNORE_KEYWORDS

    await query.edit_message_text("✓")
    if editing:
        db: Database = context.application.bot_data["db"]
        owner_id = query.from_user.id
        sub_id = int(context.user_data["edit_sub_id"])
        sub_num = _owner_sub_number(db, owner_id, sub_id)
        if not db.update_subscription(sub_id, owner_id, use_global_ignore=True):
            await context.bot.send_message(owner_id, t("sub_not_found", lang))
        else:
            await context.bot.send_message(
                owner_id,
                t("edit_updated", lang, sub_id=sub_num),
                reply_markup=_menu(lang, owner_id),
            )
        context.user_data.clear()
        return ConversationHandler.END

    context.user_data.setdefault("ignore_keywords", "")
    return await _go_after_ignore_keywords(update, context, lang)


async def receive_link_preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    context.user_data["disable_link_preview"] = query.data.endswith(":1")
    await query.edit_message_text("✓")
    return await _go_after_link_preview_step(update, context, lang)


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
    return await _continue_after_delay(update, context, lang)


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
    return await _continue_after_delay(update, context, lang)


async def receive_repeat_allow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    if query.data.endswith(":1"):
        context.user_data["suppress_repeat_minutes"] = 0
        return await _go_after_repeat(update, context, lang)
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
    return await _go_after_repeat(update, context, lang)


async def receive_schedule_reminder_ask(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    if query.data.endswith(":0"):
        context.user_data["schedule_reminder_minutes"] = 0
        context.user_data["schedule_reminder_configured"] = False
        context.user_data.pop("notify_on_live", None)
        _set_wizard_back(context, DEST_TYPE)
        return await _prompt_dest_step(update, context, lang)
    context.user_data["notify_on_live"] = False
    await query.edit_message_text("✓")
    await context.bot.send_message(
        query.from_user.id,
        t("schedule_reminder_minutes_prompt", lang),
        reply_markup=_wizard(lang),
    )
    _set_wizard_back(context, SCHEDULE_REMINDER_MINUTES)
    return SCHEDULE_REMINDER_MINUTES


async def receive_schedule_reminder_minutes(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    lang = _user_lang(context, update.effective_user.id)
    raw = (update.effective_message.text or "").strip()
    if raw in all_menu_buttons():
        await update.effective_message.reply_text(t("finish_setup_first", lang))
        return SCHEDULE_REMINDER_MINUTES
    if not raw.isdigit() or int(raw) < 1:
        await update.effective_message.reply_text(
            t("schedule_reminder_minutes_invalid", lang)
        )
        return SCHEDULE_REMINDER_MINUTES
    context.user_data["schedule_reminder_minutes"] = int(raw)
    context.user_data["schedule_reminder_configured"] = True
    _set_wizard_back(context, DEST_TYPE)
    return await _prompt_dest_step(update, context, lang)


_LIVE_ADDON_CLEAR_KEYS = (
    "message_template",
    "pending_template",
    "pending_template_preview_disabled",
    "image_file_id",
    "image_position",
    "ignore_keywords",
    "use_global_ignore",
    "disable_link_preview",
    "strip_name_mentions",
    "delay_minutes",
    "suppress_repeat_minutes",
    "dest_type",
    "delete_previous",
    "notify_delete_fail",
    "pending_chat_id",
    "pending_thread_id",
    "lucky_quick",
    "channel_input_was_url",
)


async def receive_schedule_live_add(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    owner_id = query.from_user.id
    db: Database = context.application.bot_data["db"]
    sub_id = context.user_data.get("pending_live_sub_id")
    sub = db.get_subscription(sub_id, owner_id) if sub_id else None
    if not sub:
        await query.edit_message_text(t("sub_not_found", lang))
        context.user_data.clear()
        return ConversationHandler.END

    if query.data.endswith(":0"):
        await query.edit_message_text("✓")
        user_sub_num = _owner_sub_number(db, owner_id, sub.id)
        thread_note = (
            t("thread_note", lang, thread_id=sub.thread_id) if sub.thread_id else ""
        )
        remind = int(sub.schedule_reminder_minutes or 0)
        text = t(
            "setup_schedule_only_done",
            lang,
            sub_id=user_sub_num,
            twitch_username=sub.twitch_username,
            schedule_reminder_note=t(
                "schedule_reminder_yes_note", lang, minutes=remind
            ),
            dest=dest_label(sub.dest_type, lang),
            thread_note=thread_note,
        )
        await context.bot.send_message(owner_id, text, reply_markup=_menu(lang, owner_id))
        context.user_data.clear()
        return ConversationHandler.END

    await query.edit_message_text("✓")
    context.user_data["edit_sub_id"] = sub.id
    context.user_data["live_addon_pass"] = True
    context.user_data["skip_schedule_check"] = True
    context.user_data["notify_on_live"] = True
    context.user_data["twitch_username"] = sub.twitch_username
    context.user_data["twitch_user_id"] = sub.twitch_user_id
    context.user_data["schedule_reminder_minutes"] = sub.schedule_reminder_minutes
    context.user_data["schedule_reminder_configured"] = sub.schedule_reminder_configured
    context.user_data.pop("pending_live_sub_id", None)
    for key in _LIVE_ADDON_CLEAR_KEYS:
        context.user_data.pop(key, None)
    return await _go_template_prompt(update, context, lang)


async def start_edit_delay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    sub_id = int(query.data.split(":")[1])
    db: Database = context.application.bot_data["db"]
    if not await prem.has_feature(context.bot, db, query.from_user.id, "delay"):
        from premium_handlers import send_premium_screen

        await query.edit_message_text(
            t("premium_gate", lang, action=t("premium_gate_action_cancel", lang))
        )
        await send_premium_screen(context.bot, query.from_user.id, lang, db)
        return ConversationHandler.END
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
    if not await prem.has_feature(context.bot, db, query.from_user.id, "repeat"):
        from premium_handlers import send_premium_screen

        await query.edit_message_text(
            t("premium_gate", lang, action=t("premium_gate_action_cancel", lang))
        )
        await send_premium_screen(context.bot, query.from_user.id, lang, db)
        return ConversationHandler.END
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


async def start_edit_schedule_reminder(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    sub_id = int(query.data.split(":")[1])
    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    sub = db.get_subscription(sub_id, query.from_user.id)
    if not sub or not sub.schedule_reminder_configured:
        await query.edit_message_text(t("sub_not_found", lang))
        return ConversationHandler.END

    has_schedule = True
    try:
        has_schedule = await asyncio.to_thread(
            twitch.has_channel_schedule, sub.twitch_user_id
        )
    except Exception:
        logger.exception(
            "Twitch schedule check failed for edit sub %s", sub.twitch_user_id
        )
        # ponytail: on API failure keep current setting; only disable on confirmed 404/absent
        has_schedule = True

    if not has_schedule:
        db.update_subscription(
            sub_id, query.from_user.id, schedule_reminder_minutes=0
        )
        await query.edit_message_text(
            t("edit_schedule_reminder_no_schedule", lang),
        )
        await context.bot.send_message(
            query.from_user.id,
            t("edit_updated", lang, sub_id=_owner_sub_number(db, query.from_user.id, sub_id)),
            reply_markup=_menu(lang, query.from_user.id),
        )
        return ConversationHandler.END

    context.user_data["edit_sub_id"] = sub_id
    context.user_data["wizard_edit"] = True
    current = _schedule_reminder_current_label(sub.schedule_reminder_minutes, lang)
    sub_num = _owner_sub_number(db, query.from_user.id, sub_id)
    await query.edit_message_text("✓")
    await context.bot.send_message(
        query.from_user.id,
        t(
            "edit_schedule_reminder_prompt",
            lang,
            sub_id=sub_num,
            current=current,
        ),
        reply_markup=_wizard(lang, back=False),
    )
    return EDIT_SCHEDULE_REMINDER


async def receive_edit_schedule_reminder(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    lang = _user_lang(context, update.effective_user.id)
    sub_id = context.user_data.get("edit_sub_id")
    if not sub_id:
        return ConversationHandler.END

    raw = (update.effective_message.text or "").strip()
    if raw in all_menu_buttons():
        await update.effective_message.reply_text(t("finish_setup_first", lang))
        return EDIT_SCHEDULE_REMINDER
    if not raw.isdigit():
        await update.effective_message.reply_text(
            t("edit_schedule_reminder_invalid", lang)
        )
        return EDIT_SCHEDULE_REMINDER

    minutes = int(raw)
    db: Database = context.application.bot_data["db"]
    owner_id = update.effective_user.id
    sub_num = _owner_sub_number(db, owner_id, sub_id)
    if not db.update_subscription(
        sub_id, owner_id, schedule_reminder_minutes=minutes
    ):
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

    context.user_data["pending_template_preview_disabled"] = _is_link_preview_disabled(
        update.effective_message
    )
    if await _offer_template_typo_fix(update, context, lang, template):
        return TEMPLATE_TYPO_CONFIRM

    return await _save_edit_template(update, context, lang, template)


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
    if context.user_data.get("lucky_quick"):
        context.user_data["delete_previous"] = False
        context.user_data["notify_delete_fail"] = False
        return await _finish_subscription(
            update, context, update.effective_user.id, chat_id, thread_id
        )
    return await _prompt_delete_old(update, context, lang)


async def _prompt_delete_old(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    db: Database = context.application.bot_data["db"]
    if not await prem.has_feature(context.bot, db, update.effective_user.id, "delete_prev"):
        return await _show_premium_gate(
            update, context, feature="delete_old", first_step=False
        )
    text = _delete_old_prompt_text(context, lang)
    target = update.effective_message
    if target:
        await target.reply_text(
            text,
            reply_markup=delete_old_keyboard(lang),
        )
    else:
        await context.bot.send_message(
            update.effective_user.id,
            text,
            reply_markup=delete_old_keyboard(lang),
        )
    _set_wizard_back(context, DELETE_OLD)
    return DELETE_OLD


def _delete_old_prompt_text(context: ContextTypes.DEFAULT_TYPE, lang: str) -> str:
    if context.user_data.get("alert_type") == "category":
        return t("delete_old_text_category", lang)
    return t("delete_old_text", lang)


def _has_sibling_publication_subs(
    db: Database,
    owner_id: int,
    twitch_user_id: str,
    chat_id: int,
    thread_id: int | None,
    *,
    exclude_sub_id: int | None = None,
) -> bool:
    for sub in _subs_for_owner(db, owner_id):
        if exclude_sub_id is not None and sub.id == exclude_sub_id:
            continue
        if sub.twitch_user_id != twitch_user_id:
            continue
        if sub.chat_id != chat_id:
            continue
        if (sub.thread_id or None) != (thread_id or None):
            continue
        return True
    return False


def _edit_options_for_sub(sub: Subscription, lang: str) -> InlineKeyboardMarkup:
    alert_type = _alert_type_from_sub(sub)
    return edit_options_keyboard(
        sub.id,
        lang,
        dest_type=sub.dest_type,
        delete_previous=sub.delete_previous,
        has_image=bool(sub.image_file_id),
        show_link_preview=not bool(sub.image_file_id)
        and template_has_link(sub.message_template or ""),
        schedule_reminder_configured=sub.schedule_reminder_configured,
        notify_on_category_change=sub.notify_on_category_change,
        notify_on_end=sub.notify_on_end,
        is_upcoming=alert_type == "upcoming",
    )


def _alert_type_from_sub(sub: Subscription) -> str:
    if sub.notify_on_category_change:
        return "category"
    if sub.notify_on_end:
        return "end"
    if sub.schedule_reminder_minutes > 0 and not sub.notify_on_live:
        return "upcoming"
    return "live"


async def _prompt_delete_fail_notify(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    db: Database = context.application.bot_data["db"]
    user_id = update.effective_user.id
    if not await prem.has_feature(context.bot, db, user_id, "delete_prev"):
        return await _show_premium_gate(
            update, context, feature="delete_fail", first_step=False
        )
    text = t("delete_fail_notify_text", lang)
    markup = delete_fail_notify_keyboard(lang)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)
    _set_wizard_back(context, DELETE_FAIL_NOTIFY)
    return DELETE_FAIL_NOTIFY


async def receive_delete_old(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    delete_previous = query.data.endswith(":1")
    context.user_data["delete_previous"] = delete_previous
    context.user_data.pop("delete_sibling_asked", None)
    if not delete_previous:
        context.user_data["notify_delete_fail"] = False
        context.user_data["delete_other_alerts"] = False
        chat_id = context.user_data["pending_chat_id"]
        thread_id = context.user_data.get("pending_thread_id")
        return await _finish_subscription(
            update, context, query.from_user.id, chat_id, thread_id
        )
    if context.user_data.get("alert_type") == "category":
        db: Database = context.application.bot_data["db"]
        chat_id = context.user_data["pending_chat_id"]
        thread_id = context.user_data.get("pending_thread_id")
        twitch_user_id = str(context.user_data.get("twitch_user_id") or "")
        if twitch_user_id and _has_sibling_publication_subs(
            db,
            query.from_user.id,
            twitch_user_id,
            chat_id,
            thread_id,
            exclude_sub_id=context.user_data.get("edit_sub_id"),
        ):
            context.user_data["delete_sibling_asked"] = True
            await query.edit_message_text(
                t("delete_sibling_text", lang),
                reply_markup=delete_sibling_keyboard(lang),
            )
            _set_wizard_back(context, DELETE_SIBLING_ALERTS)
            return DELETE_SIBLING_ALERTS
        context.user_data["delete_other_alerts"] = False
    else:
        context.user_data["delete_other_alerts"] = False
    return await _prompt_delete_fail_notify(update, context, lang)


async def receive_delete_sibling(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    context.user_data["delete_other_alerts"] = query.data.endswith(":1")
    return await _prompt_delete_fail_notify(update, context, lang)


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
    live_addon = bool(data.get("live_addon_pass"))
    dest_type = data["dest_type"]
    delete_previous = bool(data.get("delete_previous", False)) and dest_type != "dm"
    notify_delete_fail = bool(data.get("notify_delete_fail", False)) and delete_previous
    alert_type = str(data.get("alert_type") or "")
    notify_on_end = bool(data.get("notify_on_end", False)) or alert_type == "end"
    notify_on_category_change = (
        bool(data.get("notify_on_category_change", False)) or alert_type == "category"
    )
    delete_other_alerts = (
        bool(data.get("delete_other_alerts", False))
        and delete_previous
        and notify_on_category_change
    )
    if alert_type == "live":
        notify_on_live = True
        notify_on_end = False
        notify_on_category_change = False
    elif alert_type == "end":
        notify_on_live = False
        notify_on_end = True
        notify_on_category_change = False
    elif alert_type == "category":
        notify_on_live = False
        notify_on_end = False
        notify_on_category_change = True
    elif alert_type == "upcoming":
        notify_on_live = False
        notify_on_end = False
        notify_on_category_change = False
    else:
        notify_on_live = bool(data.get("notify_on_live", True))

    try:
        if edit_sub_id and live_addon:
            ok = db.update_subscription(
                edit_sub_id,
                owner_id,
                message_template=data["message_template"],
                dest_type=dest_type,
                chat_id=chat_id,
                thread_id=thread_id,
                delete_previous=delete_previous,
                notify_delete_fail=notify_delete_fail,
                disable_link_preview=bool(data.get("disable_link_preview", False))
                or bool(data.get("image_file_id")),
                strip_name_mentions=bool(data.get("strip_name_mentions")),
                delay_minutes=int(data.get("delay_minutes", 0)),
                suppress_repeat_minutes=int(data.get("suppress_repeat_minutes", 0)),
                ignore_keywords=str(data.get("ignore_keywords", "")),
                use_global_ignore=bool(data.get("use_global_ignore")),
                image_file_id=data.get("image_file_id") or None,
                image_position=str(data.get("image_position") or ""),
                notify_on_live=True,
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
        elif edit_sub_id:
            ok = db.update_subscription(
                edit_sub_id,
                owner_id,
                dest_type=dest_type,
                chat_id=chat_id,
                thread_id=thread_id,
                delete_previous=delete_previous,
                notify_delete_fail=notify_delete_fail,
                delete_other_alerts=delete_other_alerts,
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

            if len(_subs_for_owner(db, owner_id)) >= MAX_SUBSCRIPTIONS_PER_OWNER:
                await context.bot.send_message(
                    owner_id,
                    t("sub_limit", lang, limit=MAX_SUBSCRIPTIONS_PER_OWNER),
                    reply_markup=_menu(lang, owner_id),
                )
                context.user_data.clear()
                return ConversationHandler.END
            create_enabled = True
            if not await prem.can_enable_more_async(context.bot, db, owner_id):
                create_enabled = False
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
                disable_link_preview=bool(data.get("disable_link_preview", False))
                or bool(data.get("image_file_id")),
                strip_name_mentions=bool(data.get("strip_name_mentions")),
                delay_minutes=int(data.get("delay_minutes", 0)),
                suppress_repeat_minutes=(
                    0
                    if notify_on_category_change
                    else int(data.get("suppress_repeat_minutes", 0))
                ),
                schedule_reminder_minutes=int(
                    data.get("schedule_reminder_minutes", 0)
                ),
                schedule_reminder_configured=bool(
                    data.get("schedule_reminder_configured")
                )
                or int(data.get("schedule_reminder_minutes", 0)) > 0,
                ignore_keywords=str(data.get("ignore_keywords", "")),
                use_global_ignore=bool(data.get("use_global_ignore")),
                image_file_id=data.get("image_file_id") or None,
                image_position=str(data.get("image_position") or ""),
                enabled=create_enabled,
                notify_on_live=notify_on_live,
                notify_on_end=notify_on_end,
                notify_on_category_change=notify_on_category_change,
                delete_other_alerts=delete_other_alerts,
                is_demo=demo_mode.is_active(owner_id),
            )
            if not create_enabled:
                context.user_data["premium_created_disabled"] = True
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

    schedule_only_pending = (
        not edit_sub_id
        and not notify_on_live
        and not notify_on_end
        and not notify_on_category_change
        and int(data.get("schedule_reminder_minutes", 0)) > 0
    )
    if schedule_only_pending and alert_type != "upcoming":
        if update.callback_query:
            try:
                await update.callback_query.edit_message_text(
                    t(
                        "sub_created_short",
                        lang,
                        sub_id=_owner_sub_number(db, owner_id, sub_id),
                    )
                )
            except BadRequest:
                pass
        context.user_data.clear()
        context.user_data["pending_live_sub_id"] = sub_id
        await context.bot.send_message(
            owner_id,
            t("schedule_live_add_prompt", lang),
            reply_markup=schedule_live_add_keyboard(lang),
        )
        return SCHEDULE_LIVE_ASK

    created_disabled = bool(context.user_data.pop("premium_created_disabled", False))
    context.user_data.clear()

    if edit_sub_id and not live_addon:
        text = t("edit_updated", lang, sub_id=sub_id)
    elif schedule_only_pending:
        thread_note = (
            t("thread_note", lang, thread_id=thread_id) if thread_id else ""
        )
        remind = int(data.get("schedule_reminder_minutes", 0))
        user_sub_num = _owner_sub_number(db, owner_id, sub_id)
        text = t(
            "setup_schedule_only_done",
            lang,
            sub_id=user_sub_num,
            twitch_username=data["twitch_username"],
            schedule_reminder_note=t(
                "schedule_reminder_yes_note", lang, minutes=remind
            ),
            dest=dest_label(dest_type, lang),
            thread_note=thread_note,
        )
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
        if delete_previous and notify_on_category_change:
            delete_note = (
                t("delete_yes_all", lang)
                if delete_other_alerts
                else t("delete_yes_category", lang)
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
        has_image = bool(data.get("image_file_id"))
        preview_disabled = bool(data.get("disable_link_preview", False)) or has_image
        preview_note = (
            t("preview_off", lang) if preview_disabled else t("preview_on", lang)
        )
        if has_image:
            pos = str(data.get("image_position") or "")
            image_note = (
                t("image_after_note", lang)
                if pos == "after"
                else t("image_before_note", lang)
            )
        else:
            image_note = t("image_no_note", lang)
        ignore_keywords = str(data.get("ignore_keywords", ""))
        use_global_ignore = bool(data.get("use_global_ignore"))
        ignore_keywords_note = _ignore_keywords_note(
            ignore_keywords, use_global_ignore, lang
        )
        delay_minutes = int(data.get("delay_minutes", 0))
        delay_note = (
            t("delay_yes_note", lang, minutes=delay_minutes)
            if delay_minutes > 0
            else t("delay_no_note", lang)
        )
        suppress = int(data.get("suppress_repeat_minutes", 0))
        if notify_on_category_change:
            repeat_note = ""
        else:
            repeat_note = (
                t("repeat_no_note", lang, minutes=suppress)
                if suppress > 0
                else t("repeat_yes_note", lang)
            )
        remind = int(data.get("schedule_reminder_minutes", 0))
        schedule_reminder_note = (
            t("schedule_reminder_yes_note", lang, minutes=remind)
            if remind > 0
            else t("schedule_reminder_no_note", lang)
        )
        if notify_on_category_change:
            alert_note = t(
                "alert_note_category",
                lang,
                twitch_username=data["twitch_username"],
            )
        else:
            alert_note = t(
                "alert_note_end" if notify_on_end else "alert_note_live",
                lang,
                twitch_username=data["twitch_username"],
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
            image_note=image_note,
            ignore_keywords_note=ignore_keywords_note,
            delay_note=delay_note,
            repeat_note=repeat_note,
            schedule_reminder_note=schedule_reminder_note,
            alert_note=alert_note,
        )
        try:
            await _deliver_alert_content(
                context.bot,
                chat_id=owner_id,
                text=preview,
                image_file_id=data.get("image_file_id") or None,
                image_position=str(data.get("image_position") or ""),
                disable_link_preview=preview_disabled,
            )
        except (BadRequest, Forbidden) as exc:
            logger.warning("Cannot send setup preview to %s: %s", owner_id, exc)

    if update.callback_query:
        try:
            msg_key = "sub_created_short" if not edit_sub_id or live_addon else "edit_updated"
            await update.callback_query.edit_message_text(
                t(msg_key, lang, sub_id=_owner_sub_number(db, owner_id, sub_id))
            )
        except BadRequest:
            pass

    await context.bot.send_message(owner_id, text, reply_markup=_menu(lang, owner_id))
    if created_disabled:
        await context.bot.send_message(
            owner_id,
            t("premium_created_disabled", lang, limit=prem.free_active_limit()),
        )
    return ConversationHandler.END


async def start_stream_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    context.user_data.clear()
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


def _pending_schedule_publishes(application: Application) -> dict[int, dict]:
    return application.bot_data.setdefault("pending_schedule_publishes", {})


async def stream_schedule_publish_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    publish = query.data.split(":")[-1] == "1"
    entries = context.user_data.get("stream_schedule_entries", [])
    if not publish or not entries:
        context.user_data.clear()
        await query.edit_message_text(t("cancelled", lang) if not publish else "—")
        await context.bot.send_message(
            user_id, t("menu_main", lang), reply_markup=_menu(lang, user_id)
        )
        return ConversationHandler.END

    db: Database = context.application.bot_data["db"]
    if not await prem.has_feature(context.bot, db, user_id, "schedule_publish"):
        from premium_handlers import send_premium_screen

        context.user_data.clear()
        await query.edit_message_text(
            t("premium_gate", lang, action=t("premium_gate_action_cancel", lang))
        )
        await send_premium_screen(context.bot, user_id, lang, db)
        return ConversationHandler.END

    await query.edit_message_text(
        t("stream_schedule_duration_prompt", lang),
        reply_markup=stream_schedule_duration_keyboard(lang),
    )
    return STREAM_SCHEDULE_DURATION


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
    if not entries:
        context.user_data.clear()
        await query.edit_message_text(t("stream_schedule_publish_fail", lang, error="no data"))
        await context.bot.send_message(
            user_id, t("menu_main", lang), reply_markup=_menu(lang, user_id)
        )
        return ConversationHandler.END

    _pending_schedule_publishes(context.application)[user_id] = {
        "entries": [
            {"date": e["date"].isoformat(), "time": e["time"], "game": e["game"]}
            for e in entries
        ],
        "duration": duration_min,
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

    redirect_uri = twitch_oauth_redirect_uri()
    if not redirect_uri:
        text = t("stream_schedule_publish_auth_unavailable", lang)
        if query:
            await query.edit_message_text(text)
        else:
            await context.bot.send_message(user_id, text)
        await context.bot.send_message(
            user_id, t("menu_main", lang), reply_markup=_menu(lang, user_id)
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
        await context.bot.send_message(user_id, auth_text, reply_markup=markup)
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
    elif isinstance(pending, dict):
        entries = pending.get("entries") or []
        duration_min = int(pending.get("duration") or _SCHEDULE_DEFAULT_DURATION_MIN)
    else:
        entries, duration_min = [], _SCHEDULE_DEFAULT_DURATION_MIN
    if not entries or not token_info:
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

    try:
        await asyncio.to_thread(
            twitch.clear_channel_schedule, access, twitch_user_id
        )
    except Exception as exc:
        logger.warning(
            "Failed to clear Twitch schedule before publish (user=%s): %s",
            owner_id,
            exc,
        )

    ok_count = 0
    errors: list[str] = []
    prefer_recurring = False
    used_recurring_fallback = False
    for entry in entries:
        hour, minute = (int(x) for x in entry["time"].split(":", 1))
        y, m, d = (int(x) for x in entry["date"].split("-", 2))
        local_dt = datetime(y, m, d, hour, minute, tzinfo=SCHEDULE_TZ)
        start_iso = local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        game_text = entry.get("game", "")
        category_id = ""
        if game_text:
            try:
                cats = twitch.search_categories(game_text)
                if cats:
                    category_id = cats[0]["id"]
            except Exception:
                pass
        try:
            # Partner/Affiliate → one-off; else Twitch 403 → weekly recurring fallback.
            _, recurring = twitch.create_schedule_segment_with_fallback(
                access,
                twitch_user_id,
                start_time=start_iso,
                timezone=SCHEDULE_TZ_NAME,
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
            errors.append(f"{entry['date']}: {exc}")

    total = len(entries)
    if ok_count == total:
        key = (
            "stream_schedule_publish_ok_recurring"
            if used_recurring_fallback
            else "stream_schedule_publish_ok"
        )
        text = t(key, lang)
    elif ok_count > 0:
        text = t("stream_schedule_publish_partial", lang, ok=ok_count, total=total, errors="; ".join(errors))
    else:
        text = t("stream_schedule_publish_fail", lang, error="; ".join(errors))

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
        user_id, t("menu_main", lang), reply_markup=_menu(lang, user_id)
    )


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


def _parse_watch_viewers(text: str) -> tuple[int, int | None] | None:
    m = _WATCH_VIEWERS_RE.match(text.strip())
    if not m:
        return None
    lo = int(m.group(1))
    hi = int(m.group(2)) if m.group(2) is not None else None
    if hi is not None and hi < lo:
        lo, hi = hi, lo
    return lo, hi


def _watch_viewers_label(prefs: WatchPrefs, lang: str) -> str:
    if prefs.min_viewers <= 0 and prefs.max_viewers is None:
        return t("watch_viewers_label_any", lang)
    if prefs.max_viewers is None:
        return t("watch_viewers_label_min", lang, min=prefs.min_viewers)
    return t(
        "watch_viewers_label_range",
        lang,
        min=prefs.min_viewers,
        max=prefs.max_viewers,
    )


def _watch_prefs_summary(prefs: WatchPrefs, lang: str) -> str:
    cats = ", ".join(c["name"] for c in prefs.categories) or "—"
    tags = ", ".join(prefs.tags) if prefs.tags else t("watch_tags_label_any", lang)
    return t(
        "watch_prefs_summary",
        lang,
        cats=cats,
        viewers=_watch_viewers_label(prefs, lang),
        language=prefs.language or t("watch_lang_label_any", lang),
        tags=tags,
        mature=(
            t("watch_mature_label_exclude", lang)
            if prefs.exclude_mature
            else t("watch_mature_label_allow", lang)
        ),
    )


def _format_watch_suggestions(
    streams: list[dict], prefs: WatchPrefs, lang: str
) -> str:
    lines = [
        t("watch_suggest_header", lang),
        "",
        _watch_prefs_summary(prefs, lang),
        "",
    ]
    for i, s in enumerate(streams, start=1):
        login = html.escape(str(s.get("user_login") or ""))
        display = html.escape(str(s.get("user_name") or login))
        title = html.escape(str(s.get("title") or "—"))
        game = html.escape(str(s.get("game_name") or "—"))
        viewers = int(s.get("viewer_count") or 0)
        lines.append(
            t(
                "watch_suggest_item",
                lang,
                n=i,
                display=display,
                login=login,
                title=title,
                game=game,
                viewers=viewers,
            )
        )
        lines.append("")
    return "\n".join(lines).rstrip()


async def _fetch_watch_suggestions(
    twitch: TwitchClient, prefs: WatchPrefs
) -> list[dict]:
    pooled: list[dict] = []
    for cat in prefs.categories:
        try:
            batch = await asyncio.to_thread(
                twitch.get_streams_by_game,
                cat["id"],
                language=prefs.language,
                first=100,
            )
        except Exception:
            logger.exception("watch streams fetch failed for game_id=%s", cat.get("id"))
            continue
        pooled.extend(batch)
    filtered = filter_streams_for_watch(
        pooled,
        min_viewers=prefs.min_viewers,
        max_viewers=prefs.max_viewers,
        exclude_mature=prefs.exclude_mature,
        tags=prefs.tags,
    )
    return pick_random_streams(filtered, _WATCH_SUGGEST_N)


async def _send_watch_suggestions(
    *,
    bot,
    chat_id: int,
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    prefs: WatchPrefs,
    edit_message=None,
) -> None:
    lang = _user_lang(context, user_id)
    context.application.bot_data.setdefault("watch_last_prefs", {})[user_id] = prefs
    twitch: TwitchClient = context.application.bot_data["twitch"]
    try:
        streams = await _fetch_watch_suggestions(twitch, prefs)
    except Exception:
        logger.exception("watch suggestions failed")
        text = t("watch_suggest_error", lang)
        markup = watch_suggest_keyboard(lang)
        if edit_message is not None:
            try:
                await edit_message.edit_text(text, reply_markup=markup)
                return
            except BadRequest:
                pass
        await bot.send_message(
            chat_id, t("menu_main", lang), reply_markup=_menu(lang, user_id)
        )
        await bot.send_message(chat_id, text, reply_markup=markup)
        return

    if not streams:
        text = (
            t("watch_suggest_empty", lang)
            + "\n\n"
            + _watch_prefs_summary(prefs, lang)
        )
    else:
        text = _format_watch_suggestions(streams, prefs, lang)
    markup = watch_suggest_keyboard(lang)
    if edit_message is not None:
        try:
            await edit_message.edit_text(
                text,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return
        except BadRequest:
            pass
    await bot.send_message(
        chat_id,
        t("menu_main", lang),
        reply_markup=_menu(lang, user_id),
    )
    await bot.send_message(
        chat_id,
        text,
        reply_markup=markup,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


def _watch_prefs_from_user_data(context: ContextTypes.DEFAULT_TYPE) -> WatchPrefs:
    max_v = context.user_data.get("watch_max_viewers")
    return WatchPrefs(
        categories=list(context.user_data.get("watch_categories") or []),
        min_viewers=int(context.user_data.get("watch_min_viewers") or 0),
        max_viewers=int(max_v) if max_v is not None else None,
        language=context.user_data.get("watch_language"),
        tags=list(context.user_data.get("watch_tags") or []),
        exclude_mature=bool(context.user_data.get("watch_exclude_mature", True)),
    )


def _resolve_watch_prefs(
    context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> WatchPrefs | None:
    last = context.application.bot_data.get("watch_last_prefs") or {}
    cached = last.get(user_id)
    if isinstance(cached, WatchPrefs):
        return cached
    db: Database = context.application.bot_data["db"]
    filters = db.get_watch_filters(user_id)
    return filters[0].prefs if filters else None


async def _start_watch_wizard(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    context.user_data.clear()
    context.user_data["watch_categories"] = []
    context.user_data["watch_tags"] = []
    return await _go_watch_categories_prompt(update, context, lang)


async def _go_watch_pick_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    db: Database = context.application.bot_data["db"]
    user_id = update.effective_user.id
    filters = db.get_watch_filters(user_id)
    if not filters:
        await update.effective_message.reply_text(t("watch_pick_empty", lang))
        return await _start_watch_wizard(update, context, lang)
    msg = update.effective_message
    if update.callback_query and update.callback_query.message:
        try:
            await update.callback_query.edit_message_text(
                t("watch_pick_prompt", lang),
                reply_markup=watch_pick_keyboard(lang, filters),
            )
            _set_wizard_back(context, WATCH_PICK)
            return WATCH_PICK
        except BadRequest:
            pass
    await msg.reply_text(
        t("watch_pick_prompt", lang),
        reply_markup=watch_pick_keyboard(lang, filters),
    )
    _set_wizard_back(context, WATCH_PICK)
    return WATCH_PICK


async def _go_watch_categories_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    cats = context.user_data.setdefault("watch_categories", [])
    await update.effective_message.reply_text(
        t("watch_cats_prompt", lang, max=_WATCH_MAX_CATS),
        reply_markup=_wizard(lang, back=False),
        parse_mode=ParseMode.HTML,
    )
    if cats:
        await update.effective_message.reply_text(
            t(
                "watch_cats_added",
                lang,
                name=cats[-1]["name"],
                count=len(cats),
                max=_WATCH_MAX_CATS,
                list=", ".join(c["name"] for c in cats),
            ),
            reply_markup=watch_cats_nav_keyboard(lang, has_cats=True),
        )
    _set_wizard_back(context, WATCH_CATEGORIES)
    return WATCH_CATEGORIES


async def _go_watch_tags_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    await update.effective_message.reply_text(
        t("watch_tags_prompt", lang),
        reply_markup=_wizard(lang),
        parse_mode=ParseMode.HTML,
    )
    await update.effective_message.reply_text(
        t("watch_tags_skip", lang),
        reply_markup=watch_tags_keyboard(lang),
    )
    _set_wizard_back(context, WATCH_TAGS)
    return WATCH_TAGS


async def _go_watch_viewers_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    await update.effective_message.reply_text(
        t("watch_viewers_prompt", lang),
        reply_markup=_wizard(lang),
        parse_mode=ParseMode.HTML,
    )
    await update.effective_message.reply_text(
        t("watch_viewers_any", lang),
        reply_markup=watch_viewers_keyboard(lang),
    )
    _set_wizard_back(context, WATCH_VIEWERS)
    return WATCH_VIEWERS


async def _go_watch_language_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    context.user_data.pop("watch_lang_await_other", None)
    await update.effective_message.reply_text(
        t("watch_lang_prompt", lang),
        reply_markup=_wizard(lang),
    )
    await update.effective_message.reply_text(
        t("watch_lang_any", lang),
        reply_markup=watch_lang_keyboard(lang),
    )
    _set_wizard_back(context, WATCH_LANGUAGE)
    return WATCH_LANGUAGE


async def _go_watch_mature_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    await update.effective_message.reply_text(
        t("watch_mature_prompt", lang),
        reply_markup=_wizard(lang),
    )
    await update.effective_message.reply_text(
        t("watch_mature_exclude", lang),
        reply_markup=watch_mature_keyboard(lang),
    )
    _set_wizard_back(context, WATCH_MATURE)
    return WATCH_MATURE


async def _go_watch_save_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    prefs = _watch_prefs_from_user_data(context)
    await update.effective_message.reply_text(
        t(
            "watch_save_prompt",
            lang,
            max=WATCH_MAX_FILTERS,
            summary=_watch_prefs_summary(prefs, lang),
        ),
        reply_markup=_wizard(lang),
        parse_mode=ParseMode.HTML,
    )
    await update.effective_message.reply_text(
        t("watch_save_yes", lang),
        reply_markup=watch_save_keyboard(lang),
    )
    _set_wizard_back(context, WATCH_SAVE)
    return WATCH_SAVE


async def _complete_watch_wizard(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, save: bool
) -> int:
    user_id = update.effective_user.id
    db: Database = context.application.bot_data["db"]
    prefs = _watch_prefs_from_user_data(context)
    if save:
        db.add_watch_filter(user_id, prefs)
    context.user_data.clear()
    chat_id = update.effective_chat.id
    if update.callback_query:
        try:
            await update.callback_query.edit_message_reply_markup(None)
        except BadRequest:
            pass
    await _send_watch_suggestions(
        bot=context.bot,
        chat_id=chat_id,
        user_id=user_id,
        context=context,
        prefs=prefs,
    )
    return ConversationHandler.END


async def start_what_to_watch(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    user_id = update.effective_user.id
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    lang = _user_lang(context, user_id)
    filters = db.get_watch_filters(user_id)
    context.user_data.clear()
    if filters:
        return await _go_watch_pick_prompt(update, context, lang)
    return await _start_watch_wizard(update, context, lang)


async def start_watch_change(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    return await start_what_to_watch(update, context)


async def on_watch_again(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    prefs = _resolve_watch_prefs(context, user_id)
    if not prefs:
        lang = _user_lang(context, user_id)
        await query.edit_message_text(t("watch_cats_need_one", lang))
        return
    await _send_watch_suggestions(
        bot=context.bot,
        chat_id=query.message.chat_id,
        user_id=user_id,
        context=context,
        prefs=prefs,
        edit_message=query.message,
    )


async def receive_watch_pick_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    data = query.data or ""
    if data == "watch_pick:new":
        try:
            await query.edit_message_reply_markup(None)
        except BadRequest:
            pass
        return await _start_watch_wizard(update, context, lang)
    if data == "watch_pick:delete":
        filters = db.get_watch_filters(user_id)
        if not filters:
            return await _go_watch_pick_prompt(update, context, lang)
        context.user_data["watch_delete_selected"] = set()
        await query.edit_message_text(
            t("watch_delete_pick", lang),
            reply_markup=watch_delete_pick_keyboard(lang, filters, set()),
        )
        _set_wizard_back(context, WATCH_DELETE)
        return WATCH_DELETE
    if data.startswith("watch_pick:"):
        fid = data.split(":", 1)[1]
        filters = db.get_watch_filters(user_id)
        match = next((f for f in filters if f.id == fid), None)
        if not match:
            return await _go_watch_pick_prompt(update, context, lang)
        context.user_data.clear()
        try:
            await query.edit_message_reply_markup(None)
        except BadRequest:
            pass
        await _send_watch_suggestions(
            bot=context.bot,
            chat_id=query.message.chat_id,
            user_id=user_id,
            context=context,
            prefs=match.prefs,
        )
        return ConversationHandler.END
    return WATCH_PICK


async def receive_watch_del_sel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    fid = (query.data or "").split(":", 1)[-1]
    selected: set[str] = context.user_data.setdefault("watch_delete_selected", set())
    if fid in selected:
        selected.discard(fid)
    else:
        selected.add(fid)
    db: Database = context.application.bot_data["db"]
    filters = db.get_watch_filters(user_id)
    await query.edit_message_reply_markup(
        reply_markup=watch_delete_pick_keyboard(lang, filters, selected)
    )
    return WATCH_DELETE


async def receive_watch_del_clear(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    context.user_data["watch_delete_selected"] = set()
    db: Database = context.application.bot_data["db"]
    filters = db.get_watch_filters(user_id)
    await query.edit_message_reply_markup(
        reply_markup=watch_delete_pick_keyboard(lang, filters, set())
    )
    return WATCH_DELETE


async def receive_watch_del_go(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    selected: set[str] = set(context.user_data.get("watch_delete_selected") or ())
    if not selected:
        await query.answer(t("watch_delete_none", lang), show_alert=True)
        return WATCH_DELETE
    await query.answer()
    db: Database = context.application.bot_data["db"]
    deleted = 0
    for fid in list(selected):
        if db.delete_watch_filter(user_id, fid):
            deleted += 1
    context.user_data["watch_delete_selected"] = set()
    filters = db.get_watch_filters(user_id)
    try:
        await query.edit_message_text(t("watch_deleted", lang, count=deleted))
    except BadRequest:
        pass
    if not filters:
        await context.bot.send_message(
            query.message.chat_id,
            t("watch_pick_empty", lang),
            reply_markup=_menu(lang, user_id),
        )
        return await _start_watch_wizard(update, context, lang)
    await context.bot.send_message(
        query.message.chat_id,
        t("watch_pick_prompt", lang),
        reply_markup=watch_pick_keyboard(lang, filters),
    )
    _set_wizard_back(context, WATCH_PICK)
    return WATCH_PICK


async def receive_watch_del_back(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    context.user_data.pop("watch_delete_selected", None)
    return await _go_watch_pick_prompt(update, context, lang)


async def receive_watch_category_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    cats: list[dict[str, str]] = context.user_data.setdefault("watch_categories", [])
    if len(cats) >= _WATCH_MAX_CATS:
        await update.effective_message.reply_text(
            t("watch_cats_full", lang, max=_WATCH_MAX_CATS),
            reply_markup=watch_cats_nav_keyboard(lang, has_cats=True),
        )
        return WATCH_CATEGORIES
    query = (update.effective_message.text or "").strip()
    if not query:
        return WATCH_CATEGORIES
    twitch: TwitchClient = context.application.bot_data["twitch"]
    try:
        found = await asyncio.to_thread(twitch.search_categories, query, first=5)
    except Exception:
        logger.exception("watch category search failed")
        await update.effective_message.reply_text(
            t("watch_cats_not_found", lang, query=query),
        )
        return WATCH_CATEGORIES
    if not found:
        await update.effective_message.reply_text(
            t("watch_cats_not_found", lang, query=query),
        )
        return WATCH_CATEGORIES
    if len(found) == 1:
        return await _add_watch_category(update, context, lang, found[0])
    context.user_data["watch_cat_candidates"] = [
        {"id": str(c["id"]), "name": str(c.get("name") or "")} for c in found
    ]
    await update.effective_message.reply_text(
        t("watch_cats_pick", lang),
        reply_markup=watch_cats_pick_keyboard(
            lang, context.user_data["watch_cat_candidates"]
        ),
    )
    return WATCH_CATEGORIES


async def _add_watch_category(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    lang: str,
    cat: dict,
) -> int:
    cats: list[dict[str, str]] = context.user_data.setdefault("watch_categories", [])
    entry = {"id": str(cat["id"]), "name": str(cat.get("name") or "")}
    if not any(c["id"] == entry["id"] for c in cats):
        cats.append(entry)
    context.user_data.pop("watch_cat_candidates", None)
    await update.effective_message.reply_text(
        t(
            "watch_cats_added",
            lang,
            name=entry["name"],
            count=len(cats),
            max=_WATCH_MAX_CATS,
            list=", ".join(c["name"] for c in cats),
        ),
        reply_markup=watch_cats_nav_keyboard(lang, has_cats=True),
    )
    return WATCH_CATEGORIES


async def receive_watch_category_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    data = query.data or ""
    if data == "watch_cat:done":
        cats = context.user_data.get("watch_categories") or []
        if not cats:
            await query.edit_message_text(t("watch_cats_need_one", lang))
            return WATCH_CATEGORIES
        try:
            await query.edit_message_reply_markup(None)
        except BadRequest:
            pass
        return await _go_watch_tags_prompt(update, context, lang)
    if data == "watch_cat:clear":
        context.user_data["watch_categories"] = []
        await query.edit_message_text(t("watch_cats_prompt", lang, max=_WATCH_MAX_CATS))
        return WATCH_CATEGORIES
    if data.startswith("watch_cat:pick:"):
        try:
            idx = int(data.rsplit(":", 1)[1])
        except ValueError:
            return WATCH_CATEGORIES
        candidates = context.user_data.get("watch_cat_candidates") or []
        if idx < 0 or idx >= len(candidates):
            return WATCH_CATEGORIES
        try:
            await query.edit_message_reply_markup(None)
        except BadRequest:
            pass
        return await _add_watch_category(update, context, lang, candidates[idx])
    return WATCH_CATEGORIES


async def receive_watch_viewers_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    lang = _user_lang(context, update.effective_user.id)
    parsed = _parse_watch_viewers(update.effective_message.text or "")
    if parsed is None:
        await update.effective_message.reply_text(
            t("watch_viewers_bad", lang),
            reply_markup=watch_viewers_keyboard(lang),
            parse_mode=ParseMode.HTML,
        )
        return WATCH_VIEWERS
    lo, hi = parsed
    context.user_data["watch_min_viewers"] = lo
    context.user_data["watch_max_viewers"] = hi
    return await _go_watch_language_prompt(update, context, lang)


async def receive_watch_viewers_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    if query.data == "watch_viewers:any":
        context.user_data["watch_min_viewers"] = 0
        context.user_data["watch_max_viewers"] = None
        try:
            await query.edit_message_reply_markup(None)
        except BadRequest:
            pass
        return await _go_watch_language_prompt(update, context, lang)
    return WATCH_VIEWERS


async def receive_watch_language_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    data = query.data or ""
    if data == "watch_lang:any":
        context.user_data["watch_language"] = None
        context.user_data.pop("watch_lang_await_other", None)
        try:
            await query.edit_message_reply_markup(None)
        except BadRequest:
            pass
        return await _go_watch_mature_prompt(update, context, lang)
    if data in ("watch_lang:ru", "watch_lang:en"):
        context.user_data["watch_language"] = data.rsplit(":", 1)[1]
        context.user_data.pop("watch_lang_await_other", None)
        try:
            await query.edit_message_reply_markup(None)
        except BadRequest:
            pass
        return await _go_watch_mature_prompt(update, context, lang)
    if data == "watch_lang:other":
        context.user_data["watch_lang_await_other"] = True
        await query.edit_message_text(t("watch_lang_other_prompt", lang))
        return WATCH_LANGUAGE
    return WATCH_LANGUAGE


async def receive_watch_language_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    lang = _user_lang(context, update.effective_user.id)
    if not context.user_data.get("watch_lang_await_other"):
        await update.effective_message.reply_text(
            t("watch_lang_prompt", lang),
            reply_markup=watch_lang_keyboard(lang),
        )
        return WATCH_LANGUAGE
    code = (update.effective_message.text or "").strip().lower()
    if not _WATCH_LANG_RE.match(code):
        await update.effective_message.reply_text(t("watch_lang_bad", lang))
        return WATCH_LANGUAGE
    context.user_data["watch_language"] = code
    context.user_data.pop("watch_lang_await_other", None)
    return await _go_watch_mature_prompt(update, context, lang)


async def receive_watch_tags_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    lang = _user_lang(context, update.effective_user.id)
    tags = normalize_watch_tags(
        update.effective_message.text or "", limit=_WATCH_MAX_TAGS
    )
    if not tags:
        await update.effective_message.reply_text(
            t("watch_tags_bad", lang),
            reply_markup=watch_tags_keyboard(lang),
            parse_mode=ParseMode.HTML,
        )
        return WATCH_TAGS
    context.user_data["watch_tags"] = tags
    return await _go_watch_viewers_prompt(update, context, lang)


async def receive_watch_tags_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    if query.data == "watch_tags:skip":
        context.user_data["watch_tags"] = []
        try:
            await query.edit_message_reply_markup(None)
        except BadRequest:
            pass
        return await _go_watch_viewers_prompt(update, context, lang)
    return WATCH_TAGS


async def receive_watch_mature_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    data = query.data or ""
    context.user_data["watch_exclude_mature"] = data == "watch_mature:1"
    try:
        await query.edit_message_reply_markup(None)
    except BadRequest:
        pass
    return await _go_watch_save_prompt(update, context, lang)


async def receive_watch_save_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    save = (query.data or "") == "watch_save:1"
    return await _complete_watch_wizard(update, context, save=save)


async def report_problem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    user_id = update.effective_user.id
    db.upsert_user(user_id)
    lang = _user_lang(context, user_id)
    await update.effective_message.reply_text(
        t("feedback", lang, github=GITHUB_ISSUES_URL, user_id=user_id),
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
    if demo_mode.is_active(user_id):
        lang = _user_lang(context, user_id)
        await update.effective_message.reply_text(
            t("demo_on", lang),
            reply_markup=_menu(lang, user_id),
        )
        return
    lang = _user_lang(context, user_id)
    await update.effective_message.reply_text(
        t("menu_admin", lang),
        reply_markup=admin_menu(lang),
    )


async def toggle_demo_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        return
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    context.user_data.clear()
    if demo_mode.is_active(user_id):
        db.delete_demo_subscriptions(user_id)
        demo_mode.deactivate(user_id)
        await update.effective_message.reply_text(
            t("demo_off", lang),
            reply_markup=admin_menu(lang),
        )
        return
    await _enter_demo_mode(update, context, user_id, lang, db)


async def _enter_demo_mode(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    lang: str,
    db: Database,
) -> None:
    db.delete_demo_subscriptions(user_id)
    twitch: TwitchClient = context.application.bot_data["twitch"]
    login = prem.twitch_channel_login() or "marfapr"
    user = await asyncio.to_thread(twitch.get_user, login)
    if user:
        uid = str(user["id"])
        uname = str(user.get("login") or login).lower()
        for template_key in ("demo_seed_template", "demo_seed_template_2"):
            db.add_subscription(
                owner_id=user_id,
                twitch_username=uname,
                twitch_user_id=uid,
                message_template=t(template_key, lang),
                dest_type="dm",
                chat_id=user_id,
                thread_id=None,
                disable_link_preview=True,
                enabled=True,
                notify_on_live=True,
                notify_on_end=False,
                is_demo=True,
            )
    demo_mode.activate(user_id)
    await update.effective_message.reply_text(
        t("demo_on", lang),
        reply_markup=_menu(lang, user_id),
    )


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


async def back_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    await update.effective_message.reply_text(
        t("menu_main", lang),
        reply_markup=_menu(lang, user_id),
    )


async def start_twitch_import(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from config import MAX_SUBSCRIPTIONS_PER_OWNER, twitch_oauth_redirect_uri
    from health import create_oauth_state

    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    redirect_uri = twitch_oauth_redirect_uri()
    if not redirect_uri:
        await update.effective_message.reply_text(
            t("import_oauth_unavailable", lang),
            reply_markup=_menu(lang, user_id),
        )
        return
    if len(_subs_for_owner(db, user_id)) >= MAX_SUBSCRIPTIONS_PER_OWNER:
        await update.effective_message.reply_text(
            t("sub_limit", lang, limit=MAX_SUBSCRIPTIONS_PER_OWNER),
            reply_markup=_menu(lang, user_id),
        )
        return
    twitch: TwitchClient = context.application.bot_data["twitch"]
    state = create_oauth_state(user_id, lang)
    url = twitch.build_authorize_url(redirect_uri=redirect_uri, state=state)
    await update.effective_message.reply_text(
        t("import_oauth_prompt", lang),
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(t("import_oauth_button", lang), url=url)]]
        ),
    )


async def _format_subs_overview_lines(
    bot, db: Database, owner_id: int, lang: str
) -> tuple[list[str], list[Subscription]]:
    subs = _subs_for_owner(db, owner_id)
    lines: list[str] = []
    for i, sub in enumerate(subs, 1):
        try:
            chat_display = await _resolve_chat_display_name(bot, sub)
            thread_display = None
            if sub.thread_id:
                thread_display = await _resolve_thread_display_name(
                    bot, sub.chat_id, sub.thread_id
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
        except Exception:
            logger.exception("Failed to format subscription %s for list", sub.id)
            lines.append(f"{'✅' if sub.enabled else '⏸'} #{i} — {sub.twitch_username}")
    return lines, subs


async def list_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    lines, subs = await _format_subs_overview_lines(context.bot, db, user_id, lang)
    if not subs:
        await update.effective_message.reply_text(
            t("no_subs", lang),
            reply_markup=subscriptions_menu(lang),
        )
        return

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    _inline_btn_label(
                        f"{t('toggle_off', lang) if s.enabled else t('toggle_on', lang)} "
                        f"#{i} {s.twitch_username}"
                    ),
                    callback_data=f"toggle:{s.id}",
                )
            ]
            for i, s in enumerate(subs, 1)
        ]
    )
    text = t("subs_list", lang) + "\n".join(lines)
    chunks = _split_telegram_text(text)
    for index, chunk in enumerate(chunks):
        markup = keyboard if index == len(chunks) - 1 else None
        try:
            await update.effective_message.reply_text(chunk, reply_markup=markup)
        except BadRequest:
            logger.exception("Failed to send subscriptions list chunk to %s", user_id)
            if markup is not None:
                await update.effective_message.reply_text(
                    t("subs_list", lang).strip() or "—",
                    reply_markup=markup,
                )
    await update.effective_message.reply_text(
        t("menu_subs", lang),
        reply_markup=subscriptions_menu(lang),
    )


_EDIT_ALERT_TYPE_ORDER = ("live", "category", "upcoming", "end")


def _edit_present_types(subs: list[Subscription]) -> list[str]:
    present = {_alert_type_from_sub(s) for s in subs}
    return [kind for kind in _EDIT_ALERT_TYPE_ORDER if kind in present]


def _edit_type_keyboard(lang: str, types: list[str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t(f"alert_type_{kind}", lang),
                    callback_data=f"edit_type:{kind}",
                )
            ]
            for kind in types
        ]
    )


def _edit_pick_keyboard(
    db: Database, owner_id: int, subs: list[Subscription]
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    _inline_btn_label(
                        f"✏️ #{_owner_sub_number(db, owner_id, s.id)} {s.twitch_username}"
                    ),
                    callback_data=f"edit:{s.id}",
                )
            ]
            for s in subs
        ]
    )


async def edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    subs = _subs_for_owner(db, user_id)
    if not subs:
        await update.effective_message.reply_text(
            t("no_subs_short", lang),
            reply_markup=subscriptions_menu(lang),
        )
        return

    types = _edit_present_types(subs)
    if len(types) == 1:
        text = t("edit_pick", lang)
        markup = _edit_pick_keyboard(db, user_id, subs)
    else:
        text = t("edit_type_pick", lang)
        markup = _edit_type_keyboard(lang, types)
    await update.effective_message.reply_text(text, reply_markup=markup)
    await update.effective_message.reply_text(
        t("menu_subs", lang),
        reply_markup=subscriptions_menu(lang),
    )


async def on_edit_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    kind = query.data.split(":", 1)[1]
    if kind not in _EDIT_ALERT_TYPE_ORDER:
        return
    db: Database = context.application.bot_data["db"]
    filtered = [s for s in _subs_for_owner(db, user_id) if _alert_type_from_sub(s) == kind]
    if not filtered:
        await query.edit_message_text(t("no_subs_short", lang))
        return
    await query.edit_message_text(
        t("edit_pick", lang),
        reply_markup=_edit_pick_keyboard(db, user_id, filtered),
    )


async def _deliver_import_result(
    application: Application,
    owner_id: int,
    lang: str,
    imported: int,
    skipped: int,
    limited: int,
    new_subs: list[Subscription],
    *,
    removed: int = 0,
) -> None:
    from config import MAX_SUBSCRIPTIONS_PER_OWNER

    if imported == 0 and skipped == 0 and limited == 0 and removed == 0:
        await application.bot.send_message(
            owner_id,
            t("import_empty", lang),
            reply_markup=_menu(lang, owner_id),
        )
        return
    limit_note = ""
    if limited:
        limit_note = t(
            "import_limit_note",
            lang,
            limit=MAX_SUBSCRIPTIONS_PER_OWNER,
            limited=limited,
        )
    removed_note = ""
    if removed:
        removed_note = t("import_removed_note", lang, removed=removed)
    header = t(
        "import_success",
        lang,
        imported=imported,
        skipped=skipped,
        limit_note=limit_note,
        removed_note=removed_note,
    )
    markup = _import_result_keyboard(lang, new_subs) if new_subs else None
    await application.bot.send_message(
        owner_id,
        header,
        reply_markup=markup,
        disable_web_page_preview=True,
    )
    await application.bot.send_message(
        owner_id,
        t("menu_main", lang),
        reply_markup=_menu(lang, owner_id),
    )


def _pending_imports(application: Application) -> dict[int, dict]:
    return application.bot_data.setdefault("pending_imports", {})


def _store_pending_import(
    application: Application,
    owner_id: int,
    followed: list[dict],
    token_info: dict[str, str] | None,
) -> None:
    _pending_imports(application)[owner_id] = {
        "followed": followed,
        "token_info": token_info or {},
        "expires": datetime.now(timezone.utc).timestamp() + _PENDING_IMPORT_TTL_SEC,
    }


def _pop_pending_import(application: Application, owner_id: int) -> dict | None:
    pending = _pending_imports(application).pop(owner_id, None)
    if not pending:
        return None
    if pending["expires"] < datetime.now(timezone.utc).timestamp():
        return None
    return pending


def _peek_pending_import(application: Application, owner_id: int) -> dict | None:
    pending = _pending_imports(application).get(owner_id)
    if not pending:
        return None
    if pending["expires"] < datetime.now(timezone.utc).timestamp():
        _pending_imports(application).pop(owner_id, None)
        return None
    return pending


def _next_sync_iso(period_days: int, *, from_dt: datetime | None = None) -> str:
    base = from_dt or datetime.now(timezone.utc)
    return (base + timedelta(days=period_days)).isoformat()


def _format_sync_next(next_sync_at: str) -> str:
    try:
        dt = datetime.fromisoformat(next_sync_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(SCHEDULE_TZ).strftime("%d.%m.%Y %H:%M MSK")
    except ValueError:
        return next_sync_at


async def _run_followed_import(
    application: Application,
    owner_id: int,
    followed: list[dict],
    *,
    prune_missing: bool = False,
    enabled: bool = False,
) -> tuple[int, int, int, int, list[Subscription]]:
    from config import MAX_SUBSCRIPTIONS_PER_OWNER

    db: Database = application.bot_data["db"]
    lang = db.get_user_locale(owner_id) or DEFAULT_LOCALE
    return import_followed_as_subscriptions(
        db,
        owner_id,
        followed,
        template=t("import_default_template", lang),
        limit=MAX_SUBSCRIPTIONS_PER_OWNER,
        prune_missing=prune_missing,
        enabled=enabled,
        is_demo=demo_mode.is_active(owner_id),
    )


async def complete_twitch_import(
    application: Application,
    owner_id: int,
    followed: list[dict] | None,
    error: str | None,
    token_info: dict[str, str] | None = None,
) -> None:
    purpose = (token_info or {}).get("purpose", "import")
    if purpose == "schedule":
        await _complete_schedule_publish(application, owner_id, error, token_info)
        return
    if purpose == "premium":
        from premium_handlers import complete_premium_oauth

        await complete_premium_oauth(application, owner_id, error, token_info)
        return
    db: Database = application.bot_data["db"]
    lang = db.get_user_locale(owner_id) or DEFAULT_LOCALE
    if error:
        key = "import_denied" if error == "access_denied" else "import_failed"
        await application.bot.send_message(
            owner_id,
            t(key, lang),
            reply_markup=_menu(lang, owner_id),
        )
        return
    if followed is None:
        await application.bot.send_message(
            owner_id,
            t("import_failed", lang),
            reply_markup=_menu(lang, owner_id),
        )
        return
    _store_pending_import(application, owner_id, followed, token_info)
    await application.bot.send_message(
        owner_id,
        t("import_mode_prompt", lang),
        reply_markup=import_mode_keyboard(lang),
    )


async def on_import_mode_once(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    owner_id = query.from_user.id
    lang = _user_lang(context, owner_id)
    pending = _pop_pending_import(context.application, owner_id)
    if not pending:
        await query.edit_message_text(t("import_pending_expired", lang))
        return
    await query.edit_message_text(t("import_mode_once", lang))
    imported, skipped, limited, removed, new_subs = await _run_followed_import(
        context.application, owner_id, pending["followed"]
    )
    await _deliver_import_result(
        context.application, owner_id, lang, imported, skipped, limited, new_subs,
        removed=removed,
    )


async def on_import_mode_sync(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    owner_id = query.from_user.id
    lang = _user_lang(context, owner_id)
    db: Database = context.application.bot_data["db"]
    if not await prem.has_feature(context.bot, db, owner_id, "twitch_sync"):
        return await _show_premium_gate(
            update, context, feature="sync", first_step=True
        )
    pending = _peek_pending_import(context.application, owner_id)
    if not pending:
        await query.edit_message_text(t("import_pending_expired", lang))
        return ConversationHandler.END
    refresh = (pending.get("token_info") or {}).get("refresh_token") or ""
    if not refresh:
        pending = _pop_pending_import(context.application, owner_id)
        await query.edit_message_text(t("import_sync_no_refresh", lang))
        imported, skipped, limited, removed, new_subs = await _run_followed_import(
            context.application, owner_id, pending["followed"]
        )
        await _deliver_import_result(
            context.application, owner_id, lang, imported, skipped, limited, new_subs,
            removed=removed,
        )
        return ConversationHandler.END
    context.user_data["sync_days_mode"] = "import"
    await query.edit_message_text(t("import_mode_sync", lang))
    await context.bot.send_message(
        owner_id,
        t("import_sync_days_prompt", lang),
        reply_markup=_wizard(lang, back=False),
    )
    return SYNC_DAYS


async def receive_sync_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    raw = (update.effective_message.text or "").strip()
    if raw in all_menu_buttons() or raw in all_wizard_nav_buttons():
        return ConversationHandler.END
    if not raw.isdigit() or not (_SYNC_PERIOD_MIN <= int(raw) <= _SYNC_PERIOD_MAX):
        await update.effective_message.reply_text(t("import_sync_days_invalid", lang))
        return SYNC_DAYS
    days = int(raw)
    mode = context.user_data.get("sync_days_mode", "import")
    db: Database = context.application.bot_data["db"]
    now = datetime.now(timezone.utc)
    next_at = _next_sync_iso(days, from_dt=now)

    if mode == "settings":
        if not db.set_twitch_sync_period(user_id, days, next_at):
            await update.effective_message.reply_text(
                t("sync_menu_off", lang),
                reply_markup=settings_menu(lang),
            )
            return ConversationHandler.END
        await update.effective_message.reply_text(
            t("sync_period_updated", lang, days=days),
            reply_markup=settings_menu(lang),
        )
        context.user_data.pop("sync_days_mode", None)
        return ConversationHandler.END

    pending = _pop_pending_import(context.application, user_id)
    if not pending:
        await update.effective_message.reply_text(
            t("import_pending_expired", lang),
            reply_markup=_menu(lang, user_id),
        )
        return ConversationHandler.END
    token_info = pending.get("token_info") or {}
    refresh = token_info.get("refresh_token") or ""
    twitch_user_id = token_info.get("twitch_user_id") or ""
    if not refresh or not twitch_user_id:
        await update.effective_message.reply_text(t("import_sync_no_refresh", lang))
        imported, skipped, limited, removed, new_subs = await _run_followed_import(
            context.application, user_id, pending["followed"]
        )
        await _deliver_import_result(
            context.application, user_id, lang, imported, skipped, limited, new_subs,
            removed=removed,
        )
        return ConversationHandler.END

    db.upsert_twitch_sync(
        owner_id=user_id,
        twitch_user_id=twitch_user_id,
        refresh_token=refresh,
        period_days=days,
        next_sync_at=next_at,
        last_sync_at=now.isoformat(),
    )
    await update.effective_message.reply_text(
        t("import_sync_enabled", lang, days=days),
    )
    imported, skipped, limited, removed, new_subs = await _run_followed_import(
        context.application, user_id, pending["followed"], enabled=True
    )
    await _deliver_import_result(
        context.application, user_id, lang, imported, skipped, limited, new_subs,
        removed=removed,
    )
    context.user_data.pop("sync_days_mode", None)
    return ConversationHandler.END


async def open_sync_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    if not await prem.has_feature(context.bot, db, user_id, "twitch_sync"):
        from premium_handlers import send_premium_screen

        await update.effective_message.reply_text(
            t("premium_gate", lang, action=t("premium_gate_action_cancel", lang))
        )
        await send_premium_screen(context.bot, user_id, lang, db)
        return
    sync = db.get_twitch_sync(user_id)
    if not sync or sync.period_days <= 0:
        await update.effective_message.reply_text(
            t("sync_menu_off", lang),
            reply_markup=settings_menu(lang),
        )
        return
    await update.effective_message.reply_text(
        t(
            "sync_menu_on",
            lang,
            days=sync.period_days,
            next_at=_format_sync_next(sync.next_sync_at),
        ),
        reply_markup=sync_settings_keyboard(lang),
    )


async def on_sync_disable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    db.delete_twitch_sync(user_id)
    await query.edit_message_text(t("sync_disabled", lang))


async def on_sync_change_period(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    if not db.get_twitch_sync(user_id):
        await query.edit_message_text(t("sync_menu_off", lang))
        return ConversationHandler.END
    context.user_data["sync_days_mode"] = "settings"
    await query.edit_message_text(t("sync_change_period", lang))
    await context.bot.send_message(
        user_id,
        t("import_sync_days_prompt", lang),
        reply_markup=_wizard(lang, back=False),
    )
    return SYNC_DAYS


async def _sync_owner_follows(
    application: Application,
    row: TwitchSync,
    *,
    advance_schedule: bool = True,
) -> tuple[int, int, int, int] | None:
    """Run one follow sync. Returns (imported, skipped, limited, removed) or None on auth failure."""
    from config import MAX_SUBSCRIPTIONS_PER_OWNER

    db: Database = application.bot_data["db"]
    twitch: TwitchClient = application.bot_data["twitch"]
    lang = db.get_user_locale(row.owner_id) or DEFAULT_LOCALE
    now = datetime.now(timezone.utc)
    try:
        token_data = await asyncio.to_thread(
            twitch.refresh_user_token, row.refresh_token
        )
        access = token_data.get("access_token") or ""
        refresh = token_data.get("refresh_token") or row.refresh_token
        followed = await asyncio.to_thread(
            twitch.get_followed_channels, access, row.twitch_user_id
        )
    except Exception:
        logger.exception("Twitch sync failed for owner %s", row.owner_id)
        db.delete_twitch_sync(row.owner_id)
        if db.get_receive_sync_updates(row.owner_id):
            try:
                await application.bot.send_message(
                    row.owner_id,
                    t("sync_job_failed", lang),
                    reply_markup=_menu(lang, row.owner_id),
                )
            except Exception:
                logger.exception("Cannot notify owner %s about sync failure", row.owner_id)
        return None

    imported, skipped, limited, removed, _new = import_followed_as_subscriptions(
        db,
        row.owner_id,
        followed,
        template=t("import_default_template", lang),
        limit=MAX_SUBSCRIPTIONS_PER_OWNER,
        prune_missing=True,
        enabled=True,
        is_demo=demo_mode.is_active(row.owner_id),
    )
    if advance_schedule and row.period_days > 0:
        next_at = _next_sync_iso(row.period_days, from_dt=now)
    else:
        next_at = row.next_sync_at
    db.update_twitch_sync_tokens(
        row.owner_id,
        refresh,
        last_sync_at=now.isoformat(),
        next_sync_at=next_at,
    )
    return imported, skipped, limited, removed


def _sync_result_notes(
    lang: str, *, limited: int, removed: int
) -> tuple[str, str]:
    from config import MAX_SUBSCRIPTIONS_PER_OWNER

    limit_note = ""
    if limited:
        limit_note = t(
            "import_limit_note",
            lang,
            limit=MAX_SUBSCRIPTIONS_PER_OWNER,
            limited=limited,
        )
    removed_note = ""
    if removed:
        removed_note = t("import_removed_note", lang, removed=removed)
    return limit_note, removed_note


async def sync_twitch_follows(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic job: sync Twitch follows; new channels are enabled by default."""
    db: Database = context.application.bot_data["db"]
    now = datetime.now(timezone.utc)
    due = db.get_due_twitch_syncs(now.isoformat())
    for row in due:
        if row.period_days <= 0:
            continue
        result = await _sync_owner_follows(context.application, row)
        if result is None:
            continue
        imported, skipped, limited, removed = result
        if imported or limited or removed:
            if not db.get_receive_sync_updates(row.owner_id):
                continue
            lang = db.get_user_locale(row.owner_id) or DEFAULT_LOCALE
            limit_note, removed_note = _sync_result_notes(
                lang, limited=limited, removed=removed
            )
            try:
                await context.bot.send_message(
                    row.owner_id,
                    t(
                        "sync_job_done",
                        lang,
                        imported=imported,
                        skipped=skipped,
                        limit_note=limit_note,
                        removed_note=removed_note,
                    ),
                    reply_markup=_menu(lang, row.owner_id),
                )
            except Exception:
                logger.exception("Cannot notify owner %s about sync result", row.owner_id)


async def on_sync_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    sync = db.get_twitch_sync(user_id)
    if not sync or sync.period_days <= 0:
        await query.edit_message_text(t("sync_menu_off", lang))
        return
    await query.edit_message_text(t("sync_now_running", lang))
    result = await _sync_owner_follows(context.application, sync, advance_schedule=True)
    if result is None:
        return
    imported, skipped, limited, removed = result
    limit_note, removed_note = _sync_result_notes(
        lang, limited=limited, removed=removed
    )
    if imported or limited or removed:
        text = t(
            "sync_now_ok",
            lang,
            imported=imported,
            skipped=skipped,
            limit_note=limit_note,
            removed_note=removed_note,
        )
    else:
        text = t("sync_now_none", lang)
    await context.bot.send_message(
        user_id,
        text,
        reply_markup=settings_menu(lang),
    )


async def on_enable_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    owner_id = query.from_user.id
    lang = _user_lang(context, owner_id)
    db: Database = context.application.bot_data["db"]
    count = db.enable_all_subscriptions(
        owner_id, demo=demo_mode.is_active(owner_id)
    )
    if count:
        await query.edit_message_text(t("enable_all_done", lang, count=count))
    else:
        await query.edit_message_text(t("enable_all_none", lang))


async def on_edit_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    sub_id = int(query.data.split(":", 1)[1])
    db: Database = context.application.bot_data["db"]
    sub = db.get_subscription(sub_id, query.from_user.id)
    if not sub or not _sub_in_current_mode(sub, query.from_user.id):
        await query.edit_message_text(t("sub_not_found", lang))
        return
    sub_num = _owner_sub_number(db, query.from_user.id, sub_id)
    await query.edit_message_text(
        t("edit_menu", lang, sub_id=sub_num, username=sub.twitch_username),
        reply_markup=_edit_options_for_sub(sub, lang),
    )


async def start_edit_template(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    sub_id = int(query.data.split(":")[1])
    db: Database = context.application.bot_data["db"]
    sub = db.get_subscription(sub_id, query.from_user.id)
    if not sub:
        await query.edit_message_text(t("sub_not_found", lang))
        return ConversationHandler.END
    sub_num = _owner_sub_number(db, query.from_user.id, sub_id)
    context.user_data["edit_sub_id"] = sub_id
    context.user_data["wizard_edit"] = True
    context.user_data["strip_name_mentions"] = bool(sub.strip_name_mentions)
    await query.edit_message_text("✓")
    await _prompt_edit_template(
        bot=context.bot,
        user_id=query.from_user.id,
        lang=lang,
        sub=sub,
        sub_num=sub_num,
        strip_name_mentions=bool(sub.strip_name_mentions),
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
    if not await prem.has_feature(context.bot, db, query.from_user.id, "ignore_keywords"):
        from premium_handlers import send_premium_screen

        await query.edit_message_text(
            t("premium_gate", lang, action=t("premium_gate_action_cancel", lang))
        )
        await send_premium_screen(context.bot, query.from_user.id, lang, db)
        return ConversationHandler.END
    sub = db.get_subscription(sub_id, query.from_user.id)
    if not sub:
        await query.edit_message_text(t("sub_not_found", lang))
        return ConversationHandler.END
    context.user_data["edit_sub_id"] = sub_id
    context.user_data["wizard_edit"] = True
    context.user_data["use_global_ignore"] = bool(sub.use_global_ignore)
    has_keywords = bool(sub.ignore_keywords.strip())
    context.user_data["ignore_keywords_as_cancel"] = has_keywords
    current = _ignore_keywords_current_label(sub.ignore_keywords, lang)
    if has_keywords:
        current = f"<code>{html.escape(current)}</code>"
    hint = t(
        "edit_ignore_keywords_hint_cancel" if has_keywords else "edit_ignore_keywords_hint_skip",
        lang,
    )
    sub_num = _owner_sub_number(db, query.from_user.id, sub_id)
    await query.edit_message_text("✓")
    await context.bot.send_message(
        query.from_user.id,
        t(
            "edit_ignore_keywords_prompt",
            lang,
            sub_id=sub_num,
            current=current,
            hint=hint,
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=ignore_keywords_keyboard(
            lang,
            as_cancel=has_keywords,
            use_global=bool(sub.use_global_ignore),
        ),
    )
    await _pulse_wizard_keyboard(context.bot, query.from_user.id, lang, back=False)
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
    if not db.update_subscription(
        sub_id,
        owner_id,
        ignore_keywords=keywords,
        use_global_ignore=bool(context.user_data.get("use_global_ignore")),
    ):
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
    if not db.update_subscription(
        sub_id,
        owner_id,
        ignore_keywords="",
        use_global_ignore=bool(context.user_data.get("use_global_ignore")),
    ):
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
    context.user_data["use_global_ignore"] = sub.use_global_ignore
    context.user_data["disable_link_preview"] = sub.disable_link_preview
    context.user_data["suppress_repeat_minutes"] = sub.suppress_repeat_minutes
    context.user_data["alert_type"] = _alert_type_from_sub(sub)
    context.user_data["notify_on_live"] = sub.notify_on_live
    context.user_data["notify_on_end"] = sub.notify_on_end
    context.user_data["notify_on_category_change"] = sub.notify_on_category_change
    context.user_data["delete_other_alerts"] = sub.delete_other_alerts
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
    if field in ("delete_old", "delete_fail", "delete_other") and sub.dest_type == "dm":
        await query.edit_message_text(
            t(
                "edit_menu",
                lang,
                sub_id=_owner_sub_number(db, query.from_user.id, sub_id),
                username=sub.twitch_username,
            ),
            reply_markup=_edit_options_for_sub(sub, lang),
        )
        return
    if field == "delete_other" and (
        not sub.notify_on_category_change or not sub.delete_previous
    ):
        await query.edit_message_text(
            t(
                "edit_menu",
                lang,
                sub_id=_owner_sub_number(db, query.from_user.id, sub_id),
                username=sub.twitch_username,
            ),
            reply_markup=_edit_options_for_sub(sub, lang),
        )
        return
    if field in ("delete_old", "delete_fail", "delete_other", "repeat"):
        feat = "repeat" if field == "repeat" else "delete_prev"
        if not await prem.has_feature(context.bot, db, query.from_user.id, feat):
            from premium_handlers import send_premium_screen

            await query.edit_message_text(
                t("premium_gate", lang, action=t("premium_gate_action_cancel", lang))
            )
            await send_premium_screen(context.bot, query.from_user.id, lang, db)
            return
    if field == "delete_old" and sub.notify_on_category_change:
        menu_key = "edit_delete_old_menu_category"
    else:
        menu_keys = {
            "delete_old": "edit_delete_old_menu",
            "delete_fail": "edit_delete_fail_menu",
            "delete_other": "edit_delete_other_menu",
            "preview": "edit_preview_menu",
            "repeat": "edit_repeat_menu",
        }
        menu_key = menu_keys[field]
    await query.edit_message_text(
        t(menu_key, lang),
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
    if field in ("delete_old", "delete_fail", "delete_other") and sub.dest_type == "dm":
        await query.edit_message_text(t("sub_not_found", lang))
        return
    if field == "delete_old":
        kwargs: dict = {"delete_previous": value}
        if not value:
            kwargs["notify_delete_fail"] = False
            kwargs["delete_other_alerts"] = False
    elif field == "delete_fail":
        if not sub.delete_previous:
            await query.edit_message_text(t("sub_not_found", lang))
            return
        kwargs = {"notify_delete_fail": value}
    elif field == "delete_other":
        if not sub.notify_on_category_change or not sub.delete_previous:
            await query.edit_message_text(t("sub_not_found", lang))
            return
        kwargs = {"delete_other_alerts": value}
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
    subs = _subs_for_owner(db, user_id)
    if not subs:
        await update.effective_message.reply_text(
            t("no_subs_short", lang),
            reply_markup=subscriptions_menu(lang),
        )
        return
    context.user_data["delete_selected"] = set()
    await update.effective_message.reply_text(
        t("delete_pick", lang),
        reply_markup=_delete_pick_keyboard(lang, subs, set()),
    )
    await update.effective_message.reply_text(
        t("menu_subs", lang),
        reply_markup=subscriptions_menu(lang),
    )


def _delete_pick_keyboard(
    lang: str, subs: list[Subscription], selected: set[int]
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i, s in enumerate(subs, 1):
        mark = "✅ " if s.id in selected else ""
        rows.append(
            [
                InlineKeyboardButton(
                    f"{mark}🗑 #{i} {s.twitch_username}",
                    callback_data=f"delete_sel:{s.id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                t("delete_go", lang, count=len(selected)),
                callback_data="delete_go",
            )
        ]
    )
    if selected:
        rows.append(
            [InlineKeyboardButton(t("delete_clear", lang), callback_data="delete_clear")]
        )
    return InlineKeyboardMarkup(rows)


async def on_delete_sel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    sub_id = int(query.data.split(":", 1)[1])
    selected: set[int] = context.user_data.setdefault("delete_selected", set())
    if sub_id in selected:
        selected.discard(sub_id)
    else:
        selected.add(sub_id)
    db: Database = context.application.bot_data["db"]
    subs = _subs_for_owner(db, user_id)
    await query.edit_message_reply_markup(
        reply_markup=_delete_pick_keyboard(lang, subs, selected)
    )


async def on_delete_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    context.user_data["delete_selected"] = set()
    db: Database = context.application.bot_data["db"]
    subs = _subs_for_owner(db, user_id)
    await query.edit_message_reply_markup(
        reply_markup=_delete_pick_keyboard(lang, subs, set())
    )


async def on_delete_go(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    selected: set[int] = set(context.user_data.get("delete_selected") or ())
    if not selected:
        await query.answer(t("delete_none", lang), show_alert=True)
        return
    await query.answer()
    db: Database = context.application.bot_data["db"]
    deleted = 0
    for sub_id in list(selected):
        sub = db.get_subscription(sub_id, user_id)
        if sub is None or not _sub_in_current_mode(sub, user_id):
            continue
        if db.delete_subscription(sub_id, user_id):
            deleted += 1
    context.user_data["delete_selected"] = set()
    await query.edit_message_text(t("subs_deleted", lang, count=deleted))


async def on_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    sub_id = int(query.data.split(":", 1)[1])
    db: Database = context.application.bot_data["db"]
    sub = db.get_subscription_by_id(sub_id)
    if (
        sub is None
        or sub.owner_id != query.from_user.id
        or not _sub_in_current_mode(sub, query.from_user.id)
    ):
        await query.edit_message_text(t("sub_not_found", lang))
        return
    if not sub.enabled and getattr(sub, "trial_paused", False):
        if not await prem.has_premium(context.bot, db, query.from_user.id):
            from premium_handlers import send_premium_screen

            await query.edit_message_text(t("premium_trial_paused_enable", lang))
            await send_premium_screen(context.bot, query.from_user.id, lang, db)
            return
    if not sub.enabled and not await prem.can_enable_more_async(context.bot, db, query.from_user.id):
        from premium_handlers import send_premium_screen

        await query.edit_message_text(
            t("premium_active_limit", lang, limit=prem.free_active_limit())
        )
        await send_premium_screen(context.bot, query.from_user.id, lang, db)
        return
    new_state = db.toggle_subscription(sub_id, query.from_user.id)
    if new_state is None:
        await query.edit_message_text(t("sub_not_found", lang))
        return
    key = "sub_enabled" if new_state else "sub_disabled"
    sub_num = _owner_sub_number(db, query.from_user.id, sub_id)
    await query.edit_message_text(t(key, lang, sub_id=sub_num))


async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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


async def _send_dm_html(
    bot,
    db: Database,
    uid: int,
    message: str,
    *,
    reply_markup=None,
) -> str:
    """Send one DM. Returns 'sent', 'blocked', or 'failed'."""
    kwargs: dict = {}
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup
    try:
        try:
            await bot.send_message(
                uid, message, parse_mode=ParseMode.HTML, **kwargs
            )
        except BadRequest:
            # Plain legacy text or translation broke tags — send without parse_mode.
            await bot.send_message(uid, message, **kwargs)
        return "sent"
    except RetryAfter as exc:
        await asyncio.sleep(float(exc.retry_after) + 0.5)
        try:
            try:
                await bot.send_message(
                    uid, message, parse_mode=ParseMode.HTML, **kwargs
                )
            except BadRequest:
                await bot.send_message(uid, message, **kwargs)
            return "sent"
        except Forbidden as retry_exc:
            if "blocked" in str(retry_exc).lower():
                db.set_bot_blocked(uid, True)
                return "blocked"
            logger.warning("Broadcast to %s failed after RetryAfter: %s", uid, retry_exc)
            return "failed"
        except (BadRequest, RetryAfter) as retry_exc:
            logger.warning("Broadcast to %s failed after RetryAfter: %s", uid, retry_exc)
            return "failed"
    except Forbidden as exc:
        if "blocked" in str(exc).lower():
            db.set_bot_blocked(uid, True)
            return "blocked"
        logger.warning("Broadcast to %s failed: %s", uid, exc)
        return "failed"
    except BadRequest as exc:
        logger.warning("Broadcast to %s failed: %s", uid, exc)
        return "failed"


async def _send_admin_broadcast(
    context: ContextTypes.DEFAULT_TYPE,
    msg_type: str,
    text: str,
    *,
    source_lang: str | None = None,
) -> tuple[int, int, int]:
    db: Database = context.application.bot_data["db"]
    if msg_type == "bot_update":
        user_ids = db.get_bot_update_recipients()
    elif msg_type == "availability":
        user_ids = db.get_availability_recipients()
    elif msg_type == "other":
        user_ids = db.get_other_recipients()
    else:
        user_ids = [
            uid for uid in db.get_notify_user_ids() if not db.is_bot_blocked(uid)
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
    # Bot-update broadcasts also push the current main ReplyKeyboard.
    attach_menu = msg_type == "bot_update"
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
        markup = _menu(locale, uid) if attach_menu else None
        result = await _send_dm_html(
            context.bot, db, uid, message, reply_markup=markup
        )
        if result == "sent":
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(_BROADCAST_SEND_PAUSE)
    return sent, failed, len(user_ids)


async def _report_broadcast_done(
    context: ContextTypes.DEFAULT_TYPE,
    admin_id: int,
    *,
    sent: int,
    failed: int,
    total: int,
) -> None:
    db: Database = context.application.bot_data["db"]
    lang = db.get_user_locale(admin_id) or DEFAULT_LOCALE
    blocked_users = db.get_bot_stats().blocked_users
    text = t(
        "broadcast_done",
        lang,
        sent=sent,
        failed=failed,
        blocked_users=blocked_users,
        total=total,
    )
    try:
        await context.bot.send_message(admin_id, text)
    except (BadRequest, Forbidden) as exc:
        logger.warning("Cannot send broadcast stats to admin %s: %s", admin_id, exc)


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
        # Offload to job queue so the conversation handler returns immediately
        # and the bot keeps processing other updates while the mass send runs.
        scheduled_at = datetime.now(timezone.utc).isoformat()
        broadcast_id = db.add_scheduled_broadcast(msg_type, text, scheduled_at, user_id)
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
        broadcast_id = db.add_scheduled_broadcast(msg_type, text, scheduled_at, user_id)
        when = (
            datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
            - datetime.now(timezone.utc)
        ).total_seconds()
        context.user_data.clear()
        if when <= 0:
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
        else:
            context.job_queue.run_once(
                _run_scheduled_broadcast,
                when=when,
                data={"broadcast_id": broadcast_id},
                name=_broadcast_job_name(broadcast_id),
            )
            when_local = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00")).astimezone(
                SCHEDULE_TZ
            )
            when_label = when_local.strftime("%d.%m.%Y %H:%M MSK")
            await query.edit_message_text(
                t("broadcast_scheduled", lang, when=when_label)
            )
        await context.bot.send_message(user_id, t("menu_broadcast", lang), reply_markup=broadcast_menu(lang))
        return ConversationHandler.END

    return ADMIN_MSG_SCHEDULE


async def _run_scheduled_broadcast(context: ContextTypes.DEFAULT_TYPE) -> None:
    broadcast_id = context.job.data["broadcast_id"]
    bot_data = context.application.bot_data
    if not _claim_broadcast_send(bot_data, broadcast_id):
        return
    db: Database = bot_data["db"]
    try:
        pending = db.get_pending_scheduled_broadcasts()
        item = next((b for b in pending if b.id == broadcast_id), None)
        if not item:
            unsent = db.get_unsent_scheduled_broadcasts()
            item = next((b for b in unsent if b.id == broadcast_id), None)
        if not item:
            return
        source_lang = db.get_user_locale(item.created_by) or DEFAULT_LOCALE
        sent, failed, total = await _send_admin_broadcast(
            context, item.msg_type, item.text, source_lang=source_lang
        )
        db.mark_scheduled_broadcast_sent(broadcast_id)
        await _report_broadcast_done(
            context,
            item.created_by,
            sent=sent,
            failed=failed,
            total=total,
        )
    finally:
        _release_broadcast_send(bot_data, broadcast_id)


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
    if plain in all_menu_buttons() or plain in all_wizard_nav_buttons():
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
            _cancel_broadcast_job(context.application.job_queue, int(broadcast_id))
            try:
                await query.edit_message_text(t("broadcast_started", lang))
            except BadRequest:
                pass
            context.application.job_queue.run_once(
                _run_scheduled_broadcast,
                when=0,
                data={"broadcast_id": int(broadcast_id)},
                name=_broadcast_job_name(int(broadcast_id)),
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


def _alert_history_type_label(alert_type: str, lang: str) -> str:
    key = {
        "live": "alert_history_type_live",
        "end": "alert_history_type_end",
        "category": "alert_history_type_category",
        "schedule": "alert_history_type_schedule",
    }.get(alert_type)
    return t(key, lang) if key else alert_type


def _parse_alert_sent_at(sent_at: str) -> datetime:
    raw = (sent_at or "").strip()
    if not raw:
        return datetime.now(timezone.utc)
    # SQLite datetime('now') is UTC without timezone marker.
    if "T" not in raw and " " in raw:
        raw = raw.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def show_alert_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    deep = await prem.has_feature(context.bot, db, user_id, "alert_history")
    days = (
        prem.ALERT_HISTORY_PREMIUM_DAYS if deep else prem.ALERT_HISTORY_FREE_DAYS
    )
    since = datetime.now(timezone.utc) - timedelta(days=days)
    items = db.list_alert_history(user_id, since=since)
    more_kb = (
        None
        if deep
        else InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        btn("alert_history_more", lang),
                        callback_data="alert_history:more",
                    )
                ]
            ]
        )
    )
    if not items:
        await update.effective_message.reply_text(
            t("alert_history_empty", lang),
            reply_markup=more_kb,
            disable_web_page_preview=True,
        )
        return

    title = t("alert_history_title", lang, days=days, n=len(items))
    blocks: list[str] = []
    last_day: date | None = None
    for item in items:
        local = _parse_alert_sent_at(item.sent_at).astimezone(SCHEDULE_TZ)
        parts: list[str] = []
        if last_day != local.date():
            parts.append(
                t(
                    "alert_history_day",
                    lang,
                    date=format_stream_schedule_prompt_date(local.date(), lang),
                )
            )
            last_day = local.date()
        parts.append(
            t(
                "alert_history_line",
                lang,
                time=local.strftime("%H:%M"),
                username=item.twitch_username,
            )
        )
        body = (item.message_text or "").strip()
        if not body:
            body = _alert_history_type_label(item.alert_type, lang)
        parts.append(t("alert_history_body", lang, text=body))
        blocks.append("\n".join(parts))

    chunks: list[str] = []
    buf = title
    for block in blocks:
        candidate = f"{buf}\n\n{block}" if buf else block
        if buf and len(candidate) > 4000:
            chunks.append(buf if len(buf) <= 4000 else buf[:3990].rstrip() + "\n…")
            buf = block if len(block) <= 4000 else block[:3990].rstrip() + "\n…"
        else:
            buf = candidate if len(candidate) <= 4000 else candidate[:3990].rstrip() + "\n…"
    if buf:
        chunks.append(buf)

    for i, chunk in enumerate(chunks):
        kb = more_kb if i == len(chunks) - 1 else None
        await update.effective_message.reply_text(
            chunk,
            reply_markup=kb,
            disable_web_page_preview=True,
        )


async def on_alert_history_more(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    from premium_handlers import send_premium_screen

    await send_premium_screen(context.bot, user_id, lang, db)


async def open_partner_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from config import REFERRAL_COMMISSION_PERCENT, REFERRAL_WITHDRAW_MIN_STARS

    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    await update.effective_message.reply_text(
        t(
            "partner_intro",
            lang,
            percent=REFERRAL_COMMISSION_PERCENT,
            min_stars=REFERRAL_WITHDRAW_MIN_STARS,
        ),
        reply_markup=partner_menu(lang),
    )


async def partner_show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    stats = db.get_referral_stats(user_id)
    await update.effective_message.reply_text(
        t(
            "partner_stats",
            lang,
            invited=stats.invited,
            payments=stats.payments,
            available=stats.available_stars,
        ),
        reply_markup=partner_menu(lang),
    )


async def partner_show_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    username = context.bot.username or ""
    if not username:
        me = await context.bot.get_me()
        username = me.username or ""
    link = f"https://t.me/{username}?start=ref_{user_id}" if username else f"ref_{user_id}"
    await update.effective_message.reply_text(
        t("partner_link", lang, link=link),
        reply_markup=partner_menu(lang),
    )


async def partner_request_withdraw(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    from config import ADMIN_USER_IDS, REFERRAL_WITHDRAW_MIN_STARS

    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    stats = db.get_referral_stats(user_id)
    available = stats.available_stars
    if available < REFERRAL_WITHDRAW_MIN_STARS:
        await update.effective_message.reply_text(
            t(
                "partner_withdraw_min",
                lang,
                min_stars=REFERRAL_WITHDRAW_MIN_STARS,
                available=available,
            ),
            reply_markup=partner_menu(lang),
        )
        return
    request_id = db.request_referral_withdrawal(user_id, available)
    if request_id is None:
        await update.effective_message.reply_text(
            t(
                "partner_withdraw_min",
                lang,
                min_stars=REFERRAL_WITHDRAW_MIN_STARS,
                available=available,
            ),
            reply_markup=partner_menu(lang),
        )
        return
    await update.effective_message.reply_text(
        t("partner_withdraw_ok", lang, id=request_id, amount=available),
        reply_markup=partner_menu(lang),
    )
    for admin_id in ADMIN_USER_IDS:
        admin_lang = db.get_user_locale(admin_id) or DEFAULT_LOCALE
        try:
            await context.bot.send_message(
                admin_id,
                t(
                    "partner_withdraw_admin",
                    admin_lang,
                    id=request_id,
                    user_id=user_id,
                    amount=available,
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=withdrawal_actions_keyboard(request_id, admin_lang),
            )
        except (BadRequest, Forbidden) as exc:
            logger.warning("Cannot notify admin %s about withdrawal: %s", admin_id, exc)


def _partner_wd_status_label(status: str, lang: str) -> str:
    mapping = {
        "pending": "partner_wd_status_pending",
        "paid": "partner_wd_status_paid",
        "rejected": "partner_wd_status_rejected",
    }
    key = mapping.get(status)
    return t(key, lang) if key else status


async def partner_show_withdrawals(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    items = db.list_referral_withdrawals(user_id, limit=20)
    if not items:
        await update.effective_message.reply_text(
            t("partner_withdrawals_empty", lang),
            reply_markup=partner_menu(lang),
        )
        return
    lines = [t("partner_withdrawals_title", lang)]
    for item in items:
        lines.append(
            t(
                "partner_withdrawal_line",
                lang,
                id=item.id,
                amount=item.amount,
                status=_partner_wd_status_label(item.status, lang),
            )
        )
    await update.effective_message.reply_text(
        "\n".join(lines),
        reply_markup=partner_menu(lang),
    )


async def admin_show_withdrawals(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user_id = update.effective_user.id
    if not _can_use_admin_tools(user_id):
        return
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    items = db.list_pending_referral_withdrawals()
    if not items:
        await update.effective_message.reply_text(
            t("admin_withdrawals_empty", lang),
            reply_markup=admin_menu(lang),
        )
        return
    await update.effective_message.reply_text(
        t("admin_withdrawals_title", lang),
        reply_markup=admin_menu(lang),
    )
    for item in items:
        await update.effective_message.reply_text(
            t(
                "admin_withdrawal_line",
                lang,
                id=item.id,
                user_id=item.user_id,
                amount=item.amount,
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=withdrawal_actions_keyboard(item.id, lang),
        )


async def on_referral_withdrawal_action(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    admin_id = query.from_user.id
    if not _is_admin(admin_id):
        return
    lang = _user_lang(context, admin_id)
    parts = (query.data or "").split(":")
    if len(parts) != 3:
        return
    _, action, raw_id = parts
    try:
        withdrawal_id = int(raw_id)
    except ValueError:
        return
    new_status = "paid" if action == "paid" else "rejected" if action == "reject" else ""
    if not new_status:
        return
    db: Database = context.application.bot_data["db"]
    item = db.resolve_referral_withdrawal(withdrawal_id, new_status)
    if item is None:
        existing = db.get_referral_withdrawal(withdrawal_id)
        status_label = (
            _partner_wd_status_label(existing.status, lang) if existing else "?"
        )
        await query.edit_message_text(
            t("admin_wd_already", lang, id=withdrawal_id, status=status_label)
        )
        return
    admin_key = (
        "admin_wd_resolved_paid" if new_status == "paid" else "admin_wd_resolved_rejected"
    )
    await query.edit_message_text(t(admin_key, lang, id=item.id))
    user_lang = db.get_user_locale(item.user_id) or DEFAULT_LOCALE
    user_key = (
        "partner_wd_paid_user" if new_status == "paid" else "partner_wd_rejected_user"
    )
    try:
        await context.bot.send_message(
            item.user_id,
            t(user_key, user_lang, id=item.id, amount=item.amount),
        )
    except (BadRequest, Forbidden) as exc:
        logger.warning(
            "Cannot notify user %s about withdrawal %s: %s",
            item.user_id,
            item.id,
            exc,
        )


async def open_premium_from_settings(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    from premium_handlers import open_premium_menu

    await open_premium_menu(update, context)


async def on_premium_callback_router(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    from premium_handlers import on_premium_callback

    await on_premium_callback(update, context)


async def precheckout_premium_router(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    from premium_handlers import precheckout_premium

    await precheckout_premium(update, context)


async def successful_premium_payment_router(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    from premium_handlers import successful_premium_payment

    await successful_premium_payment(update, context)


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
            other_enabled=db.get_receive_other_updates(user_id),
            sync_enabled=db.get_receive_sync_updates(user_id),
        ),
    )


async def start_ignored_words(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    if not await prem.has_feature(context.bot, db, user_id, "ignore_keywords"):
        from premium_handlers import send_premium_screen

        await update.effective_message.reply_text(
            t("premium_gate", lang, action=t("premium_gate_action_cancel", lang))
        )
        await send_premium_screen(context.bot, user_id, lang, db)
        return ConversationHandler.END
    current_raw = db.get_global_ignore_keywords(user_id)
    has_words = bool(current_raw.strip())
    current = _ignore_keywords_current_label(current_raw, lang)
    if has_words:
        current = f"<code>{html.escape(current)}</code>"
    await update.effective_message.reply_text(
        t(
            "ignored_words_prompt",
            lang,
            current=current,
            hint=t(
                "ignored_words_hint_edit" if has_words else "ignored_words_hint_empty",
                lang,
            ),
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=ignored_words_keyboard(lang, has_words=has_words),
    )
    await _pulse_wizard_keyboard(context.bot, user_id, lang, back=False)
    return GLOBAL_IGNORE_KEYWORDS


async def receive_ignored_words(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    text = (update.effective_message.text or "").strip()
    if text in all_wizard_nav_buttons():
        await update.effective_message.reply_text(
            t("menu_settings", lang),
            reply_markup=settings_menu(lang),
        )
        context.user_data.clear()
        return ConversationHandler.END
    if text in all_menu_buttons():
        await update.effective_message.reply_text(t("finish_setup_first", lang))
        return GLOBAL_IGNORE_KEYWORDS
    added = normalize_ignore_keywords(text)
    if not added:
        await update.effective_message.reply_text(t("ignored_words_hint_empty", lang))
        return GLOBAL_IGNORE_KEYWORDS
    db: Database = context.application.bot_data["db"]
    keywords = merge_ignore_keywords(db.get_global_ignore_keywords(user_id), added)
    db.set_global_ignore_keywords(user_id, keywords)
    await update.effective_message.reply_text(
        t("ignored_words_saved", lang),
        reply_markup=settings_menu(lang),
    )
    context.user_data.clear()
    return ConversationHandler.END


async def receive_ignored_words_clear(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    db.set_global_ignore_keywords(user_id, "")
    await query.edit_message_text("✓")
    await context.bot.send_message(
        user_id,
        t("ignored_words_cleared", lang),
        reply_markup=settings_menu(lang),
    )
    context.user_data.clear()
    return ConversationHandler.END


async def receive_ignored_words_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    await query.edit_message_text("✓")
    await context.bot.send_message(
        user_id,
        t("menu_settings", lang),
        reply_markup=settings_menu(lang),
    )
    context.user_data.clear()
    return ConversationHandler.END


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
            other_enabled=db.get_receive_other_updates(user_id),
            sync_enabled=db.get_receive_sync_updates(user_id),
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


async def on_sys_other_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    db.set_receive_other_updates(user_id, not db.get_receive_other_updates(user_id))
    await _refresh_sys_notifications_menu(query, context, lang, user_id)


async def on_sys_sync_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    db.set_receive_sync_updates(user_id, not db.get_receive_sync_updates(user_id))
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
        premium_paid=stats.premium_paid,
        sys_updates=stats.sys_updates,
        sys_availability=stats.sys_availability,
        sys_other=stats.sys_other,
        blocked_users=stats.blocked_users,
        locale_en=stats.locale_en,
        locale_ru=stats.locale_ru,
        locale_unset=stats.locale_unset,
    )


async def admin_show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not _can_use_admin_tools(user_id):
        return
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    stats = db.get_bot_stats()
    await update.effective_message.reply_text(
        _format_stats(stats, lang),
        reply_markup=admin_menu(lang),
    )


async def _send_delayed_notification(context: ContextTypes.DEFAULT_TYPE) -> None:
    job_data = context.job.data or {}
    sub_id = job_data["sub_id"]
    silent_offline = bool(job_data.get("silent_offline"))
    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    sub = db.get_subscription_by_id(sub_id)
    if not sub or not sub.enabled or not sub.notify_on_live:
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
        if silent_offline:
            return
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
    if should_ignore_stream(_effective_ignore_keywords(sub, db), game, title):
        return
    text = _render_sub_template(
        sub, username, game, title, twitch=twitch, stream=stream
    )
    await _send_notification(context.bot, db, sub, text, alert_type="live")


async def _send_delayed_end_notification(context: ContextTypes.DEFAULT_TYPE) -> None:
    sub_id = context.job.data["sub_id"]
    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    sub = db.get_subscription_by_id(sub_id)
    if not sub or not sub.enabled or not sub.notify_on_end:
        return

    try:
        live_streams = await asyncio.to_thread(
            twitch.get_live_streams, [sub.twitch_user_id]
        )
    except Exception:
        logger.exception("Twitch poll failed for delayed end notification sub %s", sub_id)
        return

    if sub.twitch_user_id in live_streams:
        return
    if is_on_notify_cooldown(sub):
        return
    text = _render_sub_template(sub, sub.twitch_username, "—", "—", twitch=twitch)
    await _send_notification(context.bot, db, sub, text, alert_type="end")


async def _send_delayed_category_notification(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    sub_id = context.job.data["sub_id"]
    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    sub = db.get_subscription_by_id(sub_id)
    if not sub or not sub.enabled or not sub.notify_on_category_change:
        return

    try:
        live_streams = await asyncio.to_thread(
            twitch.get_live_streams, [sub.twitch_user_id]
        )
    except Exception:
        logger.exception(
            "Twitch poll failed for delayed category notification sub %s", sub_id
        )
        return

    if sub.twitch_user_id not in live_streams:
        return
    if is_on_notify_cooldown(sub):
        return
    stream = live_streams[sub.twitch_user_id]
    username = stream.get("user_login", stream.get("user_name", ""))
    game = stream.get("game_name", "")
    title = stream.get("title", "")
    if should_ignore_stream(_effective_ignore_keywords(sub, db), game, title):
        return
    text = _render_sub_template(
        sub, username, game, title, twitch=twitch, stream=stream
    )
    await _send_notification(context.bot, db, sub, text, alert_type="category")


async def check_streams(context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    last_live: dict[str, bool] = context.application.bot_data.setdefault("last_live", {})
    last_games: dict[str, str] = context.application.bot_data.setdefault("last_games", {})
    # After restart memory is empty; first successful poll only seeds state so
    # already-live streams are not treated as fresh starts.
    primed = bool(context.application.bot_data.get("last_live_primed"))

    user_ids = db.get_unique_twitch_user_ids()
    if not user_ids:
        return

    try:
        live_streams = await asyncio.to_thread(twitch.get_live_streams, user_ids)
    except Exception:
        logger.exception("Twitch poll failed")
        return

    went_live, went_offline = live_transitions(
        last_live, user_ids, live_streams, primed=primed
    )
    category_changed = category_change_events(
        last_games, user_ids, live_streams, primed=primed
    )
    context.application.bot_data["last_live_primed"] = True

    for uid in went_live:
        stream = live_streams[uid]
        username = stream.get("user_login", stream.get("user_name", ""))
        game = stream.get("game_name", "")
        title = stream.get("title", "")
        for sub in db.get_enabled_by_twitch_user_id(uid):
            if not sub.notify_on_live:
                continue
            if is_on_notify_cooldown(sub):
                continue
            if should_ignore_stream(_effective_ignore_keywords(sub, db), game, title):
                continue
            if sub.delay_minutes > 0:
                context.job_queue.run_once(
                    _send_delayed_notification,
                    when=sub.delay_minutes * 60,
                    data={"sub_id": sub.id},
                    name=f"delay_{sub.id}",
                )
                continue
            # Helix often returns empty game_name for a few seconds after go-live.
            if needs_live_game_recheck(game, sub.delay_minutes):
                context.job_queue.run_once(
                    _send_delayed_notification,
                    when=LIVE_GAME_RECHECK_SECONDS,
                    data={"sub_id": sub.id, "silent_offline": True},
                    name=f"live_game_{sub.id}",
                )
                continue
            text = _render_sub_template(
                sub, username, game, title, twitch=twitch, stream=stream
            )
            await _send_notification(context.bot, db, sub, text, alert_type="live")

    for uid in went_offline:
        for sub in db.get_enabled_by_twitch_user_id(uid):
            if not sub.notify_on_end:
                continue
            if is_on_notify_cooldown(sub):
                continue
            if sub.delay_minutes > 0:
                context.job_queue.run_once(
                    _send_delayed_end_notification,
                    when=sub.delay_minutes * 60,
                    data={"sub_id": sub.id},
                    name=f"delay_end_{sub.id}",
                )
                continue
            text = _render_sub_template(
                sub, sub.twitch_username, "—", "—", twitch=twitch
            )
            await _send_notification(context.bot, db, sub, text, alert_type="end")

    for uid in category_changed:
        stream = live_streams[uid]
        username = stream.get("user_login", stream.get("user_name", ""))
        game = stream.get("game_name", "")
        title = stream.get("title", "")
        for sub in db.get_enabled_by_twitch_user_id(uid):
            if not sub.notify_on_category_change:
                continue
            if is_on_notify_cooldown(sub):
                continue
            if should_ignore_stream(_effective_ignore_keywords(sub, db), game, title):
                continue
            if sub.delay_minutes > 0:
                game_id = str(stream.get("game_id") or "")
                context.job_queue.run_once(
                    _send_delayed_category_notification,
                    when=sub.delay_minutes * 60,
                    data={"sub_id": sub.id},
                    name=f"delay_cat_{sub.id}_{game_id}",
                )
                continue
            text = _render_sub_template(
                sub, username, game, title, twitch=twitch, stream=stream
            )
            await _send_notification(
                context.bot, db, sub, text, alert_type="category"
            )


def _parse_segment_start(segment: dict) -> datetime | None:
    raw = segment.get("start_time")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def check_schedule_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    user_ids = db.get_unique_schedule_reminder_twitch_ids()
    if not user_ids:
        return
    now = datetime.now(timezone.utc)
    for uid in user_ids:
        try:
            segments = await asyncio.to_thread(twitch.get_schedule_segments, uid)
        except Exception:
            logger.exception("Twitch schedule poll failed for %s", uid)
            continue
        for sub in db.get_enabled_by_twitch_user_id(uid):
            remind_before = int(sub.schedule_reminder_minutes or 0)
            if remind_before <= 0:
                continue
            for segment in segments:
                if segment.get("canceled_until"):
                    continue
                seg_id = str(segment.get("id") or "")
                if not seg_id or seg_id == sub.last_schedule_reminder_segment_id:
                    continue
                start = _parse_segment_start(segment)
                if start is None or start <= now:
                    continue
                minutes_left = int((start - now).total_seconds() // 60)
                if minutes_left > remind_before:
                    continue
                username = sub.twitch_username
                title = str(segment.get("title") or "—")
                category = segment.get("category") or {}
                game = (
                    str(category.get("name") or "")
                    if isinstance(category, dict)
                    else ""
                )
                template = (sub.message_template or "").strip()
                if not template:
                    continue
                text = _render_sub_template(
                    sub,
                    username,
                    game,
                    title,
                    twitch=twitch,
                    extra={"minutes": str(max(1, minutes_left))},
                )
                ok = await _send_notification(
                    context.bot, db, sub, text, alert_type="schedule"
                )
                if not ok:
                    continue
                db.set_last_schedule_reminder_segment(sub.id, seg_id)
                break


# Helix can omit category right after go-live; wait once, then send with whatever we get.
LIVE_GAME_RECHECK_SECONDS = 20


def needs_live_game_recheck(game: str, delay_minutes: int) -> bool:
    """True when live alert should wait briefly for Helix to fill game_name."""
    return int(delay_minutes or 0) <= 0 and not (game or "").strip()


def live_transitions(
    last_live: dict[str, bool],
    user_ids: list[str],
    live_ids: set[str] | dict,
    *,
    primed: bool,
) -> tuple[list[str], list[str]]:
    """Update last_live; return (went_live, went_offline) when primed."""
    went_live: list[str] = []
    went_offline: list[str] = []
    for uid in user_ids:
        is_live = uid in live_ids
        was_live = last_live.get(uid, False)
        if primed:
            if is_live and not was_live:
                went_live.append(uid)
            if was_live and not is_live:
                went_offline.append(uid)
        last_live[uid] = is_live
    return went_live, went_offline


def category_change_events(
    last_games: dict[str, str],
    user_ids: list[str],
    live_streams: dict[str, dict],
    *,
    primed: bool,
) -> list[str]:
    """Update last_games; return uids whose game_id changed while live (when primed)."""
    changed: list[str] = []
    for uid in user_ids:
        if uid not in live_streams:
            last_games.pop(uid, None)
            continue
        game_id = str(live_streams[uid].get("game_id") or "")
        prev = last_games.get(uid)
        if primed and prev is not None and game_id != prev:
            changed.append(uid)
        last_games[uid] = game_id
    return changed


async def process_scheduled_broadcasts(context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    bot_data = context.application.bot_data
    for item in db.get_pending_scheduled_broadcasts():
        if not _claim_broadcast_send(bot_data, item.id):
            continue
        try:
            source_lang = db.get_user_locale(item.created_by) or DEFAULT_LOCALE
            sent, failed, total = await _send_admin_broadcast(
                context, item.msg_type, item.text, source_lang=source_lang
            )
            db.mark_scheduled_broadcast_sent(item.id)
            await _report_broadcast_done(
                context,
                item.created_by,
                sent=sent,
                failed=failed,
                total=total,
            )
        finally:
            _release_broadcast_send(bot_data, item.id)


def _twitch_status_label(lang: str, status: str) -> str:
    key = _TWITCH_COMPONENT_KEYS.get(status)
    if key:
        return t(key, lang)
    return status.replace("_", " ")


def _twitch_indicator_label(lang: str, indicator: str) -> str:
    key = _TWITCH_INDICATOR_KEYS.get(indicator)
    if key:
        return t(key, lang)
    return indicator


def _format_twitch_status_message(lang: str, summary: dict) -> str:
    status = summary.get("status") or {}
    indicator = str(status.get("indicator") or "none")
    headline = _twitch_indicator_label(lang, indicator)
    lines = [
        t("twitch_status_title", lang),
        "",
        headline,
    ]
    affected = [
        comp
        for comp in summary.get("components") or []
        if isinstance(comp, dict)
        and not comp.get("group")
        and str(comp.get("status") or "operational") != "operational"
    ]
    if affected:
        lines.append("")
        lines.append(t("twitch_status_affected", lang))
        for comp in affected:
            name = html.escape(str(comp.get("name") or "?"))
            label = html.escape(_twitch_status_label(lang, str(comp.get("status") or "")))
            lines.append(f"• <b>{name}</b> — {label}")
    incidents = [
        inc for inc in summary.get("incidents") or [] if isinstance(inc, dict)
    ]
    if incidents:
        lines.append("")
        lines.append(t("twitch_status_incidents", lang))
        for inc in incidents:
            name = html.escape(str(inc.get("name") or "?").strip() or "?")
            lines.append(f"• {name}")
    lines.append("")
    lines.append(f'<a href="{TWITCH_STATUS_PAGE_URL}">{TWITCH_STATUS_PAGE_URL}</a>')
    return "\n".join(lines)


async def check_twitch_status(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Poll status.twitch.com; notify availability opt-in users on changes."""
    try:
        summary = await asyncio.to_thread(fetch_twitch_status_summary)
        fingerprint = twitch_status_fingerprint(summary)
    except Exception as exc:
        logger.warning("Twitch status poll failed: %s", exc)
        return

    bot_data = context.application.bot_data
    previous = bot_data.get("twitch_status_fingerprint")
    bot_data["twitch_status_fingerprint"] = fingerprint
    if previous is None:
        # First poll after start — baseline only, no spam.
        return
    if fingerprint == previous:
        return

    db: Database = bot_data["db"]
    user_ids = db.get_availability_recipients()
    if not user_ids:
        return

    messages = {
        locale: _format_twitch_status_message(locale, summary)
        for locale in SUPPORTED_LOCALES
    }
    locale_rows = db.get_user_locales(user_ids)
    for uid in user_ids:
        locale = locale_rows.get(uid) or DEFAULT_LOCALE
        message = messages.get(locale) or messages[DEFAULT_LOCALE]
        await _send_dm_html(context.bot, db, uid, message)
        await asyncio.sleep(_BROADCAST_SEND_PAUSE)


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
    paid = db.count_stars_payers_since(since)
    if count <= 0 and paid <= 0:
        return
    for admin_id in ADMIN_USER_IDS:
        lang = db.get_user_locale(admin_id) or DEFAULT_LOCALE
        try:
            await context.bot.send_message(
                admin_id,
                t("weekly_new_users", lang, count=count, paid=paid),
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
        from config import twitch_oauth_redirect_uri
        from health import register_oauth_bridge

        await _restore_broadcast_jobs(application)
        redirect_uri = twitch_oauth_redirect_uri()
        if redirect_uri:
            loop = asyncio.get_running_loop()

            async def on_oauth_complete(
                owner_id: int,
                followed: list[dict] | None,
                error: str | None,
                token_info: dict[str, str] | None = None,
            ) -> None:
                await complete_twitch_import(
                    application, owner_id, followed, error, token_info
                )

            register_oauth_bridge(
                loop,
                twitch=twitch,
                redirect_uri=redirect_uri,
                on_complete=on_oauth_complete,
            )

    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .build()
    )
    app.bot_data["db"] = db
    app.bot_data["twitch"] = twitch
    app.bot_data["last_live"] = {}
    app.bot_data["last_live_primed"] = False
    app.bot_data["twitch_status_fingerprint"] = None
    app.bot_data["sending_broadcasts"] = set()
    app.add_error_handler(error_handler)

    app.add_handler(
        MessageHandler(_btn_filter("feedback"), report_problem),
        group=0,
    )
    app.add_handler(CommandHandler("feedback", report_problem), group=0)
    app.add_handler(
        MessageHandler(_btn_filter("manage"), open_subscriptions_menu),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("import_twitch"), start_twitch_import),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("admin"), open_admin_menu),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("demo"), toggle_demo_mode),
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
    app.add_handler(
        CallbackQueryHandler(on_sb_edit_text_click, pattern=r"^sb_edit_f:\d+:text$"),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(on_sb_edit_time_click, pattern=r"^sb_edit_f:\d+:time$"),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(on_sb_sched_callback, pattern=r"^sb_sched:"),
        group=0,
    )
    app.add_handler(CallbackQueryHandler(on_sb_delete, pattern=r"^sb_delete:\d+$"), group=0)
    app.add_handler(
        ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("alert_history"), show_alert_history),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(on_alert_history_more, pattern=r"^alert_history:more$"),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("settings"), open_settings_menu),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("premium"), open_premium_from_settings),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("partner"), open_partner_menu),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("partner_stats"), partner_show_stats),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("partner_link"), partner_show_link),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("partner_withdraw"), partner_request_withdraw),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("partner_withdrawals"), partner_show_withdrawals),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("back_settings"), open_settings_menu),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("admin_withdrawals"), admin_show_withdrawals),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(
            on_referral_withdrawal_action, pattern=r"^ref_wd:(paid|reject):\d+$"
        ),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("sync_subs"), open_sync_settings),
        group=0,
    )
    app.add_handler(CommandHandler("settings", open_settings_menu), group=0)
    app.add_handler(
        CallbackQueryHandler(on_import_mode_once, pattern=r"^import_mode:once$"),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(on_sync_disable, pattern=r"^sync:disable$"),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(on_sync_now, pattern=r"^sync:now$"),
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
    app.add_handler(
        CallbackQueryHandler(on_sys_other_toggle, pattern=r"^sys_other:toggle$"),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(on_sys_sync_toggle, pattern=r"^sys_sync:toggle$"),
        group=0,
    )
    app.add_handler(CallbackQueryHandler(on_toggle, pattern=r"^toggle:"), group=0)
    app.add_handler(
        CallbackQueryHandler(
            on_premium_callback_router,
            pattern=r"^premium:(pay|month|year|life|cancel|marfapr|trial|trial_confirm|features|feat_back|feat_pay|feat_toggle:.+)$",
        ),
        group=0,
    )
    app.add_handler(
        PreCheckoutQueryHandler(precheckout_premium_router),
        group=0,
    )
    app.add_handler(
        MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_premium_payment_router),
        group=0,
    )
    app.add_handler(CallbackQueryHandler(on_enable_all, pattern=r"^enable_all$"), group=0)
    app.add_handler(CallbackQueryHandler(on_delete_sel, pattern=r"^delete_sel:\d+$"), group=0)
    app.add_handler(CallbackQueryHandler(on_delete_go, pattern=r"^delete_go$"), group=0)
    app.add_handler(CallbackQueryHandler(on_delete_clear, pattern=r"^delete_clear$"), group=0)
    app.add_handler(CallbackQueryHandler(on_watch_again, pattern=r"^watch:again$"), group=0)
    app.add_handler(CallbackQueryHandler(schedule_save_token_callback, pattern=r"^sched_save_token:"), group=0)
    app.add_handler(CallbackQueryHandler(on_edit_type, pattern=r"^edit_type:\w+$"), group=0)
    app.add_handler(CallbackQueryHandler(on_edit_pick, pattern=r"^edit:\d+$"), group=0)
    app.add_handler(
        CallbackQueryHandler(
            on_edit_bool_menu,
            pattern=r"^edit_f:\d+:(delete_old|delete_fail|delete_other|preview|repeat)$",
        ),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(
            on_edit_set,
            pattern=r"^edit_set:\d+:(delete_old|delete_fail|delete_other|preview):[01]$|^edit_set:\d+:repeat:1$",
        ),
        group=0,
    )

    _wiz_cancel = MessageHandler(_btn_filter("wizard_cancel"), cancel)
    _wiz_back = MessageHandler(_btn_filter("wizard_back"), wizard_back)

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("help", help_command),
            CommandHandler("schedule", start_stream_schedule),
            MessageHandler(_btn_filter("new"), start_new_subscription),
            MessageHandler(_btn_filter("watch"), start_what_to_watch),
            MessageHandler(_btn_filter("create_schedule"), start_stream_schedule),
            MessageHandler(_btn_filter("language"), start_language_change),
            MessageHandler(_btn_filter("ignored_words"), start_ignored_words),
            MessageHandler(_btn_filter("broadcast_new"), admin_broadcast_start),
            CallbackQueryHandler(on_import_mode_sync, pattern=r"^import_mode:sync$"),
            CallbackQueryHandler(on_sync_change_period, pattern=r"^sync:period$"),
            CallbackQueryHandler(start_edit_template, pattern=r"^edit_f:\d+:template$"),
            CallbackQueryHandler(start_edit_image, pattern=r"^edit_f:\d+:image$"),
            CallbackQueryHandler(delete_edit_image, pattern=r"^edit_f:\d+:image_del$"),
            CallbackQueryHandler(start_edit_ignore_keywords, pattern=r"^edit_f:\d+:ignore_keywords$"),
            CallbackQueryHandler(start_edit_dest, pattern=r"^edit_f:\d+:dest$"),
            CallbackQueryHandler(start_edit_delay, pattern=r"^edit_f:\d+:delay$"),
            CallbackQueryHandler(start_edit_repeat, pattern=r"^edit_f:\d+:repeat$"),
            CallbackQueryHandler(
                start_edit_schedule_reminder, pattern=r"^edit_f:\d+:sched_remind$"
            ),
            CallbackQueryHandler(start_edit_repeat_mute, pattern=r"^edit_set:\d+:repeat:0$"),
            CallbackQueryHandler(start_watch_change, pattern=r"^watch:change$"),
        ],
        states={
            LANG_SELECT: [CallbackQueryHandler(receive_language, pattern=r"^lang:")],
            ALERT_TYPE: [
                _wiz_cancel,
                CallbackQueryHandler(cancel, pattern=r"^alert_type:cancel$"),
                CallbackQueryHandler(
                    receive_alert_type, pattern=r"^alert_type:(live|category|upcoming|end)$"
                ),
            ],
            PREMIUM_GATE: [
                _wiz_cancel,
                CallbackQueryHandler(
                    on_premium_gate, pattern=r"^premium_gate:(get|skip|cancel)$"
                ),
            ],
            CHANNEL: [
                _wiz_cancel,
                _wiz_back,
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_channel),
            ],
            CHANNEL_DUP: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(
                    receive_channel_dup, pattern=r"^dup:(edit:\d+|continue)$"
                ),
            ],
            TEMPLATE: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(
                    receive_strip_name_toggle, pattern=r"^strip_name:(toggle|back|cancel)$"
                ),
                CallbackQueryHandler(lucky_generate, pattern=r"^lucky:go$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_template),
            ],
            TEMPLATE_TYPO_CONFIRM: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(receive_template_typo_confirm, pattern=r"^template_typo:[01]$"),
            ],
            IMAGE_ASK: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(
                    receive_image_ask, pattern=r"^image_ask:(add|skip|delete|keep)$"
                ),
            ],
            IMAGE_UPLOAD: [
                _wiz_cancel,
                _wiz_back,
                MessageHandler(filters.PHOTO, receive_image_upload),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_image_upload),
            ],
            IMAGE_POSITION: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(receive_image_position, pattern=r"^image_pos:(before|after)$"),
            ],
            LUCKY_PREVIEW: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(lucky_generate, pattern=r"^lucky:go$"),
                CallbackQueryHandler(lucky_continue, pattern=r"^lucky:continue$"),
                CallbackQueryHandler(lucky_full_wizard, pattern=r"^lucky:full$"),
            ],
            IGNORE_KEYWORDS: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(
                    receive_ignore_keywords_global_toggle,
                    pattern=r"^ignore_keywords:global_toggle$",
                ),
                CallbackQueryHandler(
                    receive_ignore_keywords_skip, pattern=r"^ignore_keywords:skip$"
                ),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_ignore_keywords),
            ],
            GLOBAL_IGNORE_KEYWORDS: [
                CallbackQueryHandler(
                    receive_ignored_words_clear, pattern=r"^ignored_words:clear$"
                ),
                CallbackQueryHandler(
                    receive_ignored_words_cancel, pattern=r"^ignored_words:cancel$"
                ),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_ignored_words),
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
            SCHEDULE_REMINDER_ASK: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(
                    receive_schedule_reminder_ask, pattern=r"^sched_remind:"
                ),
            ],
            SCHEDULE_REMINDER_MINUTES: [
                _wiz_cancel,
                _wiz_back,
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, receive_schedule_reminder_minutes
                ),
            ],
            EDIT_TEMPLATE: [
                _wiz_cancel,
                CallbackQueryHandler(
                    receive_strip_name_toggle, pattern=r"^strip_name:(toggle|back|cancel)$"
                ),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_edit_template),
            ],
            EDIT_IGNORE_KEYWORDS: [
                _wiz_cancel,
                CallbackQueryHandler(
                    receive_ignore_keywords_global_toggle,
                    pattern=r"^ignore_keywords:global_toggle$",
                ),
                CallbackQueryHandler(
                    receive_edit_ignore_keywords_skip, pattern=r"^ignore_keywords:skip$"
                ),
                CallbackQueryHandler(cancel, pattern=r"^ignore_keywords:cancel$"),
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
            EDIT_SCHEDULE_REMINDER: [
                _wiz_cancel,
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, receive_edit_schedule_reminder
                ),
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
            DELETE_SIBLING_ALERTS: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(
                    receive_delete_sibling, pattern=r"^delete_sibling:"
                ),
            ],
            DELETE_FAIL_NOTIFY: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(receive_delete_fail_notify, pattern=r"^delete_fail:"),
            ],
            SCHEDULE_LIVE_ASK: [
                _wiz_cancel,
                CallbackQueryHandler(
                    receive_schedule_live_add, pattern=r"^sched_live:"
                ),
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
            STREAM_SCHEDULE_PUBLISH: [
                _wiz_cancel,
                CallbackQueryHandler(
                    stream_schedule_publish_callback, pattern=r"^stream_sched:publish:"
                ),
            ],
            STREAM_SCHEDULE_DURATION: [
                _wiz_cancel,
                CallbackQueryHandler(
                    stream_schedule_duration_callback,
                    pattern=r"^stream_sched:duration:",
                ),
            ],
            SYNC_DAYS: [
                _wiz_cancel,
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_sync_days),
            ],
            WATCH_PICK: [
                _wiz_cancel,
                CallbackQueryHandler(
                    receive_watch_pick_callback, pattern=r"^watch_pick:"
                ),
            ],
            WATCH_DELETE: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(
                    receive_watch_del_sel, pattern=r"^watch_del_sel:"
                ),
                CallbackQueryHandler(receive_watch_del_go, pattern=r"^watch_del_go$"),
                CallbackQueryHandler(
                    receive_watch_del_clear, pattern=r"^watch_del_clear$"
                ),
                CallbackQueryHandler(
                    receive_watch_del_back, pattern=r"^watch_del_back$"
                ),
            ],
            WATCH_CATEGORIES: [
                _wiz_cancel,
                CallbackQueryHandler(
                    receive_watch_category_callback, pattern=r"^watch_cat:"
                ),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_watch_category_text),
            ],
            WATCH_TAGS: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(
                    receive_watch_tags_callback, pattern=r"^watch_tags:"
                ),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_watch_tags_text),
            ],
            WATCH_VIEWERS: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(
                    receive_watch_viewers_callback, pattern=r"^watch_viewers:"
                ),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_watch_viewers_text),
            ],
            WATCH_LANGUAGE: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(
                    receive_watch_language_callback, pattern=r"^watch_lang:"
                ),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_watch_language_text),
            ],
            WATCH_MATURE: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(
                    receive_watch_mature_callback, pattern=r"^watch_mature:"
                ),
            ],
            WATCH_SAVE: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(
                    receive_watch_save_callback, pattern=r"^watch_save:"
                ),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("help", help_command),
            MessageHandler(_btn_filter("wizard_cancel"), cancel),
        ],
        allow_reentry=True,
        name="main_conversation",
    )

    app.add_handler(conv, group=1)
    app.bot_data["main_conv"] = conv

    def _clear_stuck_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            key = conv._get_key(update)
        except Exception:
            return
        if key in conv._conversations:
            del conv._conversations[key]
            context.user_data.clear()

    async def wake_stuck_on_menu_callback(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        # Inline menu buttons (edit/list/etc.) must break a stuck wizard conversation.
        _clear_stuck_conversation(update, context)

    async def wake_stuck_on_menu_message(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        _clear_stuck_conversation(update, context)

    async def orphan_wizard_nav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # After deploy, wizard ReplyKeyboard may remain while conversation state is gone.
        try:
            key = conv._get_key(update)
        except Exception:
            key = None
        if key is not None and key in conv._conversations:
            return
        user_id = update.effective_user.id
        lang = _user_lang(context, user_id)
        context.user_data.clear()
        await update.effective_message.reply_text(
            t("cancelled", lang),
            reply_markup=_menu(lang, user_id),
        )

    # group=-1 runs before menu/conversation handlers and clears a stuck wizard.
    app.add_handler(
        CallbackQueryHandler(
            wake_stuck_on_menu_callback,
            pattern=(
                r"^(edit:\d+$|edit_f:|edit_set:|toggle:|enable_all$|delete:\d+$|"
                r"delete_sel:|delete_go$|delete_clear$|"
                r"sb_edit:\d+$|sb_edit_f:|sb_delete:|"
                r"sys_updates:|sys_availability:|sys_other:|sys_sync:|"
                r"import_mode:|sync:|premium:|ref_wd:|watch:|alert_history:)"
            ),
        ),
        group=-1,
    )
    app.add_handler(
        MessageHandler(
            (
                _btn_filter("feedback")
                | _btn_filter("manage")
                | _btn_filter("import_twitch")
                | _btn_filter("list")
                | _btn_filter("edit")
                | _btn_filter("delete")
                | _btn_filter("alert_history")
                | _btn_filter("settings")
                | _btn_filter("premium")
                | _btn_filter("partner")
                | _btn_filter("partner_stats")
                | _btn_filter("partner_link")
                | _btn_filter("partner_withdraw")
                | _btn_filter("partner_withdrawals")
                | _btn_filter("back_settings")
                | _btn_filter("admin_withdrawals")
                | _btn_filter("new")
                | _btn_filter("watch")
                | _btn_filter("create_schedule")
                | _btn_filter("back")
                | _btn_filter("language")
                | _btn_filter("sys_notifications")
                | _btn_filter("ignored_words")
                | _btn_filter("sync_subs")
                | _btn_filter("admin")
                | _btn_filter("broadcast")
                | _btn_filter("scheduled_broadcasts")
                | _btn_filter("stats")
            ),
            wake_stuck_on_menu_message,
        ),
        group=-1,
    )
    app.add_handler(MessageHandler(_btn_filter("wizard_cancel"), orphan_wizard_nav), group=0)
    app.add_handler(MessageHandler(_btn_filter("wizard_back"), orphan_wizard_nav), group=0)
    # After all menu ReplyKeyboard handlers — must not steal Settings/etc.
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            receive_sb_edit_text,
            block=False,
        ),
        group=0,
    )

    from config import CHECK_INTERVAL, SCHEDULE_CHECK_INTERVAL
    from premium_handlers import refresh_premium_twitch_job

    app.job_queue.run_repeating(check_streams, interval=CHECK_INTERVAL, first=10)
    app.job_queue.run_repeating(
        check_schedule_reminders, interval=SCHEDULE_CHECK_INTERVAL, first=25
    )
    app.job_queue.run_repeating(process_scheduled_broadcasts, interval=60, first=20)
    app.job_queue.run_repeating(check_twitch_status, interval=120, first=40)
    app.job_queue.run_repeating(sync_twitch_follows, interval=3600, first=90)
    app.job_queue.run_repeating(refresh_premium_twitch_job, interval=3600, first=120)
    app.job_queue.run_repeating(
        weekly_new_users_report,
        interval=7 * 24 * 3600,
        first=_seconds_until_next_weekly_report(),
    )
    return app
