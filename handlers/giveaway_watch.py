"""Giveaway-watch subscriptions: wait for a free giveaway of a chosen IGDB game."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import secrets
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import ContextTypes, ConversationHandler

import analytics
import beta as beta_features
import premium as prem
from bot_helpers import _menu, reply_chat_id
from config import MAX_SUBSCRIPTIONS_PER_OWNER
from db import (
    Database,
    Subscription,
    dump_giveaway_watch_prefs,
    is_giveaway_watch_sub,
    parse_giveaway_watch_prefs,
)
from db.models import GiveawayPlatformPref, GiveawayWatchPrefs
from giveaway_sources import GiveawayOffer, fetch_active_giveaways
from i18n import DEFAULT_LOCALE, btn, t
from igdb_dumps import igdb_image_url

logger = logging.getLogger(__name__)

GIVEAWAYS_BETA_ID = "giveaways-alerts"
_SEARCH_LIMIT = 100
_PICK_PAGE_SIZE = 5


def giveaways_feature_available(db: Database, user_id: int) -> bool:
    return beta_features.is_enabled(db, user_id, GIVEAWAYS_BETA_ID)


def _user_lang(db: Database, user_id: int) -> str:
    return db.get_user_locale(user_id) or DEFAULT_LOCALE


def _wz() -> dict[str, int]:
    from bot import (
        GIVEAWAY_WATCH_DUP,
        GIVEAWAY_WATCH_PICK,
        GIVEAWAY_WATCH_PLATFORMS,
        GIVEAWAY_WATCH_SEARCH,
    )

    return {
        "GIVEAWAY_WATCH_SEARCH": GIVEAWAY_WATCH_SEARCH,
        "GIVEAWAY_WATCH_PICK": GIVEAWAY_WATCH_PICK,
        "GIVEAWAY_WATCH_DUP": GIVEAWAY_WATCH_DUP,
        "GIVEAWAY_WATCH_PLATFORMS": GIVEAWAY_WATCH_PLATFORMS,
    }


def edit_giveaway_watch_options_keyboard(
    sub_id: int, lang: str
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("edit_giveaway_watch_platforms", lang),
                    callback_data=f"edit_gw:{sub_id}:platforms",
                )
            ]
        ]
    )


def _game_pick_keyboard(
    games: list[dict[str, Any]],
    lang: str,
    *,
    companies: dict[int, tuple[str, str]] | None = None,
    page: int = 0,
) -> InlineKeyboardMarkup:
    companies = companies or {}
    total = len(games)
    pages = max(1, (total + _PICK_PAGE_SIZE - 1) // _PICK_PAGE_SIZE)
    page = max(0, min(int(page), pages - 1))
    start = page * _PICK_PAGE_SIZE
    chunk = games[start : start + _PICK_PAGE_SIZE]
    rows: list[list[InlineKeyboardButton]] = []
    for g in chunk:
        gid = int(g["id"])
        name = str(g.get("name") or "").strip() or f"#{gid}"
        pub, dev = companies.get(gid, ("", ""))
        suffix = ""
        if pub or dev:
            bits = [b for b in (pub, dev) if b]
            suffix = f" ({', '.join(bits[:2])})"
        label = f"{name}{suffix}"
        if len(label) > 60:
            label = label[:57] + "…"
        rows.append(
            [
                InlineKeyboardButton(
                    label, callback_data=f"gvw:pick:{gid}"
                )
            ]
        )
    if pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton("‹", callback_data=f"gvw:page:{page - 1}")
            )
        nav.append(
            InlineKeyboardButton(
                f"{page + 1}/{pages}", callback_data="gvw:page:noop"
            )
        )
        if page < pages - 1:
            nav.append(
                InlineKeyboardButton("›", callback_data=f"gvw:page:{page + 1}")
            )
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(
                btn("wizard_cancel", lang), callback_data="gvw:cancel"
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def _platforms_keyboard(
    platforms: list[dict[str, Any]],
    selected: set[int],
    lang: str,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for row in platforms:
        pid = int(row["platform_id"])
        mark = "✅ " if pid in selected else "⬜️ "
        name = str(row.get("platform_name") or "—")
        rows.append(
            [
                InlineKeyboardButton(
                    f"{mark}{name}",
                    callback_data=f"gvw:toggle:{pid}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                t("giveaway_watch_any_platform", lang),
                callback_data="gvw:create:any",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                t("giveaway_watch_create", lang),
                callback_data="gvw:create",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                btn("wizard_cancel", lang), callback_data="gvw:cancel"
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def canonicalize_igdb_platform_name(name: str) -> set[str]:
    """Map IGDB platform display name → giveaway_sources PLATFORM_IDS."""
    from giveaway_sources import _PLATFORM_ALIAS

    n = (name or "").casefold().strip()
    if not n:
        return set()
    hits: set[str] = set()
    if n in _PLATFORM_ALIAS:
        hits.add(_PLATFORM_ALIAS[n])
    for alias, pid in _PLATFORM_ALIAS.items():
        if alias in n or n in alias:
            hits.add(pid)
    # Common IGDB long names
    extras = {
        "microsoft windows": "pc",
        "pc (microsoft windows)": "pc",
        "macos": "mac",
        "mac os": "mac",
        "playstation 4": "ps4",
        "playstation 5": "ps5",
        "xbox series x|s": "xbox_series",
        "xbox series x": "xbox_series",
        "xbox series s": "xbox_series",
        "nintendo switch": "switch",
    }
    for key, pid in extras.items():
        if key in n or n == key:
            hits.add(pid)
    return hits


def platforms_match_offer(
    prefs: GiveawayWatchPrefs, offer: GiveawayOffer
) -> bool:
    if not prefs.platforms:
        return True
    wanted: set[str] = set()
    for p in prefs.platforms:
        wanted |= canonicalize_igdb_platform_name(p.platform_name)
    if not wanted:
        return True
    return bool(wanted & set(offer.platform_ids))


def offer_matches_game(
    db: Database, offer: GiveawayOffer, *, igdb_game_id: int, game_name: str
) -> bool:
    title = (offer.title or "").strip()
    if not title:
        return False
    want = (game_name or "").casefold().strip()
    if want and want in title.casefold():
        return True
    try:
        hits = db.igdb_search_games_by_name(title, limit=5)
    except Exception:
        logger.exception("giveaway_watch igdb search failed title=%s", title[:80])
        return False
    if not hits:
        return False
    return int(hits[0].get("id") or 0) == int(igdb_game_id)


async def start_giveaway_watch_wizard(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    db: Database = context.application.bot_data["db"]
    user_id = update.effective_user.id
    lang = _user_lang(db, user_id)
    chat_id = reply_chat_id(update)
    if not giveaways_feature_available(db, user_id):
        text = t("giveaways_beta_required", lang)
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(text)
        else:
            await context.bot.send_message(
                chat_id, text, reply_markup=_menu(lang, user_id)
            )
        context.user_data.clear()
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["alert_type"] = "giveaway_watch"
    prompt = t("giveaway_watch_game_prompt", lang)
    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    btn("wizard_cancel", lang), callback_data="gvw:cancel"
                )
            ]
        ]
    )
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text("✓")
        except BadRequest:
            pass
        await context.bot.send_message(chat_id, prompt, reply_markup=markup)
    else:
        await context.bot.send_message(chat_id, prompt, reply_markup=markup)
    return _wz()["GIVEAWAY_WATCH_SEARCH"]


async def cancel_giveaway_watch_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    db: Database = context.application.bot_data["db"]
    user_id = query.from_user.id
    lang = _user_lang(db, user_id)
    await query.edit_message_text(t("cancelled", lang))
    context.user_data.clear()
    await context.bot.send_message(
        reply_chat_id(update),
        t("menu_main", lang),
        reply_markup=_menu(lang, user_id),
    )
    return ConversationHandler.END


async def receive_giveaway_watch_game_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    db: Database = context.application.bot_data["db"]
    user_id = update.effective_user.id
    lang = _user_lang(db, user_id)
    if not giveaways_feature_available(db, user_id):
        await update.effective_message.reply_text(
            t("giveaways_beta_required", lang), reply_markup=_menu(lang, user_id)
        )
        context.user_data.clear()
        return ConversationHandler.END
    query = (update.effective_message.text or "").strip()
    if not query:
        await update.effective_message.reply_text(
            t("giveaway_watch_game_prompt", lang)
        )
        return _wz()["GIVEAWAY_WATCH_SEARCH"]
    status = await update.effective_message.reply_text(
        t("release_game_searching", lang)
    )
    games = db.igdb_search_games_by_name(query, limit=_SEARCH_LIMIT)
    if not games:
        await status.edit_text(
            t("release_game_not_found", lang),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            btn("wizard_cancel", lang), callback_data="gvw:cancel"
                        )
                    ]
                ]
            ),
        )
        return _wz()["GIVEAWAY_WATCH_SEARCH"]
    companies = db.igdb_company_labels_for_games([int(g["id"]) for g in games])
    context.user_data["gvw_search_hits"] = games
    context.user_data["gvw_search_companies"] = companies
    context.user_data["gvw_search_page"] = 0
    await status.edit_text(
        t("release_game_pick", lang),
        reply_markup=_game_pick_keyboard(
            games, lang, companies=companies, page=0
        ),
    )
    return _wz()["GIVEAWAY_WATCH_PICK"]


async def _after_game_chosen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    db: Database,
    user_id: int,
    lang: str,
    game: dict[str, Any],
) -> int:
    game_id = int(game.get("id") or 0)
    platforms = db.igdb_platforms_for_game(game_id)
    context.user_data["gvw_game"] = game
    context.user_data["gvw_platforms"] = platforms
    context.user_data["gvw_selected"] = set()
    chat_id = reply_chat_id(update)
    name = html.escape(str(game.get("name") or ""))
    if not platforms:
        prefs = GiveawayWatchPrefs(
            igdb_game_id=game_id,
            game_name=str(game.get("name") or "").strip(),
            platforms=[],
            notified_keys=[],
        )
        sub, status = await create_giveaway_watch_subscription(
            context.bot,
            db,
            user_id,
            lang,
            prefs=prefs,
            allow_duplicate=bool(context.user_data.get("gvw_allow_duplicate")),
        )
        return await _finish_create(
            update, context, db, user_id, lang, sub, status, prefs
        )
    text = t("giveaway_watch_pick_platforms", lang, game=name)
    markup = _platforms_keyboard(platforms, set(), lang)
    query = update.callback_query
    if query:
        try:
            await query.edit_message_text(
                text, reply_markup=markup, parse_mode=ParseMode.HTML
            )
        except BadRequest:
            await context.bot.send_message(
                chat_id, text, reply_markup=markup, parse_mode=ParseMode.HTML
            )
    else:
        await context.bot.send_message(
            chat_id, text, reply_markup=markup, parse_mode=ParseMode.HTML
        )
    return _wz()["GIVEAWAY_WATCH_PLATFORMS"]


async def receive_giveaway_watch_pick(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    db: Database = context.application.bot_data["db"]
    user_id = query.from_user.id
    lang = _user_lang(db, user_id)
    data = query.data or ""
    if data == "gvw:cancel":
        return await cancel_giveaway_watch_callback(update, context)
    if data == "gvw:page:noop":
        return _wz()["GIVEAWAY_WATCH_PICK"]
    if data.startswith("gvw:page:"):
        try:
            page = int(data.rsplit(":", 1)[1])
        except ValueError:
            return _wz()["GIVEAWAY_WATCH_PICK"]
        games = context.user_data.get("gvw_search_hits") or []
        if not isinstance(games, list) or not games:
            await query.edit_message_text(t("release_game_not_found", lang))
            return _wz()["GIVEAWAY_WATCH_SEARCH"]
        companies = context.user_data.get("gvw_search_companies") or {}
        if not isinstance(companies, dict):
            companies = {}
        context.user_data["gvw_search_page"] = page
        try:
            await query.edit_message_reply_markup(
                reply_markup=_game_pick_keyboard(
                    games, lang, companies=companies, page=page
                )
            )
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise
        return _wz()["GIVEAWAY_WATCH_PICK"]
    if not data.startswith("gvw:pick:"):
        return _wz()["GIVEAWAY_WATCH_PICK"]
    try:
        game_id = int(data.split(":")[-1])
    except ValueError:
        return _wz()["GIVEAWAY_WATCH_PICK"]
    game = db.igdb_game_by_id(game_id)
    if not game:
        await query.edit_message_text(t("release_game_not_found", lang))
        return _wz()["GIVEAWAY_WATCH_SEARCH"]
    exists = next(
        (
            s
            for s in db.get_subscriptions_by_owner(user_id)
            if is_giveaway_watch_sub(s)
            and (parsed := parse_giveaway_watch_prefs(s.giveaway_watch_prefs))
            and parsed.igdb_game_id == game_id
        ),
        None,
    )
    if exists and not context.user_data.get("gvw_allow_duplicate"):
        from i18n import alert_dup_keyboard

        context.user_data["gvw_game"] = game
        context.user_data["alert_dup_force"] = {
            "kind": "giveaway_watch_wizard",
            "sub_id": exists.id,
        }
        try:
            await query.edit_message_text(
                t("giveaway_watch_already_subscribed", lang),
                reply_markup=alert_dup_keyboard(lang, exists.id),
            )
        except BadRequest:
            await context.bot.send_message(
                reply_chat_id(update),
                t("giveaway_watch_already_subscribed", lang),
                reply_markup=alert_dup_keyboard(lang, exists.id),
            )
        return _wz()["GIVEAWAY_WATCH_DUP"]
    return await _after_game_chosen(
        update, context, db=db, user_id=user_id, lang=lang, game=game
    )


async def receive_giveaway_watch_dup(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    db: Database = context.application.bot_data["db"]
    user_id = query.from_user.id
    lang = _user_lang(db, user_id)
    data = query.data or ""
    if data.startswith("alert_dup:edit:"):
        context.user_data.pop("alert_dup_force", None)
        from handlers.subscriptions import on_share_dup_edit

        await on_share_dup_edit(update, context)
        context.user_data.clear()
        return ConversationHandler.END
    if data != "alert_dup:continue":
        return _wz()["GIVEAWAY_WATCH_DUP"]
    context.user_data.pop("alert_dup_force", None)
    context.user_data["gvw_allow_duplicate"] = True
    game = context.user_data.get("gvw_game") or {}
    if not game:
        await query.edit_message_text(t("release_game_not_found", lang))
        context.user_data.clear()
        return ConversationHandler.END
    try:
        await query.edit_message_reply_markup(None)
    except BadRequest:
        pass
    return await _after_game_chosen(
        update, context, db=db, user_id=user_id, lang=lang, game=game
    )


async def receive_giveaway_watch_platforms(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    db: Database = context.application.bot_data["db"]
    user_id = query.from_user.id
    lang = _user_lang(db, user_id)
    data = query.data or ""
    if data == "gvw:cancel":
        return await cancel_giveaway_watch_callback(update, context)
    platforms: list[dict[str, Any]] = list(
        context.user_data.get("gvw_platforms") or []
    )
    selected: set[int] = set(context.user_data.get("gvw_selected") or set())
    edit_sub_id = context.user_data.get("gvw_edit_sub_id")

    if data.startswith("gvw:toggle:"):
        try:
            pid = int(data.rsplit(":", 1)[1])
        except ValueError:
            return _wz()["GIVEAWAY_WATCH_PLATFORMS"]
        if pid in selected:
            selected.discard(pid)
        else:
            selected.add(pid)
        context.user_data["gvw_selected"] = selected
        try:
            await query.edit_message_reply_markup(
                reply_markup=_platforms_keyboard(platforms, selected, lang)
            )
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise
        return _wz()["GIVEAWAY_WATCH_PLATFORMS"]

    any_platform = data == "gvw:create:any"
    if data not in ("gvw:create", "gvw:create:any"):
        return _wz()["GIVEAWAY_WATCH_PLATFORMS"]

    game = context.user_data.get("gvw_game") or {}
    game_id = int(game.get("id") or 0)
    name = str(game.get("name") or "").strip()
    if not game_id or not name:
        await query.edit_message_text(t("giveaway_watch_create_failed", lang))
        context.user_data.clear()
        return ConversationHandler.END

    chosen: list[GiveawayPlatformPref] = []
    if not any_platform:
        by_id = {int(p["platform_id"]): p for p in platforms}
        for pid in sorted(selected):
            row = by_id.get(pid)
            if not row:
                continue
            chosen.append(
                GiveawayPlatformPref(
                    platform_id=pid,
                    platform_name=str(row.get("platform_name") or "—"),
                )
            )

    prefs = GiveawayWatchPrefs(
        igdb_game_id=game_id,
        game_name=name,
        platforms=chosen,
        notified_keys=[],
    )

    if edit_sub_id:
        sub = db.get_subscription(int(edit_sub_id), user_id)
        if not sub or not is_giveaway_watch_sub(sub):
            await query.edit_message_text(t("sub_not_found", lang))
            context.user_data.clear()
            return ConversationHandler.END
        old = parse_giveaway_watch_prefs(sub.giveaway_watch_prefs)
        if old:
            prefs.notified_keys = list(old.notified_keys)
        db.update_subscription(
            sub.id,
            user_id,
            giveaway_watch_prefs=dump_giveaway_watch_prefs(prefs),
            mark_sync_edited=False,
        )
        await query.edit_message_text(t("giveaway_watch_platforms_updated", lang))
        context.user_data.clear()
        await context.bot.send_message(
            reply_chat_id(update),
            t("menu_main", lang),
            reply_markup=_menu(lang, user_id),
        )
        return ConversationHandler.END

    sub, status = await create_giveaway_watch_subscription(
        context.bot,
        db,
        user_id,
        lang,
        prefs=prefs,
        allow_duplicate=bool(context.user_data.get("gvw_allow_duplicate")),
    )
    return await _finish_create(
        update, context, db, user_id, lang, sub, status, prefs
    )


async def _finish_create(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    db: Database,
    user_id: int,
    lang: str,
    sub: Subscription | None,
    status: str,
    prefs: GiveawayWatchPrefs,
) -> int:
    markup = _menu(lang, user_id)
    if status == "sub_limit":
        note = t("sub_limit", lang, limit=MAX_SUBSCRIPTIONS_PER_OWNER)
    elif status == "giveaway_watch_subscribed_paused":
        from config import PREMIUM_FREE_ACTIVE_LIMIT

        note = (
            f"{t('giveaway_watch_subscribed_ok', lang)}\n"
            + t(
                "created_paused_note",
                lang,
                kind=t("alert_type_giveaway_watch", lang),
                limit=PREMIUM_FREE_ACTIVE_LIMIT,
            )
        )
    elif status == "giveaway_watch_already_subscribed" and sub is not None:
        from i18n import alert_dup_keyboard

        note = t("giveaway_watch_already_subscribed", lang)
        markup = alert_dup_keyboard(lang, sub.id)
        pending = {
            "kind": "giveaway_watch",
            "prefs": dump_giveaway_watch_prefs(prefs),
        }
        context.user_data.clear()
        context.user_data["alert_dup_force"] = pending
        msg = update.callback_query.message if update.callback_query else None
        if msg:
            await msg.reply_text(note, reply_markup=markup)
        else:
            await update.effective_message.reply_text(note, reply_markup=markup)
        return ConversationHandler.END
    else:
        note = t("giveaway_watch_subscribed_ok", lang)
        if status == "giveaways_beta_required":
            note = t("giveaways_beta_required", lang)
        elif status == "giveaway_watch_create_failed":
            note = t("giveaway_watch_create_failed", lang)
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(note)
        except BadRequest:
            pass
        await context.bot.send_message(
            reply_chat_id(update),
            t("menu_main", lang),
            reply_markup=markup,
        )
    elif update.effective_message:
        await update.effective_message.reply_text(note, reply_markup=markup)
    context.user_data.clear()
    from handlers.background_jobs import sync_optional_jobs

    sync_optional_jobs(context.application.job_queue, db)
    return ConversationHandler.END


async def create_giveaway_watch_subscription(
    bot: Any,
    db: Database,
    user_id: int,
    lang: str,
    *,
    prefs: GiveawayWatchPrefs,
    allow_duplicate: bool = False,
) -> tuple[Subscription | None, str]:
    if not giveaways_feature_available(db, user_id):
        return None, "giveaways_beta_required"
    existing = [
        s
        for s in db.get_subscriptions_by_owner(user_id)
        if is_giveaway_watch_sub(s)
        and parse_giveaway_watch_prefs(s.giveaway_watch_prefs)
        and parse_giveaway_watch_prefs(s.giveaway_watch_prefs).igdb_game_id
        == prefs.igdb_game_id
    ]
    if existing and not allow_duplicate:
        return existing[0], "giveaway_watch_already_subscribed"
    if len(db.get_subscriptions_by_owner(user_id)) >= MAX_SUBSCRIPTIONS_PER_OWNER:
        return None, "sub_limit"
    label = prefs.game_name[:64]
    login = (
        re.sub(r"[^a-z0-9_]", "", prefs.game_name.lower())[:25]
        or f"gw{prefs.igdb_game_id}"
    )
    enabled = await prem.may_enable_subscription_async(
        bot, db, user_id, twitch_username=login
    )
    prefs_json = dump_giveaway_watch_prefs(prefs)
    sub_id = db.add_subscription(
        owner_id=user_id,
        twitch_username=login,
        twitch_user_id=f"gvw:{user_id}:{secrets.token_hex(4)}",
        message_template=t(
            "giveaway_watch_default_template", lang, game=prefs.game_name
        ),
        dest_type="dm",
        chat_id=user_id,
        thread_id=None,
        disable_link_preview=True,
        enabled=enabled,
        notify_on_live=False,
        notify_on_end=False,
        notify_on_category_change=False,
        notify_on_drops=False,
        giveaway_watch_prefs=prefs_json,
    )
    db.update_subscription(
        sub_id, user_id, twitch_username=label, mark_sync_edited=False
    )
    sub = db.get_subscription(sub_id, user_id)
    analytics.capture(
        user_id,
        "giveaway_watch_subscribed",
        {
            "subscription_id": sub_id,
            "igdb_game_id": prefs.igdb_game_id,
            "enabled": enabled,
            "platforms": len(prefs.platforms),
        },
    )
    if not enabled:
        return sub, "giveaway_watch_subscribed_paused"
    return sub, "giveaway_watch_subscribed_ok"


async def start_edit_giveaway_watch_platforms(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    db: Database = context.application.bot_data["db"]
    user_id = query.from_user.id
    lang = _user_lang(db, user_id)
    try:
        sub_id = int((query.data or "").split(":")[1])
    except (IndexError, ValueError):
        return ConversationHandler.END
    if not giveaways_feature_available(db, user_id):
        await query.edit_message_text(t("giveaways_beta_required", lang))
        return ConversationHandler.END
    sub = db.get_subscription(sub_id, user_id)
    prefs = parse_giveaway_watch_prefs(sub.giveaway_watch_prefs) if sub else None
    if not prefs:
        await query.edit_message_text(t("sub_not_found", lang))
        return ConversationHandler.END
    platforms = db.igdb_platforms_for_game(prefs.igdb_game_id)
    if not platforms:
        await query.edit_message_text(t("giveaway_watch_no_platforms", lang))
        return ConversationHandler.END
    selected = {p.platform_id for p in prefs.platforms}
    context.user_data.clear()
    context.user_data["gvw_edit_sub_id"] = sub_id
    context.user_data["gvw_game"] = {
        "id": prefs.igdb_game_id,
        "name": prefs.game_name,
    }
    context.user_data["gvw_platforms"] = platforms
    context.user_data["gvw_selected"] = selected
    await query.edit_message_text(
        t(
            "giveaway_watch_pick_platforms",
            lang,
            game=html.escape(prefs.game_name),
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=_platforms_keyboard(platforms, selected, lang),
    )
    return _wz()["GIVEAWAY_WATCH_PLATFORMS"]


async def check_giveaway_watch_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    bot = context.bot
    subs = db.get_giveaway_watch_subscriptions()
    if not subs:
        return
    catalog = await asyncio.to_thread(fetch_active_giveaways)
    if not catalog:
        return
    for sub in subs:
        if not sub.enabled:
            continue
        if not giveaways_feature_available(db, sub.owner_id):
            continue
        prefs = parse_giveaway_watch_prefs(sub.giveaway_watch_prefs)
        if not prefs:
            continue
        lang = _user_lang(db, sub.owner_id)
        match: GiveawayOffer | None = None
        for offer in catalog:
            key = f"{offer.source}:{offer.external_id}"
            if key in prefs.notified_keys:
                continue
            if not offer_matches_game(
                db,
                offer,
                igdb_game_id=prefs.igdb_game_id,
                game_name=prefs.game_name,
            ):
                continue
            if not platforms_match_offer(prefs, offer):
                continue
            match = offer
            break
        if not match:
            continue
        key = f"{match.source}:{match.external_id}"
        sent = await _send_giveaway_watch_notify(
            bot, db, sub, prefs, match, lang
        )
        if not sent:
            continue
        prefs.notified_keys.append(key)
        db.update_subscription(
            sub.id,
            sub.owner_id,
            giveaway_watch_prefs=dump_giveaway_watch_prefs(prefs),
            mark_sync_edited=False,
        )
        if sub.enabled:
            db.toggle_subscription(sub.id, sub.owner_id)
            analytics.capture(
                sub.owner_id,
                "giveaway_watch_paused_after_notify",
                {
                    "subscription_id": sub.id,
                    "igdb_game_id": prefs.igdb_game_id,
                    "offer": key,
                },
            )


async def _send_giveaway_watch_notify(
    bot: Any,
    db: Database,
    sub: Subscription,
    prefs: GiveawayWatchPrefs,
    offer: GiveawayOffer,
    lang: str,
) -> bool:
    chat_id = int(sub.chat_id or sub.owner_id)
    title = html.escape(offer.title or prefs.game_name)
    store = html.escape(offer.store_id or "—")
    claim = (offer.claim_url or "").strip()
    body = t(
        "giveaway_watch_notify",
        lang,
        game=title,
        store=store,
        claim=html.escape(claim) if claim else "—",
    )
    body = f"{body}\n\n{t('giveaway_watch_paused_note', lang)}"
    cover_mid = db.igdb_cover_image_id_for_game(prefs.igdb_game_id)
    igdb_attr = '<a href="https://www.igdb.com">IGDB.com</a>'
    caption = f"{body}\n\n{igdb_attr}"
    if len(caption) > 1024:
        caption = caption[:1020] + "…"
    try:
        if cover_mid:
            url = igdb_image_url(cover_mid)
            try:
                await bot.send_photo(
                    chat_id,
                    photo=url,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                )
                return True
            except BadRequest:
                pass
        await bot.send_message(
            chat_id,
            caption,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )
        return True
    except Forbidden:
        logger.info(
            "giveaway_watch notify forbidden owner=%s sub=%s",
            sub.owner_id,
            sub.id,
        )
        return False
    except Exception:
        logger.exception(
            "giveaway_watch notify failed owner=%s sub=%s",
            sub.owner_id,
            sub.id,
        )
        return False
