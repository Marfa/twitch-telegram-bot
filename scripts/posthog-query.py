#!/usr/bin/env python3
"""Run PostHog HogQL (Personal API key).

Usage (local or on VPS /opt/twitch-telegram-bot with .env):

  python scripts/posthog-query.py --query "SELECT count() FROM events WHERE event = 'bot_blocked'"
  python scripts/posthog-query.py --file /tmp/q.hogql
  python scripts/posthog-query.py --report bot_blocked_week
  python scripts/posthog-query.py --report bot_blocked_week --days 7

Requires POSTHOG_API_KEY_PERSONAL (and optional POSTHOG_PROJECT_ID).
Loads repo /.env for missing keys. Never prints the API key.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from posthog_hogql import hogql, load_env_file  # noqa: E402


def _bot_blocked_week_queries(days: int) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days)).replace(microsecond=0)
    start_s = start.strftime("%Y-%m-%d %H:%M:%S")
    end_s = now.strftime("%Y-%m-%d %H:%M:%S")
    prev_start = (start - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "_meta": json.dumps(
            {
                "window": {"start": start.isoformat(), "end": now.isoformat()},
                "days": days,
            }
        ),
        "totals": f"""
SELECT
  countIf(event = 'bot_blocked') AS blocks,
  countIf(event = 'bot_unblocked') AS unblocks,
  uniqExactIf(distinct_id, event = 'bot_blocked') AS block_users,
  uniqExactIf(distinct_id, event = 'bot_unblocked') AS unblock_users
FROM events
WHERE event IN ('bot_blocked', 'bot_unblocked')
  AND timestamp >= toDateTime('{start_s}')
  AND timestamp < toDateTime('{end_s}')
""",
        "by_source": f"""
SELECT
  coalesce(toString(properties.source), '(null)') AS source,
  count() AS events,
  count(DISTINCT distinct_id) AS users
FROM events
WHERE event = 'bot_blocked'
  AND timestamp >= toDateTime('{start_s}')
  AND timestamp < toDateTime('{end_s}')
GROUP BY source
ORDER BY events DESC
""",
        "by_day": f"""
SELECT
  toDate(timestamp) AS day,
  count() AS events,
  count(DISTINCT distinct_id) AS users
FROM events
WHERE event = 'bot_blocked'
  AND timestamp >= toDateTime('{start_s}')
  AND timestamp < toDateTime('{end_s}')
GROUP BY day
ORDER BY day
""",
        "source_x_alert": f"""
SELECT
  coalesce(toString(properties.source), '(null)') AS source,
  coalesce(
    nullIf(toString(properties.alert_type), ''),
    nullIf(toString(properties.last_alert_type), ''),
    '(null)'
  ) AS alert_type,
  count() AS events,
  count(DISTINCT distinct_id) AS users
FROM events
WHERE event = 'bot_blocked'
  AND timestamp >= toDateTime('{start_s}')
  AND timestamp < toDateTime('{end_s}')
GROUP BY source, alert_type
ORDER BY events DESC
LIMIT 40
""",
        "last_alert_age": f"""
SELECT
  coalesce(toString(properties.source), '(null)') AS source,
  multiIf(
    isNull(properties.last_alert_age_hours), 'no_last_alert',
    toFloat(properties.last_alert_age_hours) < 1, '<1h',
    toFloat(properties.last_alert_age_hours) < 24, '1-24h',
    toFloat(properties.last_alert_age_hours) < 168, '1-7d',
    '7d+'
  ) AS last_alert_age,
  count() AS events,
  count(DISTINCT distinct_id) AS users
FROM events
WHERE event = 'bot_blocked'
  AND timestamp >= toDateTime('{start_s}')
  AND timestamp < toDateTime('{end_s}')
GROUP BY source, last_alert_age
ORDER BY events DESC
LIMIT 40
""",
        "top_channels": f"""
SELECT
  coalesce(
    nullIf(toString(properties.twitch_username), ''),
    nullIf(toString(properties.last_alert_channel), ''),
    '(null)'
  ) AS channel,
  count() AS events,
  count(DISTINCT distinct_id) AS users
FROM events
WHERE event = 'bot_blocked'
  AND timestamp >= toDateTime('{start_s}')
  AND timestamp < toDateTime('{end_s}')
GROUP BY channel
ORDER BY events DESC
LIMIT 20
""",
        "active_subs": f"""
SELECT
  coalesce(toString(properties.source), '(null)') AS source,
  multiIf(
    isNull(properties.active_subs), 'unknown',
    toFloat(properties.active_subs) = 0, '0',
    toFloat(properties.active_subs) <= 5, '1-5',
    toFloat(properties.active_subs) <= 20, '6-20',
    '21+'
  ) AS active_subs_bucket,
  count() AS events,
  count(DISTINCT distinct_id) AS users
FROM events
WHERE event = 'bot_blocked'
  AND timestamp >= toDateTime('{start_s}')
  AND timestamp < toDateTime('{end_s}')
GROUP BY source, active_subs_bucket
ORDER BY events DESC
LIMIT 40
""",
        "prev_period_by_source": f"""
SELECT
  coalesce(toString(properties.source), '(null)') AS source,
  count() AS events,
  count(DISTINCT distinct_id) AS users
FROM events
WHERE event = 'bot_blocked'
  AND timestamp >= toDateTime('{prev_start}')
  AND timestamp < toDateTime('{start_s}')
GROUP BY source
ORDER BY events DESC
""",
        "unblocks_by_day": f"""
SELECT
  toDate(timestamp) AS day,
  count() AS events,
  count(DISTINCT distinct_id) AS users
FROM events
WHERE event = 'bot_unblocked'
  AND timestamp >= toDateTime('{start_s}')
  AND timestamp < toDateTime('{end_s}')
GROUP BY day
ORDER BY day
""",
    }


def _run_report(name: str, days: int) -> dict:
    if name == "bot_blocked_week":
        queries = _bot_blocked_week_queries(days)
        out: dict = {}
        meta_raw = queries.pop("_meta", None)
        if meta_raw:
            out["meta"] = json.loads(meta_raw)
        for qname, q in queries.items():
            try:
                payload = hogql(q)
                out[qname] = {
                    "columns": payload.get("columns"),
                    "results": payload.get("results") or payload.get("result") or [],
                }
                err = payload.get("error") or payload.get("detail")
                if err:
                    out[qname]["error"] = err
            except RuntimeError as exc:
                out[qname] = {"error": str(exc)}
        return out
    raise SystemExit(f"unknown report: {name} (known: bot_blocked_week)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", help="Inline HogQL")
    parser.add_argument("--file", type=Path, help="Read HogQL from file")
    parser.add_argument(
        "--report",
        choices=("bot_blocked_week",),
        help="Named multi-query report",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Window length for reports (default 7)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Optional .env path (default: repo .env)",
    )
    args = parser.parse_args()
    load_env_file(args.env_file)

    modes = sum(1 for x in (args.query, args.file, args.report) if x)
    if modes != 1:
        parser.error("specify exactly one of --query, --file, --report")

    if args.report:
        print(json.dumps(_run_report(args.report, max(1, args.days)), indent=2, default=str))
        return

    if args.file:
        query = args.file.read_text(encoding="utf-8")
    else:
        query = args.query or ""
    if not query.strip():
        raise SystemExit("empty query")

    try:
        payload = hogql(query)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(payload, indent=2, default=str))


if __name__ == "__main__":
    main()
