"""IGDB game release alerts: wizard, create, daily notify."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler

import analytics
import beta as beta_features
import premium as prem
from bot_helpers import _menu, reply_chat_id
from config import MAX_SUBSCRIPTIONS_PER_OWNER
from db import (
    Database,
    Subscription,
    dump_release_watch_prefs,
    is_release_watch_sub,
    parse_release_watch_prefs,
)
from db.models import ReleasePlatformPref, ReleaseWatchPrefs, release_platform_key
from i18n import DEFAULT_LOCALE, alert_dup_keyboard, btn, t
from igdb_dumps import igdb_image_url

logger = logging.getLogger(__name__)

RELEASE_BETA_ID = "release-alerts"
_RELEASE_PICK_PAGE_SIZE = 5
_RELEASE_SEARCH_LIMIT = 100


def release_feature_available(db: Database, user_id: int) -> bool:
    return beta_features.is_enabled(db, user_id, RELEASE_BETA_ID)


def _user_lang(db: Database, user_id: int) -> str:
    return db.get_user_locale(user_id) or DEFAULT_LOCALE


def _wz() -> dict[str, int]:
    from bot import (
        RELEASE_DATES,
        RELEASE_DAYS,
        RELEASE_DUP,
        RELEASE_PICK,
        RELEASE_SEARCH,
    )

    return {
        "RELEASE_SEARCH": RELEASE_SEARCH,
        "RELEASE_PICK": RELEASE_PICK,
        "RELEASE_DUP": RELEASE_DUP,
        "RELEASE_DATES": RELEASE_DATES,
        "RELEASE_DAYS": RELEASE_DAYS,
    }


def _format_release_date(ts: int, lang: str) -> str:
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return str(ts)
    if lang.startswith("ru"):
        return dt.strftime("%d.%m.%Y")
    return dt.strftime("%Y-%m-%d")


def _platform_label(row: dict[str, Any], lang: str) -> str:
    date_s = _format_release_date(int(row["date"]), lang)
    name = str(row.get("platform_name") or "—").strip() or "—"
    human = str(row.get("human") or "").strip()
    if human and human != date_s:
        return f"{date_s} · {name} ({human})"
    return f"{date_s} · {name}"


def release_game_pick_keyboard(
    games: list[dict[str, Any]],
    lang: str,
    *,
    companies: dict[int, str] | None = None,
    page: int = 0,
    page_size: int = _RELEASE_PICK_PAGE_SIZE,
) -> InlineKeyboardMarkup:
    from collections import Counter

    usable = [
        g
        for g in games
        if g.get("id") is not None and str(g.get("name") or "").strip()
    ]
    size = max(1, int(page_size))
    total_pages = max(1, (len(usable) + size - 1) // size) if usable else 1
    page = max(0, min(int(page), total_pages - 1))
    chunk = usable[page * size : (page + 1) * size]
    # Counts over the full hit list so duplicates stay labeled across pages.
    name_counts = Counter(
        str(g.get("name") or "").strip().casefold() for g in usable
    )
    rows: list[list[InlineKeyboardButton]] = []
    for g in chunk:
        name = str(g.get("name") or "").strip()
        label = name
        if name_counts[name.casefold()] > 1:
            company = (companies or {}).get(int(g["id"]))
            if company:
                label = f"{name} ({company})"
        rows.append(
            [
                InlineKeyboardButton(
                    label[:64],
                    callback_data=f"rel:pick:{int(g['id'])}",
                )
            ]
        )
    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton("‹", callback_data=f"rel:page:{page - 1}")
            )
        nav.append(
            InlineKeyboardButton(
                f"{page + 1}/{total_pages}", callback_data="rel:page:noop"
            )
        )
        if page < total_pages - 1:
            nav.append(
                InlineKeyboardButton("›", callback_data=f"rel:page:{page + 1}")
            )
        rows.append(nav)
    rows.append(
        [InlineKeyboardButton(btn("wizard_cancel", lang), callback_data="rel:cancel")]
    )
    return InlineKeyboardMarkup(rows)


def release_dates_keyboard(
    dates: list[dict[str, Any]],
    selected: set[str],
    lang: str,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i, row in enumerate(dates):
        key = release_platform_key(int(row["platform_id"]), int(row["date"]))
        mark = "✅ " if key in selected else "⬜️ "
        rows.append(
            [
                InlineKeyboardButton(
                    f"{mark}{_platform_label(row, lang)}"[:64],
                    callback_data=f"rel:toggle:{i}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                t("release_create_alerts", lang),
                callback_data="rel:create",
            )
        ]
    )
    rows.append(
        [InlineKeyboardButton(btn("wizard_cancel", lang), callback_data="rel:cancel")]
    )
    return InlineKeyboardMarkup(rows)


def edit_release_options_keyboard(sub_id: int, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("edit_release_days", lang),
                    callback_data=f"edit_r:{sub_id}:days",
                )
            ],
            [
                InlineKeyboardButton(
                    t("edit_release_platforms", lang),
                    callback_data=f"edit_r:{sub_id}:platforms",
                )
            ],
        ]
    )


def _release_delete_keyboard(sub_id: int, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("delivery_fail_delete_btn", lang),
                    callback_data=f"rel:del:{sub_id}",
                )
            ]
        ]
    )


async def _send_game_card(
    bot: Any,
    chat_id: int,
    *,
    db: Database,
    game_id: int,
    game_name: str,
    summary: str,
    body_html: str,
    lang: str = DEFAULT_LOCALE,
    reply_markup: InlineKeyboardMarkup | None = None,
    footer_html: str = "",
) -> None:
    from twitch import igdb_game_page_url, localize_igdb_summary

    cover_mid = db.igdb_cover_image_id_for_game(game_id)
    caption = body_html
    localized = localize_igdb_summary(summary, lang, db=db) if summary else ""
    if localized:
        cap_sum = html.escape(localized[:800])
        caption = f"{body_html}\n\n{cap_sum}"
    game = db.igdb_game_by_id(game_id) or {}
    page_url = igdb_game_page_url(game.get("slug")) or "https://www.igdb.com"
    igdb_attr = f'<a href="{html.escape(page_url, quote=True)}">IGDB.com</a>'
    caption = f"{caption}\n\n{igdb_attr}"
    if footer_html:
        caption = f"{caption}\n\n{footer_html}"
    if len(caption) > 1024:
        caption = caption[:1020] + "…"
    if cover_mid:
        url = igdb_image_url(cover_mid)
        try:
            await bot.send_photo(
                chat_id,
                photo=url,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
            return
        except BadRequest:
            logger.info("release cover send failed game_id=%s", game_id)
    await bot.send_message(
        chat_id,
        caption,
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )


async def start_release_wizard(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    db: Database = context.application.bot_data["db"]
    user_id = update.effective_user.id
    lang = _user_lang(db, user_id)
    chat_id = reply_chat_id(update)
    if not release_feature_available(db, user_id):
        text = t("release_beta_required", lang)
        if update.callback_query:
            await update.callback_query.edit_message_text(text)
        else:
            await context.bot.send_message(
                chat_id, text, reply_markup=_menu(lang, user_id)
            )
        context.user_data.clear()
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["alert_type"] = "release"
    prompt = t("release_game_prompt", lang)
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton(btn("wizard_cancel", lang), callback_data="rel:cancel")]]
    )
    if update.callback_query:
        await update.callback_query.edit_message_text("✓")
        await context.bot.send_message(chat_id, prompt, reply_markup=markup)
    else:
        await context.bot.send_message(chat_id, prompt, reply_markup=markup)
    return _wz()["RELEASE_SEARCH"]


async def receive_release_game_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    db: Database = context.application.bot_data["db"]
    user_id = update.effective_user.id
    lang = _user_lang(db, user_id)
    if not release_feature_available(db, user_id):
        await update.effective_message.reply_text(
            t("release_beta_required", lang), reply_markup=_menu(lang, user_id)
        )
        context.user_data.clear()
        return ConversationHandler.END
    query = (update.effective_message.text or "").strip()
    if not query:
        await update.effective_message.reply_text(t("release_game_prompt", lang))
        return _wz()["RELEASE_SEARCH"]
    status = await update.effective_message.reply_text(
        t("release_game_searching", lang)
    )
    games = db.igdb_search_games_by_name(query, limit=_RELEASE_SEARCH_LIMIT)
    if not games:
        await status.edit_text(
            t("release_game_not_found", lang),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            btn("wizard_cancel", lang), callback_data="rel:cancel"
                        )
                    ]
                ]
            ),
        )
        return _wz()["RELEASE_SEARCH"]
    companies = db.igdb_company_labels_for_games([int(g["id"]) for g in games])
    context.user_data["release_search_hits"] = games
    context.user_data["release_search_companies"] = companies
    context.user_data["release_search_page"] = 0
    await status.edit_text(
        t("release_game_pick", lang),
        reply_markup=release_game_pick_keyboard(
            games, lang, companies=companies, page=0
        ),
    )
    return _wz()["RELEASE_PICK"]


async def receive_release_pick(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    db: Database = context.application.bot_data["db"]
    user_id = query.from_user.id
    lang = _user_lang(db, user_id)
    chat_id = reply_chat_id(update)
    data = query.data or ""
    if data == "rel:cancel":
        await query.edit_message_text(t("cancelled", lang))
        context.user_data.clear()
        await context.bot.send_message(
            chat_id, t("menu_main", lang), reply_markup=_menu(lang, user_id)
        )
        return ConversationHandler.END
    if data == "rel:page:noop":
        return _wz()["RELEASE_PICK"]
    if data.startswith("rel:page:"):
        try:
            page = int(data.rsplit(":", 1)[1])
        except ValueError:
            return _wz()["RELEASE_PICK"]
        games = context.user_data.get("release_search_hits") or []
        if not isinstance(games, list) or not games:
            await query.edit_message_text(t("release_game_not_found", lang))
            return _wz()["RELEASE_SEARCH"]
        companies = context.user_data.get("release_search_companies") or {}
        if not isinstance(companies, dict):
            companies = {}
        context.user_data["release_search_page"] = page
        try:
            await query.edit_message_reply_markup(
                reply_markup=release_game_pick_keyboard(
                    games, lang, companies=companies, page=page
                )
            )
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise
        return _wz()["RELEASE_PICK"]
    if not data.startswith("rel:pick:"):
        return _wz()["RELEASE_PICK"]
    try:
        game_id = int(data.split(":")[-1])
    except ValueError:
        return _wz()["RELEASE_PICK"]
    game = db.igdb_game_by_id(game_id)
    if not game:
        await query.edit_message_text(t("release_game_not_found", lang))
        return _wz()["RELEASE_SEARCH"]
    exists = next(
        (
            s
            for s in db.get_subscriptions_by_owner(user_id)
            if is_release_watch_sub(s)
            and (parsed := parse_release_watch_prefs(s.release_watch_prefs))
            and parsed.igdb_game_id == game_id
        ),
        None,
    )
    if exists and not context.user_data.get("release_allow_duplicate"):
        context.user_data["release_game"] = game
        context.user_data["alert_dup_force"] = {
            "kind": "release_wizard",
            "sub_id": exists.id,
        }
        try:
            await query.edit_message_text(
                t("release_already_subscribed", lang),
                reply_markup=alert_dup_keyboard(lang, exists.id),
            )
        except BadRequest:
            await context.bot.send_message(
                chat_id,
                t("release_already_subscribed", lang),
                reply_markup=alert_dup_keyboard(lang, exists.id),
            )
        return _wz()["RELEASE_DUP"]
    return await _continue_release_with_game(update, context, lang, game)


async def receive_release_dup_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Edit existing release alert or continue wizard to create another."""
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
    if data == "alert_dup:continue":
        context.user_data.pop("alert_dup_force", None)
        context.user_data["release_allow_duplicate"] = True
        game = context.user_data.get("release_game")
        if not isinstance(game, dict) or not game.get("id"):
            await query.edit_message_text(t("release_game_not_found", lang))
            context.user_data.clear()
            return ConversationHandler.END
        try:
            await query.edit_message_reply_markup(None)
        except BadRequest:
            pass
        return await _continue_release_with_game(update, context, lang, game)
    return _wz()["RELEASE_DUP"]


async def _continue_release_with_game(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    lang: str,
    game: dict[str, Any],
) -> int:
    query = update.callback_query
    db: Database = context.application.bot_data["db"]
    chat_id = reply_chat_id(update)
    user_id = update.effective_user.id
    game_id = int(game["id"])
    now = int(time.time())
    all_dates = db.igdb_release_dates_for_game(game_id)
    future = [r for r in all_dates if int(r["date"]) > now]
    name = str(game["name"])
    summary = str(game.get("summary") or "")
    if not all_dates:
        # Date unknown in local IGDB dumps — still create an alert after days prompt.
        context.user_data["release_game"] = game
        context.user_data["release_date_unknown"] = True
        context.user_data["release_dates"] = []
        context.user_data["release_selected"] = set()
        text = t("release_date_unknown", lang, game=html.escape(name))
        if query:
            try:
                await query.edit_message_text(text, parse_mode=ParseMode.HTML)
            except BadRequest:
                await context.bot.send_message(
                    chat_id, text, parse_mode=ParseMode.HTML
                )
        else:
            await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
        await context.bot.send_message(chat_id, t("release_days_prompt", lang))
        return _wz()["RELEASE_DAYS"]
    if not future:
        body = t(
            "release_already_out",
            lang,
            game=html.escape(name),
        )
        if query:
            try:
                await query.edit_message_text("✓")
            except BadRequest:
                pass
        find_kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        t("release_find_streams", lang),
                        callback_data=f"rel:streams:{game_id}",
                    )
                ]
            ]
        )
        await _send_game_card(
            context.bot,
            chat_id,
            db=db,
            game_id=game_id,
            game_name=name,
            summary=summary,
            body_html=body,
            lang=lang,
            reply_markup=find_kb,
        )
        await context.bot.send_message(
            chat_id, t("menu_main", lang), reply_markup=_menu(lang, user_id)
        )
        context.user_data.clear()
        return ConversationHandler.END
    context.user_data["release_game"] = game
    context.user_data["release_date_unknown"] = False
    context.user_data["release_dates"] = future
    if len(future) == 1:
        key = release_platform_key(
            int(future[0]["platform_id"]), int(future[0]["date"])
        )
        context.user_data["release_selected"] = {key}
    else:
        context.user_data["release_selected"] = set()
    selected: set[str] = context.user_data["release_selected"]
    text = t("release_planned_dates", lang, game=html.escape(name))
    markup = release_dates_keyboard(future, selected, lang)
    if query:
        try:
            await query.edit_message_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
        except BadRequest:
            await context.bot.send_message(
                chat_id,
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
    else:
        await context.bot.send_message(
            chat_id,
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
    return _wz()["RELEASE_DATES"]


async def receive_release_dates_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    db: Database = context.application.bot_data["db"]
    user_id = query.from_user.id
    lang = _user_lang(db, user_id)
    chat_id = reply_chat_id(update)
    data = query.data or ""
    if data == "rel:cancel":
        await query.edit_message_text(t("cancelled", lang))
        context.user_data.clear()
        await context.bot.send_message(
            chat_id, t("menu_main", lang), reply_markup=_menu(lang, user_id)
        )
        return ConversationHandler.END
    dates: list[dict[str, Any]] = list(context.user_data.get("release_dates") or [])
    selected: set[str] = set(context.user_data.get("release_selected") or set())
    game = context.user_data.get("release_game") or {}
    name = str(game.get("name") or "")
    if data.startswith("rel:toggle:"):
        try:
            idx = int(data.split(":")[-1])
        except ValueError:
            return _wz()["RELEASE_DATES"]
        if 0 <= idx < len(dates):
            key = release_platform_key(
                int(dates[idx]["platform_id"]), int(dates[idx]["date"])
            )
            if key in selected:
                selected.discard(key)
            else:
                selected.add(key)
            context.user_data["release_selected"] = selected
        text = t("release_planned_dates", lang, game=html.escape(name))
        try:
            await query.edit_message_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=release_dates_keyboard(dates, selected, lang),
            )
        except BadRequest:
            pass
        return _wz()["RELEASE_DATES"]
    if data == "rel:create":
        if not selected:
            await query.answer(t("release_select_one", lang), show_alert=True)
            return _wz()["RELEASE_DATES"]
        # Editing existing sub platforms
        edit_sid = context.user_data.get("release_edit_sub_id")
        if edit_sid:
            return await _finish_platform_edit(
                update, context, int(edit_sid), dates, selected
            )
        await query.edit_message_text(t("release_days_prompt", lang))
        return _wz()["RELEASE_DAYS"]
    return _wz()["RELEASE_DATES"]


async def _finish_platform_edit(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    sub_id: int,
    dates: list[dict[str, Any]],
    selected: set[str],
) -> int:
    query = update.callback_query
    db: Database = context.application.bot_data["db"]
    user_id = query.from_user.id
    lang = _user_lang(db, user_id)
    chat_id = reply_chat_id(update)
    sub = db.get_subscription(sub_id, user_id)
    prefs = parse_release_watch_prefs(sub.release_watch_prefs) if sub else None
    if not sub or not prefs:
        await query.edit_message_text(t("sub_not_found", lang))
        context.user_data.clear()
        return ConversationHandler.END
    platforms: list[ReleasePlatformPref] = []
    for row in dates:
        key = release_platform_key(int(row["platform_id"]), int(row["date"]))
        if key not in selected:
            continue
        platforms.append(
            ReleasePlatformPref(
                platform_id=int(row["platform_id"]),
                platform_name=str(row.get("platform_name") or "—"),
                date=int(row["date"]),
                human=str(row.get("human") or ""),
            )
        )
    if not platforms:
        await query.answer(t("release_select_one", lang), show_alert=True)
        return _wz()["RELEASE_DATES"]
    prefs.platforms = platforms
    # Drop notified keys that no longer apply
    valid = {release_platform_key(p.platform_id, p.date) for p in platforms}
    prefs.notified_keys = [k for k in prefs.notified_keys if k in valid]
    db.update_subscription(
        sub_id,
        user_id,
        release_watch_prefs=dump_release_watch_prefs(prefs),
        mark_sync_edited=False,
    )
    await query.edit_message_text(t("release_platforms_updated", lang))
    await context.bot.send_message(
        chat_id, t("menu_main", lang), reply_markup=_menu(lang, user_id)
    )
    context.user_data.clear()
    return ConversationHandler.END


async def receive_release_days(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    db: Database = context.application.bot_data["db"]
    user_id = update.effective_user.id
    lang = _user_lang(db, user_id)
    raw = (update.effective_message.text or "").strip()
    if not re.fullmatch(r"\d+", raw):
        await update.effective_message.reply_text(t("release_days_invalid", lang))
        return _wz()["RELEASE_DAYS"]
    days = int(raw)
    edit_sid = context.user_data.get("release_edit_sub_id")
    if edit_sid:
        sub = db.get_subscription(int(edit_sid), user_id)
        prefs = parse_release_watch_prefs(sub.release_watch_prefs) if sub else None
        if not sub or not prefs:
            await update.effective_message.reply_text(
                t("sub_not_found", lang), reply_markup=_menu(lang, user_id)
            )
            context.user_data.clear()
            return ConversationHandler.END
        prefs.days_before = days
        db.update_subscription(
            int(edit_sid),
            user_id,
            release_watch_prefs=dump_release_watch_prefs(prefs),
            mark_sync_edited=False,
        )
        await update.effective_message.reply_text(
            t("release_days_updated", lang, days=days),
            reply_markup=_menu(lang, user_id),
        )
        context.user_data.clear()
        return ConversationHandler.END

    game = context.user_data.get("release_game") or {}
    dates: list[dict[str, Any]] = list(context.user_data.get("release_dates") or [])
    selected: set[str] = set(context.user_data.get("release_selected") or set())
    date_unknown = bool(context.user_data.get("release_date_unknown"))
    game_id = int(game.get("id") or 0)
    name = str(game.get("name") or "").strip()
    if not game_id or not name:
        await update.effective_message.reply_text(
            t("release_create_failed", lang), reply_markup=_menu(lang, user_id)
        )
        context.user_data.clear()
        return ConversationHandler.END
    if not date_unknown and not selected:
        await update.effective_message.reply_text(
            t("release_create_failed", lang), reply_markup=_menu(lang, user_id)
        )
        context.user_data.clear()
        return ConversationHandler.END

    platforms: list[ReleasePlatformPref] = []
    if not date_unknown:
        for row in dates:
            key = release_platform_key(int(row["platform_id"]), int(row["date"]))
            if key not in selected:
                continue
            platforms.append(
                ReleasePlatformPref(
                    platform_id=int(row["platform_id"]),
                    platform_name=str(row.get("platform_name") or "—"),
                    date=int(row["date"]),
                    human=str(row.get("human") or ""),
                )
            )
    prefs = ReleaseWatchPrefs(
        igdb_game_id=game_id,
        game_name=name,
        days_before=days,
        platforms=platforms,
        notified_keys=[],
        date_unknown=date_unknown,
    )
    sub, status = await create_release_subscription(
        context.bot,
        db,
        user_id,
        lang,
        prefs=prefs,
        allow_duplicate=bool(context.user_data.get("release_allow_duplicate")),
    )
    markup = _menu(lang, user_id)
    if status == "sub_limit":
        note = t("sub_limit", lang, limit=MAX_SUBSCRIPTIONS_PER_OWNER)
    elif status == "release_subscribed_paused":
        from config import PREMIUM_FREE_ACTIVE_LIMIT

        note = (
            f"{t('release_subscribed_ok', lang)}\n"
            + t(
                "created_paused_note",
                lang,
                kind=t("alert_type_release", lang),
                limit=PREMIUM_FREE_ACTIVE_LIMIT,
            )
        )
    elif status == "release_already_subscribed" and sub is not None:
        from i18n import alert_dup_keyboard

        note = t("release_already_subscribed", lang)
        markup = alert_dup_keyboard(lang, sub.id)
        pending = {
            "kind": "release",
            "prefs": dump_release_watch_prefs(prefs),
        }
        context.user_data.clear()
        context.user_data["alert_dup_force"] = pending
        await update.effective_message.reply_text(note, reply_markup=markup)
        return ConversationHandler.END
    elif status == "release_subscribed_ok" and date_unknown:
        note = t("release_subscribed_unknown_date", lang)
    else:
        note = t(status, lang)
    await update.effective_message.reply_text(note, reply_markup=markup)
    context.user_data.clear()
    return ConversationHandler.END


async def create_release_subscription(
    bot: Any,
    db: Database,
    user_id: int,
    lang: str,
    *,
    prefs: ReleaseWatchPrefs,
    allow_duplicate: bool = False,
) -> tuple[Subscription | None, str]:
    if not release_feature_available(db, user_id):
        return None, "release_beta_required"
    existing = [
        s
        for s in db.get_subscriptions_by_owner(user_id)
        if is_release_watch_sub(s)
        and parse_release_watch_prefs(s.release_watch_prefs)
        and parse_release_watch_prefs(s.release_watch_prefs).igdb_game_id
        == prefs.igdb_game_id
    ]
    if existing and not allow_duplicate:
        return existing[0], "release_already_subscribed"
    if len(db.get_subscriptions_by_owner(user_id)) >= MAX_SUBSCRIPTIONS_PER_OWNER:
        return None, "sub_limit"
    label = prefs.game_name[:64]
    login = (
        re.sub(r"[^a-z0-9_]", "", prefs.game_name.lower())[:25]
        or f"g{prefs.igdb_game_id}"
    )
    enabled = await prem.may_enable_subscription_async(
        bot, db, user_id, twitch_username=login
    )
    prefs_json = dump_release_watch_prefs(prefs)
    sub_id = db.add_subscription(
        owner_id=user_id,
        twitch_username=login,
        twitch_user_id=f"rel:{user_id}:{secrets.token_hex(4)}",
        message_template=t("release_default_template", lang, game=prefs.game_name),
        dest_type="dm",
        chat_id=user_id,
        thread_id=None,
        disable_link_preview=True,
        enabled=enabled,
        notify_on_live=False,
        notify_on_end=False,
        notify_on_category_change=False,
        notify_on_drops=False,
        release_watch_prefs=prefs_json,
    )
    db.update_subscription(
        sub_id, user_id, twitch_username=label, mark_sync_edited=False
    )
    sub = db.get_subscription(sub_id, user_id)
    analytics.capture(
        user_id,
        "release_watch_subscribed",
        {
            "subscription_id": sub_id,
            "igdb_game_id": prefs.igdb_game_id,
            "enabled": enabled,
            "days_before": prefs.days_before,
        },
    )
    if not enabled:
        return sub, "release_subscribed_paused"
    return sub, "release_subscribed_ok"


async def start_edit_release_days(
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
    sub = db.get_subscription(sub_id, user_id)
    prefs = parse_release_watch_prefs(sub.release_watch_prefs) if sub else None
    if not prefs:
        await query.edit_message_text(t("sub_not_found", lang))
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["release_edit_sub_id"] = sub_id
    await query.edit_message_text(
        t("release_days_prompt", lang)
        + "\n"
        + t("release_days_current", lang, days=prefs.days_before)
    )
    return _wz()["RELEASE_DAYS"]


async def start_edit_release_platforms(
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
    sub = db.get_subscription(sub_id, user_id)
    prefs = parse_release_watch_prefs(sub.release_watch_prefs) if sub else None
    if not prefs:
        await query.edit_message_text(t("sub_not_found", lang))
        return ConversationHandler.END
    now = int(time.time())
    all_dates = db.igdb_release_dates_for_game(prefs.igdb_game_id)
    # Keep already-selected even if past; plus future options
    by_key: dict[str, dict[str, Any]] = {}
    for row in all_dates:
        key = release_platform_key(int(row["platform_id"]), int(row["date"]))
        if int(row["date"]) > now or any(
            p.platform_id == int(row["platform_id"]) and p.date == int(row["date"])
            for p in prefs.platforms
        ):
            by_key[key] = row
    dates = sorted(by_key.values(), key=lambda r: (int(r["date"]), str(r["platform_name"])))
    if not dates:
        await query.edit_message_text(t("release_no_dates_edit", lang))
        return ConversationHandler.END
    selected = {
        release_platform_key(p.platform_id, p.date) for p in prefs.platforms
    }
    context.user_data.clear()
    context.user_data["release_edit_sub_id"] = sub_id
    context.user_data["release_game"] = {
        "id": prefs.igdb_game_id,
        "name": prefs.game_name,
    }
    context.user_data["release_dates"] = dates
    context.user_data["release_selected"] = selected
    await query.edit_message_text(
        t("release_planned_dates", lang, game=html.escape(prefs.game_name)),
        parse_mode=ParseMode.HTML,
        reply_markup=release_dates_keyboard(dates, selected, lang),
    )
    return _wz()["RELEASE_DATES"]


def _refresh_platforms_from_db(
    db: Database, prefs: ReleaseWatchPrefs
) -> list[ReleasePlatformPref]:
    """Re-read dates from local dump; fill unknown-date alerts when dates appear."""
    rows = db.igdb_release_dates_for_game(prefs.igdb_game_id)
    if prefs.date_unknown or not prefs.platforms:
        out: list[ReleasePlatformPref] = []
        seen: set[str] = set()
        for row in rows:
            key = release_platform_key(int(row["platform_id"]), int(row["date"]))
            if key in seen:
                continue
            seen.add(key)
            out.append(
                ReleasePlatformPref(
                    platform_id=int(row["platform_id"]),
                    platform_name=str(row.get("platform_name") or "—"),
                    date=int(row["date"]),
                    human=str(row.get("human") or ""),
                )
            )
        return out
    wanted = {p.platform_id for p in prefs.platforms}
    by_platform: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        pid = int(row["platform_id"])
        if pid not in wanted:
            continue
        by_platform.setdefault(pid, []).append(row)
    out = []
    for pref in prefs.platforms:
        candidates = by_platform.get(pref.platform_id) or []
        if not candidates:
            out.append(pref)
            continue
        exact = next((c for c in candidates if int(c["date"]) == pref.date), None)
        chosen = exact or min(candidates, key=lambda c: int(c["date"]))
        out.append(
            ReleasePlatformPref(
                platform_id=pref.platform_id,
                platform_name=str(chosen.get("platform_name") or pref.platform_name),
                date=int(chosen["date"]),
                human=str(chosen.get("human") or pref.human),
            )
        )
    return out


def _release_all_platforms_notified(prefs: ReleaseWatchPrefs) -> bool:
    """True when every selected platform has already been notified (alert done)."""
    if not prefs.platforms:
        return False
    notified = set(prefs.notified_keys)
    return all(
        release_platform_key(p.platform_id, p.date) in notified for p in prefs.platforms
    )


async def check_release_watch_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    bot = context.bot
    now = int(time.time())
    subs = db.get_release_watch_subscriptions()
    for sub in subs:
        prefs = parse_release_watch_prefs(sub.release_watch_prefs)
        if not prefs:
            continue
        beta_ok = release_feature_available(db, sub.owner_id)
        waiting = prefs.date_unknown or not prefs.platforms
        refreshed = _refresh_platforms_from_db(db, prefs)
        if waiting:
            if not refreshed:
                continue
            prefs.platforms = refreshed
            prefs.date_unknown = False
            analytics.capture(
                sub.owner_id,
                "release_watch_date_backfilled",
                {
                    "subscription_id": sub.id,
                    "igdb_game_id": prefs.igdb_game_id,
                    "platforms": len(refreshed),
                },
            )
        else:
            prefs.platforms = refreshed
        lang = _user_lang(db, sub.owner_id)
        due: list[ReleasePlatformPref] = []
        # Fire only while beta is on; still pause spent alerts if beta was turned off.
        if sub.enabled and beta_ok:
            for p in prefs.platforms:
                key = release_platform_key(p.platform_id, p.date)
                fire_at = int(p.date) - max(0, int(prefs.days_before)) * 86400
                if now >= fire_at and key not in prefs.notified_keys:
                    due.append(p)
            if due:
                pending = set(prefs.notified_keys)
                for p in due:
                    pending.add(release_platform_key(p.platform_id, p.date))
                will_complete = bool(prefs.platforms) and all(
                    release_platform_key(p.platform_id, p.date) in pending
                    for p in prefs.platforms
                )
                sent = await _send_release_notify(
                    bot,
                    db,
                    sub,
                    prefs,
                    due,
                    lang,
                    now=now,
                    pausing=will_complete,
                )
                if sent:
                    for p in due:
                        key = release_platform_key(p.platform_id, p.date)
                        if key not in prefs.notified_keys:
                            prefs.notified_keys.append(key)
        all_done = _release_all_platforms_notified(prefs)
        db.update_subscription(
            sub.id,
            sub.owner_id,
            release_watch_prefs=dump_release_watch_prefs(prefs),
            mark_sync_edited=False,
        )
        if all_done and sub.enabled:
            db.toggle_subscription(sub.id, sub.owner_id)
            analytics.capture(
                sub.owner_id,
                "release_watch_paused_all_out",
                {"subscription_id": sub.id, "igdb_game_id": prefs.igdb_game_id},
            )


async def _send_release_notify(
    bot: Any,
    db: Database,
    sub: Subscription,
    prefs: ReleaseWatchPrefs,
    due: list[ReleasePlatformPref],
    lang: str,
    *,
    now: int,
    pausing: bool = False,
) -> bool:
    platforms_txt = ", ".join(
        f"{html.escape(p.platform_name)} ({_format_release_date(p.date, lang)})"
        for p in due
    )
    all_out = all(int(p.date) <= now for p in due)
    if all_out:
        body = t(
            "release_notify_out",
            lang,
            game=html.escape(prefs.game_name),
            platforms=platforms_txt,
        )
    else:
        body = t(
            "release_notify_soon",
            lang,
            game=html.escape(prefs.game_name),
            platforms=platforms_txt,
            days=prefs.days_before,
        )
    game = db.igdb_game_by_id(prefs.igdb_game_id) or {}
    summary = str(game.get("summary") or "")
    footer = t("release_notify_paused_note", lang) if pausing else ""
    markup = _release_delete_keyboard(sub.id, lang) if pausing else None
    try:
        await _send_game_card(
            bot,
            sub.chat_id or sub.owner_id,
            db=db,
            game_id=prefs.igdb_game_id,
            game_name=prefs.game_name,
            summary=summary,
            body_html=body,
            lang=lang,
            reply_markup=markup,
            footer_html=footer,
        )
    except Exception:
        logger.exception(
            "release notify failed sub=%s owner=%s", sub.id, sub.owner_id
        )
        return False
    analytics.capture(
        sub.owner_id,
        "release_watch_notified",
        {
            "subscription_id": sub.id,
            "igdb_game_id": prefs.igdb_game_id,
            "platforms": len(due),
            "paused": pausing,
        },
    )
    return True


async def on_release_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete release alert from the fired-notify «Delete» button."""
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()
    user_id = query.from_user.id
    db: Database = context.application.bot_data["db"]
    lang = _user_lang(db, user_id)
    try:
        sub_id = int(query.data.rsplit(":", 1)[-1])
    except (TypeError, ValueError):
        return
    sub = db.get_subscription(sub_id, user_id)
    if sub is None or not is_release_watch_sub(sub):
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except BadRequest:
            pass
        return
    to_cart = beta_features.is_enabled(db, user_id, "deleted-subscriptions-cart")
    if not db.delete_subscription(sub_id, user_id, to_cart=to_cart):
        return
    done = t("subs_deleted", lang, count=1)
    try:
        if query.message is not None and query.message.photo:
            await query.edit_message_caption(caption=done, reply_markup=None)
        else:
            await query.edit_message_text(done)
    except BadRequest:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except BadRequest:
            pass
    analytics.capture(
        user_id,
        "release_watch_deleted_from_notify",
        {"subscription_id": sub_id},
    )


async def cancel_release_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    db: Database = context.application.bot_data["db"]
    user_id = query.from_user.id
    lang = _user_lang(db, user_id)
    chat_id = reply_chat_id(update)
    try:
        await query.edit_message_text(t("cancelled", lang))
    except BadRequest:
        pass
    context.user_data.clear()
    await context.bot.send_message(
        chat_id, t("menu_main", lang), reply_markup=_menu(lang, user_id)
    )
    return ConversationHandler.END


async def on_release_find_streams(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Already-out card CTA: live streams for this IGDB game with default WatchPrefs."""
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()
    user_id = query.from_user.id
    db: Database = context.application.bot_data["db"]
    lang = _user_lang(db, user_id)
    try:
        game_id = int((query.data or "").split(":")[-1])
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
            logger.exception("release find-streams Helix search failed game=%s", game_id)
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
            user_id,
            t("release_find_streams_none", lang, game=html.escape(game_name)),
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
        chat_id=user_id,
        user_id=user_id,
        context=context,
        prefs=prefs,
    )
