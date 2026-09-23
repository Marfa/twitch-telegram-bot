"""Live stream preview refresh (photo / MP4) while the channel is online."""
from __future__ import annotations

import asyncio
import logging
import time
from io import BytesIO
from typing import Any

from telegram import InputFile, InputMediaAnimation, InputMediaPhoto, InputMediaVideo
from telegram.error import BadRequest, Forbidden, RetryAfter

from db import Database, Subscription
from stream_capture import (
    CapturedPreview,
    build_thumbnail_placeholder_mp4,
    capture_live_preview_mp4,
    forget_and_unlink,
    invalidate_shared,
    placeholder_preview_ready,
    video_preview_ready,
)
from twitch import (
    TwitchClient,
    format_stream_thumbnail_url,
    is_stream_preview_image,
    is_stream_video_preview_image,
    is_stream_file_video_preview_image,
    is_stream_capture_preview_image,
)

logger = logging.getLogger(__name__)

_BOT_DATA_REFRESH_KEY = "stream_preview_refresh_at"
# Subs whose media cannot be edited (e.g. old Video-typed alerts) — skip until stream ends.
_BOT_DATA_SKIP_KEY = "stream_preview_refresh_skip"
# One ~30s+ ffmpeg capture per tick keeps this job under CHECK_INTERVAL.
_MAX_VIDEO_CAPTURES_PER_TICK = 1
# Telegram autoplay animations: muted H.264 ~640p (matches light re-encode).
_ANIM_WIDTH = 640
_ANIM_HEIGHT = 360
_ANIM_DURATION = 30


def build_stream_video_mp4(
    *,
    login: str,
    twitch_user_id: str,
    duration: float = 30.0,
    force: bool = False,
) -> CapturedPreview | None:
    """Record ~30s live MP4; shared per streamer until force refresh / stream end."""
    if not video_preview_ready():
        return None
    return capture_live_preview_mp4(
        login,
        twitch_user_id=twitch_user_id,
        duration=duration,
        force=force,
    )


def build_preview_placeholder_mp4(stream: dict[str, Any] | None) -> bytes | None:
    """Still Helix thumbnail → short muted MP4 (same Animation/Video type as live clip)."""
    if not placeholder_preview_ready():
        return None
    thumb = format_stream_thumbnail_url(
        str((stream or {}).get("thumbnail_url") or ""),
        cache_bust=True,
    )
    if not thumb:
        return None
    return build_thumbnail_placeholder_mp4(thumb)


def schedule_preview_upgrade(
    bot,
    db: Database,
    twitch: TwitchClient | None,
    sub: Subscription,
    stream: dict[str, Any] | None,
    bot_data: dict[str, Any] | None,
) -> None:
    """Fire-and-forget: replace placeholder MP4 with a live capture (same media type)."""
    stream_snap = dict(stream) if isinstance(stream, dict) else {}

    async def _run() -> None:
        try:
            await upgrade_placeholder_preview(
                bot, db, twitch, sub, stream_snap, bot_data
            )
        except Exception:
            logger.exception(
                "Stream preview upgrade failed sub=%s chat=%s",
                getattr(sub, "id", None),
                getattr(sub, "chat_id", None),
            )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(
            "No running loop for stream preview upgrade sub=%s",
            getattr(sub, "id", None),
        )
        return
    loop.create_task(_run(), name=f"stream_preview_upgrade_{int(sub.id)}")


async def upgrade_placeholder_preview(
    bot,
    db: Database,
    twitch: TwitchClient | None,
    sub: Subscription,
    stream: dict[str, Any],
    bot_data: dict[str, Any] | None,
) -> bool:
    """Capture live MP4 and editMessageMedia in-place (Animation↔Animation / Video↔Video)."""
    if not video_preview_ready():
        return False
    fresh = db.get_subscription(int(sub.id), int(sub.owner_id)) or sub
    if not fresh.last_message_id:
        return False
    if not is_stream_capture_preview_image(fresh.image_file_id):
        return False
    login = preview_login_from_stream(stream, fresh)
    uid = str(
        (stream or {}).get("user_id") or getattr(fresh, "twitch_user_id", None) or ""
    ).strip()
    if not login or not uid:
        return False
    captured = await asyncio.to_thread(
        build_stream_video_mp4,
        login=login,
        twitch_user_id=uid,
        force=False,
    )
    if captured is None:
        return False
    try:
        ok = await _edit_preview_media(
            bot,
            fresh,
            stream,
            captured=captured,
            bot_data=bot_data,
            twitch=twitch,
            db=db,
        )
        if ok and bot_data is not None:
            mark_preview_refresh(bot_data, int(fresh.id))
            logger.info(
                "Stream preview upgraded sub=%s chat=%s mid=%s",
                fresh.id,
                fresh.chat_id,
                fresh.last_message_id,
            )
        return ok
    finally:
        forget_and_unlink(captured.path)


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


def _clear_preview_skip(bot_data: dict[str, Any] | None, sub_id: int) -> None:
    """Allow refresh again after a successful edit (or a recovered mid)."""
    if bot_data is None:
        return
    skip = bot_data.get(_BOT_DATA_SKIP_KEY)
    if isinstance(skip, set):
        skip.discard(int(sub_id))


def _is_permanent_preview_edit_failure(exc: BaseException) -> bool:
    """True only for media-type / structure errors that will keep failing until stream end.

    Transient races (placeholder upgrade vs refresh, delete_previous, wrong mid)
    often look like \"message to edit not found\" — those must NOT permanent-skip,
    or sibling chats for the same streamer stop refreshing for the rest of the stream.
    """
    err = str(exc).lower()
    if not err:
        return False
    if (
        "message to edit not found" in err
        or "message_id_invalid" in err
        or "message is not modified" in err
        or "message can't be found" in err
    ):
        return False
    return (
        "there is no media" in err
        or "wrong type of the web page content" in err
        or "wrong type" in err
        or "media type" in err
        or "can't edit" in err
        or "cannot edit" in err
    )


def live_streams_from_poll_snapshot(bot_data: dict[str, Any]) -> dict[str, dict]:
    """Build Helix-shaped live map from the last check_streams poll (no extra Helix call)."""
    last_live = bot_data.get("last_live") or {}
    last_streams = bot_data.get("last_streams") or {}
    if not isinstance(last_live, dict) or not isinstance(last_streams, dict):
        return {}
    out: dict[str, dict] = {}
    for uid, is_live in last_live.items():
        if not is_live:
            continue
        snap = last_streams.get(uid)
        if isinstance(snap, dict) and snap:
            out[str(uid)] = snap
    return out


async def check_stream_previews(context) -> None:
    """JobQueue callback: refresh due stream previews outside the 60s check_streams tick.

    Video MP4 capture alone is ~30s+ per streamer; running it inside check_streams
    made that job overrun its interval and skip ticks (max_instances=1).
    """
    started = time.monotonic()
    bot_data = context.application.bot_data
    live_streams = live_streams_from_poll_snapshot(bot_data)
    if not live_streams:
        return
    db: Database = bot_data["db"]
    twitch: TwitchClient = bot_data["twitch"]
    try:
        await refresh_live_stream_previews(
            context.bot,
            db,
            twitch,
            live_streams,
            bot_data,
        )
    finally:
        elapsed = time.monotonic() - started
        # Captures are expected to be long; warn so PostHog still sees backlog here.
        if elapsed >= 60.0:
            logger.warning(
                "check_stream_previews took %.1fs (outside check_streams)",
                elapsed,
            )
        elif elapsed >= 15.0:
            logger.info(
                "check_stream_previews took %.1fs (outside check_streams)",
                elapsed,
            )


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

    If edit fails with a hard media-type error, stop further refresh attempts for
    that subscription until the stream ends — do not delete/resend. Transient
    errors (e.g. message not found during upgrade race) are retried next tick.
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
            if is_stream_capture_preview_image(sub.image_file_id):
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

    if due:
        logger.info(
            "stream preview refresh due streamers=%s video_subs=%s photo_subs=%s",
            len(due),
            sum(len(v) for _, v, _ in due.values()),
            sum(len(p) for _, _, p in due.values()),
        )

    video_captures = 0
    for uid, (stream, video_subs, photo_subs) in due.items():
        captured: CapturedPreview | None = None
        run_video = bool(video_subs) and video_captures < _MAX_VIDEO_CAPTURES_PER_TICK
        if video_subs and not run_video:
            # Defer extra MP4 captures; still refresh cheap photo previews.
            video_subs = []
        if run_video:
            login = preview_login_from_stream(stream, video_subs[0])
            if login:
                invalidate_shared(uid)
                captured = await asyncio.to_thread(
                    build_stream_video_mp4,
                    login=login,
                    twitch_user_id=uid,
                    force=True,
                )
            video_captures += 1
        for sub in video_subs:
            ok = await _edit_preview_media(
                bot,
                sub,
                stream,
                captured=captured,
                bot_data=bot_data,
                twitch=twitch,
                db=db,
            )
            if ok:
                refresh_at[sub.id] = now
        for sub in photo_subs:
            ok = await _edit_preview_media(
                bot,
                sub,
                stream,
                captured=None,
                bot_data=bot_data,
                twitch=twitch,
                db=db,
            )
            if ok:
                refresh_at[sub.id] = now
        # Keep the fresh clip in _SHARED for later first-sends (category/end/other
        # owners) until the next forced refresh or stream end.
        if captured is not None:
            forget_and_unlink(captured.path)


def animation_input_file(data: bytes, *, attach: bool = False) -> InputFile:
    # Named MP4; attach=True is required for InputMedia* (editMessageMedia multipart).
    return InputFile(BytesIO(data), filename="preview.mp4", attach=attach)


# Back-compat alias.
video_input_file = animation_input_file


async def edit_animation_message(
    bot,
    *,
    chat_id: int,
    message_id: int,
    data: bytes,
    caption: str | None = None,
    parse_mode: str | None = None,
    show_caption_above_media: bool | None = None,
    reply_markup=None,
) -> None:
    """Replace animation media; local files must use attach:// via InputFile(attach=True).

    Caption and reply_markup must be passed explicitly — omitting either clears
    text / inline buttons on editMessageMedia.
    """
    kwargs: dict[str, Any] = {
        "media": animation_input_file(data, attach=True),
        "width": _ANIM_WIDTH,
        "height": _ANIM_HEIGHT,
        "duration": _ANIM_DURATION,
    }
    if caption is not None:
        kwargs["caption"] = caption
        if parse_mode:
            kwargs["parse_mode"] = parse_mode
        if show_caption_above_media is not None:
            kwargs["show_caption_above_media"] = show_caption_above_media
    media = InputMediaAnimation(**kwargs)
    edit_kwargs: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "media": media,
    }
    if reply_markup is not None:
        edit_kwargs["reply_markup"] = reply_markup
    await bot.edit_message_media(**edit_kwargs)


async def edit_file_video_message(
    bot,
    *,
    chat_id: int,
    message_id: int,
    data: bytes,
    caption: str | None = None,
    parse_mode: str | None = None,
    show_caption_above_media: bool | None = None,
    reply_markup=None,
) -> None:
    """Replace Video media (muted MP4); keep type stable for editMessageMedia."""
    kwargs: dict[str, Any] = {
        "media": animation_input_file(data, attach=True),
        "width": _ANIM_WIDTH,
        "height": _ANIM_HEIGHT,
        "duration": _ANIM_DURATION,
        "supports_streaming": True,
    }
    if caption is not None:
        kwargs["caption"] = caption
        if parse_mode:
            kwargs["parse_mode"] = parse_mode
        if show_caption_above_media is not None:
            kwargs["show_caption_above_media"] = show_caption_above_media
    media = InputMediaVideo(**kwargs)
    edit_kwargs: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "media": media,
    }
    if reply_markup is not None:
        edit_kwargs["reply_markup"] = reply_markup
    await bot.edit_message_media(**edit_kwargs)


# Back-compat alias (old name meant Animation).
edit_video_message = edit_animation_message


def _preview_caption(
    sub: Subscription,
    stream: dict[str, Any],
    twitch: TwitchClient | None,
) -> tuple[str, str | None, bool]:
    """Rebuild alert caption for editMessageMedia (Telegram clears it if omitted)."""
    from handlers.wizard import _render_sub_template
    from telegram.constants import ParseMode
    from twitch import template_uses_html

    login = preview_login_from_stream(stream, sub) or (sub.twitch_username or "")
    text = _render_sub_template(
        sub,
        login,
        str(stream.get("game_name") or ""),
        str(stream.get("title") or ""),
        twitch=twitch,
        stream=stream,
    )
    if len(text) > 1024:
        text = text[:1020] + "…"
    parse_mode = (
        ParseMode.HTML if template_uses_html(sub.message_template or "") else None
    )
    position = (sub.image_position or "").strip()
    if position not in ("before", "after"):
        position = "before"
    return text, parse_mode, position == "after"


async def _preview_reply_markup(bot, db: Database | None, sub: Subscription):
    """Rebuild alert inline keyboard (editMessageMedia drops it if omitted)."""
    if db is None:
        return None
    from handlers.delivery import _alert_chat_button_markup
    from i18n import DEFAULT_LOCALE

    lang = db.get_user_locale(sub.owner_id) or DEFAULT_LOCALE
    bot_username = ""
    if getattr(sub, "attach_live_remind_button", False):
        me = await bot.get_me()
        bot_username = (me.username or "").strip()
    return _alert_chat_button_markup(
        sub, lang, db=db, bot_username=bot_username
    )


async def _edit_preview_media(
    bot,
    sub: Subscription,
    stream: dict[str, Any],
    *,
    captured: CapturedPreview | None,
    bot_data: dict[str, Any] | None = None,
    twitch: TwitchClient | None = None,
    db: Database | None = None,
) -> bool:
    mid = sub.last_message_id
    if not mid:
        return False
    caption, parse_mode, caption_above = _preview_caption(sub, stream, twitch)
    reply_markup = await _preview_reply_markup(bot, db, sub)
    try:
        if is_stream_capture_preview_image(sub.image_file_id):
            # Keep Animation vs Video media type stable so editMessageMedia works.
            if captured is None:
                return False
            as_file_video = is_stream_file_video_preview_image(sub.image_file_id)
            edit_fn = edit_file_video_message if as_file_video else edit_animation_message
            try:
                await edit_fn(
                    bot,
                    chat_id=sub.chat_id,
                    message_id=int(mid),
                    data=captured.data,
                    caption=caption,
                    parse_mode=parse_mode,
                    show_caption_above_media=caption_above,
                    reply_markup=reply_markup,
                )
            except BadRequest as exc:
                err = str(exc).lower()
                if parse_mode and ("parse" in err or "entity" in err or "tag" in err):
                    await edit_fn(
                        bot,
                        chat_id=sub.chat_id,
                        message_id=int(mid),
                        data=captured.data,
                        caption=caption,
                        parse_mode=None,
                        show_caption_above_media=caption_above,
                        reply_markup=reply_markup,
                    )
                else:
                    raise
        else:
            photo = format_stream_thumbnail_url(
                str(stream.get("thumbnail_url") or ""),
                cache_bust=True,
            )
            if not photo:
                return False
            media_kwargs: dict[str, Any] = {
                "media": photo,
                "caption": caption,
                "show_caption_above_media": caption_above,
            }
            if parse_mode:
                media_kwargs["parse_mode"] = parse_mode
            edit_kwargs: dict[str, Any] = {
                "chat_id": sub.chat_id,
                "message_id": mid,
                "media": InputMediaPhoto(**media_kwargs),
            }
            if reply_markup is not None:
                edit_kwargs["reply_markup"] = reply_markup
            try:
                await bot.edit_message_media(**edit_kwargs)
            except BadRequest as exc:
                err = str(exc).lower()
                if parse_mode and ("parse" in err or "entity" in err or "tag" in err):
                    media_kwargs.pop("parse_mode", None)
                    edit_kwargs["media"] = InputMediaPhoto(**media_kwargs)
                    await bot.edit_message_media(**edit_kwargs)
                else:
                    raise
        if is_stream_file_video_preview_image(sub.image_file_id):
            kind = "file_video"
        elif is_stream_video_preview_image(sub.image_file_id):
            kind = "gif"
        else:
            kind = "photo"
        logger.info(
            "Stream preview refreshed sub=%s chat=%s mid=%s kind=%s",
            sub.id,
            sub.chat_id,
            mid,
            kind,
        )
        _clear_preview_skip(bot_data, int(sub.id))
        return True
    except RetryAfter as exc:
        await asyncio.sleep(float(exc.retry_after) + 0.5)
        return False
    except (BadRequest, Forbidden) as exc:
        # Permanent skip only for hard media-type failures (e.g. Animation↔Video).
        # "Message to edit not found" is often a race with placeholder upgrade /
        # delete_previous — skipping would freeze sibling chats for the whole stream.
        if (
            is_stream_capture_preview_image(sub.image_file_id)
            and bot_data is not None
            and _is_permanent_preview_edit_failure(exc)
        ):
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
