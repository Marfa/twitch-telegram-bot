"""Scheduled live-alert repeats + overlap-guard helpers."""

from __future__ import annotations

from typing import Any

BETA_FEATURE_ID = "alert-repeats-overlap"

REPEAT_NOTIFY_MAX_VALUES = 8
REPEAT_NOTIFY_MAX_MINUTES = 24 * 60


def parse_repeat_notify_minutes(raw: str | None) -> list[int]:
    """Parse CSV minutes. Lone 0 / empty → []. Invalid tokens ignored."""
    text = str(raw or "").strip()
    if not text or text == "0":
        return []
    out: list[int] = []
    seen: set[int] = set()
    for part in text.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            minutes = int(token)
        except ValueError:
            continue
        if minutes <= 0 or minutes > REPEAT_NOTIFY_MAX_MINUTES:
            continue
        if minutes in seen:
            continue
        seen.add(minutes)
        out.append(minutes)
        if len(out) >= REPEAT_NOTIFY_MAX_VALUES:
            break
    return sorted(out)


def format_repeat_notify_minutes(minutes: list[int] | str | None) -> str:
    if isinstance(minutes, str):
        mins = parse_repeat_notify_minutes(minutes)
    else:
        mins = [int(m) for m in (minutes or []) if int(m) > 0]
    if not mins:
        return ""
    return ",".join(str(m) for m in sorted(set(mins))[:REPEAT_NOTIFY_MAX_VALUES])


def repeat_notify_job_name(sub_id: int, minutes: int) -> str:
    return f"repeat_{int(sub_id)}_{int(minutes)}"


def repeat_notify_job_prefix(sub_id: int) -> str:
    return f"repeat_{int(sub_id)}_"


def sub_tracks_last_message_for_overlap(sub: Any) -> bool:
    """Channel/group subs that need last_message_id for overlap/repeats."""
    if str(getattr(sub, "dest_type", "") or "") == "dm":
        return False
    if bool(getattr(sub, "delete_previous", False)):
        return True
    if bool(getattr(sub, "block_overlap", False)):
        return True
    if bool(getattr(sub, "block_repeat_when_last", False)):
        return True
    if parse_repeat_notify_minutes(getattr(sub, "repeat_notify_minutes", "")):
        return True
    return False


def beta_or_advanced_mode(
    db: Any, user_id: int, *, has_advanced_mode: bool
) -> bool:
    """Edit/create gate: Premium advanced_mode, or beta enrollment while stage is beta."""
    if has_advanced_mode:
        return True
    import beta as beta_features

    return beta_features.is_enabled(db, user_id, BETA_FEATURE_ID)


def is_last_alert_in_chat(db: Any, sub: Any) -> bool:
    """True when this sub's last_message_id is the newest among owner alerts in chat."""
    mid = getattr(sub, "last_message_id", None)
    if mid is None:
        return False
    chat_id = int(sub.chat_id)
    thread_id = sub.thread_id or None
    owner_id = int(sub.owner_id)
    max_mid = int(mid)
    for other in db.get_subscriptions_by_owner(owner_id):
        if other.id == sub.id:
            continue
        if int(other.chat_id) != chat_id:
            continue
        if (other.thread_id or None) != thread_id:
            continue
        if str(getattr(other, "dest_type", "") or "") == "dm":
            continue
        other_mid = getattr(other, "last_message_id", None)
        if other_mid is None:
            continue
        if int(other_mid) > max_mid:
            return False
    return True
