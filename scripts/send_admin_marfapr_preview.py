#!/usr/bin/env python3
"""Send or refresh admin DM live alert for marfapr with video preview.

Inside VPS bot container:
  python scripts/send_admin_marfapr_preview.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("send_admin_marfapr_preview")


async def _edit_animation(bot, chat_id: int, message_id: int, data: bytes) -> bool:
    from telegram import InputMediaAnimation
    from telegram.error import BadRequest, Forbidden

    from handlers.stream_preview import animation_input_file

    try:
        await bot.edit_message_media(
            chat_id=chat_id,
            message_id=message_id,
            media=InputMediaAnimation(
                media=animation_input_file(data),
                width=480,
                height=270,
                duration=30,
            ),
        )
        return True
    except (BadRequest, Forbidden) as exc:
        logger.info("edit mid=%s failed: %s", message_id, exc)
        return False


async def main() -> int:
    from telegram import Bot

    from config import ADMIN_USER_IDS, DATABASE_PATH, DATABASE_URL, TELEGRAM_BOT_TOKEN
    from db import open_database
    from handlers.delivery import _send_notification
    from handlers.stream_preview import build_stream_video_mp4
    from handlers.wizard import _render_sub_template
    from twitch import STREAM_VIDEO_PREVIEW_IMAGE_ID, TwitchClient
    import premium as prem

    if not ADMIN_USER_IDS:
        logger.error("ADMIN_USER_IDS empty")
        return 1

    db = open_database(DATABASE_PATH, DATABASE_URL)
    twitch = TwitchClient()
    twitch.bind_igdb_db(db)
    bot = Bot(TELEGRAM_BOT_TOKEN)

    login = (prem.twitch_channel_login() or "marfapr").lower()
    user = await asyncio.to_thread(twitch.get_user, login)
    if not user:
        logger.error("Twitch user not found: %s", login)
        return 1
    uid = str(user["id"])
    live = await asyncio.to_thread(twitch.get_live_streams, [uid])
    stream = live.get(uid)
    if not stream:
        logger.error("%s is not live", login)
        return 2
    stream = dict(stream)
    stream.setdefault("user_login", login)
    stream.setdefault("user_id", uid)

    template = "В эфире {name}\n{game}"
    bot_data: dict = {}

    for admin_id in ADMIN_USER_IDS:
        admin_id = int(admin_id)
        db.upsert_user(admin_id)
        can_enable = prem.may_enable_subscription(
            db, admin_id, twitch_username=login
        )

        existing = None
        for sub in db.get_subscriptions_by_owner(admin_id):
            if (
                sub.twitch_user_id == uid
                and sub.dest_type == "dm"
                and int(sub.chat_id) == admin_id
                and sub.image_file_id == STREAM_VIDEO_PREVIEW_IMAGE_ID
            ):
                existing = sub
                break

        if existing is None:
            sub_id = db.add_subscription(
                owner_id=admin_id,
                twitch_username=login,
                twitch_user_id=uid,
                message_template=template,
                dest_type="dm",
                chat_id=admin_id,
                thread_id=None,
                enabled=can_enable,
                image_file_id=STREAM_VIDEO_PREVIEW_IMAGE_ID,
                image_position="before",
                notify_on_live=True,
                notify_on_end=False,
                notify_on_category_change=False,
                delete_previous=True,
            )
            existing = db.get_subscription(sub_id, admin_id)
        else:
            db.update_subscription(
                existing.id,
                admin_id,
                enabled=True if can_enable else existing.enabled,
                image_file_id=STREAM_VIDEO_PREVIEW_IMAGE_ID,
                image_position=existing.image_position or "before",
                delete_previous=True,
            )
            existing = db.get_subscription(existing.id, admin_id)

        if existing is None:
            logger.error("No subscription for admin %s", admin_id)
            return 1

        captured = await asyncio.to_thread(
            build_stream_video_mp4,
            login=login,
            twitch_user_id=uid,
            force=True,
        )
        if captured is None:
            logger.error("Capture failed for %s", login)
            return 5
        logger.info("Captured %s bytes", len(captured.data))

        mid = existing.last_message_id
        if mid:
            ok = await _edit_animation(bot, admin_id, int(mid), captured.data)
            logger.info("Refresh tracked mid=%s ok=%s", mid, ok)
            # Orphan from first one-shot send (no last_message_id stored).
            orphan = int(mid) - 1
            if orphan > 0:
                o_ok = await _edit_animation(bot, admin_id, orphan, captured.data)
                logger.info("Refresh orphan mid=%s ok=%s", orphan, o_ok)
            if ok:
                from handlers.stream_preview import mark_preview_refresh

                mark_preview_refresh(bot_data, existing.id)
                from stream_capture import forget_and_unlink

                forget_and_unlink(captured.path)
                logger.info("Updated existing DM(s); no new message.")
                continue

        text = _render_sub_template(
            existing,
            stream.get("user_login", login),
            stream.get("game_name", ""),
            stream.get("title", ""),
            twitch=twitch,
            stream=stream,
        )
        ok = await _send_notification(
            bot,
            db,
            existing,
            text,
            alert_type="live",
            stream=stream,
            twitch=twitch,
            bot_data=bot_data,
        )
        refreshed = db.get_subscription(existing.id, admin_id)
        new_mid = refreshed.last_message_id if refreshed else None
        logger.info(
            "admin=%s sub=%s ok=%s last_message_id=%s",
            admin_id,
            existing.id,
            ok,
            new_mid,
        )
        if not ok or not new_mid:
            return 3

    logger.info("Done. Periodic refresh runs with check_streams.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
