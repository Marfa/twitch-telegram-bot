"""Private-chat forum topics for alert types (Bot API 9.3+ / setMySettings 10.5)."""
from __future__ import annotations

import logging
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

# Telegram General topic in forums / private topic mode.
GENERAL_TOPIC_ID = 1

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

    Product opt-in is per-user (users.topics_enabled, default off).
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


async def resolve_alert_thread_id(
    bot,
    db,
    sub,
    alert_type: str,
    *,
    lang: str | None = None,
) -> int | None:
    """Thread for this alert: group sub.thread_id, or DM topic when opted in."""
    if getattr(sub, "dest_type", None) != "dm":
        return sub.thread_id or None
    if not db.is_topics_enabled(sub.owner_id):
        return None
    key = topic_key_for_subscription(sub, alert_type)
    if key is None:
        return None
    if lang is None:
        lang = db.get_user_locale(sub.owner_id) or "en"
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
