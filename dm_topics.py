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
    "end": 0xFF93C2,
    "category": 0xFF865D,
    "upcoming": 0xFFD67E,
    "drops": 0xCB86DB,
    "game": 0xFB6F5F,
    "release": 0xFFD67E,
    "giveaway_watch": 0xFF865D,
}

_bot_settings_ok: bool | None = None


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
        return int(existing)
    await ensure_bot_topic_settings(bot)
    from i18n import t

    name = t(f"topic_name_{topic_key}", lang)
    icon_color = _TOPIC_ICON_COLOR.get(topic_key, 0x6FB9F0)
    try:
        result = await bot.do_api_request(
            "createForumTopic",
            api_kwargs={
                "chat_id": user_id,
                "name": name[:128],
                "icon_color": icon_color,
            },
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
    return thread_id


async def ensure_all_user_topics(bot, db, user_id: int, *, lang: str) -> int:
    """Create all product topics for a user. Returns how many thread ids are stored."""
    ok = 0
    for key in TOPIC_KEYS:
        tid = await ensure_user_topic(bot, db, user_id, key, lang=lang)
        if tid:
            ok += 1
    return ok


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
