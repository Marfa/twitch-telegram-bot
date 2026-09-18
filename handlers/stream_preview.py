"""Live stream preview refresh (photo / GIF) while the channel is online."""
from __future__ import annotations

import asyncio
import logging
import time
from io import BytesIO
from typing import Any

from telegram import InputFile, InputMediaAnimation, InputMediaPhoto
from telegram.error import BadRequest, Forbidden, RetryAfter

from cloudconvert_gif import cloudconvert_configured, mp4_url_to_gif_bytes
from db import Database, Subscription
from twitch import (
    TwitchClient,
    format_stream_thumbnail_url,
    is_stream_preview_image,
    is_stream_video_preview_image,
)

logger = logging.getLogger(__name__)

_BOT_DATA_REFRESH_KEY = "stream_preview_refresh_at"


def video_preview_ready() -> bool:
    """True when Create Clip + CloudConvert are configured."""
    from config import TWITCH_CLIPS_REFRESH_TOKEN

    return bool(TWITCH_CLIPS_REFRESH_TOKEN) and cloudconvert_configured()


def build_stream_video_gif_bytes(
    twitch: TwitchClient,
    *,
    broadcaster_id: str,
) -> bytes | None:
    """Create a ~30s clip, convert to GIF via CloudConvert; nothing stored on disk."""
    if not video_preview_ready():
        return None
    mp4_url = twitch.create_live_clip_mp4_url(broadcaster_id, duration=30.0)
    if not mp4_url:
        return None
    return mp4_url_to_gif_bytes(mp4_url)


async def refresh_live_stream_previews(
    bot,
    db: Database,
    twitch: TwitchClient,
    live_streams: dict[str, dict],
    bot_data: dict[str, Any],
) -> None:
    """Every ~30 min: editMessageMedia for stream photo / video GIF previews."""
    from config import STREAM_PREVIEW_REFRESH_SECONDS
    import premium as prem

    if not live_streams:
        return
    refresh_at: dict[int, float] = bot_data.setdefault(_BOT_DATA_REFRESH_KEY, {})
    now = time.time()
    interval = max(60, int(STREAM_PREVIEW_REFRESH_SECONDS))

    for uid, stream in live_streams.items():
        for sub in db.get_enabled_by_twitch_user_id(uid):
            if sub.dest_type == "dm":
                continue
            if not sub.last_message_id:
                continue
            if is_stream_video_preview_image(sub.image_file_id):
                if not prem.has_feature_sync(db, sub.owner_id, "stream_video_preview"):
                    continue
                if not video_preview_ready():
                    continue
            elif not is_stream_preview_image(sub.image_file_id):
                continue
            last = float(refresh_at.get(sub.id) or 0)
            if not last:
                # Seed timer on first sight so we don't refresh right after the live send.
                refresh_at[sub.id] = now
                continue
            if (now - last) < interval:
                continue
            ok = await _refresh_one(bot, twitch, sub, stream)
            if ok:
                refresh_at[sub.id] = now


def clear_preview_refresh(bot_data: dict[str, Any], sub_ids: list[int]) -> None:
    refresh_at = bot_data.get(_BOT_DATA_REFRESH_KEY)
    if not isinstance(refresh_at, dict):
        return
    for sid in sub_ids:
        refresh_at.pop(int(sid), None)


async def _refresh_one(
    bot,
    twitch: TwitchClient,
    sub: Subscription,
    stream: dict[str, Any],
) -> bool:
    mid = sub.last_message_id
    if not mid:
        return False
    try:
        if is_stream_video_preview_image(sub.image_file_id):
            bid = str(stream.get("user_id") or sub.twitch_user_id or "").strip()
            gif = await asyncio.to_thread(
                build_stream_video_gif_bytes, twitch, broadcaster_id=bid
            )
            if not gif:
                # Fall back to fresh frame if clip/GIF path fails.
                photo = format_stream_thumbnail_url(
                    str(stream.get("thumbnail_url") or ""),
                    cache_bust=True,
                )
                if not photo:
                    return False
                media = InputMediaPhoto(media=photo)
            else:
                media = InputMediaAnimation(
                    media=InputFile(BytesIO(gif), filename="preview.gif")
                )
        else:
            photo = format_stream_thumbnail_url(
                str(stream.get("thumbnail_url") or ""),
                cache_bust=True,
            )
            if not photo:
                return False
            media = InputMediaPhoto(media=photo)
        await bot.edit_message_media(
            chat_id=sub.chat_id,
            message_id=mid,
            media=media,
        )
        return True
    except RetryAfter as exc:
        await asyncio.sleep(float(exc.retry_after) + 0.5)
        return False
    except (BadRequest, Forbidden) as exc:
        logger.info(
            "Stream preview refresh skipped sub=%s chat=%s: %s",
            sub.id,
            sub.chat_id,
            exc,
        )
        return False
    except Exception:
        logger.exception(
            "Stream preview refresh failed sub=%s chat=%s", sub.id, sub.chat_id
        )
        return False
