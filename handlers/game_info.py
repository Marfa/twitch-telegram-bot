"""Browse IGDB game info from Other menu — search like release alerts, card like releases."""
from __future__ import annotations

import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler

from bot_helpers import _menu, reply_chat_id
from db import Database
from handlers.release_watch import (
    _send_game_card,
    release_game_pick_keyboard,
)
from i18n import DEFAULT_LOCALE, btn, t

logger = logging.getLogger(__name__)

_SEARCH_LIMIT = 100
_PICK_PAGE_SIZE = 5


def _user_lang(db: Database, user_id: int) -> str:
    return db.get_user_locale(user_id) or DEFAULT_LOCALE


def _wz() -> dict[str, int]:
    from bot import GAME_INFO_PICK, GAME_INFO_SEARCH

    return {"GAME_INFO_SEARCH": GAME_INFO_SEARCH, "GAME_INFO_PICK": GAME_INFO_PICK}


def _pick_keyboard(
    games: list[dict[str, Any]],
    lang: str,
    *,
    companies: dict[int, str] | None = None,
    page: int = 0,
) -> InlineKeyboardMarkup:
    """Reuse release pick UI but with ginfo: callback prefix."""
    markup = release_game_pick_keyboard(
        games, lang, companies=companies, page=page, page_size=_PICK_PAGE_SIZE
    )
    rows: list[list[InlineKeyboardButton]] = []
    for row in markup.inline_keyboard:
        new_row: list[InlineKeyboardButton] = []
        for b in row:
            cb = b.callback_data or ""
            if cb.startswith("rel:"):
                cb = "ginfo:" + cb[4:]
            new_row.append(InlineKeyboardButton(b.text, callback_data=cb))
        rows.append(new_row)
    return InlineKeyboardMarkup(rows)


async def start_game_info(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    db: Database = context.application.bot_data["db"]
    user_id = update.effective_user.id
    lang = _user_lang(db, user_id)
    chat_id = reply_chat_id(update)
    context.user_data.clear()
    prompt = t("game_info_prompt", lang)
    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    btn("wizard_cancel", lang), callback_data="ginfo:cancel"
                )
            ]
        ]
    )
    await context.bot.send_message(
        chat_id,
        prompt,
        reply_markup=markup,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    return _wz()["GAME_INFO_SEARCH"]


async def receive_game_info_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    db: Database = context.application.bot_data["db"]
    user_id = update.effective_user.id
    lang = _user_lang(db, user_id)
    query = (update.effective_message.text or "").strip()
    if not query:
        await update.effective_message.reply_text(
            t("game_info_prompt", lang),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return _wz()["GAME_INFO_SEARCH"]
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
                            btn("wizard_cancel", lang),
                            callback_data="ginfo:cancel",
                        )
                    ]
                ]
            ),
        )
        return _wz()["GAME_INFO_SEARCH"]
    companies = db.igdb_company_labels_for_games([int(g["id"]) for g in games])
    context.user_data["ginfo_search_hits"] = games
    context.user_data["ginfo_search_companies"] = companies
    context.user_data["ginfo_search_page"] = 0
    await status.edit_text(
        t("release_game_pick", lang),
        reply_markup=_pick_keyboard(games, lang, companies=companies, page=0),
    )
    return _wz()["GAME_INFO_PICK"]


async def receive_game_info_pick(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    db: Database = context.application.bot_data["db"]
    user_id = query.from_user.id
    lang = _user_lang(db, user_id)
    chat_id = reply_chat_id(update)
    data = query.data or ""
    if data == "ginfo:cancel":
        await query.edit_message_text(t("cancelled", lang))
        context.user_data.clear()
        await context.bot.send_message(
            chat_id, t("menu_main", lang), reply_markup=_menu(lang, user_id)
        )
        return ConversationHandler.END
    if data == "ginfo:page:noop":
        return _wz()["GAME_INFO_PICK"]
    if data.startswith("ginfo:page:"):
        try:
            page = int(data.rsplit(":", 1)[1])
        except ValueError:
            return _wz()["GAME_INFO_PICK"]
        games = context.user_data.get("ginfo_search_hits") or []
        if not isinstance(games, list) or not games:
            await query.edit_message_text(t("release_game_not_found", lang))
            return _wz()["GAME_INFO_SEARCH"]
        companies = context.user_data.get("ginfo_search_companies") or {}
        if not isinstance(companies, dict):
            companies = {}
        context.user_data["ginfo_search_page"] = page
        try:
            await query.edit_message_reply_markup(
                reply_markup=_pick_keyboard(
                    games, lang, companies=companies, page=page
                )
            )
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise
        return _wz()["GAME_INFO_PICK"]
    if not data.startswith("ginfo:pick:"):
        return _wz()["GAME_INFO_PICK"]
    try:
        game_id = int(data.split(":")[-1])
    except ValueError:
        return _wz()["GAME_INFO_PICK"]
    game = db.igdb_game_by_id(game_id)
    if not game:
        await query.edit_message_text(t("release_game_not_found", lang))
        return _wz()["GAME_INFO_SEARCH"]
    name = str(game.get("name") or "").strip() or f"#{game_id}"
    summary = str(game.get("summary") or "")
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
        body_html=None,
        lang=lang,
        reply_markup=find_kb,
    )
    await context.bot.send_message(
        chat_id, t("menu_main", lang), reply_markup=_menu(lang, user_id)
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_game_info_callback(
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
