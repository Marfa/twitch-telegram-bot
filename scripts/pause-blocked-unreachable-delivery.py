#!/usr/bin/env python3
"""One-shot: pause enabled alerts to blocked users / unreachable chats."""
from __future__ import annotations

from config import DATABASE_PATH, DATABASE_URL
from db import open_database


def main() -> None:
    db = open_database(DATABASE_PATH, DATABASE_URL)
    blocked_users = 0
    paused_from_blocked = 0
    unreachable_chats = 0
    paused_from_chats = 0

    # Blocked Telegram users: pause DM destinations (chat_id == user_id).
    if DATABASE_URL:
        import psycopg

        url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        with psycopg.connect(url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_id FROM users WHERE COALESCE(bot_blocked, FALSE) = TRUE"
                )
                blocked_ids = [int(r[0]) for r in cur.fetchall()]
                cur.execute("SELECT chat_id FROM unreachable_chats")
                chat_ids = [int(r[0]) for r in cur.fetchall()]
    else:
        import sqlite3

        conn = sqlite3.connect(DATABASE_PATH)
        blocked_ids = [
            int(r[0])
            for r in conn.execute(
                "SELECT user_id FROM users WHERE COALESCE(bot_blocked, 0) = 1"
            )
        ]
        try:
            chat_ids = [
                int(r[0]) for r in conn.execute("SELECT chat_id FROM unreachable_chats")
            ]
        except sqlite3.OperationalError:
            chat_ids = []
        conn.close()

    for uid in blocked_ids:
        blocked_users += 1
        paused_from_blocked += db.pause_delivery_for_chat(uid)

    for cid in chat_ids:
        unreachable_chats += 1
        # Also ensure flag stays set (idempotent).
        db.set_chat_unreachable(cid, True)
        paused_from_chats += db.pause_delivery_for_chat(cid)

    print(
        "pause_blocked_unreachable: "
        f"blocked_users={blocked_users} paused_dm={paused_from_blocked} "
        f"unreachable_chats={unreachable_chats} paused_chat={paused_from_chats}"
    )


if __name__ == "__main__":
    main()
