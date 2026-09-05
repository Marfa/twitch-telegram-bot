"""Shared create/edit UI for custom alert URL buttons."""
from __future__ import annotations

import html
from typing import Awaitable, Callable

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import beta as beta_features
import custom_buttons as cbtn
import premium as prem
from bot_helpers import _user_lang, reply_chat_id
from db import Database
from i18n import all_wizard_nav_buttons, custom_buttons_keyboard, is_menu_button, t

OnDone = Callable[[Update, ContextTypes.DEFAULT_TYPE, str], Awaitable[int]]
OnBack = Callable[[Update, ContextTypes.DEFAULT_TYPE, str], Awaitable[int]]


def _ud_buttons(context: ContextTypes.DEFAULT_TYPE) -> list[dict[str, str]]:
    raw = context.user_data.get("custom_buttons_list")
    if isinstance(raw, list):
        return [b for b in raw if isinstance(b, dict)]
    return []


def _set_ud_buttons(
    context: ContextTypes.DEFAULT_TYPE, buttons: list[dict[str, str]]
) -> None:
    context.user_data["custom_buttons_list"] = buttons
    context.user_data["custom_buttons"] = cbtn.dump_custom_buttons(buttons)


def _prompt_text(lang: str, buttons: list[dict[str, str]], *, awaiting: bool) -> str:
    if awaiting:
        return t("custom_buttons_input_prompt", lang)
    key = "custom_buttons_prompt" if buttons else "custom_buttons_prompt_empty"
    return t(key, lang, max=cbtn.CUSTOM_BUTTONS_MAX)


async def show_custom_buttons_screen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    lang: str,
    *,
    state: int,
    show_skip: bool = False,
    show_back: bool = True,
    edit_message: bool = False,
) -> int:
    buttons = _ud_buttons(context)
    awaiting = bool(context.user_data.get("cbtn_awaiting"))
    text = _prompt_text(lang, buttons, awaiting=awaiting)
    markup = custom_buttons_keyboard(
        lang,
        buttons,
        awaiting_input=awaiting,
        show_skip=show_skip,
        show_back=show_back,
        show_cancel=True,
    )
    chat_id = reply_chat_id(update)
    if edit_message and update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=markup, parse_mode=ParseMode.HTML
            )
            return state
        except Exception:
            pass
    if update.callback_query:
        await context.bot.send_message(
            chat_id, text, reply_markup=markup, parse_mode=ParseMode.HTML
        )
    else:
        await update.effective_message.reply_text(
            text, reply_markup=markup, parse_mode=ParseMode.HTML
        )
    return state


async def maybe_entitled_custom_buttons(
    bot,
    db: Database,
    user_id: int,
    *,
    channel: str | None,
) -> bool:
    """Beta enrolled + Premium advanced_mode (or legacy custom_buttons grant)."""
    if not beta_features.is_enabled(db, user_id, cbtn.BETA_FEATURE_ID):
        return False
    return await prem.has_feature(bot, db, user_id, cbtn.FEATURE_ID, channel=channel)


async def receive_custom_buttons_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    state: int,
    on_done: OnDone,
    on_back: OnBack | None = None,
    on_cancel: Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[int]] | None = None,
    show_skip: bool = False,
    persist: Callable[[ContextTypes.DEFAULT_TYPE], None] | None = None,
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)

    parts = (query.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "cancel" and on_cancel:
        return await on_cancel(update, context)
    if action == "back":
        if context.user_data.get("cbtn_awaiting"):
            context.user_data.pop("cbtn_awaiting", None)
            context.user_data.pop("cbtn_edit_index", None)
            return await show_custom_buttons_screen(
                update,
                context,
                lang,
                state=state,
                show_skip=show_skip,
                edit_message=True,
            )
        if on_back:
            return await on_back(update, context, lang)
        return state
    if action == "done":
        context.user_data.pop("cbtn_awaiting", None)
        context.user_data.pop("cbtn_edit_index", None)
        _set_ud_buttons(context, _ud_buttons(context))
        if persist:
            persist(context)
        try:
            await query.edit_message_text("✓")
        except Exception:
            pass
        return await on_done(update, context, lang)
    if action == "list":
        context.user_data.pop("cbtn_awaiting", None)
        context.user_data.pop("cbtn_edit_index", None)
        return await show_custom_buttons_screen(
            update,
            context,
            lang,
            state=state,
            show_skip=show_skip,
            edit_message=True,
        )
    if action == "add":
        buttons = _ud_buttons(context)
        if len(buttons) >= cbtn.CUSTOM_BUTTONS_MAX:
            await query.answer(
                t("custom_buttons_full", lang, max=cbtn.CUSTOM_BUTTONS_MAX),
                show_alert=True,
            )
            return state
        context.user_data["cbtn_awaiting"] = "add"
        context.user_data.pop("cbtn_edit_index", None)
        return await show_custom_buttons_screen(
            update,
            context,
            lang,
            state=state,
            show_skip=show_skip,
            edit_message=True,
        )
    if action == "edit" and len(parts) > 2:
        try:
            idx = int(parts[2])
        except ValueError:
            return state
        buttons = _ud_buttons(context)
        if idx < 0 or idx >= len(buttons):
            return state
        context.user_data["cbtn_awaiting"] = "edit"
        context.user_data["cbtn_edit_index"] = idx
        return await show_custom_buttons_screen(
            update,
            context,
            lang,
            state=state,
            show_skip=show_skip,
            edit_message=True,
        )
    if action == "del" and len(parts) > 2:
        try:
            idx = int(parts[2])
        except ValueError:
            return state
        buttons = _ud_buttons(context)
        if 0 <= idx < len(buttons):
            del buttons[idx]
            _set_ud_buttons(context, buttons)
            if persist:
                persist(context)
        return await show_custom_buttons_screen(
            update,
            context,
            lang,
            state=state,
            show_skip=show_skip,
            edit_message=True,
        )
    return state


async def receive_custom_buttons_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    state: int,
    show_skip: bool = False,
    persist: Callable[[ContextTypes.DEFAULT_TYPE], None] | None = None,
) -> int:
    lang = _user_lang(context, update.effective_user.id)
    text = (update.effective_message.text or "").strip()
    if text in all_wizard_nav_buttons():
        return state
    if is_menu_button(text):
        await update.effective_message.reply_text(t("finish_setup_first", lang))
        return state

    parsed = cbtn.parse_button_line(text)
    if not parsed:
        await update.effective_message.reply_text(
            t("custom_buttons_invalid", lang),
            parse_mode=ParseMode.HTML,
        )
        return state
    anchor, url = parsed
    buttons = _ud_buttons(context)
    mode = context.user_data.get("cbtn_awaiting")
    if mode == "edit":
        idx = int(context.user_data.get("cbtn_edit_index", -1))
        if 0 <= idx < len(buttons):
            buttons[idx] = {"text": anchor, "url": url}
        else:
            buttons.append({"text": anchor, "url": url})
    else:
        if len(buttons) >= cbtn.CUSTOM_BUTTONS_MAX:
            await update.effective_message.reply_text(
                t("custom_buttons_full", lang, max=cbtn.CUSTOM_BUTTONS_MAX)
            )
            return state
        buttons.append({"text": anchor, "url": url})
    _set_ud_buttons(context, buttons)
    context.user_data.pop("cbtn_awaiting", None)
    context.user_data.pop("cbtn_edit_index", None)
    if persist:
        persist(context)
    return await show_custom_buttons_screen(
        update,
        context,
        lang,
        state=state,
        show_skip=show_skip,
        edit_message=False,
    )


def buttons_summary_html(buttons: list[dict[str, str]], lang: str) -> str:
    if not buttons:
        return ""
    lines = [t("sub_list_custom_buttons", lang, count=len(buttons))]
    for i, btn in enumerate(buttons[: cbtn.CUSTOM_BUTTONS_MAX], start=1):
        lines.append(f"  {i}. {html.escape(btn.get('text') or '')}")
    return "\n".join(lines)
