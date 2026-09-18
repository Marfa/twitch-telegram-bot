"""Live stream preview refresh (photo / MP4) while the channel is online."""
from __future__ import annotations

import asyncio
import logging
import time
from io import BytesIO
from typing import Any

from telegram import InputFile, InputMediaAnimation, InputMediaPhoto
from telegram.error import BadRequest, Forbidden, RetryAfter

from db import Database, Subscription
from stream_capture import (
    CapturedPreview,
    capture_live_preview_mp4,
    forget_and_unlink,
    video_preview_ready,
)
from twitch import (
    TwitchClient,
    format_stream_thumbnail_url,
    is_stream_preview_image,
    is_stream_video_preview_image,
)

logger = logging.getLogger(__name__)

_BOT_DATA_REFRESH_KEY = "stream_preview_refresh_at"


def build_stream_video_mp4(
    *,
    login: str,
    twitch_user_id: str,
    duration: float = 30.0,
) -> CapturedPreview | None:
    """Record ~30s live MP4; file stays pending until forget_and_unlink / purge."""
    if not video_preview_ready():
        return None
    return capture_live_preview_mp4(
        login, twitch_user_id=twitch_user_id, duration=duration
    )


def preview_login_from_stream(
    stream: dict[str, Any] | None,
    sub: Subscription | None = None,
) -> str:
    login = str((stream or {}).get("user_login") or "").strip()
    if not login and sub is not None:
        login = str(getattr(sub, "twitch_username", None) or "").strip()
    return login.lstrip("@").lower()


async def refresh_live_stream_previews(
    bot,
    db: Database,
    twitch: TwitchClient,
    live_streams: dict[str, dict],
    bot_data: dict[str, Any],
) -> None:
    """Every ~30 min: editMessageMedia for stream photo / video MP4 previews."""
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
    captured: CapturedPreview | None = None
    try:
        if is_stream_video_preview_image(sub.image_file_id):
            login = preview_login_from_stream(stream, sub)
            uid = str(stream.get("user_id") or sub.twitch_user_id or "").strip()
            if login and uid:
                captured = await asyncio.to_thread(
                    build_stream_video_mp4,
                    login=login,
                    twitch_user_id=uid,
                )
            if not captured:
                # Fall back to fresh frame if capture fails.
                photo = format_stream_thumbnail_url(
                    str(stream.get("thumbnail_url") or ""),
                    cache_bust=True,
                )
                if not photo:
                    return False
                media = InputMediaPhoto(media=photo)
            else:
                media = InputMediaAnimation(
                    media=InputFile(
                        BytesIO(captured.data), filename="preview.mp4"
                    )
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
        if captured:
            forget_and_unlink(captured.path)
            captured = None
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
    # On failure leave captured file pending for purge_for_streamer on offline.
