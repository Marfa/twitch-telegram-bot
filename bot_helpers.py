from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

from telegram import ReplyKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter
from telegram.ext import ContextTypes, filters

import beta as beta_features
import demo_mode
from db import Database
from i18n import (
    DEFAULT_LOCALE,
    SUPPORTED_LOCALES,
    btn,
    main_menu,
    settings_menu,
    wizard_menu,
)

logger = logging.getLogger(__name__)

# Soft pacing for mass DM sends — keeps under Telegram flood limits.
_BROADCAST_SEND_PAUSE = 0.05
_PAUSE_NOTIFICATIONS_BETA_ID = "pause-notifications"


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
    except BadRequest:
        pass


def _btn_filter(key: str) -> filters.Regex:
    texts = "|".join(re.escape(btn(key, loc)) for loc in SUPPORTED_LOCALES)
    if key == "beta_mode":
        return filters.Regex(rf"^({texts})( \(\d+/\d+\))?$")
    return filters.Regex(f"^({texts})$")


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
) -> str:
    """Send one DM. Returns 'sent', 'blocked', or 'failed'."""
    if _user_notifications_paused(db, uid):
        return "sent"
    kwargs: dict = {}
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup
    if disable_web_page_preview:
        kwargs["disable_web_page_preview"] = True
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

