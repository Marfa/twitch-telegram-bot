"""In-process admin Demo mode: free-tier UX with disposable demo subscriptions."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from db import Database

_active: set[int] = set()
_snapshots: dict[int, dict[str, Any]] = {}


def is_active(user_id: int) -> bool:
    return user_id in _active


def activate(user_id: int) -> None:
    _active.add(user_id)


def deactivate(user_id: int) -> None:
    _active.discard(user_id)
    _snapshots.pop(user_id, None)


def capture_user_state(db: Database, user_id: int) -> None:
    """Snapshot prefs that demo can mutate; restored on exit. Not Premium/Stars."""
    import beta as beta_features

    beta: dict[str, bool | None] = {}
    for feat in beta_features.list_features():
        beta[feat.id] = db.beta_enrollment_explicit(user_id, feat.id)
    hour, minute = db.get_saved_schedule(user_id)
    _snapshots[user_id] = {
        "locale": db.get_user_locale(user_id),
        "watch": db.get_watch_filters(user_id),
        "ignore": db.get_global_ignore_keywords(user_id),
        "advanced_mode": db.get_advanced_mode_setting(user_id),
        "paused_until": db.get_notifications_paused_until(user_id),
        "tz_offset": db.get_schedule_utc_offset_minutes(user_id),
        "sched_hour": hour,
        "sched_minute": minute,
        "recv_bot": db.get_receive_bot_updates(user_id),
        "recv_avail": db.get_receive_availability_updates(user_id),
        "recv_other": db.get_receive_other_updates(user_id),
        "recv_sync": db.get_receive_sync_updates(user_id),
        "beta": beta,
        "sync": db.get_twitch_sync(user_id),
        "whisper": db.get_whisper_alert(user_id),
        "chat_auth": db.get_chat_auth(user_id),
    }


def restore_user_state(db: Database, user_id: int) -> None:
    snap = _snapshots.pop(user_id, None)
    if snap is None:
        return
    locale = snap.get("locale")
    if locale:
        db.set_user_locale(user_id, locale)
    db.set_watch_filters(user_id, list(snap.get("watch") or []))
    db.set_global_ignore_keywords(user_id, str(snap.get("ignore") or ""))
    adv = snap.get("advanced_mode")
    if adv is not None:
        db.set_advanced_mode_setting(user_id, bool(adv))
    db.set_notifications_paused_until(user_id, int(snap.get("paused_until") or 0))
    tz = snap.get("tz_offset")
    if tz is not None:
        db.set_schedule_utc_offset_minutes(user_id, int(tz))
    hour, minute = snap.get("sched_hour"), snap.get("sched_minute")
    if hour is not None and minute is not None:
        db.set_saved_schedule(user_id, int(hour), int(minute))
    db.set_receive_bot_updates(user_id, bool(snap.get("recv_bot", True)))
    db.set_receive_availability_updates(user_id, bool(snap.get("recv_avail", True)))
    db.set_receive_other_updates(user_id, bool(snap.get("recv_other", True)))
    db.set_receive_sync_updates(user_id, bool(snap.get("recv_sync", True)))
    for fid, enrolled in (snap.get("beta") or {}).items():
        if enrolled is None:
            db.clear_beta_enrollment(user_id, fid)
        else:
            db.set_beta_enrollment(user_id, fid, bool(enrolled))
    sync = snap.get("sync")
    if sync is None:
        db.delete_twitch_sync(user_id)
    else:
        db.upsert_twitch_sync(
            user_id,
            sync.twitch_user_id,
            sync.refresh_token,
            sync.period_days,
            sync.next_sync_at,
            sync.last_sync_at,
        )
    whisper = snap.get("whisper")
    if whisper is None:
        db.delete_whisper_alert(user_id)
    else:
        db.upsert_whisper_alert(
            user_id,
            enabled=whisper.enabled,
            twitch_user_id=whisper.twitch_user_id,
            twitch_login=whisper.twitch_login,
            refresh_token=whisper.refresh_token,
            eventsub_id=whisper.eventsub_id,
        )
    auth = snap.get("chat_auth")
    if auth is None:
        db.delete_chat_auth(user_id)
    else:
        db.upsert_chat_auth(
            user_id,
            twitch_user_id=auth.twitch_user_id,
            twitch_login=auth.twitch_login,
            refresh_token=auth.refresh_token,
        )
