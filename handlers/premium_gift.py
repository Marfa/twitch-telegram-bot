"""Premium gift: post-pay customize wizard + deep-link redeem."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import analytics
import beta as beta_features
import premium as prem
from bot_helpers import _user_lang, _wizard, reply_chat_id
from db import Database
from i18n import all_wizard_nav_buttons, btn, is_menu_button, main_menu, t

logger = logging.getLogger(__name__)

BETA_FEATURE_ID = "gift-premium"
_GIFT_WIZ_TOKEN = "gift_wiz_token"
_GIFT_WIZ_STEP = "gift_wiz_step"  # message | image
_PENDING_GIFT = "pending_gift_token"


def gift_enabled(db: Database, user_id: int) -> bool:
    return beta_features.is_enabled(db, user_id, BETA_FEATURE_ID)


def gift_link(bot_username: str, token: str) -> str:
    return f"https://t.me/{bot_username}?start=gift_{token}"


def _fmt_until(unix: int) -> str:
    return datetime.fromtimestamp(unix, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _clear_wiz(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(_GIFT_WIZ_TOKEN, None)
    context.user_data.pop(_GIFT_WIZ_STEP, None)


def _message_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    btn("premium_gift_write_msg", lang),
                    callback_data="premium:gift_write_msg",
                )
            ],
            [
                InlineKeyboardButton(
                    btn("premium_gift_skip", lang),
                    callback_data="premium:gift_skip_msg",
                )
            ],
        ]
    )


def _image_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    btn("premium_gift_skip", lang),
                    callback_data="premium:gift_skip_img",
                )
            ],
        ]
    )


async def start_gift_customize(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    token: str,
) -> None:
    """Begin post-payment customize wizard (message → image → link)."""
    user = update.effective_user
    if user is None:
        return
    lang = _user_lang(context, user.id)
    context.user_data[_GIFT_WIZ_TOKEN] = token
    context.user_data[_GIFT_WIZ_STEP] = "message"
    chat_id = reply_chat_id(update)
    await context.bot.send_message(
        chat_id,
        t("premium_gift_ask_message", lang),
        reply_markup=_wizard(lang, back=False),
    )
    await context.bot.send_message(
        chat_id,
        t("premium_gift_ask_message_hint", lang),
        reply_markup=_message_keyboard(lang),
    )


async def _finish_gift_wiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    token: str,
) -> None:
    user = update.effective_user
    if user is None:
        return
    lang = _user_lang(context, user.id)
    db: Database = context.application.bot_data["db"]
    gift = db.mark_premium_gift_ready(token) or db.get_premium_gift(token)
    _clear_wiz(context)
    if gift is None or gift.status not in ("ready", "redeemed"):
        await context.bot.send_message(
            reply_chat_id(update),
            t("premium_gift_invalid", lang),
            reply_markup=main_menu(lang),
        )
        return
    me = await context.bot.get_me()
    link = gift_link(me.username or "", gift.token)
    await context.bot.send_message(
        reply_chat_id(update),
        t("premium_gift_link_ready", lang, link=link),
        reply_markup=main_menu(lang),
        disable_web_page_preview=True,
    )


async def on_gift_skip_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    token = context.user_data.get(_GIFT_WIZ_TOKEN)
    if not token:
        return
    lang = _user_lang(context, query.from_user.id)
    context.user_data[_GIFT_WIZ_STEP] = "image"
    await query.edit_message_text(t("premium_gift_ask_image", lang))
    await query.message.reply_text(
        t("premium_gift_ask_image_hint", lang),
        reply_markup=_image_keyboard(lang),
    )


async def on_gift_write_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    token = context.user_data.get(_GIFT_WIZ_TOKEN)
    if not token:
        return
    lang = _user_lang(context, query.from_user.id)
    context.user_data[_GIFT_WIZ_STEP] = "message"
    await query.edit_message_text(t("premium_gift_await_message", lang))


async def on_gift_skip_img(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    token = context.user_data.get(_GIFT_WIZ_TOKEN)
    if not token:
        return
    await _finish_gift_wiz(update, context, token=token)


async def gift_wiz_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply Cancel during gift wizard: skip remaining steps and show link."""
    token = context.user_data.get(_GIFT_WIZ_TOKEN)
    if not token:
        return
    await _finish_gift_wiz(update, context, token=token)


async def gift_wiz_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handle text while gift wizard awaits a message. Returns True if consumed."""
    token = context.user_data.get(_GIFT_WIZ_TOKEN)
    step = context.user_data.get(_GIFT_WIZ_STEP)
    if not token or step != "message":
        return False
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    raw = (update.effective_message.text or "").strip()
    if is_menu_button(raw) or raw in all_wizard_nav_buttons():
        if raw == btn("wizard_cancel", lang) or raw in {
            btn("wizard_cancel", loc) for loc in ("ru", "en")
        }:
            await gift_wiz_cancel(update, context)
            return True
        return False
    db: Database = context.application.bot_data["db"]
    # Cap length to keep Telegram messages sane.
    msg = raw[:1000]
    db.update_premium_gift_customize(token, message=msg)
    context.user_data[_GIFT_WIZ_STEP] = "image"
    await update.effective_message.reply_text(
        t("premium_gift_ask_image", lang),
        reply_markup=_wizard(lang, back=False),
    )
    await update.effective_message.reply_text(
        t("premium_gift_ask_image_hint", lang),
        reply_markup=_image_keyboard(lang),
    )
    return True


async def gift_wiz_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handle photo while gift wizard awaits an image. Returns True if consumed."""
    token = context.user_data.get(_GIFT_WIZ_TOKEN)
    step = context.user_data.get(_GIFT_WIZ_STEP)
    if not token or step != "image":
        return False
    photos = update.effective_message.photo or []
    if not photos:
        return False
    file_id = photos[-1].file_id
    db: Database = context.application.bot_data["db"]
    db.update_premium_gift_customize(token, image_file_id=file_id)
    await _finish_gift_wiz(update, context, token=token)
    return True


def apply_gift_start_arg(
    db: Database, context: ContextTypes.DEFAULT_TYPE, args: list[str] | None
) -> None:
    if not args:
        return
    raw = str(args[0] or "").strip()
    if not raw.startswith("gift_"):
        return
    token = raw[len("gift_") :].strip()
    if not token:
        return
    gift = db.get_premium_gift(token)
    if gift is None or gift.status != "ready":
        context.user_data["pending_gift_invalid"] = True
        return
    context.user_data[_PENDING_GIFT] = token


async def maybe_offer_pending_gift(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, lang: str
) -> None:
    if context.user_data.pop("pending_gift_invalid", None):
        await context.bot.send_message(user_id, t("premium_gift_invalid", lang))
        return
    token = context.user_data.pop(_PENDING_GIFT, None)
    if not token:
        return
    db: Database = context.application.bot_data["db"]
    gift = db.get_premium_gift(token)
    if gift is None or gift.status != "ready":
        await context.bot.send_message(user_id, t("premium_gift_invalid", lang))
        return
    kind_label = t(f"premium_gift_kind_{gift.kind}", lang)
    await context.bot.send_message(
        user_id,
        t("premium_gift_offer", lang, kind=kind_label),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        t("premium_gift_accept", lang),
                        callback_data=f"gift_accept:{token}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        t("premium_gift_decline", lang),
                        callback_data="gift_decline",
                    )
                ],
            ]
        ),
    )


async def on_gift_accept(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    token = data.split(":", 1)[1] if ":" in data else ""
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    await query.answer()
    result = prem.apply_gift_redeem(db, token, user_id)
    if result is None:
        await query.edit_message_text(t("premium_gift_invalid", lang))
        return
    gift, until_unix = result
    analytics.capture(
        user_id,
        "premium_gift_redeemed",
        {"kind": gift.kind, "buyer_id": gift.buyer_id},
    )
    custom = (gift.message or "").strip()
    if custom:
        body = custom
    elif gift.kind == "life":
        body = t("premium_gift_activated_life", lang)
    else:
        body = t(
            "premium_gift_activated",
            lang,
            until=_fmt_until(until_unix),
        )
    await query.edit_message_text(body)
    if gift.image_file_id:
        try:
            await context.bot.send_photo(user_id, gift.image_file_id)
        except Exception:
            logger.exception("gift image send failed token=%s", token[:12])


async def on_gift_decline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    lang = _user_lang(context, query.from_user.id)
    await query.answer()
    await query.edit_message_text(t("premium_gift_declined", lang))
