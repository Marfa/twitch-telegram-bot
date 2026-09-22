from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any

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
    is_dynamic_alert_image,
    is_stream_file_video_preview_image,
    is_stream_capture_preview_image,
    resolve_sub_image_photo,
)

logger = logging.getLogger(__name__)

_TELEGRAM_CAPTION_LIMIT = 1024
# Telegram refuses delete_message after ~48h; purge slightly earlier so it still works.
_DELETE_PREVIOUS_PURGE_AFTER = timedelta(hours=47)


def _effective_image_position(sub: Subscription) -> str:
    position = (sub.image_position or "").strip()
    if position in ("before", "after"):
        return position
    # Dynamic images always store "before" in the wizard; recover if position was lost.
    if is_dynamic_alert_image(sub.image_file_id):
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
    animation_bytes: bytes | None = None,
    video_bytes: bytes | None = None,
    disable_link_preview: bool = False,
    reply_markup=None,
    parse_mode: str | None = None,
    prefer_media_message_id: bool = False,
):
    """Send alert text, optionally with image above/below. Returns the primary message."""
    from message_fx import message_fx_disabled

    # Live alerts: deliver immediately (no typing/draft delay).
    with message_fx_disabled():
        return await _deliver_alert_content_plain(
            bot,
            chat_id=chat_id,
            text=text,
            thread_id=thread_id,
            image_file_id=image_file_id,
            image_position=image_position,
            animation_bytes=animation_bytes,
            video_bytes=video_bytes,
            disable_link_preview=disable_link_preview,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            prefer_media_message_id=prefer_media_message_id,
        )


async def _deliver_alert_content_plain(
    bot,
    *,
    chat_id: int,
    text: str,
    thread_id: int | None = None,
    image_file_id: str | None = None,
    image_position: str = "",
    animation_bytes: bytes | None = None,
    video_bytes: bytes | None = None,
    disable_link_preview: bool = False,
    reply_markup=None,
    parse_mode: str | None = None,
    prefer_media_message_id: bool = False,
):
    """Send alert text, optionally with image/animation/video above/below. Returns the primary message."""
    thread_kwargs: dict = {}
    if thread_id:
        thread_kwargs["message_thread_id"] = thread_id
    markup_kwargs: dict = {}
    if reply_markup is not None:
        markup_kwargs["reply_markup"] = reply_markup
    parse_kwargs: dict = {}
    if parse_mode:
        parse_kwargs["parse_mode"] = parse_mode

    file_id = image_file_id
    position = (image_position or "").strip()
    has_media = bool(
        (file_id or animation_bytes or video_bytes) and position in ("before", "after")
    )
    # Image posts always disable link preview (caption has no separate preview toggle).
    if has_media:
        disable_link_preview = True

    def _plain_fallback(body: str) -> str:
        import html as _html
        import re as _re

        return _html.unescape(_re.sub(r"<[^>]+>", "", body or ""))

    async def _photo(**photo_kwargs):
        return await _send_photo_with_url_fallback(
            bot, chat_id=chat_id, photo=file_id, **photo_kwargs
        )

    async def _animation(**anim_kwargs):
        # Muted H.264 MP4 as Animation — Telegram autoplays inline (no sound).
        return await bot.send_animation(
            chat_id=chat_id,
            animation=InputFile(BytesIO(animation_bytes), filename="preview.mp4"),
            width=640,
            height=360,
            duration=30,
            **anim_kwargs,
        )

    async def _video(**video_kwargs):
        # Muted H.264 as Video. Autoplay is client-dependent (unlike Animation).
        return await bot.send_video(
            chat_id=chat_id,
            video=InputFile(BytesIO(video_bytes), filename="preview.mp4"),
            width=640,
            height=360,
            duration=30,
            supports_streaming=True,
            **video_kwargs,
        )

    async def _send_media(**media_kwargs):
        if animation_bytes:
            return await _animation(**media_kwargs)
        if video_bytes:
            return await _video(**media_kwargs)
        return await _photo(**media_kwargs)

    async def _send_text(**extra):
        text_kwargs: dict = {
            "chat_id": chat_id,
            "text": text,
            **thread_kwargs,
            **markup_kwargs,
            **parse_kwargs,
            **extra,
        }
        if disable_link_preview:
            text_kwargs["disable_web_page_preview"] = True
        try:
            return await bot.send_message(**text_kwargs)
        except BadRequest as exc:
            err = str(exc).lower()
            if parse_kwargs and ("parse" in err or "entity" in err or "tag" in err):
                text_kwargs["text"] = _plain_fallback(text)
                text_kwargs.pop("parse_mode", None)
                return await bot.send_message(**text_kwargs)
            raise

    if has_media and len(text) <= _TELEGRAM_CAPTION_LIMIT:
        try:
            return await _send_media(
                caption=text,
                show_caption_above_media=(position == "after"),
                **thread_kwargs,
                **markup_kwargs,
                **parse_kwargs,
            )
        except BadRequest as exc:
            err = str(exc).lower()
            if parse_kwargs and ("parse" in err or "entity" in err or "tag" in err):
                try:
                    return await _send_media(
                        caption=_plain_fallback(text),
                        show_caption_above_media=(position == "after"),
                        **thread_kwargs,
                        **markup_kwargs,
                    )
                except BadRequest:
                    pass
            else:
                logger.warning(
                    "Media send failed for %s (%s); falling back to text-only",
                    chat_id,
                    exc,
                )

    elif has_media:
        if position == "before":
            try:
                media_msg = await _send_media(**thread_kwargs)
            except BadRequest as exc:
                logger.warning(
                    "Media send failed for %s (%s); falling back to text-only",
                    chat_id,
                    exc,
                )
            else:
                text_msg = await _send_text()
                # Dynamic previews need the media message id for editMessageMedia.
                return media_msg if prefer_media_message_id else text_msg
        else:
            msg = await _send_text()
            try:
                media_msg = await _send_media(**thread_kwargs)
            except BadRequest as exc:
                logger.warning(
                    "Media send failed for %s after text (%s)",
                    chat_id,
                    exc,
                )
                return msg
            return media_msg if prefer_media_message_id else msg

    return await _send_text()


def _alert_chat_button_markup(
    sub: Subscription,
    lang: str,
    *,
    db: Database | None = None,
    bot_username: str = "",
) -> InlineKeyboardMarkup | None:
    import custom_buttons as cbtn

    buttons: list[InlineKeyboardButton] = []
    login = (sub.twitch_username or "").strip().lstrip("@").lower()
    style = cbtn.normalize_button_style(getattr(sub, "button_style", None))
    for btn_def in cbtn.parse_custom_buttons(getattr(sub, "custom_buttons", None)):
        url = str(btn_def.get("url") or "")
        if login and "{username}" in url:
            url = url.replace("{username}", login)
        buttons.append(
            cbtn.styled_inline_button(
                str(btn_def.get("text") or "")[:64],
                url=url,
                style=style,
            )
        )
    if sub.attach_chat_button:
        from chat_webapp import alert_chat_button_url

        url = alert_chat_button_url(
            login=sub.twitch_username,
            lang=lang,
            user_id=sub.owner_id,
        )
        if url:
            # Telegram accepts web_app inline buttons only in private chats with the bot.
            # Groups/channels get a URL button (same Mini App page) — otherwise Button_type_invalid.
            if sub.dest_type == "dm":
                buttons.append(
                    cbtn.styled_inline_button(
                        t("alert_chat_button", lang),
                        web_app=WebAppInfo(url=url),
                        style=style,
                    )
                )
            else:
                buttons.append(
                    cbtn.styled_inline_button(
                        t("alert_chat_button", lang),
                        url=url,
                        style=style,
                    )
                )
    remind_url = _live_remind_button_url(sub, lang, db=db, bot_username=bot_username)
    if remind_url:
        buttons.append(
            cbtn.styled_inline_button(
                t("alert_live_remind_button", lang),
                url=remind_url,
                style=style,
            )
        )
    if not buttons:
        return None
    # Always 2 per row (custom + chat + live-remind share the same grid).
    return InlineKeyboardMarkup(cbtn.chunk_buttons(buttons, per_row=2))


_SHARE_PURPOSE_LIVE_REMIND = "live_remind"


def _live_default_share_snapshot(sub: Subscription, lang: str) -> dict:
    """Share snapshot: stream-start alert for the same channel, other settings default."""
    return {
        "twitch_username": sub.twitch_username,
        "twitch_user_id": sub.twitch_user_id,
        "message_template": t("import_default_template", lang),
        "dest_type": "dm",
        "chat_id": sub.owner_id,
        "thread_id": None,
        "delete_previous": False,
        "notify_delete_fail": False,
        "disable_link_preview": False,
        "strip_name_mentions": False,
        "attach_chat_button": False,
        "attach_live_remind_button": False,
        "custom_buttons": "[]",
        "delay_minutes": 0,
        "suppress_repeat_minutes": 0,
        "schedule_reminder_minutes": 0,
        "schedule_reminder_configured": False,
        "ignore_keywords": "",
        "use_global_ignore": False,
        "image_file_id": None,
        "image_position": "",
        "notify_on_live": True,
        "notify_on_end": False,
        "notify_on_category_change": False,
        "delete_other_alerts": False,
    }


def _live_remind_button_url(
    sub: Subscription,
    lang: str,
    *,
    db: Database | None,
    bot_username: str,
) -> str | None:
    if not db or not bot_username:
        return None
    if not getattr(sub, "attach_live_remind_button", False):
        return None
    is_upcoming = (
        int(sub.schedule_reminder_minutes or 0) > 0
        and not sub.notify_on_live
        and not sub.notify_on_end
        and not sub.notify_on_category_change
    )
    if not is_upcoming:
        return None
    import beta as beta_features

    if not beta_features.is_enabled(db, sub.owner_id, "share-alerts"):
        return None
    token = db.ensure_alert_share_token(
        sub.owner_id,
        sub.id,
        _live_default_share_snapshot(sub, lang),
        purpose=_SHARE_PURPOSE_LIVE_REMIND,
    )
    return f"https://t.me/{bot_username}?start=share_{token}"


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


def _is_topic_closed_error(exc: BaseException) -> bool:
    # Thread-only; do not treat as chat-unreachable (other topics may still work).
    return "topic_closed" in _exc_text(exc)


def test_fail_user_text(exc: BaseException, lang: str) -> str:
    if _is_topic_closed_error(exc):
        return t("test_failed_topic_closed", lang)
    return t("test_failed", lang)


def delivery_fail_reason_text(exc: BaseException, lang: str) -> str:
    if _is_topic_closed_error(exc):
        return t("delivery_fail_reason_topic_closed", lang)
    return str(exc)


def _block_context(sub: Subscription, *, alert_type: str = "live") -> dict[str, Any]:
    return {
        "dest_type": sub.dest_type,
        "alert_type": alert_type,
        "subscription_id": sub.id,
        "twitch_username": sub.twitch_username,
    }


def _mark_destination_unreachable(
    db: Database,
    sub: Subscription,
    exc: BaseException,
    *,
    alert_type: str = "live",
) -> None:
    props = _block_context(sub, alert_type=alert_type)
    if sub.dest_type == "dm":
        if _is_user_blocked_error(exc) or _is_chat_unreachable_error(exc):
            apply_user_blocked(db, sub.chat_id, source="delivery", properties=props)
        return
    if _is_chat_unreachable_error(exc):
        apply_chat_unreachable(db, sub.chat_id)
        return
    if _is_user_blocked_error(exc):
        apply_user_blocked(db, sub.owner_id, source="delivery", properties=props)


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


def apply_user_blocked(
    db: Database,
    user_id: int,
    *,
    source: str = "unknown",
    properties: dict[str, Any] | None = None,
) -> int:
    already = db.is_bot_blocked(user_id)
    db.set_bot_blocked(user_id, True)
    if not already:
        props: dict[str, Any] = {"source": source}
        if properties:
            props.update(properties)
        analytics.capture(user_id, "bot_blocked", props)
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
    notice_kwargs = dict(
        sub_id=_owner_sub_number(db, sub.owner_id, sub.id),
        twitch_username=sub.twitch_username,
        chat_name=chat_label,
    )
    if _is_topic_closed_error(exc):
        notice = t("delivery_fail_notice_topic_closed", lang, **notice_kwargs)
    else:
        notice = t(
            "delivery_fail_notice",
            lang,
            reason=delivery_fail_reason_text(exc, lang),
            **notice_kwargs,
        )
    try:
        await bot.send_message(
            sub.owner_id,
            notice,
            reply_markup=delivery_fail_notice_keyboard(sub.id, lang),
        )
        _delivery_fail_notified[sub.id] = datetime.now(timezone.utc)
    except (BadRequest, Forbidden) as notify_exc:
        if _is_user_blocked_error(notify_exc):
            apply_user_blocked(
                db,
                sub.owner_id,
                source="delivery_fail_notice",
                properties=_block_context(sub),
            )
        logger.warning(
            "Cannot notify owner %s about delivery failure: %s",
            sub.owner_id,
            notify_exc,
        )


async def _maybe_notify_delete_fail(
    bot,
    db: Database,
    *,
    owner_id: int,
    chat_id: int,
    message_id: int,
    thread_id: int | None,
    notify: bool,
) -> None:
    if not notify:
        return
    lang = db.get_user_locale(owner_id) or DEFAULT_LOCALE
    link = _message_link(chat_id, message_id, thread_id)
    try:
        await bot.send_message(
            owner_id,
            t("delete_fail_notice", lang, link=link),
        )
    except (BadRequest, Forbidden) as notify_exc:
        logger.warning(
            "Cannot notify owner %s about delete failure: %s",
            owner_id,
            notify_exc,
        )


async def _delete_one_previous_message(
    bot,
    db: Database,
    *,
    chat_id: int,
    message_id: int,
    thread_id: int | None,
    owner_id: int,
    notify_delete_fail: bool,
) -> bool:
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except (BadRequest, Forbidden) as exc:
        logger.warning(
            "Cannot delete message %s in %s: %s",
            message_id,
            chat_id,
            exc,
        )
        await _maybe_notify_delete_fail(
            bot,
            db,
            owner_id=owner_id,
            chat_id=chat_id,
            message_id=message_id,
            thread_id=thread_id,
            notify=notify_delete_fail,
        )
        return False


async def _delete_previous_before_send(bot, db: Database, sub: Subscription) -> None:
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
        ok = await _delete_one_previous_message(
            bot,
            db,
            chat_id=sub.chat_id,
            message_id=message_id,
            thread_id=sub.thread_id,
            owner_id=sub.owner_id,
            notify_delete_fail=sub.notify_delete_fail,
        )
        if not ok:
            continue
        if owner_sub_id != sub.id:
            db.set_last_message_id(owner_sub_id, None)
            sibling = db.get_subscription_by_id(owner_sub_id)
            if (
                sibling
                and sibling.pinned_message_id
                and sibling.pinned_message_id == message_id
            ):
                db.set_pinned_message_id(owner_sub_id, None)
        elif (
            sub.pinned_message_id
            and sub.pinned_message_id == message_id
        ):
            db.set_pinned_message_id(sub.id, None)


async def _unpin_one_message(bot, *, chat_id: int, message_id: int) -> bool:
    try:
        await bot.unpin_chat_message(chat_id=chat_id, message_id=message_id)
        return True
    except (BadRequest, Forbidden) as exc:
        err = str(exc).lower()
        # Already gone / not pinned — treat as cleaned up.
        if any(
            token in err
            for token in (
                "message to unpin not found",
                "message can't be unpinned",
                "message not found",
                "chat not found",
            )
        ):
            return True
        logger.warning(
            "Cannot unpin message %s in %s: %s",
            message_id,
            chat_id,
            exc,
        )
        return False


async def _pin_after_send(
    bot, db: Database, sub: Subscription, message_id: int
) -> None:
    old_pin = getattr(sub, "pinned_message_id", None)
    if old_pin and old_pin != message_id:
        await _unpin_one_message(bot, chat_id=sub.chat_id, message_id=old_pin)
    try:
        await bot.pin_chat_message(
            chat_id=sub.chat_id,
            message_id=message_id,
            disable_notification=True,
        )
        db.set_pinned_message_id(sub.id, message_id)
    except (BadRequest, Forbidden) as exc:
        logger.warning(
            "Cannot pin message %s in %s: %s",
            message_id,
            sub.chat_id,
            exc,
        )


async def unpin_stream_alert_messages(
    bot, db: Database, twitch_user_id: str
) -> None:
    """Unpin bot alerts for a streamer when the stream ends (any row with a pin id)."""
    for sub in db.get_subs_with_pinned_message(twitch_user_id):
        if sub.dest_type == "dm":
            continue
        pinned_id = getattr(sub, "pinned_message_id", None)
        if not pinned_id:
            continue
        if await _unpin_one_message(bot, chat_id=sub.chat_id, message_id=pinned_id):
            db.set_pinned_message_id(sub.id, None)


async def unpin_orphaned_alert_pins(
    bot, db: Database, *, live_user_ids: set[str]
) -> None:
    """Unpin leftover alert pins for streamers who are not live (cold start / missed offline)."""
    for sub in db.get_subs_with_pinned_message():
        if sub.dest_type == "dm":
            continue
        uid = str(sub.twitch_user_id or "").strip()
        if not uid or uid in live_user_ids:
            continue
        pinned_id = getattr(sub, "pinned_message_id", None)
        if not pinned_id:
            continue
        if await _unpin_one_message(bot, chat_id=sub.chat_id, message_id=pinned_id):
            db.set_pinned_message_id(sub.id, None)


async def unpin_subscription_alert(bot, db: Database, sub: Subscription) -> None:
    """Unpin one subscription's stored alert pin, if any."""
    if sub.dest_type == "dm":
        return
    pinned_id = getattr(sub, "pinned_message_id", None)
    if not pinned_id:
        return
    if await _unpin_one_message(bot, chat_id=sub.chat_id, message_id=pinned_id):
        db.set_pinned_message_id(sub.id, None)


async def purge_stale_previous_messages(context) -> None:
    """Delete tracked alert messages before Telegram's ~48h delete window closes."""
    db: Database = context.application.bot_data["db"]
    bot = context.bot
    cutoff = datetime.now(timezone.utc) - _DELETE_PREVIOUS_PURGE_AFTER
    due = db.get_subs_due_previous_message_purge(cutoff)
    if not due:
        return
    purged = 0
    for sub in due:
        if not sub.last_message_id:
            continue
        await _delete_one_previous_message(
            bot,
            db,
            chat_id=sub.chat_id,
            message_id=sub.last_message_id,
            thread_id=sub.thread_id,
            owner_id=sub.owner_id,
            notify_delete_fail=sub.notify_delete_fail,
        )
        # Clear either way — undeletable/gone messages must not be retried forever.
        db.set_last_message_id(sub.id, None)
        purged += 1
    if purged:
        logger.info("Purged %s stale previous alert message(s)", purged)


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
    parse_mode: str | None = None,
    bot_data: dict | None = None,
) -> bool:
    if _user_notifications_paused(db, sub.owner_id):
        return True
    if sub.dest_type == "dm" and db.is_bot_blocked(sub.chat_id):
        return True
    if db.is_chat_unreachable(sub.chat_id):
        return True
    await _maybe_notify_stored_template_typos(bot, db, sub)
    if sub.delete_previous and sub.dest_type != "dm":
        await _delete_previous_before_send(bot, db, sub)

    image_photo = None
    image_position = _effective_image_position(sub)
    preview_off = False
    chat_markup = None
    animation_bytes: bytes | None = None
    video_bytes: bytes | None = None
    captured_preview = None
    need_preview_upgrade = False
    prefer_media_id = False
    from twitch import template_uses_html

    if parse_mode is None:
        alert_parse_mode = (
            ParseMode.HTML if template_uses_html(sub.message_template or "") else None
        )
    else:
        alert_parse_mode = parse_mode
    try:
        lang = db.get_user_locale(sub.owner_id) or DEFAULT_LOCALE
        bot_username = ""
        if getattr(sub, "attach_live_remind_button", False):
            me = await bot.get_me()
            bot_username = (me.username or "").strip()
        chat_markup = _alert_chat_button_markup(
            sub, lang, db=db, bot_username=bot_username
        )
        preview_off = (
            bool(sub.disable_link_preview)
            or bool(sub.image_file_id)
            or bool(sub.attach_chat_button)
        )
        image_photo = await asyncio.to_thread(
            resolve_sub_image_photo, sub, stream, twitch
        )
        if is_stream_capture_preview_image(sub.image_file_id):
            from handlers.stream_preview import (
                build_preview_placeholder_mp4,
                build_stream_video_mp4,
                preview_login_from_stream,
            )
            from stream_capture import peek_shared_preview, video_preview_ready

            login = preview_login_from_stream(stream, sub)
            uid = str(
                (stream or {}).get("user_id") or sub.twitch_user_id or ""
            ).strip()
            # Fast first-send: shared cache → thumbnail placeholder → blocking capture.
            if uid:
                captured_preview = peek_shared_preview(uid)
            if captured_preview is None and login and uid:
                placeholder = await asyncio.to_thread(
                    build_preview_placeholder_mp4, stream
                )
                if placeholder:
                    if is_stream_file_video_preview_image(sub.image_file_id):
                        video_bytes = placeholder
                    else:
                        animation_bytes = placeholder
                    need_preview_upgrade = video_preview_ready()
            if (
                captured_preview is None
                and not animation_bytes
                and not video_bytes
                and login
                and uid
            ):
                captured_preview = await asyncio.to_thread(
                    build_stream_video_mp4,
                    login=login,
                    twitch_user_id=uid,
                )
            if captured_preview:
                if is_stream_file_video_preview_image(sub.image_file_id):
                    video_bytes = captured_preview.data
                else:
                    animation_bytes = captured_preview.data
                need_preview_upgrade = False
        if (
            is_dynamic_alert_image(sub.image_file_id)
            and not image_photo
            and not animation_bytes
            and not video_bytes
        ):
            logger.warning(
                "Dynamic image unresolved for sub %s (alert_type=%s image=%s); sending text only",
                sub.id,
                alert_type,
                sub.image_file_id,
            )
        prefer_media_id = is_dynamic_alert_image(sub.image_file_id)
        msg = await _deliver_alert_content(
            bot,
            chat_id=sub.chat_id,
            text=text,
            thread_id=sub.thread_id,
            image_file_id=None if (animation_bytes or video_bytes) else image_photo,
            image_position=image_position,
            animation_bytes=animation_bytes,
            video_bytes=video_bytes,
            disable_link_preview=preview_off,
            reply_markup=chat_markup,
            parse_mode=alert_parse_mode,
            prefer_media_message_id=prefer_media_id,
        )
    except RetryAfter as exc:
        await asyncio.sleep(float(exc.retry_after) + 0.5)
        try:
            msg = await _deliver_alert_content(
                bot,
                chat_id=sub.chat_id,
                text=text,
                thread_id=sub.thread_id,
                image_file_id=None if (animation_bytes or video_bytes) else image_photo,
                image_position=image_position,
                animation_bytes=animation_bytes,
                video_bytes=video_bytes,
                disable_link_preview=preview_off,
                reply_markup=chat_markup,
                parse_mode=alert_parse_mode,
                prefer_media_message_id=prefer_media_id,
            )
        except (BadRequest, Forbidden, RetryAfter) as retry_exc:
            logger.warning("Cannot send to %s after RetryAfter: %s", sub.chat_id, retry_exc)
            _mark_destination_unreachable(db, sub, retry_exc, alert_type=alert_type)
            await _maybe_notify_delivery_failure(bot, db, sub, retry_exc)
            # Keep captured MP4 pending until stream ends.
            return False
    except (BadRequest, Forbidden) as exc:
        logger.warning("Cannot send to %s: %s", sub.chat_id, exc)
        _mark_destination_unreachable(db, sub, exc, alert_type=alert_type)
        await _maybe_notify_delivery_failure(bot, db, sub, exc)
        # Keep captured MP4 pending until stream ends.
        return False

    if captured_preview is not None:
        from stream_capture import forget_and_unlink

        forget_and_unlink(captured_preview.path)
        captured_preview = None

    if db.is_chat_unreachable(sub.chat_id):
        clear_chat_unreachable(db, sub.chat_id)
    if sub.dest_type == "dm" and db.is_bot_blocked(sub.chat_id):
        clear_user_blocked(db, sub.chat_id)
    # Track message id for delete_previous (channels/groups) and for live
    # stream photo/video preview refresh — including DM, so editMessageMedia works.
    track_preview = bool(msg) and is_dynamic_alert_image(sub.image_file_id)
    track_delete_prev = bool(msg) and sub.dest_type != "dm" and bool(sub.delete_previous)
    if track_preview or track_delete_prev:
        db.set_last_message_id(sub.id, msg.message_id)
        if track_preview:
            from handlers.stream_preview import (
                mark_preview_refresh,
                schedule_preview_upgrade,
            )

            mark_preview_refresh(bot_data, sub.id)
            if need_preview_upgrade and bot_data is not None:
                schedule_preview_upgrade(
                    bot, db, twitch, sub, stream, bot_data
                )
    if (
        msg
        and getattr(sub, "pin_message", False)
        and sub.dest_type != "dm"
        and alert_type != "end"
    ):
        await _pin_after_send(bot, db, sub, msg.message_id)
    if sub.suppress_repeat_minutes > 0:
        db.set_notify_cooldown(sub.id, sub.suppress_repeat_minutes)
    # History is for the user's DM inbox only — skip channel/group destinations.
    if sub.dest_type == "dm":
        try:
            # Prefer stream broadcaster id — category-watch/drops subs store cw:/drops: synthetics.
            stream_uid = str((stream or {}).get("user_id") or "").strip()
            history_uid = (
                stream_uid
                if stream_uid.isdigit()
                else (sub.twitch_user_id or "")
            )
            db.add_alert_history(
                sub.owner_id,
                subscription_id=sub.id,
                twitch_username=sub.twitch_username,
                alert_type=alert_type,
                message_text=text,
                twitch_user_id=history_uid,
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
        analytics.capture(
            sub.owner_id,
            "alert_sent",
            {
                "alert_type": alert_type,
                "dest_type": sub.dest_type,
                "subscription_id": sub.id,
                "twitch_username": sub.twitch_username,
            },
        )
    return True


async def _send_test(
    bot, chat_id: int, thread_id: int | None, text: str, *, db: Database | None = None
) -> BaseException | None:
    """Send a destination probe. Returns None on success, else the Telegram error."""
    kwargs: dict = {"chat_id": chat_id, "text": text}
    if thread_id:
        kwargs["message_thread_id"] = thread_id
    try:
        await bot.send_message(**kwargs)
        if db is not None:
            clear_chat_unreachable(db, chat_id)
        return None
    except (BadRequest, Forbidden) as exc:
        logger.warning("Cannot send to %s: %s", chat_id, exc)
        if db is not None and _is_chat_unreachable_error(exc):
            apply_chat_unreachable(db, chat_id)
        elif db is not None and _is_user_blocked_error(exc):
            apply_user_blocked(db, chat_id, source="test_send")
        return exc


async def purge_expired_blocked_users(context) -> None:
    db: Database = context.application.bot_data["db"]
    removed = db.purge_expired_blocked_users()
    if removed:
        logger.info("Purged %s user(s) blocked for 365+ days", removed)


async def purge_stale_log_tables(context) -> None:
    db: Database = context.application.bot_data["db"]
    removed = db.purge_stale_log_tables()
    if removed:
        logger.info(
            "Purged stale log rows: %s",
            ", ".join(f"{k}={v}" for k, v in sorted(removed.items())),
        )

