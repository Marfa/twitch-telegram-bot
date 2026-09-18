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
) -> list[tuple[str, list[dict[str, str]]]]:
    """Days that had future slots and now have none, without replacement.

    Skips days whose earliest previously seen start is already <= now
    (scrolled into the past / stream started) so Helix window drift does not
    look like a cancellation.
    """
    if not previous:
        return []
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    emptied: list[tuple[str, list[dict[str, str]]]] = []
    for day, prev_segs in previous.items():
        if not prev_segs:
            continue
        if current.get(day):
            continue
        earliest: datetime | None = None
        for seg in prev_segs:
            start = _parse_start(seg.get("start"))
            if start is None:
                continue
            if earliest is None or start < earliest:
                earliest = start
        if earliest is None or earliest <= now:
            continue
        emptied.append((day, list(prev_segs)))
    emptied.sort(key=lambda item: item[0])
    return emptied


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
