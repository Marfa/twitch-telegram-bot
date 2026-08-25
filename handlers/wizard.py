from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Any

from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import ContextTypes, ConversationHandler

import analytics
import demo_mode
import premium as prem
from bot_helpers import _menu, _settings_kb, _user_lang, _wizard
from db import Database, Subscription
from handlers.watch import (
    _go_watch_categories_prompt,
    _go_watch_language_prompt,
    _go_watch_mature_prompt,
    _go_watch_pick_prompt,
    _go_watch_tags_prompt,
    _go_watch_viewers_prompt,
)
from hf_text import generate_alert_template
from i18n import (
    admin_other_audience_keyboard,
    admin_type_keyboard,
    admin_wizard_menu,
    alert_type_keyboard,
    all_btn_texts,
    all_wizard_nav_buttons,
    broadcast_menu,
    channel_dup_keyboard,
    chat_button_keyboard,
    delay_keyboard,
    delete_fail_notify_keyboard,
    delete_old_keyboard,
    delete_sibling_keyboard,
    dest_keyboard,
    dest_label,
    ignore_keywords_keyboard,
    image_edit_keyboard,
    image_position_keyboard,
    is_menu_button,
    link_preview_keyboard,
    lucky_preview_keyboard,
    placeholders_link_html,
    premium_gate_keyboard,
    repeat_keyboard,
    schedule_live_add_keyboard,
    schedule_reminder_keyboard,
    t,
    template_strip_keyboard,
    template_typo_keyboard,
)
from links import TelegramTopicLink, parse_telegram_topic_link
from twitch import (
    TwitchClient,
    find_placeholder_typos,
    merge_ignore_keywords,
    normalize_ignore_keywords,
    preview_stream_title,
    render_template,
    template_has_link,
)

logger = logging.getLogger(__name__)


def _wz() -> dict[str, int]:
    from bot import (
        ADMIN_MSG_AUDIENCE,
        ADMIN_MSG_IDS,
        ADMIN_MSG_SCHEDULE,
        ADMIN_MSG_TEXT,
        ADMIN_MSG_TYPE,
        ALERT_TYPE,
        CHANNEL,
        CHANNEL_DUP,
        CHAT_BUTTON_ASK,
        DELAY_MINUTES,
        DELAY_SEND,
        DELETE_FAIL_NOTIFY,
        DELETE_OLD,
        DELETE_SIBLING_ALERTS,
        DEST_CHAT,
        DEST_TYPE,
        EDIT_IGNORE_KEYWORDS,
        EDIT_TEMPLATE,
        IGNORE_KEYWORDS,
        IMAGE_ASK,
        IMAGE_POSITION,
        IMAGE_UPLOAD,
        LINK_PREVIEW,
        LUCKY_PREVIEW,
        PREMIUM_GATE,
        REPEAT_ALLOW,
        REPEAT_MUTE_MINUTES,
        SCHEDULE_LIVE_ASK,
        SCHEDULE_REMINDER_ASK,
        SCHEDULE_REMINDER_MINUTES,
        TEMPLATE,
        TEMPLATE_TYPO_CONFIRM,
        WATCH_DELETE,
        WATCH_LANGUAGE,
        WATCH_MATURE,
        WATCH_SAVE,
        WATCH_TAGS,
        WATCH_VIEWERS,
    )

    return {
        "ADMIN_MSG_AUDIENCE": ADMIN_MSG_AUDIENCE,
        "ADMIN_MSG_IDS": ADMIN_MSG_IDS,
        "ADMIN_MSG_SCHEDULE": ADMIN_MSG_SCHEDULE,
        "ADMIN_MSG_TEXT": ADMIN_MSG_TEXT,
        "ADMIN_MSG_TYPE": ADMIN_MSG_TYPE,
        "ALERT_TYPE": ALERT_TYPE,
        "CHANNEL": CHANNEL,
        "CHANNEL_DUP": CHANNEL_DUP,
        "CHAT_BUTTON_ASK": CHAT_BUTTON_ASK,
        "DELAY_MINUTES": DELAY_MINUTES,
        "DELAY_SEND": DELAY_SEND,
        "DELETE_FAIL_NOTIFY": DELETE_FAIL_NOTIFY,
        "DELETE_OLD": DELETE_OLD,
        "DELETE_SIBLING_ALERTS": DELETE_SIBLING_ALERTS,
        "DEST_CHAT": DEST_CHAT,
        "DEST_TYPE": DEST_TYPE,
        "EDIT_IGNORE_KEYWORDS": EDIT_IGNORE_KEYWORDS,
        "EDIT_TEMPLATE": EDIT_TEMPLATE,
        "IGNORE_KEYWORDS": IGNORE_KEYWORDS,
        "IMAGE_ASK": IMAGE_ASK,
        "IMAGE_POSITION": IMAGE_POSITION,
        "IMAGE_UPLOAD": IMAGE_UPLOAD,
        "LINK_PREVIEW": LINK_PREVIEW,
        "LUCKY_PREVIEW": LUCKY_PREVIEW,
        "PREMIUM_GATE": PREMIUM_GATE,
        "REPEAT_ALLOW": REPEAT_ALLOW,
        "REPEAT_MUTE_MINUTES": REPEAT_MUTE_MINUTES,
        "SCHEDULE_LIVE_ASK": SCHEDULE_LIVE_ASK,
        "SCHEDULE_REMINDER_ASK": SCHEDULE_REMINDER_ASK,
        "SCHEDULE_REMINDER_MINUTES": SCHEDULE_REMINDER_MINUTES,
        "TEMPLATE": TEMPLATE,
        "TEMPLATE_TYPO_CONFIRM": TEMPLATE_TYPO_CONFIRM,
        "WATCH_DELETE": WATCH_DELETE,
        "WATCH_LANGUAGE": WATCH_LANGUAGE,
        "WATCH_MATURE": WATCH_MATURE,
        "WATCH_SAVE": WATCH_SAVE,
        "WATCH_TAGS": WATCH_TAGS,
        "WATCH_VIEWERS": WATCH_VIEWERS,
    }


def _subs_for_owner(db: Database, owner_id: int):
    from bot import _subs_for_owner as _impl

    return _impl(db, owner_id)


def _owner_sub_number(db: Database, owner_id: int, sub_id: int) -> int:
    from bot import _owner_sub_number as _impl

    return _impl(db, owner_id, sub_id)


def _ignore_keywords_note(keywords: str, use_global: bool, lang: str) -> str:
    from bot import _ignore_keywords_note as _impl

    return _impl(keywords, use_global, lang)


def _edit_menu_text(lang: str, sub, sub_num: int) -> str:
    from bot import _edit_menu_text as _impl

    return _impl(lang, sub, sub_num)


def _edit_options_for_sub(sub, lang: str):
    from bot import _edit_options_for_sub as _impl

    return _impl(sub, lang)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from bot import cancel as _impl

    return await _impl(update, context)


async def _save_edit_image(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> int:
    from bot import _save_edit_image as _impl

    return await _impl(update, context, lang)


async def _save_edit_template(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str, template: str) -> int:
    from bot import _save_edit_template as _impl

    return await _impl(update, context, lang, template)


async def _prompt_edit_template(*, bot, user_id: int, lang: str, sub, sub_num: int) -> None:
    from bot import _prompt_edit_template as _impl

    return await _impl(bot=bot, user_id=user_id, lang=lang, sub=sub, sub_num=sub_num)


async def _deliver_alert_content(*args, **kwargs):
    from handlers.delivery import _deliver_alert_content as _impl

    return await _impl(*args, **kwargs)


def _membership_check_blocked(exc: BadRequest | Forbidden) -> bool:
    msg = str(exc).lower()
    return "member list is inaccessible" in msg or "chat_admin_required" in msg


async def _resolve_from_topic_link(bot, link: TelegramTopicLink) -> tuple[int, int]:
    from bot import _resolve_from_topic_link as _impl

    return await _impl(bot, link)


def _extract_forward_chat(message) -> tuple[int | None, int | None]:
    from bot import _extract_forward_chat as _impl

    return _impl(message)


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
    if not await prem.advanced_mode_on(context.bot, db, user_id, channel=_wizard_channel(context)):
        context.user_data["suppress_repeat_minutes"] = 0
        return await _go_after_repeat(update, context, lang)
    if not await prem.has_feature(
        context.bot, db, user_id, "repeat", channel=_wizard_channel(context)
    ):
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
    _set_wizard_back(context, _wz()["REPEAT_ALLOW"])
    return _wz()["REPEAT_ALLOW"]

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
    text = _premium_gate_text(lang, feature, action)
    markup = premium_gate_keyboard(lang, first_step=first_step)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)
    return _wz()["PREMIUM_GATE"]

_GATE_FEATURE_LABEL = {
    "alert_type": "premium_feat_alert_types",
    "sync": "premium_feat_twitch_sync",
    "ignore_keywords": "premium_feat_ignore_keywords",
    "delay": "premium_feat_delay",
    "repeat": "premium_feat_repeat",
    "delete_old": "premium_feat_delete_prev",
    "delete_fail": "premium_feat_delete_prev",
}

def _premium_gate_text(lang: str, feature: str, action: str) -> str:
    if feature == "active_limit":
        return t("premium_active_limit", lang, limit=prem.free_active_limit())
    label_key = _GATE_FEATURE_LABEL.get(feature)
    if label_key:
        reason = t(label_key, lang, free_limit=prem.free_active_limit())
        return t("premium_gate_feature", lang, feature=reason, action=action)
    return t("premium_gate", lang, action=action)

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
            reply_markup=_settings_kb(lang, db, user_id),
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
    return await cancel(update, context)

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
    _set_wizard_back(context, _wz()["SCHEDULE_REMINDER_ASK"])
    return _wz()["SCHEDULE_REMINDER_ASK"]

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
    _set_wizard_back(context, _wz()["DEST_TYPE"])
    return _wz()["DEST_TYPE"]

async def _go_chat_button_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    chat_id = update.effective_user.id
    context.user_data["attach_chat_button"] = False
    text = t("chat_button_prompt", lang)
    markup = chat_button_keyboard(lang)
    if update.callback_query:
        await context.bot.send_message(chat_id, text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)
    _set_wizard_back(context, _wz()["CHAT_BUTTON_ASK"])
    return _wz()["CHAT_BUTTON_ASK"]

async def _go_before_dest_step(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    db: Database = context.application.bot_data["db"]
    user_id = update.effective_user.id
    if not await prem.advanced_mode_on(context.bot, db, user_id, channel=_wizard_channel(context)):
        context.user_data.setdefault("attach_chat_button", False)
        return await _prompt_dest_step(update, context, lang)
    return await _go_chat_button_prompt(update, context, lang)

async def _wizard_back_before_dest(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    if context.user_data.get("lucky_quick"):
        return await _show_lucky_preview(update, context, lang)
    if context.user_data.get("alert_type") == "upcoming":
        return await _go_schedule_reminder_minutes(update, context, lang)
    if context.user_data.get("alert_type") in ("end", "category"):
        after = context.user_data.get("after_delay_state", _wz()["DELAY_SEND"])
        if after == _wz()["DELAY_MINUTES"]:
            return await _go_delay_minutes_prompt(update, context, lang)
        return await _go_delay_prompt(update, context, lang)
    if context.user_data.get("schedule_reminder_offered"):
        if int(context.user_data.get("schedule_reminder_minutes", 0)) > 0:
            return await _go_schedule_reminder_minutes(update, context, lang)
        return await _go_schedule_reminder_ask(update, context, lang)
    return await _go_repeat_prompt(update, context, lang)

async def _go_channel_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> int:
    has_alert_type = bool(context.user_data.get("alert_type"))
    await update.effective_message.reply_text(
        t("new_sub_prompt", lang),
        reply_markup=_wizard(lang, back=has_alert_type),
    )
    _set_wizard_back(context, _wz()["CHANNEL"])
    return _wz()["CHANNEL"]

async def _go_alert_type_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    chat_id = update.effective_user.id
    db: Database = context.application.bot_data["db"]
    text = t("alert_type_prompt", lang)
    if not await prem.advanced_mode_on(context.bot, db, chat_id):
        text = f"{text}\n\n{t('wizard_simple_mode_note', lang)}"
    markup = alert_type_keyboard(lang)
    parse_mode = ParseMode.HTML if "<b>" in text else None
    if update.callback_query:
        await context.bot.send_message(
            chat_id, text, reply_markup=markup, parse_mode=parse_mode
        )
    else:
        await update.effective_message.reply_text(
            text, reply_markup=markup, parse_mode=parse_mode
        )
    _set_wizard_back(context, _wz()["ALERT_TYPE"])
    return _wz()["ALERT_TYPE"]

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
    _set_wizard_back(context, _wz()["TEMPLATE"])
    return _wz()["TEMPLATE"]

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
    _set_wizard_back(context, _wz()["IMAGE_ASK"])
    return _wz()["IMAGE_ASK"]

async def _go_ignore_keywords_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> int:
    db: Database = context.application.bot_data["db"]
    user_id = update.effective_user.id
    if not await prem.advanced_mode_on(context.bot, db, user_id, channel=_wizard_channel(context)):
        context.user_data["ignore_keywords"] = ""
        context.user_data["use_global_ignore"] = False
        return await _go_after_ignore_keywords(update, context, lang)
    if not await prem.has_feature(
        context.bot, db, user_id, "ignore_keywords", channel=_wizard_channel(context)
    ):
        return await _show_premium_gate(
            update, context, feature="ignore_keywords", first_step=False
        )
    context.user_data.setdefault("use_global_ignore", False)
    context.user_data["ignore_keywords_as_cancel"] = False
    # Inline Back/Cancel (like template step). Reply pulse+delete is unreliable here.
    await update.effective_message.reply_text(
        t("ignore_keywords_prompt", lang),
        parse_mode=ParseMode.HTML,
        reply_markup=ignore_keywords_keyboard(
            lang,
            use_global=bool(context.user_data.get("use_global_ignore")),
            show_back=True,
            show_cancel=True,
        ),
    )
    _set_wizard_back(context, _wz()["IGNORE_KEYWORDS"])
    return _wz()["IGNORE_KEYWORDS"]

async def _go_link_preview_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> int:
    chat_id = update.effective_user.id
    text = t("link_preview_prompt", lang)
    markup = link_preview_keyboard(lang)
    if update.callback_query:
        await context.bot.send_message(chat_id, text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)
    _set_wizard_back(context, _wz()["LINK_PREVIEW"])
    return _wz()["LINK_PREVIEW"]

async def _go_delay_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> int:
    db: Database = context.application.bot_data["db"]
    user_id = update.effective_user.id
    if not await prem.advanced_mode_on(context.bot, db, user_id, channel=_wizard_channel(context)):
        context.user_data["delay_minutes"] = 0
        return await _continue_after_delay(update, context, lang)
    if not await prem.has_feature(
        context.bot, db, user_id, "delay", channel=_wizard_channel(context)
    ):
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
    _set_wizard_back(context, _wz()["DELAY_SEND"])
    return _wz()["DELAY_SEND"]

async def _go_delay_minutes_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> int:
    await update.effective_message.reply_text(
        t("delay_minutes_prompt", lang),
        reply_markup=_wizard(lang),
    )
    _set_wizard_back(context, _wz()["DELAY_MINUTES"])
    return _wz()["DELAY_MINUTES"]

def _wizard_channel(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    raw = context.user_data.get("twitch_username")
    return str(raw) if raw else None

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
    _set_wizard_back(context, _wz()["SCHEDULE_REMINDER_MINUTES"])
    return _wz()["SCHEDULE_REMINDER_MINUTES"]

async def _go_after_repeat(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    """After repeat step: offer schedule reminders if Twitch schedule exists."""
    context.user_data.setdefault("schedule_reminder_minutes", 0)
    context.user_data.setdefault("schedule_reminder_configured", False)
    context.user_data.pop("schedule_reminder_offered", None)
    if context.user_data.get("skip_schedule_check"):
        return await _go_before_dest_step(update, context, lang)
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
        return await _go_before_dest_step(update, context, lang)
    return await _prompt_schedule_reminder_ask(update, context, lang)

async def _go_dest_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> int:
    return await _prompt_dest_step(update, context, lang)

async def wizard_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = _user_lang(context, update.effective_user.id)
    state = context.user_data.get("wizard_back_state")
    if state == _wz()["CHANNEL"]:
        # Back to alert-type picker: drop channel + everything chosen after it.
        # Leaving dest_type/template behind could finish a create without twitch_username.
        for key in (
            "twitch_username",
            "twitch_user_id",
            "twitch_display_name",
            "channel_input_was_url",
            "message_template",
            "pending_template",
            "pending_template_preview_disabled",
            "image_file_id",
            "image_position",
            "ignore_keywords",
            "use_global_ignore",
            "disable_link_preview",
            "strip_name_mentions",
            "attach_chat_button",
            "delay_minutes",
            "suppress_repeat_minutes",
            "schedule_reminder_minutes",
            "schedule_reminder_configured",
            "schedule_reminder_offered",
            "after_delay_state",
            "dest_type",
            "delete_previous",
            "notify_delete_fail",
            "delete_other_alerts",
            "delete_sibling_asked",
            "pending_chat_id",
            "pending_thread_id",
            "lucky_quick",
            "alert_type",
            "notify_on_live",
            "notify_on_end",
            "notify_on_category_change",
            "skip_schedule_check",
        ):
            context.user_data.pop(key, None)
        return await _go_alert_type_prompt(update, context, lang)
    if state == _wz()["TEMPLATE"]:
        context.user_data.pop("pending_template", None)
        context.user_data.pop("pending_template_preview_disabled", None)
        return await _go_channel_prompt(update, context, lang)
    if state == _wz()["CHANNEL_DUP"]:
        return await _go_channel_prompt(update, context, lang)
    if state == _wz()["IMAGE_ASK"]:
        if context.user_data.get("edit_sub_id"):
            owner_id = update.effective_user.id
            context.user_data.clear()
            await update.effective_message.reply_text(
                t("cancelled", lang),
                reply_markup=_menu(lang, owner_id),
            )
            return ConversationHandler.END
        return await _go_template_prompt(update, context, lang)
    if state == _wz()["IMAGE_UPLOAD"]:
        return await _go_image_ask_prompt(update, context, lang)
    if state == _wz()["IMAGE_POSITION"]:
        await update.effective_message.reply_text(
            t("image_send_prompt", lang),
            reply_markup=_wizard(lang, back=not bool(context.user_data.get("edit_sub_id"))),
        )
        _set_wizard_back(context, _wz()["IMAGE_UPLOAD"])
        return _wz()["IMAGE_UPLOAD"]
    if state == _wz()["LUCKY_PREVIEW"]:
        return await _go_template_prompt(update, context, lang)
    if state == _wz()["IGNORE_KEYWORDS"]:
        return await _go_image_ask_prompt(update, context, lang)
    if state == _wz()["LINK_PREVIEW"]:
        return await _go_ignore_keywords_prompt(update, context, lang)
    if state == _wz()["DELAY_SEND"]:
        if context.user_data.get("image_file_id") or not template_has_link(
            str(context.user_data.get("message_template") or "")
        ):
            return await _go_ignore_keywords_prompt(update, context, lang)
        return await _go_link_preview_prompt(update, context, lang)
    if state == _wz()["DELAY_MINUTES"]:
        return await _go_delay_prompt(update, context, lang)
    if state == _wz()["REPEAT_ALLOW"]:
        after = context.user_data.get("after_delay_state", _wz()["DELAY_SEND"])
        if after == _wz()["DELAY_MINUTES"]:
            return await _go_delay_minutes_prompt(update, context, lang)
        return await _go_delay_prompt(update, context, lang)
    if state == _wz()["REPEAT_MUTE_MINUTES"]:
        return await _go_repeat_prompt(update, context, lang)
    if state == _wz()["SCHEDULE_REMINDER_ASK"]:
        return await _go_repeat_prompt(update, context, lang)
    if state == _wz()["SCHEDULE_REMINDER_MINUTES"]:
        if context.user_data.get("alert_type") == "upcoming":
            # Upcoming skips delay/repeat; back to preview or ignore.
            if context.user_data.get("image_file_id") or not template_has_link(
                str(context.user_data.get("message_template") or "")
            ):
                return await _go_ignore_keywords_prompt(update, context, lang)
            return await _go_link_preview_prompt(update, context, lang)
        return await _go_schedule_reminder_ask(update, context, lang)
    if state == _wz()["DEST_TYPE"]:
        db: Database = context.application.bot_data["db"]
        if await prem.advanced_mode_on(
            context.bot, db, update.effective_user.id, channel=_wizard_channel(context)
        ):
            return await _go_chat_button_prompt(update, context, lang)
        return await _wizard_back_before_dest(update, context, lang)
    if state == _wz()["CHAT_BUTTON_ASK"]:
        return await _wizard_back_before_dest(update, context, lang)
    if state == _wz()["DEST_CHAT"]:
        return await _go_dest_prompt(update, context, lang)
    if state == _wz()["DELETE_OLD"]:
        dest_type = context.user_data.get("dest_type")
        if dest_type == "dm":
            return await _go_dest_prompt(update, context, lang)
        setup_key = "channel_setup" if dest_type == "channel" else "group_setup"
        await update.effective_message.reply_text(
            t(setup_key, lang),
            reply_markup=_wizard(lang),
        )
        _set_wizard_back(context, _wz()["DEST_CHAT"])
        return _wz()["DEST_CHAT"]
    if state == _wz()["DELETE_SIBLING_ALERTS"]:
        await update.effective_message.reply_text(
            _delete_old_prompt_text(context, lang),
            reply_markup=delete_old_keyboard(lang),
        )
        _set_wizard_back(context, _wz()["DELETE_OLD"])
        return _wz()["DELETE_OLD"]
    if state == _wz()["DELETE_FAIL_NOTIFY"]:
        if context.user_data.get("delete_sibling_asked"):
            await update.effective_message.reply_text(
                t("delete_sibling_text", lang),
                reply_markup=delete_sibling_keyboard(lang),
            )
            _set_wizard_back(context, _wz()["DELETE_SIBLING_ALERTS"])
            return _wz()["DELETE_SIBLING_ALERTS"]
        await update.effective_message.reply_text(
            _delete_old_prompt_text(context, lang),
            reply_markup=delete_old_keyboard(lang),
        )
        _set_wizard_back(context, _wz()["DELETE_OLD"])
        return _wz()["DELETE_OLD"]
    if state == _wz()["ADMIN_MSG_TEXT"]:
        if context.user_data.get("admin_msg_type") == "other":
            await update.effective_message.reply_text(
                t("broadcast_audience_prompt", lang),
                reply_markup=admin_other_audience_keyboard(lang),
            )
            _set_wizard_back(context, _wz()["ADMIN_MSG_AUDIENCE"])
            return _wz()["ADMIN_MSG_AUDIENCE"]
        user_id = update.effective_user.id
        await update.effective_message.reply_text(
            t("broadcast_prompt", lang),
            reply_markup=admin_type_keyboard(lang),
        )
        _set_wizard_back(context, _wz()["ADMIN_MSG_TYPE"])
        return _wz()["ADMIN_MSG_TYPE"]
    if state == _wz()["ADMIN_MSG_IDS"]:
        await update.effective_message.reply_text(
            t("broadcast_audience_prompt", lang),
            reply_markup=admin_other_audience_keyboard(lang),
        )
        _set_wizard_back(context, _wz()["ADMIN_MSG_AUDIENCE"])
        return _wz()["ADMIN_MSG_AUDIENCE"]
    if state == _wz()["ADMIN_MSG_AUDIENCE"]:
        await update.effective_message.reply_text(
            t("broadcast_prompt", lang),
            reply_markup=admin_type_keyboard(lang),
        )
        _set_wizard_back(context, _wz()["ADMIN_MSG_TYPE"])
        return _wz()["ADMIN_MSG_TYPE"]
    if state == _wz()["ADMIN_MSG_SCHEDULE"]:
        await update.effective_message.reply_text(
            t("broadcast_text_prompt", lang),
            reply_markup=admin_wizard_menu(lang),
        )
        _set_wizard_back(context, _wz()["ADMIN_MSG_TEXT"])
        return _wz()["ADMIN_MSG_TEXT"]
    if state == _wz()["WATCH_DELETE"]:
        return await _go_watch_pick_prompt(update, context, lang)
    if state == _wz()["WATCH_TAGS"]:
        return await _go_watch_categories_prompt(update, context, lang)
    if state == _wz()["WATCH_VIEWERS"]:
        return await _go_watch_tags_prompt(update, context, lang)
    if state == _wz()["WATCH_LANGUAGE"]:
        return await _go_watch_viewers_prompt(update, context, lang)
    if state == _wz()["WATCH_MATURE"]:
        return await _go_watch_language_prompt(update, context, lang)
    if state == _wz()["WATCH_SAVE"]:
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


async def _send_test(*args, **kwargs):
    from handlers.delivery import _send_test as _impl

    return await _impl(*args, **kwargs)


async def _user_can_manage_chat(bot, chat_id: int, user_id: int) -> bool | None:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except (BadRequest, Forbidden) as exc:
        if _membership_check_blocked(exc):
            logger.warning(
                "Cannot verify membership of %s in %s (bot needs admin rights): %s",
                user_id,
                chat_id,
                exc,
            )
            return None
        logger.warning(
            "Cannot verify membership of %s in %s: %s", user_id, chat_id, exc
        )
        return False
    return member.status in (
        ChatMemberStatus.OWNER,
        ChatMemberStatus.ADMINISTRATOR,
    )

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
    # Active-limit gate is at finish (promo channel marfapr is always enableable).
    return await _go_alert_type_prompt(update, context, lang)

async def receive_alert_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    kind = query.data.split(":", 1)[1]
    if kind not in ("live", "category", "upcoming", "end"):
        return _wz()["ALERT_TYPE"]
    # Non-live types: Premium gate after channel is known (promo channel unlocks).
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
    if is_menu_button(text):
        await update.effective_message.reply_text(t("finish_setup_first", lang))
        return _wz()["CHANNEL"]

    twitch: TwitchClient = context.application.bot_data["twitch"]
    username = twitch.parse_username(text)
    if not username:
        await update.effective_message.reply_text(t("channel_not_parsed", lang))
        return _wz()["CHANNEL"]

    user = await asyncio.to_thread(twitch.get_user, username)
    if not user:
        await update.effective_message.reply_text(
            t("channel_not_found", lang, username=username)
        )
        return _wz()["CHANNEL"]

    context.user_data["twitch_username"] = user["login"]
    context.user_data["twitch_user_id"] = user["id"]
    context.user_data["twitch_display_name"] = user.get("display_name") or user["login"]
    context.user_data["channel_input_was_url"] = twitch.is_twitch_url(text)

    db: Database = context.application.bot_data["db"]
    alert_kind = context.user_data.get("alert_type") or "live"
    if alert_kind != "live" and not await prem.has_feature(
        context.bot,
        db,
        update.effective_user.id,
        "alert_types",
        channel=user["login"],
    ):
        return await _show_premium_gate(
            update, context, feature="alert_type", first_step=True
        )

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
            return _wz()["CHANNEL"]

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
        _set_wizard_back(context, _wz()["CHANNEL_DUP"])
        return _wz()["CHANNEL_DUP"]

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
        show_adv = await prem.advanced_mode_on(
            context.bot, db, query.from_user.id, channel=sub.twitch_username
        )
        await query.edit_message_text(
            _edit_menu_text(
                lang,
                sub_id=sub_num,
                username=sub.twitch_username,
                show_advanced=show_adv,
            ),
            reply_markup=_edit_options_for_sub(sub, lang, show_advanced=show_adv),
            parse_mode=ParseMode.HTML,
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
    if is_menu_button(template):
        await update.effective_message.reply_text(t("finish_setup_first", lang))
        return _wz()["TEMPLATE"]
    if not template:
        await update.effective_message.reply_text(t("template_empty", lang))
        return _wz()["TEMPLATE"]

    if await _offer_template_typo_fix(update, context, lang, template):
        return _wz()["TEMPLATE_TYPO_CONFIRM"]

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
            return _wz()["EDIT_TEMPLATE"]
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
        _set_wizard_back(context, _wz()["TEMPLATE"])
        return _wz()["TEMPLATE"]

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
    _set_wizard_back(context, _wz()["LUCKY_PREVIEW"])
    return _wz()["LUCKY_PREVIEW"]

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
        _set_wizard_back(context, _wz()["TEMPLATE"])
        return _wz()["TEMPLATE"]

    context.user_data["message_template"] = template
    return await _show_lucky_preview(update, context, lang)

async def lucky_continue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    if not context.user_data.get("message_template"):
        await query.edit_message_text(t("template_empty", lang))
        return _wz()["TEMPLATE"]
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
    return await _go_before_dest_step(update, context, lang)

async def receive_chat_button_ask(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    yes = query.data.endswith(":1")
    context.user_data["attach_chat_button"] = yes
    if yes:
        context.user_data["disable_link_preview"] = True
    await query.edit_message_text("✓")
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
    _set_wizard_back(context, _wz()["IMAGE_UPLOAD"])
    return _wz()["IMAGE_UPLOAD"]

async def receive_image_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = _user_lang(context, update.effective_user.id)
    message = update.effective_message
    photo = message.photo
    if not photo:
        await message.reply_text(t("image_need_photo", lang))
        return _wz()["IMAGE_UPLOAD"]
    context.user_data["image_file_id"] = photo[-1].file_id
    await message.reply_text(
        t("image_position_prompt", lang),
        reply_markup=image_position_keyboard(lang),
    )
    _set_wizard_back(context, _wz()["IMAGE_POSITION"])
    return _wz()["IMAGE_POSITION"]

async def receive_image_position(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    position = query.data.split(":", 1)[1]
    if position not in ("before", "after"):
        return _wz()["IMAGE_POSITION"]
    context.user_data["image_position"] = position
    await query.edit_message_text("✓")
    if context.user_data.get("edit_sub_id"):
        return await _save_edit_image(update, context, lang)
    return await _go_ignore_keywords_prompt(update, context, lang)

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
    return _wz()["EDIT_TEMPLATE"] if editing else _wz()["TEMPLATE"]

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
    if text in all_wizard_nav_buttons():
        if text in all_btn_texts("wizard_cancel"):
            return await cancel(update, context)
        return await wizard_back(update, context)
    if is_menu_button(text):
        await update.effective_message.reply_text(t("finish_setup_first", lang))
        return _wz()["IGNORE_KEYWORDS"]

    context.user_data["ignore_keywords"] = normalize_ignore_keywords(text)
    return await _go_after_ignore_keywords(update, context, lang)

async def receive_ignore_keywords_back(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    return await wizard_back(update, context)

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
        as_cancel = bool(context.user_data.get("ignore_keywords_as_cancel"))
        await query.edit_message_reply_markup(
            reply_markup=ignore_keywords_keyboard(
                lang,
                as_cancel=as_cancel,
                use_global=False,
                show_back=not editing and not as_cancel,
                show_cancel=not as_cancel,
            )
        )
        return _wz()["EDIT_IGNORE_KEYWORDS"] if editing else _wz()["IGNORE_KEYWORDS"]

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
        context.user_data["after_delay_state"] = _wz()["DELAY_MINUTES"]
        await query.edit_message_text("✓")
        await context.bot.send_message(
            query.from_user.id,
            t("delay_minutes_prompt", lang),
            reply_markup=_wizard(lang),
        )
        _set_wizard_back(context, _wz()["DELAY_MINUTES"])
        return _wz()["DELAY_MINUTES"]
    context.user_data["delay_minutes"] = 0
    context.user_data["after_delay_state"] = _wz()["DELAY_SEND"]
    return await _continue_after_delay(update, context, lang)

async def receive_delay_minutes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = _user_lang(context, update.effective_user.id)
    raw = (update.effective_message.text or "").strip()
    if is_menu_button(raw):
        await update.effective_message.reply_text(t("finish_setup_first", lang))
        return _wz()["DELAY_MINUTES"]
    if not raw.isdigit() or int(raw) < 1:
        await update.effective_message.reply_text(t("delay_minutes_invalid", lang))
        return _wz()["DELAY_MINUTES"]
    context.user_data["delay_minutes"] = int(raw)
    context.user_data["after_delay_state"] = _wz()["DELAY_MINUTES"]
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
    _set_wizard_back(context, _wz()["REPEAT_MUTE_MINUTES"])
    return _wz()["REPEAT_MUTE_MINUTES"]

async def receive_repeat_mute_minutes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = _user_lang(context, update.effective_user.id)
    raw = (update.effective_message.text or "").strip()
    if is_menu_button(raw):
        await update.effective_message.reply_text(t("finish_setup_first", lang))
        return _wz()["REPEAT_MUTE_MINUTES"]
    if not raw.isdigit() or int(raw) < 1:
        await update.effective_message.reply_text(t("repeat_mute_invalid", lang))
        return _wz()["REPEAT_MUTE_MINUTES"]
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
        return await _go_before_dest_step(update, context, lang)
    context.user_data["notify_on_live"] = False
    await query.edit_message_text("✓")
    await context.bot.send_message(
        query.from_user.id,
        t("schedule_reminder_minutes_prompt", lang),
        reply_markup=_wizard(lang),
    )
    _set_wizard_back(context, _wz()["SCHEDULE_REMINDER_MINUTES"])
    return _wz()["SCHEDULE_REMINDER_MINUTES"]

async def receive_schedule_reminder_minutes(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    lang = _user_lang(context, update.effective_user.id)
    raw = (update.effective_message.text or "").strip()
    if is_menu_button(raw):
        await update.effective_message.reply_text(t("finish_setup_first", lang))
        return _wz()["SCHEDULE_REMINDER_MINUTES"]
    if not raw.isdigit() or int(raw) < 1:
        await update.effective_message.reply_text(
            t("schedule_reminder_minutes_invalid", lang)
        )
        return _wz()["SCHEDULE_REMINDER_MINUTES"]
    context.user_data["schedule_reminder_minutes"] = int(raw)
    context.user_data["schedule_reminder_configured"] = True
    return await _go_before_dest_step(update, context, lang)

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
    "attach_chat_button",
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

async def receive_dest_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    dest_type = query.data.split(":", 1)[1]
    context.user_data["dest_type"] = dest_type

    if dest_type == "dm":
        if not context.user_data.get("edit_sub_id") and not (
            context.user_data.get("twitch_username")
            and context.user_data.get("twitch_user_id")
            and context.user_data.get("message_template")
        ):
            logger.warning(
                "dest:dm without create payload for %s keys=%s",
                query.from_user.id,
                sorted(context.user_data.keys()),
            )
            await query.edit_message_text(t("save_failed", lang))
            await context.bot.send_message(
                query.from_user.id,
                t("menu_main", lang),
                reply_markup=_menu(lang, query.from_user.id),
            )
            context.user_data.clear()
            return ConversationHandler.END
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
    _set_wizard_back(context, _wz()["DEST_CHAT"])
    return _wz()["DEST_CHAT"]

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

    if message.forward_origin:
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
        return _wz()["DEST_CHAT"]
    if chat_id is None:
        await message.reply_text(t("chat_not_determined", lang))
        return _wz()["DEST_CHAT"]

    if dest_type == "channel":
        try:
            chat = await context.bot.get_chat(chat_id)
            if chat.type != ChatType.CHANNEL:
                await message.reply_text(t("not_a_channel", lang))
                return _wz()["DEST_CHAT"]
        except BadRequest:
            await message.reply_text(t("bot_no_channel", lang))
            return _wz()["DEST_CHAT"]

    if dest_type == "group":
        try:
            chat = await context.bot.get_chat(chat_id)
            if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
                await message.reply_text(t("not_a_group", lang))
                return _wz()["DEST_CHAT"]
        except BadRequest:
            await message.reply_text(t("bot_no_group", lang))
            return _wz()["DEST_CHAT"]

    can_manage = await _user_can_manage_chat(
        context.bot, chat_id, update.effective_user.id
    )
    if can_manage is None:
        await message.reply_text(t("bot_cannot_verify_admin", lang))
        return _wz()["DEST_CHAT"]
    if not can_manage:
        await message.reply_text(t("dest_not_admin", lang))
        return _wz()["DEST_CHAT"]

    ok = await _send_test(context.bot, chat_id, thread_id, t("test_ok", lang))
    if not ok:
        await message.reply_text(t("test_failed", lang))
        return _wz()["DEST_CHAT"]

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
    user_id = update.effective_user.id
    if not await prem.advanced_mode_on(context.bot, db, user_id, channel=_wizard_channel(context)):
        context.user_data["delete_previous"] = False
        context.user_data["notify_delete_fail"] = False
        context.user_data["delete_other_alerts"] = False
        chat_id = context.user_data.get("pending_chat_id", user_id)
        thread_id = context.user_data.get("pending_thread_id")
        return await _finish_subscription(
            update, context, user_id, chat_id, thread_id
        )
    if not await prem.has_feature(
        context.bot, db, user_id, "delete_prev", channel=_wizard_channel(context)
    ):
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
    _set_wizard_back(context, _wz()["DELETE_OLD"])
    return _wz()["DELETE_OLD"]

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

async def _prompt_delete_fail_notify(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    db: Database = context.application.bot_data["db"]
    user_id = update.effective_user.id
    if not await prem.has_feature(
        context.bot, db, user_id, "delete_prev", channel=_wizard_channel(context)
    ):
        return await _show_premium_gate(
            update, context, feature="delete_fail", first_step=False
        )
    text = t("delete_fail_notify_text", lang)
    markup = delete_fail_notify_keyboard(lang)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)
    _set_wizard_back(context, _wz()["DELETE_FAIL_NOTIFY"])
    return _wz()["DELETE_FAIL_NOTIFY"]

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
            _set_wizard_back(context, _wz()["DELETE_SIBLING_ALERTS"])
            return _wz()["DELETE_SIBLING_ALERTS"]
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
    dest_type = data.get("dest_type")
    if not dest_type:
        logger.warning(
            "finish_subscription missing dest_type for %s keys=%s",
            owner_id,
            sorted(data.keys()),
        )
        await context.bot.send_message(
            owner_id,
            t("save_failed", lang),
            reply_markup=_menu(lang, owner_id),
        )
        context.user_data.clear()
        return ConversationHandler.END
    if not edit_sub_id and not (
        data.get("twitch_username")
        and data.get("twitch_user_id")
        and data.get("message_template")
    ):
        logger.warning(
            "finish_subscription incomplete create for %s keys=%s",
            owner_id,
            sorted(data.keys()),
        )
        await context.bot.send_message(
            owner_id,
            t("save_failed", lang),
            reply_markup=_menu(lang, owner_id),
        )
        context.user_data.clear()
        return ConversationHandler.END
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
                or bool(data.get("image_file_id"))
                or bool(data.get("attach_chat_button")),
                strip_name_mentions=bool(data.get("strip_name_mentions")),
                attach_chat_button=bool(data.get("attach_chat_button")),
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
            if not await prem.can_enable_more_async(
                context.bot,
                db,
                owner_id,
                twitch_username=str(data.get("twitch_username") or ""),
            ):
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
                or bool(data.get("image_file_id"))
                or bool(data.get("attach_chat_button")),
                strip_name_mentions=bool(data.get("strip_name_mentions")),
                attach_chat_button=bool(data.get("attach_chat_button")),
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

    analytics.capture(
        owner_id,
        "subscription_updated" if edit_sub_id else "subscription_created",
        {
            "dest_type": dest_type,
            "notify_on_live": notify_on_live,
            "notify_on_end": notify_on_end,
            "notify_on_category_change": notify_on_category_change,
            "has_schedule_reminder": int(data.get("schedule_reminder_minutes", 0)) > 0,
            "is_demo": demo_mode.is_active(owner_id),
            "live_addon": bool(live_addon),
        },
    )

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
        return _wz()["SCHEDULE_LIVE_ASK"]

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

