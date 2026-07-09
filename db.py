from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Protocol
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

logger = logging.getLogger(__name__)


@dataclass
class BotStats:
    users: int
    notify_users: int
    subscriptions_total: int
    subscriptions_enabled: int
    subscriptions_disabled: int
    unique_owners: int
    unique_twitch_channels: int
    locale_en: int
    locale_ru: int
    locale_unset: int


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
    disable_link_preview: bool
    last_message_id: int | None


def _row_to_sub(row: Any) -> Subscription:
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
        disable_link_preview=bool(row["disable_link_preview"]),
        last_message_id=row["last_message_id"],
    )


def _normalize_pg_url(database_url: str) -> str:
    url = database_url.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("sslmode", "require")
    return urlunparse(parsed._replace(query=urlencode(query)))


class Database(Protocol):
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
        disable_link_preview: bool = False,
    ) -> int: ...

    def set_last_message_id(self, sub_id: int, message_id: int) -> None: ...

    def get_subscriptions_by_owner(self, owner_id: int) -> list[Subscription]: ...

    def get_subscription(self, sub_id: int, owner_id: int) -> Subscription | None: ...

    def toggle_subscription(self, sub_id: int, owner_id: int) -> bool | None: ...

    def delete_subscription(self, sub_id: int, owner_id: int) -> bool: ...

    def update_subscription(self, sub_id: int, owner_id: int, **fields: object) -> bool: ...

    def get_user_locale(self, user_id: int) -> str | None: ...

    def set_user_locale(self, user_id: int, locale: str) -> None: ...

    def get_unique_twitch_user_ids(self) -> list[str]: ...

    def get_enabled_by_twitch_user_id(self, twitch_user_id: str) -> list[Subscription]: ...

    def get_all_owner_ids(self) -> list[int]: ...

    def is_status_seen(self, guid: str) -> bool: ...

    def mark_status_seen(self, guid: str) -> None: ...

    def upsert_user(self, user_id: int) -> None: ...

    def get_notify_user_ids(self) -> list[int]: ...

    def get_bot_stats(self) -> BotStats: ...


class SqliteDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        logger.info("Database: SQLite %s", self.path)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA busy_timeout=5000")
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
        if "disable_link_preview" not in cols:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN disable_link_preview INTEGER NOT NULL DEFAULT 0"
            )
        user_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        if "locale" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN locale TEXT")

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
        disable_link_preview: bool = False,
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO subscriptions (
                    owner_id, twitch_username, twitch_user_id,
                    message_template, dest_type, chat_id, thread_id,
                    delete_previous, disable_link_preview
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    int(disable_link_preview),
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
        return [_row_to_sub(r) for r in rows]

    def get_subscription(self, sub_id: int, owner_id: int) -> Subscription | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM subscriptions WHERE id = ? AND owner_id = ?",
                (sub_id, owner_id),
            ).fetchone()
        return _row_to_sub(row) if row else None

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

    def update_subscription(self, sub_id: int, owner_id: int, **fields: object) -> bool:
        allowed = {
            "message_template",
            "dest_type",
            "chat_id",
            "thread_id",
            "delete_previous",
            "disable_link_preview",
        }
        updates: list[str] = []
        values: list[object] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            updates.append(f"{key} = ?")
            if key in ("delete_previous", "disable_link_preview"):
                values.append(int(bool(value)))
            else:
                values.append(value)
        if not updates:
            return self.get_subscription(sub_id, owner_id) is not None
        values.extend([sub_id, owner_id])
        with self._conn() as conn:
            cur = conn.execute(
                f"UPDATE subscriptions SET {', '.join(updates)} "
                "WHERE id = ? AND owner_id = ?",
                values,
            )
        return cur.rowcount > 0

    def get_user_locale(self, user_id: int) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT locale FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        if not row or not row["locale"]:
            return None
        return str(row["locale"])

    def set_user_locale(self, user_id: int, locale: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO users (user_id, locale) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET locale = excluded.locale",
                (user_id, locale),
            )

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
        return [_row_to_sub(r) for r in rows]

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

    def get_bot_stats(self) -> BotStats:
        with self._conn() as conn:
            users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
            notify = conn.execute(
                """
                SELECT COUNT(*) AS c FROM (
                    SELECT user_id FROM users
                    UNION
                    SELECT DISTINCT owner_id FROM subscriptions
                )
                """
            ).fetchone()["c"]
            subs_total = conn.execute(
                "SELECT COUNT(*) AS c FROM subscriptions"
            ).fetchone()["c"]
            subs_enabled = conn.execute(
                "SELECT COUNT(*) AS c FROM subscriptions WHERE enabled = 1"
            ).fetchone()["c"]
            unique_owners = conn.execute(
                "SELECT COUNT(DISTINCT owner_id) AS c FROM subscriptions"
            ).fetchone()["c"]
            unique_twitch = conn.execute(
                "SELECT COUNT(DISTINCT twitch_user_id) AS c FROM subscriptions"
            ).fetchone()["c"]
            locale_en = conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE locale = 'en'"
            ).fetchone()["c"]
            locale_ru = conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE locale = 'ru'"
            ).fetchone()["c"]
            locale_unset = conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE locale IS NULL OR locale = ''"
            ).fetchone()["c"]
        return BotStats(
            users=int(users),
            notify_users=int(notify),
            subscriptions_total=int(subs_total),
            subscriptions_enabled=int(subs_enabled),
            subscriptions_disabled=int(subs_total) - int(subs_enabled),
            unique_owners=int(unique_owners),
            unique_twitch_channels=int(unique_twitch),
            locale_en=int(locale_en),
            locale_ru=int(locale_ru),
            locale_unset=int(locale_unset),
        )


class PostgresDatabase:
    def __init__(self, database_url: str) -> None:
        import psycopg  # noqa: PLC0415

        self._psycopg = psycopg
        self._dsn = _normalize_pg_url(database_url)
        self._init_schema()
        logger.info("Database: PostgreSQL (DATABASE_URL)")

    @contextmanager
    def _conn(self) -> Iterator[Any]:
        with self._psycopg.connect(self._dsn, connect_timeout=30) as conn:
            yield conn

    def _cursor(self, conn: Any) -> Any:
        from psycopg.rows import dict_row  # noqa: PLC0415

        return conn.cursor(row_factory=dict_row)

    def _init_schema(self) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id SERIAL PRIMARY KEY,
                    owner_id BIGINT NOT NULL,
                    twitch_username TEXT NOT NULL,
                    twitch_user_id TEXT NOT NULL,
                    message_template TEXT NOT NULL,
                    dest_type TEXT NOT NULL,
                    chat_id BIGINT NOT NULL,
                    thread_id BIGINT,
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    delete_previous BOOLEAN NOT NULL DEFAULT FALSE,
                    disable_link_preview BOOLEAN NOT NULL DEFAULT FALSE,
                    last_message_id BIGINT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_subs_twitch_user_id
                ON subscriptions(twitch_user_id)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_subs_owner_id
                ON subscriptions(owner_id)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS render_status_seen (
                    guid TEXT PRIMARY KEY,
                    seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    locale TEXT,
                    first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
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
        disable_link_preview: bool = False,
    ) -> int:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO subscriptions (
                    owner_id, twitch_username, twitch_user_id,
                    message_template, dest_type, chat_id, thread_id,
                    delete_previous, disable_link_preview
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    owner_id,
                    twitch_username.lower(),
                    twitch_user_id,
                    message_template,
                    dest_type,
                    chat_id,
                    thread_id,
                    delete_previous,
                    disable_link_preview,
                ),
            )
            row = cur.fetchone()
            return int(row["id"])

    def set_last_message_id(self, sub_id: int, message_id: int) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "UPDATE subscriptions SET last_message_id = %s WHERE id = %s",
                (message_id, sub_id),
            )

    def get_subscriptions_by_owner(self, owner_id: int) -> list[Subscription]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT * FROM subscriptions WHERE owner_id = %s ORDER BY id",
                (owner_id,),
            )
            rows = cur.fetchall()
        return [_row_to_sub(r) for r in rows]

    def get_subscription(self, sub_id: int, owner_id: int) -> Subscription | None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT * FROM subscriptions WHERE id = %s AND owner_id = %s",
                (sub_id, owner_id),
            )
            row = cur.fetchone()
        return _row_to_sub(row) if row else None

    def toggle_subscription(self, sub_id: int, owner_id: int) -> bool | None:
        sub = self.get_subscription(sub_id, owner_id)
        if not sub:
            return None
        new_state = not sub.enabled
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "UPDATE subscriptions SET enabled = %s WHERE id = %s AND owner_id = %s",
                (new_state, sub_id, owner_id),
            )
        return new_state

    def delete_subscription(self, sub_id: int, owner_id: int) -> bool:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "DELETE FROM subscriptions WHERE id = %s AND owner_id = %s",
                (sub_id, owner_id),
            )
            deleted = cur.rowcount > 0
        return deleted

    def update_subscription(self, sub_id: int, owner_id: int, **fields: object) -> bool:
        allowed = {
            "message_template",
            "dest_type",
            "chat_id",
            "thread_id",
            "delete_previous",
            "disable_link_preview",
        }
        updates: list[str] = []
        values: list[object] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            updates.append(f"{key} = %s")
            if key in ("delete_previous", "disable_link_preview"):
                values.append(bool(value))
            else:
                values.append(value)
        if not updates:
            return self.get_subscription(sub_id, owner_id) is not None
        values.extend([sub_id, owner_id])
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                f"UPDATE subscriptions SET {', '.join(updates)} "
                "WHERE id = %s AND owner_id = %s",
                values,
            )
            updated = cur.rowcount > 0
        return updated

    def get_user_locale(self, user_id: int) -> str | None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute("SELECT locale FROM users WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
        if not row or not row["locale"]:
            return None
        return str(row["locale"])

    def set_user_locale(self, user_id: int, locale: str) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO users (user_id, locale) VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET locale = EXCLUDED.locale
                """,
                (user_id, locale),
            )

    def get_unique_twitch_user_ids(self) -> list[str]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT DISTINCT twitch_user_id
                FROM subscriptions
                WHERE enabled = TRUE
                """
            )
            rows = cur.fetchall()
        return [r["twitch_user_id"] for r in rows]

    def get_enabled_by_twitch_user_id(self, twitch_user_id: str) -> list[Subscription]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT * FROM subscriptions
                WHERE twitch_user_id = %s AND enabled = TRUE
                ORDER BY id
                """,
                (twitch_user_id,),
            )
            rows = cur.fetchall()
        return [_row_to_sub(r) for r in rows]

    def get_all_owner_ids(self) -> list[int]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT DISTINCT owner_id FROM subscriptions ORDER BY owner_id"
            )
            rows = cur.fetchall()
        return [int(r["owner_id"]) for r in rows]

    def is_status_seen(self, guid: str) -> bool:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT 1 FROM render_status_seen WHERE guid = %s", (guid,)
            )
            row = cur.fetchone()
        return row is not None

    def mark_status_seen(self, guid: str) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO render_status_seen (guid) VALUES (%s)
                ON CONFLICT DO NOTHING
                """,
                (guid,),
            )

    def upsert_user(self, user_id: int) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO users (user_id) VALUES (%s)
                ON CONFLICT DO NOTHING
                """,
                (user_id,),
            )

    def get_notify_user_ids(self) -> list[int]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT user_id FROM users
                UNION
                SELECT DISTINCT owner_id FROM subscriptions
                """
            )
            rows = cur.fetchall()
        return [int(r["user_id"]) for r in rows]

    def get_bot_stats(self) -> BotStats:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute("SELECT COUNT(*) AS c FROM users")
            users = int(cur.fetchone()["c"])
            cur.execute(
                """
                SELECT COUNT(*) AS c FROM (
                    SELECT user_id FROM users
                    UNION
                    SELECT DISTINCT owner_id FROM subscriptions
                ) AS u
                """
            )
            notify = int(cur.fetchone()["c"])
            cur.execute("SELECT COUNT(*) AS c FROM subscriptions")
            subs_total = int(cur.fetchone()["c"])
            cur.execute(
                "SELECT COUNT(*) AS c FROM subscriptions WHERE enabled = TRUE"
            )
            subs_enabled = int(cur.fetchone()["c"])
            cur.execute(
                "SELECT COUNT(DISTINCT owner_id) AS c FROM subscriptions"
            )
            unique_owners = int(cur.fetchone()["c"])
            cur.execute(
                "SELECT COUNT(DISTINCT twitch_user_id) AS c FROM subscriptions"
            )
            unique_twitch = int(cur.fetchone()["c"])
            cur.execute("SELECT COUNT(*) AS c FROM users WHERE locale = 'en'")
            locale_en = int(cur.fetchone()["c"])
            cur.execute("SELECT COUNT(*) AS c FROM users WHERE locale = 'ru'")
            locale_ru = int(cur.fetchone()["c"])
            cur.execute(
                "SELECT COUNT(*) AS c FROM users WHERE locale IS NULL OR locale = ''"
            )
            locale_unset = int(cur.fetchone()["c"])
        return BotStats(
            users=users,
            notify_users=notify,
            subscriptions_total=subs_total,
            subscriptions_enabled=subs_enabled,
            subscriptions_disabled=subs_total - subs_enabled,
            unique_owners=unique_owners,
            unique_twitch_channels=unique_twitch,
            locale_en=locale_en,
            locale_ru=locale_ru,
            locale_unset=locale_unset,
        )


def open_database(path: Path, database_url: str | None = None) -> Database:
    if database_url:
        return PostgresDatabase(database_url)
    return SqliteDatabase(path)
