from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    MenuButtonCommands,
    MenuButtonWebApp,
    MessageOriginChannel,
    MessageOriginChat,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, Conflict, Forbidden, NetworkError, RetryAfter
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
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
import analytics
import beta as beta_features
from db import (
    AlertHistoryEntry,
    BotStats,
    Database,
    Subscription,
    TwitchSync,
    WATCH_MAX_FILTERS,
    WatchPrefs,
    dump_category_watch_prefs,
    is_category_watch_sub,
    is_on_notify_cooldown,
    parse_category_watch_prefs,
    watch_filter_auto_name,
)
from i18n import (
    DEFAULT_LOCALE,
    SCHEDULE_TZ,
    SCHEDULE_TZ_NAME,
    SUPPORTED_LOCALES,
    admin_menu,
    admin_type_keyboard,
    admin_other_audience_keyboard,
    admin_wizard_menu,
    alert_type_keyboard,
    all_btn_texts,
    all_wizard_nav_buttons,
    broadcast_menu,
    btn,
    channel_dup_keyboard,
    delete_old_keyboard,
    delete_fail_notify_keyboard,
    delivery_fail_notice_keyboard,
    delete_sibling_keyboard,
    dest_keyboard,
    dest_label,
    delay_keyboard,
    edit_bool_keyboard,
    edit_options_keyboard,
    ignore_keywords_keyboard,
    ignored_words_keyboard,
    is_menu_button,
    whisper_alerts_keyboard,
    advanced_mode_keyboard,
    beta_mode_keyboard,
    image_edit_keyboard,
    image_position_keyboard,
    import_mode_keyboard,
    language_keyboard,
    link_preview_keyboard,
    chat_button_keyboard,
    lucky_preview_keyboard,
    main_menu,
    other_menu,
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
    stream_schedule_fix_day_keyboard,
    stream_schedule_more_keyboard,
    stream_schedule_occupied_keyboard,
    stream_schedule_duration_keyboard,
    stream_schedule_publish_keyboard,
    format_stream_schedule_prompt_date,
    format_stream_schedule_result,
    subscriptions_menu,
    sync_settings_keyboard,
    sync_unfollow_keyboard,
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
    welcome_demo_keyboard,
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
    template_has_game_placeholder,
    template_has_link,
    twitch_status_fingerprint,
)
from translate import build_translations
from hf_text import generate_alert_template
from bot_helpers import (
    _BROADCAST_SEND_PAUSE,
    _btn_filter,
    _can_use_admin_tools,
    _inline_btn_label,
    _is_admin,
    _is_link_preview_disabled,
    _menu,
    _pause_notifications_enabled,
    _pulse_wizard_keyboard,
    _send_dm_html,
    _settings_kb,
    _split_telegram_text,
    _user_lang,
    _user_notifications_paused,
    _wizard,
    chat_context_properties,
    dm_only_conv_entry,
    group_setup_menu_filter,
    GROUP_SETUP_CALLBACK_PATTERN,
    is_private_chat,
    handle_group_setup_rejection,
    reply_chat_id,
    reply_setup_private_only,
)
from handlers.admin_stats import admin_show_stats, _format_stats
from handlers.alert_history import (
    _alert_history_item_url,
    _alert_history_nav_keyboard,
    _build_alert_history_chunks,
    _format_alert_history_block,
    _format_vod_timestamp,
    _twitch_vod_url,
    _vod_id_from_videos,
    _vod_offset_seconds,
    on_alert_history_menu,
    on_alert_history_more,
    on_alert_history_noop,
    on_alert_history_page,
    show_alert_history,
)
from handlers.monitoring import (
    TWITCH_STATUS_HOST,
    TWITCH_STATUS_PAGE_URL,
    _format_posthog_status_message,
    _format_twitch_status_message,
    _is_http_timeout,
    _is_unchanged_message_edit,
    _load_posthog_seen_report_ids,
    _save_posthog_seen_report_ids,
    _seconds_until_next_daily_stats,
    _seconds_until_next_weekly_report,
    check_posthog_status,
    check_twitch_status,
    daily_bot_stats_snapshot,
    notify_admins_posthog_issue,
    poll_posthog_inbox_reports,
    weekly_new_users_report,
)
from handlers.partner import (
    admin_show_withdrawals,
    on_referral_withdrawal_action,
    open_partner_menu,
    partner_request_withdraw,
    partner_show_link,
    partner_show_stats,
    partner_show_withdrawals,
)

from handlers.broadcast import (
    _broadcast_job_name,
    _broadcast_type_label,
    _cancel_broadcast_job,
    _claim_broadcast_send,
    _clear_main_conversation,
    _dump_broadcast_recipient_ids,
    _format_scheduled_at_label,
    _go_sb_edit_text_prompt,
    _go_sb_edit_time_prompt,
    _parse_broadcast_recipient_ids,
    _parse_sb_edit_f_id,
    _release_broadcast_send,
    _report_broadcast_done,
    _restore_broadcast_jobs,
    _run_scheduled_broadcast,
    _schedule_broadcast_job,
    _schedule_to_utc_iso,
    _scheduled_text_preview,
    _send_admin_broadcast,
    _utc_iso_to_schedule,
    admin_audience_callback,
    admin_broadcast_start,
    admin_receive_ids,
    admin_receive_text,
    admin_sb_schedule_callback,
    admin_schedule_callback,
    admin_scheduled_list,
    admin_sent_list,
    admin_select_type,
    on_broadcast_feedback,
    on_sb_delete,
    on_sb_edit_pick,
    on_sb_edit_text_click,
    on_sb_edit_time_click,
    on_sb_sched_callback,
    open_broadcast_menu,
    process_scheduled_broadcasts,
    purge_old_broadcasts,
    refresh_broadcast_feedback_keyboards,
    receive_sb_edit_text,
)
from handlers.notifications import (
    LIVE_GAME_RECHECK_SECONDS,
    _WATCH_CATEGORY_NOTIFY_CAP,
    _check_category_watch_alerts,
    _parse_category_watch_live_ids,
    _parse_segment_start,
    _send_delayed_category_notification,
    _send_delayed_end_notification,
    _send_delayed_notification,
    category_change_events,
    check_schedule_reminders,
    check_streams,
    end_cover_stream,
    live_transitions,
    needs_live_game_recheck,
)
from handlers.stream_schedule import (
    _SCHEDULE_DEFAULT_DURATION_MIN,
    _SCHEDULE_FIX_DAY_BETA_ID,
    _STREAM_TIME_PATTERN,
    _advance_stream_schedule_day,
    _complete_schedule_publish,
    _day_slots_view,
    _finish_stream_schedule,
    _init_stream_schedule,
    _next_week_dates,
    _occupied_slots_text,
    _owner_schedule_broadcaster_id,
    _parse_stream_time,
    _pending_schedule_preview,
    _pending_schedule_publishes,
    _prompt_add_another_slot,
    _prompt_stream_schedule_fix_game,
    _prompt_stream_schedule_fix_time,
    _prompt_stream_schedule_game,
    _prompt_stream_schedule_time,
    _schedule_publish_error_text,
    _schedule_segment_game,
    _show_day_slots,
    _slots_on_local_day,
    _start_schedule_publish_auth,
    _stream_schedule_show_finish,
    schedule_save_token_callback,
    start_stream_schedule,
    stream_schedule_confirm_callback,
    stream_schedule_duration_callback,
    stream_schedule_finish_callback,
    stream_schedule_fix_add_callback,
    stream_schedule_fix_day_callback,
    stream_schedule_fix_delete_callback,
    stream_schedule_fix_edit_callback,
    stream_schedule_fix_game,
    stream_schedule_fix_slots_done_callback,
    stream_schedule_fix_time,
    stream_schedule_game,
    stream_schedule_mode_callback,
    stream_schedule_more_callback,
    stream_schedule_noop_callback,
    stream_schedule_publish_callback,
    stream_schedule_skip_callback,
    stream_schedule_time,
    stream_schedule_tz,
    stream_schedule_tz_callback,
)

from handlers.settings import (
    _beta_mode_features_block,
    _enable_whisper_eventsub,
    _refresh_sys_notifications_menu,
    _send_whisper_oauth_prompt,
    _whisper_alerts_ready,
    complete_chat_oauth,
    complete_whisper_oauth,
    notify_whisper_received,
    on_advanced_mode_toggle,
    on_beta_toggle,
    on_sys_availability_toggle,
    on_sys_other_toggle,
    on_sys_sync_toggle,
    on_sys_updates_toggle,
    on_whisper_alerts_toggle,
    on_whisper_eventsub_revoked,
    open_advanced_mode_menu,
    open_beta_mode_menu,
    open_other_menu,
    open_settings_menu,
    open_stream_chat,
    open_sys_notifications_menu,
    open_whisper_alerts_menu,
    receive_ignored_words,
    receive_ignored_words_cancel,
    receive_ignored_words_clear,
    start_ignored_words,
    start_language_change,
    sync_stream_chat_menu_button,
)

from handlers.watch import (
    _WATCH_LANG_RE,
    _WATCH_MAX_CATS,
    _WATCH_MAX_TAGS,
    _WATCH_SUGGEST_N,
    _WATCH_VIEWERS_RE,
    _add_watch_category,
    _bot_lang_to_twitch,
    _complete_watch_wizard,
    _fetch_lucky_watch_suggestions,
    _fetch_recommended_promo_streams,
    _fetch_watch_suggestions,
    _fetch_watch_vod_suggestions,
    _format_watch_suggestions,
    _format_watch_vod_suggestions,
    _go_watch_categories_prompt,
    _go_watch_language_prompt,
    _go_watch_mature_prompt,
    _go_watch_pick_prompt,
    _go_watch_save_prompt,
    _go_watch_tags_prompt,
    _go_watch_viewers_prompt,
    _live_promo_streams,
    _lucky_streams_from_igdb,
    _parse_watch_viewers,
    _premium_channel_badge_html,
    _promo_channel_user_ids,
    _promo_streams_matching,
    _refresh_watch_recommended_flag,
    _resolve_watch_prefs,
    _send_recommended_promo_suggestions,
    _send_watch_suggestions,
    _set_watch_lucky_mode,
    _set_watch_recommended_mode,
    _start_watch_wizard,
    _watch_cats_keyboard,
    _watch_channel_refs,
    _watch_lucky_mode,
    _watch_prefs_from_user_data,
    _watch_prefs_summary,
    _watch_recommended_mode,
    _watch_viewers_label,
    on_watch_again,
    on_watch_create_alerts,
    receive_watch_category_callback,
    receive_watch_category_text,
    receive_watch_del_back,
    receive_watch_del_clear,
    receive_watch_del_go,
    receive_watch_del_sel,
    receive_watch_language_callback,
    receive_watch_language_text,
    receive_watch_mature_callback,
    receive_watch_nav_back,
    receive_watch_pick_callback,
    receive_watch_save_callback,
    receive_watch_tags_callback,
    receive_watch_tags_text,
    receive_watch_viewers_callback,
    receive_watch_viewers_text,
    start_watch_change,
    start_what_to_watch,
)

from handlers.wizard import (
    _GATE_FEATURE_LABEL,
    _LIVE_ADDON_CLEAR_KEYS,
    _continue_after_delay,
    _delete_old_prompt_text,
    _finish_subscription,
    _go_after_ignore_keywords,
    _go_after_link_preview_step,
    _go_after_repeat,
    _go_alert_type_prompt,
    _go_before_dest_step,
    _go_channel_prompt,
    _go_chat_button_prompt,
    _go_delay_minutes_prompt,
    _go_delay_prompt,
    _go_dest_prompt,
    _go_ignore_keywords_prompt,
    _go_image_ask_prompt,
    _go_link_preview_prompt,
    _go_repeat_prompt,
    _go_schedule_reminder_ask,
    _go_schedule_reminder_minutes,
    _go_template_prompt,
    _has_sibling_publication_subs,
    _membership_check_blocked,
    _offer_template_typo_fix,
    _parse_dest_input,
    _premium_gate_text,
    _prompt_delete_fail_notify,
    _prompt_delete_old,
    _prompt_dest_step,
    _prompt_repeat_step,
    _prompt_schedule_reminder_ask,
    _render_sub_template,
    _send_prompt_with_wizard_inline,
    _set_wizard_back,
    _show_lucky_preview,
    _show_premium_gate,
    _user_can_manage_chat,
    _wizard_back_before_dest,
    _wizard_channel,
    lucky_continue,
    lucky_full_wizard,
    lucky_generate,
    on_premium_gate,
    receive_alert_type,
    receive_channel,
    receive_channel_dup,
    receive_chat_button_ask,
    receive_delay_minutes,
    receive_delay_send,
    receive_delete_fail_notify,
    receive_delete_old,
    receive_delete_sibling,
    receive_dest_chat,
    receive_dest_type,
    receive_ignore_keywords,
    receive_ignore_keywords_back,
    receive_ignore_keywords_global_toggle,
    receive_ignore_keywords_skip,
    receive_image_ask,
    receive_image_position,
    receive_image_upload,
    receive_link_preview,
    receive_repeat_allow,
    receive_repeat_mute_minutes,
    receive_schedule_live_add,
    receive_schedule_reminder_ask,
    receive_schedule_reminder_minutes,
    receive_strip_name_toggle,
    receive_template,
    receive_template_typo_confirm,
    start_new_subscription,
    wizard_back,
)

from handlers.delivery import (
    _TELEGRAM_CAPTION_LIMIT,
    _alert_chat_button_markup,
    _deliver_alert_content,
    _delivery_fail_chat_label,
    _delivery_fail_notice_due,
    _delivery_fail_notified,
    _DELIVERY_FAIL_NOTICE_COOLDOWN,
    _maybe_notify_delivery_failure,
    _message_link,
    _resolve_chat_display_name,
    _send_notification,
    _send_test,
    on_stored_template_typo_fix,
)

from handlers.subscriptions import (
    LEGACY_IMPORT_TEMPLATES,
    _CART_BETA_ID,
    _EDIT_ALERT_TYPE_ORDER,
    _PAUSE_DAYS_MAX,
    _PENDING_IMPORT_TTL_SEC,
    _PENDING_SYNC_UNFOLLOW_TTL_SEC,
    _SYNC_PERIOD_MAX,
    _SYNC_PERIOD_MIN,
    _alert_type_from_sub,
    _alert_type_pick_keyboard,
    _ask_sync_unfollow_if_needed,
    _cart_items_of_type,
    _cart_present_types,
    _delete_cart_keyboard,
    _delete_cart_view,
    _delete_pick_keyboard,
    _delete_subs_for_owner,
    _deleted_subscriptions_cart_enabled,
    _deliver_import_result,
    _deliver_subs_list,
    _edit_pick_keyboard,
    _edit_present_types,
    _edit_type_keyboard,
    _format_pause_until,
    _format_sub_line,
    _format_subs_overview_lines,
    _format_sync_next,
    _import_result_keyboard,
    _next_sync_iso,
    _owner_sub_number,
    _peek_pending_import,
    _pending_imports,
    _pending_sync_unfollows,
    _pop_pending_import,
    _run_followed_import,
    _show_delete_cart,
    _store_delete_cart_state,
    _store_pending_import,
    _sub_in_current_mode,
    _subs_for_owner,
    _subs_kb,
    _subs_toggle_keyboard,
    _sync_owner_follows,
    _sync_result_notes,
    cancel_pause_notifications,
    complete_twitch_import,
    delete_menu,
    edit_menu,
    import_followed_as_subscriptions,
    list_subscriptions,
    migrate_import_sync_subscriptions,
    on_delete_cart_clear,
    on_delete_cart_open,
    on_delete_cart_restore_go,
    on_delete_cart_sel,
    on_delete_cart_type,
    on_delete_clear,
    on_delete_all,
    on_delete_all_confirm,
    on_delete_go,
    on_delete_sel,
    on_delete_type,
    on_delivery_fail_delete,
    on_edit_bool_menu,
    on_edit_change_type_click,
    on_edit_copy_click,
    on_edit_copy_change_click,
    on_edit_pick,
    on_edit_set,
    on_edit_type,
    on_edit_type_pick,
    on_edit_type_pick_cancel,
    on_enable_all,
    on_import_mode_once,
    on_import_mode_sync,
    on_list_type,
    on_list_page,
    on_list_page_noop,
    on_list_delete,
    on_list_delete_confirm,
    on_edit_page,
    on_edit_page_noop,
    on_delete_page,
    on_delete_page_noop,
    on_share_accept,
    on_share_decline,
    on_share_dup_continue,
    on_share_dup_edit,
    on_share_show,
    offer_shared_alert,
    on_sync_change_period,
    on_sync_disable,
    on_sync_now,
    on_sync_unfollow_answer,
    on_toggle,
    on_welcome_demo_delete,
    open_cart_menu,
    open_subscriptions_menu,
    open_sync_settings,
    receive_pause_notifications_days,
    receive_sync_days,
    start_edit_dest,
    start_edit_ignore_keywords,
    start_edit_repeat_mute,
    start_edit_template,
    start_pause_notifications,
    start_twitch_import,
    cancel_twitch_import,
    sync_twitch_follows,
    receive_edit_ignore_keywords,
    receive_edit_ignore_keywords_skip,
)

logger = logging.getLogger(__name__)

GITHUB_ISSUES_URL = "https://github.com/Marfa/twitch-telegram-bot/issues"

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
    CHAT_BUTTON_ASK,
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
    STREAM_SCHEDULE_TZ,
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
    ADMIN_MSG_AUDIENCE,
    ADMIN_MSG_IDS,
    STREAM_SCHEDULE_MODE,
    STREAM_SCHEDULE_FIX_DAY,
    STREAM_SCHEDULE_FIX_GAME,
    STREAM_SCHEDULE_FIX_TIME,
    STREAM_SCHEDULE_FIX_SLOTS,
    STREAM_SCHEDULE_MORE,
    PAUSE_ALERTS_DAYS,
) = range(61)

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


def _help_text(lang: str) -> str:
    return t(
        "help",
        lang,
        btn_new=btn("new", lang),
        btn_import_twitch=btn("import_twitch", lang),
        btn_list=btn("list", lang),
        btn_alert_history=btn("alert_history", lang),
        btn_other=btn("other", lang),
        btn_whisper_alerts=btn("whisper_alerts", lang),
        btn_create_schedule=btn("create_schedule", lang),
        btn_watch=btn("watch", lang),
        btn_chat=btn("chat", lang),
        btn_settings=btn("settings", lang),
        btn_feedback=btn("feedback", lang),
    )


async def _prompt_language(update: Update) -> int:
    await update.effective_message.reply_text(
        t("lang_pick", DEFAULT_LOCALE),
        reply_markup=language_keyboard(),
    )
    return LANG_SELECT


async def _ensure_welcome_premium_channel_subscription(
    application: Application,
    bot,
    user_id: int,
    lang: str,
) -> tuple[int, str] | None:
    """First-start demo: random Premium channel (config + paid)."""
    db: Database = application.bot_data["db"]
    twitch: TwitchClient = application.bot_data["twitch"]
    candidates = prem.list_promo_channel_logins(db)
    if not candidates:
        candidates = [prem.twitch_channel_login() or "marfapr"]
    random.shuffle(candidates)
    for login in candidates:
        for sub in _subs_for_owner(db, user_id):
            if (
                sub.twitch_username == login
                and sub.notify_on_live
                and not sub.notify_on_category_change
                and not sub.notify_on_end
            ):
                return sub.id, login
        user = await asyncio.to_thread(twitch.get_user, login)
        if not user:
            logger.warning("Welcome premium seed: Twitch user %s not found", login)
            continue
        uid = str(user["id"])
        uname = str(user.get("login") or login).lower()
        enabled = await prem.may_enable_subscription_async(
            bot, db, user_id, twitch_username=uname
        )
        sub_id = db.add_subscription(
            owner_id=user_id,
            twitch_username=uname,
            twitch_user_id=uid,
            message_template=t("import_default_template", lang),
            dest_type="dm",
            chat_id=user_id,
            thread_id=None,
            disable_link_preview=True,
            enabled=enabled,
            notify_on_live=True,
            notify_on_end=False,
            notify_on_category_change=False,
            is_demo=False,
        )
        analytics.capture(
            user_id,
            "welcome_demo_subscription_created",
            {"sub_id": sub_id, "enabled": enabled, "channel": uname},
        )
        return sub_id, uname
    return None


async def _send_welcome_bundle(
    application: Application,
    bot,
    chat_id: int,
    user_id: int,
    lang: str,
    *,
    first_start: bool = False,
) -> None:
    db: Database = application.bot_data["db"]
    await sync_stream_chat_menu_button(bot, db, user_id)
    await bot.send_message(
        chat_id,
        t("start_welcome", lang),
        reply_markup=_menu(lang, user_id),
    )
    if not first_start:
        return
    seeded = await _ensure_welcome_premium_channel_subscription(
        application, bot, user_id, lang
    )
    if not seeded:
        return
    sub_id, channel = seeded
    await bot.send_message(
        chat_id,
        t("start_welcome_demo", lang, channel=channel),
        reply_markup=welcome_demo_keyboard(lang, sub_id),
    )


async def _send_welcome(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    lang: str,
    *,
    first_start: bool = False,
) -> int:
    user_id = update.effective_user.id
    await _send_welcome_bundle(
        context.application,
        context.bot,
        update.effective_chat.id,
        user_id,
        lang,
        first_start=first_start,
    )
    await _maybe_offer_pending_share(context, user_id, lang)
    return ConversationHandler.END


async def _maybe_offer_pending_share(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, lang: str
) -> None:
    token = context.user_data.pop("pending_share_token", None)
    if not token:
        return
    db: Database = context.application.bot_data["db"]
    await offer_shared_alert(context.bot, db, user_id, lang, str(token))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    db: Database = context.application.bot_data["db"]
    user_id = update.effective_user.id
    is_first_start = not db.user_exists(user_id)
    db.upsert_user(user_id)
    _apply_referral_start_arg(db, user_id, context.args)
    _apply_share_start_arg(db, context, context.args)
    lang = db.get_user_locale(user_id)
    analytics.capture(
        user_id,
        "bot_started",
        {
            "has_locale": bool(lang),
            "has_start_arg": bool(context.args),
            "is_first_start": is_first_start,
        },
    )
    if not lang:
        context.user_data["after_lang"] = "welcome"
        context.user_data["first_welcome"] = is_first_start
        return await _prompt_language(update)
    return await _send_welcome(update, context, lang, first_start=is_first_start)


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


def _apply_share_start_arg(
    db: Database, context: ContextTypes.DEFAULT_TYPE, args: list[str] | None
) -> None:
    if not args:
        return
    raw = (args[0] or "").strip()
    if not raw.startswith("share_"):
        return
    token = raw[6:].strip()
    if not token or db.get_alert_share_snapshot(token) is None:
        return
    context.user_data["pending_share_token"] = token


async def receive_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = query.data.split(":", 1)[1]
    if lang not in SUPPORTED_LOCALES:
        lang = DEFAULT_LOCALE
    db: Database = context.application.bot_data["db"]
    db.set_user_locale(query.from_user.id, lang)
    analytics.capture(
        query.from_user.id,
        "locale_set",
        {"locale": lang, "$set": {"locale": lang}},
    )
    await query.edit_message_text(t("lang_set", lang))
    after = context.user_data.pop("after_lang", "welcome")
    first_start = context.user_data.pop("first_welcome", False)
    await sync_stream_chat_menu_button(context.bot, db, query.from_user.id)
    chat_id = reply_chat_id(update)
    if after == "help":
        await context.bot.send_message(
            chat_id,
            _help_text(lang),
            reply_markup=_menu(lang, query.from_user.id),
        )
        return ConversationHandler.END
    if after == "settings":
        await context.bot.send_message(
            chat_id,
            t("menu_settings", lang),
            reply_markup=_settings_kb(lang, db, query.from_user.id),
        )
        return ConversationHandler.END
    await _send_welcome_bundle(
        context.application,
        context.bot,
        chat_id,
        query.from_user.id,
        lang,
        first_start=first_start,
    )
    await _maybe_offer_pending_share(context, query.from_user.id, lang)
    return ConversationHandler.END


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
    context.user_data["message_template"] = sub.message_template or ""
    if has_image:
        context.user_data["image_file_id"] = sub.image_file_id
        context.user_data["image_position"] = sub.image_position or ""
    await query.edit_message_text("✓")
    show_game_cover = template_has_game_placeholder(sub.message_template or "")
    await context.bot.send_message(
        query.from_user.id,
        t("edit_image_prompt", lang) if has_image else t("image_ask", lang),
        reply_markup=image_edit_keyboard(
            lang, has_image=has_image, show_game_cover=show_game_cover
        ),
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
    sub = db.get_subscription(sub_id, owner_id)
    # Chat button and link preview cannot both be on (Telegram); force preview off.
    if sub and sub.attach_chat_button:
        preview_disabled = True
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
    if not await prem.has_feature(
        context.bot, db, query.from_user.id, "delay", channel=sub.twitch_username
    ):
        from premium_handlers import send_premium_screen

        await query.edit_message_text(
            t("premium_gate", lang, action=t("premium_gate_action_cancel", lang))
        )
        await send_premium_screen(context.bot, query.from_user.id, lang, db, update=update)
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


async def receive_edit_delay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = _user_lang(context, update.effective_user.id)
    sub_id = context.user_data.get("edit_sub_id")
    if not sub_id:
        return ConversationHandler.END

    raw = (update.effective_message.text or "").strip()
    if is_menu_button(raw):
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
    if is_menu_button(raw):
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
    if is_menu_button(template):
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
    if is_menu_button(raw):
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
    # A personal forward (MessageOriginUser / MessageOriginHiddenUser) has no
    # usable destination chat.
    return None, None


def _edit_options_for_sub(
    sub: Subscription, lang: str, *, show_advanced: bool
) -> InlineKeyboardMarkup:
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
        show_advanced=show_advanced,
    )


def _edit_menu_text(
    lang: str,
    *,
    sub_id: int,
    username: str,
    show_advanced: bool,
) -> str:
    text = t(
        "edit_menu",
        lang,
        sub_id=sub_id,
        username=html.escape(username),
    )
    if not show_advanced:
        text = f"{text}\n\n{t('wizard_simple_mode_note', lang)}"
    return text


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
            reply_chat_id(update),
            t("menu_main", lang),
            reply_markup=_menu(lang, user_id),
        )
    else:
        await update.effective_message.reply_text(
            t("cancelled", lang),
            reply_markup=_menu(lang, user_id),
        )
    return ConversationHandler.END


async def report_problem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    user_id = update.effective_user.id
    db.upsert_user(user_id)
    lang = _user_lang(context, user_id)
    analytics.capture(user_id, "feedback_opened")
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


async def back_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    await update.effective_message.reply_text(
        t("menu_main", lang),
        reply_markup=_menu(lang, user_id),
    )


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    result = update.my_chat_member
    if result is None or result.chat.type != ChatType.PRIVATE:
        return
    db: Database = context.application.bot_data["db"]
    user_id = result.from_user.id
    status = result.new_chat_member.status
    if status == ChatMemberStatus.BANNED:
        db.set_bot_blocked(user_id, True)
        analytics.capture(user_id, "bot_blocked")
    elif status in (ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED):
        db.set_bot_blocked(user_id, False)
        analytics.capture(user_id, "bot_unblocked")


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


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, Conflict):
        logger.warning(t("conflict_polling", DEFAULT_LOCALE))
        return
    if isinstance(err, NetworkError) and not isinstance(err, BadRequest):
        logger.warning(t("network_transient", DEFAULT_LOCALE, err=err))
        return
    if isinstance(err, BaseException) and _is_unchanged_message_edit(err):
        return
    logger.exception(t("unhandled_error", DEFAULT_LOCALE, err=err))
    user_id = None
    if isinstance(update, Update) and update.effective_user is not None:
        user_id = update.effective_user.id
    analytics.capture_exception(
        err if isinstance(err, BaseException) else None,
        user_id=user_id,
        properties={
            "handler": "telegram_error_handler",
            **(
                chat_context_properties(update)
                if isinstance(update, Update)
                else {}
            ),
        },
    )
    # Best-effort: stop spinner if the handler crashed before answering.
    if isinstance(update, Update) and update.callback_query is not None:
        try:
            await update.callback_query.answer()
        except Exception:
            pass


def build_application(token: str, db: Database, twitch: TwitchClient) -> Application:
    beta_features.load_manifest()
    async def post_init(application: Application) -> None:
        from chat_webapp import register_chat_webapp
        from config import POSTHOG_ISSUE_WEBHOOK_SECRET, twitch_oauth_redirect_uri
        from health import (
            mark_ready,
            register_eventsub_bridge,
            register_oauth_bridge,
            register_posthog_issue_bridge,
        )

        await _restore_broadcast_jobs(application)
        loop = asyncio.get_running_loop()
        register_chat_webapp(db=db, twitch=twitch)
        redirect_uri = twitch_oauth_redirect_uri()
        if redirect_uri:

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
        if POSTHOG_ISSUE_WEBHOOK_SECRET:

            async def on_posthog_issue(payload: dict[str, str]) -> None:
                await notify_admins_posthog_issue(application, payload)

            register_posthog_issue_bridge(loop, on_posthog_issue)

        async def on_eventsub_whisper(event: Any) -> None:
            await notify_whisper_received(application, event)

        async def on_eventsub_revoke(twitch_user_id: str) -> None:
            await on_whisper_eventsub_revoked(application, twitch_user_id)

        register_eventsub_bridge(
            loop, on_whisper=on_eventsub_whisper, on_revoke=on_eventsub_revoke
        )
        # Ready only after Application init — avoid deploy cutting over before polling.
        mark_ready()
        from handlers.settings import ensure_stream_chat_menu_buttons_for_all

        asyncio.create_task(
            ensure_stream_chat_menu_buttons_for_all(application.bot, db),
            name="stream-chat-menu-buttons",
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

    async def reject_group_bot_setup(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if is_private_chat(update):
            return
        if await handle_group_setup_rejection(update, context):
            if update.callback_query:
                raise ApplicationHandlerStop

    app.add_handler(
        MessageHandler(group_setup_menu_filter(), reject_group_bot_setup),
        group=-2,
    )
    app.add_handler(
        CallbackQueryHandler(
            reject_group_bot_setup,
            pattern=GROUP_SETUP_CALLBACK_PATTERN,
            block=False,
        ),
        group=-2,
    )
    app.add_handler(
        CommandHandler(
            ["start", "schedule"],
            reject_group_bot_setup,
            filters=~filters.ChatType.PRIVATE,
        ),
        group=-2,
    )

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
        CallbackQueryHandler(cancel_twitch_import, pattern=r"^import_oauth:cancel$"),
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
        MessageHandler(_btn_filter("cart"), open_cart_menu),
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
    app.add_handler(CommandHandler("stats", admin_show_stats), group=0)
    app.add_handler(
        MessageHandler(_btn_filter("broadcast"), open_broadcast_menu),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("scheduled_broadcasts"), admin_scheduled_list),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("sent_broadcasts"), admin_sent_list),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(on_broadcast_feedback, pattern=r"^bcf:(up|down):\d+$"),
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
        CallbackQueryHandler(
            on_alert_history_page, pattern=r"^alert_history:page:\d+$"
        ),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(on_alert_history_noop, pattern=r"^alert_history:noop$"),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(on_alert_history_more, pattern=r"^alert_history:more$"),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(on_alert_history_menu, pattern=r"^alert_history:menu$"),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("settings"), open_settings_menu),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("other"), open_other_menu),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("chat"), open_stream_chat),
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
        CallbackQueryHandler(on_sync_unfollow_answer, pattern=r"^sync_unfollow:(yes|no)$"),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("sys_notifications"), open_sys_notifications_menu),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("advanced_mode"), open_advanced_mode_menu),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("whisper_alerts"), open_whisper_alerts_menu),
        group=0,
    )
    app.add_handler(
        MessageHandler(_btn_filter("beta_mode"), open_beta_mode_menu),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(on_advanced_mode_toggle, pattern=r"^advanced_mode:toggle$"),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(on_whisper_alerts_toggle, pattern=r"^whisper_alerts:toggle$"),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(on_beta_toggle, pattern=r"^beta:toggle:[\w.-]+$"),
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
            pattern=r"^premium:(pay|month|year|life|cancel|cancel_feat:.+|owned|marfapr|channel|channel_confirm|channel_pay|trial|trial_confirm|features|feat_back|feat_pay|feat_toggle:.+)$",
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
    app.add_handler(CallbackQueryHandler(on_delete_all, pattern=r"^delete_all$"), group=0)
    app.add_handler(
        CallbackQueryHandler(on_delete_all_confirm, pattern=r"^delete_all:(yes|no)$"),
        group=0,
    )
    app.add_handler(CallbackQueryHandler(on_delete_go, pattern=r"^delete_go$"), group=0)
    app.add_handler(CallbackQueryHandler(on_delete_clear, pattern=r"^delete_clear$"), group=0)
    app.add_handler(CallbackQueryHandler(on_delete_type, pattern=r"^delete_type:\w+$"), group=0)
    app.add_handler(
        CallbackQueryHandler(on_list_delete, pattern=r"^list_del:\d+$"), group=0
    )
    app.add_handler(
        CallbackQueryHandler(
            on_list_delete_confirm, pattern=r"^list_del_(ok|no):\d+$"
        ),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(on_delete_cart_open, pattern=r"^delete_cart_open$"),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(on_delete_cart_type, pattern=r"^delete_cart_type:\w+$"),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(on_delete_cart_sel, pattern=r"^delete_cart_sel:\d+$"),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(on_delete_cart_restore_go, pattern=r"^delete_cart_restore_go$"),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(on_delete_cart_clear, pattern=r"^delete_cart_clear$"),
        group=0,
    )
    app.add_handler(CallbackQueryHandler(on_watch_again, pattern=r"^watch:again$"), group=0)
    app.add_handler(
        CallbackQueryHandler(on_watch_create_alerts, pattern=r"^watch:create_alerts$"),
        group=0,
    )
    app.add_handler(CallbackQueryHandler(schedule_save_token_callback, pattern=r"^sched_save_token:"), group=0)
    app.add_handler(CallbackQueryHandler(on_list_type, pattern=r"^list_type:\w+$"), group=0)
    app.add_handler(CallbackQueryHandler(on_list_page, pattern=r"^list_page:\d+$"), group=0)
    app.add_handler(CallbackQueryHandler(on_list_page_noop, pattern=r"^list_page:noop$"), group=0)
    app.add_handler(CallbackQueryHandler(on_edit_type, pattern=r"^edit_type:\w+$"), group=0)
    app.add_handler(CallbackQueryHandler(on_edit_page, pattern=r"^edit_page:\d+$"), group=0)
    app.add_handler(CallbackQueryHandler(on_edit_page_noop, pattern=r"^edit_page:noop$"), group=0)
    app.add_handler(CallbackQueryHandler(on_delete_page, pattern=r"^delete_page:\d+$"), group=0)
    app.add_handler(
        CallbackQueryHandler(on_delete_page_noop, pattern=r"^delete_page:noop$"), group=0
    )
    app.add_handler(
        CallbackQueryHandler(on_share_show, pattern=r"^share_show:\d+$"),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(on_share_accept, pattern=r"^share_accept:[A-Za-z0-9_-]+$"),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(
            on_share_dup_edit, pattern=r"^share_dup:edit:\d+$"
        ),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(
            on_share_dup_continue,
            pattern=r"^share_dup:continue:[A-Za-z0-9_-]+$",
        ),
        group=0,
    )
    app.add_handler(CallbackQueryHandler(on_share_decline, pattern=r"^share_decline$"), group=0)
    app.add_handler(
        CallbackQueryHandler(
            on_edit_change_type_click,
            pattern=r"^edit_f:\d+:change_type$",
        ),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(on_edit_copy_click, pattern=r"^edit_f:\d+:copy$"),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(
            on_edit_copy_change_click,
            pattern=r"^edit_f:\d+:copy_change$",
        ),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(
            on_edit_type_pick,
            pattern=r"^edit_type_pick:(change|copy):\d+:(live|category|upcoming|end)$",
        ),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(
            on_edit_type_pick_cancel,
            pattern=r"^edit_type_pick_cancel:(change|copy):\d+$",
        ),
        group=0,
    )
    app.add_handler(CallbackQueryHandler(on_edit_pick, pattern=r"^edit:\d+$"), group=0)
    app.add_handler(
        CallbackQueryHandler(on_welcome_demo_delete, pattern=r"^welcome_del:\d+$"),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(on_delivery_fail_delete, pattern=r"^delivery_fail_del:\d+$"),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(on_stored_template_typo_fix, pattern=r"^stored_typo_fix:[01]$"),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(
            on_edit_bool_menu,
            pattern=r"^edit_f:\d+:(delete_old|delete_fail|delete_other|preview|chat_button|repeat)$",
        ),
        group=0,
    )
    app.add_handler(
        CallbackQueryHandler(
            on_edit_set,
            pattern=r"^edit_set:\d+:(delete_old|delete_fail|delete_other|preview|chat_button):[01]$|^edit_set:\d+:repeat:1$",
        ),
        group=0,
    )

    _wiz_cancel = MessageHandler(_btn_filter("wizard_cancel"), cancel)
    _wiz_back = MessageHandler(_btn_filter("wizard_back"), wizard_back)

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", dm_only_conv_entry(start)),
            CommandHandler("help", help_command),
            CommandHandler("schedule", dm_only_conv_entry(start_stream_schedule)),
            MessageHandler(_btn_filter("new"), dm_only_conv_entry(start_new_subscription)),
            MessageHandler(_btn_filter("watch"), dm_only_conv_entry(start_what_to_watch)),
            MessageHandler(
                _btn_filter("create_schedule"), dm_only_conv_entry(start_stream_schedule)
            ),
            MessageHandler(
                _btn_filter("language"), dm_only_conv_entry(start_language_change)
            ),
            MessageHandler(
                _btn_filter("ignored_words"), dm_only_conv_entry(start_ignored_words)
            ),
            MessageHandler(
                _btn_filter("pause_notifications"),
                dm_only_conv_entry(start_pause_notifications),
            ),
            MessageHandler(
                _btn_filter("broadcast_new"), dm_only_conv_entry(admin_broadcast_start)
            ),
            CallbackQueryHandler(
                dm_only_conv_entry(on_import_mode_sync), pattern=r"^import_mode:sync$"
            ),
            CallbackQueryHandler(
                dm_only_conv_entry(on_sync_change_period), pattern=r"^sync:period$"
            ),
            CallbackQueryHandler(
                dm_only_conv_entry(start_edit_template), pattern=r"^edit_f:\d+:template$"
            ),
            CallbackQueryHandler(
                dm_only_conv_entry(start_edit_image), pattern=r"^edit_f:\d+:image$"
            ),
            CallbackQueryHandler(
                dm_only_conv_entry(delete_edit_image), pattern=r"^edit_f:\d+:image_del$"
            ),
            CallbackQueryHandler(
                dm_only_conv_entry(start_edit_ignore_keywords),
                pattern=r"^edit_f:\d+:ignore_keywords$",
            ),
            CallbackQueryHandler(
                dm_only_conv_entry(start_edit_dest), pattern=r"^edit_f:\d+:dest$"
            ),
            CallbackQueryHandler(
                dm_only_conv_entry(start_edit_delay), pattern=r"^edit_f:\d+:delay$"
            ),
            CallbackQueryHandler(
                dm_only_conv_entry(start_edit_schedule_reminder),
                pattern=r"^edit_f:\d+:sched_remind$",
            ),
            CallbackQueryHandler(
                dm_only_conv_entry(start_edit_repeat_mute),
                pattern=r"^edit_set:\d+:repeat:0$",
            ),
            CallbackQueryHandler(
                dm_only_conv_entry(start_watch_change), pattern=r"^watch:change$"
            ),
        ],
        states={
            LANG_SELECT: [
                CallbackQueryHandler(cancel, pattern=r"^lang:cancel$"),
                CallbackQueryHandler(receive_language, pattern=r"^lang:(en|ru)$"),
            ],
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
                    receive_image_ask, pattern=r"^image_ask:(add|skip|delete|keep|game_cover)$"
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
                CallbackQueryHandler(
                    receive_ignore_keywords_back, pattern=r"^ignore_keywords:back$"
                ),
                CallbackQueryHandler(cancel, pattern=r"^ignore_keywords:cancel$"),
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
            CHAT_BUTTON_ASK: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(receive_chat_button_ask, pattern=r"^chat_button:"),
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
                CallbackQueryHandler(cancel, pattern=r"^admin_type:cancel$"),
                CallbackQueryHandler(admin_select_type, pattern=r"^admin_type:"),
            ],
            ADMIN_MSG_AUDIENCE: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(cancel, pattern=r"^admin_audience:cancel$"),
                CallbackQueryHandler(admin_audience_callback, pattern=r"^admin_audience:"),
            ],
            ADMIN_MSG_IDS: [
                _wiz_cancel,
                _wiz_back,
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_receive_ids),
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
            STREAM_SCHEDULE_MODE: [
                _wiz_cancel,
                CallbackQueryHandler(cancel, pattern=r"^stream_sched:cancel$"),
                CallbackQueryHandler(
                    stream_schedule_mode_callback, pattern=r"^stream_sched:mode:"
                ),
                CallbackQueryHandler(
                    stream_schedule_tz_callback, pattern=r"^stream_sched:tz:"
                ),
            ],
            STREAM_SCHEDULE_FIX_DAY: [
                _wiz_cancel,
                CallbackQueryHandler(cancel, pattern=r"^stream_sched:cancel$"),
                CallbackQueryHandler(
                    stream_schedule_fix_day_callback,
                    pattern=r"^stream_sched:fix_day:\d+$",
                ),
            ],
            STREAM_SCHEDULE_FIX_SLOTS: [
                _wiz_cancel,
                CallbackQueryHandler(cancel, pattern=r"^stream_sched:cancel$"),
                CallbackQueryHandler(
                    stream_schedule_noop_callback, pattern=r"^stream_sched:noop$"
                ),
                CallbackQueryHandler(
                    stream_schedule_fix_add_callback, pattern=r"^stream_sched:add$"
                ),
                CallbackQueryHandler(
                    stream_schedule_fix_edit_callback,
                    pattern=r"^stream_sched:edit:\d+$",
                ),
                CallbackQueryHandler(
                    stream_schedule_fix_delete_callback,
                    pattern=r"^stream_sched:delete:\d+$",
                ),
                CallbackQueryHandler(
                    stream_schedule_fix_slots_done_callback,
                    pattern=r"^stream_sched:slots_done$",
                ),
            ],
            STREAM_SCHEDULE_FIX_GAME: [
                _wiz_cancel,
                CallbackQueryHandler(cancel, pattern=r"^stream_sched:cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, stream_schedule_fix_game),
            ],
            STREAM_SCHEDULE_FIX_TIME: [
                _wiz_cancel,
                CallbackQueryHandler(cancel, pattern=r"^stream_sched:cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, stream_schedule_fix_time),
            ],
            STREAM_SCHEDULE_MORE: [
                _wiz_cancel,
                CallbackQueryHandler(cancel, pattern=r"^stream_sched:cancel$"),
                CallbackQueryHandler(
                    stream_schedule_more_callback, pattern=r"^stream_sched:more:[01]$"
                ),
            ],
            STREAM_SCHEDULE_CONFIRM: [
                _wiz_cancel,
                CallbackQueryHandler(
                    stream_schedule_confirm_callback, pattern=r"^stream_sched:confirm:"
                ),
                CallbackQueryHandler(
                    stream_schedule_tz_callback, pattern=r"^stream_sched:tz:"
                ),
            ],
            STREAM_SCHEDULE_GAME: [
                _wiz_cancel,
                CallbackQueryHandler(cancel, pattern=r"^stream_sched:cancel$"),
                CallbackQueryHandler(stream_schedule_skip_callback, pattern=r"^stream_sched:skip$"),
                CallbackQueryHandler(stream_schedule_finish_callback, pattern=r"^stream_sched:finish$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, stream_schedule_game),
            ],
            STREAM_SCHEDULE_TIME: [
                _wiz_cancel,
                CallbackQueryHandler(cancel, pattern=r"^stream_sched:cancel$"),
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
            STREAM_SCHEDULE_TZ: [
                _wiz_cancel,
                MessageHandler(filters.TEXT & ~filters.COMMAND, stream_schedule_tz),
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
                CallbackQueryHandler(cancel, pattern=r"^watch_nav:cancel$"),
                CallbackQueryHandler(
                    receive_watch_category_callback, pattern=r"^watch_cat:"
                ),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_watch_category_text),
            ],
            WATCH_TAGS: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(receive_watch_nav_back, pattern=r"^watch_nav:back$"),
                CallbackQueryHandler(cancel, pattern=r"^watch_nav:cancel$"),
                CallbackQueryHandler(
                    receive_watch_tags_callback, pattern=r"^watch_tags:"
                ),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_watch_tags_text),
            ],
            WATCH_VIEWERS: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(receive_watch_nav_back, pattern=r"^watch_nav:back$"),
                CallbackQueryHandler(cancel, pattern=r"^watch_nav:cancel$"),
                CallbackQueryHandler(
                    receive_watch_viewers_callback, pattern=r"^watch_viewers:"
                ),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_watch_viewers_text),
            ],
            WATCH_LANGUAGE: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(receive_watch_nav_back, pattern=r"^watch_nav:back$"),
                CallbackQueryHandler(cancel, pattern=r"^watch_nav:cancel$"),
                CallbackQueryHandler(
                    receive_watch_language_callback, pattern=r"^watch_lang:"
                ),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_watch_language_text),
            ],
            WATCH_MATURE: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(receive_watch_nav_back, pattern=r"^watch_nav:back$"),
                CallbackQueryHandler(cancel, pattern=r"^watch_nav:cancel$"),
                CallbackQueryHandler(
                    receive_watch_mature_callback, pattern=r"^watch_mature:"
                ),
            ],
            WATCH_SAVE: [
                _wiz_cancel,
                _wiz_back,
                CallbackQueryHandler(receive_watch_nav_back, pattern=r"^watch_nav:back$"),
                CallbackQueryHandler(cancel, pattern=r"^watch_nav:cancel$"),
                CallbackQueryHandler(
                    receive_watch_save_callback, pattern=r"^watch_save:"
                ),
            ],
            PAUSE_ALERTS_DAYS: [
                MessageHandler(
                    _btn_filter("wizard_cancel"), cancel_pause_notifications
                ),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, receive_pause_notifications_days
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
                r"^(edit:\d+$|edit_f:|edit_set:|edit_type_pick:|edit_type_pick_cancel:|toggle:|enable_all$|delete:\d+$|"
                r"welcome_del:\d+$|"
                r"delivery_fail_del:|"
                r"delete_sel:|delete_go$|delete_all$|delete_all:(yes|no)$|delete_clear$|delete_type:|"
                r"delete_cart_open$|delete_cart_type:|delete_cart_sel:|delete_cart_restore_go$|delete_cart_clear$|"
                r"list_type:|list_del:\d+$|list_del_ok:\d+$|list_del_no:\d+$|"
                r"sb_edit:\d+$|sb_edit_f:|sb_delete:|"
                r"sys_updates:|sys_availability:|sys_other:|sys_sync:|"
                r"advanced_mode:|whisper_alerts:|"
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
                | _btn_filter("cart")
                | _btn_filter("edit")
                | _btn_filter("delete")
                | _btn_filter("pause_notifications")
                | _btn_filter("alert_history")
                | _btn_filter("other")
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
                | _btn_filter("chat")
                | _btn_filter("create_schedule")
                | _btn_filter("back")
                | _btn_filter("language")
                | _btn_filter("sys_notifications")
                | _btn_filter("ignored_words")
                | _btn_filter("whisper_alerts")
                | _btn_filter("advanced_mode")
                | _btn_filter("sync_subs")
                | _btn_filter("admin")
                | _btn_filter("broadcast")
                | _btn_filter("scheduled_broadcasts")
                | _btn_filter("sent_broadcasts")
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
    app.job_queue.run_repeating(purge_old_broadcasts, interval=24 * 3600, first=300)
    app.job_queue.run_repeating(
        refresh_broadcast_feedback_keyboards, interval=3600, first=180
    )
    app.job_queue.run_repeating(check_twitch_status, interval=120, first=40)
    app.job_queue.run_repeating(check_posthog_status, interval=120, first=50)
    app.job_queue.run_repeating(poll_posthog_inbox_reports, interval=300, first=60)
    app.job_queue.run_repeating(sync_twitch_follows, interval=3600, first=90)
    app.job_queue.run_repeating(refresh_premium_twitch_job, interval=3600, first=120)
    app.job_queue.run_repeating(
        weekly_new_users_report,
        interval=7 * 24 * 3600,
        first=_seconds_until_next_weekly_report(),
    )
    app.job_queue.run_repeating(
        daily_bot_stats_snapshot,
        interval=24 * 3600,
        first=_seconds_until_next_daily_stats(),
    )
    return app
