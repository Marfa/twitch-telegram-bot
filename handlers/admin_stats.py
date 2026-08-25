from __future__ import annotations

from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes

from bot_helpers import _can_use_admin_tools, _user_lang
from db import BotStats, Database
from i18n import admin_menu, t


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
        blocked_users=stats.blocked_users,
        locale_en=stats.locale_en,
        locale_ru=stats.locale_ru,
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

