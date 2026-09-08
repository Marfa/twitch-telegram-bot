"""Twitch Drops alerts: OAuth, campaign poll job, subscribe-to-streams callback."""

from __future__ import annotations

import asyncio
import html
import logging
import time
from datetime import datetime, timezone
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import analytics
import beta as beta_features
import premium as prem
from bot_helpers import _menu
from db import Database, Subscription, is_drops_sub
from i18n import DEFAULT_LOCALE, drops_catalog_keyboard, t
from twitch import TwitchClient

logger = logging.getLogger(__name__)

DROPS_BETA_ID = "drops-alerts"
DROPS_FEATURE_ID = "alert_types"
_DROPS_STREAM_SUBSCRIBE_CAP = 5
_DROPS_CATALOG_LIMIT = 12
_ACTIVE_CAMPAIGN_STATUSES = frozenset({"ACTIVE", "ENABLED", ""})
_PENDING_DEVICE_KEY = "drops_device_pending"


def drops_feature_available(db: Database, user_id: int) -> bool:
    if not beta_features.is_enabled(db, user_id, DROPS_BETA_ID):
        return False
    return True


async def drops_entitled(bot: Any, db: Database, user_id: int) -> bool:
    if not drops_feature_available(db, user_id):
        return False
    return await prem.has_feature(bot, db, user_id, DROPS_FEATURE_ID)


def _user_lang(db: Database, user_id: int) -> str:
    return db.get_user_locale(user_id) or DEFAULT_LOCALE


def user_has_drops_oauth(db: Database, user_id: int) -> bool:
    """True when a Drops device-code refresh token is stored (Helix sync tokens are not enough)."""
    auth = db.get_drops_auth(user_id)
    return bool(auth and auth.refresh_token)


# Back-compat alias for older call sites / checks.
user_has_twitch_oauth = user_has_drops_oauth


async def send_drops_oauth_prompt(
    bot: Any,
    twitch: TwitchClient,
    user_id: int,
    lang: str,
    *,
    application: Any | None = None,
) -> None:
    """Start Twitch device-code login required for the Drops GQL catalog."""
    try:
        started = await asyncio.to_thread(twitch.start_drops_device_code)
    except Exception:
        logger.exception("drops device-code start failed user=%s", user_id)
        await bot.send_message(user_id, t("drops_oauth_failed", lang))
        return
    device_code = str(started.get("device_code") or "")
    user_code = str(started.get("user_code") or "")
    uri = str(started.get("verification_uri") or "https://www.twitch.tv/activate")
    interval = int(started.get("interval") or 5)
    expires_in = int(started.get("expires_in") or 1800)
    if not device_code or not user_code:
        await bot.send_message(user_id, t("drops_oauth_failed", lang))
        return
    await bot.send_message(
        user_id,
        t("drops_oauth_prompt", lang, code=user_code, url=uri),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(t("drops_oauth_button", lang), url=uri)]]
        ),
    )
    if application is None:
        return
    pending = application.bot_data.setdefault(_PENDING_DEVICE_KEY, {})
    pending[user_id] = {
        "device_code": device_code,
        "interval": interval,
        "expires_at": time.time() + expires_in,
        "lang": lang,
    }
    jq = getattr(application, "job_queue", None)
    if jq is not None:
        name = f"drops_device:{user_id}"
        for old in jq.get_jobs_by_name(name) or []:
            old.schedule_removal()
        jq.run_repeating(
            poll_drops_device_code_job,
            interval=max(3, interval),
            first=max(3, interval),
            data={"user_id": user_id},
            name=name,
            job_kwargs={"misfire_grace_time": 30},
        )


def _access_token_for_owner(
    db: Database, twitch: TwitchClient, owner_id: int
) -> str | None:
    auth = db.get_drops_auth(owner_id)
    if not auth or not auth.refresh_token:
        return None
    try:
        data = twitch.refresh_drops_gql_token(auth.refresh_token)
    except Exception:
        logger.warning("drops GQL token refresh failed owner=%s — clearing auth", owner_id)
        try:
            db.delete_drops_auth(owner_id)
        except Exception:
            logger.exception("drops_auth delete failed owner=%s", owner_id)
        return None
    access = str(data.get("access_token") or "")
    new_refresh = str(data.get("refresh_token") or "") or auth.refresh_token
    if new_refresh != auth.refresh_token:
        db.update_drops_auth_refresh(owner_id, new_refresh)
    return access or None


async def poll_drops_device_code_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    if job is None:
        return
    user_id = int((job.data or {}).get("user_id") or 0)
    if not user_id:
        job.schedule_removal()
        return
    pending_map = context.application.bot_data.get(_PENDING_DEVICE_KEY) or {}
    pending = pending_map.get(user_id)
    if not pending:
        job.schedule_removal()
        return
    if time.time() >= float(pending.get("expires_at") or 0):
        pending_map.pop(user_id, None)
        job.schedule_removal()
        lang = str(pending.get("lang") or DEFAULT_LOCALE)
        await context.bot.send_message(user_id, t("drops_oauth_failed", lang))
        return
    twitch: TwitchClient = context.application.bot_data["twitch"]
    db: Database = context.application.bot_data["db"]
    lang = str(pending.get("lang") or _user_lang(db, user_id))
    try:
        token_info = await asyncio.to_thread(
            twitch.poll_drops_device_code, str(pending.get("device_code") or "")
        )
    except Exception:
        logger.exception("drops device poll failed user=%s", user_id)
        pending_map.pop(user_id, None)
        job.schedule_removal()
        await context.bot.send_message(user_id, t("drops_oauth_failed", lang))
        return
    if token_info is None:
        return
    pending_map.pop(user_id, None)
    job.schedule_removal()
    refresh = str(token_info.get("refresh_token") or "")
    access = str(token_info.get("access_token") or "")
    if not refresh or not access:
        await context.bot.send_message(user_id, t("drops_oauth_failed", lang))
        return
    login = ""
    uid = ""
    try:
        user = await asyncio.to_thread(twitch.get_drops_token_user, access)
        if user:
            login = str(user.get("login") or "")
            uid = str(user.get("id") or "")
    except Exception:
        logger.warning("drops device user lookup failed user=%s", user_id)
    db.upsert_drops_auth(
        user_id,
        twitch_user_id=uid,
        twitch_login=login,
        refresh_token=refresh,
    )
    analytics.capture(user_id, "drops_oauth_linked", {})
    await context.bot.send_message(user_id, t("drops_oauth_done", lang))
    await send_drops_catalog(
        context.bot,
        db,
        twitch,
        user_id,
        lang,
        bot_data=context.application.bot_data,
    )


def list_active_drop_campaigns(
    db: Database, twitch: TwitchClient, owner_id: int
) -> list[dict[str, Any]] | None:
    """Return ACTIVE campaigns or None on auth/API failure."""
    access = _access_token_for_owner(db, twitch, owner_id)
    if not access:
        return None
    try:
        campaigns = twitch.get_viewer_drop_campaigns(access)
    except Exception:
        logger.exception("drops catalog fetch failed owner=%s", owner_id)
        return None
    active = [c for c in campaigns if _campaign_active(c) and str(c.get("id") or "")]
    return active[:_DROPS_CATALOG_LIMIT]


async def send_drops_catalog(
    bot: Any,
    db: Database,
    twitch: TwitchClient,
    user_id: int,
    lang: str,
    *,
    bot_data: dict[str, Any] | None = None,
    user_data: dict[str, Any] | None = None,
    reply_markup_extra: Any = None,
    application: Any | None = None,
) -> list[dict[str, Any]]:
    """Fetch and send the available-Drops list. Returns campaigns stored for pick."""
    campaigns = await asyncio.to_thread(list_active_drop_campaigns, db, twitch, user_id)
    store = bot_data if bot_data is not None else None

    def _clear_store() -> None:
        if store is not None:
            store.setdefault("drops_catalog_by_user", {}).pop(user_id, None)
        if user_data is not None:
            user_data.pop("drops_catalog_candidates", None)

    if campaigns is None:
        _clear_store()
        if not user_has_drops_oauth(db, user_id):
            await send_drops_oauth_prompt(
                bot, twitch, user_id, lang, application=application
            )
            await bot.send_message(
                user_id,
                t("drops_catalog_need_oauth", lang),
                reply_markup=reply_markup_extra,
            )
            return []
        await bot.send_message(
            user_id,
            t("drops_catalog_fetch_failed", lang),
            reply_markup=reply_markup_extra,
        )
        return []
    if not campaigns:
        _clear_store()
        await bot.send_message(
            user_id,
            t("drops_catalog_empty", lang),
            reply_markup=reply_markup_extra,
        )
        return []

    compact = [
        {
            "id": str(c.get("id") or ""),
            "name": str(c.get("name") or ""),
            "game_id": str(c.get("game_id") or ""),
            "game_name": str(c.get("game_name") or ""),
        }
        for c in campaigns
    ]
    if store is not None:
        store.setdefault("drops_catalog_by_user", {})[user_id] = compact
    if user_data is not None:
        user_data["drops_catalog_candidates"] = compact
    await bot.send_message(
        user_id,
        t("drops_catalog_prompt", lang),
        reply_markup=drops_catalog_keyboard(lang, compact),
    )
    if reply_markup_extra is not None:
        await bot.send_message(
            user_id,
            t("drops_catalog_pick_hint", lang),
            reply_markup=reply_markup_extra,
        )
    return compact


async def complete_drops_oauth(
    application: Any,
    owner_id: int,
    error: str | None,
    token_info: dict[str, str] | None,
) -> None:
    """Legacy Helix redirect callback — Drops now uses device-code; ask user to retry."""
    del token_info  # Helix tokens cannot load the GQL catalog.
    db: Database = application.bot_data["db"]
    lang = _user_lang(db, owner_id)
    if error == "access_denied":
        await application.bot.send_message(owner_id, t("oauth_denied", lang))
        return
    await application.bot.send_message(owner_id, t("drops_oauth_use_device", lang))


def _campaign_active(campaign: dict[str, Any]) -> bool:
    status = str(campaign.get("status") or "").upper()
    return status in _ACTIVE_CAMPAIGN_STATUSES or status == "ACTIVE"


def _format_campaign_alert(
    lang: str,
    *,
    campaign: dict[str, Any],
    streams: list[dict[str, Any]],
) -> str:
    name = html.escape(str(campaign.get("name") or t("drops_unnamed", lang)))
    game = html.escape(str(campaign.get("game_name") or ""))
    starts = html.escape(str(campaign.get("starts_at") or "—"))
    ends = html.escape(str(campaign.get("ends_at") or "—"))
    cond_parts: list[str] = []
    for d in campaign.get("drops") or []:
        if not isinstance(d, dict):
            continue
        mins = d.get("required_minutes")
        dname = str(d.get("name") or "")
        benefits = ", ".join(d.get("benefit_names") or [])
        bit = dname or benefits or "?"
        if mins is not None:
            cond_parts.append(f"{html.escape(bit)} ({mins} min)")
        else:
            cond_parts.append(html.escape(bit))
    conditions = "; ".join(cond_parts) if cond_parts else "—"
    stream_lines: list[str] = []
    for s in streams[:3]:
        login = html.escape(str(s.get("user_login") or ""))
        title = html.escape(str(s.get("title") or "")[:80])
        stream_lines.append(
            f'• <a href="https://twitch.tv/{login}">{login}</a> — {title}'
        )
    streams_block = (
        "\n".join(stream_lines)
        if stream_lines
        else t("drops_alert_no_streams", lang)
    )
    return t(
        "drops_alert_body",
        lang,
        name=name,
        game=game,
        starts=starts,
        ends=ends,
        conditions=conditions,
        streams=streams_block,
    )


def _drops_alert_keyboard(lang: str, sub_id: int, campaign_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("drops_subscribe_streams_btn", lang),
                    callback_data=f"drops_sub_streams:{sub_id}:{campaign_id[:32]}",
                )
            ]
        ]
    )


async def check_drops(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Poll GQL campaigns for enabled Drops subscriptions and notify on new matches."""
    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    subs = db.get_enabled_drops_subscriptions()
    if not subs:
        return

    by_owner: dict[int, list[Subscription]] = {}
    for sub in subs:
        if not is_drops_sub(sub):
            continue
        by_owner.setdefault(sub.owner_id, []).append(sub)

    now_iso = datetime.now(timezone.utc).isoformat()
    for owner_id, owner_subs in by_owner.items():
        if not await drops_entitled(context.bot, db, owner_id):
            continue
        access = await asyncio.to_thread(_access_token_for_owner, db, twitch, owner_id)
        if not access:
            continue
        try:
            campaigns = await asyncio.to_thread(twitch.get_viewer_drop_campaigns, access)
        except Exception:
            logger.exception("drops campaign fetch failed owner=%s", owner_id)
            continue

        lang = _user_lang(db, owner_id)
        for sub in owner_subs:
            game_id = (sub.drops_game_id or "").strip()
            if not game_id:
                continue
            matching = [
                c
                for c in campaigns
                if _campaign_active(c)
                and str(c.get("game_id") or "") == game_id
                and str(c.get("id") or "")
            ]
            # First poll for this subscription: mark active campaigns seen, no spam.
            any_seen = any(
                db.has_seen_drop_campaign(owner_id, str(c["id"]), sub.id)
                for c in matching
            )
            if not any_seen and matching:
                for campaign in matching:
                    db.mark_drop_campaign_seen(
                        owner_id,
                        str(campaign["id"]),
                        sub.id,
                        first_seen_at=now_iso,
                    )
                continue
            for campaign in matching:
                cid = str(campaign["id"])
                if db.has_seen_drop_campaign(owner_id, cid, sub.id):
                    continue
                db.mark_drop_campaign_seen(
                    owner_id, cid, sub.id, first_seen_at=now_iso
                )
                streams: list[dict[str, Any]] = []
                try:
                    streams = await asyncio.to_thread(
                        twitch.get_streams_with_drops,
                        game_id,
                        first=40,
                        limit=3,
                    )
                except Exception:
                    logger.exception("drops streams fetch failed game=%s", game_id)
                text = _format_campaign_alert(lang, campaign=campaign, streams=streams)
                markup = _drops_alert_keyboard(lang, sub.id, cid)
                try:
                    await context.bot.send_message(
                        sub.chat_id,
                        text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                        reply_markup=markup,
                        message_thread_id=sub.thread_id,
                    )
                    analytics.capture(
                        owner_id,
                        "drops_alert_sent",
                        {"subscription_id": sub.id, "campaign_id": cid},
                    )
                except Exception:
                    logger.exception(
                        "drops alert send failed owner=%s sub=%s", owner_id, sub.id
                    )


async def on_drops_subscribe_streams(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()
    user_id = query.from_user.id
    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    lang = _user_lang(db, user_id)
    parts = query.data.split(":", 2)
    if len(parts) != 3:
        return
    try:
        sub_id = int(parts[1])
    except ValueError:
        return
    sub = db.get_subscription(sub_id, user_id)
    if sub is None or not is_drops_sub(sub):
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(user_id, t("drops_subscribe_gone", lang))
        return
    if not await drops_entitled(context.bot, db, user_id):
        await context.bot.send_message(user_id, t("drops_need_premium", lang))
        return

    game_id = (sub.drops_game_id or "").strip()
    try:
        streams = await asyncio.to_thread(
            twitch.get_streams_with_drops,
            game_id,
            first=50,
            limit=_DROPS_STREAM_SUBSCRIBE_CAP,
        )
    except Exception:
        logger.exception("drops subscribe streams fetch failed")
        await context.bot.send_message(user_id, t("drops_subscribe_failed", lang))
        return

    if not streams:
        await context.bot.send_message(user_id, t("drops_alert_no_streams", lang))
        return

    from config import MAX_SUBSCRIPTIONS_PER_OWNER

    existing = db.get_subscriptions_by_owner(user_id)
    existing_uids = {s.twitch_user_id for s in existing}
    created = 0
    paused = 0
    skipped = 0
    for stream in streams:
        uid = str(stream.get("user_id") or "")
        login = str(stream.get("user_login") or "").lower()
        if not uid or not login:
            continue
        if uid in existing_uids or any(
            s.twitch_username == login and s.notify_on_live and not s.notify_on_drops
            for s in existing
        ):
            skipped += 1
            continue
        if len(existing) + created >= MAX_SUBSCRIPTIONS_PER_OWNER:
            break
        enabled = await prem.may_enable_subscription_async(
            context.bot, db, user_id, twitch_username=login
        )
        db.add_subscription(
            owner_id=user_id,
            twitch_username=login,
            twitch_user_id=uid,
            message_template=t("import_default_template", lang),
            dest_type=sub.dest_type,
            chat_id=sub.chat_id,
            thread_id=sub.thread_id,
            disable_link_preview=True,
            enabled=enabled,
            notify_on_live=True,
            notify_on_end=False,
            notify_on_category_change=False,
            notify_on_drops=False,
        )
        created += 1
        if not enabled:
            paused += 1
        existing_uids.add(uid)

    await context.bot.send_message(
        user_id,
        t(
            "drops_subscribe_result",
            lang,
            created=created,
            paused=paused,
            skipped=skipped,
        ),
        reply_markup=_menu(lang, user_id),
    )
