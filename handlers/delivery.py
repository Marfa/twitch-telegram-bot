from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timedelta, timezone
from io import BytesIO

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, WebAppInfo
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter

import analytics
from bot_helpers import _user_notifications_paused
from db import Database, Subscription
from handlers.alert_history import _vod_offset_seconds
from i18n import DEFAULT_LOCALE, delivery_fail_notice_keyboard, stored_typo_fix_keyboard, t
from twitch import (
    TwitchClient,
    find_placeholder_typos,
    fix_placeholder_typos,
    is_game_cover_image,
    resolve_sub_image_photo,
)

logger = logging.getLogger(__name__)

_TELEGRAM_CAPTION_LIMIT = 1024


def _effective_image_position(sub: Subscription) -> str:
    position = (sub.image_position or "").strip()
    if position in ("before", "after"):
        return position
    # Game cover always stores "before" in the wizard; recover if position was lost.
    if is_game_cover_image(sub.image_file_id):
        return "before"
    return ""


async def _send_photo_with_url_fallback(bot, *, chat_id: int, photo: str, **kwargs):
    """send_photo by URL; if Telegram cannot fetch the CDN, upload bytes ourselves."""
    try:
        return await bot.send_photo(chat_id=chat_id, photo=photo, **kwargs)
    except BadRequest:
        if not (isinstance(photo, str) and photo.startswith(("http://", "https://"))):
            raise

        def _download() -> bytes:
            resp = requests.get(photo, timeout=20)
            resp.raise_for_status()
            return resp.content

        try:
            data = await asyncio.to_thread(_download)
        except Exception:
            logger.exception("Failed to download alert image %s", photo)
            raise
        return await bot.send_photo(
            chat_id=chat_id,
            photo=InputFile(BytesIO(data), filename="cover.jpg"),
            **kwargs,
        )

async def _resolve_chat_display_name(bot, sub: Subscription) -> str:
    try:
        chat = await bot.get_chat(sub.chat_id)
        if sub.dest_type == "dm":
            parts = [chat.first_name or "", chat.last_name or ""]
            name = " ".join(part for part in parts if part).strip()
            if name:
                return name
        elif chat.title:
            return chat.title
        if chat.username:
            return f"@{chat.username}"
    except (BadRequest, Forbidden) as exc:
        logger.debug("Cannot resolve chat name for %s: %s", sub.chat_id, exc)
    except Exception:
        logger.exception("Unexpected error resolving chat name for %s", sub.chat_id)
    return str(sub.chat_id)


def _message_link(chat_id: int, message_id: int, thread_id: int | None = None) -> str:
    s = str(chat_id)
    if s.startswith("-100"):
        internal = s[4:]
    else:
        internal = str(abs(chat_id))
    if thread_id:
        return f"https://t.me/c/{internal}/{thread_id}/{message_id}"
    return f"https://t.me/c/{internal}/{message_id}"


async def _deliver_alert_content(
    bot,
    *,
    chat_id: int,
    text: str,
    thread_id: int | None = None,
    image_file_id: str | None = None,
    image_position: str = "",
    disable_link_preview: bool = False,
    reply_markup=None,
):
    """Send alert text, optionally with image above/below. Returns the primary message."""
    thread_kwargs: dict = {}
    if thread_id:
        thread_kwargs["message_thread_id"] = thread_id
    markup_kwargs: dict = {}
    if reply_markup is not None:
        markup_kwargs["reply_markup"] = reply_markup

    file_id = image_file_id
    position = (image_position or "").strip()
    # Image posts always disable link preview (caption has no separate preview toggle).
    if file_id and position in ("before", "after"):
        disable_link_preview = True

    async def _photo(**photo_kwargs):
        return await _send_photo_with_url_fallback(
            bot, chat_id=chat_id, photo=file_id, **photo_kwargs
        )

    if file_id and position in ("before", "after") and len(text) <= _TELEGRAM_CAPTION_LIMIT:
        try:
            return await _photo(
                caption=text,
                show_caption_above_media=(position == "after"),
                **thread_kwargs,
                **markup_kwargs,
            )
        except BadRequest as exc:
            logger.warning(
                "Photo send failed for %s (%s); falling back to text-only",
                chat_id,
                exc,
            )

    elif file_id and position in ("before", "after"):
        if position == "before":
            try:
                await _photo(**thread_kwargs)
            except BadRequest as exc:
                logger.warning(
                    "Photo send failed for %s (%s); falling back to text-only",
                    chat_id,
                    exc,
                )
            else:
                text_kwargs: dict = {
                    "chat_id": chat_id,
                    "text": text,
                    **thread_kwargs,
                    **markup_kwargs,
                }
                if disable_link_preview:
                    text_kwargs["disable_web_page_preview"] = True
                return await bot.send_message(**text_kwargs)
        else:
            text_kwargs = {
                "chat_id": chat_id,
                "text": text,
                **thread_kwargs,
                **markup_kwargs,
            }
            if disable_link_preview:
                text_kwargs["disable_web_page_preview"] = True
            msg = await bot.send_message(**text_kwargs)
            try:
                await _photo(**thread_kwargs)
            except BadRequest as exc:
                logger.warning(
                    "Photo send failed for %s after text (%s)",
                    chat_id,
                    exc,
                )
            return msg

    kwargs: dict = {"chat_id": chat_id, "text": text, **thread_kwargs, **markup_kwargs}
    if disable_link_preview:
        kwargs["disable_web_page_preview"] = True
    return await bot.send_message(**kwargs)


def _alert_chat_button_markup(sub: Subscription, lang: str) -> InlineKeyboardMarkup | None:
    if not sub.attach_chat_button:
        return None
    from chat_webapp import alert_chat_button_url

    url = alert_chat_button_url(
        login=sub.twitch_username,
        lang=lang,
        user_id=sub.owner_id,
    )
    if not url:
        return None
    # Telegram accepts web_app inline buttons only in private chats with the bot.
    # Groups/channels get a URL button (same Mini App page) — otherwise Button_type_invalid.
    if sub.dest_type == "dm":
        button = InlineKeyboardButton(
            t("alert_chat_button", lang),
            web_app=WebAppInfo(url=url),
        )
    else:
        button = InlineKeyboardButton(t("alert_chat_button", lang), url=url)
    return InlineKeyboardMarkup([[button]])


# ponytail: in-memory dedupe; resets on restart (acceptable for owner DM notices).
_DELIVERY_FAIL_NOTICE_COOLDOWN = timedelta(hours=24)
_delivery_fail_notified: dict[int, datetime] = {}

_USER_BLOCKED_NEEDLES = (
    "blocked by the user",
    "user is deactivated",
    "user is deleted",
)
_CHAT_UNREACHABLE_NEEDLES = (
    "chat not found",
    "bot is not a member",
    "bot was kicked",
    "have no rights to send",
    "not enough rights",
    "need administrator rights",
    "group chat was deleted",
    "channel chat was deleted",
    "peer_id_invalid",
    "chat_id is empty",
)


def _exc_text(exc: BaseException) -> str:
    return str(exc).lower()


def _is_user_blocked_error(exc: BaseException) -> bool:
    msg = _exc_text(exc)
    return any(n in msg for n in _USER_BLOCKED_NEEDLES)


def _is_chat_unreachable_error(exc: BaseException) -> bool:
    msg = _exc_text(exc)
    return any(n in msg for n in _CHAT_UNREACHABLE_NEEDLES)


def _mark_destination_unreachable(db: Database, sub: Subscription, exc: BaseException) -> None:
    if sub.dest_type == "dm":
        if _is_user_blocked_error(exc) or _is_chat_unreachable_error(exc):
            apply_user_blocked(db, sub.chat_id)
        return
    if _is_chat_unreachable_error(exc):
        apply_chat_unreachable(db, sub.chat_id)
        return
    if _is_user_blocked_error(exc):
        apply_user_blocked(db, sub.owner_id)


def resume_delivery_for_chat(db: Database, chat_id: int) -> int:
    """Clear delivery_paused for chat; re-enable via active-subscription gate."""
    import premium as prem

    resumed = 0
    for sub in db.list_delivery_paused_for_chat(chat_id):
        can = prem.may_enable_subscription(
            db,
            sub.owner_id,
            demo=bool(sub.is_demo),
            twitch_username=sub.twitch_username,
        )
        db.clear_delivery_paused(sub.id, enabled=can)
        if can:
            resumed += 1
    return resumed


def apply_user_blocked(db: Database, user_id: int) -> int:
    already = db.is_bot_blocked(user_id)
    db.set_bot_blocked(user_id, True)
    if not already:
        analytics.capture(user_id, "bot_blocked")
    return db.pause_delivery_for_chat(user_id)


def clear_user_blocked(db: Database, user_id: int) -> int:
    was_blocked = db.is_bot_blocked(user_id)
    db.set_bot_blocked(user_id, False)
    db.set_chat_unreachable(user_id, False)
    if was_blocked:
        analytics.capture(user_id, "bot_unblocked")
    return resume_delivery_for_chat(db, user_id)


def apply_chat_unreachable(db: Database, chat_id: int) -> int:
    db.set_chat_unreachable(chat_id, True)
    return db.pause_delivery_for_chat(chat_id)


def clear_chat_unreachable(db: Database, chat_id: int) -> int:
    db.set_chat_unreachable(chat_id, False)
    return resume_delivery_for_chat(db, chat_id)


def _delivery_fail_notice_due(sub_id: int, *, now: datetime | None = None) -> bool:
    last = _delivery_fail_notified.get(sub_id)
    if last is None:
        return True
    at = now or datetime.now(timezone.utc)
    return at - last >= _DELIVERY_FAIL_NOTICE_COOLDOWN


def _delivery_fail_chat_label(display_name: str, chat_id: int) -> str:
    cid = str(chat_id)
    if display_name == cid:
        return cid
    return f"{display_name} ({chat_id})"


def _owner_typo_report(
    db: Database, owner_id: int
) -> tuple[list[tuple[str, str]], list[Subscription]]:
    seen_typos: set[tuple[str, str]] = set()
    typo_lines: list[tuple[str, str]] = []
    affected: list[Subscription] = []
    for sub in db.get_subscriptions_by_owner(owner_id):
        typos = find_placeholder_typos(sub.message_template)
        if not typos:
            continue
        affected.append(sub)
        for pair in typos:
            if pair in seen_typos:
                continue
            seen_typos.add(pair)
            typo_lines.append(pair)
    return typo_lines, affected


def _format_stored_typo_notice(
    db: Database,
    owner_id: int,
    lang: str,
    typo_lines: list[tuple[str, str]],
    affected: list[Subscription],
) -> str:
    from handlers.subscriptions import _alert_type_from_sub, _alert_type_label, _owner_sub_number

    typos = "\n".join(
        t(
            "template_typo_item",
            lang,
            found=html.escape(found),
            suggested=html.escape(suggested),
        )
        for found, suggested in typo_lines
    )
    subs = "\n".join(
        t(
            "stored_typo_notice_sub",
            lang,
            sub_id=_owner_sub_number(db, owner_id, sub.id),
            username=html.escape(sub.twitch_username),
            alert_type=html.escape(
                _alert_type_label(_alert_type_from_sub(sub), lang)
            ),
        )
        for sub in affected
    )
    return t("stored_typo_notice_prompt", lang, typos=typos, subs=subs)


async def _maybe_notify_stored_template_typos(
    bot,
    db: Database,
    sub: Subscription,
) -> None:
    if not find_placeholder_typos(sub.message_template):
        return
    if not db.mark_template_typo_notice_sent(sub.owner_id):
        return
    lang = db.get_user_locale(sub.owner_id) or DEFAULT_LOCALE
    typo_lines, affected = _owner_typo_report(db, sub.owner_id)
    if not typo_lines:
        return
    try:
        await bot.send_message(
            sub.owner_id,
            _format_stored_typo_notice(db, sub.owner_id, lang, typo_lines, affected),
            parse_mode=ParseMode.HTML,
            reply_markup=stored_typo_fix_keyboard(lang),
        )
    except (BadRequest, Forbidden) as exc:
        logger.warning(
            "Cannot notify owner %s about template typos: %s",
            sub.owner_id,
            exc,
        )


async def on_stored_template_typo_fix(update, context) -> None:
    query = update.callback_query
    await query.answer()
    owner_id = query.from_user.id
    db: Database = context.application.bot_data["db"]
    lang = db.get_user_locale(owner_id) or DEFAULT_LOCALE
    if not query.data.endswith(":1"):
        await query.edit_message_reply_markup(reply_markup=None)
        return

    fixed = 0
    for sub in db.get_subscriptions_by_owner(owner_id):
        if not find_placeholder_typos(sub.message_template):
            continue
        template = fix_placeholder_typos(sub.message_template)
        if db.update_subscription(sub.id, owner_id, message_template=template):
            fixed += 1
    await query.edit_message_reply_markup(reply_markup=None)
    if fixed:
        await context.bot.send_message(owner_id, t("stored_typo_fixed", lang))


async def _maybe_notify_delivery_failure(
    bot,
    db: Database,
    sub: Subscription,
    exc: BaseException,
) -> None:
    from handlers.subscriptions import _owner_sub_number

    if sub.dest_type == "dm":
        return
    if db.is_bot_blocked(sub.owner_id):
        return
    if not _delivery_fail_notice_due(sub.id):
        return
    lang = db.get_user_locale(sub.owner_id) or DEFAULT_LOCALE
    chat_label = _delivery_fail_chat_label(
        await _resolve_chat_display_name(bot, sub), sub.chat_id
    )
    try:
        await bot.send_message(
            sub.owner_id,
            t(
                "delivery_fail_notice",
                lang,
                sub_id=_owner_sub_number(db, sub.owner_id, sub.id),
                twitch_username=sub.twitch_username,
                chat_name=chat_label,
                reason=str(exc),
            ),
            reply_markup=delivery_fail_notice_keyboard(sub.id, lang),
        )
        _delivery_fail_notified[sub.id] = datetime.now(timezone.utc)
    except (BadRequest, Forbidden) as notify_exc:
        if _is_user_blocked_error(notify_exc):
            apply_user_blocked(db, sub.owner_id)
        logger.warning(
            "Cannot notify owner %s about delivery failure: %s",
            sub.owner_id,
            notify_exc,
        )


async def _send_notification(
    bot,
    db: Database,
    sub: Subscription,
    text: str,
    *,
    alert_type: str = "live",
    stream: dict | None = None,
    stream_id: str = "",
    vod_offset_seconds: int | None = None,
    twitch: TwitchClient | None = None,
) -> bool:
    if _user_notifications_paused(db, sub.owner_id):
        return True
    if sub.dest_type == "dm" and db.is_bot_blocked(sub.chat_id):
        return True
    if db.is_chat_unreachable(sub.chat_id):
        return True
    await _maybe_notify_stored_template_typos(bot, db, sub)
    if sub.delete_previous and sub.dest_type != "dm":
        to_delete: list[tuple[int, int]] = []
        seen_msg: set[int] = set()
        if sub.last_message_id and sub.last_message_id not in seen_msg:
            to_delete.append((sub.id, sub.last_message_id))
            seen_msg.add(sub.last_message_id)
        if sub.delete_other_alerts:
            for sibling in db.get_enabled_by_twitch_user_id(sub.twitch_user_id):
                if sibling.id == sub.id:
                    continue
                if sibling.owner_id != sub.owner_id:
                    continue
                if sibling.chat_id != sub.chat_id:
                    continue
                if (sibling.thread_id or None) != (sub.thread_id or None):
                    continue
                if not sibling.last_message_id or sibling.last_message_id in seen_msg:
                    continue
                to_delete.append((sibling.id, sibling.last_message_id))
                seen_msg.add(sibling.last_message_id)
        for owner_sub_id, message_id in to_delete:
            try:
                await bot.delete_message(chat_id=sub.chat_id, message_id=message_id)
                if owner_sub_id != sub.id:
                    db.set_last_message_id(owner_sub_id, None)
            except (BadRequest, Forbidden) as exc:
                logger.warning(
                    "Cannot delete message %s in %s: %s",
                    message_id,
                    sub.chat_id,
                    exc,
                )
                if sub.notify_delete_fail:
                    lang = db.get_user_locale(sub.owner_id) or DEFAULT_LOCALE
                    link = _message_link(sub.chat_id, message_id, sub.thread_id)
                    try:
                        await bot.send_message(
                            sub.owner_id,
                            t("delete_fail_notice", lang, link=link),
                        )
                    except (BadRequest, Forbidden) as notify_exc:
                        logger.warning(
                            "Cannot notify owner %s about delete failure: %s",
                            sub.owner_id,
                            notify_exc,
                        )

    image_photo = None
    image_position = _effective_image_position(sub)
    preview_off = False
    chat_markup = None
    try:
        lang = db.get_user_locale(sub.owner_id) or DEFAULT_LOCALE
        chat_markup = _alert_chat_button_markup(sub, lang)
        preview_off = (
            bool(sub.disable_link_preview)
            or bool(sub.image_file_id)
            or bool(sub.attach_chat_button)
        )
        image_photo = await asyncio.to_thread(
            resolve_sub_image_photo, sub, stream, twitch
        )
        if is_game_cover_image(sub.image_file_id) and not image_photo:
            logger.warning(
                "Game cover unresolved for sub %s (alert_type=%s); sending text only",
                sub.id,
                alert_type,
            )
        msg = await _deliver_alert_content(
            bot,
            chat_id=sub.chat_id,
            text=text,
            thread_id=sub.thread_id,
            image_file_id=image_photo,
            image_position=image_position,
            disable_link_preview=preview_off,
            reply_markup=chat_markup,
        )
    except RetryAfter as exc:
        await asyncio.sleep(float(exc.retry_after) + 0.5)
        try:
            msg = await _deliver_alert_content(
                bot,
                chat_id=sub.chat_id,
                text=text,
                thread_id=sub.thread_id,
                image_file_id=image_photo,
                image_position=image_position,
                disable_link_preview=preview_off,
                reply_markup=chat_markup,
            )
        except (BadRequest, Forbidden, RetryAfter) as retry_exc:
            logger.warning("Cannot send to %s after RetryAfter: %s", sub.chat_id, retry_exc)
            _mark_destination_unreachable(db, sub, retry_exc)
            await _maybe_notify_delivery_failure(bot, db, sub, retry_exc)
            return False
    except (BadRequest, Forbidden) as exc:
        logger.warning("Cannot send to %s: %s", sub.chat_id, exc)
        _mark_destination_unreachable(db, sub, exc)
        await _maybe_notify_delivery_failure(bot, db, sub, exc)
        return False

    if db.is_chat_unreachable(sub.chat_id):
        clear_chat_unreachable(db, sub.chat_id)
    if sub.dest_type == "dm" and db.is_bot_blocked(sub.chat_id):
        clear_user_blocked(db, sub.chat_id)
    if msg and sub.delete_previous and sub.dest_type != "dm":
        db.set_last_message_id(sub.id, msg.message_id)
    if sub.suppress_repeat_minutes > 0:
        db.set_notify_cooldown(sub.id, sub.suppress_repeat_minutes)
    # History is for the user's DM inbox only — skip channel/group destinations.
    if sub.dest_type == "dm":
        try:
            db.add_alert_history(
                sub.owner_id,
                subscription_id=sub.id,
                twitch_username=sub.twitch_username,
                alert_type=alert_type,
                message_text=text,
                twitch_user_id=sub.twitch_user_id,
                stream_id=(
                    (stream_id or "").strip()
                    or (str(stream.get("id") or "") if stream else "")
                ),
                vod_offset_seconds=(
                    vod_offset_seconds
                    if vod_offset_seconds is not None
                    else (
                        _vod_offset_seconds(stream)
                        if alert_type == "category"
                        else None
                    )
                ),
            )
        except Exception:
            logger.exception("Failed to record alert history for sub %s", sub.id)
    return True


async def _send_test(
    bot, chat_id: int, thread_id: int | None, text: str, *, db: Database | None = None
) -> bool:
    kwargs: dict = {"chat_id": chat_id, "text": text}
    if thread_id:
        kwargs["message_thread_id"] = thread_id
    try:
        await bot.send_message(**kwargs)
        if db is not None:
            clear_chat_unreachable(db, chat_id)
        return True
    except (BadRequest, Forbidden) as exc:
        logger.warning("Cannot send to %s: %s", chat_id, exc)
        if db is not None and _is_chat_unreachable_error(exc):
            apply_chat_unreachable(db, chat_id)
        elif db is not None and _is_user_blocked_error(exc):
            apply_user_blocked(db, chat_id)
        return False


async def purge_expired_blocked_users(context) -> None:
    db: Database = context.application.bot_data["db"]
    removed = db.purge_expired_blocked_users()
    if removed:
        logger.info("Purged %s user(s) blocked for 365+ days", removed)

