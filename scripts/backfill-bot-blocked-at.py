#!/usr/bin/env python3
"""Set users.bot_blocked_at from PostHog bot_blocked / bot_unblocked events.

For currently blocked users: last bot_blocked after last bot_unblocked.
Cleanup date = blocked_at + 365 days.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analytics import distinct_id
from config import (
    BLOCKED_USER_RETENTION_DAYS,
    DATABASE_PATH,
    DATABASE_URL,
    POSTHOG_PERSONAL_API_KEY,
    POSTHOG_PROJECT_ID,
)
from db import open_database


_HOGQL = """
SELECT distinct_id, event, max(timestamp) AS ts
FROM events
WHERE event IN ('bot_blocked', 'bot_unblocked')
GROUP BY distinct_id, event
"""


def _parse_ts(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _hogql_rows() -> list[tuple[str, str, datetime]]:
    if not POSTHOG_PERSONAL_API_KEY:
        print("PostHog personal API key unset; skip event lookup")
        return []
    host = "https://us.posthog.com"
    url = f"{host}/api/projects/{POSTHOG_PROJECT_ID}/query/"
    body = json.dumps({"query": {"kind": "HogQLQuery", "query": _HOGQL}}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {POSTHOG_PERSONAL_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read())
    results = payload.get("results") or payload.get("result") or []
    out: list[tuple[str, str, datetime]] = []
    for row in results:
        if not row or len(row) < 3:
            continue
        ts = _parse_ts(row[2])
        if ts is None:
            continue
        out.append((str(row[0]), str(row[1]), ts))
    return out


def main() -> None:
    db = open_database(DATABASE_PATH, DATABASE_URL)
    blocked = db.list_blocked_user_ids()
    by_id: dict[str, dict[str, datetime]] = {}
    for distinct, event, ts in _hogql_rows():
        slot = by_id.setdefault(distinct, {})
        prev = slot.get(event)
        if prev is None or ts > prev:
            slot[event] = ts

    from_posthog = 0
    no_event = 0
    days = max(1, int(BLOCKED_USER_RETENTION_DAYS))
    for uid in blocked:
        events = by_id.get(distinct_id(uid), {})
        last_block = events.get("bot_blocked")
        last_unblock = events.get("bot_unblocked")
        if last_block is None:
            no_event += 1
            continue
        if last_unblock is not None and last_unblock >= last_block:
            no_event += 1
            continue
        unix = int(last_block.timestamp())
        db.set_bot_blocked_at(uid, unix)
        from_posthog += 1
        cleanup = last_block + timedelta(days=days)
        print(
            f"user={uid} blocked_at={last_block.isoformat()} "
            f"cleanup_at={cleanup.isoformat()}"
        )

    still_null_ids = [
        uid for uid in db.list_blocked_user_ids() if db.get_bot_blocked_at(uid) is None
    ]
    fallback_now = 0
    now = datetime.now(timezone.utc)
    now_unix = int(now.timestamp())
    for uid in still_null_ids:
        db.set_bot_blocked_at(uid, now_unix)
        fallback_now += 1
        cleanup = now + timedelta(days=days)
        print(
            f"user={uid} blocked_at={now.isoformat()} "
            f"cleanup_at={cleanup.isoformat()} source=fallback_now"
        )
    print(
        "backfill_bot_blocked_at: "
        f"blocked={len(blocked)} from_posthog={from_posthog} "
        f"no_event={no_event} fallback_now={fallback_now} "
        f"retention_days={days}"
    )


if __name__ == "__main__":
    main()
