"""Twitch Drops alerts: device-code OAuth, catalog, stream + claim jobs."""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
import secrets
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
from config import MAX_SUBSCRIPTIONS_PER_OWNER
from db import Database, Subscription, is_drops_sub
from i18n import DEFAULT_LOCALE, drops_catalog_keyboard, t
from twitch import TwitchClient

logger = logging.getLogger(__name__)

DROPS_BETA_ID = "drops-alerts"
DROPS_FEATURE_ID = "alert_types"


def drops_device_id_for(owner_id: int) -> str:
    """Stable per-user X-Device-Id so GQL works after bot restarts."""
    return hashlib.sha256(f"drops-gql-device:{int(owner_id)}".encode()).hexdigest()[:32]


_DROPS_CATALOG_LIMIT = 12
_DIGEST_SEEN_SUB_ID = 0
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
    """True when a Drops device-code refresh token is stored."""
    auth = db.get_drops_auth(user_id)
    return bool(auth and auth.refresh_token)


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
    device_id = drops_device_id_for(user_id)
    try:
        started = await asyncio.to_thread(
            twitch.start_drops_device_code, device_id=device_id
        )
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
        "device_id": device_id,
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
    now = int(time.time())
    if auth.access_token and int(auth.access_expires_at or 0) > now + 60:
        return auth.access_token
    try:
        data = twitch.refresh_drops_gql_token(auth.refresh_token)
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        twitch_msg = ""
        try:
            body = getattr(getattr(exc, "response", None), "json", lambda: {})()
            if isinstance(body, dict):
                twitch_msg = str(body.get("message") or body.get("error") or "")[:80]
        except Exception:
            twitch_msg = ""
        # Keep drops_auth on soft failures and on "missing client secret" (Android
        # public client cannot refresh — wiping forced a useless re-prompt loop).
        # Wipe only when Twitch says the refresh token itself is dead.
        msg_l = twitch_msg.lower()
        hard = status in (400, 401) and any(
            n in msg_l
            for n in (
                "invalid refresh",
                "invalid_grant",
            )
        )
        logger.warning(
            "drops GQL token refresh failed owner=%s status=%s msg=%s",
            owner_id,
            status,
            twitch_msg or "-",
        )
        if hard:
            try:
                db.delete_drops_auth(owner_id)
            except Exception:
                logger.exception("drops_auth wipe after hard refresh fail owner=%s", owner_id)
            return None
        if auth.access_token and int(auth.access_expires_at or 0) > now:
            return auth.access_token
        return None
    access = str(data.get("access_token") or "")
    if not access:
        return None
    new_refresh = str(data.get("refresh_token") or "") or auth.refresh_token
    expires_in = max(60, int(data.get("expires_in") or 14400))
    db.update_drops_auth_access(
        owner_id,
        access_token=access,
        access_expires_at=now + expires_in,
        refresh_token=new_refresh if new_refresh != auth.refresh_token else None,
    )
    return access


def _drops_list_label(*, game_name: str, campaign_name: str, game_id: str) -> str:
    game = (game_name or "").strip() or game_id
    drop = (campaign_name or "").strip()
    if game and drop and drop.casefold() != game.casefold():
        return f"{game} — {drop}"[:64]
    return (game or drop or game_id)[:64]


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
            twitch.poll_drops_device_code,
            str(pending.get("device_code") or ""),
            device_id=str(pending.get("device_id") or "") or None,
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
        access_token=access,
        access_expires_at=int(time.time())
        + max(60, int(token_info.get("expires_in") or 14400)),
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
        access_token=access,
        application=context.application,
    )


def _campaign_active(campaign: dict[str, Any]) -> bool:
    status = str(campaign.get("status") or "").upper()
    return status in _ACTIVE_CAMPAIGN_STATUSES or status == "ACTIVE"


def _enrich_claimed(
    campaigns: list[dict[str, Any]], claims: dict[str, dict[str, Any]]
) -> None:
    claimed_ids = {
        did for did, info in claims.items() if info.get("is_claimed")
    }
    for c in campaigns:
        drops = c.get("drops") or []
        ids = [str(d.get("id") or "") for d in drops if isinstance(d, dict)]
        ids = [i for i in ids if i]
        if ids and all(i in claimed_ids for i in ids):
            c["claimed"] = True
        for d in drops:
            if not isinstance(d, dict):
                continue
            did = str(d.get("id") or "")
            if did in claims:
                d["is_claimed"] = bool(claims[did].get("is_claimed"))


def list_active_drop_campaigns(
    db: Database,
    twitch: TwitchClient,
    owner_id: int,
    *,
    access_token: str | None = None,
) -> list[dict[str, Any]] | None:
    """Return ACTIVE campaigns from twitchdrops.app, or None on API failure.

    OAuth is optional: used only to mark already-claimed drops in the catalog.
    """
    try:
        campaigns = twitch.fetch_twitchdrops_app_campaigns(sort="new")
    except Exception:
        logger.exception("drops catalog fetch failed owner=%s", owner_id)
        return None
    active = [
        c for c in campaigns if _campaign_active(c) and str(c.get("id") or "")
    ]
    out = active[:_DROPS_CATALOG_LIMIT]
    access = (access_token or "").strip() or _access_token_for_owner(
        db, twitch, owner_id
    )
    if access:
        try:
            claims = twitch.get_inventory_claimed_drops(
                access, device_id=drops_device_id_for(owner_id)
            )
            _enrich_claimed(out, claims)
        except Exception:
            logger.warning("drops inventory claim enrich failed owner=%s", owner_id)
    return out


def _with_extra_markup(
    markup: InlineKeyboardMarkup, extra: Any | None
) -> InlineKeyboardMarkup:
    if extra is None or not getattr(extra, "inline_keyboard", None):
        return markup
    rows = [list(r) for r in markup.inline_keyboard]
    rows.extend(list(r) for r in extra.inline_keyboard)
    return InlineKeyboardMarkup(rows)


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
    access_token: str | None = None,
    application: Any | None = None,
) -> list[dict[str, Any]]:
    """Fetch and send the available-Drops list with digest checkbox."""
    del application  # reserved for callers that still pass application=
    campaigns = await asyncio.to_thread(
        list_active_drop_campaigns,
        db,
        twitch,
        user_id,
        access_token=access_token,
    )
    store = bot_data if bot_data is not None else None

    def _clear_store() -> None:
        if store is not None:
            store.setdefault("drops_catalog_by_user", {}).pop(user_id, None)
        if user_data is not None:
            user_data.pop("drops_catalog_candidates", None)

    auth = db.get_drops_auth(user_id)
    digest_on = bool(auth and auth.digest_enabled)

    if campaigns is None:
        _clear_store()
        await bot.send_message(
            user_id,
            t("drops_catalog_fetch_failed", lang),
            reply_markup=_with_extra_markup(
                drops_catalog_keyboard(
                    lang, [], digest_enabled=digest_on, show_rebind=True
                ),
                reply_markup_extra,
            ),
        )
        return []
    if not campaigns:
        _clear_store()
        await bot.send_message(
            user_id,
            t("drops_catalog_empty", lang),
            reply_markup=_with_extra_markup(
                drops_catalog_keyboard(
                    lang, [], digest_enabled=digest_on, show_rebind=True
                ),
                reply_markup_extra,
            ),
        )
        return []

    compact = [
        {
            "id": str(c.get("id") or ""),
            "name": str(c.get("name") or ""),
            "game_id": str(c.get("game_id") or ""),
            "game_name": str(c.get("game_name") or ""),
            "game_slug": str(c.get("game_slug") or ""),
            "claimed": bool(c.get("claimed")),
            "how_to_earn": str(c.get("how_to_earn") or ""),
            "starts_at": str(c.get("starts_at") or ""),
            "ends_at": str(c.get("ends_at") or ""),
            "drops": c.get("drops") or [],
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
        reply_markup=_with_extra_markup(
            drops_catalog_keyboard(lang, compact, digest_enabled=digest_on),
            reply_markup_extra,
        ),
    )
    return compact


async def complete_drops_oauth(
    application: Any,
    owner_id: int,
    error: str | None,
    token_info: dict[str, str] | None,
) -> None:
    """Legacy Helix redirect — Drops uses device-code."""
    del token_info
    db: Database = application.bot_data["db"]
    lang = _user_lang(db, owner_id)
    if error == "access_denied":
        await application.bot.send_message(owner_id, t("oauth_denied", lang))
        return
    await application.bot.send_message(owner_id, t("drops_oauth_use_device", lang))


def _format_dates(campaign: dict[str, Any]) -> str:
    starts = html.escape(str(campaign.get("starts_at") or "—"))
    ends = html.escape(str(campaign.get("ends_at") or "—"))
    return f"{starts} — {ends}"


def _format_digest_alert(lang: str, campaign: dict[str, Any]) -> str:
    name = html.escape(str(campaign.get("name") or t("drops_unnamed", lang)))
    game = html.escape(str(campaign.get("game_name") or "").strip() or "—")
    return t(
        "drops_digest_alert_body",
        lang,
        name=name,
        game=game,
        dates=_format_dates(campaign),
    )


def _resolve_how_to_earn(twitch: TwitchClient, campaign: dict[str, Any]) -> str:
    """Prefer site «How to get these drops»; fall back to campaign description."""
    how = str(campaign.get("how_to_earn") or "").strip()
    slug = str(campaign.get("game_slug") or "").strip()
    if not slug:
        slug = (
            TwitchClient.twitchdrops_app_slug_for_game_id(
                str(campaign.get("game_id") or "")
            )
            or ""
        )
    if slug:
        try:
            from_site = twitch.fetch_twitchdrops_app_how_to(slug)
        except Exception:
            logger.warning("drops how-to fetch failed slug=%s", slug)
            from_site = ""
        if from_site:
            return from_site
    return how


def _format_stream_alert(
    lang: str,
    *,
    campaign: dict[str, Any],
    streams: list[dict[str, Any]],
    db: Database,
) -> str:
    from handlers.watch import _premium_channel_badge_html
    from translate import translate_text

    game = html.escape(str(campaign.get("game_name") or "").strip() or "—")
    name = html.escape(str(campaign.get("name") or t("drops_unnamed", lang)))
    lines = [
        t("drops_stream_alert_header", lang, game=game, name=name),
        "",
    ]
    for i, stream in enumerate(streams, start=1):
        login_raw = str(stream.get("user_login") or "").strip().lower()
        if not login_raw:
            continue
        login = html.escape(login_raw)
        display = html.escape(str(stream.get("user_name") or login_raw))
        title = html.escape(str(stream.get("title") or "—"))
        stream_game = html.escape(
            str(stream.get("game_name") or campaign.get("game_name") or "—")
        )
        viewers = int(stream.get("viewer_count") or 0)
        badge = _premium_channel_badge_html(lang, login=login_raw, db=db)
        tag = TwitchClient.matched_drops_tag(stream)
        drops_tag = f" ({html.escape(tag)})" if tag else ""
        lines.append(
            t(
                "drops_stream_alert_item",
                lang,
                n=i,
                display=display,
                login=login,
                title=title,
                game=stream_game,
                viewers=viewers,
                premium_badge=badge,
                drops_tag=drops_tag,
            )
        )
        lines.append("")
    how = str(campaign.get("how_to_earn") or "").strip()
    if how:
        how_tr = translate_text(how, target_lang=lang, source_lang="en")
        lines.append(t("drops_how_to_get", lang, text=html.escape(how_tr)))
    return "\n".join(lines).rstrip()


def _mark_streams_seen(
    db: Database,
    owner_id: int,
    sub_id: int,
    streams: list[dict[str, Any]],
    *,
    now_iso: str,
) -> None:
    for stream in streams:
        sid = str(stream.get("id") or "")
        if sid:
            db.mark_drop_stream_seen(
                owner_id, sub_id, sid, first_seen_at=now_iso
            )


async def send_drops_stream_alert(
    bot: Any,
    db: Database,
    twitch: TwitchClient,
    sub: Subscription,
    lang: str,
    *,
    campaign: dict[str, Any] | None = None,
    only_new: bool = False,
) -> int:
    """Send stream list alert. Returns number of streams included (0 = none sent)."""
    from handlers.notifications import (
        _category_watch_cooling_down,
        apply_category_watch_cooldown,
        category_watch_cooldown_minutes,
    )

    if sub is None or not is_drops_sub(sub) or not sub.enabled:
        return 0
    # Periodic job: respect per-sub cooldown. Immediate snapshot (only_new=False) skips.
    if only_new and _category_watch_cooling_down(sub):
        return 0
    game_id = (sub.drops_game_id or "").strip()
    if not game_id:
        return 0
    promo_logins = {
        str(x).strip().lower()
        for x in prem.list_promo_channel_logins(db)
        if str(x).strip()
    }
    try:
        streams = await asyncio.to_thread(
            twitch.get_streams_with_drops,
            game_id,
            first=40,
            limit=40 if only_new else 5,
            promo_logins=promo_logins,
        )
    except Exception:
        logger.exception("drops streams fetch failed game=%s", game_id)
        return 0
    if only_new:
        streams = [
            s
            for s in streams
            if str(s.get("id") or "")
            and not db.has_seen_drop_stream(sub.owner_id, sub.id, str(s.get("id")))
        ][:5]
    else:
        streams = streams[:5]
    if not streams:
        return 0
    camp = dict(
        campaign
        or {
            "name": sub.twitch_username,
            "game_name": sub.twitch_username,
            "game_id": game_id,
            "how_to_earn": "",
            "drops": [],
        }
    )
    if not str(camp.get("game_id") or "").strip():
        camp["game_id"] = game_id
    camp["how_to_earn"] = await asyncio.to_thread(_resolve_how_to_earn, twitch, camp)
    text = _format_stream_alert(lang, campaign=camp, streams=streams, db=db)
    now_iso = datetime.now(timezone.utc).isoformat()
    # Claim before send so overlapping ticks / deploy races cannot re-notify.
    if only_new:
        _mark_streams_seen(db, sub.owner_id, sub.id, streams, now_iso=now_iso)
        if category_watch_cooldown_minutes(sub) > 0:
            apply_category_watch_cooldown(db, sub)
    try:
        await bot.send_message(
            sub.chat_id,
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            message_thread_id=sub.thread_id,
        )
    except Exception:
        logger.exception(
            "drops stream alert failed owner=%s sub=%s", sub.owner_id, sub.id
        )
        return 0
    if not only_new:
        _mark_streams_seen(db, sub.owner_id, sub.id, streams, now_iso=now_iso)
        apply_category_watch_cooldown(db, sub)
    analytics.capture(
        sub.owner_id,
        "drops_stream_alert_sent",
        {
            "subscription_id": sub.id,
            "game_id": game_id,
            "stream_count": len(streams),
            "only_new": only_new,
        },
    )
    return len(streams)


def _digest_alert_keyboard(
    lang: str, campaign_id: str, *, drop_name: str = ""
) -> InlineKeyboardMarkup:
    cid = campaign_id[:48]
    name = (drop_name or "").strip() or t("drops_unnamed", lang)
    get_label = t("drops_get_alerts_btn", lang, name=name)[:64]
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    get_label,
                    callback_data=f"drops_get:{cid}",
                )
            ],
            [
                InlineKeyboardButton(
                    t("drops_digest_disable_btn", lang)[:64],
                    callback_data="drops_digest:off",
                )
            ],
        ]
    )


def _claim_alert_keyboard(lang: str, sub_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("drops_claim_pause_btn", lang),
                    callback_data=f"drops_claim:pause:{sub_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    t("drops_claim_delete_btn", lang),
                    callback_data=f"drops_claim:del:{sub_id}",
                )
            ],
        ]
    )


async def create_drops_game_subscription(
    bot: Any,
    db: Database,
    user_id: int,
    lang: str,
    *,
    game_id: str,
    game_name: str,
    campaign_name: str = "",
    twitch: TwitchClient | None = None,
    campaign: dict[str, Any] | None = None,
) -> tuple[Subscription | None, str]:
    """One-tap private-chat drops subscription. Returns (sub, status_key)."""
    from handlers.notifications import CATEGORY_WATCH_COOLDOWN_MINUTES

    game_id = (game_id or "").strip()
    if not game_id:
        return None, "drops_catalog_fetch_failed"
    if not await drops_entitled(bot, db, user_id):
        return None, "drops_need_premium"
    existing = [
        s
        for s in db.get_subscriptions_by_owner(user_id)
        if is_drops_sub(s) and (s.drops_game_id or "") == game_id
    ]
    if existing:
        game_display = (game_name or "").strip() or game_id
        drop_display = (campaign_name or "").strip()
        label = _drops_list_label(
            game_name=game_display,
            campaign_name=drop_display,
            game_id=game_id,
        )
        patch: dict[str, object] = {}
        if label and (existing[0].twitch_username or "") != label:
            patch["twitch_username"] = label
        if (existing[0].dest_type or "") != "dm":
            patch["dest_type"] = "dm"
        if patch:
            db.update_subscription(
                existing[0].id, user_id, mark_sync_edited=False, **patch
            )
            refreshed = db.get_subscription(existing[0].id, user_id)
            if refreshed is not None:
                return refreshed, "drops_already_subscribed"
        return existing[0], "drops_already_subscribed"
    if len(db.get_subscriptions_by_owner(user_id)) >= MAX_SUBSCRIPTIONS_PER_OWNER:
        return None, "sub_limit"
    game_display = (game_name or "").strip() or game_id
    drop_display = (campaign_name or "").strip() or game_display
    label = _drops_list_label(
        game_name=game_display, campaign_name=drop_display, game_id=game_id
    )
    login = re.sub(r"[^a-z0-9_]", "", game_display.lower())[:25] or f"g{game_id}"[:25]
    enabled = await prem.may_enable_subscription_async(
        bot, db, user_id, twitch_username=login
    )
    sub_id = db.add_subscription(
        owner_id=user_id,
        twitch_username=login,
        twitch_user_id=f"drops:{user_id}:{secrets.token_hex(4)}",
        message_template=t(
            "drops_default_template", lang, game=game_display, drop=drop_display
        ),
        dest_type="dm",
        chat_id=user_id,
        thread_id=None,
        disable_link_preview=True,
        enabled=enabled,
        notify_on_live=False,
        notify_on_end=False,
        notify_on_category_change=False,
        notify_on_drops=True,
        drops_game_id=game_id,
        suppress_repeat_minutes=CATEGORY_WATCH_COOLDOWN_MINUTES,
    )
    # Preserve casing / drop title for list UI (add_subscription lowercases login).
    db.update_subscription(
        sub_id, user_id, twitch_username=label, mark_sync_edited=False
    )
    sub = db.get_subscription(sub_id, user_id)
    analytics.capture(
        user_id,
        "drops_game_subscribed",
        {"subscription_id": sub_id, "game_id": game_id, "enabled": enabled},
    )
    if not enabled:
        return sub, "drops_subscribed_paused"
    if sub is not None and twitch is not None:
        camp = campaign or {
            "name": drop_display,
            "game_name": game_display,
            "game_id": game_id,
            "how_to_earn": "",
            "drops": [],
        }
        await send_drops_stream_alert(
            bot, db, twitch, sub, lang, campaign=camp, only_new=False
        )
    return sub, "drops_subscribed_ok"


async def on_drops_rebind(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Clear stored Drops OAuth and start device-code login again."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    user_id = query.from_user.id
    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    lang = _user_lang(db, user_id)
    try:
        db.delete_drops_auth(user_id)
    except Exception:
        logger.exception("drops rebind delete failed user=%s", user_id)
    await context.bot.send_message(user_id, t("drops_rebind_started", lang))
    await send_drops_oauth_prompt(
        context.bot,
        twitch,
        user_id,
        lang,
        application=context.application,
    )


def _ensure_drops_auth_row(db: Database, user_id: int) -> None:
    """Create a digest-capable drops_auth row without requiring OAuth."""
    if db.get_drops_auth(user_id) is not None:
        return
    db.upsert_drops_auth(
        user_id, twitch_user_id="", twitch_login="", refresh_token=""
    )


async def on_drops_digest_toggle(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()
    user_id = query.from_user.id
    db: Database = context.application.bot_data["db"]
    lang = _user_lang(db, user_id)
    _ensure_drops_auth_row(db, user_id)
    auth = db.get_drops_auth(user_id)
    new_state = not bool(auth and auth.digest_enabled)
    db.set_drops_digest_enabled(user_id, new_state)
    cands = (
        context.user_data.get("drops_catalog_candidates")
        or (context.application.bot_data.get("drops_catalog_by_user") or {}).get(
            user_id
        )
        or []
    )
    try:
        await query.edit_message_reply_markup(
            reply_markup=drops_catalog_keyboard(
                lang,
                cands,
                digest_enabled=new_state,
                show_rebind=not cands,
            )
        )
    except Exception:
        pass
    tip = (
        t("drops_digest_on", lang) if new_state else t("drops_digest_off", lang)
    )
    await context.bot.send_message(user_id, tip)


async def on_drops_digest_off(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Disable new-Drops digest from a digest alert button."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    user_id = query.from_user.id
    db: Database = context.application.bot_data["db"]
    lang = _user_lang(db, user_id)
    _ensure_drops_auth_row(db, user_id)
    db.set_drops_digest_enabled(user_id, False)
    try:
        markup = query.message.reply_markup if query.message else None
        rows: list[list[InlineKeyboardButton]] = []
        if markup:
            for row in markup.inline_keyboard:
                kept = [
                    b
                    for b in row
                    if (b.callback_data or "") != "drops_digest:off"
                ]
                if kept:
                    rows.append(kept)
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup(rows) if rows else None
        )
    except Exception:
        pass
    await context.bot.send_message(user_id, t("drops_digest_off", lang))


async def create_drops_from_campaign_payload(
    bot: Any,
    db: Database,
    user_id: int,
    lang: str,
    camp: dict[str, Any],
    *,
    twitch: TwitchClient | None = None,
) -> str:
    sub, key = await create_drops_game_subscription(
        bot,
        db,
        user_id,
        lang,
        game_id=str(camp.get("game_id") or ""),
        game_name=str(camp.get("game_name") or ""),
        campaign_name=str(camp.get("name") or ""),
        twitch=twitch,
        campaign=camp,
    )
    del sub
    from config import MAX_SUBSCRIPTIONS_PER_OWNER

    game = str(camp.get("game_name") or "")
    drop = str(camp.get("name") or game)
    return t(
        key,
        lang,
        game=game or drop,
        drop=drop or game,
        limit=MAX_SUBSCRIPTIONS_PER_OWNER,
    )


async def on_drops_get_alerts(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Digest CTA / catalog pick outside wizard: one-tap subscribe."""
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()
    user_id = query.from_user.id
    db: Database = context.application.bot_data["db"]
    lang = _user_lang(db, user_id)
    camp_id = (query.data.split(":", 1) + [""])[1]
    cands = (
        context.user_data.get("drops_catalog_candidates")
        or (context.application.bot_data.get("drops_catalog_by_user") or {}).get(
            user_id
        )
        or []
    )
    camp = next((c for c in cands if str(c.get("id") or "") == camp_id), None)
    if camp is None:
        camp = next(
            (c for c in cands if str(c.get("id") or "").startswith(camp_id)),
            None,
        )
    if camp is None:
        await context.bot.send_message(user_id, t("drops_catalog_fetch_failed", lang))
        return
    text = await create_drops_from_campaign_payload(
        context.bot,
        db,
        user_id,
        lang,
        camp,
        twitch=context.application.bot_data["twitch"],
    )
    await context.bot.send_message(user_id, text, reply_markup=_menu(lang, user_id))


async def on_drops_claim_action(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()
    user_id = query.from_user.id
    db: Database = context.application.bot_data["db"]
    lang = _user_lang(db, user_id)
    parts = query.data.split(":")
    if len(parts) != 3 or parts[0] != "drops_claim":
        return
    action = parts[1]
    try:
        sub_id = int(parts[2])
    except ValueError:
        return
    sub = db.get_subscription(sub_id, user_id)
    if sub is None or not is_drops_sub(sub):
        await context.bot.send_message(user_id, t("drops_subscribe_gone", lang))
        return
    if action == "pause":
        if sub.enabled:
            db.toggle_subscription(sub_id, user_id)
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(user_id, t("drops_claim_paused", lang))
        return
    if action == "del":
        db.delete_subscription(sub_id, user_id, to_cart=True)
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(user_id, t("drops_claim_deleted", lang))


async def check_drops(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Digest new campaigns, stream alerts for game subs, claim notifications."""
    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    now_iso = datetime.now(timezone.utc).isoformat()

    await _check_drops_digest(context, db, twitch, now_iso)
    await _check_drops_streams(context, db, twitch, now_iso)
    await _check_drops_claims(context, db, twitch, now_iso)


async def _check_drops_digest(
    context: ContextTypes.DEFAULT_TYPE,
    db: Database,
    twitch: TwitchClient,
    now_iso: str,
) -> None:
    owners = db.list_drops_digest_owner_ids()
    if not owners:
        return
    try:
        campaigns = await asyncio.to_thread(
            twitch.fetch_twitchdrops_app_campaigns, sort="new"
        )
    except Exception:
        logger.exception("drops digest fetch failed")
        return
    active = [
        c for c in campaigns if _campaign_active(c) and str(c.get("id") or "")
    ]
    store = context.application.bot_data.setdefault("drops_catalog_by_user", {})
    for owner_id in owners:
        if not await drops_entitled(context.bot, db, owner_id):
            continue
        lang = _user_lang(db, owner_id)
        # First poll: seed seen without spam.
        any_seen = any(
            db.has_seen_drop_campaign(owner_id, str(c["id"]), _DIGEST_SEEN_SUB_ID)
            for c in active
        )
        if not any_seen and active:
            for c in active:
                db.mark_drop_campaign_seen(
                    owner_id,
                    str(c["id"]),
                    _DIGEST_SEEN_SUB_ID,
                    first_seen_at=now_iso,
                )
            continue
        for campaign in active:
            cid = str(campaign["id"])
            if db.has_seen_drop_campaign(owner_id, cid, _DIGEST_SEEN_SUB_ID):
                continue
            db.mark_drop_campaign_seen(
                owner_id, cid, _DIGEST_SEEN_SUB_ID, first_seen_at=now_iso
            )
            compact_row = {
                "id": cid,
                "name": str(campaign.get("name") or ""),
                "game_id": str(campaign.get("game_id") or ""),
                "game_name": str(campaign.get("game_name") or ""),
                "game_slug": str(campaign.get("game_slug") or ""),
                "claimed": bool(campaign.get("claimed")),
                "how_to_earn": str(campaign.get("how_to_earn") or ""),
                "starts_at": str(campaign.get("starts_at") or ""),
                "ends_at": str(campaign.get("ends_at") or ""),
                "drops": campaign.get("drops") or [],
            }
            prev = store.get(owner_id) or []
            store[owner_id] = [compact_row] + [
                x for x in prev if str(x.get("id")) != cid
            ]
            text = _format_digest_alert(lang, campaign)
            try:
                await context.bot.send_message(
                    owner_id,
                    text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_markup=_digest_alert_keyboard(
                        lang,
                        cid,
                        drop_name=str(campaign.get("name") or ""),
                    ),
                )
                analytics.capture(
                    owner_id, "drops_digest_sent", {"campaign_id": cid}
                )
            except Exception:
                logger.exception("drops digest send failed owner=%s", owner_id)


async def _check_drops_streams(
    context: ContextTypes.DEFAULT_TYPE,
    db: Database,
    twitch: TwitchClient,
    now_iso: str,
) -> None:
    del now_iso
    subs = db.get_enabled_drops_subscriptions()
    if not subs:
        return
    by_owner: dict[int, list[Subscription]] = {}
    for sub in subs:
        if not is_drops_sub(sub):
            continue
        by_owner.setdefault(sub.owner_id, []).append(sub)

    campaigns_by_game: dict[str, list[dict[str, Any]]] = {}
    try:
        all_c = await asyncio.to_thread(
            twitch.fetch_twitchdrops_app_campaigns, sort="new"
        )
        for c in all_c:
            gid = str(c.get("game_id") or "")
            if gid and _campaign_active(c):
                campaigns_by_game.setdefault(gid, []).append(c)
    except Exception:
        logger.exception("drops stream campaigns fetch failed")

    for owner_id, owner_subs in by_owner.items():
        if not await drops_entitled(context.bot, db, owner_id):
            continue
        lang = _user_lang(db, owner_id)
        for sub in owner_subs:
            game_id = (sub.drops_game_id or "").strip()
            if not game_id:
                continue
            camps = campaigns_by_game.get(game_id) or None
            campaign = camps[0] if camps else None
            await send_drops_stream_alert(
                context.bot,
                db,
                twitch,
                sub,
                lang,
                campaign=campaign,
                only_new=True,
            )


async def _check_drops_claims(
    context: ContextTypes.DEFAULT_TYPE,
    db: Database,
    twitch: TwitchClient,
    now_iso: str,
) -> None:
    subs = db.get_enabled_drops_subscriptions()
    if not subs:
        return
    by_owner: dict[int, list[Subscription]] = {}
    for sub in subs:
        if is_drops_sub(sub):
            by_owner.setdefault(sub.owner_id, []).append(sub)

    for owner_id, owner_subs in by_owner.items():
        if not await drops_entitled(context.bot, db, owner_id):
            continue
        access = await asyncio.to_thread(_access_token_for_owner, db, twitch, owner_id)
        if not access:
            continue
        try:
            claims = await asyncio.to_thread(
                twitch.get_inventory_claimed_drops,
                access,
                device_id=drops_device_id_for(owner_id),
            )
        except Exception:
            logger.exception("drops claims fetch failed owner=%s", owner_id)
            continue
        game_ids = {
            (s.drops_game_id or "").strip()
            for s in owner_subs
            if (s.drops_game_id or "").strip()
        }
        lang = _user_lang(db, owner_id)
        claimed_items = [
            (drop_id, info)
            for drop_id, info in claims.items()
            if info.get("is_claimed")
            and (
                not str(info.get("game_id") or "")
                or str(info.get("game_id") or "") in game_ids
            )
        ]
        any_seen = any(
            db.has_seen_drop_claim(owner_id, did) for did, _ in claimed_items
        )
        if not any_seen and claimed_items:
            for drop_id, _info in claimed_items:
                db.mark_drop_claim_seen(owner_id, drop_id, first_seen_at=now_iso)
            continue
        for drop_id, info in claimed_items:
            if db.has_seen_drop_claim(owner_id, drop_id):
                continue
            db.mark_drop_claim_seen(owner_id, drop_id, first_seen_at=now_iso)
            game_id = str(info.get("game_id") or "")
            matching = [
                s
                for s in owner_subs
                if not game_id or (s.drops_game_id or "") == game_id
            ]
            if not matching:
                continue
            sub = matching[0]
            name = html.escape(
                str(info.get("name") or t("drops_unnamed", lang))
            )
            text = t("drops_claim_alert_body", lang, name=name)
            try:
                await context.bot.send_message(
                    sub.chat_id,
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=_claim_alert_keyboard(lang, sub.id),
                    message_thread_id=sub.thread_id,
                )
                analytics.capture(
                    owner_id,
                    "drops_claim_alert_sent",
                    {"subscription_id": sub.id, "drop_id": drop_id},
                )
            except Exception:
                logger.exception(
                    "drops claim alert failed owner=%s drop=%s", owner_id, drop_id
                )

