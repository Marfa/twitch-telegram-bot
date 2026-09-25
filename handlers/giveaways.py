"""Game giveaways digest (GamerPower + ITAD) — beta giveaways-alerts."""
from __future__ import annotations

import asyncio
import html
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import ContextTypes, ConversationHandler

import beta as beta_features
from bot_helpers import _menu, reply_chat_id
from db import Database
from db.models import GiveawayCatalogEntry
from giveaway_sources import (
    PLATFORM_IDS,
    STORE_IDS,
    GiveawayOffer,
    attribution_html,
    claim_url_or_search,
    fetch_active_giveaways,
)
from i18n import DEFAULT_LOCALE, t
from igdb_dumps import igdb_image_url

logger = logging.getLogger(__name__)

GIVEAWAYS_BETA_ID = "giveaways-alerts"
_PAGE_SIZE = 5
_DIGEST_MIN_INTERVAL_SEC = 20 * 3600
_CATALOG_MAX_AGE_SEC = 24 * 3600
_BROWSE_KEY = "giveaways_browse"


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
                    callback_data="gv:fresh",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                t("giveaways_btn_watch", lang),
                callback_data="gv:watch",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                t("giveaways_btn_back", lang),
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
                t("giveaways_btn_back", lang),
                callback_data="gv:hub",
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def _card_keyboard(
    lang: str,
    *,
    claim_url: str,
    igdb_game_id: int | None,
    show_more_offset: int | None = None,
) -> InlineKeyboardMarkup:
    row: list[InlineKeyboardButton] = []
    url = (claim_url or "").strip()
    if url.startswith(("http://", "https://")):
        row.append(
            InlineKeyboardButton(
                t("giveaways_go_store", lang),
                url=url,
            )
        )
    if igdb_game_id:
        row.append(
            InlineKeyboardButton(
                t("giveaways_find_streams", lang),
                callback_data=f"gv:streams:{igdb_game_id}",
            )
        )
    rows: list[list[InlineKeyboardButton]] = []
    if row:
        rows.append(row)
    if show_more_offset is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    t("giveaways_show_more", lang),
                    callback_data=f"gv:more:{int(show_more_offset)}",
                )
            ]
        )
    return InlineKeyboardMarkup(rows) if rows else InlineKeyboardMarkup([])


def _details_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("giveaways_details", lang),
                    callback_data="gv:details",
                )
            ]
        ]
    )


def _store_browse(
    application: Any,
    user_id: int,
    entries: list[GiveawayCatalogEntry],
    lang: str,
) -> None:
    application.bot_data.setdefault(_BROWSE_KEY, {})[int(user_id)] = {
        "entries": list(entries),
        "lang": lang,
    }


def _load_browse(
    application: Any, user_id: int
) -> tuple[list[GiveawayCatalogEntry], str] | None:
    raw = (application.bot_data.get(_BROWSE_KEY) or {}).get(int(user_id))
    if not isinstance(raw, dict):
        return None
    entries = raw.get("entries")
    if not isinstance(entries, list) or not entries:
        return None
    lang = str(raw.get("lang") or DEFAULT_LOCALE)
    return entries, lang


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


def catalog_entry_to_offer(entry: GiveawayCatalogEntry) -> GiveawayOffer:
    return GiveawayOffer(
        source=entry.source,
        external_id=entry.external_id,
        title=entry.title,
        store_id=entry.store_id,
        platform_ids=entry.platform_ids,
        claim_url=entry.claim_url,
        start_at=entry.start_at,
        end_at=entry.end_at,
        description=entry.description,
        image_url=entry.image_url,
        dedupe_key=entry.dedupe_key,
    )


def filter_catalog_entries(
    entries: list[GiveawayCatalogEntry],
    *,
    stores: set[str],
    platforms: set[str],
) -> list[GiveawayCatalogEntry]:
    if not stores or not platforms:
        return []
    out: list[GiveawayCatalogEntry] = []
    for e in entries:
        if e.store_id not in stores:
            continue
        if not (set(e.platform_ids) & platforms):
            continue
        out.append(e)
    return out


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


def _enrich_offer_base(db: Database, offer: GiveawayOffer) -> GiveawayCatalogEntry:
    """IGDB enrich once for the daily snapshot (no locale / DeepL)."""
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
            summary = str(game.get("summary") or "").strip()
            mid = db.igdb_cover_image_id_for_game(igdb_id)
            if mid:
                cover_url = igdb_image_url(mid)
    if not summary:
        summary = (offer.description or "")[:800]
    return GiveawayCatalogEntry(
        source=offer.source,
        external_id=offer.external_id,
        title=offer.title,
        store_id=offer.store_id,
        platform_ids=tuple(offer.platform_ids),
        claim_url=offer.claim_url,
        start_at=offer.start_at,
        end_at=offer.end_at,
        description=offer.description,
        image_url=offer.image_url,
        dedupe_key=offer.dedupe_key,
        igdb_id=igdb_id,
        name=name,
        year=year,
        publisher=publisher,
        developer=developer,
        summary=summary,
        cover_url=cover_url or "",
        refreshed_at=0,
    )


def _enriched_from_catalog(
    db: Database, entry: GiveawayCatalogEntry, lang: str
) -> _Enriched:
    from twitch import localize_igdb_summary

    summary = entry.summary
    if entry.igdb_id and summary:
        summary = localize_igdb_summary(summary, lang, db=db)
    return _Enriched(
        offer=catalog_entry_to_offer(entry),
        igdb_id=entry.igdb_id,
        name=entry.name or entry.title,
        year=entry.year,
        publisher=entry.publisher,
        developer=entry.developer,
        summary=summary,
        cover_url=entry.cover_url,
    )


def rebuild_giveaways_catalog_sync(db: Database) -> list[GiveawayCatalogEntry]:
    """Fetch GP/ITAD, enrich once, replace DB snapshot. Sync — call via to_thread."""
    offers = fetch_active_giveaways(force=True)
    now = int(time.time())
    entries = [_enrich_offer_base(db, o) for o in offers]
    for e in entries:
        e.refreshed_at = now
    db.replace_giveaways_catalog(entries)
    logger.info("giveaways catalog rebuilt entries=%s", len(entries))
    return entries


async def ensure_giveaways_catalog(
    db: Database, *, force: bool = False
) -> list[GiveawayCatalogEntry]:
    if not force:
        entries = db.list_giveaways_catalog()
        age = int(time.time()) - int(db.giveaways_catalog_refreshed_at() or 0)
        if entries and age < _CATALOG_MAX_AGE_SEC:
            return entries
    return await asyncio.to_thread(rebuild_giveaways_catalog_sync, db)


def _format_dates(offer: GiveawayOffer, lang: str) -> str:
    start = (offer.start_at or "").strip()
    end = (offer.end_at or "").strip()
    if end.upper() in ("N/A", "NONE", ""):
        end = t("giveaways_date_open", lang)
    if start and end:
        return f"{start} — {end}"
    return start or end or "—"


def _escaped_fit(text: str, room: int) -> str:
    """Escape plain text so the result length is ≤ room (no mid-tag cuts)."""
    if room <= 0 or not text:
        return ""
    raw = text
    escaped = html.escape(raw)
    if len(escaped) <= room:
        return escaped
    ellipsis_room = 1 if room > 1 else 0
    target = max(0, room - ellipsis_room)
    while raw and len(html.escape(raw)) > target:
        over = len(html.escape(raw)) - target
        step = max(1, over // 2)
        raw = raw[: max(0, len(raw) - step)]
    if not raw:
        return ""
    out = html.escape(raw)
    if ellipsis_room and len(text) > len(raw):
        out = out + "…"
    return out


def _build_card_html(item: _Enriched, lang: str, *, footer: str = "") -> str:
    # Photo captions ≤1024. Never slice assembled HTML — that splits <b>/<a>.
    _CAP = 1024
    year_bit = f" ({html.escape(item.year)})" if item.year else ""
    # Leave room for <b></b> (7) + year_bit.
    name_room = max(16, 180 - len(year_bit))
    title = f"<b>{_escaped_fit(item.name, name_room)}{year_bit}</b>"
    pub = html.escape(item.publisher) if item.publisher else "—"
    dev = html.escape(item.developer) if item.developer else "—"
    dates = html.escape(_format_dates(item.offer, lang))
    plat_parts = [
        html.escape(_platform_label(lang, pid)) for pid in item.offer.platform_ids
    ]
    plats = ", ".join(plat_parts) if plat_parts else "—"
    meta = "\n".join(
        [
            title,
            f"{t('giveaways_publisher', lang)} {pub}",
            f"{t('giveaways_developer', lang)} {dev}",
            f"{t('giveaways_dates', lang)} {dates}",
            f"{t('giveaways_platforms', lang)} {plats}",
        ]
    )
    use_footer = footer
    footer_block = f"\n\n{use_footer}" if use_footer else ""
    if len(meta) + len(footer_block) > _CAP:
        use_footer = ""
        footer_block = ""
    room = _CAP - len(meta) - len(footer_block)
    desc = ""
    if item.summary and room > 22:
        room -= 2  # blank line before description
        desc = _escaped_fit(item.summary.strip(), room)
    parts = [meta]
    if desc:
        parts.extend(["", desc])
    if use_footer:
        parts.extend(["", use_footer])
    return "\n".join(parts)


async def _send_one_card(
    bot: Any,
    chat_id: int,
    item: _Enriched,
    lang: str,
    *,
    footer: str = "",
    show_more_offset: int | None = None,
) -> None:
    body = _build_card_html(item, lang, footer=footer)
    markup = _card_keyboard(
        lang,
        claim_url=claim_url_or_search(item.offer),
        igdb_game_id=item.igdb_id,
        show_more_offset=show_more_offset,
    )
    if item.cover_url:
        try:
            await bot.send_photo(
                chat_id,
                photo=item.cover_url,
                caption=body,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
            return
        except BadRequest:
            logger.info("giveaways cover send failed title=%s", item.name[:60])
    await bot.send_message(
        chat_id,
        body,
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
        disable_web_page_preview=True,
    )


async def _send_cards_batch(
    bot: Any,
    chat_id: int,
    *,
    db: Database,
    entries: list[GiveawayCatalogEntry],
    lang: str,
    offset: int = 0,
) -> int:
    """Send up to _PAGE_SIZE cards from offset; 'Show more' on the last if needed."""
    if offset < 0:
        offset = 0
    if offset >= len(entries):
        return 0
    chunk = entries[offset : offset + _PAGE_SIZE]
    next_offset = offset + len(chunk)
    has_more = next_offset < len(entries)
    used_gp = any(e.source == "gamerpower" for e in entries)
    used_itad = any(e.source == "itad" for e in entries)
    attr = attribution_html(used_gp=used_gp, used_itad=used_itad)
    igdb_attr = '<a href="https://www.igdb.com">IGDB.com</a>'
    footer = " · ".join(p for p in (attr, igdb_attr) if p)
    items = [_enriched_from_catalog(db, e, lang) for e in chunk]
    for idx, item in enumerate(items):
        is_last = idx == len(items) - 1
        await _send_one_card(
            bot,
            chat_id,
            item,
            lang,
            footer=footer if is_last and not has_more else "",
            show_more_offset=next_offset if is_last and has_more else None,
        )
    return len(chunk)


def _summary_text(lang: str, entries: list[GiveawayCatalogEntry]) -> str:
    names: list[str] = []
    for e in entries:
        label = e.name or _clean_title_for_search(e.title) or e.title
        names.append(f"• {html.escape(label)}")
    listing = "\n".join(names)
    return t("giveaways_new_summary", lang, list=listing)


async def _send_matching_list(
    bot: Any,
    chat_id: int,
    *,
    db: Database,
    application: Any,
    user_id: int,
    lang: str,
    mark_seen: bool,
    set_first_sent: bool = False,
) -> int:
    prefs = _prefs_or_empty(db, user_id)
    catalog = await ensure_giveaways_catalog(db)
    entries = filter_catalog_entries(
        catalog,
        stores=set(prefs.stores),
        platforms=set(prefs.platforms),
    )
    if not entries:
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

    _store_browse(application, user_id, entries, lang)
    await _send_cards_batch(
        bot, chat_id, db=db, entries=entries, lang=lang, offset=0
    )
    if mark_seen:
        now = int(time.time())
        for e in entries:
            db.mark_giveaway_seen(user_id, e.source, e.external_id, seen_at=now)
    if set_first_sent:
        db.upsert_giveaways_prefs(
            user_id,
            stores=prefs.stores,
            platforms=prefs.platforms,
            digest_enabled=True,
            first_digest_sent=True,
            last_digest_at=int(time.time()),
        )
    return len(entries)


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
        application=context.application,
        user_id=user_id,
        lang=lang,
        mark_seen=True,
        set_first_sent=True,
    )


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

    if data == "gv:fresh" or data.startswith("gv:fresh:"):
        await query.answer()
        prefs = _prefs_or_empty(db, user_id)
        if not (prefs.stores and prefs.platforms and prefs.first_digest_sent):
            await query.edit_message_text(t("giveaways_fresh_locked", lang))
            return
        await context.bot.send_message(chat_id, t("giveaways_loading", lang))
        await _send_matching_list(
            context.bot,
            chat_id,
            db=db,
            application=context.application,
            user_id=user_id,
            lang=lang,
            mark_seen=False,
        )
        return

    if data == "gv:details":
        await query.answer()
        await context.bot.send_message(chat_id, t("giveaways_loading", lang))
        loaded = _load_browse(context.application, user_id)
        if not loaded:
            prefs = _prefs_or_empty(db, user_id)
            catalog = await ensure_giveaways_catalog(db)
            entries = filter_catalog_entries(
                catalog,
                stores=set(prefs.stores),
                platforms=set(prefs.platforms),
            )
            if not entries:
                await context.bot.send_message(
                    chat_id, t("giveaways_empty", lang)
                )
                return
            _store_browse(context.application, user_id, entries, lang)
            loaded = (entries, lang)
        entries, browse_lang = loaded
        await _send_cards_batch(
            context.bot,
            chat_id,
            db=db,
            entries=entries,
            lang=browse_lang or lang,
            offset=0,
        )
        return

    if data.startswith("gv:more:"):
        await query.answer()
        try:
            offset = int(data.split(":")[-1])
        except ValueError:
            offset = 0
        loaded = _load_browse(context.application, user_id)
        if not loaded:
            await context.bot.send_message(
                chat_id, t("giveaways_empty", lang)
            )
            return
        entries, browse_lang = loaded
        await _send_cards_batch(
            context.bot,
            chat_id,
            db=db,
            entries=entries,
            lang=browse_lang or lang,
            offset=offset,
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
    # Daily enriched snapshot (HTTP + IGDB) — browse/digest/watch read from DB.
    catalog = await ensure_giveaways_catalog(db, force=True)
    owner_ids = db.list_giveaways_digest_owner_ids()
    now = int(time.time())
    for owner_id in owner_ids:
        if not giveaways_feature_available(db, owner_id):
            continue
        prefs = _prefs_or_empty(db, owner_id)
        if not prefs.stores or not prefs.platforms or not prefs.digest_enabled:
            continue
        if prefs.last_digest_at and (now - prefs.last_digest_at) < _DIGEST_MIN_INTERVAL_SEC:
            continue
        lang = _user_lang(db, owner_id)
        matched = filter_catalog_entries(
            catalog,
            stores=set(prefs.stores),
            platforms=set(prefs.platforms),
        )
        new_entries = [
            e
            for e in matched
            if not db.has_seen_giveaway(owner_id, e.source, e.external_id)
        ]
        if not new_entries:
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
            _store_browse(context.application, owner_id, new_entries, lang)
            await bot.send_message(
                owner_id,
                _summary_text(lang, new_entries),
                parse_mode=ParseMode.HTML,
                reply_markup=_details_keyboard(lang),
                disable_web_page_preview=True,
            )
            for e in new_entries:
                db.mark_giveaway_seen(owner_id, e.source, e.external_id, seen_at=now)
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
    from handlers.giveaway_watch import check_giveaway_watch_alerts

    await check_giveaway_watch_alerts(context)
