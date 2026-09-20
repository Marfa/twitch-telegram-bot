"""Unified Twitch OAuth fan-out, reauth pause, and restore."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application

from db import Database
from i18n import DEFAULT_LOCALE, t
from twitch import BOT_TWITCH_OAUTH_SCOPES, TwitchClient

logger = logging.getLogger(__name__)

REAUTH_PURPOSE = "reauth"


def fan_out_twitch_refresh(
    db: Database,
    owner_id: int,
    *,
    refresh: str,
    twitch_user_id: str,
    twitch_login: str,
) -> None:
    if not refresh or not twitch_user_id:
        return
    db.fan_out_twitch_oauth_token(
        owner_id,
        twitch_user_id=twitch_user_id,
        twitch_login=twitch_login or "",
        refresh_token=refresh,
    )


async def restore_after_twitch_oauth(
    application: Application,
    owner_id: int,
) -> None:
    import premium as prem
    from handlers.background_jobs import sync_optional_jobs

    db: Database = application.bot_data["db"]
    twitch: TwitchClient = application.bot_data["twitch"]

    for sub in db.list_subscriptions_paused_for_reauth(owner_id):
        enable = False
        if prem.alert_type_entitled_sync(db, owner_id, sub):
            if await prem.may_enable_subscription_async(
                application.bot,
                db,
                owner_id,
                twitch_username=sub.twitch_username,
            ):
                enable = True
        db.clear_subscription_paused_for_reauth(
            sub.id, owner_id, enabled=enable
        )

    paused_whisper = db.take_whisper_paused_for_reauth(owner_id)
    if paused_whisper and paused_whisper.refresh_token and paused_whisper.twitch_user_id:
        try:
            from handlers.settings import _enable_whisper_eventsub

            _enable_whisper_eventsub(
                db,
                twitch,
                owner_id,
                refresh=paused_whisper.refresh_token,
                twitch_user_id=paused_whisper.twitch_user_id,
                twitch_login=paused_whisper.twitch_login,
            )
        except Exception as exc:
            logger.warning(
                "whisper restore after reauth failed owner=%s: %s",
                owner_id,
                type(exc).__name__,
            )

    sync_optional_jobs(application.job_queue, db)

    mon = db.get_follow_monitor(owner_id)
    if mon and mon.enabled and mon.refresh_token and not mon.needs_reauth:
        from handlers.follow_monitor import _sync_owner

        import asyncio

        asyncio.create_task(_sync_owner(application, owner_id))


async def request_twitch_reauth(
    application: Application,
    owner_id: int,
    *,
    reason: str = "",
) -> None:
    """Mark stores needs_reauth, pause sync alerts, send one full-scope OAuth DM."""
    from bot_helpers import with_oauth_legal
    from config import twitch_oauth_redirect_uri
    from handlers.background_jobs import sync_optional_jobs
    from health import create_pending_login_state

    db: Database = application.bot_data["db"]
    twitch: TwitchClient = application.bot_data["twitch"]
    lang = db.get_user_locale(owner_id) or DEFAULT_LOCALE

    db.mark_twitch_stores_needs_reauth(owner_id)
    paused_n, _whisper = db.pause_for_twitch_reauth(owner_id)
    sync_optional_jobs(application.job_queue, db)

    already = db.get_twitch_reauth_notified_at(owner_id)
    if already:
        logger.info(
            "twitch reauth already notified owner=%s reason=%s paused_subs=%s",
            owner_id,
            reason or "-",
            paused_n,
        )
        return

    redirect = twitch_oauth_redirect_uri()
    markup = None
    text = t("twitch_reauth_required", lang)
    if paused_n:
        text = t("twitch_reauth_required_paused", lang, count=paused_n)
    if redirect:
        state = create_pending_login_state(
            owner_id, lang, purpose=REAUTH_PURPOSE
        )
        url = twitch.build_authorize_url(
            redirect_uri=redirect,
            state=state,
            scopes=BOT_TWITCH_OAUTH_SCOPES,
            force_verify=True,
        )
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        t("twitch_reauth_button", lang), url=url
                    )
                ]
            ]
        )
        text = with_oauth_legal(text, lang)

    try:
        await application.bot.send_message(
            owner_id, text, reply_markup=markup
        )
        db.set_twitch_reauth_notified_at(
            owner_id, datetime.now(timezone.utc).isoformat()
        )
    except Exception:
        logger.exception("Cannot send twitch reauth DM to %s", owner_id)


async def complete_reauth_oauth(
    application: Application,
    owner_id: int,
    error: str | None,
    token_info: dict[str, str] | None,
) -> None:
    db: Database = application.bot_data["db"]
    lang = db.get_user_locale(owner_id) or DEFAULT_LOCALE
    if error:
        key = "oauth_denied" if error == "access_denied" else "twitch_reauth_failed"
        await application.bot.send_message(owner_id, t(key, lang))
        return
    info = token_info or {}
    refresh = info.get("refresh_token") or ""
    twitch_user_id = info.get("twitch_user_id") or ""
    twitch_login = info.get("twitch_login") or ""
    if not refresh or not twitch_user_id:
        await application.bot.send_message(
            owner_id, t("twitch_reauth_failed", lang)
        )
        return
    fan_out_twitch_refresh(
        db,
        owner_id,
        refresh=refresh,
        twitch_user_id=twitch_user_id,
        twitch_login=twitch_login,
    )
    await restore_after_twitch_oauth(application, owner_id)
    await application.bot.send_message(
        owner_id, t("twitch_reauth_done", lang)
    )


def apply_oauth_success_tokens(
    db: Database,
    owner_id: int,
    token_info: dict[str, Any] | None,
) -> bool:
    """Fan-out refresh from a completer's token_info. Returns True if applied."""
    info = token_info or {}
    refresh = str(info.get("refresh_token") or "")
    twitch_user_id = str(info.get("twitch_user_id") or "")
    twitch_login = str(info.get("twitch_login") or "")
    if not refresh or not twitch_user_id:
        return False
    fan_out_twitch_refresh(
        db,
        owner_id,
        refresh=refresh,
        twitch_user_id=twitch_user_id,
        twitch_login=twitch_login,
    )
    return True


def revoke_twitch_oauth_tokens(db: Database, owner_id: int) -> None:
    """User-initiated wipe of Twitch feature tokens (sync, chat, whispers, …)."""
    db.revoke_user_twitch_oauth_tokens(owner_id)
    db.pause_for_twitch_reauth(owner_id)


def revoke_donationalerts_oauth_tokens(db: Database, owner_id: int) -> None:
    """User-initiated wipe of DonationAlerts OAuth."""
    db.revoke_user_donationalerts_oauth_tokens(owner_id)


def revoke_all_oauth_tokens(db: Database, owner_id: int) -> None:
    """Wipe Twitch feature tokens + DonationAlerts (tests / full reset)."""
    revoke_twitch_oauth_tokens(db, owner_id)
    revoke_donationalerts_oauth_tokens(db, owner_id)
