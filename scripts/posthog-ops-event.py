#!/usr/bin/env python3
"""Emit ops_job_* PostHog events from host cron (pg-backup / Aiven DR).

Designed to run inside the bot container (preferred) or on the host with
APP_DIR/.env. Never prints secrets. Exit 0 even if PostHog is disabled/unreachable
so reporting never masks the original job exit code when used from a trap.

Usage:
  python scripts/posthog-ops-event.py --job pg_backup --status failed \\
      --reason "pg_dump failed" --exit-code 1
  python scripts/posthog-ops-event.py --job pg_sync_aiven --status skipped \\
      --reason "AIVEN_DATABASE_URL unset"
  python scripts/posthog-ops-event.py --job pg_backup --status ok
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_env_file(path: Path) -> None:
    """Set missing keys from a KEY=value .env (no shell source)."""
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        val = val.strip().strip("'").strip('"')
        os.environ[key] = val


def _capture_http(
    *,
    api_key: str,
    host: str,
    event: str,
    properties: dict,
) -> None:
    payload = {
        "api_key": api_key,
        "event": event,
        "distinct_id": "bot_system",
        "properties": {
            **properties,
            "$process_person_profile": False,
        },
    }
    url = f"{host.rstrip('/')}/capture/"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True, help="ops job id, e.g. pg_backup")
    parser.add_argument(
        "--status",
        required=True,
        choices=("ok", "failed", "skipped"),
    )
    parser.add_argument("--reason", default="", help="short machine-readable reason")
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument(
        "--env-file",
        default="",
        help="optional .env path when POSTHOG_* not already in the environment",
    )
    args = parser.parse_args()

    env_file = Path(args.env_file) if args.env_file else ROOT / ".env"
    _load_env_file(env_file)

    props = {
        "job": args.job,
        "status": args.status,
        "reason": (args.reason or "")[:500],
        "exit_code": int(args.exit_code),
        "source": "host_cron",
    }
    event = f"ops_job_{args.status}"

    # Prefer in-process analytics (redaction, version, flush) when deps exist.
    try:
        from analytics import capture, capture_exception, init_analytics, is_enabled, shutdown_analytics

        init_analytics()
        if is_enabled():
            capture(None, event, props)
            if args.status == "failed":
                capture_exception(
                    RuntimeError(
                        f"ops job failed: {args.job}"
                        + (f" ({args.reason})" if args.reason else "")
                    ),
                    properties={**props, "ops_job": args.job},
                )
            shutdown_analytics()
            return 0
        shutdown_analytics()
    except Exception as exc:
        print(f"posthog-ops-event: analytics path failed: {type(exc).__name__}", file=sys.stderr)

    api_key = (os.environ.get("POSTHOG_API_KEY") or "").strip()
    if not api_key:
        print("posthog-ops-event: POSTHOG_API_KEY unset; skip", file=sys.stderr)
        return 0
    host = (os.environ.get("POSTHOG_HOST") or "https://us.i.posthog.com").strip()
    try:
        _capture_http(api_key=api_key, host=host, event=event, properties=props)
        if args.status == "failed":
            # Minimal $exception so Error Tracking can group cron failures.
            _capture_http(
                api_key=api_key,
                host=host,
                event="$exception",
                properties={
                    **props,
                    "$exception_list": [
                        {
                            "type": "OpsJobFailed",
                            "value": f"ops job failed: {args.job}"
                            + (f" ({args.reason})" if args.reason else ""),
                        }
                    ],
                },
            )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"posthog-ops-event: capture failed: {type(exc).__name__}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
