#!/usr/bin/env python3
"""Probe stored Twitch user refresh tokens (read-only; no DB writes)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import DATABASE_PATH, DATABASE_URL  # noqa: E402
from db import open_database  # noqa: E402
from twitch import TwitchClient  # noqa: E402


async def _probe_refresh(twitch: TwitchClient, refresh: str) -> str:
    try:
        await asyncio.to_thread(twitch.refresh_user_token, refresh)
        return "ok"
    except Exception as exc:
        return f"FAIL:{type(exc).__name__}"


async def main() -> None:
    db = open_database(DATABASE_PATH, DATABASE_URL)
    twitch = TwitchClient()
    rows: list[tuple[str, int, str, str]] = []

    for owner_id in db.get_all_owner_ids():
        sync = db.get_twitch_sync(owner_id)
        if sync and sync.refresh_token:
            status = await _probe_refresh(twitch, sync.refresh_token)
            rows.append(
                ("twitch_sync", owner_id, f"period={sync.period_days}", status)
            )

        whisper = db.get_whisper_alert(owner_id)
        if whisper and whisper.refresh_token:
            status = await _probe_refresh(twitch, whisper.refresh_token)
            rows.append(
                (
                    "whisper",
                    owner_id,
                    f"enabled={bool(whisper.enabled)}",
                    status,
                )
            )

        chat = db.get_chat_auth(owner_id)
        if chat and chat.refresh_token:
            status = await _probe_refresh(twitch, chat.refresh_token)
            rows.append(("chat", owner_id, "", status))

    for uid in db.list_premium_twitch_user_ids():
        refresh = db.get_premium_twitch_refresh(uid)
        if not refresh:
            continue
        status = await _probe_refresh(twitch, refresh)
        rows.append(("premium_twitch", uid, "", status))

    fail = [r for r in rows if r[3].startswith("FAIL")]
    print(f"probed={len(rows)} failed={len(fail)}")
    for kind, uid, meta, status in rows:
        if status.startswith("FAIL") or kind != "chat":
            print(f"{kind}\t{uid}\t{meta}\t{status}")
    chat_fail = sum(1 for r in rows if r[0] == "chat" and r[3].startswith("FAIL"))
    chat_ok = sum(1 for r in rows if r[0] == "chat" and r[3] == "ok")
    print(f"chat_summary ok={chat_ok} fail={chat_fail}")


if __name__ == "__main__":
    asyncio.run(main())
