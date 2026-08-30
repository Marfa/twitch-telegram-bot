from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

from telegram import ReplyKeyboardMarkup
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter
from telegram.ext import ContextTypes, ConversationHandler, filters

import beta as beta_features
import demo_mode
from db import Database
from i18n import (
    DEFAULT_LOCALE,
    SUPPORTED_LOCALES,
    btn,
    main_menu,
    settings_menu,
    t,
    wizard_menu,
)

logger = logging.getLogger(__name__)

# Soft pacing for mass DM sends — keeps under Telegram flood limits.
_BROADCAST_SEND_PAUSE = 0.05
_PAUSE_NOTIFICATIONS_BETA_ID = "pause-notifications"


def reply_chat_id(update) -> int:
    """Chat to reply in — current conversation, not the user's private DM."""
    if update.effective_chat is not None:
        return int(update.effective_chat.id)
    query = update.callback_query
    if query and query.message:
        return int(query.message.chat_id)
    return int(update.effective_user.id)


def is_private_chat(update) -> bool:
    chat = update.effective_chat
    if chat is not None:
        return chat.type == ChatType.PRIVATE
    query = update.callback_query
    if query and query.message and query.message.chat:
        return query.message.chat.type == ChatType.PRIVATE
    return True


def chat_context_properties(update) -> dict[str, int | str]:
    """PostHog-friendly chat context for exception reports."""
    props: dict[str, int | str] = {}
    chat = update.effective_chat
    if chat is None and update.callback_query and update.callback_query.message:
        chat = update.callback_query.message.chat
    if chat is not None:
        props["chat_id"] = int(chat.id)
        props["chat_type"] = str(chat.type)
    return props


async def reply_setup_private_only(update, lang: str) -> None:
    text = t("setup_private_only", lang)
    if update.callback_query:
        try:
            await update.callback_query.answer(text, show_alert=True)
        except BadRequest:
            pass
        return
    if update.effective_message:
        await update.effective_message.reply_text(text)


# ponytail: in-memory; one hint per (chat, user) for non-admins until restart
_group_setup_notified: set[tuple[int, int]] = set()


def reset_group_setup_notified() -> None:
    _group_setup_notified.clear()


def should_send_group_setup_hint(chat_id: int, user_id: int) -> bool:
    """Admins get the hint every time; other users once per chat."""
    if _is_admin(user_id):
        return True
    key = (chat_id, user_id)
    if key in _group_setup_notified:
        return False
    _group_setup_notified.add(key)
    return True


async def handle_group_setup_rejection(
    update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Block group/channel bot setup. Returns True if the update was consumed."""
    if is_private_chat(update):
        return False
    user_id = update.effective_user.id
    chat_id = reply_chat_id(update)
    if not should_send_group_setup_hint(chat_id, user_id):
        if update.callback_query:
            try:
                await update.callback_query.answer()
            except BadRequest:
                pass
        return True
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    lang = _user_lang(context, user_id)
    import analytics

    analytics.capture(
        user_id,
        "group_setup_private_hint",
        {
            "chat_id": chat_id,
            **chat_context_properties(update),
            "is_admin": _is_admin(user_id),
        },
    )
    await reply_setup_private_only(update, lang)
    return True


def dm_only_conv_entry(handler):
    """ConversationHandler entry: refuse bot setup in group/channel chats."""

    async def wrapped(update, context: ContextTypes.DEFAULT_TYPE):
        if is_private_chat(update):
            return await handler(update, context)
        await handle_group_setup_rejection(update, context)
        return ConversationHandler.END

    return wrapped


def _pause_notifications_enabled(db: Database, user_id: int) -> bool:
    return beta_features.is_enabled(db, user_id, _PAUSE_NOTIFICATIONS_BETA_ID)


def _user_notifications_paused(db: Database, user_id: int) -> bool:
    if not _pause_notifications_enabled(db, user_id):
        return False
    until = db.get_notifications_paused_until(user_id)
    return until > int(datetime.now(timezone.utc).timestamp())


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
    except (BadRequest, Forbidden):
        pass


def _btn_filter(key: str) -> filters.Regex:
    texts = "|".join(re.escape(btn(key, loc)) for loc in SUPPORTED_LOCALES)
    if key == "beta_mode":
        return filters.Regex(rf"^({texts})( \(\d+/\d+\))?$")
    return filters.Regex(f"^({texts})$")


_NON_PRIVATE = ~filters.ChatType.PRIVATE


def group_setup_menu_filter():
    """Reply-keyboard actions that configure the bot — DM only (not stream chat)."""
    return _NON_PRIVATE & (
        filters.UpdateType.MESSAGE
        & (
            _btn_filter("manage")
            | _btn_filter("import_twitch")
            | _btn_filter("list")
            | _btn_filter("edit")
            | _btn_filter("delete")
            | _btn_filter("pause_notifications")
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
            | _btn_filter("language")
            | _btn_filter("sys_notifications")
            | _btn_filter("ignored_words")
            | _btn_filter("whisper_alerts")
            | _btn_filter("advanced_mode")
            | _btn_filter("sync_subs")
            | _btn_filter("beta_mode")
            | _btn_filter("admin")
            | _btn_filter("broadcast")
            | _btn_filter("scheduled_broadcasts")
            | _btn_filter("sent_broadcasts")
            | _btn_filter("stats")
            | _btn_filter("demo")
        )
    )


GROUP_SETUP_CALLBACK_PATTERN = (
    r"^(import_mode:|sync:|edit_f:|edit_set:|watch:|alert_type:|premium_gate:|"
    r"dup:|dest:|strip_name:|lucky:|image_ask:|image_pos:|ignore_keywords:|"
    r"template_typo:|list_type:|delete_|enable_all|toggle:|sub_toggle:|"
    r"sys_updates:|sys_availability:|sys_other:|sys_sync:|advanced_mode:|"
    r"whisper_alerts:|beta_mode:|premium:|alert_history:|lang:(?!cancel)|"
    r"sb_edit:|sb_sched:|import_oauth:)"
)


def _settings_kb(lang: str, db: Database, user_id: int) -> ReplyKeyboardMarkup:
    enrolled, total = beta_features.enrollment_counts(db, user_id)
    return settings_menu(lang, beta_enrolled=enrolled, beta_total=total)


def _is_admin(user_id: int) -> bool:
    from config import ADMIN_USER_IDS

    return user_id in ADMIN_USER_IDS


def _can_use_admin_tools(user_id: int) -> bool:
    return _is_admin(user_id) and not demo_mode.is_active(user_id)


def _user_lang(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str:
    db: Database = context.application.bot_data["db"]
    return db.get_user_locale(user_id) or DEFAULT_LOCALE


def _is_link_preview_disabled(message) -> bool:
    opts = message.link_preview_options
    return bool(opts and opts.is_disabled)


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


async def _send_dm_html(
    bot,
    db: Database,
    uid: int,
    message: str,
    *,
    reply_markup=None,
    disable_web_page_preview: bool = False,
    return_message_id: bool = False,
) -> str | tuple[str, int | None]:
    """Send one DM. Returns 'sent', 'blocked', or 'failed' (optionally with message_id)."""
    if _user_notifications_paused(db, uid):
        return ("sent", None) if return_message_id else "sent"
    kwargs: dict = {}
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup
    if disable_web_page_preview:
        kwargs["disable_web_page_preview"] = True

    async def _deliver() -> object | None:
        try:
            return await bot.send_message(
                uid, message, parse_mode=ParseMode.HTML, **kwargs
            )
        except BadRequest:
            return await bot.send_message(uid, message, **kwargs)

    def _result(status: str, msg: object | None = None) -> str | tuple[str, int | None]:
        if return_message_id:
            mid = getattr(msg, "message_id", None)
            return status, int(mid) if mid is not None else None
        return status

    try:
        msg = await _deliver()
        return _result("sent", msg)
    except RetryAfter as exc:
        await asyncio.sleep(float(exc.retry_after) + 0.5)
        try:
            msg = await _deliver()
            return _result("sent", msg)
        except Forbidden as retry_exc:
            if "blocked" in str(retry_exc).lower():
                db.set_bot_blocked(uid, True)
                return _result("blocked")
            logger.warning("Broadcast to %s failed after RetryAfter: %s", uid, retry_exc)
            return _result("failed")
        except (BadRequest, RetryAfter) as retry_exc:
            logger.warning("Broadcast to %s failed after RetryAfter: %s", uid, retry_exc)
            return _result("failed")
    except Forbidden as exc:
        if "blocked" in str(exc).lower():
            db.set_bot_blocked(uid, True)
            return _result("blocked")
        logger.warning("Broadcast to %s failed: %s", uid, exc)
        return _result("failed")
    except BadRequest as exc:
        logger.warning("Broadcast to %s failed: %s", uid, exc)
        return _result("failed")

