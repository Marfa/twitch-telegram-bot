"""Private-chat forum topics for alert types (Bot API 9.3+ / setMySettings 10.5).

Also supports Premium advanced_mode per-streamer topics (one topic per Twitch
channel). When a streamer topic is enabled and entitled, alerts for that
streamer go there; otherwise routing falls back to alert-type topics.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from telegram.error import BadRequest, TelegramError

logger = logging.getLogger(__name__)

# Product topic keys ↔ createForumTopic names (via i18n topic_name_*).
TOPIC_KEYS: tuple[str, ...] = (
    "live",
    "end",
    "category",
    "upcoming",
    "drops",
    "game",
    "release",
    "giveaway_watch",
)

# Max concurrent enabled per-streamer DM topics (product cap).
STREAMER_TOPIC_LIMIT = 5

# Default icon color when user emoji is not in getForumTopicIconStickers.
STREAMER_TOPIC_ICON_COLOR = 0x6FB9F0

# Telegram General topic in forums / private topic mode.
GENERAL_TOPIC_ID = 1

# Separate Unicode emoji blocks (not one giant range) — CodeQL py/overly-large-range.
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F5FF"  # Misc Symbols and Pictographs
    "\U0001F600-\U0001F64F"  # Emoticons
    "\U0001F680-\U0001F6FF"  # Transport and Map
    "\U0001F900-\U0001F9FF"  # Supplemental Symbols and Pictographs
    "\U0001FA00-\U0001FAFF"  # Symbols and Pictographs Extended-A
    "\U00002600-\U000026FF"  # Miscellaneous Symbols
    "\U00002700-\U000027BF"  # Dingbats
    "\U00002300-\U000023FF"  # Miscellaneous Technical
    "\U0000FE0F"  # variation selector-16
    "\U0000200D"  # ZWJ
    "]+",
    flags=re.UNICODE,
)

# Allowed icon_color values only (createForumTopic).
_TOPIC_ICON_COLOR: dict[str, int] = {
    "live": 0x6FB9F0,
    "end": 0xFF93B2,
    "category": 0x8EEE98,
    "upcoming": 0xFFD67E,
    "drops": 0xCB86DB,
    "game": 0xFB6F5F,
    "release": 0xFFD67E,
    "giveaway_watch": 0xFF93B2,
}

# First emoji found in getForumTopicIconStickers wins per topic key.
_TOPIC_ICON_EMOJI: dict[str, tuple[str, ...]] = {
    "live": ("📣", "🔥", "⚡️"),
    "end": ("🏁", "⏹", "💤"),
    "category": ("🎮", "💻", "📱"),
    "upcoming": ("📆", "📅", "⏰"),
    "drops": ("🎁", "💎", "🛍"),
    "game": ("🔎", "💡", "🎲"),
    "release": ("📰", "📚", "🆕"),
    "giveaway_watch": ("🎉", "🏆", "🎟"),
}

_bot_settings_ok: bool | None = None
_icon_stickers_by_emoji: dict[str, str] | None = None
# (user_id, topic_key) already edited with an emoji this process — avoid spam on alerts.
_icons_applied: set[tuple[int, str]] = set()


def topic_key_for_subscription(sub, alert_type: str) -> str | None:
    """Map delivery alert_type + subscription shape → topic key (or None = General)."""
    from handlers.subscriptions import _alert_type_from_sub

    kind = _alert_type_from_sub(sub)
    if kind in TOPIC_KEYS:
        return kind
    if alert_type in ("schedule", "schedule_cancel"):
        return "upcoming"
    if alert_type in TOPIC_KEYS:
        return alert_type
    return None


async def ensure_bot_topic_settings(bot) -> bool:
    """Bot-wide: topics on, users cannot create/edit/delete; sidebar on.

    Product flag is per-user (users.topics_enabled: on for new users,
    opt-in for legacy via settings / topics_intro).
    """
    global _bot_settings_ok
    if _bot_settings_ok is True:
        return True
    try:
        await bot.do_api_request(
            "setMySettings",
            api_kwargs={
                "has_topics_enabled": True,
                "allows_users_to_create_topics": False,
                "has_topic_sidebar": True,
            },
        )
        _bot_settings_ok = True
        logger.info(
            "setMySettings: has_topics_enabled=true, "
            "allows_users_to_create_topics=false, has_topic_sidebar=true"
        )
        return True
    except (BadRequest, TelegramError) as exc:
        logger.warning("setMySettings (topics) failed: %s", exc)
        _bot_settings_ok = False
        return False


async def _load_icon_stickers(bot) -> dict[str, str]:
    """emoji → custom_emoji_id from getForumTopicIconStickers (cached)."""
    global _icon_stickers_by_emoji
    if _icon_stickers_by_emoji is not None:
        return _icon_stickers_by_emoji
    by_emoji: dict[str, str] = {}
    try:
        stickers = await bot.get_forum_topic_icon_stickers()
    except (BadRequest, TelegramError) as exc:
        logger.warning("getForumTopicIconStickers failed: %s", exc)
        _icon_stickers_by_emoji = {}
        return _icon_stickers_by_emoji
    except Exception as exc:
        logger.warning("getForumTopicIconStickers unexpected: %s", exc)
        _icon_stickers_by_emoji = {}
        return _icon_stickers_by_emoji
    for sticker in stickers or ():
        emoji = getattr(sticker, "emoji", None) or ""
        custom_id = getattr(sticker, "custom_emoji_id", None)
        if not emoji or not custom_id:
            continue
        # Prefer first id for a given emoji glyph.
        by_emoji.setdefault(str(emoji), str(custom_id))
    _icon_stickers_by_emoji = by_emoji
    logger.info("forum topic icon stickers loaded count=%s", len(by_emoji))
    return by_emoji


async def icon_custom_emoji_id_for_topic(bot, topic_key: str) -> str | None:
    """Resolve a custom_emoji_id for topic_key from Telegram's allowed sticker set."""
    if topic_key not in TOPIC_KEYS:
        return None
    by_emoji = await _load_icon_stickers(bot)
    if not by_emoji:
        return None
    for emoji in _TOPIC_ICON_EMOJI.get(topic_key, ()):
        custom_id = by_emoji.get(emoji)
        if custom_id:
            return custom_id
    return None


async def _apply_topic_icon(
    bot, user_id: int, topic_key: str, thread_id: int, *, force: bool = False
) -> bool:
    """editForumTopic with emoji icon. Returns True when an edit was attempted ok."""
    key = (int(user_id), topic_key)
    if not force and key in _icons_applied:
        return False
    custom_id = await icon_custom_emoji_id_for_topic(bot, topic_key)
    if not custom_id:
        return False
    try:
        await bot.edit_forum_topic(
            chat_id=user_id,
            message_thread_id=int(thread_id),
            icon_custom_emoji_id=custom_id,
        )
    except (BadRequest, TelegramError) as exc:
        logger.warning(
            "editForumTopic icon failed user=%s key=%s thread=%s: %s",
            user_id,
            topic_key,
            thread_id,
            exc,
        )
        return False
    _icons_applied.add(key)
    return True


async def ensure_user_topic(
    bot,
    db,
    user_id: int,
    topic_key: str,
    *,
    lang: str,
) -> int | None:
    """Return message_thread_id for topic_key, creating the forum topic if needed."""
    if topic_key not in TOPIC_KEYS:
        return None
    existing = db.get_user_topic_thread(user_id, topic_key)
    if existing:
        await _apply_topic_icon(bot, user_id, topic_key, int(existing))
        return int(existing)
    await ensure_bot_topic_settings(bot)
    from i18n import t

    name = t(f"topic_name_{topic_key}", lang)
    icon_color = _TOPIC_ICON_COLOR.get(topic_key, 0x6FB9F0)
    custom_id = await icon_custom_emoji_id_for_topic(bot, topic_key)
    api_kwargs: dict[str, Any] = {
        "chat_id": user_id,
        "name": name[:128],
        "icon_color": icon_color,
    }
    if custom_id:
        api_kwargs["icon_custom_emoji_id"] = custom_id
    try:
        result = await bot.do_api_request(
            "createForumTopic",
            api_kwargs=api_kwargs,
        )
    except (BadRequest, TelegramError) as exc:
        logger.warning(
            "createForumTopic failed user=%s key=%s: %s", user_id, topic_key, exc
        )
        return None
    thread_id = _thread_id_from_forum_topic(result)
    if thread_id is None:
        logger.warning(
            "createForumTopic missing message_thread_id user=%s key=%s result=%r",
            user_id,
            topic_key,
            result,
        )
        return None
    db.set_user_topic_thread(user_id, topic_key, thread_id)
    if custom_id:
        _icons_applied.add((int(user_id), topic_key))
    return thread_id


async def ensure_all_user_topics(bot, db, user_id: int, *, lang: str) -> int:
    """Create all product topics for a user. Returns how many thread ids are stored.

    Existing threads get an emoji icon via ensure_user_topic (once per process).
    Opening Bot settings also runs sync_user_topic_icons for a full refresh.
    """
    ok = 0
    for key in TOPIC_KEYS:
        tid = await ensure_user_topic(bot, db, user_id, key, lang=lang)
        if tid:
            ok += 1
    return ok


async def sync_user_topic_icons(bot, db, user_id: int) -> int:
    """Apply emoji icons to stored topic threads. Returns successful edits."""
    updated = 0
    for key in TOPIC_KEYS:
        tid = db.get_user_topic_thread(user_id, key)
        if not tid:
            continue
        if await _apply_topic_icon(bot, user_id, key, int(tid), force=True):
            updated += 1
    return updated


def extract_topic_emoji(text: str) -> str | None:
    """First emoji grapheme cluster from user text, or None."""
    raw = (text or "").strip()
    if not raw:
        return None
    match = _EMOJI_RE.search(raw)
    if not match:
        return None
    return match.group(0)


async def icon_custom_emoji_id_for_emoji(bot, emoji: str) -> str | None:
    """Map a user emoji to an allowed forum topic custom_emoji_id."""
    glyph = (emoji or "").strip()
    if not glyph:
        return None
    by_emoji = await _load_icon_stickers(bot)
    if not by_emoji:
        return None
    if glyph in by_emoji:
        return by_emoji[glyph]
    # Try without variation selector / with common variants.
    base = glyph.replace("\ufe0f", "")
    if base in by_emoji:
        return by_emoji[base]
    for key, custom_id in by_emoji.items():
        if key.replace("\ufe0f", "") == base:
            return custom_id
    return None


def streamer_topic_slots_used(db, user_id: int) -> int:
    return int(db.count_enabled_streamer_topics(user_id) or 0)


def streamer_topic_can_enable(
    db, user_id: int, twitch_user_id: str
) -> bool:
    """True if enabling this streamer would stay within STREAMER_TOPIC_LIMIT."""
    tid = str(twitch_user_id or "").strip()
    existing = db.get_streamer_topic(user_id, tid) if tid else None
    if existing and existing.get("enabled") and existing.get("message_thread_id"):
        return True
    return streamer_topic_slots_used(db, user_id) < STREAMER_TOPIC_LIMIT


async def ensure_streamer_topic(
    bot,
    db,
    user_id: int,
    twitch_user_id: str,
    *,
    twitch_login: str = "",
    icon_emoji: str | None = None,
    lang: str = "en",
) -> int | None:
    """Return message_thread_id for this streamer's DM topic, creating if needed."""
    tid = str(twitch_user_id or "").strip()
    if not tid:
        return None
    login = str(twitch_login or "").strip().lstrip("@").lower()
    existing = db.get_streamer_topic(user_id, tid)
    if existing and existing.get("message_thread_id"):
        thread_id = int(existing["message_thread_id"])
        # Optional icon refresh when a new emoji was chosen.
        if icon_emoji is not None and icon_emoji != str(
            existing.get("icon_emoji") or ""
        ):
            custom_id = await icon_custom_emoji_id_for_emoji(bot, icon_emoji) or ""
            try:
                kwargs: dict[str, Any] = {
                    "chat_id": user_id,
                    "message_thread_id": thread_id,
                }
                if custom_id:
                    kwargs["icon_custom_emoji_id"] = custom_id
                if login:
                    kwargs["name"] = login[:128]
                await bot.edit_forum_topic(**kwargs)
            except (BadRequest, TelegramError) as exc:
                logger.warning(
                    "editForumTopic streamer icon failed user=%s twitch=%s: %s",
                    user_id,
                    tid,
                    exc,
                )
            db.upsert_streamer_topic(
                user_id,
                tid,
                twitch_login=login or str(existing.get("twitch_login") or ""),
                message_thread_id=thread_id,
                enabled=True,
                icon_emoji=icon_emoji or "",
                icon_custom_emoji_id=custom_id,
            )
        elif not existing.get("enabled"):
            db.set_streamer_topic_enabled(user_id, tid, True)
        return thread_id

    if not streamer_topic_can_enable(db, user_id, tid):
        logger.info(
            "streamer topic limit reached user=%s limit=%s",
            user_id,
            STREAMER_TOPIC_LIMIT,
        )
        return None

    await ensure_bot_topic_settings(bot)
    name = login or tid
    emoji = icon_emoji
    if emoji is None and existing:
        emoji = str(existing.get("icon_emoji") or "") or None
    custom_id = await icon_custom_emoji_id_for_emoji(bot, emoji or "") if emoji else None
    api_kwargs: dict[str, Any] = {
        "chat_id": user_id,
        "name": name[:128],
        "icon_color": STREAMER_TOPIC_ICON_COLOR,
    }
    if custom_id:
        api_kwargs["icon_custom_emoji_id"] = custom_id
    try:
        result = await bot.do_api_request(
            "createForumTopic",
            api_kwargs=api_kwargs,
        )
    except (BadRequest, TelegramError) as exc:
        logger.warning(
            "createForumTopic streamer failed user=%s twitch=%s: %s",
            user_id,
            tid,
            exc,
        )
        return None
    thread_id = _thread_id_from_forum_topic(result)
    if thread_id is None:
        logger.warning(
            "createForumTopic streamer missing thread user=%s twitch=%s result=%r",
            user_id,
            tid,
            result,
        )
        return None
    db.upsert_streamer_topic(
        user_id,
        tid,
        twitch_login=login,
        message_thread_id=thread_id,
        enabled=True,
        icon_emoji=emoji or "",
        icon_custom_emoji_id=custom_id or "",
    )
    _ = lang  # reserved for future localized topic titles
    return thread_id


async def delete_streamer_forum_topic(
    bot, db, user_id: int, twitch_user_id: str
) -> bool:
    """deleteForumTopic + clear DB row. Returns True if Telegram delete succeeded
    or there was no thread to delete.
    """
    tid = str(twitch_user_id or "").strip()
    if not tid:
        return True
    existing = db.get_streamer_topic(user_id, tid)
    thread_id = None
    if existing and existing.get("message_thread_id") is not None:
        try:
            thread_id = int(existing["message_thread_id"])
        except (TypeError, ValueError):
            thread_id = None
    ok = True
    if thread_id is not None:
        try:
            await bot.delete_forum_topic(
                chat_id=user_id, message_thread_id=thread_id
            )
        except (BadRequest, TelegramError) as exc:
            logger.warning(
                "deleteForumTopic streamer failed user=%s twitch=%s thread=%s: %s",
                user_id,
                tid,
                thread_id,
                exc,
            )
            ok = False
    db.delete_streamer_topic(user_id, tid)
    return ok


async def disable_streamer_topic(
    bot,
    db,
    user_id: int,
    twitch_user_id: str,
    *,
    delete_topic: bool,
) -> None:
    """Stop routing to the streamer topic; optionally delete the forum topic."""
    tid = str(twitch_user_id or "").strip()
    if not tid:
        return
    if delete_topic:
        await delete_streamer_forum_topic(bot, db, user_id, tid)
        return
    db.set_streamer_topic_enabled(user_id, tid, False)


async def resolve_alert_thread_id(
    bot,
    db,
    sub,
    alert_type: str,
    *,
    lang: str | None = None,
) -> int | None:
    """Thread for this alert: group sub.thread_id, or DM topic when opted in.

    DM order: entitled enabled streamer topic → alert-type topic → General.
    """
    if getattr(sub, "dest_type", None) != "dm":
        return sub.thread_id or None
    if not db.is_topics_enabled(sub.owner_id):
        return None
    if lang is None:
        lang = db.get_user_locale(sub.owner_id) or "en"

    twitch_uid = str(getattr(sub, "twitch_user_id", "") or "").strip()
    if twitch_uid:
        row = db.get_streamer_topic(sub.owner_id, twitch_uid)
        if row and row.get("enabled") and row.get("message_thread_id"):
            from premium import has_feature_sync

            if has_feature_sync(
                db,
                sub.owner_id,
                "advanced_mode",
                channel=getattr(sub, "twitch_username", None),
            ):
                return int(row["message_thread_id"])
            # Unentitled: keep settings, fall through to type topics.

    key = topic_key_for_subscription(sub, alert_type)
    if key is None:
        return None
    return await ensure_user_topic(bot, db, sub.owner_id, key, lang=lang)


def _thread_id_from_forum_topic(result: Any) -> int | None:
    if result is None:
        return None
    if isinstance(result, dict):
        raw = result.get("message_thread_id")
    else:
        raw = getattr(result, "message_thread_id", None)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def reset_topic_icon_cache_for_tests() -> None:
    """Clear sticker + applied-icon caches (self_check)."""
    global _icon_stickers_by_emoji
    _icon_stickers_by_emoji = None
    _icons_applied.clear()


def user_alert_topic_thread_ids(db, user_id: int) -> list[int]:
    """Stored message_thread_id values for this user's alert topics."""
    out: list[int] = []
    for key in TOPIC_KEYS:
        tid = db.get_user_topic_thread(user_id, key)
        if tid:
            out.append(int(tid))
    return out


async def strip_reply_keyboards_from_threads(
    bot, chat_id: int, thread_ids: list[int]
) -> None:
    """Remove sticky ReplyKeyboard inside alert topics (Эфир, …).

    Menu keyboards belong only in General. A keyboard left in Эфир intercepts
    taps there and looks like «buttons broken everywhere».
    """
    if not thread_ids:
        return
    import asyncio

    from telegram import ReplyKeyboardRemove
    from telegram.error import BadRequest, Forbidden, TelegramError

    from bot_helpers import delete_chat_message
    from message_fx import message_fx_disabled

    remove = ReplyKeyboardRemove()
    with message_fx_disabled():
        for tid in thread_ids:
            try:
                msg = await bot.send_message(
                    int(chat_id),
                    "·",
                    message_thread_id=int(tid),
                    reply_markup=remove,
                )
                await asyncio.sleep(0.2)
                try:
                    await delete_chat_message(
                        bot, int(chat_id), msg.message_id, kind="bot_own"
                    )
                except (BadRequest, Forbidden):
                    pass
            except (BadRequest, Forbidden, TelegramError) as exc:
                logger.info(
                    "strip reply keyboard thread=%s chat=%s: %s",
                    tid,
                    chat_id,
                    exc,
                )
            except Exception:
                logger.warning(
                    "strip reply keyboard failed thread=%s chat=%s",
                    tid,
                    chat_id,
                    exc_info=True,
                )


async def strip_alert_topic_reply_keyboards(
    bot, db, user_id: int, *, force: bool = False
) -> None:
    """Clear ReplyKeyboard from stored alert topics.

    Only when force=True (topics enable). Menu / pulse / redeploy must not
    post into Эфир — that bumps the sidebar and looks like topics recreating.
    """
    if not force:
        return
    tids = user_alert_topic_thread_ids(db, user_id)
    await strip_reply_keyboards_from_threads(bot, user_id, tids)


# --- Private UI → General (alerts alone use message_thread_id) -----------------

_private_ui_patched = False

# Message.reply_* → Bot.send_* (content replies that would inherit a topic).
_REPLY_SEND_MAP: tuple[tuple[str, str], ...] = (
    ("reply_text", "send_message"),
    ("reply_photo", "send_photo"),
    ("reply_document", "send_document"),
    ("reply_animation", "send_animation"),
    ("reply_video", "send_video"),
    ("reply_video_note", "send_video_note"),
    ("reply_audio", "send_audio"),
    ("reply_voice", "send_voice"),
    ("reply_sticker", "send_sticker"),
    ("reply_media_group", "send_media_group"),
    ("reply_location", "send_location"),
    ("reply_venue", "send_venue"),
    ("reply_contact", "send_contact"),
    ("reply_poll", "send_poll"),
    ("reply_dice", "send_dice"),
    ("reply_game", "send_game"),
    ("reply_invoice", "send_invoice"),
    ("reply_paid_media", "send_paid_media"),
    ("reply_chat_action", "send_chat_action"),
)

_TOPIC_REPLY_STRIP = frozenset(
    {
        "message_thread_id",
        "reply_to_message_id",
        "reply_parameters",
        "do_quote",
        "quote",
        "allow_sending_without_reply",
    }
)


def private_ui_should_go_to_general(message) -> bool:
    """True when a private Message.reply_* would otherwise land in a topic."""
    from telegram.constants import ChatType

    chat = getattr(message, "chat", None)
    if chat is None or getattr(chat, "type", None) != ChatType.PRIVATE:
        return False
    if getattr(message, "is_topic_message", False):
        return True
    tid = getattr(message, "message_thread_id", None)
    if tid is None:
        return False
    try:
        return int(tid) != GENERAL_TOPIC_ID
    except (TypeError, ValueError):
        return False


def _kwargs_for_general_send(kwargs: dict) -> dict:
    return {k: v for k, v in kwargs.items() if k not in _TOPIC_REPLY_STRIP}


def install_private_ui_to_general() -> None:
    """Patch Message.reply_* so private topic replies go to General.

    Product rule: only alerts use topics (``send_message(..., message_thread_id=)``).
    Menus, wizards, settings, help, callbacks that ``reply_*`` to a message in
    Эфир / other topics / «All messages» must post in General instead.
    Idempotent — safe to call from ``build_application``.
    """
    global _private_ui_patched
    if _private_ui_patched:
        return

    from telegram import Message
    from telegram.constants import ParseMode

    def _wrap(reply_name: str, send_name: str):
        original = getattr(Message, reply_name)

        async def wrapped(self, *args, **kwargs):
            if not private_ui_should_go_to_general(self):
                return await original(self, *args, **kwargs)
            bot = self.get_bot()
            send = getattr(bot, send_name)
            return await send(self.chat_id, *args, **_kwargs_for_general_send(kwargs))

        wrapped.__name__ = reply_name
        wrapped.__qualname__ = f"Message.{reply_name}"
        setattr(Message, reply_name, wrapped)

    for reply_name, send_name in _REPLY_SEND_MAP:
        if not hasattr(Message, reply_name):
            continue
        _wrap(reply_name, send_name)

    # HTML / Markdown shortcuts → send_message with parse_mode (same General rule).
    for reply_name, parse_mode in (
        ("reply_html", ParseMode.HTML),
        ("reply_markdown", ParseMode.MARKDOWN),
        ("reply_markdown_v2", ParseMode.MARKDOWN_V2),
    ):
        if not hasattr(Message, reply_name):
            continue
        original = getattr(Message, reply_name)

        async def wrapped(self, text, *args, _orig=original, _mode=parse_mode, **kwargs):
            if not private_ui_should_go_to_general(self):
                return await _orig(self, text, *args, **kwargs)
            clean = _kwargs_for_general_send(kwargs)
            clean.setdefault("parse_mode", _mode)
            return await self.get_bot().send_message(self.chat_id, text, *args, **clean)

        wrapped.__name__ = reply_name
        wrapped.__qualname__ = f"Message.{reply_name}"
        setattr(Message, reply_name, wrapped)

    _private_ui_patched = True
    logger.info(
        "private UI replies patched to General "
        "(topics reserved for alerts)"
    )
