"""Game giveaways digest (GamerPower + ITAD) — beta giveaways-alerts."""
from __future__ import annotations

import asyncio
import html
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import ContextTypes, ConversationHandler

import beta as beta_features
from bot_helpers import _menu, reply_chat_id
from db import Database
from giveaway_sources import (
    PLATFORM_IDS,
    STORE_IDS,
    GiveawayOffer,
    attribution_html,
    claim_url_or_search,
    fetch_active_giveaways,
    filter_giveaways,
)
from i18n import DEFAULT_LOCALE, t
from igdb_dumps import igdb_image_url

logger = logging.getLogger(__name__)

GIVEAWAYS_BETA_ID = "giveaways-alerts"
_PAGE_SIZE = 5
_DIGEST_MIN_INTERVAL_SEC = 20 * 3600


def giveaways_feature_available(db: Database, user_id: int) -> bool:
    return beta_features.is_enabled(db, user_id, GIVEAWAYS_BETA_ID)


def _user_lang(db: Database, user_id: int) -> str:
    return db.get_user_locale(user_id) or DEFAULT_LOCALE


def _prefs_or_empty(db: Database, owner_id: int):
    from db.models import GiveawaysPrefs

    p = db.get_giveaways_prefs(owner_id)
    if p:
        return p
    return GiveawaysPrefs(
        owner_id=owner_id,
        stores=[],
        platforms=[],
        digest_enabled=False,
        first_digest_sent=False,
        last_digest_at=0,
    )


def _store_label(lang: str, store_id: str) -> str:
    key = f"giveaway_store_{store_id}"
    label = t(key, lang)
    return label if label != key else store_id


def _platform_label(lang: str, platform_id: str) -> str:
    key = f"giveaway_platform_{platform_id}"
    label = t(key, lang)
    return label if label != key else platform_id


def giveaways_hub_keyboard(
    lang: str,
    *,
    digest_enabled: bool,
    show_fresh: bool,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                t("giveaways_btn_stores", lang),
                callback_data="gv:stores",
            )
        ],
        [
            InlineKeyboardButton(
                t("giveaways_btn_platforms", lang),
                callback_data="gv:platforms",
            )
        ],
    ]
    if digest_enabled:
        rows.append(
            [
                InlineKeyboardButton(
                    t("giveaways_btn_disable", lang),
                    callback_data="gv:disable",
                )
            ]
        )
    if show_fresh:
        rows.append(
            [
                InlineKeyboardButton(
                    t("giveaways_btn_fresh", lang),
                    callback_data="gv:fresh:0",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                t("btn_back", lang) if t("btn_back", lang) != "btn_back" else "« Back",
                callback_data="gv:close",
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def _checkbox_list_keyboard(
    lang: str,
    *,
    kind: str,
    ids: list[str],
    selected: set[str],
    label_fn,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for sid in ids:
        mark = "✅ " if sid in selected else "⬜️ "
        rows.append(
            [
                InlineKeyboardButton(
                    f"{mark}{label_fn(lang, sid)}",
                    callback_data=f"gv:{kind}:toggle:{sid}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                t("giveaways_select_all", lang),
                callback_data=f"gv:{kind}:all",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                t("giveaways_done", lang),
                callback_data=f"gv:{kind}:done",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                t("btn_back", lang) if t("btn_back", lang) != "btn_back" else "« Back",
                callback_data="gv:hub",
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def _page_keyboard(lang: str, page: int, total_pages: int) -> InlineKeyboardMarkup:
    buttons: list[InlineKeyboardButton] = []
    if page > 0:
        buttons.append(
            InlineKeyboardButton("‹", callback_data=f"gv:fresh:{page - 1}")
        )
    buttons.append(
        InlineKeyboardButton(
            f"{page + 1}/{total_pages}",
            callback_data="gv:noop",
        )
    )
    if page + 1 < total_pages:
        buttons.append(
            InlineKeyboardButton("›", callback_data=f"gv:fresh:{page + 1}")
        )
    rows = [buttons] if buttons else []
    rows.append(
        [
            InlineKeyboardButton(
                t("giveaways_back_hub", lang),
                callback_data="gv:hub",
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def _streams_keyboard(lang: str, igdb_game_id: int | None) -> InlineKeyboardMarkup | None:
    if not igdb_game_id:
        return None
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("giveaways_find_streams", lang),
                    callback_data=f"gv:streams:{igdb_game_id}",
                )
            ]
        ]
    )


@dataclass
class _Enriched:
    offer: GiveawayOffer
    igdb_id: int | None
    name: str
    year: str
    publisher: str
    developer: str
    summary: str
    cover_url: str


def _clean_title_for_search(title: str) -> str:
    import re

    s = re.sub(r"\s*\(.*?\)\s*", " ", title or "")
    s = re.sub(
        r"\b(giveaway|key|free|steam|epic|gog)\b",
        " ",
        s,
        flags=re.I,
    )
    return " ".join(s.split()).strip() or (title or "").strip()


def _year_from_unix(ts: Any) -> str:
    try:
        n = int(ts or 0)
        if n <= 0:
            return ""
        return str(datetime.fromtimestamp(n, tz=timezone.utc).year)
    except Exception:
        return ""


def _enrich_offer(db: Database, offer: GiveawayOffer, lang: str) -> _Enriched:
    from twitch import localize_igdb_summary

    q = _clean_title_for_search(offer.title)
    igdb_id: int | None = None
    name = offer.title
    year = ""
    publisher = ""
    developer = ""
    summary = ""
    cover_url = offer.image_url
    try:
        hits = db.igdb_search_games_by_name(q, limit=5) if q else []
    except Exception:
        logger.exception("giveaway igdb search failed title=%s", offer.title[:80])
        hits = []
    if hits:
        hit = hits[0]
        igdb_id = int(hit.get("id") or 0) or None
        name = str(hit.get("name") or offer.title).strip() or offer.title
        year = _year_from_unix(hit.get("first_release_date"))
        if igdb_id:
            publisher, developer = db.igdb_publisher_developer_names(igdb_id)
            game = db.igdb_game_by_id(igdb_id) or {}
            raw_sum = str(game.get("summary") or "").strip()
            summary = localize_igdb_summary(raw_sum, lang) if raw_sum else ""
            mid = db.igdb_cover_image_id_for_game(igdb_id)
            if mid:
                cover_url = igdb_image_url(mid)
    if not summary:
        summary = (offer.description or "")[:800]
    return _Enriched(
        offer=offer,
        igdb_id=igdb_id,
        name=name,
        year=year,
        publisher=publisher,
        developer=developer,
        summary=summary,
        cover_url=cover_url,
    )


def _format_dates(offer: GiveawayOffer, lang: str) -> str:
    start = (offer.start_at or "").strip()
    end = (offer.end_at or "").strip()
    if end.upper() in ("N/A", "NONE", ""):
        end = t("giveaways_date_open", lang)
    if start and end:
        return f"{start} — {end}"
    return start or end or "—"


def _build_card_html(item: _Enriched, lang: str) -> str:
    year_bit = f" ({html.escape(item.year)})" if item.year else ""
    title = f"<b>{html.escape(item.name)}{year_bit}</b>"
    pub = html.escape(item.publisher) if item.publisher else "—"
    dev = html.escape(item.developer) if item.developer else "—"
    dates = html.escape(_format_dates(item.offer, lang))
    claim = claim_url_or_search(item.offer)
    plat_parts: list[str] = []
    for pid in item.offer.platform_ids:
        label = html.escape(_platform_label(lang, pid))
        plat_parts.append(
            f'<a href="{html.escape(claim, quote=True)}">{label}</a>'
        )
    plats = ", ".join(plat_parts) if plat_parts else "—"
    desc = html.escape(item.summary[:800]) if item.summary else ""
    lines = [
        title,
        f"{t('giveaways_publisher', lang)} {pub}",
        f"{t('giveaways_developer', lang)} {dev}",
        f"{t('giveaways_dates', lang)} {dates}",
        f"{t('giveaways_platforms', lang)} {plats}",
    ]
    if desc:
        lines.append("")
        lines.append(desc)
    return "\n".join(lines)


async def _send_page(
    bot: Any,
    chat_id: int,
    *,
    db: Database,
    items: list[_Enriched],
    lang: str,
    page: int,
    total_pages: int,
    header: str,
    used_gp: bool,
    used_itad: bool,
) -> None:
    attr = attribution_html(used_gp=used_gp, used_itad=used_itad)
    igdb_attr = '<a href="https://www.igdb.com">IGDB.com</a>'
    footer = " · ".join(p for p in (attr, igdb_attr) if p)

    covers = [i.cover_url for i in items if i.cover_url][:10]
    if len(covers) > 1:
        media = [InputMediaPhoto(media=u) for u in covers]
        try:
            await bot.send_media_group(chat_id, media=media)
        except Exception:
            logger.info("giveaways media_group failed chat=%s", chat_id)

    for idx, item in enumerate(items):
        body = _build_card_html(item, lang)
        if idx == 0 and header:
            body = f"{header}\n\n{body}"
        if idx == len(items) - 1 and footer:
            body = f"{body}\n\n{footer}"
        if len(body) > 1024:
            body = body[:1020] + "…"
        markup = _streams_keyboard(lang, item.igdb_id)
        if item.cover_url and len(covers) <= 1:
            try:
                await bot.send_photo(
                    chat_id,
                    photo=item.cover_url,
                    caption=body,
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                )
                continue
            except BadRequest:
                pass
        await bot.send_message(
            chat_id,
            body,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            disable_web_page_preview=True,
        )

    if total_pages > 1:
        await bot.send_message(
            chat_id,
            t("giveaways_page_nav", lang, page=page + 1, total=total_pages),
            reply_markup=_page_keyboard(lang, page, total_pages),
        )


async def open_giveaways_hub(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    user = update.effective_user
    if not user:
        return ConversationHandler.END
    db: Database = context.application.bot_data["db"]
    lang = _user_lang(db, user.id)
    if not giveaways_feature_available(db, user.id):
        text = t("giveaways_beta_required", lang)
        if query:
            await query.answer()
            await query.edit_message_text(text)
        else:
            await update.effective_message.reply_text(text)
        return ConversationHandler.END
    prefs = _prefs_or_empty(db, user.id)
    show_fresh = bool(
        prefs.stores and prefs.platforms and prefs.first_digest_sent
    )
    text = t(
        "giveaways_hub",
        lang,
        stores=len(prefs.stores),
        platforms=len(prefs.platforms),
        status=(
            t("giveaways_status_on", lang)
            if prefs.digest_enabled
            else t("giveaways_status_off", lang)
        ),
    )
    markup = giveaways_hub_keyboard(
        lang,
        digest_enabled=prefs.digest_enabled,
        show_fresh=show_fresh,
    )
    if query:
        await query.answer()
        try:
            await query.edit_message_text(
                text, reply_markup=markup, parse_mode=ParseMode.HTML
            )
        except BadRequest:
            await context.bot.send_message(
                reply_chat_id(update),
                text,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
            )
    else:
        await update.effective_message.reply_text(
            text, reply_markup=markup, parse_mode=ParseMode.HTML
        )
    return ConversationHandler.END


async def _maybe_activate_and_send_first(
    context: ContextTypes.DEFAULT_TYPE,
    db: Database,
    user_id: int,
    lang: str,
    chat_id: int,
) -> None:
    prefs = _prefs_or_empty(db, user_id)
    if not prefs.stores or not prefs.platforms:
        return
    if prefs.digest_enabled and prefs.first_digest_sent:
        return
    db.upsert_giveaways_prefs(
        user_id,
        stores=prefs.stores,
        platforms=prefs.platforms,
        digest_enabled=True,
    )
    from handlers.background_jobs import sync_optional_jobs

    sync_optional_jobs(context.application.job_queue, db)
    await _send_matching_list(
        context.bot,
        chat_id,
        db=db,
        user_id=user_id,
        lang=lang,
        page=0,
        only_new=False,
        mark_seen=True,
        header=t("giveaways_first_digest_header", lang),
        set_first_sent=True,
    )


async def _send_matching_list(
    bot: Any,
    chat_id: int,
    *,
    db: Database,
    user_id: int,
    lang: str,
    page: int,
    only_new: bool,
    mark_seen: bool,
    header: str,
    set_first_sent: bool = False,
) -> int:
    prefs = _prefs_or_empty(db, user_id)
    stores = set(prefs.stores)
    platforms = set(prefs.platforms)
    offers = filter_giveaways(
        fetch_active_giveaways(),
        stores=stores,
        platforms=platforms,
    )
    if only_new:
        offers = [
            o
            for o in offers
            if not db.has_seen_giveaway(user_id, o.source, o.external_id)
        ]
    if not offers:
        await bot.send_message(
            chat_id,
            t("giveaways_empty", lang),
            reply_markup=_menu(lang, user_id),
        )
        if set_first_sent:
            db.upsert_giveaways_prefs(
                user_id,
                stores=prefs.stores,
                platforms=prefs.platforms,
                digest_enabled=True,
                first_digest_sent=True,
                last_digest_at=int(time.time()),
            )
        return 0

    total_pages = max(1, (len(offers) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = offers[page * _PAGE_SIZE : (page + 1) * _PAGE_SIZE]
    items = [await asyncio.to_thread(_enrich_offer, db, o, lang) for o in chunk]
    used_gp = any(i.offer.source == "gamerpower" for i in items)
    used_itad = any(i.offer.source == "itad" for i in items)
    # Attribution for whole catalog sources available
    if any(True for _ in fetch_active_giveaways()):
        # already cached; mark sources from full filtered set for footer honesty
        used_gp = used_gp or any(o.source == "gamerpower" for o in offers)
        used_itad = used_itad or any(o.source == "itad" for o in offers)

    await _send_page(
        bot,
        chat_id,
        db=db,
        items=items,
        lang=lang,
        page=page,
        total_pages=total_pages,
        header=header,
        used_gp=used_gp,
        used_itad=used_itad,
    )
    if mark_seen:
        now = int(time.time())
        for o in offers if set_first_sent else chunk:
            db.mark_giveaway_seen(user_id, o.source, o.external_id, seen_at=now)
    if set_first_sent:
        db.upsert_giveaways_prefs(
            user_id,
            stores=prefs.stores,
            platforms=prefs.platforms,
            digest_enabled=True,
            first_digest_sent=True,
            last_digest_at=int(time.time()),
        )
    return len(offers)


async def on_giveaways_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    user_id = query.from_user.id
    db: Database = context.application.bot_data["db"]
    lang = _user_lang(db, user_id)
    data = query.data
    chat_id = reply_chat_id(update)

    if not giveaways_feature_available(db, user_id) and data != "gv:close":
        await query.answer()
        await query.edit_message_text(t("giveaways_beta_required", lang))
        return

    if data == "gv:noop":
        await query.answer()
        return

    if data in ("gv:close",):
        await query.answer()
        try:
            await query.edit_message_text(t("giveaways_closed", lang))
        except BadRequest:
            pass
        await context.bot.send_message(
            chat_id, "✓", reply_markup=_menu(lang, user_id)
        )
        return

    if data == "gv:hub":
        await open_giveaways_hub(update, context)
        return

    if data == "gv:disable":
        await query.answer()
        prefs = _prefs_or_empty(db, user_id)
        db.upsert_giveaways_prefs(
            user_id,
            stores=prefs.stores,
            platforms=prefs.platforms,
            digest_enabled=False,
        )
        from handlers.background_jobs import sync_optional_jobs

        sync_optional_jobs(context.application.job_queue, db)
        await query.edit_message_text(t("giveaways_disabled", lang))
        await open_giveaways_hub(update, context)
        return

    if data == "gv:stores":
        await query.answer()
        prefs = _prefs_or_empty(db, user_id)
        selected = set(prefs.stores)
        await query.edit_message_text(
            t("giveaways_pick_stores", lang),
            reply_markup=_checkbox_list_keyboard(
                lang,
                kind="stores",
                ids=STORE_IDS,
                selected=selected,
                label_fn=_store_label,
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "gv:platforms":
        await query.answer()
        prefs = _prefs_or_empty(db, user_id)
        selected = set(prefs.platforms)
        await query.edit_message_text(
            t("giveaways_pick_platforms", lang),
            reply_markup=_checkbox_list_keyboard(
                lang,
                kind="platforms",
                ids=PLATFORM_IDS,
                selected=selected,
                label_fn=_platform_label,
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    if data.startswith("gv:stores:") or data.startswith("gv:platforms:"):
        await query.answer()
        kind = "stores" if data.startswith("gv:stores:") else "platforms"
        action = data.split(":")[2]
        prefs = _prefs_or_empty(db, user_id)
        selected = set(prefs.stores if kind == "stores" else prefs.platforms)
        ids = STORE_IDS if kind == "stores" else PLATFORM_IDS
        label_fn = _store_label if kind == "stores" else _platform_label

        if action == "toggle":
            sid = data.split(":")[-1]
            if sid in selected:
                selected.discard(sid)
            else:
                selected.add(sid)
        elif action == "all":
            selected = set(ids)
        elif action == "done":
            stores = list(selected) if kind == "stores" else prefs.stores
            platforms = list(selected) if kind == "platforms" else prefs.platforms
            if kind == "stores":
                stores = [s for s in STORE_IDS if s in selected]
            else:
                platforms = [p for p in PLATFORM_IDS if p in selected]
            db.upsert_giveaways_prefs(
                user_id,
                stores=stores,
                platforms=platforms,
                digest_enabled=prefs.digest_enabled,
                first_digest_sent=prefs.first_digest_sent,
                last_digest_at=prefs.last_digest_at,
            )
            await open_giveaways_hub(update, context)
            await _maybe_activate_and_send_first(
                context, db, user_id, lang, chat_id
            )
            return

        if kind == "stores":
            db.upsert_giveaways_prefs(
                user_id,
                stores=[s for s in STORE_IDS if s in selected],
                platforms=prefs.platforms,
                digest_enabled=prefs.digest_enabled,
                first_digest_sent=prefs.first_digest_sent,
                last_digest_at=prefs.last_digest_at,
            )
        else:
            db.upsert_giveaways_prefs(
                user_id,
                stores=prefs.stores,
                platforms=[p for p in PLATFORM_IDS if p in selected],
                digest_enabled=prefs.digest_enabled,
                first_digest_sent=prefs.first_digest_sent,
                last_digest_at=prefs.last_digest_at,
            )
        prompt = (
            t("giveaways_pick_stores", lang)
            if kind == "stores"
            else t("giveaways_pick_platforms", lang)
        )
        await query.edit_message_reply_markup(
            reply_markup=_checkbox_list_keyboard(
                lang,
                kind=kind,
                ids=ids,
                selected=selected,
                label_fn=label_fn,
            )
        )
        # keep prompt text stable
        try:
            await query.edit_message_text(
                prompt,
                reply_markup=_checkbox_list_keyboard(
                    lang,
                    kind=kind,
                    ids=ids,
                    selected=selected,
                    label_fn=label_fn,
                ),
                parse_mode=ParseMode.HTML,
            )
        except BadRequest:
            pass
        return

    if data.startswith("gv:fresh:"):
        await query.answer()
        prefs = _prefs_or_empty(db, user_id)
        if not (prefs.stores and prefs.platforms and prefs.first_digest_sent):
            await query.edit_message_text(t("giveaways_fresh_locked", lang))
            return
        try:
            page = int(data.split(":")[-1])
        except ValueError:
            page = 0
        await context.bot.send_message(
            chat_id, t("giveaways_loading", lang)
        )
        await _send_matching_list(
            context.bot,
            chat_id,
            db=db,
            user_id=user_id,
            lang=lang,
            page=page,
            only_new=False,
            mark_seen=False,
            header=t("giveaways_fresh_header", lang),
        )
        return

    if data.startswith("gv:streams:"):
        await on_giveaways_find_streams(update, context)
        return

    await query.answer()


async def on_giveaways_find_streams(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()
    user_id = query.from_user.id
    db: Database = context.application.bot_data["db"]
    lang = _user_lang(db, user_id)
    try:
        game_id = int(query.data.split(":")[-1])
    except (TypeError, ValueError):
        return
    game = db.igdb_game_by_id(game_id) or {}
    game_name = str(game.get("name") or "").strip() or f"#{game_id}"
    twitch = context.application.bot_data.get("twitch")
    uids = db.igdb_twitch_uids_for_game(game_id)
    cat_id = uids[0] if uids else ""
    cat_name = game_name
    if not cat_id and twitch is not None:
        from search_normalize import normalize_search_query

        try:
            found = await asyncio.to_thread(
                twitch.search_categories,
                normalize_search_query(game_name) or game_name,
                first=10,
            )
        except Exception:
            logger.exception("giveaways find-streams Helix failed game=%s", game_id)
            found = []
        want = game_name.casefold()
        exact = next(
            (c for c in found if str(c.get("name") or "").casefold() == want),
            None,
        )
        pick = exact or (found[0] if found else None)
        if pick:
            cat_id = str(pick.get("id") or "").strip()
            cat_name = str(pick.get("name") or game_name).strip() or game_name
    if not cat_id:
        await context.bot.send_message(
            reply_chat_id(update),
            t("giveaways_find_streams_none", lang, game=html.escape(game_name)),
            parse_mode=ParseMode.HTML,
            reply_markup=_menu(lang, user_id),
        )
        return
    from db.models import WatchPrefs
    from handlers.watch import _send_watch_suggestions

    prefs = WatchPrefs(
        categories=[{"id": cat_id, "name": cat_name}],
        min_viewers=0,
        max_viewers=None,
        language=None,
        tags=[],
        exclude_mature=True,
    )
    await _send_watch_suggestions(
        bot=context.bot,
        chat_id=reply_chat_id(update),
        user_id=user_id,
        context=context,
        prefs=prefs,
    )


async def check_giveaways_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    bot = context.bot
    owner_ids = db.list_giveaways_digest_owner_ids()
    if not owner_ids:
        return
    now = int(time.time())
    catalog = await asyncio.to_thread(fetch_active_giveaways)
    for owner_id in owner_ids:
        if not giveaways_feature_available(db, owner_id):
            continue
        prefs = _prefs_or_empty(db, owner_id)
        if not prefs.stores or not prefs.platforms or not prefs.digest_enabled:
            continue
        if prefs.last_digest_at and (now - prefs.last_digest_at) < _DIGEST_MIN_INTERVAL_SEC:
            continue
        lang = _user_lang(db, owner_id)
        matched = filter_giveaways(
            catalog,
            stores=set(prefs.stores),
            platforms=set(prefs.platforms),
        )
        new_offers = [
            o
            for o in matched
            if not db.has_seen_giveaway(owner_id, o.source, o.external_id)
        ]
        if not new_offers:
            db.upsert_giveaways_prefs(
                owner_id,
                stores=prefs.stores,
                platforms=prefs.platforms,
                digest_enabled=True,
                first_digest_sent=prefs.first_digest_sent or True,
                last_digest_at=now,
            )
            continue
        try:
            # send first page of new items; mark all new as seen
            total_pages = max(1, (len(new_offers) + _PAGE_SIZE - 1) // _PAGE_SIZE)
            chunk = new_offers[:_PAGE_SIZE]
            items = [
                await asyncio.to_thread(_enrich_offer, db, o, lang) for o in chunk
            ]
            used_gp = any(o.source == "gamerpower" for o in new_offers)
            used_itad = any(o.source == "itad" for o in new_offers)
            await _send_page(
                bot,
                owner_id,
                db=db,
                items=items,
                lang=lang,
                page=0,
                total_pages=total_pages,
                header=t("giveaways_daily_header", lang, count=len(new_offers)),
                used_gp=used_gp,
                used_itad=used_itad,
            )
            for o in new_offers:
                db.mark_giveaway_seen(owner_id, o.source, o.external_id, seen_at=now)
            db.upsert_giveaways_prefs(
                owner_id,
                stores=prefs.stores,
                platforms=prefs.platforms,
                digest_enabled=True,
                first_digest_sent=True,
                last_digest_at=now,
            )
        except Forbidden:
            logger.info("giveaways digest forbidden owner=%s", owner_id)
        except Exception:
            logger.exception("giveaways digest failed owner=%s", owner_id)
