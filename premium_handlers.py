"""Premium UI, Stars payments, Twitch sub verification."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes

import premium as prem
import demo_mode
import analytics
from config import twitch_oauth_redirect_uri
from db import Database
from health import create_oauth_state
from i18n import (
    DEFAULT_LOCALE,
    btn,
    main_menu,
    premium_actions_keyboard,
    premium_features_keyboard,
    premium_owned_keyboard,
    t,
)
from twitch import SUBSCRIPTIONS_SCOPE, TwitchClient

logger = logging.getLogger(__name__)


def _fmt_until(unix: int) -> str:
    return datetime.fromtimestamp(unix, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _has_premium_without_autorenew(
    st: prem.PremiumStatus, *, free_chat: bool = False
) -> bool:
    """True if any active entitlement is not on Stars auto-renew."""
    if st.permanent or st.trial_active or st.twitch_active or free_chat:
        return True
    if st.stars_active and st.stars_canceled:
        return True
    now = int(time.time())
    for fid, until in st.features.items():
        if int(until) > now and st.is_feature_canceled(fid):
            return True
    return False


def _premium_feat_pick_text(lang: str, user_id: int) -> str:
    """Premium à la carte: feature select prompt with short explanations."""
    price = prem.stars_feature_price(user_id)
    lines: list[str] = []
    free_limit = prem.free_active_limit()
    for fid in prem.purchasable_feature_ids():
        name = t(prem.feature_label_key(fid), lang, free_limit=free_limit)
        desc = t(prem.feature_desc_key(fid), lang, free_limit=free_limit)
        lines.append(f"• <b>{name}</b>\n{desc}")
    return (
        t("premium_feat_pick", lang, price=price)
        + "\n\n"
        + t("premium_feat_pick_available", lang)
        + "\n\n"
        + "\n\n".join(lines)
    )


def _status_text(
    db: Database,
    user_id: int,
    lang: str,
    *,
    free_chat: bool = False,
    force_free: bool = False,
) -> str:
    if force_free:
        return t("premium_status_none", lang)
    prem.ensure_trial_expired(db, user_id)
    st = prem.get_status(db, user_id)
    channel = prem.twitch_channel_login()
    if st.permanent:
        body = t("premium_status_permanent", lang)
    else:
        parts: list[str] = []
        if st.trial_active:
            parts.append(t("premium_status_trial", lang, until=_fmt_until(st.trial_until)))
        if st.stars_active:
            until = _fmt_until(st.stars_until)
            key = (
                "premium_status_stars_canceled"
                if st.stars_canceled
                else "premium_status_stars"
            )
            parts.append(t(key, lang, until=until))
        if st.twitch_active:
            parts.append(t("premium_status_twitch", lang, channel=channel))
        if parts:
            body = "\n".join(parts)
        else:
            active_feats = [
                (fid, until)
                for fid, until in st.features.items()
                if until > int(time.time())
            ]
            if active_feats:
                lines = []
                for fid, until in sorted(active_feats, key=lambda x: x[0]):
                    name = t(
                        prem.feature_label_key(fid),
                        lang,
                        free_limit=prem.free_active_limit(),
                    )
                    key = (
                        "premium_feat_line_canceled"
                        if st.is_feature_canceled(fid)
                        else "premium_feat_line"
                    )
                    lines.append(
                        t(key, lang, name=name, until=_fmt_until(until))
                    )
                body = t("premium_status_features", lang, features="\n".join(lines))
            elif free_chat:
                body = t("premium_status_permanent", lang)
            else:
                body = t("premium_status_none", lang)
    if _has_premium_without_autorenew(st, free_chat=free_chat):
        return body + "\n" + t("premium_buy_after_current", lang)
    return body


def premium_benefits_text(lang: str) -> str:
    """Shared Premium benefit list (full plan screen + à la carte feature names)."""
    free_limit = prem.free_active_limit()
    lines = [
        f"• {t(prem.feature_label_key(fid), lang, free_limit=free_limit)}"
        for fid in prem.purchasable_feature_ids()
    ]
    lines.append(f"• {t('premium_channel_benefit', lang)}")
    return "\n".join(lines)


def premium_screen_text(
    db: Database,
    user_id: int,
    lang: str,
    *,
    free_chat: bool = False,
    force_free: bool = False,
) -> str:
    return t(
        "premium_title",
        lang,
        benefits=premium_benefits_text(lang),
        channel=prem.twitch_channel_login(),
        status=_status_text(
            db, user_id, lang, free_chat=free_chat, force_free=force_free
        ),
    )


def _premium_markup(
    db: Database, user_id: int, lang: str, *, free_chat: bool, force_free: bool
) -> InlineKeyboardMarkup | None:
    """Action buttons for free / partial UX; cancel when Stars auto-renew is on."""
    if force_free:
        return premium_actions_keyboard(
            lang,
            show_trial=True,
            show_plans=True,
            show_features=True,
            show_owned=False,
            user_id=user_id,
        )
    st = prem.get_status(db, user_id)
    # Custom 1⭐ (etc.) testers still need pay buttons even with free-chat Premium.
    if (st.permanent or free_chat) and not prem.has_custom_stars_price(user_id):
        return None
    if (
        st.twitch_active
        and not st.stars_active
        and not st.trial_active
        and not st.has_active_features
        and not prem.has_custom_stars_price(user_id)
    ):
        return None
    full = st.has_full_plan
    show_owned = bool(
        st.has_active_features
        or (st.stars_active and st.stars_charge_id)
    )
    # Full plan → no purchases. À la carte → more features only (not month/year/life).
    show_plans = not full and not st.has_active_features
    show_features = not full
    show_trial = (
        not st.trial_used and not full and not st.has_active_features and not free_chat
    )
    return premium_actions_keyboard(
        lang,
        show_trial=show_trial,
        show_plans=show_plans,
        show_features=show_features,
        show_owned=show_owned,
        user_id=user_id,
    )


def _owned_features(st: prem.PremiumStatus) -> list[str]:
    now = int(time.time())
    return sorted(fid for fid, until in st.features.items() if int(until) > now)


async def _show_owned_subscriptions(
    query, db: Database, user_id: int, lang: str
) -> None:
    st = prem.get_status(db, user_id)
    lines: list[str] = []
    stars_cancelable = bool(
        st.stars_active and st.stars_charge_id and not st.stars_canceled
    )
    if st.stars_active:
        key = (
            "premium_owned_stars_canceled"
            if st.stars_canceled
            else "premium_owned_stars"
        )
        lines.append(t(key, lang, until=_fmt_until(st.stars_until)))
    feat_ids = _owned_features(st)
    cancelable_feats = [fid for fid in feat_ids if st.feature_cancelable(fid)]
    for fid in feat_ids:
        name = t(
            prem.feature_label_key(fid),
            lang,
            free_limit=prem.free_active_limit(),
        )
        key = (
            "premium_feat_line_canceled"
            if st.is_feature_canceled(fid)
            else "premium_feat_line"
        )
        lines.append(
            t(
                key,
                lang,
                name=name,
                until=_fmt_until(st.feature_until(fid)),
            )
        )
    text = (
        t("premium_owned_title", lang, items="\n".join(lines))
        if lines
        else t("premium_owned_empty", lang)
    )
    await query.edit_message_text(
        text,
        reply_markup=premium_owned_keyboard(
            lang,
            stars_cancelable=stars_cancelable,
            feature_ids=cancelable_feats,
        ),
    )


async def send_premium_screen(
    bot,
    user_id: int,
    lang: str,
    db: Database,
    *,
    edit_message=None,
    update=None,
) -> None:
    # Demo: show free-plan screen even if admin has permanent / free-chat Premium.
    force_free = demo_mode.is_active(user_id)
    prem.ensure_trial_expired(db, user_id)
    st = prem.get_status(db, user_id)
    if force_free:
        free_chat = False
    elif st.permanent or st.stars_active or st.twitch_active or st.trial_active:
        # Full Premium already in DB — skip getChatMember.
        free_chat = False
    else:
        free_chat = await prem.is_free_chat_member(bot, user_id)
    text = premium_screen_text(
        db, user_id, lang, free_chat=free_chat, force_free=force_free
    )
    markup = _premium_markup(
        db, user_id, lang, free_chat=free_chat, force_free=force_free
    )
    if edit_message is not None:
        await edit_message.edit_text(
            text,
            reply_markup=markup,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return
    from bot_helpers import reply_chat_id

    chat_id = reply_chat_id(update) if update is not None else user_id
    await bot.send_message(
        chat_id,
        text,
        reply_markup=markup,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def open_premium_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from bot import _user_lang
    from config import show_premium_ui

    if not show_premium_ui():
        return
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    await send_premium_screen(context.bot, user_id, lang, db, update=update)


async def _send_invoice_link(
    query,
    *,
    title: str,
    description: str,
    payload: str,
    stars: int,
    lang: str,
    subscription_period: int | None = None,
) -> None:
    prices = [LabeledPrice(title, stars)]
    kwargs: dict = {
        "title": title,
        "description": description,
        "payload": payload,
        "provider_token": "",
        "currency": "XTR",
        "prices": prices,
    }
    if subscription_period is not None:
        kwargs["subscription_period"] = subscription_period
    try:
        link = await query.get_bot().create_invoice_link(**kwargs)
    except Exception:
        logger.exception("create_invoice_link failed payload=%s", payload)
        await query.edit_message_text(t("premium_pay_failed", lang))
        return
    await query.edit_message_text(
        t("premium_pay_link", lang),
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(btn("premium_pay", lang), url=link)]]
        ),
    )


def _demo_force_free(user_id: int) -> bool:
    """Demo shows free Premium UI; purchase checks must match that screen."""
    return demo_mode.is_active(user_id)


def _blocks_plan_purchase(db: Database, user_id: int) -> bool:
    if _demo_force_free(user_id):
        return False
    st = prem.get_status(db, user_id)
    return st.has_full_plan or st.has_active_features


def _blocks_feature_purchase(db: Database, user_id: int) -> bool:
    if _demo_force_free(user_id):
        return False
    return prem.get_status(db, user_id).has_full_plan


async def on_premium_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from bot import _user_lang
    from config import show_premium_ui

    query = update.callback_query
    if not show_premium_ui():
        await query.answer()
        return
    data = query.data or ""
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]

    # Feature multi-select toggles
    if data.startswith("premium:feat_toggle:"):
        if _blocks_feature_purchase(db, user_id):
            await query.answer(t("premium_plans_blocked", lang), show_alert=True)
            return
        st = prem.get_status(db, user_id)
        fid = data.split(":", 2)[2]
        owned = set(_owned_features(st))
        if fid in owned and not _demo_force_free(user_id):
            await query.answer(t("premium_feat_owned", lang), show_alert=True)
            return
        await query.answer()
        selected: set[str] = set(context.user_data.get("premium_feat_sel") or [])
        if fid in selected:
            selected.discard(fid)
        else:
            if fid in prem.purchasable_feature_ids():
                selected.add(fid)
        context.user_data["premium_feat_sel"] = sorted(selected)
        await query.edit_message_text(
            _premium_feat_pick_text(lang, user_id),
            reply_markup=premium_features_keyboard(
                lang, selected, user_id=user_id, owned=owned if not _demo_force_free(user_id) else set()
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "premium:feat_back":
        await query.answer()
        context.user_data.pop("premium_feat_sel", None)
        await send_premium_screen(context.bot, user_id, lang, db, edit_message=query.message)
        return

    if data == "premium:owned":
        await query.answer()
        await _show_owned_subscriptions(query, db, user_id, lang)
        return

    if data.startswith("premium:cancel_feat:"):
        fid = data.split(":", 2)[2]
        st = prem.get_status(db, user_id)
        if not st.feature_active(fid):
            await query.answer(t("premium_cancel_none", lang), show_alert=True)
            return
        if st.is_feature_canceled(fid):
            await query.answer(
                t(
                    "premium_cancel_done",
                    lang,
                    until=_fmt_until(st.feature_until(fid)),
                ),
                show_alert=True,
            )
            return
        charge_id = st.feature_charge_id(fid)
        if charge_id:
            try:
                await context.bot.edit_user_star_subscription(
                    user_id=user_id,
                    telegram_payment_charge_id=charge_id,
                    is_canceled=True,
                )
            except Exception:
                # Charge may be invalid/already canceled; still mark local cancel.
                logger.exception(
                    "edit_user_star_subscription feature failed user=%s feat=%s",
                    user_id,
                    fid,
                )
        db.set_premium_feature_canceled(user_id, fid)
        until = _fmt_until(st.feature_until(fid))
        await query.answer()
        await query.edit_message_text(
            t("premium_cancel_done", lang, until=until)
        )
        return

    if data == "premium:feat_pay":
        if _blocks_feature_purchase(db, user_id):
            await query.answer(t("premium_plans_blocked", lang), show_alert=True)
            return
        st = prem.get_status(db, user_id)
        owned = set() if _demo_force_free(user_id) else set(_owned_features(st))
        selected = [
            f
            for f in (context.user_data.get("premium_feat_sel") or [])
            if f in prem.purchasable_feature_ids() and f not in owned
        ]
        if not selected:
            await query.answer(t("premium_pay_failed", lang), show_alert=True)
            return
        await query.answer()
        total = prem.stars_feature_price(user_id) * len(selected)
        await _send_invoice_link(
            query,
            title=t("premium_pay_feat_title", lang),
            description=t("premium_pay_feat_description", lang, stars=total),
            payload=prem.invoice_payload(user_id, "feat", selected),
            stars=total,
            lang=lang,
            subscription_period=prem.stars_period(),
        )
        return

    if data == "premium:features":
        if _blocks_feature_purchase(db, user_id):
            await query.answer(t("premium_plans_blocked", lang), show_alert=True)
            return
        await query.answer()
        st = prem.get_status(db, user_id)
        context.user_data["premium_feat_sel"] = []
        owned = set() if _demo_force_free(user_id) else set(_owned_features(st))
        await query.edit_message_text(
            _premium_feat_pick_text(lang, user_id),
            reply_markup=premium_features_keyboard(
                lang, set(), user_id=user_id, owned=owned
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    action = data.split(":", 1)[1] if ":" in data else ""

    if action in ("month", "year", "life", "pay", "trial", "trial_confirm"):
        if _blocks_plan_purchase(db, user_id):
            await query.answer(t("premium_plans_blocked", lang), show_alert=True)
            return

    if action == "trial_confirm":
        ok, reason = prem.start_trial(db, user_id)
        st = prem.get_status(db, user_id)
        if ok:
            await query.answer()
            await query.edit_message_text(
                t(
                    "premium_trial_started",
                    lang,
                    until=_fmt_until(st.trial_until),
                )
            )
            return
        if reason == "active":
            await query.answer(
                t("premium_trial_active", lang, until=_fmt_until(st.trial_until)),
                show_alert=True,
            )
            return
        await query.answer(t("premium_trial_used", lang), show_alert=True)
        return

    await query.answer()

    if action == "trial":
        await query.edit_message_text(
            t("premium_trial_confirm", lang, days=prem.trial_days()),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            btn("premium_trial_confirm", lang),
                            callback_data="premium:trial_confirm",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            btn("premium_feat_back", lang),
                            callback_data="premium:feat_back",
                        )
                    ],
                ]
            ),
        )
        return

    if action in ("pay", "month"):
        month_stars = prem.stars_price(user_id)
        await _send_invoice_link(
            query,
            title=t("premium_pay_title", lang),
            description=t(
                "premium_pay_description", lang, stars=month_stars
            ),
            payload=prem.invoice_payload(user_id, "month"),
            stars=month_stars,
            lang=lang,
            subscription_period=prem.stars_period(),
        )
        return

    if action == "year":
        year_stars = prem.stars_year_price(user_id)
        await _send_invoice_link(
            query,
            title=t("premium_pay_year_title", lang),
            description=t(
                "premium_pay_year_description", lang, stars=year_stars
            ),
            payload=prem.invoice_payload(user_id, "year"),
            stars=year_stars,
            lang=lang,
            subscription_period=None,
        )
        return

    if action == "life":
        life_stars = prem.stars_lifetime_price(user_id)
        await _send_invoice_link(
            query,
            title=t("premium_pay_life_title", lang),
            description=t(
                "premium_pay_life_description",
                lang,
                stars=life_stars,
            ),
            payload=prem.invoice_payload(user_id, "life"),
            stars=life_stars,
            lang=lang,
            subscription_period=None,
        )
        return

    if action == "cancel":
        st = prem.get_status(db, user_id)
        if not st.stars_charge_id or not st.stars_active:
            await query.edit_message_text(t("premium_cancel_none", lang))
            return
        try:
            await context.bot.edit_user_star_subscription(
                user_id=user_id,
                telegram_payment_charge_id=st.stars_charge_id,
                is_canceled=True,
            )
        except Exception:
            # Invalid/test charge_id: still stop offering cancel in-bot.
            logger.exception("edit_user_star_subscription failed for %s", user_id)
        db.set_premium_stars_canceled(user_id, True)
        await query.edit_message_text(
            t("premium_cancel_done", lang, until=_fmt_until(st.stars_until))
        )
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
        return

    if action == "channel":
        stars = prem.stars_channel_price(user_id)
        await query.edit_message_text(
            t("premium_channel_intro", lang, stars=stars),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            btn("premium_channel_confirm", lang),
                            callback_data="premium:channel_confirm",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            btn("premium_feat_back", lang),
                            callback_data="premium:feat_back",
                        )
                    ],
                ]
            ),
        )
        return

    if action == "channel_confirm":
        redirect = twitch_oauth_redirect_uri()
        if not redirect:
            await query.edit_message_text(t("import_failed", lang))
            return
        state = create_oauth_state(user_id, lang, purpose="premium_channel")
        url = twitch.build_authorize_url(
            redirect_uri=redirect,
            state=state,
            scopes="",
        )
        await query.edit_message_text(
            t("premium_channel_oauth", lang),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            t("premium_channel_oauth_button", lang), url=url
                        )
                    ]
                ]
            ),
        )
        return

    if action == "channel_pay":
        pending = context.application.bot_data.get("pending_premium_channel") or {}
        info = pending.get(user_id)
        if not info:
            await query.edit_message_text(t("premium_channel_failed", lang))
            return
        login = str(info.get("twitch_login") or "")
        tid = str(info.get("twitch_user_id") or "")
        if not login or not tid:
            await query.edit_message_text(t("premium_channel_failed", lang))
            return
        if db.get_premium_channel(tid) is not None or prem.is_promo_channel(login, db):
            await query.edit_message_text(t("premium_channel_already", lang))
            return
        stars = prem.stars_channel_price(user_id)
        await _send_invoice_link(
            query,
            title=t("premium_channel_pay_title", lang),
            description=t(
                "premium_channel_pay_description", lang, channel=login
            ),
            payload=prem.invoice_payload(
                user_id,
                "channel",
                twitch_user_id=tid,
                twitch_login=login,
            ),
            stars=stars,
            lang=lang,
            subscription_period=None,
        )
        return


async def complete_premium_channel_oauth(
    application: Application,
    owner_id: int,
    error: str | None,
    token_info: dict[str, str] | None,
) -> None:
    db: Database = application.bot_data["db"]
    lang = db.get_user_locale(owner_id) or DEFAULT_LOCALE
    menu = main_menu(
        lang,
        is_admin=False,
        demo_active=demo_mode.is_active(owner_id),
    )
    if error or not token_info:
        await application.bot.send_message(
            owner_id, t("premium_channel_failed", lang), reply_markup=menu
        )
        return
    login = str(token_info.get("twitch_login") or "").strip().lower()
    tid = str(token_info.get("twitch_user_id") or "").strip()
    display = str(
        token_info.get("twitch_display_name") or login
    ).strip() or login
    if not login or not tid:
        await application.bot.send_message(
            owner_id, t("premium_channel_failed", lang), reply_markup=menu
        )
        return
    if db.get_premium_channel(tid) is not None or (
        login == prem.twitch_channel_login()
    ):
        await application.bot.send_message(
            owner_id, t("premium_channel_already", lang), reply_markup=menu
        )
        return
    pending = application.bot_data.setdefault("pending_premium_channel", {})
    pending[owner_id] = {
        "twitch_user_id": tid,
        "twitch_login": login,
        "display_name": display,
    }
    stars = prem.stars_channel_price(owner_id)
    channel_label = display if display.lower() != login else login
    await application.bot.send_message(
        owner_id,
        t("premium_channel_confirm_pay", lang, channel=channel_label),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        t("btn_premium_channel_pay", lang, stars=stars),
                        callback_data="premium:channel_pay",
                    )
                ]
            ]
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
    from config import show_premium_ui

    query = update.pre_checkout_query
    if not show_premium_ui():
        await query.answer(ok=False, error_message="Premium disabled")
        return
    if prem.parse_invoice_payload(query.invoice_payload) is None:
        await query.answer(ok=False, error_message="Unknown invoice")
        return
    await query.answer(ok=True)


async def successful_premium_payment(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    from bot import _user_lang
    from config import show_premium_ui

    if not show_premium_ui():
        return
    msg = update.message
    if not msg or not msg.successful_payment:
        return
    payment = msg.successful_payment
    parsed = prem.parse_invoice_payload(payment.invoice_payload)
    if parsed is None:
        return
    lang = _user_lang(context, update.effective_user.id)
    db: Database = context.application.bot_data["db"]
    charge_id = payment.telegram_payment_charge_id
    stars_paid = int(payment.total_amount or 0)
    now = int(time.time())
    # PTB 21.8+: subscription_expiration_date is datetime, not unix int
    exp = payment.subscription_expiration_date
    until_sub = int(exp.timestamp()) if exp is not None else 0

    if parsed.kind in ("month", "legacy"):
        until = until_sub if until_sub > 0 else now + prem.stars_period()
        prem.apply_stars_payment(
            db,
            parsed.user_id,
            charge_id=charge_id,
            until_unix=until,
            stars_paid=stars_paid or prem.stars_price(parsed.user_id),
        )
    elif parsed.kind == "year":
        until = now + prem.year_seconds()
        prem.apply_stars_payment(
            db,
            parsed.user_id,
            charge_id=charge_id,
            until_unix=until,
            stars_paid=stars_paid or prem.stars_year_price(parsed.user_id),
        )
        # One-shot year: no Telegram auto-renew to cancel.
        db.set_premium_stars_canceled(parsed.user_id, True)
    elif parsed.kind == "life":
        prem.apply_lifetime_payment(
            db,
            parsed.user_id,
            charge_id=charge_id,
            stars_paid=stars_paid or prem.stars_lifetime_price(parsed.user_id),
        )
    elif parsed.kind == "feat":
        until = until_sub if until_sub > 0 else now + prem.stars_period()
        prem.apply_features_payment(
            db,
            parsed.user_id,
            feature_ids=parsed.features,
            charge_id=charge_id,
            until_unix=until,
            stars_paid=stars_paid
            or prem.stars_feature_price(parsed.user_id) * max(1, len(parsed.features)),
        )
    elif parsed.kind == "channel":
        pending = context.application.bot_data.get("pending_premium_channel") or {}
        info = pending.pop(parsed.user_id, None) or {}
        display = str(info.get("display_name") or parsed.twitch_login)
        prem.apply_premium_channel_payment(
            db,
            parsed.user_id,
            twitch_user_id=parsed.twitch_user_id,
            twitch_login=parsed.twitch_login,
            display_name=display,
            charge_id=charge_id,
            stars_paid=stars_paid or prem.stars_channel_price(parsed.user_id),
        )
        analytics.capture(
            parsed.user_id,
            "premium_channel_purchased",
            {
                "stars": stars_paid,
                "twitch_login": parsed.twitch_login,
                "twitch_user_id": parsed.twitch_user_id,
            },
        )
        await msg.reply_text(
            t("premium_channel_pay_done", lang, channel=display or parsed.twitch_login)
        )
        return
    analytics.capture(
        parsed.user_id,
        "premium_purchased",
        {
            "kind": parsed.kind,
            "stars": stars_paid,
            "features": list(parsed.features) if parsed.kind == "feat" else [],
        },
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
