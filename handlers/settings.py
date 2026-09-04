from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    MenuButtonCommands,
    MenuButtonWebApp,
    Update,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter
from telegram.ext import Application, ContextTypes, ConversationHandler

import analytics
import beta as beta_features
import demo_mode
import premium as prem
from bot_helpers import (
    _menu,
    _pulse_wizard_keyboard,
    _settings_kb,
    _user_lang,
    _user_notifications_paused,
    is_private_chat,
    reply_chat_id,
)
from db import Database
from i18n import (
    DEFAULT_LOCALE,
    SUPPORTED_LOCALES,
    all_wizard_nav_buttons,
    beta_mode_keyboard,
    ignored_words_keyboard,
    is_menu_button,
    language_keyboard,
    other_menu,
    sys_notifications_keyboard,
    t,
    whisper_alerts_keyboard,
)
from twitch import TwitchClient, merge_ignore_keywords, normalize_ignore_keywords

logger = logging.getLogger(__name__)


def _log_whisper_oauth_missing_fields() -> None:
    logger.warning("Whisper OAuth missing token fields")


def _log_whisper_eventsub_failed(exc_type: str) -> None:
    logger.warning("Whisper EventSub subscribe failed (%s)", exc_type)


def _log_whisper_eventsub_delete_failed(exc_type: str) -> None:
    logger.warning("Delete whisper EventSub failed: %s", exc_type)


def _log_whisper_enable_failed(exc_type: str) -> None:
    logger.warning("Enable whisper alerts failed: %s", exc_type)


def _lang_select_state() -> int:
    from bot import LANG_SELECT

    return LANG_SELECT

def _global_ignore_state() -> int:
    from bot import GLOBAL_IGNORE_KEYWORDS

    return GLOBAL_IGNORE_KEYWORDS

async def open_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    await update.effective_message.reply_text(
        t("menu_settings", lang),
        reply_markup=_settings_kb(lang, db, user_id),
    )

async def open_other_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    await update.effective_message.reply_text(
        t("menu_other", lang),
        reply_markup=other_menu(lang),
    )

async def open_stream_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    from chat_webapp import BETA_FEATURE_ID, chat_webapp_url, stream_chat_open_markup

    if not beta_features.is_enabled(db, user_id, BETA_FEATURE_ID):
        await update.effective_message.reply_text(
            t("chat_beta_required", lang),
            reply_markup=other_menu(lang),
        )
        return
    open_url = chat_webapp_url(
        lang=lang if lang in SUPPORTED_LOCALES else None,
        user_id=user_id,
    )
    if not open_url:
        await update.effective_message.reply_text(
            t("chat_beta_required", lang),
            reply_markup=other_menu(lang),
        )
        return
    await sync_stream_chat_menu_button(context.bot, db, user_id)
    await update.effective_message.reply_text(
        t("menu_other", lang),
        reply_markup=other_menu(lang),
    )
    await update.effective_message.reply_text(
        t("chat_open_hint", lang),
        reply_markup=stream_chat_open_markup(
            lang, open_url, private=is_private_chat(update)
        ),
    )

async def start_language_change(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    context.user_data["after_lang"] = "settings"
    await update.effective_message.reply_text(
        t("lang_pick", lang),
        reply_markup=language_keyboard(lang),
    )
    return _lang_select_state()

async def open_sys_notifications_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    await update.effective_message.reply_text(
        t("sys_notifications_menu", lang),
        reply_markup=sys_notifications_keyboard(
            lang,
            updates_enabled=db.get_receive_bot_updates(user_id),
            availability_enabled=db.get_receive_availability_updates(user_id),
            other_enabled=db.get_receive_other_updates(user_id),
            sync_enabled=db.get_receive_sync_updates(user_id),
        ),
    )

def _beta_mode_features_block(
    db: Database, user_id: int, lang: str
) -> tuple[str, list[tuple[str, str, bool, str]]]:
    features = beta_features.list_features()
    if not features:
        return t("beta_mode_empty", lang), []
    lines: list[str] = []
    kb_rows: list[tuple[str, str, bool, str]] = []
    for feat in features:
        title = t(feat.title_key, lang)
        desc = t(feat.description_key, lang)
        enrolled = beta_features.is_enrolled(db, user_id, feat.id)
        lines.append(f"<b>{html.escape(title)}</b>\n{html.escape(desc)}")
        kb_rows.append(
            (
                feat.id,
                title,
                enrolled,
                beta_features.issue_url(feat, user_id=user_id),
            )
        )
    block = "\n\n".join(lines)
    if beta_features.is_admin(user_id) and not demo_mode.is_active(user_id):
        block += f"\n\n<i>{html.escape(t('beta_mode_admin_note', lang))}</i>"
    return block, kb_rows

async def open_beta_mode_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    analytics.capture(user_id, "beta_menu_opened")
    features_block, kb_rows = _beta_mode_features_block(db, user_id, lang)
    text = t("beta_mode_menu", lang, features_block=features_block)
    markup = beta_mode_keyboard(lang, kb_rows) if kb_rows else None
    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )

async def on_beta_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    data = query.data or ""
    prefix = "beta:toggle:"
    if not data.startswith(prefix):
        await query.answer()
        return
    feature_id = data[len(prefix) :].strip()
    feat = beta_features.get_feature(feature_id)
    if feat is None or feat.stage not in ("alpha", "beta"):
        await query.answer()
        return
    title = t(feat.title_key, lang)
    enrolled = beta_features.is_enrolled(db, user_id, feature_id)
    new_state = not enrolled
    db.set_beta_enrollment(user_id, feature_id, new_state)
    analytics.capture(
        user_id,
        "beta_feature_opt_in" if new_state else "beta_feature_opt_out",
        {"feature_id": feature_id, "premium_feature_id": feat.premium_feature_id or ""},
    )
    if feature_id == "stream-chat":
        await sync_stream_chat_menu_button(context.bot, db, user_id)
        chat_id = reply_chat_id(update)
        await context.bot.send_message(
            chat_id,
            t("menu_main", lang),
            reply_markup=_menu(lang, user_id),
        )
        if new_state:
            from chat_webapp import chat_webapp_url, stream_chat_open_markup

            open_url = chat_webapp_url(
                lang=lang if lang in SUPPORTED_LOCALES else None,
                user_id=user_id,
            )
            if open_url:
                await context.bot.send_message(
                    chat_id,
                    t("chat_open_hint", lang),
                    reply_markup=stream_chat_open_markup(
                        lang, open_url, private=is_private_chat(update)
                    ),
                )
    toast = t(
        "beta_mode_opt_in" if new_state else "beta_mode_opt_out",
        lang,
        name=title,
    )
    await query.answer()
    features_block, kb_rows = _beta_mode_features_block(db, user_id, lang)
    text = t("beta_mode_menu", lang, features_block=features_block)
    try:
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=beta_mode_keyboard(lang, kb_rows),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except BadRequest:
        pass
    await context.bot.send_message(
        reply_chat_id(update),
        toast,
        reply_markup=_settings_kb(lang, db, user_id),
    )

async def start_ignored_words(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    if not await prem.has_feature(context.bot, db, user_id, "ignore_keywords"):
        from premium_handlers import send_premium_screen

        await update.effective_message.reply_text(
            t("premium_gate", lang, action=t("premium_gate_action_cancel", lang))
        )
        await send_premium_screen(context.bot, user_id, lang, db, update=update)
        return ConversationHandler.END
    from bot import _ignore_keywords_current_label

    current_raw = db.get_global_ignore_keywords(user_id)
    has_words = bool(current_raw.strip())
    current = _ignore_keywords_current_label(current_raw, lang)
    if has_words:
        current = f"<code>{html.escape(current)}</code>"
    await update.effective_message.reply_text(
        t(
            "ignored_words_prompt",
            lang,
            current=current,
            hint=t(
                "ignored_words_hint_edit" if has_words else "ignored_words_hint_empty",
                lang,
            ),
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=ignored_words_keyboard(lang, has_words=has_words),
    )
    await _pulse_wizard_keyboard(context.bot, user_id, lang, back=False)
    return _global_ignore_state()

async def receive_ignored_words(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    text = (update.effective_message.text or "").strip()
    if text in all_wizard_nav_buttons():
        await update.effective_message.reply_text(
            t("menu_settings", lang),
            reply_markup=_settings_kb(lang, db, user_id),
        )
        context.user_data.clear()
        return ConversationHandler.END
    if is_menu_button(text):
        await update.effective_message.reply_text(t("finish_setup_first", lang))
        return _global_ignore_state()
    added = normalize_ignore_keywords(text)
    if not added:
        await update.effective_message.reply_text(t("ignored_words_hint_empty", lang))
        return _global_ignore_state()
    keywords = merge_ignore_keywords(db.get_global_ignore_keywords(user_id), added)
    db.set_global_ignore_keywords(user_id, keywords)
    await update.effective_message.reply_text(
        t("ignored_words_saved", lang),
        reply_markup=_settings_kb(lang, db, user_id),
    )
    context.user_data.clear()
    return ConversationHandler.END

async def receive_ignored_words_clear(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    db.set_global_ignore_keywords(user_id, "")
    await query.edit_message_text("✓")
    await context.bot.send_message(
        user_id,
        t("ignored_words_cleared", lang),
        reply_markup=_settings_kb(lang, db, user_id),
    )
    context.user_data.clear()
    return ConversationHandler.END

async def receive_ignored_words_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    await query.edit_message_text("✓")
    await context.bot.send_message(
        user_id,
        t("menu_settings", lang),
        reply_markup=_settings_kb(lang, db, user_id),
    )
    context.user_data.clear()
    return ConversationHandler.END

def _whisper_alerts_ready() -> bool:
    from config import twitch_eventsub_callback_url, twitch_oauth_redirect_uri

    return bool(twitch_oauth_redirect_uri() and twitch_eventsub_callback_url())

def _enable_whisper_eventsub(
    db: Database,
    twitch: TwitchClient,
    owner_id: int,
    *,
    refresh: str,
    twitch_user_id: str,
    twitch_login: str,
) -> str:
    from eventsub import eventsub_callback_url, eventsub_secret

    callback = eventsub_callback_url()
    if not callback:
        raise RuntimeError("no_callback")
    sub_id = twitch.create_whisper_eventsub(
        user_id=twitch_user_id,
        callback=callback,
        secret=eventsub_secret(),
    )
    db.upsert_whisper_alert(
        owner_id,
        enabled=True,
        twitch_user_id=twitch_user_id,
        twitch_login=twitch_login,
        refresh_token=refresh,
        eventsub_id=sub_id,
    )
    return sub_id

async def _send_whisper_oauth_prompt(
    bot: Any,
    twitch: TwitchClient,
    user_id: int,
    lang: str,
) -> None:
    from config import twitch_oauth_redirect_uri
    from health import create_oauth_state
    from twitch import WHISPERS_SCOPE

    if not _whisper_alerts_ready():
        await bot.send_message(user_id, t("whisper_alerts_oauth_unavailable", lang))
        return
    redirect = twitch_oauth_redirect_uri()
    state = create_oauth_state(user_id, lang, purpose="whispers")
    url = twitch.build_authorize_url(
        redirect_uri=redirect, state=state, scopes=WHISPERS_SCOPE
    )
    await bot.send_message(
        user_id,
        t("whisper_alerts_oauth_prompt", lang),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        t("whisper_alerts_oauth_button", lang), url=url
                    )
                ]
            ]
        ),
    )

async def open_whisper_alerts_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    row = db.get_whisper_alert(user_id)
    enabled = bool(row and row.enabled)
    await update.effective_message.reply_text(
        t("whisper_alerts_screen", lang),
        parse_mode=ParseMode.HTML,
        reply_markup=whisper_alerts_keyboard(lang, enabled=enabled),
    )

async def on_whisper_alerts_toggle(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    from twitch import WHISPERS_SCOPE

    query = update.callback_query
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    db.upsert_user(user_id)
    row = db.get_whisper_alert(user_id)
    currently_on = bool(row and row.enabled)
    if currently_on:
        await query.answer()
        if row and row.eventsub_id:
            try:
                twitch.delete_eventsub_subscription(row.eventsub_id)
            except Exception as exc:
                _log_whisper_eventsub_delete_failed(type(exc).__name__)
        db.set_whisper_alert_enabled(user_id, False, eventsub_id="")
        analytics.capture(user_id, "whisper_alerts_toggled", {"enabled": False})
        await query.edit_message_reply_markup(
            reply_markup=whisper_alerts_keyboard(lang, enabled=False)
        )
        return
    if not _whisper_alerts_ready():
        await query.answer()
        await context.bot.send_message(
            user_id, t("whisper_alerts_oauth_unavailable", lang)
        )
        return
    if not row or not row.refresh_token:
        await query.answer()
        await _send_whisper_oauth_prompt(context.bot, twitch, user_id, lang)
        return
    try:
        token_data = twitch.refresh_user_token(row.refresh_token)
        access = token_data.get("access_token") or ""
        refresh = token_data.get("refresh_token") or row.refresh_token
        if not access or not twitch.token_has_scope(access, WHISPERS_SCOPE):
            await query.answer()
            await _send_whisper_oauth_prompt(context.bot, twitch, user_id, lang)
            return
        user = twitch.get_token_user(access) or {}
        twitch_user_id = str(user.get("id") or row.twitch_user_id)
        twitch_login = str(user.get("login") or row.twitch_login)
        _enable_whisper_eventsub(
            db,
            twitch,
            user_id,
            refresh=refresh,
            twitch_user_id=twitch_user_id,
            twitch_login=twitch_login,
        )
    except Exception as exc:
        _log_whisper_enable_failed(type(exc).__name__)
        await query.answer()
        await _send_whisper_oauth_prompt(context.bot, twitch, user_id, lang)
        return
    await query.answer()
    analytics.capture(user_id, "whisper_alerts_toggled", {"enabled": True})
    await query.edit_message_reply_markup(
        reply_markup=whisper_alerts_keyboard(lang, enabled=True)
    )

async def sync_stream_chat_menu_button(bot: Any, db: Database, user_id: int) -> None:
    """Show or clear the Chat Menu Button for a private chat."""
    from chat_webapp import BETA_FEATURE_ID, chat_webapp_url

    lang = db.get_user_locale(user_id) or DEFAULT_LOCALE
    url = chat_webapp_url(
        lang=lang if lang in SUPPORTED_LOCALES else None,
        user_id=user_id,
    )
    enabled = bool(url) and beta_features.is_enabled(db, user_id, BETA_FEATURE_ID)
    menu_button = (
        MenuButtonWebApp(
            text=t("menu_btn_chat", lang),
            web_app=WebAppInfo(url=url),
        )
        if enabled
        else MenuButtonCommands()
    )
    # Transient Telegram blips (ReadError etc.); RetryAfter still bubbles for bulk.
    attempts = 3
    for attempt in range(attempts):
        try:
            await bot.set_chat_menu_button(chat_id=user_id, menu_button=menu_button)
            return
        except RetryAfter:
            raise
        except NetworkError:
            if attempt + 1 >= attempts:
                logger.exception(
                    "Failed to sync stream-chat menu button for %s", user_id
                )
                return
            await asyncio.sleep(0.5 * (attempt + 1))
        except Exception:
            logger.exception("Failed to sync stream-chat menu button for %s", user_id)
            return


def _stream_chat_is_ga() -> bool:
    from chat_webapp import BETA_FEATURE_ID

    feat = beta_features.get_feature(BETA_FEATURE_ID)
    return bool(feat and feat.stage == "ga")


async def set_default_stream_chat_menu_button(bot: Any) -> None:
    """Bot-wide default Menu Button (new private chats inherit it)."""
    from chat_webapp import chat_webapp_url

    if not _stream_chat_is_ga():
        return
    url = chat_webapp_url(lang=DEFAULT_LOCALE)
    if not url:
        return
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text=t("menu_btn_chat", DEFAULT_LOCALE),
                web_app=WebAppInfo(url=url),
            ),
        )
    except Exception:
        logger.exception("Failed to set default stream-chat menu button")


async def sync_all_stream_chat_menu_buttons(bot: Any, db: Database) -> None:
    """Per-chat Menu Button for known users (clears Commands overrides after GA)."""
    if not _stream_chat_is_ga():
        return
    user_ids = sorted({int(uid) for uid in db.get_notify_user_ids()})
    ok = 0
    skipped = 0
    for user_id in user_ids:
        if db.is_bot_blocked(user_id):
            skipped += 1
            continue
        try:
            await sync_stream_chat_menu_button(bot, db, user_id)
            ok += 1
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 0.5)
            try:
                await sync_stream_chat_menu_button(bot, db, user_id)
                ok += 1
            except Exception:
                logger.exception("Retry failed stream-chat menu button for %s", user_id)
        # ponytail: ~20 setChatMenuButton/s; ceiling = Telegram flood control.
        await asyncio.sleep(0.05)
    logger.info(
        "stream-chat menu button bulk sync done: ok=%s skipped_blocked=%s total=%s",
        ok,
        skipped,
        len(user_ids),
    )


async def ensure_stream_chat_menu_buttons_for_all(bot: Any, db: Database) -> None:
    """Default Menu Button + one bulk pass over users (GA only)."""
    await set_default_stream_chat_menu_button(bot)
    await sync_all_stream_chat_menu_buttons(bot, db)

async def complete_chat_oauth(
    application: Application,
    owner_id: int,
    error: str | None,
    token_info: dict[str, str] | None,
) -> None:
    db: Database = application.bot_data["db"]
    lang = db.get_user_locale(owner_id) or DEFAULT_LOCALE
    if error:
        await application.bot.send_message(
            owner_id,
            t("chat_oauth_failed", lang),
            reply_markup=_menu(lang, owner_id),
        )
        return
    info = token_info or {}
    refresh = info.get("refresh_token") or ""
    twitch_user_id = info.get("twitch_user_id") or ""
    twitch_login = info.get("twitch_login") or ""
    if not refresh or not twitch_user_id:
        await application.bot.send_message(
            owner_id,
            t("chat_oauth_failed", lang),
            reply_markup=_menu(lang, owner_id),
        )
        return
    db.upsert_chat_auth(
        owner_id,
        twitch_user_id=twitch_user_id,
        twitch_login=twitch_login,
        refresh_token=refresh,
    )
    analytics.capture(owner_id, "stream_chat_oauth_linked", {"twitch_login": twitch_login})
    await application.bot.send_message(
        owner_id,
        t("chat_oauth_done", lang),
        reply_markup=_menu(lang, owner_id),
    )

async def complete_whisper_oauth(
    application: Application,
    owner_id: int,
    error: str | None,
    token_info: dict[str, str] | None,
) -> None:
    db: Database = application.bot_data["db"]
    twitch: TwitchClient = application.bot_data["twitch"]
    lang = db.get_user_locale(owner_id) or DEFAULT_LOCALE
    if error:
        key = (
            "oauth_denied"
            if error == "access_denied"
            else "whisper_alerts_failed"
        )
        await application.bot.send_message(
            owner_id,
            t(key, lang),
            reply_markup=_settings_kb(lang, db, owner_id),
        )
        return
    info = token_info or {}
    if not info.get("access_token") or not info.get("twitch_user_id"):
        _log_whisper_oauth_missing_fields()
        await application.bot.send_message(
            owner_id,
            t("whisper_alerts_failed", lang),
            reply_markup=other_menu(lang),
        )
        return
    refresh = info.get("refresh_token") or ""
    twitch_user_id = info.get("twitch_user_id") or ""
    twitch_login = info.get("twitch_login") or ""
    try:
        _enable_whisper_eventsub(
            db,
            twitch,
            owner_id,
            refresh=refresh,
            twitch_user_id=twitch_user_id,
            twitch_login=twitch_login,
        )
    except Exception as exc:
        # Avoid logger.exception — access/refresh may sit in locals.
        _log_whisper_eventsub_failed(type(exc).__name__)
        await application.bot.send_message(
            owner_id,
            t("whisper_alerts_failed", lang),
            reply_markup=other_menu(lang),
        )
        return
    analytics.capture(owner_id, "whisper_alerts_toggled", {"enabled": True})
    await application.bot.send_message(
        owner_id,
        t("whisper_alerts_enabled", lang),
        reply_markup=other_menu(lang),
    )

async def notify_whisper_received(application: Application, event: Any) -> None:
    from eventsub import format_whisper_alert, whisper_conversation_url

    db: Database = application.bot_data["db"]
    to_user_id = str(getattr(event, "to_user_id", "") or "")
    from_user_id = str(getattr(event, "from_user_id", "") or "")
    if not to_user_id or (from_user_id and from_user_id == to_user_id):
        return
    alerts = db.get_whisper_alerts_by_twitch_user_id(to_user_id)
    if not alerts:
        return
    for alert in alerts:
        if db.is_bot_blocked(alert.owner_id):
            continue
        if _user_notifications_paused(db, alert.owner_id):
            continue
        lang = db.get_user_locale(alert.owner_id) or DEFAULT_LOCALE
        to_login = str(getattr(event, "to_user_login", "") or "") or alert.twitch_login
        url = whisper_conversation_url(
            to_login=to_login,
            from_login=str(getattr(event, "from_user_login", "") or ""),
        )
        text = format_whisper_alert(lang, event, url=url)
        try:
            await application.bot.send_message(
                alert.owner_id,
                text,
                parse_mode=ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except Forbidden:
            from handlers.delivery import apply_user_blocked

            apply_user_blocked(db, alert.owner_id)
        except BadRequest as exc:
            logger.warning(
                "Cannot send whisper alert to %s: %s", alert.owner_id, exc
            )

async def on_whisper_eventsub_revoked(
    application: Application, twitch_user_id: str
) -> None:
    db: Database = application.bot_data["db"]
    owner_ids = db.disable_whisper_alerts_for_twitch_user(twitch_user_id)
    for owner_id in owner_ids:
        if db.is_bot_blocked(owner_id):
            continue
        if _user_notifications_paused(db, owner_id):
            continue
        lang = db.get_user_locale(owner_id) or DEFAULT_LOCALE
        try:
            await application.bot.send_message(
                owner_id, t("whisper_alerts_revoked", lang)
            )
        except Forbidden:
            from handlers.delivery import apply_user_blocked

            apply_user_blocked(db, owner_id)
        except BadRequest as exc:
            logger.warning("Cannot send whisper revoke notice to %s: %s", owner_id, exc)

async def _refresh_sys_notifications_menu(
    query, context: ContextTypes.DEFAULT_TYPE, lang: str, user_id: int
) -> None:
    db: Database = context.application.bot_data["db"]
    await query.edit_message_text(
        t("sys_notifications_menu", lang),
        reply_markup=sys_notifications_keyboard(
            lang,
            updates_enabled=db.get_receive_bot_updates(user_id),
            availability_enabled=db.get_receive_availability_updates(user_id),
            other_enabled=db.get_receive_other_updates(user_id),
            sync_enabled=db.get_receive_sync_updates(user_id),
        ),
    )

async def on_sys_updates_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    db.set_receive_bot_updates(user_id, not db.get_receive_bot_updates(user_id))
    await _refresh_sys_notifications_menu(query, context, lang, user_id)

async def on_sys_availability_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    db.set_receive_availability_updates(
        user_id, not db.get_receive_availability_updates(user_id)
    )
    await _refresh_sys_notifications_menu(query, context, lang, user_id)

async def on_sys_other_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    db.set_receive_other_updates(user_id, not db.get_receive_other_updates(user_id))
    await _refresh_sys_notifications_menu(query, context, lang, user_id)

async def on_sys_sync_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    db.set_receive_sync_updates(user_id, not db.get_receive_sync_updates(user_id))
    await _refresh_sys_notifications_menu(query, context, lang, user_id)

