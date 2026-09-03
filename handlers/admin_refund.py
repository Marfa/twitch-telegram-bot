"""Admin: refund Stars payment by telegram_payment_charge_id and revoke Premium."""
from __future__ import annotations

import logging
import re

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from bot_helpers import _can_use_admin_tools, _user_lang, _wizard
from db import Database
from i18n import admin_menu, all_wizard_nav_buttons, is_menu_button, t
import premium as prem

logger = logging.getLogger(__name__)

_CHARGE_RE = re.compile(r"^stx[A-Za-z0-9_-]{20,}$")


def _admin_refund_state() -> int:
    from bot import ADMIN_REFUND_CHARGE

    return ADMIN_REFUND_CHARGE


async def admin_refund_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not _can_use_admin_tools(user_id):
        return ConversationHandler.END
    lang = _user_lang(context, user_id)
    await update.effective_message.reply_text(
        t("admin_refund_prompt", lang),
        reply_markup=_wizard(lang, back=False),
    )
    return _admin_refund_state()


async def admin_refund_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    await update.effective_message.reply_text(
        t("cancelled", lang),
        reply_markup=admin_menu(lang),
    )
    return ConversationHandler.END


async def admin_refund_receive(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    if not _can_use_admin_tools(user_id):
        return ConversationHandler.END
    raw = (update.effective_message.text or "").strip()
    if is_menu_button(raw) or raw in all_wizard_nav_buttons():
        await update.effective_message.reply_text(
            t("cancelled", lang),
            reply_markup=admin_menu(lang),
        )
        return ConversationHandler.END
    charge_id = raw.split()[0] if raw else ""
    if not _CHARGE_RE.match(charge_id):
        await update.effective_message.reply_text(
            t("admin_refund_bad_id", lang),
            reply_markup=_wizard(lang, back=False),
        )
        return _admin_refund_state()

    db: Database = context.application.bot_data["db"]
    result = await prem.admin_refund_charge(context.bot, db, charge_id)
    if not result.ok and result.error == "not_found":
        await update.effective_message.reply_text(
            t("admin_refund_not_found", lang),
            reply_markup=admin_menu(lang),
        )
        return ConversationHandler.END

    kinds = ", ".join(result.revoked) if result.revoked else "—"
    if result.refund_failed:
        await update.effective_message.reply_text(
            t(
                "admin_refund_partial",
                lang,
                user_id=result.user_id,
                kinds=kinds,
                detail=result.detail or "—",
            ),
            reply_markup=admin_menu(lang),
        )
        return ConversationHandler.END

    await update.effective_message.reply_text(
        t(
            "admin_refund_done",
            lang,
            user_id=result.user_id,
            kinds=kinds,
            already=(
                t("admin_refund_already", lang) if result.already_refunded else ""
            ),
        ),
        reply_markup=admin_menu(lang),
    )
    return ConversationHandler.END
