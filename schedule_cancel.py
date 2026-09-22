"""Detect Twitch schedule day cancellations (slots removed without replacement)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def resolve_schedule_tz(tz_name: str | None) -> ZoneInfo:
    raw = (tz_name or "").strip() or "UTC"
    try:
        return ZoneInfo(raw)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _parse_start(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def segment_day_key(start: datetime, tz: ZoneInfo) -> str:
    return start.astimezone(tz).date().isoformat()


def build_schedule_day_map(
    segments: list[dict[str, Any]],
    *,
    now: datetime,
    tz_name: str | None,
) -> dict[str, list[dict[str, str]]]:
    """Group active future segments by broadcaster calendar day.

    Canceled segments (`canceled_until`) are ignored. Past starts are ignored.
    """
    tz = resolve_schedule_tz(tz_name)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    out: dict[str, list[dict[str, str]]] = {}
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        if segment.get("canceled_until"):
            continue
        start = _parse_start(segment.get("start_time"))
        if start is None or start <= now:
            continue
        seg_id = str(segment.get("id") or "").strip()
        if not seg_id:
            continue
        category = segment.get("category") or {}
        game = (
            str(category.get("name") or "")
            if isinstance(category, dict)
            else ""
        )
        day = segment_day_key(start, tz)
        out.setdefault(day, []).append(
            {
                "id": seg_id,
                "start": start.isoformat(),
                "title": str(segment.get("title") or ""),
                "game": game,
            }
        )
    for day in out:
        out[day].sort(key=lambda s: s["start"])
    return out


def find_emptied_schedule_days(
    previous: dict[str, list[dict[str, str]]] | None,
    current: dict[str, list[dict[str, str]]],
    *,
    now: datetime,
    tz_name: str | None = None,
) -> list[tuple[str, list[dict[str, str]]]]:
    """Today-only: local calendar day that had future slots and now has none.

    Future days (tomorrow+) are ignored even if emptied — cancel alerts are only
    for the current broadcaster day. Skips days whose earliest previously seen
    start is already <= now (stream started / scrolled into the past).
    """
    if not previous:
        return []
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now_utc = now.astimezone(timezone.utc)
    tz = resolve_schedule_tz(tz_name)
    today_key = now_utc.astimezone(tz).date().isoformat()
    prev_segs = previous.get(today_key) or []
    if not prev_segs:
        return []
    if current.get(today_key):
        return []
    earliest: datetime | None = None
    for seg in prev_segs:
        start = _parse_start(seg.get("start"))
        if start is None:
            continue
        if earliest is None or start < earliest:
            earliest = start
    if earliest is None or earliest <= now_utc:
        return []
    return [(today_key, list(prev_segs))]


def prune_notified_days(days: list[str], *, keep_after: date) -> list[str]:
    """Drop notified day keys older than keep_after."""
    out: list[str] = []
    for raw in days:
        key = str(raw or "").strip()
        if not key:
            continue
        try:
            d = date.fromisoformat(key)
        except ValueError:
            continue
        if d >= keep_after:
            out.append(key)
    return out


def schedule_cancel_suppressed(
    bot_data: dict[str, Any] | None,
    twitch_user_id: str,
    *,
    now_ts: float | None = None,
) -> bool:
    """True while our own schedule publish is mutating Helix for this channel."""
    import time

    raw = (bot_data or {}).get("schedule_cancel_suppress_until") or {}
    if not isinstance(raw, dict):
        return False
    until = raw.get(str(twitch_user_id))
    try:
        until_f = float(until)
    except (TypeError, ValueError):
        return False
    return until_f > (time.time() if now_ts is None else float(now_ts))


def suppress_schedule_cancel(
    bot_data: dict[str, Any],
    twitch_user_id: str,
    *,
    seconds: float = 600.0,
) -> None:
    """Ignore emptied-day cancels until Helix settles after our own publish."""
    import time

    if not twitch_user_id:
        return
    bucket = bot_data.setdefault("schedule_cancel_suppress_until", {})
    if not isinstance(bucket, dict):
        bot_data["schedule_cancel_suppress_until"] = {}
        bucket = bot_data["schedule_cancel_suppress_until"]
    key = str(twitch_user_id)
    until = time.time() + max(30.0, float(seconds))
    prev = bucket.get(key)
    try:
        prev_f = float(prev) if prev is not None else 0.0
    except (TypeError, ValueError):
        prev_f = 0.0
    if until > prev_f:
        bucket[key] = until


def refresh_schedule_day_snapshot(
    db: Any,
    twitch: Any,
    twitch_user_id: str,
    *,
    now: datetime | None = None,
) -> None:
    """Replace cancel baseline from a fresh Helix read (no notifications)."""
    if not twitch_user_id:
        return
    current = now or datetime.now(timezone.utc)
    schedule = twitch.get_channel_schedule(str(twitch_user_id))
    days = build_schedule_day_map(
        schedule.get("segments") or [],
        now=current,
        tz_name=str(schedule.get("broadcaster_timezone") or "") or "UTC",
    )
    db.set_schedule_day_snapshot(str(twitch_user_id), days)
