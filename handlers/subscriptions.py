from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timedelta, timezone

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import Application, ContextTypes, ConversationHandler

import analytics
import beta as beta_features
import demo_mode
import premium as prem
from bot_helpers import (
    _inline_btn_label,
    _menu,
    _pause_notifications_enabled,
    _pulse_wizard_keyboard,
    _settings_kb,
    _user_lang,
    _user_notifications_paused,
    _wizard,
)
from db import Database, Subscription, TwitchSync, is_category_watch_sub
from db.models import (
    _subscription_cart_snapshot,
    alert_type_from_payload,
    migrate_sub_fields_for_alert_type,
)
from handlers.delivery import _resolve_chat_display_name
from handlers.settings import complete_chat_oauth, complete_whisper_oauth
from handlers.stream_schedule import _complete_schedule_publish
from i18n import (
    DEFAULT_LOCALE,
    SCHEDULE_TZ,
    all_wizard_nav_buttons,
    btn,
    dest_keyboard,
    dest_label,
    edit_bool_keyboard,
    ignore_keywords_keyboard,
    import_mode_keyboard,
    is_menu_button,
    subscriptions_menu,
    sync_settings_keyboard,
    sync_unfollow_keyboard,
    delete_all_confirm_keyboard,
    t,
    t_bullet,
)
from twitch import TwitchClient, is_game_cover_image, normalize_ignore_keywords, template_has_link

logger = logging.getLogger(__name__)

_SUBS_LIST_PAGE_LIMIT = 4000
_PICK_PAGE_SIZE = 8
_SHARE_BETA_ID = "share-alerts"


def _sub_states() -> dict[str, int]:
    from bot import (
        DEST_TYPE,
        EDIT_IGNORE_KEYWORDS,
        EDIT_REPEAT,
        EDIT_TEMPLATE,
        PAUSE_ALERTS_DAYS,
        SYNC_DAYS,
    )

    return {
        "DEST_TYPE": DEST_TYPE,
        "EDIT_IGNORE_KEYWORDS": EDIT_IGNORE_KEYWORDS,
        "EDIT_REPEAT": EDIT_REPEAT,
        "EDIT_TEMPLATE": EDIT_TEMPLATE,
        "PAUSE_ALERTS_DAYS": PAUSE_ALERTS_DAYS,
        "SYNC_DAYS": SYNC_DAYS,
    }


def _edit_menu_text(*args, **kwargs):
    from bot import _edit_menu_text as _impl

    return _impl(*args, **kwargs)


def _edit_options_for_sub(*args, **kwargs):
    from bot import _edit_options_for_sub as _impl

    return _impl(*args, **kwargs)


async def _prompt_edit_template(*args, **kwargs):
    from bot import _prompt_edit_template as _impl

    return await _impl(*args, **kwargs)


def _ignore_keywords_current_label(*args, **kwargs):
    from bot import _ignore_keywords_current_label as _impl

    return _impl(*args, **kwargs)


def _repeat_current_label(*args, **kwargs):
    from bot import _repeat_current_label as _impl

    return _impl(*args, **kwargs)


def _set_wizard_back(*args, **kwargs):
    from handlers.wizard import _set_wizard_back as _impl

    return _impl(*args, **kwargs)


async def _show_premium_gate(*args, **kwargs):
    from handlers.wizard import _show_premium_gate as _impl

    return await _impl(*args, **kwargs)

_PENDING_IMPORT_TTL_SEC = 1800
_SYNC_PERIOD_MIN = 1
_SYNC_PERIOD_MAX = 365
_PAUSE_DAYS_MAX = 365


def _owner_sub_number(db: Database, owner_id: int, sub_id: int) -> int:
    for index, sub in enumerate(_subs_for_owner(db, owner_id), 1):
        if sub.id == sub_id:
            return index
    return sub_id


def _subs_for_owner(db: Database, owner_id: int) -> list[Subscription]:
    """Real subs normally; only demo rows while Demo mode is on."""
    demo = demo_mode.is_active(owner_id)
    return [s for s in db.get_subscriptions_by_owner(owner_id) if bool(s.is_demo) == demo]


def _sub_in_current_mode(sub: Subscription, owner_id: int) -> bool:
    return bool(sub.is_demo) == demo_mode.is_active(owner_id)


_CART_BETA_ID = "deleted-subscriptions-cart"


def _deleted_subscriptions_cart_enabled(db: Database, user_id: int) -> bool:
    return beta_features.is_enabled(db, user_id, _CART_BETA_ID)


def _format_pause_until(until_ts: int, lang: str) -> str:
    dt = datetime.fromtimestamp(until_ts, tz=timezone.utc).astimezone(SCHEDULE_TZ)
    return f"{dt.day:02d}.{dt.month:02d}.{dt.year} {dt.hour:02d}:{dt.minute:02d}"


def _subs_kb(lang: str, db: Database, user_id: int) -> ReplyKeyboardMarkup:
    return subscriptions_menu(
        lang,
        cart=_deleted_subscriptions_cart_enabled(db, user_id),
        pause_notifications=_pause_notifications_enabled(db, user_id),
    )


def import_followed_as_subscriptions(
    db: Database,
    owner_id: int,
    followed: list[dict],
    *,
    template: str,
    limit: int,
    prune_missing: bool = False,
    enabled: bool = False,
    is_demo: bool = False,
    delete_to_cart: bool = False,
) -> tuple[int, int, int, list[str], list[Subscription], list[dict[str, str]]]:
    """Create DM subscriptions from Helix followed channels.

    Import path keeps enabled=False (paused). Periodic sync passes enabled=True.
    New rows are marked from_twitch_sync=True. When prune_missing=True, pristine
    sync-origin subs absent from follows are deleted; edited/manual leftovers are
    returned in ask_streamers for user confirmation.
    Returns (imported, skipped, limited, removed_names, new_subs, ask_streamers).
    """
    existing_subs = [
        s for s in db.get_subscriptions_by_owner(owner_id) if s.is_demo is is_demo
    ]
    existing = {s.twitch_user_id for s in existing_subs}
    count = len(existing)
    imported = skipped = limited = 0
    new_subs: list[Subscription] = []
    seen_follow: set[str] = set()
    follow_ids: set[str] = set()
    for channel in followed:
        twitch_user_id = str(channel.get("broadcaster_id") or "")
        login = str(channel.get("broadcaster_login") or "").strip().lower()
        if not twitch_user_id or not login:
            continue
        if twitch_user_id in seen_follow:
            continue
        seen_follow.add(twitch_user_id)
        follow_ids.add(twitch_user_id)
        if twitch_user_id in existing:
            skipped += 1
            continue
        if count >= limit:
            limited += 1
            continue
        sub_enabled = False
        if enabled:
            sub_enabled = prem.may_enable_subscription(
                db, owner_id, demo=is_demo, twitch_username=login
            )
        sub_id = db.add_subscription(
            owner_id=owner_id,
            twitch_username=login,
            twitch_user_id=twitch_user_id,
            message_template=template,
            dest_type="dm",
            chat_id=owner_id,
            thread_id=None,
            disable_link_preview=True,
            enabled=sub_enabled,
            from_twitch_sync=True,
            is_demo=is_demo,
        )
        sub = db.get_subscription(sub_id, owner_id)
        if sub:
            new_subs.append(sub)
        existing.add(twitch_user_id)
        count += 1
        imported += 1
    removed_names: list[str] = []
    ask_streamers: list[dict[str, str]] = []
    if prune_missing:
        sync = db.get_twitch_sync(owner_id)
        if sync and sync.twitch_user_id:
            follow_ids.add(str(sync.twitch_user_id))
        removed_names = db.delete_synced_subscriptions_missing(
            owner_id, follow_ids, to_cart=delete_to_cart
        )
        ask_streamers = db.get_unfollowed_manual_alert_streamers(
            owner_id, follow_ids, is_demo=is_demo
        )
    return imported, skipped, limited, removed_names, new_subs, ask_streamers


LEGACY_IMPORT_TEMPLATES = frozenset(
    {
        "Streamer {username} went live with {game}",
        "Стример {username} вышел в эфир с игрой {game}",
    }
)


def migrate_import_sync_subscriptions(
    db: Database, *, dry_run: bool = False
) -> tuple[int, int]:
    """Refresh import/sync subs still on the legacy default template; disable link preview.

    Returns (templates_updated, preview_updated).
    """
    templates_updated = preview_updated = 0
    for owner_id in db.get_all_owner_ids():
        locale = db.get_user_locale(owner_id) or DEFAULT_LOCALE
        lang = "ru" if str(locale).lower().startswith("ru") else "en"
        new_template = t("import_default_template", lang)
        for sub in db.get_subscriptions_by_owner(owner_id):
            if not sub.from_twitch_sync:
                continue
            fields: dict[str, object] = {}
            if sub.message_template in LEGACY_IMPORT_TEMPLATES:
                fields["message_template"] = new_template
            if not sub.disable_link_preview:
                fields["disable_link_preview"] = True
            if not fields:
                continue
            if dry_run:
                if "message_template" in fields:
                    templates_updated += 1
                if "disable_link_preview" in fields:
                    preview_updated += 1
                continue
            if db.update_subscription(
                sub.id, owner_id, mark_sync_edited=False, **fields
            ):
                if "message_template" in fields:
                    templates_updated += 1
                if "disable_link_preview" in fields:
                    preview_updated += 1
    return templates_updated, preview_updated


def _import_result_keyboard(
    lang: str, subs: list[Subscription]
) -> InlineKeyboardMarkup:
    """Post-import: enable + edit per channel (no shared-only keyboard)."""
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(t("enable_all", lang), callback_data="enable_all")]
    ]
    seen: set[str] = set()
    unique: list[Subscription] = []
    for sub in subs:
        if sub.twitch_user_id in seen:
            continue
        seen.add(sub.twitch_user_id)
        unique.append(sub)
    for i, sub in enumerate(unique, 1):
        tag = f"#{i} {sub.twitch_username}"
        rows.append(
            [
                InlineKeyboardButton(
                    _inline_btn_label(f"{t('toggle_on', lang)} {tag}"),
                    callback_data=f"imp_en:{sub.id}",
                ),
                InlineKeyboardButton(
                    _inline_btn_label(f"{t('sub_list_edit', lang)} {tag}"),
                    callback_data=f"edit:{sub.id}",
                ),
            ]
        )
    return InlineKeyboardMarkup(rows)


def _format_sub_line(
    sub: Subscription,
    lang: str,
    sub_num: int,
    *,
    chat_display: str | None = None,
    thread_display: str | None = None,
) -> str:
    # Order matches create wizard: image → ignore → preview → delay → repeat
    # → schedule reminder → dest → delete.
    status = "✅" if sub.enabled else "⏸"
    chat_label = html.escape(
        chat_display if chat_display is not None else str(sub.chat_id)
    )
    keywords = html.escape(sub.ignore_keywords or "")
    settings: list[str] = []
    if sub.notify_on_end:
        settings.append(t("sub_list_alert_end", lang))
    elif sub.notify_on_category_change:
        settings.append(t("sub_list_alert_category", lang))
    elif sub.schedule_reminder_minutes > 0 and not sub.notify_on_live:
        settings.append(t("sub_list_alert_upcoming", lang))
    else:
        settings.append(t("sub_list_alert_live", lang))
    if sub.image_file_id:
        if is_game_cover_image(sub.image_file_id):
            settings.append(t("sub_list_image_game_cover", lang))
        else:
            pos = (sub.image_position or "").strip()
            if pos == "after":
                settings.append(t_bullet("image_after_note", lang))
            else:
                settings.append(t_bullet("image_before_note", lang))
    else:
        settings.append(t("sub_list_image_no", lang))
    if sub.ignore_keywords.strip() and sub.use_global_ignore:
        settings.append(
            t_bullet("ignore_keywords_yes_global_note", lang, keywords=keywords)
        )
    elif sub.ignore_keywords.strip():
        settings.append(t_bullet("ignore_keywords_yes_note", lang, keywords=keywords))
    elif sub.use_global_ignore:
        settings.append(t_bullet("ignore_keywords_global_only_note", lang))
    else:
        settings.append(t_bullet("ignore_keywords_no_note", lang))
    if sub.image_file_id or template_has_link(sub.message_template or ""):
        settings.append(
            t_bullet("preview_off", lang)
            if sub.disable_link_preview or sub.image_file_id
            else t_bullet("preview_on", lang)
        )
    if sub.attach_chat_button:
        settings.append(t("sub_list_chat_button_yes", lang))
    is_upcoming = (
        sub.schedule_reminder_minutes > 0
        and not sub.notify_on_live
        and not sub.notify_on_end
        and not sub.notify_on_category_change
    )
    if not is_upcoming:
        settings.append(
            t_bullet("delay_yes_note", lang, minutes=sub.delay_minutes)
            if sub.delay_minutes > 0
            else t_bullet("delay_no_note", lang)
        )
        if not sub.notify_on_category_change and not sub.notify_on_end:
            settings.append(
                t("sub_list_repeat_mute", lang, minutes=sub.suppress_repeat_minutes)
                if sub.suppress_repeat_minutes > 0
                else t("sub_list_repeat_allow", lang)
            )
    if sub.schedule_reminder_configured:
        settings.append(
            t_bullet(
                "schedule_reminder_yes_note",
                lang,
                minutes=sub.schedule_reminder_minutes,
            )
            if sub.schedule_reminder_minutes > 0
            else t_bullet("schedule_reminder_no_note", lang)
        )
    settings.append(
        t(
            "sub_list_dest",
            lang,
            dest=html.escape(dest_label(sub.dest_type, lang)),
            chat_id=chat_label,
        )
    )
    if sub.thread_id:
        thread_label = html.escape(
            thread_display if thread_display is not None else str(sub.thread_id)
        )
        settings.append(t("sub_list_thread", lang, thread_id=thread_label))
    if sub.dest_type != "dm":
        settings.append(
            t("sub_list_delete_yes", lang)
            if sub.delete_previous
            else t("sub_list_delete_no", lang)
        )
        if sub.delete_previous and sub.notify_delete_fail:
            settings.append(t_bullet("delete_fail_yes_note", lang))
        if sub.delete_previous and sub.notify_on_category_change:
            settings.append(
                t("sub_list_delete_other_yes", lang)
                if sub.delete_other_alerts
                else t("sub_list_delete_other_no", lang)
            )
    uname = html.escape(sub.twitch_username or "")
    return (
        f"{status} #{sub_num} — {uname}\n"
        + "\n".join(f"   {line}" for line in settings)
    )


def _subs_page_nav_row(prefix: str, page: int, total: int) -> list[InlineKeyboardButton]:
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("‹", callback_data=f"{prefix}:{page - 1}"))
    nav.append(
        InlineKeyboardButton(f"{page + 1}/{total}", callback_data=f"{prefix}:noop")
    )
    if page < total - 1:
        nav.append(InlineKeyboardButton("›", callback_data=f"{prefix}:{page + 1}"))
    return nav


async def _bot_username(bot, bot_data: dict | None = None) -> str:
    if bot_data is not None:
        cached = bot_data.get("bot_username")
        if cached:
            return str(cached)
    me = await bot.get_me()
    username = (me.username or "").strip()
    if bot_data is not None and username:
        bot_data["bot_username"] = username
    return username


def _share_enabled(db: Database, owner_id: int) -> bool:
    return beta_features.is_enabled(db, owner_id, _SHARE_BETA_ID)


def _share_link_for_sub(
    db: Database, owner_id: int, sub: Subscription, bot_username: str
) -> str | None:
    if not bot_username:
        return None
    if not _share_enabled(db, owner_id):
        return None
    token = db.ensure_alert_share_token(
        owner_id, sub.id, _subscription_cart_snapshot(sub)
    )
    return f"https://t.me/{bot_username}?start=share_{token}"


def _alert_type_from_sub(sub: Subscription) -> str:
    if sub.notify_on_category_change:
        return "category"
    if sub.notify_on_end:
        return "end"
    if sub.schedule_reminder_minutes > 0 and not sub.notify_on_live:
        return "upcoming"
    return "live"


async def open_subscriptions_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Legacy entry (old «Manage subscriptions» keyboard) → my subscriptions list."""
    await list_subscriptions(update, context)


async def open_cart_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    if not _deleted_subscriptions_cart_enabled(db, user_id):
        await update.effective_message.reply_text(
            t("menu_subs", lang),
            reply_markup=_subs_kb(lang, db, user_id),
        )
        return
    is_demo = demo_mode.is_active(user_id)
    days = prem.deleted_subscriptions_cart_days(db, user_id)
    items = db.list_deleted_subscriptions(
        user_id, days=days, is_demo=is_demo, limit=100
    )
    types, kind, view = _store_delete_cart_state(
        context, items, days=days, kind=None, selected=set()
    )
    if not items:
        text = t("cart_empty", lang, days=days)
        markup = None
    elif kind is None and len(types) > 1:
        text = t("cart_type_pick", lang)
        markup = _alert_type_pick_keyboard(lang, types, "delete_cart_type")
    else:
        text = t("cart_prompt", lang, days=days)
        if not view:
            text = t("cart_empty", lang, days=days)
            markup = None
        else:
            markup = _delete_cart_keyboard(lang, view, set())
    # Keyboard switch first — same mobile gap as list_subscriptions if reversed.
    await update.effective_message.reply_text(
        t("menu_subs", lang),
        reply_markup=_subs_kb(lang, db, user_id),
    )
    await update.effective_message.reply_text(text, reply_markup=markup)


async def start_pause_notifications(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    if not _pause_notifications_enabled(db, user_id):
        return ConversationHandler.END
    text = t("pause_notifications_prompt", lang)
    until = db.get_notifications_paused_until(user_id)
    now_ts = int(datetime.now(timezone.utc).timestamp())
    if until > now_ts:
        text = (
            text
            + "\n\n"
            + t(
                "pause_notifications_current",
                lang,
                until=_format_pause_until(until, lang),
            )
        )
    await update.effective_message.reply_text(
        text,
        reply_markup=_wizard(lang, back=False),
    )
    return _sub_states()["PAUSE_ALERTS_DAYS"]


async def receive_pause_notifications_days(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    if not _pause_notifications_enabled(db, user_id):
        await update.effective_message.reply_text(
            t("menu_subs", lang),
            reply_markup=_subs_kb(lang, db, user_id),
        )
        return ConversationHandler.END
    raw = (update.effective_message.text or "").strip()
    if is_menu_button(raw) or raw in all_wizard_nav_buttons():
        await update.effective_message.reply_text(
            t("cancelled", lang),
            reply_markup=_subs_kb(lang, db, user_id),
        )
        return ConversationHandler.END
    if not raw.isdigit() or int(raw) > _PAUSE_DAYS_MAX:
        await update.effective_message.reply_text(
            t("pause_notifications_invalid", lang, max_days=_PAUSE_DAYS_MAX)
        )
        return _sub_states()["PAUSE_ALERTS_DAYS"]
    days = int(raw)
    if days == 0:
        db.set_notifications_paused_until(user_id, 0)
        text = t("pause_notifications_resumed", lang)
    else:
        until = int(
            (datetime.now(timezone.utc) + timedelta(days=days)).timestamp()
        )
        db.set_notifications_paused_until(user_id, until)
        text = t(
            "pause_notifications_applied",
            lang,
            days=days,
            until=_format_pause_until(until, lang),
        )
    analytics.capture(user_id, "notifications_paused", {"days": days})
    await update.effective_message.reply_text(
        text,
        reply_markup=_subs_kb(lang, db, user_id),
    )
    return ConversationHandler.END


async def cancel_pause_notifications(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    context.user_data.clear()
    await update.effective_message.reply_text(
        t("cancelled", lang),
        reply_markup=_subs_kb(lang, db, user_id),
    )
    return ConversationHandler.END


async def start_twitch_import(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from config import MAX_SUBSCRIPTIONS_PER_OWNER, twitch_oauth_redirect_uri
    from health import create_oauth_state

    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    redirect_uri = twitch_oauth_redirect_uri()
    if not redirect_uri:
        await update.effective_message.reply_text(
            t("import_oauth_unavailable", lang),
            reply_markup=_menu(lang, user_id),
        )
        return
    if len(_subs_for_owner(db, user_id)) >= MAX_SUBSCRIPTIONS_PER_OWNER:
        await update.effective_message.reply_text(
            t("sub_limit", lang, limit=MAX_SUBSCRIPTIONS_PER_OWNER),
            reply_markup=_menu(lang, user_id),
        )
        return
    twitch: TwitchClient = context.application.bot_data["twitch"]
    state = create_oauth_state(user_id, lang)
    url = twitch.build_authorize_url(redirect_uri=redirect_uri, state=state)
    analytics.capture(user_id, "twitch_import_started")
    prompt = t("import_oauth_prompt", lang)
    sync = db.get_twitch_sync(user_id)
    if sync and sync.period_days > 0:
        prompt += "\n\n" + t(
            "import_oauth_sync_note",
            lang,
            days=sync.period_days,
            btn_settings=btn("settings", lang),
        )
    await update.effective_message.reply_text(
        prompt,
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(t("import_oauth_button", lang), url=url)],
                [
                    InlineKeyboardButton(
                        btn("wizard_cancel", lang), callback_data="import_oauth:cancel"
                    )
                ],
            ]
        ),
    )


async def cancel_twitch_import(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    await query.edit_message_text(t("cancelled", lang))
    await context.bot.send_message(
        user_id, t("menu_subs", lang), reply_markup=_subs_kb(lang, db, user_id)
    )


async def _format_subs_overview_lines(
    bot,
    db: Database,
    owner_id: int,
    lang: str,
    *,
    subs: list[Subscription] | None = None,
    bot_username: str = "",
) -> tuple[list[str], list[Subscription]]:
    if subs is None:
        subs = _subs_for_owner(db, owner_id)
    lines: list[str] = []
    for sub in subs:
        sub_num = _owner_sub_number(db, owner_id, sub.id)
        try:
            chat_display = await _resolve_chat_display_name(bot, sub)
            lines.append(
                _format_sub_line(
                    sub,
                    lang,
                    sub_num,
                    chat_display=chat_display,
                )
            )
        except Exception:
            logger.exception("Failed to format subscription %s for list", sub.id)
            uname = html.escape(sub.twitch_username or "")
            lines.append(
                f"{'✅' if sub.enabled else '⏸'} #{sub_num} — {uname}"
            )
    return lines, subs


def _sub_action_tag(num: int, username: str) -> str:
    name = (username or "").strip()
    return f"#{num} {name}" if name else f"#{num}"


def _subs_toggle_keyboard(
    db: Database, owner_id: int, lang: str, subs: list[Subscription]
) -> list[list[InlineKeyboardButton]]:
    """Per subscription: two rows of actions (toggle/edit; delete[+share if beta])."""
    show_share = _share_enabled(db, owner_id)
    rows: list[list[InlineKeyboardButton]] = []
    for s in subs:
        num = _owner_sub_number(db, owner_id, s.id)
        tag = _sub_action_tag(num, s.twitch_username or "")
        toggle_label = (
            f"{t('toggle_off', lang) if s.enabled else t('toggle_on', lang)} {tag}"
        )
        rows.append(
            [
                InlineKeyboardButton(
                    _inline_btn_label(toggle_label),
                    callback_data=f"toggle:{s.id}",
                ),
                InlineKeyboardButton(
                    _inline_btn_label(f"{t('sub_list_edit', lang)} {tag}"),
                    callback_data=f"edit:{s.id}",
                ),
            ]
        )
        row2 = [
            InlineKeyboardButton(
                _inline_btn_label(f"{t('sub_list_delete', lang)} {tag}"),
                callback_data=f"list_del:{s.id}",
            )
        ]
        if show_share:
            row2.append(
                InlineKeyboardButton(
                    _inline_btn_label(f"{t('sub_list_share_short', lang)} {tag}"),
                    callback_data=f"share_show:{s.id}",
                )
            )
        rows.append(row2)
    return rows


def _build_subs_list_pages(
    title: str,
    blocks: list[tuple[str, Subscription]],
) -> list[tuple[str, list[Subscription]]]:
    pages: list[tuple[str, list[Subscription]]] = []
    buf = title
    page_subs: list[Subscription] = []
    for block, sub in blocks:
        candidate = f"{buf}\n{block}" if buf else block
        if buf and page_subs and len(candidate) > _SUBS_LIST_PAGE_LIMIT:
            pages.append((buf, page_subs))
            buf = f"{title}{block}" if title else block
            page_subs = [sub]
            if len(buf) > _SUBS_LIST_PAGE_LIMIT:
                buf = buf[: _SUBS_LIST_PAGE_LIMIT - 1].rstrip() + "…"
        else:
            buf = (
                candidate
                if len(candidate) <= _SUBS_LIST_PAGE_LIMIT
                else candidate[: _SUBS_LIST_PAGE_LIMIT - 1].rstrip() + "…"
            )
            page_subs.append(sub)
    if buf or page_subs:
        pages.append((buf, page_subs))
    return pages or [(title.strip() or "—", [])]


def _subs_list_keyboard(
    db: Database,
    owner_id: int,
    lang: str,
    page_subs: list[Subscription],
    page: int,
    total: int,
    *,
    actions: bool = True,
) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    if actions:
        rows.extend(_subs_toggle_keyboard(db, owner_id, lang, page_subs))
    if total > 1:
        rows.append(_subs_page_nav_row("list_page", page, total))
    if not rows:
        return None
    return InlineKeyboardMarkup(rows)


async def _deliver_subs_list(
    *,
    bot,
    db: Database,
    owner_id: int,
    lang: str,
    subs: list[Subscription],
    reply_message,
    query=None,
    context: ContextTypes.DEFAULT_TYPE | None = None,
) -> None:
    bot_data = context.application.bot_data if context is not None else None
    bot_username = await _bot_username(bot, bot_data)
    lines, ordered = await _format_subs_overview_lines(
        bot, db, owner_id, lang, subs=subs, bot_username=bot_username
    )
    title = t("subs_list", lang)
    blocks = list(zip(lines, ordered))
    pages = _build_subs_list_pages(title, blocks)
    if context is not None:
        context.user_data["list_pages"] = pages
        context.user_data["list_page"] = 0
    text, page_subs = pages[0]
    markup = _subs_list_keyboard(
        db, owner_id, lang, page_subs, 0, len(pages), actions=True
    )
    try:
        if query is not None:
            await query.edit_message_text(
                text,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        else:
            await reply_message.reply_text(
                text,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
    except (BadRequest, Forbidden):
        logger.exception("Failed to send subscriptions list to %s", owner_id)
        try:
            await reply_message.reply_text(
                title.strip() or "—",
                reply_markup=markup,
            )
        except (BadRequest, Forbidden):
            pass


async def on_list_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.data.endswith(":noop"):
        return
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    try:
        page = int(query.data.rsplit(":", 1)[1])
    except (TypeError, ValueError, IndexError):
        return
    pages = context.user_data.get("list_pages")
    if not isinstance(pages, list) or not pages:
        return
    page = max(0, min(page, len(pages) - 1))
    context.user_data["list_page"] = page
    text, page_subs = pages[page]
    db: Database = context.application.bot_data["db"]
    markup = _subs_list_keyboard(
        db, user_id, lang, page_subs, page, len(pages), actions=True
    )
    try:
        await query.edit_message_text(
            text,
            reply_markup=markup,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


async def on_list_page_noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()


async def list_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    subs = _subs_for_owner(db, user_id)
    if not subs:
        await update.effective_message.reply_text(
            t("no_subs", lang),
            reply_markup=_subs_kb(lang, db, user_id),
        )
        return

    # Reply keyboard must be a separate message (can't mix with inline).
    # Send it first so the last bubble near the input is the type picker / list —
    # otherwise mobile shows a huge empty gap under a trailing "Мои подписки:".
    await update.effective_message.reply_text(
        t("menu_subs", lang),
        reply_markup=_subs_kb(lang, db, user_id),
    )
    types = _edit_present_types(subs)
    if len(types) > 1:
        await update.effective_message.reply_text(
            t("list_type_pick", lang),
            reply_markup=_alert_type_pick_keyboard(lang, types, "list_type"),
        )
        return
    await _deliver_subs_list(
        bot=context.bot,
        db=db,
        owner_id=user_id,
        lang=lang,
        subs=subs,
        reply_message=update.effective_message,
        context=context,
    )


async def on_list_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    kind = query.data.split(":", 1)[1]
    if kind not in _EDIT_ALERT_TYPE_ORDER:
        return
    db: Database = context.application.bot_data["db"]
    filtered = [
        s for s in _subs_for_owner(db, user_id) if _alert_type_from_sub(s) == kind
    ]
    if not filtered:
        await query.edit_message_text(t("no_subs_short", lang))
        return
    await _deliver_subs_list(
        bot=context.bot,
        db=db,
        owner_id=user_id,
        lang=lang,
        subs=filtered,
        reply_message=query.message,
        query=query,
        context=context,
    )


_EDIT_ALERT_TYPE_ORDER = ("live", "category", "upcoming", "end")


def _edit_present_types(subs: list[Subscription]) -> list[str]:
    present = {_alert_type_from_sub(s) for s in subs}
    return [kind for kind in _EDIT_ALERT_TYPE_ORDER if kind in present]


def _alert_type_pick_keyboard(
    lang: str,
    types: list[str],
    prefix: str,
    *,
    extra_rows: list[list[InlineKeyboardButton]] | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                t(f"alert_type_{kind}", lang),
                callback_data=f"{prefix}:{kind}",
            )
        ]
        for kind in types
    ]
    if extra_rows:
        rows.extend(extra_rows)
    return InlineKeyboardMarkup(rows)


def _edit_type_keyboard(lang: str, types: list[str]) -> InlineKeyboardMarkup:
    return _alert_type_pick_keyboard(lang, types, "edit_type")


def _edit_pick_keyboard(
    db: Database,
    owner_id: int,
    subs: list[Subscription],
    *,
    page: int = 0,
) -> InlineKeyboardMarkup:
    total = max(1, (len(subs) + _PICK_PAGE_SIZE - 1) // _PICK_PAGE_SIZE) if subs else 1
    page = max(0, min(page, total - 1))
    start = page * _PICK_PAGE_SIZE
    page_subs = subs[start : start + _PICK_PAGE_SIZE]
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                _inline_btn_label(
                    f"✏️ #{_owner_sub_number(db, owner_id, s.id)} {s.twitch_username}"
                ),
                callback_data=f"edit:{s.id}",
            )
        ]
        for s in page_subs
    ]
    if total > 1:
        rows.append(_subs_page_nav_row("edit_page", page, total))
    return InlineKeyboardMarkup(rows)


def _store_edit_pick_subs(
    context: ContextTypes.DEFAULT_TYPE, subs: list[Subscription]
) -> None:
    context.user_data["edit_pick_subs"] = [s.id for s in subs]
    context.user_data["edit_pick_page"] = 0


async def edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    subs = _subs_for_owner(db, user_id)
    if not subs:
        await update.effective_message.reply_text(
            t("no_subs_short", lang),
            reply_markup=_subs_kb(lang, db, user_id),
        )
        return

    types = _edit_present_types(subs)
    if len(types) == 1:
        text = t("edit_pick", lang)
        _store_edit_pick_subs(context, subs)
        markup = _edit_pick_keyboard(db, user_id, subs)
    else:
        text = t("edit_type_pick", lang)
        markup = _edit_type_keyboard(lang, types)
    await update.effective_message.reply_text(
        t("menu_subs", lang),
        reply_markup=_subs_kb(lang, db, user_id),
    )
    await update.effective_message.reply_text(text, reply_markup=markup)


async def on_edit_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    kind = query.data.split(":", 1)[1]
    if kind not in _EDIT_ALERT_TYPE_ORDER:
        return
    db: Database = context.application.bot_data["db"]
    filtered = [s for s in _subs_for_owner(db, user_id) if _alert_type_from_sub(s) == kind]
    if not filtered:
        await query.edit_message_text(t("no_subs_short", lang))
        return
    _store_edit_pick_subs(context, filtered)
    await query.edit_message_text(
        t("edit_pick", lang),
        reply_markup=_edit_pick_keyboard(db, user_id, filtered),
    )


async def on_edit_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.data.endswith(":noop"):
        return
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    try:
        page = int(query.data.rsplit(":", 1)[1])
    except (TypeError, ValueError, IndexError):
        return
    db: Database = context.application.bot_data["db"]
    ids = context.user_data.get("edit_pick_subs") or []
    subs = [
        s
        for sid in ids
        if (s := db.get_subscription(int(sid), user_id)) is not None
        and _sub_in_current_mode(s, user_id)
    ]
    if not subs:
        await query.edit_message_text(t("no_subs_short", lang))
        return
    context.user_data["edit_pick_page"] = page
    try:
        await query.edit_message_reply_markup(
            reply_markup=_edit_pick_keyboard(db, user_id, subs, page=page)
        )
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


async def on_edit_page_noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()


async def _deliver_import_result(
    application: Application,
    owner_id: int,
    lang: str,
    imported: int,
    skipped: int,
    limited: int,
    new_subs: list[Subscription],
    *,
    removed_names: list[str] | None = None,
    ask_streamers: list[dict[str, str]] | None = None,
) -> None:
    from config import MAX_SUBSCRIPTIONS_PER_OWNER

    removed_names = removed_names or []
    if imported == 0 and skipped == 0 and limited == 0 and not removed_names and not ask_streamers:
        await application.bot.send_message(
            owner_id,
            t("import_empty", lang),
            reply_markup=_menu(lang, owner_id),
        )
        analytics.capture(
            owner_id,
            "twitch_import_completed",
            {"imported": 0, "skipped": 0, "limited": 0, "removed": 0, "empty": True},
        )
        return
    limit_note = ""
    if limited:
        limit_note = t(
            "import_limit_note",
            lang,
            limit=MAX_SUBSCRIPTIONS_PER_OWNER,
            limited=limited,
        )
    removed_note = ""
    if removed_names:
        removed_note = t(
            "import_removed_note", lang, list=", ".join(removed_names)
        )
    header = t(
        "import_success",
        lang,
        imported=imported,
        skipped=skipped,
        limit_note=limit_note,
        removed_note=removed_note,
    )
    markup = _import_result_keyboard(lang, new_subs) if new_subs else None
    analytics.capture(
        owner_id,
        "twitch_import_completed",
        {
            "imported": imported,
            "skipped": skipped,
            "limited": limited,
            "removed": len(removed_names),
            "empty": False,
        },
    )
    await application.bot.send_message(
        owner_id,
        t("menu_main", lang),
        reply_markup=_menu(lang, owner_id),
    )
    msg = await application.bot.send_message(
        owner_id,
        header,
        reply_markup=markup,
        disable_web_page_preview=True,
    )
    if new_subs:
        mid = getattr(msg, "message_id", None)
        application.bot_data.setdefault("import_result_state", {})[owner_id] = {
            "header": header,
            "sub_ids": [s.id for s in new_subs],
            "message_id": int(mid) if mid is not None else None,
        }
    await _ask_sync_unfollow_if_needed(application, owner_id, lang, ask_streamers or [])


_PENDING_SYNC_UNFOLLOW_TTL_SEC = 24 * 3600


def _pending_sync_unfollows(application: Application) -> dict[int, dict]:
    return application.bot_data.setdefault("pending_sync_unfollows", {})


async def _ask_sync_unfollow_if_needed(
    application: Application,
    owner_id: int,
    lang: str,
    ask_streamers: list[dict[str, str]],
) -> None:
    if not ask_streamers:
        return
    db: Database = application.bot_data["db"]
    if _user_notifications_paused(db, owner_id):
        return
    _pending_sync_unfollows(application)[owner_id] = {
        "streamers": ask_streamers,
        "expires": datetime.now(timezone.utc).timestamp() + _PENDING_SYNC_UNFOLLOW_TTL_SEC,
    }
    names = ", ".join(
        html.escape(s.get("user_login") or s.get("user_id") or "?")
        for s in ask_streamers
    )
    await application.bot.send_message(
        owner_id,
        t("sync_unfollow_ask", lang, list=names),
        reply_markup=sync_unfollow_keyboard(lang),
        parse_mode=ParseMode.HTML,
    )


async def on_sync_unfollow_answer(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    owner_id = query.from_user.id
    lang = _user_lang(context, owner_id)
    data = query.data or ""
    pending = _pending_sync_unfollows(context.application).pop(owner_id, None)
    if not pending or pending.get("expires", 0) < datetime.now(timezone.utc).timestamp():
        await query.edit_message_text(t("sync_unfollow_expired", lang))
        return
    streamers: list[dict[str, str]] = list(pending.get("streamers") or [])
    names = ", ".join(
        html.escape(s.get("user_login") or s.get("user_id") or "?")
        for s in streamers
    )
    if data == "sync_unfollow:yes":
        db: Database = context.application.bot_data["db"]
        ids = {str(s.get("user_id") or "").strip() for s in streamers}
        ids.discard("")
        db.delete_subscriptions_for_twitch_users(
            owner_id,
            ids,
            is_demo=demo_mode.is_active(owner_id),
            to_cart=_deleted_subscriptions_cart_enabled(db, owner_id),
        )
        await query.edit_message_text(
            t("sync_unfollow_deleted", lang, list=names),
            parse_mode=ParseMode.HTML,
        )
        return
    await query.edit_message_text(
        t("sync_unfollow_kept", lang, list=names),
        parse_mode=ParseMode.HTML,
    )


def _pending_imports(application: Application) -> dict[int, dict]:
    return application.bot_data.setdefault("pending_imports", {})


def _store_pending_import(
    application: Application,
    owner_id: int,
    followed: list[dict],
    token_info: dict[str, str] | None,
) -> None:
    _pending_imports(application)[owner_id] = {
        "followed": followed,
        "token_info": token_info or {},
        "expires": datetime.now(timezone.utc).timestamp() + _PENDING_IMPORT_TTL_SEC,
    }


def _pop_pending_import(application: Application, owner_id: int) -> dict | None:
    pending = _pending_imports(application).pop(owner_id, None)
    if not pending:
        return None
    if pending["expires"] < datetime.now(timezone.utc).timestamp():
        return None
    return pending


def _peek_pending_import(application: Application, owner_id: int) -> dict | None:
    pending = _pending_imports(application).get(owner_id)
    if not pending:
        return None
    if pending["expires"] < datetime.now(timezone.utc).timestamp():
        _pending_imports(application).pop(owner_id, None)
        return None
    return pending


def _next_sync_iso(period_days: int, *, from_dt: datetime | None = None) -> str:
    base = from_dt or datetime.now(timezone.utc)
    return (base + timedelta(days=period_days)).isoformat()


def _format_sync_next(next_sync_at: str) -> str:
    try:
        dt = datetime.fromisoformat(next_sync_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(SCHEDULE_TZ).strftime("%d.%m.%Y %H:%M MSK")
    except ValueError:
        return next_sync_at


async def _run_followed_import(
    application: Application,
    owner_id: int,
    followed: list[dict],
    *,
    prune_missing: bool = False,
    enabled: bool = False,
) -> tuple[int, int, int, list[str], list[Subscription], list[dict[str, str]]]:
    from config import MAX_SUBSCRIPTIONS_PER_OWNER

    db: Database = application.bot_data["db"]
    lang = db.get_user_locale(owner_id) or DEFAULT_LOCALE
    return import_followed_as_subscriptions(
        db,
        owner_id,
        followed,
        template=t("import_default_template", lang),
        limit=MAX_SUBSCRIPTIONS_PER_OWNER,
        prune_missing=prune_missing,
        enabled=enabled,
        is_demo=demo_mode.is_active(owner_id),
        delete_to_cart=_deleted_subscriptions_cart_enabled(db, owner_id),
    )


async def complete_twitch_import(
    application: Application,
    owner_id: int,
    followed: list[dict] | None,
    error: str | None,
    token_info: dict[str, str] | None = None,
) -> None:
    purpose = (token_info or {}).get("purpose", "import")
    if purpose == "schedule":
        await _complete_schedule_publish(application, owner_id, error, token_info)
        return
    if purpose == "premium":
        from premium_handlers import complete_premium_oauth

        await complete_premium_oauth(application, owner_id, error, token_info)
        return
    if purpose == "premium_channel":
        from premium_handlers import complete_premium_channel_oauth

        await complete_premium_channel_oauth(application, owner_id, error, token_info)
        return
    if purpose == "whispers":
        await complete_whisper_oauth(application, owner_id, error, token_info)
        return
    if purpose == "chat":
        await complete_chat_oauth(application, owner_id, error, token_info)
        return
    db: Database = application.bot_data["db"]
    lang = db.get_user_locale(owner_id) or DEFAULT_LOCALE
    if error:
        key = "oauth_denied" if error == "access_denied" else "import_failed"
        analytics.capture(
            owner_id,
            "twitch_import_failed",
            {"error": error},
        )
        await application.bot.send_message(
            owner_id,
            t(key, lang),
            reply_markup=_menu(lang, owner_id),
        )
        return
    if followed is None:
        analytics.capture(
            owner_id,
            "twitch_import_failed",
            {"error": "no_follows"},
        )
        await application.bot.send_message(
            owner_id,
            t("import_failed", lang),
            reply_markup=_menu(lang, owner_id),
        )
        return
    _store_pending_import(application, owner_id, followed, token_info)
    await application.bot.send_message(
        owner_id,
        t("import_mode_prompt", lang),
        reply_markup=import_mode_keyboard(lang),
    )


async def on_import_mode_once(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    owner_id = query.from_user.id
    lang = _user_lang(context, owner_id)
    pending = _pop_pending_import(context.application, owner_id)
    if not pending:
        await query.edit_message_text(t("import_pending_expired", lang))
        return
    await query.edit_message_text(t("import_mode_once", lang))
    imported, skipped, limited, removed_names, new_subs, ask_streamers = await _run_followed_import(
        context.application, owner_id, pending["followed"]
    )
    await _deliver_import_result(
        context.application, owner_id, lang, imported, skipped, limited, new_subs,
        removed_names=removed_names,
        ask_streamers=ask_streamers,
    )


async def on_import_mode_sync(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    owner_id = query.from_user.id
    lang = _user_lang(context, owner_id)
    db: Database = context.application.bot_data["db"]
    if not await prem.has_feature(context.bot, db, owner_id, "twitch_sync"):
        return await _show_premium_gate(
            update, context, feature="sync", first_step=True
        )
    pending = _peek_pending_import(context.application, owner_id)
    if not pending:
        await query.edit_message_text(t("import_pending_expired", lang))
        return ConversationHandler.END
    refresh = (pending.get("token_info") or {}).get("refresh_token") or ""
    if not refresh:
        pending = _pop_pending_import(context.application, owner_id)
        await query.edit_message_text(t("import_sync_no_refresh", lang))
        imported, skipped, limited, removed_names, new_subs, ask_streamers = await _run_followed_import(
            context.application, owner_id, pending["followed"]
        )
        await _deliver_import_result(
            context.application, owner_id, lang, imported, skipped, limited, new_subs,
            removed_names=removed_names,
            ask_streamers=ask_streamers,
        )
        return ConversationHandler.END
    context.user_data["sync_days_mode"] = "import"
    await query.edit_message_text(t("import_mode_sync", lang))
    await context.bot.send_message(
        owner_id,
        t("import_sync_days_prompt", lang),
        reply_markup=_wizard(lang, back=False),
    )
    return _sub_states()["SYNC_DAYS"]


async def receive_sync_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    raw = (update.effective_message.text or "").strip()
    if is_menu_button(raw) or raw in all_wizard_nav_buttons():
        return ConversationHandler.END
    if not raw.isdigit() or not (_SYNC_PERIOD_MIN <= int(raw) <= _SYNC_PERIOD_MAX):
        await update.effective_message.reply_text(t("import_sync_days_invalid", lang))
        return _sub_states()["SYNC_DAYS"]
    days = int(raw)
    mode = context.user_data.get("sync_days_mode", "import")
    db: Database = context.application.bot_data["db"]
    now = datetime.now(timezone.utc)
    next_at = _next_sync_iso(days, from_dt=now)

    if mode == "settings":
        if not db.set_twitch_sync_period(user_id, days, next_at):
            await update.effective_message.reply_text(
                t("sync_menu_off", lang),
                reply_markup=_settings_kb(lang, db, user_id),
            )
            return ConversationHandler.END
        await update.effective_message.reply_text(
            t("sync_period_updated", lang, days=days),
            reply_markup=_settings_kb(lang, db, user_id),
        )
        context.user_data.pop("sync_days_mode", None)
        return ConversationHandler.END

    pending = _pop_pending_import(context.application, user_id)
    if not pending:
        await update.effective_message.reply_text(
            t("import_pending_expired", lang),
            reply_markup=_menu(lang, user_id),
        )
        return ConversationHandler.END
    token_info = pending.get("token_info") or {}
    refresh = token_info.get("refresh_token") or ""
    twitch_user_id = token_info.get("twitch_user_id") or ""
    if not refresh or not twitch_user_id:
        await update.effective_message.reply_text(t("import_sync_no_refresh", lang))
        imported, skipped, limited, removed_names, new_subs, ask_streamers = await _run_followed_import(
            context.application, user_id, pending["followed"]
        )
        await _deliver_import_result(
            context.application, user_id, lang, imported, skipped, limited, new_subs,
            removed_names=removed_names,
            ask_streamers=ask_streamers,
        )
        return ConversationHandler.END

    db.upsert_twitch_sync(
        owner_id=user_id,
        twitch_user_id=twitch_user_id,
        refresh_token=refresh,
        period_days=days,
        next_sync_at=next_at,
        last_sync_at=now.isoformat(),
    )
    await update.effective_message.reply_text(
        t("import_sync_enabled", lang, days=days),
    )
    imported, skipped, limited, removed_names, new_subs, ask_streamers = await _run_followed_import(
        context.application, user_id, pending["followed"], enabled=True
    )
    await _deliver_import_result(
        context.application, user_id, lang, imported, skipped, limited, new_subs,
        removed_names=removed_names,
        ask_streamers=ask_streamers,
    )
    context.user_data.pop("sync_days_mode", None)
    return ConversationHandler.END


async def open_sync_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    if not await prem.has_feature(context.bot, db, user_id, "twitch_sync"):
        from premium_handlers import send_premium_screen

        await update.effective_message.reply_text(
            t("premium_gate", lang, action=t("premium_gate_action_cancel", lang))
        )
        await send_premium_screen(context.bot, user_id, lang, db, update=update)
        return
    sync = db.get_twitch_sync(user_id)
    if not sync or sync.period_days <= 0:
        await update.effective_message.reply_text(
            t("sync_menu_off", lang),
            reply_markup=_settings_kb(lang, db, user_id),
        )
        return
    await update.effective_message.reply_text(
        t(
            "sync_menu_on",
            lang,
            days=sync.period_days,
            next_at=_format_sync_next(sync.next_sync_at),
        ),
        reply_markup=sync_settings_keyboard(lang),
    )


async def on_sync_disable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    db.delete_twitch_sync(user_id)
    await query.edit_message_text(t("sync_disabled", lang))


async def on_sync_change_period(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    if not db.get_twitch_sync(user_id):
        await query.edit_message_text(t("sync_menu_off", lang))
        return ConversationHandler.END
    context.user_data["sync_days_mode"] = "settings"
    await query.edit_message_text(t("sync_change_period", lang))
    await context.bot.send_message(
        user_id,
        t("import_sync_days_prompt", lang),
        reply_markup=_wizard(lang, back=False),
    )
    return _sub_states()["SYNC_DAYS"]


async def _sync_owner_follows(
    application: Application,
    row: TwitchSync,
    *,
    advance_schedule: bool = True,
) -> tuple[int, int, int, list[str], list[dict[str, str]]] | None:
    """Run one follow sync. Returns (imported, skipped, limited, removed_names, ask) or None on auth failure."""
    from config import MAX_SUBSCRIPTIONS_PER_OWNER

    db: Database = application.bot_data["db"]
    if not prem.has_feature_sync(db, row.owner_id, "twitch_sync"):
        logger.info("Skipping Twitch sync for owner %s: no twitch_sync feature", row.owner_id)
        return 0, 0, 0, [], []
    twitch: TwitchClient = application.bot_data["twitch"]
    lang = db.get_user_locale(row.owner_id) or DEFAULT_LOCALE
    now = datetime.now(timezone.utc)
    try:
        token_data = await asyncio.to_thread(
            twitch.refresh_user_token, row.refresh_token
        )
        access = token_data.get("access_token") or ""
        refresh = token_data.get("refresh_token") or row.refresh_token
        followed = await asyncio.to_thread(
            twitch.get_followed_channels, access, row.twitch_user_id
        )
    except Exception:
        logger.exception("Twitch sync failed for owner %s", row.owner_id)
        db.delete_twitch_sync(row.owner_id)
        try:
            await application.bot.send_message(
                row.owner_id,
                t("sync_job_failed", lang),
                reply_markup=_menu(lang, row.owner_id),
            )
        except Exception:
            logger.exception("Cannot notify owner %s about sync failure", row.owner_id)
        return None

    imported, skipped, limited, removed_names, _new, ask_streamers = import_followed_as_subscriptions(
        db,
        row.owner_id,
        followed,
        template=t("import_default_template", lang),
        limit=MAX_SUBSCRIPTIONS_PER_OWNER,
        prune_missing=True,
        enabled=True,
        is_demo=demo_mode.is_active(row.owner_id),
        delete_to_cart=_deleted_subscriptions_cart_enabled(db, row.owner_id),
    )
    if advance_schedule and row.period_days > 0:
        next_at = _next_sync_iso(row.period_days, from_dt=now)
    else:
        next_at = row.next_sync_at
    db.update_twitch_sync_tokens(
        row.owner_id,
        refresh,
        last_sync_at=now.isoformat(),
        next_sync_at=next_at,
    )
    return imported, skipped, limited, removed_names, ask_streamers


def _sync_result_notes(
    lang: str, *, limited: int, removed_names: list[str]
) -> tuple[str, str]:
    from config import MAX_SUBSCRIPTIONS_PER_OWNER

    limit_note = ""
    if limited:
        limit_note = t(
            "import_limit_note",
            lang,
            limit=MAX_SUBSCRIPTIONS_PER_OWNER,
            limited=limited,
        )
    removed_note = ""
    if removed_names:
        removed_note = t(
            "import_removed_note", lang, list=", ".join(removed_names)
        )
    return limit_note, removed_note


async def sync_twitch_follows(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic job: sync Twitch follows; new channels are enabled by default."""
    db: Database = context.application.bot_data["db"]
    now = datetime.now(timezone.utc)
    due = db.get_due_twitch_syncs(now.isoformat())
    for row in due:
        if row.period_days <= 0:
            continue
        result = await _sync_owner_follows(context.application, row)
        if result is None:
            continue
        imported, skipped, limited, removed_names, ask_streamers = result
        lang = db.get_user_locale(row.owner_id) or DEFAULT_LOCALE
        notify = (
            db.get_receive_sync_updates(row.owner_id)
            or limited > 0
            or bool(removed_names)
        )
        if (
            notify
            and (imported or limited or removed_names)
            and not _user_notifications_paused(db, row.owner_id)
        ):
            limit_note, removed_note = _sync_result_notes(
                lang, limited=limited, removed_names=removed_names
            )
            try:
                await context.bot.send_message(
                    row.owner_id,
                    t(
                        "sync_job_done",
                        lang,
                        imported=imported,
                        skipped=skipped,
                        limit_note=limit_note,
                        removed_note=removed_note,
                    ),
                    reply_markup=_menu(lang, row.owner_id),
                )
            except Exception:
                logger.exception(
                    "Cannot notify owner %s about sync result", row.owner_id
                )
        try:
            await _ask_sync_unfollow_if_needed(
                context.application, row.owner_id, lang, ask_streamers
            )
        except Exception:
            logger.exception(
                "Cannot ask owner %s about unfollowed alerts", row.owner_id
            )


async def on_sync_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    sync = db.get_twitch_sync(user_id)
    if not sync or sync.period_days <= 0:
        await query.edit_message_text(t("sync_menu_off", lang))
        return
    await query.edit_message_text(t("sync_now_running", lang))
    result = await _sync_owner_follows(context.application, sync, advance_schedule=True)
    if result is None:
        return
    imported, skipped, limited, removed_names, ask_streamers = result
    limit_note, removed_note = _sync_result_notes(
        lang, limited=limited, removed_names=removed_names
    )
    if imported or limited or removed_names:
        text = t(
            "sync_now_ok",
            lang,
            imported=imported,
            skipped=skipped,
            limit_note=limit_note,
            removed_note=removed_note,
        )
    else:
        text = t("sync_now_none", lang)
    await context.bot.send_message(
        user_id,
        text,
        reply_markup=_settings_kb(lang, db, user_id),
    )
    await _ask_sync_unfollow_if_needed(
        context.application, user_id, lang, ask_streamers
    )


async def _refresh_import_result_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_id: int,
    lang: str,
) -> None:
    query = update.callback_query
    db: Database = context.application.bot_data["db"]
    state = (context.application.bot_data.get("import_result_state") or {}).get(owner_id)
    if not state:
        return
    header = str(state.get("header") or "")
    sub_ids = [int(i) for i in (state.get("sub_ids") or [])]
    subs = [
        s
        for sid in sub_ids
        if (s := db.get_subscription(sid, owner_id)) is not None
        and _sub_in_current_mode(s, owner_id)
    ]
    if not subs:
        return
    markup = _import_result_keyboard(lang, subs)
    try:
        await query.edit_message_text(
            header,
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


async def on_import_enable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Enable one imported (paused) subscription from the import-result screen."""
    query = update.callback_query
    lang = _user_lang(context, query.from_user.id)
    sub_id = int(query.data.split(":", 1)[1])
    db: Database = context.application.bot_data["db"]
    owner_id = query.from_user.id
    sub = db.get_subscription_by_id(sub_id)
    if (
        sub is None
        or sub.owner_id != owner_id
        or not _sub_in_current_mode(sub, owner_id)
    ):
        await query.answer()
        await query.edit_message_text(t("sub_not_found", lang))
        return
    if sub.enabled:
        await query.answer(t("sub_enabled", lang, sub_id=_owner_sub_number(db, owner_id, sub_id)))
        await _refresh_import_result_message(update, context, owner_id, lang)
        return
    if getattr(sub, "trial_paused", False):
        if not await prem.has_premium(context.bot, db, owner_id):
            from premium_handlers import send_premium_screen

            await query.answer()
            await query.edit_message_text(t("premium_trial_paused_enable", lang))
            await send_premium_screen(context.bot, owner_id, lang, db, update=update)
            return
    if not await prem.alert_type_entitled(context.bot, db, owner_id, sub):
        from premium_handlers import send_premium_screen

        await query.answer()
        await query.edit_message_text(
            t(
                "premium_enable_need_feature",
                lang,
                feature=t(prem.feature_label_key("alert_types"), lang),
            )
        )
        await send_premium_screen(context.bot, owner_id, lang, db, update=update)
        return
    if not await prem.can_enable_more_async(
        context.bot, db, owner_id, twitch_username=sub.twitch_username
    ):
        from premium_handlers import send_premium_screen

        await query.answer()
        await query.edit_message_text(
            t("premium_active_limit", lang, limit=prem.free_active_limit())
        )
        await send_premium_screen(context.bot, owner_id, lang, db, update=update)
        return
    new_state = db.toggle_subscription(sub_id, owner_id)
    if new_state is None:
        await query.answer()
        await query.edit_message_text(t("sub_not_found", lang))
        return
    sub_num = _owner_sub_number(db, owner_id, sub_id)
    await query.answer(t("sub_enabled" if new_state else "sub_disabled", lang, sub_id=sub_num))
    await _refresh_import_result_message(update, context, owner_id, lang)


async def on_enable_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    owner_id = query.from_user.id
    lang = _user_lang(context, owner_id)
    db: Database = context.application.bot_data["db"]
    demo = demo_mode.is_active(owner_id)
    slots = prem.active_subscription_slots(db, owner_id, demo=demo)
    remaining = None if slots.unlimited else slots.remaining
    count = 0
    for s in _subs_for_owner(db, owner_id):
        if s.enabled or getattr(s, "trial_paused", False):
            continue
        if not await prem.alert_type_entitled(context.bot, db, owner_id, s):
            continue
        promo = prem.is_promo_channel(s.twitch_username, db)
        if not promo and remaining is not None:
            if remaining <= 0:
                continue
        if not db.toggle_subscription(s.id, owner_id):
            continue
        count += 1
        if not promo and remaining is not None:
            remaining -= 1
    if not count:
        paused = [
            s
            for s in _subs_for_owner(db, owner_id)
            if not s.enabled and not getattr(s, "trial_paused", False)
        ]
        entitled_paused = False
        for s in paused:
            if await prem.alert_type_entitled(context.bot, db, owner_id, s):
                entitled_paused = True
                break
        if paused and not entitled_paused:
            from premium_handlers import send_premium_screen

            await query.edit_message_text(
                t(
                    "premium_enable_need_feature",
                    lang,
                    feature=t(prem.feature_label_key("alert_types"), lang),
                )
            )
            await send_premium_screen(context.bot, owner_id, lang, db, update=update)
            return
        if paused and remaining is not None and remaining <= 0:
            from premium_handlers import send_premium_screen

            await query.edit_message_text(
                t("premium_active_limit", lang, limit=prem.free_active_limit())
            )
            await send_premium_screen(context.bot, owner_id, lang, db, update=update)
            return
        await query.edit_message_text(t("enable_all_none", lang))
        return
    if not slots.unlimited:
        still_paused = sum(
            1
            for s in _subs_for_owner(db, owner_id)
            if not s.enabled
            and not getattr(s, "trial_paused", False)
            and not prem.is_promo_channel(s.twitch_username, db)
        )
        if still_paused > 0:
            await query.edit_message_text(
                t(
                    "enable_all_partial",
                    lang,
                    count=count,
                    limit=prem.free_active_limit(),
                    remaining=still_paused,
                )
            )
            return
    await query.edit_message_text(t("enable_all_done", lang, count=count))


async def on_edit_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    sub_id = int(query.data.split(":", 1)[1])
    db: Database = context.application.bot_data["db"]
    sub = db.get_subscription(sub_id, query.from_user.id)
    if not sub or not _sub_in_current_mode(sub, query.from_user.id):
        await query.edit_message_text(t("sub_not_found", lang))
        return
    if sub.from_watch_suggest or is_category_watch_sub(sub):
        await query.edit_message_text(t("edit_watch_locked", lang))
        return
    sub_num = _owner_sub_number(db, query.from_user.id, sub_id)
    show_adv = await prem.advanced_mode_on(
        context.bot, db, query.from_user.id, channel=sub.twitch_username
    )
    if not show_adv:
        reset_fields = {
            "ignore_keywords": "",
            "use_global_ignore": False,
            "delay_minutes": 0,
            "suppress_repeat_minutes": 0,
            "attach_chat_button": False,
            "delete_previous": False,
            "notify_delete_fail": False,
            "delete_other_alerts": False,
        }
        needs_reset = (
            bool(sub.ignore_keywords.strip())
            or sub.use_global_ignore
            or sub.delay_minutes > 0
            or sub.suppress_repeat_minutes > 0
            or sub.attach_chat_button
            or sub.delete_previous
            or sub.notify_delete_fail
            or sub.delete_other_alerts
        )
        if needs_reset:
            db.update_subscription(sub_id, query.from_user.id, **reset_fields)
            sub = db.get_subscription(sub_id, query.from_user.id) or sub
    await query.edit_message_text(
        _edit_menu_text(
            lang,
            sub_id=sub_num,
            username=sub.twitch_username,
            show_advanced=show_adv,
        ),
        reply_markup=_edit_options_for_sub(sub, lang, show_advanced=show_adv),
        parse_mode=ParseMode.HTML,
    )


async def start_edit_template(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    sub_id = int(query.data.split(":")[1])
    db: Database = context.application.bot_data["db"]
    sub = db.get_subscription(sub_id, query.from_user.id)
    if not sub:
        await query.edit_message_text(t("sub_not_found", lang))
        return ConversationHandler.END
    sub_num = _owner_sub_number(db, query.from_user.id, sub_id)
    context.user_data["edit_sub_id"] = sub_id
    context.user_data["wizard_edit"] = True
    context.user_data["strip_name_mentions"] = bool(sub.strip_name_mentions)
    await query.edit_message_text("✓")
    await _prompt_edit_template(
        bot=context.bot,
        user_id=query.from_user.id,
        lang=lang,
        sub=sub,
        sub_num=sub_num,
        strip_name_mentions=bool(sub.strip_name_mentions),
    )
    return _sub_states()["EDIT_TEMPLATE"]


async def start_edit_ignore_keywords(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    sub_id = int(query.data.split(":")[1])
    db: Database = context.application.bot_data["db"]
    sub = db.get_subscription(sub_id, query.from_user.id)
    if not sub:
        await query.edit_message_text(t("sub_not_found", lang))
        return ConversationHandler.END
    if not await prem.has_feature(
        context.bot,
        db,
        query.from_user.id,
        "ignore_keywords",
        channel=sub.twitch_username,
    ):
        from premium_handlers import send_premium_screen

        await query.edit_message_text(
            t("premium_gate", lang, action=t("premium_gate_action_cancel", lang))
        )
        await send_premium_screen(context.bot, query.from_user.id, lang, db, update=update)
        return ConversationHandler.END
    context.user_data["edit_sub_id"] = sub_id
    context.user_data["wizard_edit"] = True
    context.user_data["use_global_ignore"] = bool(sub.use_global_ignore)
    has_keywords = bool(sub.ignore_keywords.strip())
    context.user_data["ignore_keywords_as_cancel"] = True
    current = _ignore_keywords_current_label(sub.ignore_keywords, lang)
    if has_keywords:
        current = f"<code>{html.escape(current)}</code>"
    hint = t("edit_ignore_keywords_hint_cancel", lang)
    sub_num = _owner_sub_number(db, query.from_user.id, sub_id)
    await query.edit_message_text("✓")
    # Inline only: Cancel under the prompt (always). No reply keyboard / no junk carrier.
    await context.bot.send_message(
        query.from_user.id,
        t(
            "edit_ignore_keywords_prompt",
            lang,
            sub_id=sub_num,
            current=current,
            hint=hint,
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=ignore_keywords_keyboard(
            lang,
            as_cancel=True,
            use_global=bool(sub.use_global_ignore),
        ),
    )
    return _sub_states()["EDIT_IGNORE_KEYWORDS"]


async def receive_edit_ignore_keywords(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    lang = _user_lang(context, update.effective_user.id)
    sub_id = context.user_data.get("edit_sub_id")
    if not sub_id:
        return ConversationHandler.END

    text = (update.effective_message.text or "").strip()
    if is_menu_button(text):
        await update.effective_message.reply_text(t("finish_setup_first", lang))
        return _sub_states()["EDIT_IGNORE_KEYWORDS"]

    keywords = normalize_ignore_keywords(text)
    db: Database = context.application.bot_data["db"]
    owner_id = update.effective_user.id
    sub_num = _owner_sub_number(db, owner_id, sub_id)
    if not db.update_subscription(
        sub_id,
        owner_id,
        ignore_keywords=keywords,
        use_global_ignore=bool(context.user_data.get("use_global_ignore")),
    ):
        await update.effective_message.reply_text(t("sub_not_found", lang))
    else:
        await update.effective_message.reply_text(
            t("edit_updated", lang, sub_id=sub_num),
            reply_markup=_menu(lang, owner_id),
        )
    context.user_data.clear()
    return ConversationHandler.END


async def receive_edit_ignore_keywords_skip(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    sub_id = context.user_data.get("edit_sub_id")
    if not sub_id:
        return ConversationHandler.END

    db: Database = context.application.bot_data["db"]
    owner_id = query.from_user.id
    sub_num = _owner_sub_number(db, owner_id, sub_id)
    if not db.update_subscription(
        sub_id,
        owner_id,
        ignore_keywords="",
        use_global_ignore=bool(context.user_data.get("use_global_ignore")),
    ):
        await query.edit_message_text(t("sub_not_found", lang))
    else:
        await query.edit_message_text("✓")
        await context.bot.send_message(
            owner_id,
            t("edit_updated", lang, sub_id=sub_num),
            reply_markup=_menu(lang, owner_id),
        )
    context.user_data.clear()
    return ConversationHandler.END


async def start_edit_repeat_mute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    sub_id = int(query.data.split(":")[1])
    db: Database = context.application.bot_data["db"]
    sub = db.get_subscription(sub_id, query.from_user.id)
    if not sub:
        await query.edit_message_text(t("sub_not_found", lang))
        return ConversationHandler.END
    context.user_data["edit_sub_id"] = sub_id
    context.user_data["wizard_edit"] = True
    current = _repeat_current_label(sub.suppress_repeat_minutes, lang)
    sub_num = _owner_sub_number(db, query.from_user.id, sub_id)
    await query.edit_message_text("✓")
    await context.bot.send_message(
        query.from_user.id,
        t("edit_repeat_mute_prompt", lang, sub_id=sub_num, current=current),
        reply_markup=_wizard(lang, back=False),
    )
    return _sub_states()["EDIT_REPEAT"]


async def start_edit_dest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    sub_id = int(query.data.split(":")[1])
    db: Database = context.application.bot_data["db"]
    sub = db.get_subscription(sub_id, query.from_user.id)
    if not sub:
        await query.edit_message_text(t("sub_not_found", lang))
        return ConversationHandler.END
    context.user_data["edit_sub_id"] = sub_id
    context.user_data["twitch_username"] = sub.twitch_username
    context.user_data["twitch_user_id"] = sub.twitch_user_id
    context.user_data["message_template"] = sub.message_template
    context.user_data["ignore_keywords"] = sub.ignore_keywords
    context.user_data["use_global_ignore"] = sub.use_global_ignore
    context.user_data["disable_link_preview"] = sub.disable_link_preview
    context.user_data["suppress_repeat_minutes"] = sub.suppress_repeat_minutes
    context.user_data["alert_type"] = _alert_type_from_sub(sub)
    context.user_data["notify_on_live"] = sub.notify_on_live
    context.user_data["notify_on_end"] = sub.notify_on_end
    context.user_data["notify_on_category_change"] = sub.notify_on_category_change
    context.user_data["delete_other_alerts"] = sub.delete_other_alerts
    context.user_data["wizard_edit"] = True
    await query.edit_message_text(
        t("dest_prompt", lang),
        reply_markup=dest_keyboard(lang),
    )
    _set_wizard_back(context, _sub_states()["DEST_TYPE"])
    return _sub_states()["DEST_TYPE"]


async def on_edit_bool_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    parts = query.data.split(":")
    sub_id = int(parts[1])
    field = parts[2]
    db: Database = context.application.bot_data["db"]
    sub = db.get_subscription(sub_id, query.from_user.id)
    if not sub:
        await query.edit_message_text(t("sub_not_found", lang))
        return
    if field in ("delete_old", "delete_fail", "delete_other") and sub.dest_type == "dm":
        show_adv = await prem.advanced_mode_on(
            context.bot, db, query.from_user.id, channel=sub.twitch_username
        )
        await query.edit_message_text(
            _edit_menu_text(
                lang,
                sub_id=_owner_sub_number(db, query.from_user.id, sub_id),
                username=sub.twitch_username,
                show_advanced=show_adv,
            ),
            reply_markup=_edit_options_for_sub(sub, lang, show_advanced=show_adv),
            parse_mode=ParseMode.HTML,
        )
        return
    if field == "delete_other" and (
        not sub.notify_on_category_change or not sub.delete_previous
    ):
        show_adv = await prem.advanced_mode_on(
            context.bot, db, query.from_user.id, channel=sub.twitch_username
        )
        await query.edit_message_text(
            _edit_menu_text(
                lang,
                sub_id=_owner_sub_number(db, query.from_user.id, sub_id),
                username=sub.twitch_username,
                show_advanced=show_adv,
            ),
            reply_markup=_edit_options_for_sub(sub, lang, show_advanced=show_adv),
            parse_mode=ParseMode.HTML,
        )
        return
    if field in ("delete_old", "delete_fail", "delete_other", "repeat"):
        feat = "repeat" if field == "repeat" else "delete_prev"
        if not await prem.has_feature(
            context.bot, db, query.from_user.id, feat, channel=sub.twitch_username
        ):
            from premium_handlers import send_premium_screen

            await query.edit_message_text(
                t("premium_gate", lang, action=t("premium_gate_action_cancel", lang))
            )
            await send_premium_screen(context.bot, query.from_user.id, lang, db, update=update)
            return
    if field == "delete_old" and sub.notify_on_category_change:
        menu_key = "edit_delete_old_menu_category"
    else:
        menu_keys = {
            "delete_old": "edit_delete_old_menu",
            "delete_fail": "delete_fail_notify_text",
            "delete_other": "edit_delete_other_menu",
            "preview": "link_preview_prompt",
            "chat_button": "edit_chat_button_menu",
            "repeat": "repeat_prompt",
        }
        menu_key = menu_keys[field]
    await query.edit_message_text(
        t(menu_key, lang),
        reply_markup=edit_bool_keyboard(sub_id, field, lang),
    )


async def on_edit_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    parts = query.data.split(":")
    sub_id = int(parts[1])
    field = parts[2]
    value = parts[3] == "1"
    db: Database = context.application.bot_data["db"]
    sub = db.get_subscription(sub_id, query.from_user.id)
    if not sub:
        await query.edit_message_text(t("sub_not_found", lang))
        return
    if field in ("delete_old", "delete_fail", "delete_other") and sub.dest_type == "dm":
        await query.edit_message_text(t("sub_not_found", lang))
        return
    if field == "delete_old":
        kwargs: dict = {"delete_previous": value}
        if not value:
            kwargs["notify_delete_fail"] = False
            kwargs["delete_other_alerts"] = False
    elif field == "delete_fail":
        if not sub.delete_previous:
            await query.edit_message_text(t("sub_not_found", lang))
            return
        kwargs = {"notify_delete_fail": value}
    elif field == "delete_other":
        if not sub.notify_on_category_change or not sub.delete_previous:
            await query.edit_message_text(t("sub_not_found", lang))
            return
        kwargs = {"delete_other_alerts": value}
    elif field == "preview":
        if not value and sub.attach_chat_button:
            await query.edit_message_text(t("preview_blocked_chat_button", lang))
            return
        kwargs = {"disable_link_preview": value}
    elif field == "chat_button":
        kwargs = {"attach_chat_button": value}
        if value:
            kwargs["disable_link_preview"] = True
    elif field == "repeat":
        kwargs = {"suppress_repeat_minutes": 0} if value else None
        if kwargs is None:
            return
    else:
        return
    if db.update_subscription(sub_id, query.from_user.id, **kwargs):
        sub_num = _owner_sub_number(db, query.from_user.id, sub_id)
        await query.edit_message_text(t("edit_updated", lang, sub_id=sub_num))
    else:
        await query.edit_message_text(t("sub_not_found", lang))


async def delete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    subs = _subs_for_owner(db, user_id)
    if not subs:
        await update.effective_message.reply_text(
            t("no_subs_short", lang),
            reply_markup=_subs_kb(lang, db, user_id),
        )
        return
    context.user_data["delete_selected"] = set()
    context.user_data["delete_page"] = 0
    types = _edit_present_types(subs)
    # Reply keyboard first — same mobile gap fix as list_subscriptions.
    await update.effective_message.reply_text(
        t("menu_subs", lang),
        reply_markup=_subs_kb(lang, db, user_id),
    )
    if len(types) > 1:
        extra_rows: list[list[InlineKeyboardButton]] = []
        if _deleted_subscriptions_cart_enabled(db, user_id):
            extra_rows.append(
                [InlineKeyboardButton(t("btn_cart", lang), callback_data="delete_cart_open")]
            )
        markup = _alert_type_pick_keyboard(
            lang, types, "delete_type", extra_rows=extra_rows or None
        )
        await update.effective_message.reply_text(
            t("delete_type_pick", lang),
            reply_markup=markup,
        )
        return
    await update.effective_message.reply_text(
        t("delete_pick", lang),
        reply_markup=_delete_pick_keyboard(db, user_id, lang, subs, set()),
    )


async def on_delete_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    kind = query.data.split(":", 1)[1]
    if kind not in _EDIT_ALERT_TYPE_ORDER:
        return
    db: Database = context.application.bot_data["db"]
    filtered = [
        s for s in _subs_for_owner(db, user_id) if _alert_type_from_sub(s) == kind
    ]
    if not filtered:
        await query.edit_message_text(t("no_subs_short", lang))
        return
    context.user_data["delete_selected"] = set()
    context.user_data["delete_type"] = kind
    context.user_data["delete_page"] = 0
    await query.edit_message_text(
        t("delete_pick", lang),
        reply_markup=_delete_pick_keyboard(db, user_id, lang, filtered, set()),
    )


def _delete_subs_for_owner(
    db: Database, owner_id: int, context: ContextTypes.DEFAULT_TYPE
) -> list[Subscription]:
    subs = _subs_for_owner(db, owner_id)
    kind = context.user_data.get("delete_type")
    if kind in _EDIT_ALERT_TYPE_ORDER:
        return [s for s in subs if _alert_type_from_sub(s) == kind]
    return subs


def _delete_pick_keyboard(
    db: Database,
    owner_id: int,
    lang: str,
    subs: list[Subscription],
    selected: set[int],
    *,
    page: int = 0,
) -> InlineKeyboardMarkup:
    total = max(1, (len(subs) + _PICK_PAGE_SIZE - 1) // _PICK_PAGE_SIZE) if subs else 1
    page = max(0, min(page, total - 1))
    start = page * _PICK_PAGE_SIZE
    page_subs = subs[start : start + _PICK_PAGE_SIZE]
    rows: list[list[InlineKeyboardButton]] = []
    for s in page_subs:
        mark = "✅ " if s.id in selected else ""
        num = _owner_sub_number(db, owner_id, s.id)
        rows.append(
            [
                InlineKeyboardButton(
                    f"{mark}🗑 #{num} {s.twitch_username}",
                    callback_data=f"delete_sel:{s.id}",
                )
            ]
        )
    if total > 1:
        rows.append(_subs_page_nav_row("delete_page", page, total))
    rows.append(
        [InlineKeyboardButton(t("delete_all", lang), callback_data="delete_all")]
    )
    rows.append(
        [
            InlineKeyboardButton(
                t("delete_go", lang, count=len(selected)),
                callback_data="delete_go",
            )
        ]
    )
    if selected:
        rows.append(
            [InlineKeyboardButton(t("delete_clear", lang), callback_data="delete_clear")]
        )
    if _deleted_subscriptions_cart_enabled(db, owner_id):
        rows.append(
            [InlineKeyboardButton(t("btn_cart", lang), callback_data="delete_cart_open")]
        )
    return InlineKeyboardMarkup(rows)


async def on_delete_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.data.endswith(":noop"):
        return
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    try:
        page = int(query.data.rsplit(":", 1)[1])
    except (TypeError, ValueError, IndexError):
        return
    db: Database = context.application.bot_data["db"]
    selected: set[int] = set(context.user_data.get("delete_selected") or ())
    subs = _delete_subs_for_owner(db, user_id, context)
    context.user_data["delete_page"] = page
    try:
        await query.edit_message_reply_markup(
            reply_markup=_delete_pick_keyboard(
                db, user_id, lang, subs, selected, page=page
            )
        )
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


async def on_delete_page_noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()


async def on_delete_sel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    sub_id = int(query.data.split(":", 1)[1])
    selected: set[int] = context.user_data.setdefault("delete_selected", set())
    if sub_id in selected:
        selected.discard(sub_id)
    else:
        selected.add(sub_id)
    db: Database = context.application.bot_data["db"]
    subs = _delete_subs_for_owner(db, user_id, context)
    page = int(context.user_data.get("delete_page") or 0)
    await query.edit_message_reply_markup(
        reply_markup=_delete_pick_keyboard(
            db, user_id, lang, subs, selected, page=page
        )
    )


async def on_delete_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    context.user_data["delete_selected"] = set()
    db: Database = context.application.bot_data["db"]
    subs = _delete_subs_for_owner(db, user_id, context)
    page = int(context.user_data.get("delete_page") or 0)
    await query.edit_message_reply_markup(
        reply_markup=_delete_pick_keyboard(db, user_id, lang, subs, set(), page=page)
    )


def _cart_present_types(items: list) -> list[str]:
    present = {getattr(item, "alert_type", None) or "live" for item in items}
    return [kind for kind in _EDIT_ALERT_TYPE_ORDER if kind in present]


def _cart_items_of_type(items: list, kind: str | None) -> list:
    if kind not in _EDIT_ALERT_TYPE_ORDER:
        return list(items)
    return [
        item
        for item in items
        if (getattr(item, "alert_type", None) or "live") == kind
    ]


def _delete_cart_view(items: list, kind: str | None) -> tuple[list[str], str | None, list]:
    types = _cart_present_types(items)
    if kind not in types:
        kind = None
    if kind is None and len(types) == 1:
        kind = types[0]
    view = _cart_items_of_type(items, kind) if kind else list(items)
    return types, kind, view


def _delete_cart_keyboard(
    lang: str,
    items: list,
    selected: set[int],
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for idx, item in enumerate(items, 1):
        cart_id = int(item.cart_id)
        display = str(getattr(item, "twitch_username", "") or "") or str(
            getattr(item, "twitch_user_id", "") or ""
        )
        mark = "✅ " if cart_id in selected else ""
        rows.append(
            [
                InlineKeyboardButton(
                    f"{mark}♻️ #{idx} {display}".strip(),
                    callback_data=f"delete_cart_sel:{cart_id}",
                )
            ]
        )
    if selected:
        rows.append(
            [
                InlineKeyboardButton(
                    t("cart_clear", lang),
                    callback_data="delete_cart_clear",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                t("cart_restore_go", lang, count=len(selected)),
                callback_data="delete_cart_restore_go",
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def _store_delete_cart_state(
    context: ContextTypes.DEFAULT_TYPE,
    items: list,
    *,
    days: int,
    kind: str | None,
    selected: set[int] | None = None,
) -> tuple[list[str], str | None, list]:
    types, kind, view = _delete_cart_view(items, kind)
    context.user_data["delete_cart_days"] = days
    context.user_data["delete_cart_all_items"] = items
    context.user_data["delete_cart_items"] = view
    context.user_data["delete_cart_type"] = kind
    context.user_data["delete_cart_order"] = [int(i.cart_id) for i in view]
    context.user_data["delete_cart_selected"] = set(selected or ())
    return types, kind, view


async def on_delete_cart_open(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    if not _deleted_subscriptions_cart_enabled(db, user_id):
        return
    is_demo = demo_mode.is_active(user_id)
    days = prem.deleted_subscriptions_cart_days(db, user_id)
    items = db.list_deleted_subscriptions(
        user_id, days=days, is_demo=is_demo, limit=100
    )
    await _show_delete_cart(
        query, context, lang, items, days=days, kind=None, selected=set()
    )


async def _show_delete_cart(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    lang: str,
    items: list,
    *,
    days: int,
    kind: str | None,
    selected: set[int],
    prefix: str = "",
) -> None:
    types, kind, view = _store_delete_cart_state(
        context, items, days=days, kind=kind, selected=selected
    )
    if not items:
        text = t("cart_empty", lang, days=days)
        markup = None
    elif kind is None and len(types) > 1:
        text = t("cart_type_pick", lang)
        markup = _alert_type_pick_keyboard(lang, types, "delete_cart_type")
    else:
        text = t("cart_prompt", lang, days=days)
        if not view:
            text = t("cart_empty", lang, days=days)
        markup = _delete_cart_keyboard(lang, view, selected)
    if prefix:
        text = prefix + text
    await query.edit_message_text(text, reply_markup=markup)


async def on_delete_cart_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    if not _deleted_subscriptions_cart_enabled(db, user_id):
        return
    kind = (query.data or "").split(":", 1)[-1]
    if kind not in _EDIT_ALERT_TYPE_ORDER:
        return
    days = int(context.user_data.get("delete_cart_days") or 10)
    items = context.user_data.get("delete_cart_all_items")
    if not items:
        is_demo = demo_mode.is_active(user_id)
        items = db.list_deleted_subscriptions(user_id, days=days, is_demo=is_demo, limit=100)
    await _show_delete_cart(
        query, context, lang, items, days=days, kind=kind, selected=set()
    )


async def on_delete_cart_sel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    if not _deleted_subscriptions_cart_enabled(db, user_id):
        return
    cart_id = int((query.data or "").split(":", 1)[-1])

    selected: set[int] = context.user_data.setdefault("delete_cart_selected", set())
    if cart_id in selected:
        selected.discard(cart_id)
    else:
        selected.add(cart_id)

    items = context.user_data.get("delete_cart_items") or []
    days = int(context.user_data.get("delete_cart_days") or 10)
    if not items:
        is_demo = demo_mode.is_active(user_id)
        all_items = db.list_deleted_subscriptions(
            user_id, days=days, is_demo=is_demo, limit=100
        )
        kind = context.user_data.get("delete_cart_type")
        _, kind, items = _store_delete_cart_state(
            context, all_items, days=days, kind=kind, selected=selected
        )

    await query.edit_message_reply_markup(
        reply_markup=_delete_cart_keyboard(lang, items, selected)
    )


async def on_delete_cart_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    if not _deleted_subscriptions_cart_enabled(db, user_id):
        return
    context.user_data["delete_cart_selected"] = set()
    items = context.user_data.get("delete_cart_items") or []
    await query.edit_message_reply_markup(
        reply_markup=_delete_cart_keyboard(lang, items, set())
    )


async def on_delete_cart_restore_go(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    if not _deleted_subscriptions_cart_enabled(db, user_id):
        return
    selected: set[int] = set(context.user_data.get("delete_cart_selected") or ())
    if not selected:
        await query.answer(t("cart_restore_none", lang), show_alert=True)
        return

    from config import MAX_SUBSCRIPTIONS_PER_OWNER

    days = int(context.user_data.get("delete_cart_days") or prem.deleted_subscriptions_cart_days(db, user_id))
    is_demo = demo_mode.is_active(user_id)
    kind = context.user_data.get("delete_cart_type")

    remaining = max(0, int(MAX_SUBSCRIPTIONS_PER_OWNER) - len(_subs_for_owner(db, user_id)))
    if remaining <= 0:
        await query.answer(
            t("sub_limit", lang, limit=MAX_SUBSCRIPTIONS_PER_OWNER),
            show_alert=True,
        )
        return

    order: list[int] = context.user_data.get("delete_cart_order") or []
    selected_in_order = [cid for cid in order if cid in selected]
    restore_ids = selected_in_order[:remaining]
    active_slots = prem.active_subscription_slots(db, user_id, demo=is_demo)
    max_enabled = None if active_slots.unlimited else active_slots.remaining
    restored, enabled_restored = db.restore_deleted_subscriptions(
        user_id,
        restore_ids,
        days=days,
        is_demo=is_demo,
        max_enabled=max_enabled,
    )
    limit_skipped = max(0, len(selected_in_order) - remaining)
    paused_due_active = max(0, restored - enabled_restored)

    items = db.list_deleted_subscriptions(user_id, days=days, is_demo=is_demo, limit=100)
    if limit_skipped > 0:
        restored_text = t(
            "cart_restored_partial",
            lang,
            restored=restored,
            skipped=limit_skipped,
            limit=MAX_SUBSCRIPTIONS_PER_OWNER,
        )
    else:
        restored_text = t("cart_restored", lang, count=restored)
    if paused_due_active > 0:
        restored_text += t(
            "cart_restored_active_paused",
            lang,
            paused=paused_due_active,
            limit=prem.free_active_limit(),
        )
    await _show_delete_cart(
        query,
        context,
        lang,
        items,
        days=days,
        kind=kind,
        selected=set(),
        prefix=restored_text + "\n\n",
    )


async def on_welcome_demo_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    sub_id = int(query.data.split(":", 1)[1])
    db: Database = context.application.bot_data["db"]
    sub = db.get_subscription(sub_id, user_id)
    if sub is None or not _sub_in_current_mode(sub, user_id):
        await query.edit_message_text(t("sub_not_found", lang))
        return
    to_cart = _deleted_subscriptions_cart_enabled(db, user_id)
    if not db.delete_subscription(sub_id, user_id, to_cart=to_cart):
        await query.edit_message_text(t("sub_not_found", lang))
        return
    analytics.capture(user_id, "welcome_demo_subscription_deleted", {"sub_id": sub_id})
    await query.edit_message_text(t("welcome_demo_deleted", lang))


async def on_delivery_fail_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    sub_id = int(query.data.split(":")[1])
    db: Database = context.application.bot_data["db"]
    sub = db.get_subscription(sub_id, user_id)
    if sub is None or not _sub_in_current_mode(sub, user_id):
        await query.edit_message_text(t("sub_not_found", lang))
        return
    to_cart = _deleted_subscriptions_cart_enabled(db, user_id)
    if not db.delete_subscription(sub_id, user_id, to_cart=to_cart):
        await query.edit_message_text(t("sub_not_found", lang))
        return
    sub_num = _owner_sub_number(db, user_id, sub_id)
    await query.edit_message_text(t("sub_deleted", lang, sub_id=sub_num))


async def on_delete_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    subs = _delete_subs_for_owner(db, user_id, context)
    if not subs:
        await query.answer(t("no_subs_short", lang), show_alert=True)
        return
    await query.answer()
    await query.edit_message_text(
        t("delete_all_confirm", lang),
        reply_markup=delete_all_confirm_keyboard(lang),
    )


async def on_delete_all_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    choice = query.data.rsplit(":", 1)[-1]
    db: Database = context.application.bot_data["db"]
    if choice == "no":
        await query.answer()
        subs = _delete_subs_for_owner(db, user_id, context)
        selected: set[int] = set(context.user_data.get("delete_selected") or ())
        page = int(context.user_data.get("delete_page") or 0)
        await query.edit_message_text(
            t("delete_pick", lang),
            reply_markup=_delete_pick_keyboard(
                db, user_id, lang, subs, selected, page=page
            ),
        )
        return
    if choice != "yes":
        return
    await query.answer()
    subs = _delete_subs_for_owner(db, user_id, context)
    deleted = 0
    to_cart = _deleted_subscriptions_cart_enabled(db, user_id)
    for sub in subs:
        if not _sub_in_current_mode(sub, user_id):
            continue
        if db.delete_subscription(sub.id, user_id, to_cart=to_cart):
            deleted += 1
    context.user_data["delete_selected"] = set()
    context.user_data.pop("delete_type", None)
    await query.edit_message_text(t("subs_deleted", lang, count=deleted))


async def on_delete_go(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    selected: set[int] = set(context.user_data.get("delete_selected") or ())
    if not selected:
        await query.answer(t("delete_none", lang), show_alert=True)
        return
    await query.answer()
    db: Database = context.application.bot_data["db"]
    deleted = 0
    to_cart = _deleted_subscriptions_cart_enabled(db, user_id)
    for sub_id in list(selected):
        sub = db.get_subscription(sub_id, user_id)
        if sub is None or not _sub_in_current_mode(sub, user_id):
            continue
        if db.delete_subscription(sub_id, user_id, to_cart=to_cart):
            deleted += 1
    context.user_data["delete_selected"] = set()
    context.user_data.pop("delete_type", None)
    await query.edit_message_text(t("subs_deleted", lang, count=deleted))


async def on_list_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    try:
        sub_id = int(query.data.split(":", 1)[1])
    except (TypeError, ValueError, IndexError):
        return
    db: Database = context.application.bot_data["db"]
    sub = db.get_subscription(sub_id, user_id)
    if sub is None or not _sub_in_current_mode(sub, user_id):
        await query.edit_message_text(t("sub_not_found", lang))
        return
    sub_num = _owner_sub_number(db, user_id, sub_id)
    await query.edit_message_text(
        t(
            "list_delete_confirm",
            lang,
            sub_id=sub_num,
            username=sub.twitch_username,
        ),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        t("delete_all_yes", lang),
                        callback_data=f"list_del_ok:{sub_id}",
                    ),
                    InlineKeyboardButton(
                        t("delete_all_no", lang),
                        callback_data=f"list_del_no:{sub_id}",
                    ),
                ]
            ]
        ),
    )


async def on_list_delete_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    parts = (query.data or "").split(":")
    if len(parts) != 2:
        return
    action, raw_id = parts[0], parts[1]
    try:
        sub_id = int(raw_id)
    except (TypeError, ValueError):
        return
    db: Database = context.application.bot_data["db"]
    if action == "list_del_no":
        subs = _subs_for_owner(db, user_id)
        await _deliver_subs_list(
            bot=context.bot,
            db=db,
            owner_id=user_id,
            lang=lang,
            subs=subs,
            reply_message=query.message,
            query=query,
            context=context,
        )
        return
    if action != "list_del_ok":
        return
    sub = db.get_subscription(sub_id, user_id)
    if sub is None or not _sub_in_current_mode(sub, user_id):
        await query.edit_message_text(t("sub_not_found", lang))
        return
    to_cart = _deleted_subscriptions_cart_enabled(db, user_id)
    if db.delete_subscription(sub_id, user_id, to_cart=to_cart):
        await query.edit_message_text(t("subs_deleted", lang, count=1))
    else:
        await query.edit_message_text(t("sub_not_found", lang))


async def _refresh_current_subs_list(
    *,
    bot,
    query,
    context: ContextTypes.DEFAULT_TYPE,
    db: Database,
    owner_id: int,
    lang: str,
) -> None:
    """Rebuild list text+keyboard for the current page; keep the list screen open."""
    pages = context.user_data.get("list_pages")
    page = int(context.user_data.get("list_page") or 0)
    if isinstance(pages, list) and pages:
        ids: list[int] = []
        seen: set[int] = set()
        for _, page_subs in pages:
            for s in page_subs:
                sid = int(s.id)
                if sid not in seen:
                    seen.add(sid)
                    ids.append(sid)
        subs = [
            s
            for sid in ids
            if (s := db.get_subscription(sid, owner_id)) is not None
            and _sub_in_current_mode(s, owner_id)
        ]
    else:
        subs = _subs_for_owner(db, owner_id)
    if not subs:
        await query.edit_message_text(t("no_subs_short", lang))
        context.user_data.pop("list_pages", None)
        context.user_data.pop("list_page", None)
        return
    bot_username = await _bot_username(bot, context.application.bot_data)
    lines, ordered = await _format_subs_overview_lines(
        bot, db, owner_id, lang, subs=subs, bot_username=bot_username
    )
    new_pages = _build_subs_list_pages(
        t("subs_list", lang), list(zip(lines, ordered))
    )
    context.user_data["list_pages"] = new_pages
    page = max(0, min(page, len(new_pages) - 1))
    context.user_data["list_page"] = page
    text, page_subs = new_pages[page]
    markup = _subs_list_keyboard(db, owner_id, lang, page_subs, page, len(new_pages))
    try:
        await query.edit_message_text(
            text,
            reply_markup=markup,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise
        await query.edit_message_reply_markup(reply_markup=markup)


async def on_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    lang = _user_lang(context, query.from_user.id)
    sub_id = int(query.data.split(":", 1)[1])
    db: Database = context.application.bot_data["db"]
    sub = db.get_subscription_by_id(sub_id)
    if (
        sub is None
        or sub.owner_id != query.from_user.id
        or not _sub_in_current_mode(sub, query.from_user.id)
    ):
        await query.answer()
        await query.edit_message_text(t("sub_not_found", lang))
        return
    if not sub.enabled and getattr(sub, "trial_paused", False):
        if not await prem.has_premium(context.bot, db, query.from_user.id):
            from premium_handlers import send_premium_screen

            await query.answer()
            await query.edit_message_text(t("premium_trial_paused_enable", lang))
            await send_premium_screen(
                context.bot, query.from_user.id, lang, db, update=update
            )
            return
    if not sub.enabled and not await prem.alert_type_entitled(
        context.bot, db, query.from_user.id, sub
    ):
        from premium_handlers import send_premium_screen

        await query.answer()
        await query.edit_message_text(
            t(
                "premium_enable_need_feature",
                lang,
                feature=t(prem.feature_label_key("alert_types"), lang),
            )
        )
        await send_premium_screen(
            context.bot, query.from_user.id, lang, db, update=update
        )
        return
    if not sub.enabled and not await prem.can_enable_more_async(
        context.bot, db, query.from_user.id, twitch_username=sub.twitch_username
    ):
        from premium_handlers import send_premium_screen

        await query.answer()
        await query.edit_message_text(
            t("premium_active_limit", lang, limit=prem.free_active_limit())
        )
        await send_premium_screen(
            context.bot, query.from_user.id, lang, db, update=update
        )
        return
    new_state = db.toggle_subscription(sub_id, query.from_user.id)
    if new_state is None:
        await query.answer()
        await query.edit_message_text(t("sub_not_found", lang))
        return
    sub_num = _owner_sub_number(db, query.from_user.id, sub_id)
    key = "sub_enabled" if new_state else "sub_disabled"
    await query.answer(t(key, lang, sub_id=sub_num))
    await _refresh_current_subs_list(
        bot=context.bot,
        query=query,
        context=context,
        db=db,
        owner_id=query.from_user.id,
        lang=lang,
    )


def _alert_type_label(kind: str, lang: str) -> str:
    return t(f"alert_type_{kind}", lang)


def _other_alert_types(current: str) -> list[str]:
    return [kind for kind in _EDIT_ALERT_TYPE_ORDER if kind != current]


def _edit_alert_type_pick_keyboard(
    sub_id: int,
    lang: str,
    *,
    mode: str,
    current_type: str,
) -> InlineKeyboardMarkup:
    types = _other_alert_types(current_type)
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                _alert_type_label(kind, lang),
                callback_data=f"edit_type_pick:{mode}:{sub_id}:{kind}",
            )
        ]
        for kind in types
    ]
    rows.append(
        [
            InlineKeyboardButton(
                btn("wizard_cancel", lang),
                callback_data=f"edit_type_pick_cancel:{mode}:{sub_id}",
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


_TYPE_MIGRATION_KEYS = (
    "delete_previous",
    "notify_delete_fail",
    "disable_link_preview",
    "strip_name_mentions",
    "attach_chat_button",
    "delay_minutes",
    "suppress_repeat_minutes",
    "schedule_reminder_minutes",
    "schedule_reminder_configured",
    "notify_on_live",
    "notify_on_end",
    "notify_on_category_change",
    "delete_other_alerts",
)


def _type_migration_kwargs(fields: dict) -> dict:
    return {key: fields[key] for key in _TYPE_MIGRATION_KEYS if key in fields}


async def _alert_type_allowed(
    bot,
    db: Database,
    owner_id: int,
    sub: Subscription,
    new_type: str,
    *,
    twitch: TwitchClient,
) -> str | None:
    if new_type == "live":
        return None
    if not await prem.has_feature(
        bot, db, owner_id, "alert_types", channel=sub.twitch_username
    ):
        return "premium"
    if new_type == "upcoming":
        try:
            has_schedule = await asyncio.to_thread(
                twitch.has_channel_schedule, sub.twitch_user_id
            )
        except Exception:
            logger.exception("Twitch schedule check failed for %s", sub.twitch_user_id)
            has_schedule = False
        if not has_schedule:
            return "no_schedule"
    return None


def _add_subscription_from_snapshot(
    db: Database,
    owner_id: int,
    snapshot: dict,
    *,
    enabled: bool,
) -> int:
    return db.add_subscription(
        owner_id=owner_id,
        twitch_username=str(snapshot.get("twitch_username") or ""),
        twitch_user_id=str(snapshot.get("twitch_user_id") or ""),
        message_template=str(snapshot.get("message_template") or ""),
        dest_type=str(snapshot.get("dest_type") or "dm"),
        chat_id=int(snapshot.get("chat_id") or owner_id),
        thread_id=snapshot.get("thread_id"),
        delete_previous=bool(snapshot.get("delete_previous")),
        notify_delete_fail=bool(snapshot.get("notify_delete_fail")),
        disable_link_preview=bool(snapshot.get("disable_link_preview")),
        strip_name_mentions=bool(snapshot.get("strip_name_mentions")),
        attach_chat_button=bool(snapshot.get("attach_chat_button")),
        delay_minutes=int(snapshot.get("delay_minutes") or 0),
        suppress_repeat_minutes=int(snapshot.get("suppress_repeat_minutes") or 0),
        schedule_reminder_minutes=int(snapshot.get("schedule_reminder_minutes") or 0),
        schedule_reminder_configured=bool(snapshot.get("schedule_reminder_configured")),
        ignore_keywords=str(snapshot.get("ignore_keywords") or ""),
        use_global_ignore=bool(snapshot.get("use_global_ignore")),
        image_file_id=snapshot.get("image_file_id") or None,
        image_position=str(snapshot.get("image_position") or ""),
        enabled=enabled,
        from_twitch_sync=bool(snapshot.get("from_twitch_sync")),
        from_watch_suggest=bool(snapshot.get("from_watch_suggest")),
        category_watch_prefs=str(snapshot.get("category_watch_prefs") or ""),
        notify_on_live=bool(snapshot.get("notify_on_live", True)),
        notify_on_end=bool(snapshot.get("notify_on_end")),
        notify_on_category_change=bool(snapshot.get("notify_on_category_change")),
        delete_other_alerts=bool(snapshot.get("delete_other_alerts")),
        is_demo=bool(snapshot.get("is_demo")),
    )


async def on_edit_change_type_click(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    owner_id = query.from_user.id
    lang = _user_lang(context, owner_id)
    sub_id = int(query.data.split(":")[1])
    db: Database = context.application.bot_data["db"]
    sub = db.get_subscription(sub_id, owner_id)
    if not sub or not _sub_in_current_mode(sub, owner_id):
        await query.edit_message_text(t("sub_not_found", lang))
        return
    current = _alert_type_from_sub(sub)
    if not _other_alert_types(current):
        await query.answer(t("edit_change_type_cancelled", lang), show_alert=True)
        return
    await query.edit_message_text(
        t("edit_change_type_pick", lang),
        reply_markup=_edit_alert_type_pick_keyboard(
            sub_id, lang, mode="change", current_type=current
        ),
    )


async def on_edit_copy_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    owner_id = query.from_user.id
    lang = _user_lang(context, owner_id)
    sub_id = int(query.data.split(":")[1])
    db: Database = context.application.bot_data["db"]
    sub = db.get_subscription(sub_id, owner_id)
    if not sub or not _sub_in_current_mode(sub, owner_id):
        await query.edit_message_text(t("sub_not_found", lang))
        return
    from config import MAX_SUBSCRIPTIONS_PER_OWNER

    if len(_subs_for_owner(db, owner_id)) >= MAX_SUBSCRIPTIONS_PER_OWNER:
        await query.edit_message_text(
            t("sub_limit", lang, limit=MAX_SUBSCRIPTIONS_PER_OWNER)
        )
        return
    enabled = await prem.may_enable_subscription_async(
        context.bot, db, owner_id, twitch_username=sub.twitch_username
    )
    if enabled and not await prem.alert_type_entitled(
        context.bot, db, owner_id, sub
    ):
        enabled = False
    snapshot = _subscription_cart_snapshot(sub)
    snapshot["enabled"] = enabled
    snapshot["is_demo"] = demo_mode.is_active(owner_id)
    new_id = _add_subscription_from_snapshot(db, owner_id, snapshot, enabled=enabled)
    sub_num = _owner_sub_number(db, owner_id, new_id)
    text = t(
        "edit_copied",
        lang,
        sub_id=sub_num,
        username=sub.twitch_username,
    )
    if not enabled:
        text += "\n" + t(
            "created_paused_note",
            lang,
            kind=t("paused_kind_copy", lang),
            limit=prem.free_active_limit(),
        )
    await query.edit_message_text(text)


async def on_edit_copy_change_click(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    owner_id = query.from_user.id
    lang = _user_lang(context, owner_id)
    sub_id = int(query.data.split(":")[1])
    db: Database = context.application.bot_data["db"]
    sub = db.get_subscription(sub_id, owner_id)
    if not sub or not _sub_in_current_mode(sub, owner_id):
        await query.edit_message_text(t("sub_not_found", lang))
        return
    from config import MAX_SUBSCRIPTIONS_PER_OWNER

    if len(_subs_for_owner(db, owner_id)) >= MAX_SUBSCRIPTIONS_PER_OWNER:
        await query.edit_message_text(
            t("sub_limit", lang, limit=MAX_SUBSCRIPTIONS_PER_OWNER)
        )
        return
    current = _alert_type_from_sub(sub)
    if not _other_alert_types(current):
        await query.answer(t("edit_copy_cancelled", lang), show_alert=True)
        return
    await query.edit_message_text(
        t("edit_copy_change_pick", lang),
        reply_markup=_edit_alert_type_pick_keyboard(
            sub_id, lang, mode="copy", current_type=current
        ),
    )


async def on_edit_type_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    owner_id = query.from_user.id
    lang = _user_lang(context, owner_id)
    parts = query.data.split(":")
    if len(parts) != 4:
        return
    mode, sub_id_raw, new_type = parts[1], parts[2], parts[3]
    if mode not in ("change", "copy") or new_type not in _EDIT_ALERT_TYPE_ORDER:
        return
    try:
        sub_id = int(sub_id_raw)
    except (TypeError, ValueError):
        return
    db: Database = context.application.bot_data["db"]
    sub = db.get_subscription(sub_id, owner_id)
    if not sub or not _sub_in_current_mode(sub, owner_id):
        await query.edit_message_text(t("sub_not_found", lang))
        return
    if _alert_type_from_sub(sub) == new_type:
        return
    twitch: TwitchClient = context.application.bot_data["twitch"]
    block = await _alert_type_allowed(
        context.bot, db, owner_id, sub, new_type, twitch=twitch
    )
    if block == "premium":
        from premium_handlers import send_premium_screen

        await query.edit_message_text(
            t("premium_gate", lang, action=t("premium_gate_action_cancel", lang))
        )
        await send_premium_screen(context.bot, owner_id, lang, db, update=update)
        return
    if block == "no_schedule":
        await query.edit_message_text(t("alert_type_no_schedule", lang))
        return

    snapshot = migrate_sub_fields_for_alert_type(
        _subscription_cart_snapshot(sub), new_type
    )
    if mode == "change":
        if not db.update_subscription(
            sub_id, owner_id, **_type_migration_kwargs(snapshot)
        ):
            await query.edit_message_text(t("sub_not_found", lang))
            return
        await query.edit_message_text(
            t(
                "edit_type_changed",
                lang,
                alert_type=_alert_type_label(new_type, lang),
            )
        )
        return

    from config import MAX_SUBSCRIPTIONS_PER_OWNER

    if len(_subs_for_owner(db, owner_id)) >= MAX_SUBSCRIPTIONS_PER_OWNER:
        await query.edit_message_text(
            t("sub_limit", lang, limit=MAX_SUBSCRIPTIONS_PER_OWNER)
        )
        return
    enabled = await prem.may_enable_subscription_async(
        context.bot, db, owner_id, twitch_username=sub.twitch_username
    )
    snapshot["enabled"] = enabled
    snapshot["is_demo"] = demo_mode.is_active(owner_id)
    new_id = _add_subscription_from_snapshot(db, owner_id, snapshot, enabled=enabled)
    sub_num = _owner_sub_number(db, owner_id, new_id)
    text = t(
        "edit_copied",
        lang,
        sub_id=sub_num,
        username=sub.twitch_username,
    )
    if not enabled:
        text += "\n" + t(
            "created_paused_note",
            lang,
            kind=t("paused_kind_copy", lang),
            limit=prem.free_active_limit(),
        )
    await query.edit_message_text(text)


async def on_edit_type_pick_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    parts = query.data.split(":")
    if len(parts) != 3:
        return
    mode = parts[1]
    key = "edit_copy_cancelled" if mode == "copy" else "edit_change_type_cancelled"
    await query.edit_message_text(t(key, lang))


def _share_alert_type_label(payload: dict, lang: str) -> str:
    kind = alert_type_from_payload(payload)
    key = {
        "live": "alert_type_live",
        "category": "alert_type_category",
        "upcoming": "alert_type_upcoming",
        "end": "alert_type_end",
    }.get(kind, "alert_type_live")
    return t(key, lang)


def _share_offer_keyboard(lang: str, token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("share_accept", lang),
                    callback_data=f"share_accept:{token}",
                )
            ],
            [
                InlineKeyboardButton(
                    t("share_decline", lang),
                    callback_data="share_decline",
                )
            ],
        ]
    )


def _share_dup_keyboard(lang: str, sub_id: int, token: str) -> InlineKeyboardMarkup:
    """Same labels as channel_dup_keyboard; callbacks stay in the share flow."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("channel_dup_edit", lang),
                    callback_data=f"share_dup:edit:{sub_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    t("channel_dup_continue", lang),
                    callback_data=f"share_dup:continue:{token}",
                )
            ],
        ]
    )


def _existing_sub_for_twitch(
    db: Database, owner_id: int, twitch_user_id: str
) -> Subscription | None:
    uid = (twitch_user_id or "").strip()
    if not uid:
        return None
    return next(
        (s for s in _subs_for_owner(db, owner_id) if s.twitch_user_id == uid),
        None,
    )


def _share_clone_snapshot(snapshot: dict, user_id: int) -> dict:
    out = dict(snapshot)
    out["dest_type"] = "dm"
    out["chat_id"] = user_id
    out["thread_id"] = None
    out["from_twitch_sync"] = False
    out["from_watch_suggest"] = False
    out["is_demo"] = False
    return out


async def _create_shared_subscription(
    context: ContextTypes.DEFAULT_TYPE,
    db: Database,
    user_id: int,
    lang: str,
    snapshot: dict,
) -> tuple[int, str]:
    """Create DM clone from share snapshot. Returns (sub_id, confirmation text)."""
    from config import MAX_SUBSCRIPTIONS_PER_OWNER
    from types import SimpleNamespace

    if len(_subs_for_owner(db, user_id)) >= MAX_SUBSCRIPTIONS_PER_OWNER:
        raise ValueError("sub_limit")

    login = str(snapshot.get("twitch_username") or "").strip().lower()
    clone = _share_clone_snapshot(snapshot, user_id)
    clone["twitch_username"] = login
    type_ok = await prem.alert_type_entitled(
        context.bot,
        db,
        user_id,
        SimpleNamespace(
            notify_on_live=bool(clone.get("notify_on_live", True)),
            notify_on_end=bool(clone.get("notify_on_end")),
            notify_on_category_change=bool(clone.get("notify_on_category_change")),
            schedule_reminder_configured=bool(
                clone.get("schedule_reminder_configured")
            ),
            twitch_username=login,
        ),
    )
    enabled = type_ok and await prem.may_enable_subscription_async(
        context.bot, db, user_id, twitch_username=login
    )
    clone["enabled"] = enabled
    sub_id = _add_subscription_from_snapshot(db, user_id, clone, enabled=enabled)
    analytics.capture(
        user_id,
        "alert_share_accepted",
        {"sub_id": sub_id, "enabled": enabled, "channel": login},
    )
    sub_num = _owner_sub_number(db, user_id, sub_id)
    text = t("share_created", lang, sub_id=sub_num, username=login or "—")
    if not enabled:
        if not type_ok:
            text += "\n" + t(
                "premium_enable_need_feature",
                lang,
                feature=t(prem.feature_label_key("alert_types"), lang),
            )
        else:
            text += "\n" + t(
                "share_created_paused",
                lang,
                limit=prem.free_active_limit(),
            )
    return sub_id, text


async def on_share_show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    if not _share_enabled(db, user_id):
        await query.answer()
        return
    try:
        sub_id = int(query.data.split(":", 1)[1])
    except (TypeError, ValueError, IndexError):
        await query.answer()
        return
    sub = db.get_subscription(sub_id, user_id)
    if sub is None or not _sub_in_current_mode(sub, user_id):
        await query.answer(t("sub_not_found", lang), show_alert=True)
        return
    bot_username = await _bot_username(context.bot, context.application.bot_data)
    link = _share_link_for_sub(db, user_id, sub, bot_username)
    if not link:
        await query.answer(t("share_invalid", lang), show_alert=True)
        return
    await query.answer()
    await context.bot.send_message(
        user_id,
        t("share_friend_prompt", lang, link=link),
        disable_web_page_preview=True,
    )


async def offer_shared_alert(
    bot,
    db: Database,
    user_id: int,
    lang: str,
    token: str,
) -> None:
    snapshot = db.get_alert_share_snapshot(token)
    if not snapshot:
        await bot.send_message(user_id, t("share_invalid", lang))
        return
    username = str(snapshot.get("twitch_username") or "").strip() or "—"
    await bot.send_message(
        user_id,
        t(
            "share_offer",
            lang,
            username=username,
            alert_type=_share_alert_type_label(snapshot, lang),
        ),
        reply_markup=_share_offer_keyboard(lang, token),
    )


async def on_share_accept(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    token = query.data.split(":", 1)[1]
    db: Database = context.application.bot_data["db"]
    context.user_data.pop("pending_share_token", None)
    snapshot = db.get_alert_share_snapshot(token)
    if not snapshot:
        await query.edit_message_text(t("share_invalid", lang))
        return

    twitch_uid = str(snapshot.get("twitch_user_id") or "")
    existing = _existing_sub_for_twitch(db, user_id, twitch_uid)
    if existing:
        await query.edit_message_text(
            t("channel_dup_prompt", lang),
            reply_markup=_share_dup_keyboard(lang, existing.id, token),
        )
        return

    from config import MAX_SUBSCRIPTIONS_PER_OWNER

    try:
        _sub_id, text = await _create_shared_subscription(
            context, db, user_id, lang, snapshot
        )
    except ValueError:
        await query.edit_message_text(
            t("sub_limit", lang, limit=MAX_SUBSCRIPTIONS_PER_OWNER)
        )
        return

    await context.bot.send_message(
        user_id,
        t("menu_subs", lang),
        reply_markup=_subs_kb(lang, db, user_id),
    )
    await context.bot.send_message(user_id, text)
    try:
        await query.edit_message_text("✓")
    except BadRequest:
        pass


async def on_share_dup_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    try:
        sub_id = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        return
    db: Database = context.application.bot_data["db"]
    sub = db.get_subscription(sub_id, user_id)
    if not sub or not _sub_in_current_mode(sub, user_id):
        await query.edit_message_text(t("sub_not_found", lang))
        return
    sub_num = _owner_sub_number(db, user_id, sub_id)
    show_adv = await prem.advanced_mode_on(
        context.bot, db, user_id, channel=sub.twitch_username
    )
    edit_text = _edit_menu_text(
        lang,
        sub_id=sub_num,
        username=sub.twitch_username,
        show_advanced=show_adv,
    )
    edit_markup = _edit_options_for_sub(sub, lang, show_advanced=show_adv)
    await context.bot.send_message(
        user_id,
        t("menu_subs", lang),
        reply_markup=_subs_kb(lang, db, user_id),
    )
    await context.bot.send_message(
        user_id,
        edit_text,
        reply_markup=edit_markup,
        parse_mode=ParseMode.HTML,
    )
    try:
        await query.edit_message_text("✓")
    except BadRequest:
        pass


async def on_share_dup_continue(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    token = query.data.split(":", 2)[2]
    db: Database = context.application.bot_data["db"]
    snapshot = db.get_alert_share_snapshot(token)
    if not snapshot:
        await query.edit_message_text(t("share_invalid", lang))
        return

    from config import MAX_SUBSCRIPTIONS_PER_OWNER

    try:
        _sub_id, text = await _create_shared_subscription(
            context, db, user_id, lang, snapshot
        )
    except ValueError:
        await query.edit_message_text(
            t("sub_limit", lang, limit=MAX_SUBSCRIPTIONS_PER_OWNER)
        )
        return

    await context.bot.send_message(
        user_id,
        t("menu_subs", lang),
        reply_markup=_subs_kb(lang, db, user_id),
    )
    await context.bot.send_message(user_id, text)
    try:
        await query.edit_message_text("✓")
    except BadRequest:
        pass


async def on_share_decline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    context.user_data.pop("pending_share_token", None)
    await query.edit_message_text(t("share_declined", lang))

