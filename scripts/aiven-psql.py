#!/usr/bin/env python3
"""Minimal psql-like runner for Aiven DR (used by scripts/vps-psql.sh).

Reads AIVEN_DATABASE_URL from the environment only — never prints it.
Supports the flags agents use most: -c QUERY, -A, -t (and -At).
Extra unknown flags are ignored with a warning on stderr.
"""
from __future__ import annotations

import os
import sys
from typing import Any


def _connect(url: str) -> Any:
    url = url.replace("postgres://", "postgresql://", 1)
    try:
        import psycopg

        return psycopg.connect(url, connect_timeout=30)
    except ImportError:
        import psycopg2

        return psycopg2.connect(url, connect_timeout=30)


def _parse_args(argv: list[str]) -> tuple[str | None, bool, bool]:
    query: str | None = None
    align = True
    tuples_only = False
    i = 0
    unknown: list[str] = []
    while i < len(argv):
        arg = argv[i]
        if arg in ("-c", "--command") and i + 1 < len(argv):
            query = argv[i + 1]
            i += 2
            continue
        if arg == "-At":
            align = False
            tuples_only = True
            i += 1
            continue
        if arg == "-A":
            align = False
            i += 1
            continue
        if arg == "-t":
            tuples_only = True
            i += 1
            continue
        if arg in ("-v", "--variable") and i + 1 < len(argv):
            # ignore ON_ERROR_STOP-style vars
            i += 2
            continue
        unknown.append(arg)
        i += 1
    if unknown:
        print(
            f"warning: aiven-psql.py ignoring unsupported args: {' '.join(unknown)}",
            file=sys.stderr,
        )
    return query, align, tuples_only


def _print_rows(rows: list[tuple[Any, ...]], colnames: list[str], *, align: bool, tuples_only: bool) -> None:
    if not align:
        sep = "|"
        for row in rows:
            print(sep.join("" if v is None else str(v) for v in row))
        if not tuples_only and colnames:
            print(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")
        return
    # Simple aligned table (good enough for agent reads).
    str_rows = [["" if v is None else str(v) for v in row] for row in rows]
    widths = [len(c) for c in colnames]
    for row in str_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    if not tuples_only:
        header = " | ".join(c.ljust(widths[i]) for i, c in enumerate(colnames))
        rule = "-+-".join("-" * widths[i] for i in range(len(colnames)))
        print(header)
        print(rule)
    for row in str_rows:
        print(" | ".join(row[i].ljust(widths[i]) for i in range(len(row))))
    if not tuples_only:
        print(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")


def main() -> int:
    # Windows consoles often default to cp125x; templates may include emoji.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    url = (os.environ.get("AIVEN_DATABASE_URL") or "").strip()
    if not url:
        print("error: AIVEN_DATABASE_URL unset", file=sys.stderr)
        return 1
    query, align, tuples_only = _parse_args(sys.argv[1:])
    if query is None:
        query = sys.stdin.read()
    query = (query or "").strip()
    if not query:
        print("error: empty query", file=sys.stderr)
        return 1
    try:
        with _connect(url) as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                if cur.description is None:
                    conn.commit()
                    print(f"OK ({cur.rowcount} rows)" if cur.rowcount >= 0 else "OK")
                    return 0
                rows = cur.fetchall()
                colnames = [d[0] for d in cur.description]
    except Exception as exc:  # noqa: BLE001 — surface driver errors to the agent
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _print_rows(rows, colnames, align=align, tuples_only=tuples_only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
