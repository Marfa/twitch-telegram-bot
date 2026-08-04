"""Premium UI, Stars payments, Twitch sub verification."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update
from telegram.ext import Application, ContextTypes

import premium as prem
import demo_mode
from config import twitch_oauth_redirect_uri
from db import Database
from health import create_oauth_state
from i18n import (
    DEFAULT_LOCALE,
    btn,
    main_menu,
    t,
)
from twitch import SUBSCRIPTIONS_SCOPE, TwitchClient

logger = logging.getLogger(__name__)


def _status_text(
    db: Database, user_id: int, lang: str, *, free_chat: bool = False
) -> str:
    st = prem.get_status(db, user_id)
    channel = prem.twitch_channel_login()
    if st.permanent:
        return t("premium_status_permanent", lang)
    parts: list[str] = []
    if st.stars_active:
        until = datetime.fromtimestamp(st.stars_until, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
        key = (
            "premium_status_stars_canceled"
            if st.stars_canceled
            else "premium_status_stars"
        )
        parts.append(t(key, lang, until=until))
    if st.twitch_active:
        parts.append(t("premium_status_twitch", lang, channel=channel))
    if parts:
        return "\n".join(parts)
    if free_chat:
        return t("premium_status_permanent", lang)
    return t("premium_status_none", lang)


def premium_screen_text(
    db: Database, user_id: int, lang: str, *, free_chat: bool = False
) -> str:
    return t(
        "premium_title",
        lang,
        free_limit=prem.free_active_limit(),
        stars=prem.stars_price(),
        channel=prem.twitch_channel_login(),
        status=_status_text(db, user_id, lang, free_chat=free_chat),
    )


async def send_premium_screen(
    bot, user_id: int, lang: str, db: Database, *, edit_message=None
) -> None:
    free_chat = await prem.is_free_chat_member(bot, user_id)
    text = premium_screen_text(db, user_id, lang, free_chat=free_chat)
    if edit_message is not None:
        await edit_message.edit_text(
            text, reply_markup=None, disable_web_page_preview=True
        )
        return
    await bot.send_message(
        user_id,
        text,
        disable_web_page_preview=True,
    )


async def open_premium_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from bot import _user_lang

    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    await send_premium_screen(context.bot, user_id, lang, db)


async def on_premium_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from bot import _user_lang

    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    action = query.data.split(":", 1)[1]
    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]

    if action == "pay":
        prices = [LabeledPrice(t("premium_pay_title", lang), prem.stars_price())]
        try:
            link = await context.bot.create_invoice_link(
                title=t("premium_pay_title", lang),
                description=t(
                    "premium_pay_description", lang, stars=prem.stars_price()
                ),
                payload=prem.invoice_payload(user_id),
                provider_token="",
                currency="XTR",
                prices=prices,
                subscription_period=prem.stars_period(),
            )
        except Exception:
            logger.exception("create_invoice_link failed for %s", user_id)
            await query.edit_message_text(t("import_failed", lang))
            return
        await query.edit_message_text(
            t("premium_pay_link", lang),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(btn("premium_pay", lang), url=link)]]
            ),
        )
        return

    if action == "cancel":
        st = prem.get_status(db, user_id)
        if not st.stars_charge_id or not st.stars_active:
            await query.answer(t("premium_cancel_none", lang), show_alert=True)
            return
        try:
            await context.bot.edit_user_star_subscription(
                user_id=user_id,
                telegram_payment_charge_id=st.stars_charge_id,
                is_canceled=True,
            )
        except Exception:
            logger.exception("edit_user_star_subscription failed for %s", user_id)
            await query.answer(t("import_failed", lang), show_alert=True)
            return
        db.set_premium_stars_canceled(user_id, True)
        await query.edit_message_text(t("premium_cancel_done", lang))
        return

    if action == "marfapr":
        redirect = twitch_oauth_redirect_uri()
        if not redirect:
            await query.edit_message_text(t("import_failed", lang))
            return
        state = create_oauth_state(user_id, lang, purpose="premium")
        url = twitch.build_authorize_url(
            redirect_uri=redirect,
            state=state,
            scopes=SUBSCRIPTIONS_SCOPE,
        )
        channel = prem.twitch_channel_login()
        await query.edit_message_text(
            t("premium_marfapr_oauth", lang, channel=channel),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(btn("premium_marfapr", lang), url=url)]]
            ),
        )


async def complete_premium_oauth(
    application: Application,
    owner_id: int,
    error: str | None,
    token_info: dict[str, str] | None,
) -> None:
    db: Database = application.bot_data["db"]
    twitch: TwitchClient = application.bot_data["twitch"]
    lang = db.get_user_locale(owner_id) or DEFAULT_LOCALE
    channel = prem.twitch_channel_login()
    menu = main_menu(
        lang,
        is_admin=False,
        demo_active=demo_mode.is_active(owner_id),
    )
    if error:
        await application.bot.send_message(
            owner_id, t("import_failed", lang), reply_markup=menu
        )
        return
    info = token_info or {}
    if info.get("twitch_sub_active") != "1":
        await application.bot.send_message(
            owner_id,
            t("premium_marfapr_need_sub", lang, channel=channel),
            reply_markup=menu,
            disable_web_page_preview=True,
        )
        return
    db.set_premium_twitch(
        owner_id,
        active=True,
        twitch_user_id=info.get("twitch_user_id") or "",
        refresh_token=info.get("refresh_token") or None,
    )
    user = await prem.resolve_marfapr_user(twitch)
    if not user:
        await application.bot.send_message(
            owner_id,
            t("premium_marfapr_ok", lang, channel=channel),
            reply_markup=menu,
        )
        return
    existing = next(
        (
            s
            for s in db.get_subscriptions_by_owner(owner_id)
            if s.twitch_user_id == str(user["id"])
            and s.is_demo is demo_mode.is_active(owner_id)
        ),
        None,
    )
    if existing:
        if not existing.enabled:
            db.toggle_subscription(existing.id, owner_id)
        await application.bot.send_message(
            owner_id,
            t("premium_marfapr_ok_exists", lang, channel=channel),
            reply_markup=menu,
        )
        return
    db.add_subscription(
        owner_id,
        user["login"],
        str(user["id"]),
        t("import_default_template", lang),
        "dm",
        owner_id,
        None,
        disable_link_preview=True,
        enabled=True,
        notify_on_live=True,
        notify_on_end=False,
        is_demo=demo_mode.is_active(owner_id),
    )
    await application.bot.send_message(
        owner_id,
        t("premium_marfapr_ok", lang, channel=channel),
        reply_markup=menu,
    )


async def precheckout_premium(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query
    if prem.parse_invoice_payload(query.invoice_payload) is None:
        await query.answer(ok=False, error_message="Unknown invoice")
        return
    await query.answer(ok=True)


async def successful_premium_payment(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    from bot import _user_lang

    msg = update.message
    if not msg or not msg.successful_payment:
        return
    payment = msg.successful_payment
    user_id = prem.parse_invoice_payload(payment.invoice_payload)
    if user_id is None:
        return
    lang = _user_lang(context, update.effective_user.id)
    until = int(getattr(payment, "subscription_expiration_date", None) or 0)
    if until <= 0:
        until = int(time.time()) + prem.stars_period()
    db: Database = context.application.bot_data["db"]
    prem.apply_stars_payment(
        db,
        user_id,
        charge_id=payment.telegram_payment_charge_id,
        until_unix=until,
        stars_paid=int(payment.total_amount or prem.stars_price()),
    )
    await msg.reply_text(t("premium_pay_done", lang))


async def refresh_premium_twitch_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    broadcaster = await prem.resolve_marfapr_user(twitch)
    b_id = str(broadcaster["id"]) if broadcaster else None
    for uid in db.list_premium_twitch_user_ids():
        await asyncio.to_thread(
            prem.refresh_twitch_premium, db, twitch, uid, broadcaster_id=b_id
        )
