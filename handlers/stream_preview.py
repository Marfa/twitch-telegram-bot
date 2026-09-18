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
    invalidate_shared,
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
# Subs whose media cannot be edited (e.g. old Video-typed alerts) — skip until stream ends.
_BOT_DATA_SKIP_KEY = "stream_preview_refresh_skip"
# Telegram autoplay animations: muted H.264 ~480p.
_ANIM_WIDTH = 480
_ANIM_HEIGHT = 270
_ANIM_DURATION = 30


def build_stream_video_mp4(
    *,
    login: str,
    twitch_user_id: str,
    duration: float = 30.0,
    force: bool = False,
) -> CapturedPreview | None:
    """Record ~30s live MP4; shared per streamer until TTL / force / purge."""
    if not video_preview_ready():
        return None
    return capture_live_preview_mp4(
        login,
        twitch_user_id=twitch_user_id,
        duration=duration,
        force=force,
    )


def preview_login_from_stream(
    stream: dict[str, Any] | None,
    sub: Subscription | None = None,
) -> str:
    login = str((stream or {}).get("user_login") or "").strip()
    if not login and sub is not None:
        login = str(getattr(sub, "twitch_username", None) or "").strip()
    return login.lstrip("@").lower()


def mark_preview_refresh(bot_data: dict[str, Any] | None, sub_id: int) -> None:
    """Start the refresh interval after a successful alert send (avoid instant re-capture)."""
    if bot_data is None:
        return
    refresh_at: dict[int, float] = bot_data.setdefault(_BOT_DATA_REFRESH_KEY, {})
    refresh_at[int(sub_id)] = time.time()


def clear_preview_refresh(bot_data: dict[str, Any], sub_ids: list[int]) -> None:
    refresh_at = bot_data.get(_BOT_DATA_REFRESH_KEY)
    if isinstance(refresh_at, dict):
        for sid in sub_ids:
            refresh_at.pop(int(sid), None)
    skip = bot_data.get(_BOT_DATA_SKIP_KEY)
    if isinstance(skip, set):
        for sid in sub_ids:
            skip.discard(int(sid))


def _preview_skip_set(bot_data: dict[str, Any]) -> set[int]:
    skip = bot_data.setdefault(_BOT_DATA_SKIP_KEY, set())
    if not isinstance(skip, set):
        skip = set()
        bot_data[_BOT_DATA_SKIP_KEY] = skip
    return skip


async def refresh_live_stream_previews(
    bot,
    db: Database,
    twitch: TwitchClient,
    live_streams: dict[str, dict],
    bot_data: dict[str, Any],
) -> None:
    """Every ~30 min: editMessageMedia for stream photo / video MP4 previews.

    One MP4 capture per streamer per refresh cycle (shared across all their alerts).
    Missing refresh timestamps (e.g. after deploy) are treated as due so already-sent
    messages keep updating.

    If edit fails for a video preview (e.g. message stored as Video), stop further
    refresh attempts for that subscription until the stream ends — do not delete/resend.
    """
    from config import STREAM_PREVIEW_REFRESH_SECONDS
    import premium as prem

    if not live_streams:
        return
    refresh_at: dict[int, float] = bot_data.setdefault(_BOT_DATA_REFRESH_KEY, {})
    skip = _preview_skip_set(bot_data)
    now = time.time()
    interval = max(60, int(STREAM_PREVIEW_REFRESH_SECONDS))

    # uid -> (stream, video_subs, photo_subs)
    due: dict[str, tuple[dict[str, Any], list[Subscription], list[Subscription]]] = {}

    for uid, stream in live_streams.items():
        video_subs: list[Subscription] = []
        photo_subs: list[Subscription] = []
        for sub in db.get_enabled_by_twitch_user_id(uid):
            # Refresh channel/group and DM alerts that use live stream media.
            if not sub.last_message_id:
                continue
            if int(sub.id) in skip:
                continue
            if is_stream_video_preview_image(sub.image_file_id):
                if not prem.has_feature_sync(
                    db,
                    sub.owner_id,
                    "stream_video_preview",
                    channel=sub.twitch_username,
                ):
                    continue
                if not video_preview_ready():
                    continue
                kind = "video"
            elif is_stream_preview_image(sub.image_file_id):
                kind = "photo"
            else:
                continue
            last = refresh_at.get(sub.id)
            if last is not None and (now - float(last)) < interval:
                continue
            # last is None → due (deploy / restart: keep updating existing alerts)
            if kind == "video":
                video_subs.append(sub)
            else:
                photo_subs.append(sub)
        if video_subs or photo_subs:
            due[uid] = (stream, video_subs, photo_subs)

    for uid, (stream, video_subs, photo_subs) in due.items():
        captured: CapturedPreview | None = None
        if video_subs:
            login = preview_login_from_stream(stream, video_subs[0])
            if login:
                invalidate_shared(uid)
                captured = await asyncio.to_thread(
                    build_stream_video_mp4,
                    login=login,
                    twitch_user_id=uid,
                    force=True,
                )
        for sub in video_subs:
            ok = await _edit_preview_media(
                bot, sub, stream, captured=captured, bot_data=bot_data
            )
            if ok:
                refresh_at[sub.id] = now
        for sub in photo_subs:
            ok = await _edit_preview_media(
                bot, sub, stream, captured=None, bot_data=bot_data
            )
            if ok:
                refresh_at[sub.id] = now
        if captured is not None:
            forget_and_unlink(captured.path)
            invalidate_shared(uid)


def animation_input_file(data: bytes, *, attach: bool = False) -> InputFile:
    # Named MP4 helps Telegram accept the upload for send/edit animation.
    # attach=True is required for InputMedia* (editMessageMedia multipart).
    return InputFile(BytesIO(data), filename="preview.mp4", attach=attach)


async def edit_animation_message(
    bot,
    *,
    chat_id: int,
    message_id: int,
    data: bytes,
) -> None:
    """Replace animation media; local files must use attach:// via InputFile(attach=True)."""
    media = InputMediaAnimation(
        media=animation_input_file(data, attach=True),
        width=_ANIM_WIDTH,
        height=_ANIM_HEIGHT,
        duration=_ANIM_DURATION,
    )
    await bot.edit_message_media(
        chat_id=chat_id,
        message_id=message_id,
        media=media,
    )


async def _edit_preview_media(
    bot,
    sub: Subscription,
    stream: dict[str, Any],
    *,
    captured: CapturedPreview | None,
    bot_data: dict[str, Any] | None = None,
) -> bool:
    mid = sub.last_message_id
    if not mid:
        return False
    try:
        if is_stream_video_preview_image(sub.image_file_id):
            # Never fall back to a static photo — that freezes the GIF bubble
            # and can make Telegram refuse later Animation edits.
            if captured is None:
                return False
            await edit_animation_message(
                bot,
                chat_id=sub.chat_id,
                message_id=int(mid),
                data=captured.data,
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
        logger.info(
            "Stream preview refreshed sub=%s chat=%s mid=%s kind=%s",
            sub.id,
            sub.chat_id,
            mid,
            "video" if is_stream_video_preview_image(sub.image_file_id) else "photo",
        )
        return True
    except RetryAfter as exc:
        await asyncio.sleep(float(exc.retry_after) + 0.5)
        return False
    except (BadRequest, Forbidden) as exc:
        # Old alerts may be stored as Video — cannot edit into Animation. Stop retrying.
        if is_stream_video_preview_image(sub.image_file_id) and bot_data is not None:
            _preview_skip_set(bot_data).add(int(sub.id))
            logger.info(
                "Stream preview refresh stopped for sub=%s chat=%s (uneditable media): %s",
                sub.id,
                sub.chat_id,
                exc,
            )
        else:
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
