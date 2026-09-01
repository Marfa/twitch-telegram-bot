#!/usr/bin/env python3
"""Count subscriptions / owners with likely placeholder typos in message_template."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import DATABASE_PATH, DATABASE_URL
from twitch import find_placeholder_typos

_QUERY = """
SELECT id, owner_id, message_template, enabled, notify_on_end
FROM subscriptions
"""


def _fetch_rows() -> list[dict]:
    if DATABASE_URL:
        import psycopg

        url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        with psycopg.connect(url) as conn:
            with conn.cursor() as cur:
                cur.execute(_QUERY)
                cols = [d.name for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(_QUERY).fetchall()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enabled-only", action="store_true")
    parser.add_argument("--end-only", action="store_true")
    args = parser.parse_args()

    rows = _fetch_rows()
    typo_subs: list[dict] = []
    typo_counter: Counter[tuple[str, str]] = Counter()
    examples: dict[tuple[str, str], list[tuple[int, int, str]]] = defaultdict(list)

    for row in rows:
        if args.enabled_only and not row.get("enabled"):
            continue
        if args.end_only and not row.get("notify_on_end"):
            continue
        template = str(row.get("message_template") or "")
        typos = find_placeholder_typos(template)
        if not typos:
            continue
        typo_subs.append(row)
        for found, suggested in typos:
            typo_counter[(found, suggested)] += 1
            if len(examples[(found, suggested)]) < 5:
                examples[(found, suggested)].append(
                    (int(row["id"]), int(row["owner_id"]), template[:140])
                )

    owners = {int(r["owner_id"]) for r in typo_subs}
    end_owners = {int(r["owner_id"]) for r in typo_subs if r.get("notify_on_end")}
    enabled_owners = {int(r["owner_id"]) for r in typo_subs if r.get("enabled")}

    print(f"Total subscriptions scanned: {len(rows)}")
    print(f"Subscriptions with typos: {len(typo_subs)}")
    print(f"Unique owners with typos: {len(owners)}")
    print(f"  of which enabled: {len(enabled_owners)}")
    print(f"  of which end-alert: {len(end_owners)}")
    if typo_counter:
        print("\nTop patterns:")
        for (found, suggested), count in typo_counter.most_common(20):
            print(f"  {found!r} -> {suggested!r}: {count}")
            for sub_id, owner_id, snippet in examples[(found, suggested)]:
                print(f"    sub={sub_id} owner={owner_id}: {snippet!r}")


if __name__ == "__main__":
    main()
