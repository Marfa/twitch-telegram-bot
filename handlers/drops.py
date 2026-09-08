"""Twitch Drops alerts: OAuth, campaign poll job, subscribe-to-streams callback."""

from __future__ import annotations

import asyncio
import html
import logging
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
from twitch import DROPS_OAUTH_SCOPES, TwitchClient

logger = logging.getLogger(__name__)

DROPS_BETA_ID = "drops-alerts"
DROPS_FEATURE_ID = "alert_types"
_DROPS_STREAM_SUBSCRIBE_CAP = 5
_DROPS_CATALOG_LIMIT = 12
_ACTIVE_CAMPAIGN_STATUSES = frozenset({"ACTIVE", "ENABLED", ""})


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


async def send_drops_oauth_prompt(
    bot: Any,
    twitch: TwitchClient,
    user_id: int,
    lang: str,
) -> None:
    from config import twitch_oauth_redirect_uri
    from health import create_oauth_state

    redirect = twitch_oauth_redirect_uri()
    if not redirect:
        await bot.send_message(user_id, t("drops_oauth_unavailable", lang))
        return
    state = create_oauth_state(user_id, lang, purpose="drops")
    url = twitch.build_authorize_url(
        redirect_uri=redirect, state=state, scopes=DROPS_OAUTH_SCOPES
    )
    await bot.send_message(
        user_id,
        t("drops_oauth_prompt", lang),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(t("drops_oauth_button", lang), url=url)]]
        ),
    )


def user_has_twitch_oauth(db: Database, user_id: int) -> bool:
    """True if any bot feature already stored a Twitch user refresh for this Telegram user."""
    return _first_refresh_source(db, user_id) is not None


def _first_refresh_source(
    db: Database, owner_id: int
) -> tuple[str, str, Any] | None:
    """(source, refresh_token, row_or_None) — prefer drops_auth, then other OAuth stores."""
    auth = db.get_drops_auth(owner_id)
    if auth and auth.refresh_token:
        return ("drops", auth.refresh_token, auth)
    sync = db.get_twitch_sync(owner_id)
    if sync and sync.refresh_token:
        return ("sync", sync.refresh_token, sync)
    chat = db.get_chat_auth(owner_id)
    if chat and chat.refresh_token:
        return ("chat", chat.refresh_token, chat)
    whisper = db.get_whisper_alert(owner_id)
    if whisper and whisper.refresh_token:
        return ("whisper", whisper.refresh_token, whisper)
    premium_rt = db.get_premium_twitch_refresh(owner_id)
    if premium_rt:
        return ("premium", premium_rt, None)
    return None


def _persist_rotated_refresh(
    db: Database,
    owner_id: int,
    source: str,
    new_refresh: str,
    meta: Any,
) -> None:
    """Write rotated refresh back to the same store (Twitch invalidates the old one)."""
    if source == "drops":
        db.update_drops_auth_refresh(owner_id, new_refresh)
        return
    if source == "sync" and meta is not None:
        db.update_twitch_sync_tokens(
            owner_id,
            new_refresh,
            last_sync_at=str(getattr(meta, "last_sync_at", "") or ""),
            next_sync_at=str(getattr(meta, "next_sync_at", "") or ""),
        )
        return
    if source == "chat" and meta is not None:
        db.upsert_chat_auth(
            owner_id,
            twitch_user_id=str(meta.twitch_user_id or ""),
            twitch_login=str(meta.twitch_login or ""),
            refresh_token=new_refresh,
        )
        return
    if source == "whisper" and meta is not None:
        db.upsert_whisper_alert(
            owner_id,
            enabled=bool(meta.enabled),
            twitch_user_id=str(meta.twitch_user_id or ""),
            twitch_login=str(meta.twitch_login or ""),
            refresh_token=new_refresh,
            eventsub_id=str(getattr(meta, "eventsub_id", "") or ""),
        )
        return
    if source == "premium":
        db.set_premium_twitch_refresh(owner_id, new_refresh)


def _access_token_for_owner(
    db: Database, twitch: TwitchClient, owner_id: int
) -> str | None:
    found = _first_refresh_source(db, owner_id)
    if not found:
        return None
    source, refresh, meta = found
    try:
        data = twitch.refresh_user_token(refresh)
    except Exception:
        logger.warning("drops token refresh failed owner=%s source=%s", owner_id, source)
        return None
    access = str(data.get("access_token") or "")
    new_refresh = str(data.get("refresh_token") or "") or refresh
    if new_refresh != refresh:
        _persist_rotated_refresh(db, owner_id, source, new_refresh, meta)
    return access or None


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
    # Prefer unique games first for the picker; keep campaign identity.
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
        await bot.send_message(
            user_id,
            t("drops_catalog_fetch_failed", lang),
            reply_markup=reply_markup_extra,
        )
        return []
    if not campaigns:
        _clear_store()
        text = t("drops_catalog_empty", lang)
        if reply_markup_extra is not None:
            await bot.send_message(user_id, text, reply_markup=reply_markup_extra)
        else:
            await bot.send_message(user_id, text)
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
            t("drops_game_prompt", lang),
            reply_markup=reply_markup_extra,
        )
    return compact


async def complete_drops_oauth(
    application: Any,
    owner_id: int,
    error: str | None,
    token_info: dict[str, str] | None,
) -> None:
    db: Database = application.bot_data["db"]
    twitch: TwitchClient = application.bot_data["twitch"]
    lang = _user_lang(db, owner_id)
    if error or not token_info:
        key = "oauth_denied" if error == "access_denied" else "drops_oauth_failed"
        await application.bot.send_message(owner_id, t(key, lang))
        return
    refresh = token_info.get("refresh_token") or ""
    if not refresh:
        await application.bot.send_message(owner_id, t("drops_oauth_failed", lang))
        return
    db.upsert_drops_auth(
        owner_id,
        twitch_user_id=str(token_info.get("twitch_user_id") or ""),
        twitch_login=str(token_info.get("twitch_login") or ""),
        refresh_token=refresh,
    )
    analytics.capture(owner_id, "drops_oauth_linked", {})
    await application.bot.send_message(owner_id, t("drops_oauth_done", lang))
    await send_drops_catalog(
        application.bot,
        db,
        twitch,
        owner_id,
        lang,
        bot_data=application.bot_data,
    )


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
