"""Twitch Drops alerts: twitchdrops.app catalog, digest, and stream jobs."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import secrets
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

_DROPS_CATALOG_PAGE_SIZE = 8
_DIGEST_SEEN_SUB_ID = 0
_DIGEST_MIN_INTERVAL_SEC = 3600
_DIGEST_LAST_FLUSH_KEY = "drops_digest_last_flush"
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


def _drops_list_label(*, game_name: str, campaign_name: str, game_id: str) -> str:
    game = (game_name or "").strip() or game_id
    drop = (campaign_name or "").strip()
    if game and drop and drop.casefold() != game.casefold():
        return f"{game} — {drop}"[:64]
    return (game or drop or game_id)[:64]


def _campaign_active(campaign: dict[str, Any]) -> bool:
    status = str(campaign.get("status") or "").upper()
    return status in _ACTIVE_CAMPAIGN_STATUSES or status == "ACTIVE"


def list_active_drop_campaigns(
    db: Database,
    twitch: TwitchClient,
    owner_id: int,
    **_kwargs: Any,
) -> list[dict[str, Any]] | None:
    """Return ACTIVE campaigns from twitchdrops.app, or None on API failure."""
    del db  # signature kept for callers; catalog needs no auth row
    try:
        campaigns = twitch.fetch_twitchdrops_app_campaigns(sort="new")
    except Exception:
        logger.exception("drops catalog fetch failed owner=%s", owner_id)
        return None
    active = [
        c for c in campaigns if _campaign_active(c) and str(c.get("id") or "")
    ]
    return active


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
    **_kwargs: Any,
) -> list[dict[str, Any]]:
    """Fetch and send the available-Drops list with digest checkbox."""
    campaigns = await asyncio.to_thread(
        list_active_drop_campaigns,
        db,
        twitch,
        user_id,
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
                drops_catalog_keyboard(lang, [], digest_enabled=digest_on),
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
                drops_catalog_keyboard(lang, [], digest_enabled=digest_on),
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
        user_data["drops_catalog_page"] = 0
    await bot.send_message(
        user_id,
        t("drops_catalog_prompt", lang),
        reply_markup=_with_extra_markup(
            drops_catalog_keyboard(
                lang,
                compact,
                digest_enabled=digest_on,
                page=0,
                page_size=_DROPS_CATALOG_PAGE_SIZE,
            ),
            reply_markup_extra,
        ),
    )
    return compact


def _format_dates(campaign: dict[str, Any]) -> str:
    starts = html.escape(str(campaign.get("starts_at") or "—"))
    ends = html.escape(str(campaign.get("ends_at") or "—"))
    return f"{starts} — {ends}"


def _format_digest_alert(lang: str, campaigns: list[dict[str, Any]]) -> str:
    lines = [
        t("drops_digest_alert_header", lang, n=len(campaigns)),
        "",
    ]
    for campaign in campaigns:
        name = html.escape(str(campaign.get("name") or t("drops_unnamed", lang)))
        game = html.escape(str(campaign.get("game_name") or "").strip() or "—")
        lines.append(
            t(
                "drops_digest_alert_item",
                lang,
                name=name,
                game=game,
                dates=_format_dates(campaign),
            )
        )
        lines.append("")
    return "\n".join(lines).rstrip()


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


def _digest_alert_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("drops_digest_open_btn", lang)[:64],
                    callback_data="drops_digest:open",
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
    page = int(context.user_data.get("drops_catalog_page") or 0)
    try:
        await query.edit_message_reply_markup(
            reply_markup=drops_catalog_keyboard(
                lang,
                cands,
                digest_enabled=new_state,
                page=page,
                page_size=_DROPS_CATALOG_PAGE_SIZE,
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


async def check_drops(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Digest new campaigns and stream alerts for game subs."""
    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    now_iso = datetime.now(timezone.utc).isoformat()

    await _check_drops_digest(context, db, twitch, now_iso)
    await _check_drops_streams(context, db, twitch, now_iso)


async def _check_drops_digest(
    context: ContextTypes.DEFAULT_TYPE,
    db: Database,
    twitch: TwitchClient,
    now_iso: str,
) -> None:
    import time

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
    last_flush = context.application.bot_data.setdefault(_DIGEST_LAST_FLUSH_KEY, {})
    now_ts = time.time()
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
            last_flush[owner_id] = now_ts
            continue
        new_camps = [
            c
            for c in active
            if not db.has_seen_drop_campaign(
                owner_id, str(c["id"]), _DIGEST_SEEN_SUB_ID
            )
        ]
        if not new_camps:
            continue
        if now_ts - float(last_flush.get(owner_id) or 0) < _DIGEST_MIN_INTERVAL_SEC:
            continue
        text = _format_digest_alert(lang, new_camps)
        try:
            await context.bot.send_message(
                owner_id,
                text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=_digest_alert_keyboard(lang),
            )
        except Exception:
            logger.exception("drops digest send failed owner=%s", owner_id)
            continue
        for campaign in new_camps:
            cid = str(campaign["id"])
            db.mark_drop_campaign_seen(
                owner_id, cid, _DIGEST_SEEN_SUB_ID, first_seen_at=now_iso
            )
            compact_row = {
                "id": cid,
                "name": str(campaign.get("name") or ""),
                "game_id": str(campaign.get("game_id") or ""),
                "game_name": str(campaign.get("game_name") or ""),
                "game_slug": str(campaign.get("game_slug") or ""),
                "how_to_earn": str(campaign.get("how_to_earn") or ""),
                "starts_at": str(campaign.get("starts_at") or ""),
                "ends_at": str(campaign.get("ends_at") or ""),
                "drops": campaign.get("drops") or [],
            }
            prev = store.get(owner_id) or []
            store[owner_id] = [compact_row] + [
                x for x in prev if str(x.get("id")) != cid
            ]
        last_flush[owner_id] = now_ts
        analytics.capture(
            owner_id,
            "drops_digest_sent",
            {"campaign_count": len(new_camps)},
        )


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
