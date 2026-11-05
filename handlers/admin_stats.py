from __future__ import annotations

import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes

from bot_helpers import _can_use_admin_tools, _user_lang
from db import BotStats, Database
from i18n import admin_menu, reload_strings, t

logger = logging.getLogger(__name__)


def _format_stats(stats: BotStats, lang: str, *, trials: list[tuple[int, int]] | None = None) -> str:
    if trials is None:
        trials = []
    trial_list = "".join(
        t(
            "weekly_trial_line",
            lang,
            user_id=user_id,
            until=datetime.fromtimestamp(until, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            ),
        )
        for user_id, until in trials
    )
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
        trials=len(trials),
        trial_list=trial_list,
        sys_updates=stats.sys_updates,
        sys_availability=stats.sys_availability,
        sys_other=stats.sys_other,
        sys_promo=stats.sys_promo,
        blocked_users=stats.blocked_users,
        locale_en=stats.locale_en,
        locale_ru=stats.locale_ru,
        locale_uk=stats.locale_uk,
        locale_it=stats.locale_it,
        locale_unset=stats.locale_unset,
    )


async def admin_show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not _can_use_admin_tools(user_id):
        return
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    stats = db.get_bot_stats()
    trials = db.list_active_trial_users()
    await update.effective_message.reply_text(
        _format_stats(stats, lang, trials=trials),
        reply_markup=admin_menu(lang),
    )


async def admin_reload_locales(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not _can_use_admin_tools(user_id):
        return
    lang = _user_lang(context, user_id)
    try:
        counts = reload_strings()
    except Exception as exc:
        logger.exception("locale reload failed")
        await update.effective_message.reply_text(
            t("locales_reload_failed", lang, error=str(exc)[:200]),
            reply_markup=admin_menu(lang),
        )
        return
    summary = ", ".join(f"{loc}={n}" for loc, n in counts.items())
    logger.info("locales reloaded: %s", summary)
    await update.effective_message.reply_text(
        t("locales_reloaded", lang, summary=summary),
        reply_markup=admin_menu(lang),
    )

