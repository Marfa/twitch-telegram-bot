"""Settings → default stream alert settings editor + apply/lock helpers."""

from __future__ import annotations

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler

import custom_buttons as cbtn
import multistream as ms
import premium as prem
from bot_helpers import _settings_kb, _user_lang, reply_chat_id
from db import Database
from i18n import edit_options_keyboard, t
from stream_alert_defaults import (
    draft_as_subscription,
    ensure_defaults_draft,
    keep_defaults_editor_state,
    subscription_update_from_defaults,
    update_defaults_draft,
)


def defaults_options_markup(
    context: ContextTypes.DEFAULT_TYPE, lang: str, user_id: int, db: Database
):
    import beta as beta_features
    import donationalerts as da

    draft = ensure_defaults_draft(context.user_data, db, user_id)
    sub = draft_as_subscription(draft, owner_id=user_id)
    show_custom_buttons = beta_features.is_enabled(db, user_id, cbtn.BETA_FEATURE_ID)
    show_top_donations = beta_features.is_enabled(db, user_id, da.BETA_FEATURE_ID)
    show_live_remind = beta_features.is_enabled(db, user_id, "live-remind-button")
    from twitch import template_has_link

    return edit_options_keyboard(
        0,
        lang,
        dest_type="channel",
        delete_previous=bool(sub.delete_previous),
        pin_message=bool(sub.pin_message),
        delete_other_alerts=bool(sub.delete_other_alerts),
        has_image=bool(sub.image_file_id),
        strip_name_mentions=bool(sub.strip_name_mentions),
        attach_chat_button=bool(sub.attach_chat_button),
        attach_live_remind_button=bool(sub.attach_live_remind_button),
        top_donations=bool(sub.top_donations),
        disable_link_preview=bool(sub.disable_link_preview),
        schedule_reminder_minutes=int(sub.schedule_reminder_minutes or 0),
        show_link_preview=not bool(sub.image_file_id)
        and template_has_link(sub.message_template or ""),
        schedule_reminder_configured=True,
        notify_on_category_change=False,
        notify_on_end=False,
        is_upcoming=False,
        show_advanced=True,
        show_custom_buttons=show_custom_buttons,
        show_live_remind=show_live_remind,
        notify_on_schedule_cancel=bool(sub.notify_on_schedule_cancel)
        and bool((sub.schedule_cancel_template or "").strip()),
        show_schedule_cancel=True,
        show_multistream=True,
        show_top_donations=show_top_donations,
        button_style=str(sub.button_style or ""),
        custom_buttons_count=len(cbtn.parse_custom_buttons(sub.custom_buttons)),
        multistream_count=len(ms.parse_multistream_channels(sub.multistream_channels)),
        for_defaults_editor=True,
        show_apply_defaults=False,
    )


async def show_defaults_editor(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    edit_message: bool = False,
) -> None:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    ensure_defaults_draft(context.user_data, db, user_id)
    text = t("stream_defaults_menu", lang)
    markup = defaults_options_markup(context, lang, user_id, db)
    if edit_message and update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=markup, parse_mode=ParseMode.HTML
            )
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise
        return
    chat_id = reply_chat_id(update)
    await context.bot.send_message(
        chat_id,
        text,
        reply_markup=markup,
        parse_mode=ParseMode.HTML,
    )


async def open_stream_defaults_editor(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    context.user_data.clear()
    context.user_data["editing_defaults"] = True
    ensure_defaults_draft(context.user_data, db, user_id)
    await update.effective_message.reply_text(
        t("menu_settings", lang),
        reply_markup=_settings_kb(lang, db, user_id),
    )
    await show_defaults_editor(update, context)


async def reshow_defaults_after_edit(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    kept = keep_defaults_editor_state(context.user_data)
    context.user_data.clear()
    context.user_data.update(kept)
    context.user_data["editing_defaults"] = True
    await context.bot.send_message(
        reply_chat_id(update),
        t("stream_defaults_field_saved", lang),
    )
    await show_defaults_editor(update, context)
    return ConversationHandler.END


async def on_defedit_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int | None:
    """Handle defedit:0:<field> — toggles, save/cancel, or hand off to edit starters."""
    query = update.callback_query
    lang = _user_lang(context, query.from_user.id)
    parts = (query.data or "").split(":")
    if len(parts) < 3:
        await query.answer()
        return ConversationHandler.END
    field = parts[2]
    db: Database = context.application.bot_data["db"]
    user_id = query.from_user.id
    draft = ensure_defaults_draft(context.user_data, db, user_id)

    if field == "cancel":
        await query.answer()
        context.user_data.clear()
        try:
            await query.edit_message_text("✓")
        except BadRequest:
            pass
        await context.bot.send_message(
            reply_chat_id(update),
            t("cancelled", lang),
            reply_markup=_settings_kb(lang, db, user_id),
        )
        return ConversationHandler.END

    if field == "save":
        await query.answer()
        db.set_stream_alert_defaults(user_id, draft)
        n = db.resync_bound_stream_alert_defaults(user_id)
        context.user_data.clear()
        try:
            await query.edit_message_text("✓")
        except BadRequest:
            pass
        note = t("stream_defaults_saved", lang)
        if n:
            note = f"{note}\n{t('stream_defaults_synced', lang, count=n)}"
        await context.bot.send_message(
            reply_chat_id(update),
            note,
            reply_markup=_settings_kb(lang, db, user_id),
        )
        return ConversationHandler.END

    # Instant toggles on draft
    toggle_map = {
        "strip": ("strip_name_mentions", None),
        "chat_button": ("attach_chat_button", "disable_link_preview"),
        "live_remind": ("attach_live_remind_button", None),
        "pin_message": ("pin_message", None),
        "delete_old": ("delete_previous", "notify_delete_fail"),
        "delete_other": ("delete_other_alerts", None),
        "preview": ("disable_link_preview", None),
        "top_donations": ("top_donations", None),
        "schedule_cancel": ("notify_on_schedule_cancel", None),
    }
    if field in toggle_map:
        key, _linked = toggle_map[field]
        if field == "preview":
            # ✅ means preview on → disable_link_preview is False; click flips it.
            preview_on = not bool(draft.get("disable_link_preview"))
            update_defaults_draft(
                context.user_data, disable_link_preview=preview_on
            )
        elif field == "delete_old":
            new_val = not bool(draft.get("delete_previous"))
            kwargs = {
                "delete_previous": new_val,
                "notify_delete_fail": new_val,
            }
            if not new_val:
                kwargs["delete_other_alerts"] = False
            update_defaults_draft(context.user_data, **kwargs)
        elif field == "chat_button":
            new_val = not bool(draft.get("attach_chat_button"))
            kwargs: dict = {"attach_chat_button": new_val}
            if new_val:
                kwargs["disable_link_preview"] = True
            update_defaults_draft(context.user_data, **kwargs)
        elif field == "top_donations":
            new_val = not bool(draft.get("top_donations"))
            if new_val and not str(draft.get("top_donations_template") or "").strip():
                await query.answer()
                return await _handoff_edit_field(update, context, "top_donations")
            kwargs = {"top_donations": new_val}
            if not new_val:
                kwargs["top_donations_template"] = ""
            update_defaults_draft(context.user_data, **kwargs)
        elif field == "schedule_cancel":
            new_val = not bool(draft.get("notify_on_schedule_cancel"))
            if new_val and not str(draft.get("schedule_cancel_template") or "").strip():
                await query.answer()
                return await _handoff_edit_field(update, context, "schedule_cancel")
            kwargs = {"notify_on_schedule_cancel": new_val}
            if not new_val:
                kwargs["schedule_cancel_template"] = ""
            update_defaults_draft(context.user_data, **kwargs)
        else:
            new_val = not bool(draft.get(key))
            update_defaults_draft(context.user_data, **{key: new_val})
        await query.answer()
        await show_defaults_editor(update, context, edit_message=True)
        return ConversationHandler.END

    if field == "image_del":
        update_defaults_draft(
            context.user_data, image_file_id=None, image_position=""
        )
        await query.answer()
        await show_defaults_editor(update, context, edit_message=True)
        return ConversationHandler.END

    # Complex fields → reuse existing edit ConversationHandler entry points
    await query.answer()
    return await _handoff_edit_field(update, context, field)


async def _handoff_edit_field(
    update: Update, context: ContextTypes.DEFAULT_TYPE, field: str
) -> int:
    """Call the matching edit starter with defaults-edit flags set.

    Leave ``query.data`` as ``defedit:0:<field>`` — CallbackQuery is frozen in
    PTB 21+, and starters that support defaults already honor
    ``editing_defaults`` / the ``defedit:`` prefix (``split(':')[1]`` is still 0).
    """
    query = update.callback_query
    context.user_data["editing_defaults"] = True
    context.user_data["edit_sub_id"] = 0
    context.user_data["wizard_edit"] = True

    from handlers import subscriptions as subs
    from bot import start_edit_delay, start_edit_image, start_edit_schedule_reminder

    starters = {
        "template": subs.start_edit_template,
        "image": start_edit_image,
        "ignore_keywords": subs.start_edit_ignore_keywords,
        "category_filter": subs.start_edit_category_filter,
        "delay": start_edit_delay,
        "repeat": subs.start_edit_repeat_mute,
        "custom_buttons": subs.start_edit_custom_buttons,
        "multistream": subs.start_edit_multistream,
        "sched_remind": start_edit_schedule_reminder,
        "schedule_cancel": subs.start_edit_schedule_cancel
        if hasattr(subs, "start_edit_schedule_cancel")
        else None,
        "top_donations": subs.start_edit_top_donations,
        "button_style": subs.on_edit_bool_menu,
    }
    starter = starters.get(field)
    if starter is None:
        from bot import start_edit_schedule_cancel

        if field == "schedule_cancel":
            starter = start_edit_schedule_cancel
    if starter is None:
        await query.answer()
        return ConversationHandler.END
    return await starter(update, context)


async def on_defaults_locked(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    lang = _user_lang(context, query.from_user.id)
    await query.answer(t("stream_defaults_locked", lang), show_alert=True)


async def on_edit_apply_defaults(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    lang = _user_lang(context, query.from_user.id)
    parts = (query.data or "").split(":")
    if len(parts) < 3:
        await query.answer()
        return
    sub_id = int(parts[1])
    db: Database = context.application.bot_data["db"]
    sub = db.get_subscription(sub_id, query.from_user.id)
    if not sub:
        await query.answer()
        await query.edit_message_text(t("sub_not_found", lang))
        return
    from handlers.subscriptions import _alert_type_from_sub, _edit_menu_text, _owner_sub_number
    from bot import _edit_options_for_sub

    turning_on = not bool(getattr(sub, "use_stream_defaults", False))
    if turning_on:
        defaults = db.get_stream_alert_defaults(query.from_user.id)
        kind = _alert_type_from_sub(sub)
        fields = subscription_update_from_defaults(defaults, alert_type=kind)
        db.update_subscription(sub_id, query.from_user.id, **fields)
    else:
        db.update_subscription(
            sub_id, query.from_user.id, use_stream_defaults=False
        )
    sub = db.get_subscription(sub_id, query.from_user.id) or sub
    show_adv = await prem.advanced_mode_on(
        context.bot, db, query.from_user.id, channel=sub.twitch_username
    )
    await query.answer()
    await query.edit_message_text(
        _edit_menu_text(
            lang,
            sub_id=_owner_sub_number(db, query.from_user.id, sub_id),
            username=sub.twitch_username,
            show_advanced=show_adv,
        ),
        reply_markup=_edit_options_for_sub(sub, lang, show_advanced=show_adv, db=db),
        parse_mode=ParseMode.HTML,
    )


async def on_advopt_locked(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    lang = _user_lang(context, query.from_user.id)
    await query.answer(t("stream_defaults_locked", lang), show_alert=True)
    from handlers.wizard import _wz

    return _wz()["ADVANCED_OPTIONS"]
