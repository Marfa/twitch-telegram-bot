from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass
class Subscription:
    id: int
    owner_id: int
    twitch_username: str
    twitch_user_id: str
    message_template: str
    dest_type: str
    chat_id: int
    thread_id: int | None
    enabled: bool
    delete_previous: bool
    last_message_id: int | None


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL,
                    twitch_username TEXT NOT NULL,
                    twitch_user_id TEXT NOT NULL,
                    message_template TEXT NOT NULL,
                    dest_type TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_subs_twitch_user_id
                ON subscriptions(twitch_user_id)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_subs_owner_id
                ON subscriptions(owner_id)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS render_status_seen (
                    guid TEXT PRIMARY KEY,
                    seen_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    first_seen TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(subscriptions)")}
        if "delete_previous" not in cols:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN delete_previous INTEGER NOT NULL DEFAULT 0"
            )
        if "last_message_id" not in cols:
            conn.execute("ALTER TABLE subscriptions ADD COLUMN last_message_id INTEGER")

    def _row_to_sub(self, row: sqlite3.Row) -> Subscription:
        return Subscription(
            id=row["id"],
            owner_id=row["owner_id"],
            twitch_username=row["twitch_username"],
            twitch_user_id=row["twitch_user_id"],
            message_template=row["message_template"],
            dest_type=row["dest_type"],
            chat_id=row["chat_id"],
            thread_id=row["thread_id"],
            enabled=bool(row["enabled"]),
            delete_previous=bool(row["delete_previous"]),
            last_message_id=row["last_message_id"],
        )

    def add_subscription(
        self,
        owner_id: int,
        twitch_username: str,
        twitch_user_id: str,
        message_template: str,
        dest_type: str,
        chat_id: int,
        thread_id: int | None,
        delete_previous: bool = False,
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO subscriptions (
                    owner_id, twitch_username, twitch_user_id,
                    message_template, dest_type, chat_id, thread_id,
                    delete_previous
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    owner_id,
                    twitch_username.lower(),
                    twitch_user_id,
                    message_template,
                    dest_type,
                    chat_id,
                    thread_id,
                    int(delete_previous),
                ),
            )
            return int(cur.lastrowid)

    def set_last_message_id(self, sub_id: int, message_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE subscriptions SET last_message_id = ? WHERE id = ?",
                (message_id, sub_id),
            )

    def get_subscriptions_by_owner(self, owner_id: int) -> list[Subscription]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM subscriptions WHERE owner_id = ? ORDER BY id",
                (owner_id,),
            ).fetchall()
        return [self._row_to_sub(r) for r in rows]

    def get_subscription(self, sub_id: int, owner_id: int) -> Subscription | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM subscriptions WHERE id = ? AND owner_id = ?",
                (sub_id, owner_id),
            ).fetchone()
        return self._row_to_sub(row) if row else None

    def toggle_subscription(self, sub_id: int, owner_id: int) -> bool | None:
        sub = self.get_subscription(sub_id, owner_id)
        if not sub:
            return None
        new_state = 0 if sub.enabled else 1
        with self._conn() as conn:
            conn.execute(
                "UPDATE subscriptions SET enabled = ? WHERE id = ? AND owner_id = ?",
                (new_state, sub_id, owner_id),
            )
        return bool(new_state)

    def delete_subscription(self, sub_id: int, owner_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM subscriptions WHERE id = ? AND owner_id = ?",
                (sub_id, owner_id),
            )
        return cur.rowcount > 0

    def get_unique_twitch_user_ids(self) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT twitch_user_id
                FROM subscriptions
                WHERE enabled = 1
                """
            ).fetchall()
        return [r["twitch_user_id"] for r in rows]

    def get_enabled_by_twitch_user_id(self, twitch_user_id: str) -> list[Subscription]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM subscriptions
                WHERE twitch_user_id = ? AND enabled = 1
                ORDER BY id
                """,
                (twitch_user_id,),
            ).fetchall()
        return [self._row_to_sub(r) for r in rows]

    def get_all_owner_ids(self) -> list[int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT owner_id FROM subscriptions ORDER BY owner_id"
            ).fetchall()
        return [int(r["owner_id"]) for r in rows]

    def is_status_seen(self, guid: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM render_status_seen WHERE guid = ?", (guid,)
            ).fetchone()
        return row is not None

    def mark_status_seen(self, guid: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO render_status_seen (guid) VALUES (?)",
                (guid,),
            )

    def upsert_user(self, user_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
                (user_id,),
            )

    def get_notify_user_ids(self) -> list[int]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT user_id FROM users
                UNION
                SELECT DISTINCT owner_id FROM subscriptions
                """
            ).fetchall()
        return [int(r["user_id"]) for r in rows]
