#!/usr/bin/env python3
"""Send current (and optional reconstructed) daily_bot_stats to PostHog.

Usage (on VPS / in bot container, with DATABASE_URL + POSTHOG_API_KEY):

  python scripts/posthog-stats-snapshot.py
  python scripts/posthog-stats-snapshot.py --backfill

Backfill rebuilds approximate end-of-day snapshots from users.first_seen and
subscriptions.created_at, applying *current* flags (blocked, locale, enabled,
receive_*). There is no true historical stats table — treat backfill as growth
curves, not perfect audit.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analytics import (
    capture,
    capture_bot_stats,
    init_analytics,
    is_enabled,
    shutdown_analytics,
)
from config import DATABASE_PATH, DATABASE_URL
from db import BotStats, open_database


def _send_live(stats: BotStats) -> None:
    capture_bot_stats(stats, timestamp=datetime.now(timezone.utc))


def _send_backfill(stats: SimpleNamespace, when: datetime) -> None:
    capture(
        None,
        "daily_bot_stats",
        {
            "users": stats.users,
            "notify_users": stats.notify_users,
            "unique_owners": stats.unique_owners,
            "subscriptions_total": stats.subscriptions_total,
            "subscriptions_enabled": stats.subscriptions_enabled,
            "subscriptions_disabled": stats.subscriptions_disabled,
            "unique_twitch_channels": stats.unique_twitch_channels,
            "premium_paid": stats.premium_paid,
            "blocked_users": stats.blocked_users,
            "sys_updates": stats.sys_updates,
            "sys_availability": stats.sys_availability,
            "sys_other": stats.sys_other,
            "locale_en": stats.locale_en,
            "locale_ru": stats.locale_ru,
            "locale_unset": stats.locale_unset,
            "backfill": True,
        },
        timestamp=when,
    )


def _backfill_postgres(db) -> int:
    """Return number of historical days sent (excludes today — use live snapshot)."""
    with db._conn() as conn:
        cur = db._cursor(conn)
        cur.execute(
            """
            SELECT LEAST(
              (SELECT MIN(first_seen)::date FROM users),
              (SELECT MIN(created_at)::date FROM subscriptions)
            ) AS start_day
            """
        )
        row = cur.fetchone()
        start = row["start_day"] if row else None
        if start is None:
            return 0
        start_day = start if hasattr(start, "year") else date.fromisoformat(str(start)[:10])
        end_day = datetime.now(timezone.utc).date() - timedelta(days=1)
        if end_day < start_day:
            return 0

        sent = 0
        day = start_day
        while day <= end_day:
            cur.execute(
                """
                WITH
                active_users AS (
                  SELECT * FROM users u
                  WHERE COALESCE(u.bot_blocked, FALSE) = FALSE
                    AND (u.first_seen AT TIME ZONE 'UTC')::date <= %s
                ),
                active_subs AS (
                  SELECT s.* FROM subscriptions s
                  LEFT JOIN users u ON u.user_id = s.owner_id
                  WHERE COALESCE(u.bot_blocked, FALSE) = FALSE
                    AND (s.created_at AT TIME ZONE 'UTC')::date <= %s
                ),
                people AS (
                  SELECT user_id AS id FROM active_users
                  UNION
                  SELECT DISTINCT owner_id AS id FROM active_subs
                )
                SELECT
                  (SELECT COUNT(*) FROM active_users) AS users,
                  (SELECT COUNT(*) FROM (
                     SELECT user_id AS id FROM active_users
                     UNION
                     SELECT DISTINCT owner_id FROM active_subs
                  ) n) AS notify_users,
                  (SELECT COUNT(*) FROM active_subs) AS subscriptions_total,
                  (SELECT COUNT(*) FROM active_subs WHERE enabled) AS subscriptions_enabled,
                  (SELECT COUNT(*) FROM active_subs WHERE NOT enabled) AS subscriptions_disabled,
                  (SELECT COUNT(DISTINCT owner_id) FROM active_subs) AS unique_owners,
                  (SELECT COUNT(DISTINCT twitch_user_id) FROM active_subs)
                    AS unique_twitch_channels,
                  (SELECT COUNT(*) FROM active_users u
                     WHERE COALESCE(u.premium_stars_until, 0)
                           > EXTRACT(EPOCH FROM NOW())::bigint
                        OR COALESCE(u.premium_twitch_active, FALSE) = TRUE
                        OR COALESCE(u.premium_permanent, FALSE) = TRUE
                        OR COALESCE(u.premium_features, '') NOT IN ('', '{}')
                  ) AS premium_paid,
                  (SELECT COUNT(*) FROM users
                     WHERE bot_blocked = TRUE
                       AND (first_seen AT TIME ZONE 'UTC')::date <= %s
                  ) AS blocked_users,
                  (SELECT COUNT(*) FROM people p
                     LEFT JOIN users u ON u.user_id = p.id
                     WHERE COALESCE(u.receive_bot_updates, TRUE)) AS sys_updates,
                  (SELECT COUNT(*) FROM people p
                     LEFT JOIN users u ON u.user_id = p.id
                     WHERE COALESCE(u.receive_availability_updates, TRUE)
                  ) AS sys_availability,
                  (SELECT COUNT(*) FROM people p
                     LEFT JOIN users u ON u.user_id = p.id
                     WHERE COALESCE(u.receive_other_updates, TRUE)) AS sys_other,
                  (SELECT COUNT(*) FROM active_users WHERE locale = 'en') AS locale_en,
                  (SELECT COUNT(*) FROM active_users WHERE locale = 'ru') AS locale_ru,
                  (SELECT COUNT(*) FROM active_users
                     WHERE locale IS NULL OR locale = '') AS locale_unset
                """,
                (day, day, day),
            )
            stats_row = cur.fetchone()
            stats = SimpleNamespace(
                **{k: int(stats_row[k] or 0) for k in stats_row.keys()}
            )
            when = datetime.combine(day, time(3, 0), tzinfo=timezone.utc)
            _send_backfill(stats, when)
            sent += 1
            day += timedelta(days=1)
        return sent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Reconstruct daily snapshots from first_seen/created_at (Postgres)",
    )
    args = parser.parse_args()

    init_analytics()
    if not is_enabled():
        raise SystemExit("PostHog disabled (POSTHOG_API_KEY unset)")

    db = open_database(DATABASE_PATH, DATABASE_URL)
    if args.backfill:
        if not DATABASE_URL:
            raise SystemExit("--backfill requires DATABASE_URL (Postgres)")
        print(f"backfilled_days {_backfill_postgres(db)}")

    stats = db.get_bot_stats()
    _send_live(stats)
    print(
        "snapshot_sent",
        f"users={stats.users}",
        f"subs={stats.subscriptions_total}",
        f"blocked={stats.blocked_users}",
    )
    shutdown_analytics()


if __name__ == "__main__":
    main()
