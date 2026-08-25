from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from telegram.error import BadRequest, Forbidden
from telegram.ext import ContextTypes

from bot_helpers import _user_notifications_paused
from db import (
    Database,
    Subscription,
    is_category_watch_sub,
    is_on_notify_cooldown,
    parse_category_watch_prefs,
)
from handlers.alert_history import _vod_offset_seconds
from handlers.delivery import _send_notification
from i18n import DEFAULT_LOCALE, t
from twitch import TwitchClient, filter_streams_for_watch, render_template, should_ignore_stream

logger = logging.getLogger(__name__)

_WATCH_CATEGORY_NOTIFY_CAP = 5
# Helix can omit category right after go-live; wait once, then send with whatever we get.
LIVE_GAME_RECHECK_SECONDS = 20


async def _send_delayed_notification(context: ContextTypes.DEFAULT_TYPE) -> None:
    from bot import (
        _effective_ignore_keywords,
        _render_sub_template,
    )

    job_data = context.job.data or {}
    sub_id = job_data["sub_id"]
    silent_offline = bool(job_data.get("silent_offline"))
    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    sub = db.get_subscription_by_id(sub_id)
    if not sub or not sub.enabled or not sub.notify_on_live:
        return

    try:
        live_streams = await asyncio.to_thread(
            twitch.get_live_streams, [sub.twitch_user_id]
        )
    except Exception:
        logger.exception("Twitch poll failed for delayed notification sub %s", sub_id)
        return

    lang = db.get_user_locale(sub.owner_id) or DEFAULT_LOCALE
    if sub.twitch_user_id not in live_streams:
        if silent_offline:
            return
        if _user_notifications_paused(db, sub.owner_id):
            return
        preview = render_template(
            sub.message_template,
            sub.twitch_username,
            "—",
            "—",
        )
        try:
            await context.bot.send_message(
                sub.owner_id,
                t("delayed_not_sent", lang, message=preview),
            )
        except (BadRequest, Forbidden) as exc:
            logger.warning("Cannot notify owner %s: %s", sub.owner_id, exc)
        return

    stream = live_streams[sub.twitch_user_id]
    if is_on_notify_cooldown(sub):
        return
    username = stream.get("user_login", stream.get("user_name", ""))
    game = stream.get("game_name", "")
    title = stream.get("title", "")
    if should_ignore_stream(_effective_ignore_keywords(sub, db), game, title):
        return
    text = _render_sub_template(
        sub, username, game, title, twitch=twitch, stream=stream
    )
    await _send_notification(
        context.bot,
        db,
        sub,
        text,
        alert_type="live",
        stream=stream,
        stream_id=str(job_data.get("stream_id") or ""),
    )


async def _send_delayed_end_notification(context: ContextTypes.DEFAULT_TYPE) -> None:
    from bot import (
        _effective_ignore_keywords,
        _render_sub_template,
    )

    sub_id = context.job.data["sub_id"]
    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    sub = db.get_subscription_by_id(sub_id)
    if not sub or not sub.enabled or not sub.notify_on_end:
        return

    try:
        live_streams = await asyncio.to_thread(
            twitch.get_live_streams, [sub.twitch_user_id]
        )
    except Exception:
        logger.exception("Twitch poll failed for delayed end notification sub %s", sub_id)
        return

    if sub.twitch_user_id in live_streams:
        return
    if is_on_notify_cooldown(sub):
        return
    text = _render_sub_template(sub, sub.twitch_username, "—", "—", twitch=twitch)
    await _send_notification(
        context.bot,
        db,
        sub,
        text,
        alert_type="end",
        stream_id=str((context.job.data or {}).get("stream_id") or ""),
    )


async def _send_delayed_category_notification(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    from bot import (
        _effective_ignore_keywords,
        _render_sub_template,
    )

    sub_id = context.job.data["sub_id"]
    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    sub = db.get_subscription_by_id(sub_id)
    if not sub or not sub.enabled or not sub.notify_on_category_change:
        return

    try:
        live_streams = await asyncio.to_thread(
            twitch.get_live_streams, [sub.twitch_user_id]
        )
    except Exception:
        logger.exception(
            "Twitch poll failed for delayed category notification sub %s", sub_id
        )
        return

    if sub.twitch_user_id not in live_streams:
        return
    if is_on_notify_cooldown(sub):
        return
    stream = live_streams[sub.twitch_user_id]
    username = stream.get("user_login", stream.get("user_name", ""))
    game = stream.get("game_name", "")
    title = stream.get("title", "")
    if should_ignore_stream(_effective_ignore_keywords(sub, db), game, title):
        return
    text = _render_sub_template(
        sub, username, game, title, twitch=twitch, stream=stream
    )
    job_data = context.job.data or {}
    await _send_notification(
        context.bot,
        db,
        sub,
        text,
        alert_type="category",
        stream=stream,
        stream_id=str(job_data.get("stream_id") or ""),
        vod_offset_seconds=job_data.get("vod_offset_seconds"),
    )


async def check_streams(context: ContextTypes.DEFAULT_TYPE) -> None:
    import premium as prem
    from bot import (
        _effective_ignore_keywords,
        _render_sub_template,
    )
    from bot_helpers import _user_lang
    from config import CHECK_INTERVAL

    db: Database = context.application.bot_data["db"]
    expired_trials = prem.expire_due_trials(db)
    if expired_trials:
        logger.info("Expired %s premium trial(s)", len(expired_trials))
        max_age = max(int(CHECK_INTERVAL) * 2, 120)
        now = int(datetime.now(timezone.utc).timestamp())
        for user_id, trial_until in expired_trials:
            if not prem.trial_expiry_should_notify(
                trial_until, now=now, max_age_sec=max_age
            ):
                continue
            if db.is_bot_blocked(user_id):
                continue
            lang = _user_lang(context, user_id) or DEFAULT_LOCALE
            try:
                await context.bot.send_message(
                    user_id, t("premium_trial_expired", lang)
                )
            except Forbidden:
                db.set_bot_blocked(user_id, True)
            except BadRequest:
                logger.exception(
                    "Failed to send trial expiry notice to %s", user_id
                )

    twitch: TwitchClient = context.application.bot_data["twitch"]
    last_live: dict[str, bool] = context.application.bot_data.setdefault("last_live", {})
    last_games: dict[str, str] = context.application.bot_data.setdefault("last_games", {})
    last_stream_ids: dict[str, str] = context.application.bot_data.setdefault(
        "last_stream_ids", {}
    )
    # After restart memory is empty; first successful poll only seeds state so
    # already-live streams are not treated as fresh starts.
    primed = bool(context.application.bot_data.get("last_live_primed"))

    user_ids = db.get_unique_twitch_user_ids()
    category_watch_subs = db.get_enabled_category_watch_subscriptions()
    if not user_ids and not category_watch_subs:
        return

    live_streams: dict[str, dict] = {}
    if user_ids:
        try:
            live_streams = await asyncio.to_thread(twitch.get_live_streams, user_ids)
        except Exception:
            logger.exception("Twitch poll failed")
            live_streams = {}
        else:
            went_live, went_offline = live_transitions(
                last_live, user_ids, live_streams, primed=primed
            )
            category_changed = category_change_events(
                last_games, user_ids, live_streams, primed=primed
            )
            offline_stream_ids = {
                uid: last_stream_ids.get(uid, "") for uid in went_offline
            }
            for uid, stream in live_streams.items():
                sid = str(stream.get("id") or "")
                if sid:
                    last_stream_ids[uid] = sid
            context.application.bot_data["last_live_primed"] = True

            for uid in went_live:
                stream = live_streams[uid]
                username = stream.get("user_login", stream.get("user_name", ""))
                game = stream.get("game_name", "")
                title = stream.get("title", "")
                stream_id = str(stream.get("id") or "")
                for sub in db.get_enabled_by_twitch_user_id(uid):
                    if is_category_watch_sub(sub):
                        continue
                    if not sub.notify_on_live:
                        continue
                    if is_on_notify_cooldown(sub):
                        continue
                    if should_ignore_stream(
                        _effective_ignore_keywords(sub, db), game, title
                    ):
                        continue
                    if sub.delay_minutes > 0:
                        context.job_queue.run_once(
                            _send_delayed_notification,
                            when=sub.delay_minutes * 60,
                            data={"sub_id": sub.id, "stream_id": stream_id},
                            name=f"delay_{sub.id}",
                        )
                        continue
                    # Helix often returns empty game_name for a few seconds after go-live.
                    if needs_live_game_recheck(game, sub.delay_minutes):
                        context.job_queue.run_once(
                            _send_delayed_notification,
                            when=LIVE_GAME_RECHECK_SECONDS,
                            data={
                                "sub_id": sub.id,
                                "silent_offline": True,
                                "stream_id": stream_id,
                            },
                            name=f"live_game_{sub.id}",
                        )
                        continue
                    text = _render_sub_template(
                        sub, username, game, title, twitch=twitch, stream=stream
                    )
                    await _send_notification(
                        context.bot, db, sub, text, alert_type="live", stream=stream
                    )

            for uid in went_offline:
                stream_id = offline_stream_ids.get(uid, "")
                for sub in db.get_enabled_by_twitch_user_id(uid):
                    if is_category_watch_sub(sub):
                        continue
                    if not sub.notify_on_end:
                        continue
                    if is_on_notify_cooldown(sub):
                        continue
                    if sub.delay_minutes > 0:
                        context.job_queue.run_once(
                            _send_delayed_end_notification,
                            when=sub.delay_minutes * 60,
                            data={"sub_id": sub.id, "stream_id": stream_id},
                            name=f"delay_end_{sub.id}",
                        )
                        continue
                    text = _render_sub_template(
                        sub, sub.twitch_username, "—", "—", twitch=twitch
                    )
                    await _send_notification(
                        context.bot,
                        db,
                        sub,
                        text,
                        alert_type="end",
                        stream_id=stream_id,
                    )

            for uid in category_changed:
                stream = live_streams[uid]
                username = stream.get("user_login", stream.get("user_name", ""))
                game = stream.get("game_name", "")
                title = stream.get("title", "")
                stream_id = str(stream.get("id") or "")
                for sub in db.get_enabled_by_twitch_user_id(uid):
                    if is_category_watch_sub(sub):
                        continue
                    if not sub.notify_on_category_change:
                        continue
                    if is_on_notify_cooldown(sub):
                        continue
                    if should_ignore_stream(
                        _effective_ignore_keywords(sub, db), game, title
                    ):
                        continue
                    if sub.delay_minutes > 0:
                        game_id = str(stream.get("game_id") or "")
                        context.job_queue.run_once(
                            _send_delayed_category_notification,
                            when=sub.delay_minutes * 60,
                            data={
                                "sub_id": sub.id,
                                "stream_id": stream_id,
                                "vod_offset_seconds": _vod_offset_seconds(stream),
                            },
                            name=f"delay_cat_{sub.id}_{game_id}",
                        )
                        continue
                    text = _render_sub_template(
                        sub, username, game, title, twitch=twitch, stream=stream
                    )
                    await _send_notification(
                        context.bot,
                        db,
                        sub,
                        text,
                        alert_type="category",
                        stream=stream,
                        stream_id=stream_id,
                        vod_offset_seconds=_vod_offset_seconds(stream),
                    )

    if category_watch_subs:
        await _check_category_watch_alerts(context, category_watch_subs)


def _parse_category_watch_live_ids(raw: str | None) -> set[str]:
    text = (raw or "").strip()
    if not text:
        return set()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return set()
    if not isinstance(data, list):
        return set()
    return {str(x).strip() for x in data if str(x).strip()}


async def _check_category_watch_alerts(
    context: ContextTypes.DEFAULT_TYPE, subs: list[Subscription]
) -> None:
    from bot import (
        _effective_ignore_keywords,
        _render_sub_template,
    )

    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    for sub in subs:
        prefs = parse_category_watch_prefs(sub.category_watch_prefs)
        if not prefs or not prefs.categories:
            continue
        pooled: list[dict] = []
        for cat in prefs.categories:
            try:
                batch = await asyncio.to_thread(
                    twitch.get_streams_by_game,
                    cat["id"],
                    language=prefs.language,
                    first=100,
                )
            except Exception:
                logger.exception(
                    "category watch fetch failed sub=%s game_id=%s",
                    sub.id,
                    cat.get("id"),
                )
                continue
            pooled.extend(batch)
        filtered = filter_streams_for_watch(
            pooled,
            min_viewers=prefs.min_viewers,
            max_viewers=prefs.max_viewers,
            exclude_mature=prefs.exclude_mature,
            tags=prefs.tags,
        )
        by_uid: dict[str, dict] = {}
        for stream in filtered:
            uid = str(stream.get("user_id") or "").strip()
            if uid and uid not in by_uid:
                by_uid[uid] = stream
        current_uids = set(by_uid)
        prev_uids = _parse_category_watch_live_ids(sub.category_watch_live_ids)
        if not sub.category_watch_primed:
            db.set_category_watch_live_state(
                sub.id, sorted(current_uids), primed=True
            )
            continue
        new_uids = sorted(current_uids - prev_uids)
        notified = 0
        for uid in new_uids:
            if notified >= _WATCH_CATEGORY_NOTIFY_CAP:
                break
            stream = by_uid[uid]
            username = stream.get("user_login", stream.get("user_name", ""))
            game = stream.get("game_name", "")
            title = stream.get("title", "")
            text = _render_sub_template(
                sub, username, game, title, twitch=twitch, stream=stream
            )
            await _send_notification(
                context.bot, db, sub, text, alert_type="live", stream=stream
            )
            notified += 1
        db.set_category_watch_live_state(sub.id, sorted(current_uids), primed=True)


def _parse_segment_start(segment: dict) -> datetime | None:
    raw = segment.get("start_time")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def check_schedule_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    from bot import (
        _effective_ignore_keywords,
        _render_sub_template,
    )

    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    user_ids = db.get_unique_schedule_reminder_twitch_ids()
    if not user_ids:
        return
    now = datetime.now(timezone.utc)
    for uid in user_ids:
        try:
            segments = await asyncio.to_thread(twitch.get_schedule_segments, uid)
        except Exception:
            logger.exception("Twitch schedule poll failed for %s", uid)
            continue
        for sub in db.get_enabled_by_twitch_user_id(uid):
            remind_before = int(sub.schedule_reminder_minutes or 0)
            if remind_before <= 0:
                continue
            for segment in segments:
                if segment.get("canceled_until"):
                    continue
                seg_id = str(segment.get("id") or "")
                if not seg_id or seg_id == sub.last_schedule_reminder_segment_id:
                    continue
                start = _parse_segment_start(segment)
                if start is None or start <= now:
                    continue
                minutes_left = int((start - now).total_seconds() // 60)
                if minutes_left > remind_before:
                    continue
                username = sub.twitch_username
                title = str(segment.get("title") or "—")
                category = segment.get("category") or {}
                game = (
                    str(category.get("name") or "")
                    if isinstance(category, dict)
                    else ""
                )
                template = (sub.message_template or "").strip()
                if not template:
                    continue
                text = _render_sub_template(
                    sub,
                    username,
                    game,
                    title,
                    twitch=twitch,
                    extra={"minutes": str(max(1, minutes_left))},
                )
                ok = await _send_notification(
                    context.bot, db, sub, text, alert_type="schedule"
                )
                if not ok:
                    continue
                db.set_last_schedule_reminder_segment(sub.id, seg_id)
                break



def needs_live_game_recheck(game: str, delay_minutes: int) -> bool:
    """True when live alert should wait briefly for Helix to fill game_name."""
    return int(delay_minutes or 0) <= 0 and not (game or "").strip()


def live_transitions(
    last_live: dict[str, bool],
    user_ids: list[str],
    live_ids: set[str] | dict,
    *,
    primed: bool,
) -> tuple[list[str], list[str]]:
    """Update last_live; return (went_live, went_offline) when primed."""
    went_live: list[str] = []
    went_offline: list[str] = []
    for uid in user_ids:
        is_live = uid in live_ids
        was_live = last_live.get(uid, False)
        if primed:
            if is_live and not was_live:
                went_live.append(uid)
            if was_live and not is_live:
                went_offline.append(uid)
        last_live[uid] = is_live
    return went_live, went_offline


def category_change_events(
    last_games: dict[str, str],
    user_ids: list[str],
    live_streams: dict[str, dict],
    *,
    primed: bool,
) -> list[str]:
    """Update last_games; return uids whose game_id changed while live (when primed)."""
    changed: list[str] = []
    for uid in user_ids:
        if uid not in live_streams:
            last_games.pop(uid, None)
            continue
        game_id = str(live_streams[uid].get("game_id") or "")
        prev = last_games.get(uid)
        if primed and prev is not None and game_id != prev:
            changed.append(uid)
        last_games[uid] = game_id
    return changed
