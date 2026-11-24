from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import ContextTypes

from bot_helpers import _can_use_admin_tools, _is_admin, _user_lang, reply_chat_id
from db import Database
from i18n import (
    DEFAULT_LOCALE,
    admin_menu_inline,
    partner_menu_inline,
    t,
    withdrawal_actions_keyboard,
)

logger = logging.getLogger(__name__)


def _partner_enabled() -> bool:
    from config import ENABLE_PARTNER

    return ENABLE_PARTNER


async def open_partner_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from config import REFERRAL_COMMISSION_PERCENT, REFERRAL_WITHDRAW_MIN_STARS

    if not _partner_enabled():
        return
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    await context.bot.send_message(
        reply_chat_id(update),
        t(
            "partner_intro",
            lang,
            percent=REFERRAL_COMMISSION_PERCENT,
            min_stars=REFERRAL_WITHDRAW_MIN_STARS,
        ),
        reply_markup=partner_menu_inline(lang),
    )


async def partner_show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _partner_enabled():
        return
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    stats = db.get_referral_stats(user_id)
    await context.bot.send_message(
        reply_chat_id(update),
        t(
            "partner_stats",
            lang,
            invited=stats.invited,
            payments=stats.payments,
            available=stats.available_stars,
        ),
        reply_markup=partner_menu_inline(lang),
    )


async def partner_show_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _partner_enabled():
        return
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    username = context.bot.username or ""
    if not username:
        me = await context.bot.get_me()
        username = me.username or ""
    link = f"https://t.me/{username}?start=ref_{user_id}" if username else f"ref_{user_id}"
    await context.bot.send_message(
        reply_chat_id(update),
        t("partner_link", lang, link=link),
        reply_markup=partner_menu_inline(lang),
    )


async def partner_request_withdraw(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    from config import ADMIN_USER_IDS, REFERRAL_WITHDRAW_MIN_STARS

    if not _partner_enabled():
        return
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    stats = db.get_referral_stats(user_id)
    available = stats.available_stars
    if available < REFERRAL_WITHDRAW_MIN_STARS:
        await context.bot.send_message(
            reply_chat_id(update),
            t(
                "partner_withdraw_min",
                lang,
                min_stars=REFERRAL_WITHDRAW_MIN_STARS,
                available=available,
            ),
            reply_markup=partner_menu_inline(lang),
        )
        return
    request_id = db.request_referral_withdrawal(user_id, available)
    if request_id is None:
        await context.bot.send_message(
            reply_chat_id(update),
            t(
                "partner_withdraw_min",
                lang,
                min_stars=REFERRAL_WITHDRAW_MIN_STARS,
                available=available,
            ),
            reply_markup=partner_menu_inline(lang),
        )
        return
    await context.bot.send_message(
        reply_chat_id(update),
        t("partner_withdraw_ok", lang, id=request_id, amount=available),
        reply_markup=partner_menu_inline(lang),
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
    if not _partner_enabled():
        return
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    items = db.list_referral_withdrawals(user_id, limit=20)
    if not items:
        await context.bot.send_message(
            reply_chat_id(update),
            t("partner_withdrawals_empty", lang),
            reply_markup=partner_menu_inline(lang),
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
    await context.bot.send_message(
        reply_chat_id(update),
        "\n".join(lines),
        reply_markup=partner_menu_inline(lang),
    )


async def on_menu_partner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from bot_helpers import _settings_inline
    from handlers.settings import _dismiss_inline_panel, open_settings_menu

    query = update.callback_query
    if not query:
        return
    await query.answer()
    if not _partner_enabled():
        return
    action = (query.data or "").split(":", 1)[-1]
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    if action == "back":
        try:
            await query.edit_message_text(
                t("menu_settings", lang),
                reply_markup=_settings_inline(lang, db, user_id),
            )
        except BadRequest:
            await _dismiss_inline_panel(query)
            await open_settings_menu(update, context)
        return
    try:
        await query.edit_message_text("✓")
    except BadRequest:
        pass
    if action == "stats":
        await partner_show_stats(update, context)
        return
    if action == "link":
        await partner_show_link(update, context)
        return
    if action == "withdraw":
        await partner_request_withdraw(update, context)
        return
    if action == "withdrawals":
        await partner_show_withdrawals(update, context)


async def admin_show_withdrawals(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not _partner_enabled():
        return
    user_id = update.effective_user.id
    if not _can_use_admin_tools(user_id):
        return
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    items = db.list_pending_referral_withdrawals()
    chat_id = reply_chat_id(update)
    if not items:
        await context.bot.send_message(
            chat_id,
            t("admin_withdrawals_empty", lang),
            reply_markup=admin_menu_inline(lang),
        )
        return
    await context.bot.send_message(
        chat_id,
        t("admin_withdrawals_title", lang),
        reply_markup=admin_menu_inline(lang),
    )
    for item in items:
        await context.bot.send_message(
            chat_id,
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
    if not _partner_enabled():
        return
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

