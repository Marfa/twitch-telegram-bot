from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
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
    sys_updates: int
    sys_availability: int
    blocked_users: int
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
    notify_delete_fail: bool
    disable_link_preview: bool
    delay_minutes: int
    suppress_repeat_minutes: int
    ignore_keywords: str
    notify_cooldown_until: str | None
    last_message_id: int | None


@dataclass
class ScheduledBroadcast:
    id: int
    msg_type: str
    text: str
    scheduled_at: str
    created_by: int


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
        notify_delete_fail=bool(row["notify_delete_fail"]),
        disable_link_preview=bool(row["disable_link_preview"]),
        delay_minutes=int(row["delay_minutes"] or 0),
        suppress_repeat_minutes=int(row["suppress_repeat_minutes"] or 0),
        ignore_keywords=str(row["ignore_keywords"] or ""),
        notify_cooldown_until=(
            row["notify_cooldown_until"].isoformat()
            if row["notify_cooldown_until"] is not None
            and not isinstance(row["notify_cooldown_until"], str)
            else row["notify_cooldown_until"]
        ),
        last_message_id=row["last_message_id"],
    )


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def is_on_notify_cooldown(sub: Subscription) -> bool:
    if sub.suppress_repeat_minutes <= 0:
        return False
    until = _parse_utc(sub.notify_cooldown_until)
    if until is None:
        return False
    return datetime.now(timezone.utc) < until


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
        notify_delete_fail: bool = False,
        disable_link_preview: bool = False,
        delay_minutes: int = 0,
        suppress_repeat_minutes: int = 0,
        ignore_keywords: str = "",
    ) -> int: ...

    def set_last_message_id(self, sub_id: int, message_id: int) -> None: ...

    def set_notify_cooldown(self, sub_id: int, minutes: int) -> None: ...

    def get_subscription_by_id(self, sub_id: int) -> Subscription | None: ...

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

    def count_new_users_since(self, since: datetime) -> int: ...

    def set_bot_blocked(self, user_id: int, blocked: bool) -> None: ...

    def is_bot_blocked(self, user_id: int) -> bool: ...

    def get_notify_user_ids(self) -> list[int]: ...

    def get_bot_update_recipients(self) -> list[int]: ...

    def get_availability_recipients(self) -> list[int]: ...

    def get_receive_bot_updates(self, user_id: int) -> bool: ...

    def set_receive_bot_updates(self, user_id: int, enabled: bool) -> None: ...

    def get_receive_availability_updates(self, user_id: int) -> bool: ...

    def set_receive_availability_updates(self, user_id: int, enabled: bool) -> None: ...

    def get_saved_schedule(self, user_id: int) -> tuple[int | None, int | None]: ...

    def set_saved_schedule(self, user_id: int, hour: int, minute: int) -> None: ...

    def add_scheduled_broadcast(
        self, msg_type: str, text: str, scheduled_at: str, created_by: int
    ) -> int: ...

    def get_pending_scheduled_broadcasts(self) -> list[ScheduledBroadcast]: ...

    def get_unsent_scheduled_broadcasts(self) -> list[ScheduledBroadcast]: ...

    def mark_scheduled_broadcast_sent(self, broadcast_id: int) -> None: ...

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
        if "delay_minutes" not in cols:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN delay_minutes INTEGER NOT NULL DEFAULT 0"
            )
        if "suppress_repeat_minutes" not in cols:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN suppress_repeat_minutes INTEGER NOT NULL DEFAULT 0"
            )
        if "notify_cooldown_until" not in cols:
            conn.execute("ALTER TABLE subscriptions ADD COLUMN notify_cooldown_until TEXT")
        if "notify_delete_fail" not in cols:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN notify_delete_fail INTEGER NOT NULL DEFAULT 0"
            )
        if "ignore_keywords" not in cols:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN ignore_keywords TEXT NOT NULL DEFAULT ''"
            )
        user_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        if "locale" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN locale TEXT")
        if "receive_bot_updates" not in user_cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN receive_bot_updates INTEGER NOT NULL DEFAULT 1"
            )
        if "receive_availability_updates" not in user_cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN receive_availability_updates INTEGER NOT NULL DEFAULT 1"
            )
        if "bot_blocked" not in user_cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN bot_blocked INTEGER NOT NULL DEFAULT 0"
            )
        if "saved_schedule_hour" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN saved_schedule_hour INTEGER")
        if "saved_schedule_minute" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN saved_schedule_minute INTEGER")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduled_broadcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_type TEXT NOT NULL,
                text TEXT NOT NULL,
                scheduled_at TEXT NOT NULL,
                sent_at TEXT,
                created_by INTEGER NOT NULL
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
        notify_delete_fail: bool = False,
        disable_link_preview: bool = False,
        delay_minutes: int = 0,
        suppress_repeat_minutes: int = 0,
        ignore_keywords: str = "",
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO subscriptions (
                    owner_id, twitch_username, twitch_user_id,
                    message_template, dest_type, chat_id, thread_id,
                    delete_previous, notify_delete_fail, disable_link_preview,
                    delay_minutes, suppress_repeat_minutes, ignore_keywords
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    int(notify_delete_fail),
                    int(disable_link_preview),
                    max(0, int(delay_minutes)),
                    max(0, int(suppress_repeat_minutes)),
                    ignore_keywords,
                ),
            )
            return int(cur.lastrowid)

    def get_subscription_by_id(self, sub_id: int) -> Subscription | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM subscriptions WHERE id = ?",
                (sub_id,),
            ).fetchone()
        return _row_to_sub(row) if row else None

    def set_last_message_id(self, sub_id: int, message_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE subscriptions SET last_message_id = ? WHERE id = ?",
                (message_id, sub_id),
            )

    def set_notify_cooldown(self, sub_id: int, minutes: int) -> None:
        if minutes <= 0:
            return
        until = datetime.now(timezone.utc).timestamp() + minutes * 60
        until_iso = datetime.fromtimestamp(until, tz=timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                "UPDATE subscriptions SET notify_cooldown_until = ? WHERE id = ?",
                (until_iso, sub_id),
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
            "notify_delete_fail",
            "disable_link_preview",
            "delay_minutes",
            "suppress_repeat_minutes",
            "ignore_keywords",
        }
        updates: list[str] = []
        values: list[object] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            updates.append(f"{key} = ?")
            if key in ("delete_previous", "notify_delete_fail", "disable_link_preview"):
                values.append(int(bool(value)))
            elif key in ("delay_minutes", "suppress_repeat_minutes"):
                values.append(max(0, int(value)))
            elif key == "ignore_keywords":
                values.append(str(value or ""))
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
                """
                INSERT INTO users (user_id, bot_blocked) VALUES (?, 0)
                ON CONFLICT(user_id) DO UPDATE SET bot_blocked = 0
                """,
                (user_id,),
            )

    def count_new_users_since(self, since: datetime) -> int:
        since_utc = since.astimezone(timezone.utc) if since.tzinfo else since.replace(tzinfo=timezone.utc)
        since_s = since_utc.strftime("%Y-%m-%d %H:%M:%S")
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE first_seen >= ?",
                (since_s,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def set_bot_blocked(self, user_id: int, blocked: bool) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, bot_blocked) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET bot_blocked = excluded.bot_blocked
                """,
                (user_id, int(blocked)),
            )

    def is_bot_blocked(self, user_id: int) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT bot_blocked FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if not row:
            return False
        return bool(row["bot_blocked"])

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

    def get_bot_update_recipients(self) -> list[int]:
        return [
            uid
            for uid in self.get_notify_user_ids()
            if self.get_receive_bot_updates(uid) and not self.is_bot_blocked(uid)
        ]

    def get_availability_recipients(self) -> list[int]:
        return [
            uid
            for uid in self.get_notify_user_ids()
            if self.get_receive_availability_updates(uid) and not self.is_bot_blocked(uid)
        ]

    def get_receive_bot_updates(self, user_id: int) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT receive_bot_updates FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if not row:
            return True
        return bool(row["receive_bot_updates"])

    def set_receive_bot_updates(self, user_id: int, enabled: bool) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, receive_bot_updates) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET receive_bot_updates = excluded.receive_bot_updates
                """,
                (user_id, int(enabled)),
            )

    def get_receive_availability_updates(self, user_id: int) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT receive_availability_updates FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if not row:
            return True
        return bool(row["receive_availability_updates"])

    def set_receive_availability_updates(self, user_id: int, enabled: bool) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, receive_availability_updates) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    receive_availability_updates = excluded.receive_availability_updates
                """,
                (user_id, int(enabled)),
            )

    def get_saved_schedule(self, user_id: int) -> tuple[int | None, int | None]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT saved_schedule_hour, saved_schedule_minute FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if not row:
            return None, None
        return row["saved_schedule_hour"], row["saved_schedule_minute"]

    def set_saved_schedule(self, user_id: int, hour: int, minute: int) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, saved_schedule_hour, saved_schedule_minute)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    saved_schedule_hour = excluded.saved_schedule_hour,
                    saved_schedule_minute = excluded.saved_schedule_minute
                """,
                (user_id, hour, minute),
            )

    def add_scheduled_broadcast(
        self, msg_type: str, text: str, scheduled_at: str, created_by: int
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO scheduled_broadcasts (msg_type, text, scheduled_at, created_by)
                VALUES (?, ?, ?, ?)
                """,
                (msg_type, text, scheduled_at, created_by),
            )
            return int(cur.lastrowid)

    def get_unsent_scheduled_broadcasts(self) -> list[ScheduledBroadcast]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, msg_type, text, scheduled_at, created_by
                FROM scheduled_broadcasts
                WHERE sent_at IS NULL
                ORDER BY scheduled_at
                """
            ).fetchall()
        return [
            ScheduledBroadcast(
                id=int(r["id"]),
                msg_type=r["msg_type"],
                text=r["text"],
                scheduled_at=r["scheduled_at"],
                created_by=int(r["created_by"]),
            )
            for r in rows
        ]

    def get_pending_scheduled_broadcasts(self) -> list[ScheduledBroadcast]:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, msg_type, text, scheduled_at, created_by
                FROM scheduled_broadcasts
                WHERE sent_at IS NULL AND scheduled_at <= ?
                ORDER BY scheduled_at
                """,
                (now,),
            ).fetchall()
        return [
            ScheduledBroadcast(
                id=int(r["id"]),
                msg_type=r["msg_type"],
                text=r["text"],
                scheduled_at=r["scheduled_at"],
                created_by=int(r["created_by"]),
            )
            for r in rows
        ]

    def mark_scheduled_broadcast_sent(self, broadcast_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                "UPDATE scheduled_broadcasts SET sent_at = ? WHERE id = ?",
                (now, broadcast_id),
            )

    def get_bot_stats(self) -> BotStats:
        with self._conn() as conn:
            users = conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE COALESCE(bot_blocked, 0) = 0"
            ).fetchone()["c"]
            notify = conn.execute(
                """
                SELECT COUNT(*) AS c FROM (
                    SELECT user_id AS id FROM users
                    WHERE COALESCE(bot_blocked, 0) = 0
                    UNION
                    SELECT DISTINCT s.owner_id AS id FROM subscriptions s
                    LEFT JOIN users u ON u.user_id = s.owner_id
                    WHERE COALESCE(u.bot_blocked, 0) = 0
                )
                """
            ).fetchone()["c"]
            subs_total = conn.execute(
                """
                SELECT COUNT(*) AS c FROM subscriptions s
                LEFT JOIN users u ON u.user_id = s.owner_id
                WHERE COALESCE(u.bot_blocked, 0) = 0
                """
            ).fetchone()["c"]
            subs_enabled = conn.execute(
                """
                SELECT COUNT(*) AS c FROM subscriptions s
                LEFT JOIN users u ON u.user_id = s.owner_id
                WHERE s.enabled = 1 AND COALESCE(u.bot_blocked, 0) = 0
                """
            ).fetchone()["c"]
            unique_owners = conn.execute(
                """
                SELECT COUNT(DISTINCT s.owner_id) AS c FROM subscriptions s
                LEFT JOIN users u ON u.user_id = s.owner_id
                WHERE COALESCE(u.bot_blocked, 0) = 0
                """
            ).fetchone()["c"]
            unique_twitch = conn.execute(
                """
                SELECT COUNT(DISTINCT s.twitch_user_id) AS c FROM subscriptions s
                LEFT JOIN users u ON u.user_id = s.owner_id
                WHERE COALESCE(u.bot_blocked, 0) = 0
                """
            ).fetchone()["c"]
            sys_updates = conn.execute(
                """
                SELECT COUNT(*) AS c FROM (
                    SELECT user_id AS id FROM users
                    UNION
                    SELECT DISTINCT owner_id AS id FROM subscriptions
                ) AS n
                LEFT JOIN users u ON u.user_id = n.id
                WHERE COALESCE(u.receive_bot_updates, 1) = 1
                  AND COALESCE(u.bot_blocked, 0) = 0
                """
            ).fetchone()["c"]
            sys_availability = conn.execute(
                """
                SELECT COUNT(*) AS c FROM (
                    SELECT user_id AS id FROM users
                    UNION
                    SELECT DISTINCT owner_id AS id FROM subscriptions
                ) AS n
                LEFT JOIN users u ON u.user_id = n.id
                WHERE COALESCE(u.receive_availability_updates, 1) = 1
                  AND COALESCE(u.bot_blocked, 0) = 0
                """
            ).fetchone()["c"]
            blocked_users = conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE bot_blocked = 1"
            ).fetchone()["c"]
            locale_en = conn.execute(
                """
                SELECT COUNT(*) AS c FROM users
                WHERE locale = 'en' AND COALESCE(bot_blocked, 0) = 0
                """
            ).fetchone()["c"]
            locale_ru = conn.execute(
                """
                SELECT COUNT(*) AS c FROM users
                WHERE locale = 'ru' AND COALESCE(bot_blocked, 0) = 0
                """
            ).fetchone()["c"]
            locale_unset = conn.execute(
                """
                SELECT COUNT(*) AS c FROM users
                WHERE (locale IS NULL OR locale = '')
                  AND COALESCE(bot_blocked, 0) = 0
                """
            ).fetchone()["c"]
        return BotStats(
            users=int(users),
            notify_users=int(notify),
            subscriptions_total=int(subs_total),
            subscriptions_enabled=int(subs_enabled),
            subscriptions_disabled=int(subs_total) - int(subs_enabled),
            unique_owners=int(unique_owners),
            unique_twitch_channels=int(unique_twitch),
            sys_updates=int(sys_updates),
            sys_availability=int(sys_availability),
            blocked_users=int(blocked_users),
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
                    delay_minutes INTEGER NOT NULL DEFAULT 0,
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
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS delay_minutes INTEGER NOT NULL DEFAULT 0
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS suppress_repeat_minutes INTEGER NOT NULL DEFAULT 0
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS notify_cooldown_until TIMESTAMPTZ
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS notify_delete_fail BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS ignore_keywords TEXT NOT NULL DEFAULT ''
                """
            )
            cur.execute(
                """
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS receive_bot_updates BOOLEAN NOT NULL DEFAULT TRUE
                """
            )
            cur.execute(
                """
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS receive_availability_updates BOOLEAN NOT NULL DEFAULT TRUE
                """
            )
            cur.execute(
                """
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS bot_blocked BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            cur.execute(
                """
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS saved_schedule_hour INTEGER
                """
            )
            cur.execute(
                """
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS saved_schedule_minute INTEGER
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduled_broadcasts (
                    id SERIAL PRIMARY KEY,
                    msg_type TEXT NOT NULL,
                    text TEXT NOT NULL,
                    scheduled_at TIMESTAMPTZ NOT NULL,
                    sent_at TIMESTAMPTZ,
                    created_by BIGINT NOT NULL
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
        notify_delete_fail: bool = False,
        disable_link_preview: bool = False,
        delay_minutes: int = 0,
        suppress_repeat_minutes: int = 0,
        ignore_keywords: str = "",
    ) -> int:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO subscriptions (
                    owner_id, twitch_username, twitch_user_id,
                    message_template, dest_type, chat_id, thread_id,
                    delete_previous, notify_delete_fail, disable_link_preview,
                    delay_minutes, suppress_repeat_minutes, ignore_keywords
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                    notify_delete_fail,
                    disable_link_preview,
                    max(0, int(delay_minutes)),
                    max(0, int(suppress_repeat_minutes)),
                    ignore_keywords,
                ),
            )
            row = cur.fetchone()
            return int(row["id"])

    def get_subscription_by_id(self, sub_id: int) -> Subscription | None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT * FROM subscriptions WHERE id = %s",
                (sub_id,),
            )
            row = cur.fetchone()
        return _row_to_sub(row) if row else None

    def set_last_message_id(self, sub_id: int, message_id: int) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "UPDATE subscriptions SET last_message_id = %s WHERE id = %s",
                (message_id, sub_id),
            )

    def set_notify_cooldown(self, sub_id: int, minutes: int) -> None:
        if minutes <= 0:
            return
        until = datetime.now(timezone.utc).timestamp() + minutes * 60
        until_iso = datetime.fromtimestamp(until, tz=timezone.utc)
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "UPDATE subscriptions SET notify_cooldown_until = %s WHERE id = %s",
                (until_iso, sub_id),
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
            "notify_delete_fail",
            "disable_link_preview",
            "delay_minutes",
            "suppress_repeat_minutes",
            "ignore_keywords",
        }
        updates: list[str] = []
        values: list[object] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            updates.append(f"{key} = %s")
            if key in ("delete_previous", "notify_delete_fail", "disable_link_preview"):
                values.append(bool(value))
            elif key in ("delay_minutes", "suppress_repeat_minutes"):
                values.append(max(0, int(value)))
            elif key == "ignore_keywords":
                values.append(str(value or ""))
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
                INSERT INTO users (user_id, bot_blocked) VALUES (%s, FALSE)
                ON CONFLICT (user_id) DO UPDATE SET bot_blocked = FALSE
                """,
                (user_id,),
            )

    def count_new_users_since(self, since: datetime) -> int:
        since_utc = since.astimezone(timezone.utc) if since.tzinfo else since.replace(tzinfo=timezone.utc)
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT COUNT(*) AS n FROM users WHERE first_seen >= %s",
                (since_utc,),
            )
            row = cur.fetchone()
        return int(row["n"]) if row else 0

    def set_bot_blocked(self, user_id: int, blocked: bool) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO users (user_id, bot_blocked) VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET bot_blocked = EXCLUDED.bot_blocked
                """,
                (user_id, bool(blocked)),
            )

    def is_bot_blocked(self, user_id: int) -> bool:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT bot_blocked FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        if not row:
            return False
        return bool(row["bot_blocked"])

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

    def get_bot_update_recipients(self) -> list[int]:
        return [
            uid
            for uid in self.get_notify_user_ids()
            if self.get_receive_bot_updates(uid) and not self.is_bot_blocked(uid)
        ]

    def get_availability_recipients(self) -> list[int]:
        return [
            uid
            for uid in self.get_notify_user_ids()
            if self.get_receive_availability_updates(uid) and not self.is_bot_blocked(uid)
        ]

    def get_receive_bot_updates(self, user_id: int) -> bool:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT receive_bot_updates FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        if not row:
            return True
        return bool(row["receive_bot_updates"])

    def set_receive_bot_updates(self, user_id: int, enabled: bool) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO users (user_id, receive_bot_updates) VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET receive_bot_updates = EXCLUDED.receive_bot_updates
                """,
                (user_id, enabled),
            )

    def get_receive_availability_updates(self, user_id: int) -> bool:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT receive_availability_updates FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        if not row:
            return True
        return bool(row["receive_availability_updates"])

    def set_receive_availability_updates(self, user_id: int, enabled: bool) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO users (user_id, receive_availability_updates) VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    receive_availability_updates = EXCLUDED.receive_availability_updates
                """,
                (user_id, enabled),
            )

    def get_saved_schedule(self, user_id: int) -> tuple[int | None, int | None]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT saved_schedule_hour, saved_schedule_minute FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        if not row:
            return None, None
        return row["saved_schedule_hour"], row["saved_schedule_minute"]

    def set_saved_schedule(self, user_id: int, hour: int, minute: int) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO users (user_id, saved_schedule_hour, saved_schedule_minute)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    saved_schedule_hour = EXCLUDED.saved_schedule_hour,
                    saved_schedule_minute = EXCLUDED.saved_schedule_minute
                """,
                (user_id, hour, minute),
            )

    def add_scheduled_broadcast(
        self, msg_type: str, text: str, scheduled_at: str, created_by: int
    ) -> int:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO scheduled_broadcasts (msg_type, text, scheduled_at, created_by)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (msg_type, text, scheduled_at, created_by),
            )
            row = cur.fetchone()
            return int(row["id"])

    def get_unsent_scheduled_broadcasts(self) -> list[ScheduledBroadcast]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT id, msg_type, text, scheduled_at, created_by
                FROM scheduled_broadcasts
                WHERE sent_at IS NULL
                ORDER BY scheduled_at
                """
            )
            rows = cur.fetchall()
        return [
            ScheduledBroadcast(
                id=int(r["id"]),
                msg_type=r["msg_type"],
                text=r["text"],
                scheduled_at=str(r["scheduled_at"]),
                created_by=int(r["created_by"]),
            )
            for r in rows
        ]

    def get_pending_scheduled_broadcasts(self) -> list[ScheduledBroadcast]:
        now = datetime.now(timezone.utc)
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT id, msg_type, text, scheduled_at, created_by
                FROM scheduled_broadcasts
                WHERE sent_at IS NULL AND scheduled_at <= %s
                ORDER BY scheduled_at
                """,
                (now,),
            )
            rows = cur.fetchall()
        return [
            ScheduledBroadcast(
                id=int(r["id"]),
                msg_type=r["msg_type"],
                text=r["text"],
                scheduled_at=str(r["scheduled_at"]),
                created_by=int(r["created_by"]),
            )
            for r in rows
        ]

    def mark_scheduled_broadcast_sent(self, broadcast_id: int) -> None:
        now = datetime.now(timezone.utc)
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "UPDATE scheduled_broadcasts SET sent_at = %s WHERE id = %s",
                (now, broadcast_id),
            )

    def get_bot_stats(self) -> BotStats:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT COUNT(*) AS c FROM users
                WHERE COALESCE(bot_blocked, FALSE) = FALSE
                """
            )
            users = int(cur.fetchone()["c"])
            cur.execute(
                """
                SELECT COUNT(*) AS c FROM (
                    SELECT user_id AS id FROM users
                    WHERE COALESCE(bot_blocked, FALSE) = FALSE
                    UNION
                    SELECT DISTINCT s.owner_id AS id FROM subscriptions s
                    LEFT JOIN users u ON u.user_id = s.owner_id
                    WHERE COALESCE(u.bot_blocked, FALSE) = FALSE
                ) AS u
                """
            )
            notify = int(cur.fetchone()["c"])
            cur.execute(
                """
                SELECT COUNT(*) AS c FROM subscriptions s
                LEFT JOIN users u ON u.user_id = s.owner_id
                WHERE COALESCE(u.bot_blocked, FALSE) = FALSE
                """
            )
            subs_total = int(cur.fetchone()["c"])
            cur.execute(
                """
                SELECT COUNT(*) AS c FROM subscriptions s
                LEFT JOIN users u ON u.user_id = s.owner_id
                WHERE s.enabled = TRUE AND COALESCE(u.bot_blocked, FALSE) = FALSE
                """
            )
            subs_enabled = int(cur.fetchone()["c"])
            cur.execute(
                """
                SELECT COUNT(DISTINCT s.owner_id) AS c FROM subscriptions s
                LEFT JOIN users u ON u.user_id = s.owner_id
                WHERE COALESCE(u.bot_blocked, FALSE) = FALSE
                """
            )
            unique_owners = int(cur.fetchone()["c"])
            cur.execute(
                """
                SELECT COUNT(DISTINCT s.twitch_user_id) AS c FROM subscriptions s
                LEFT JOIN users u ON u.user_id = s.owner_id
                WHERE COALESCE(u.bot_blocked, FALSE) = FALSE
                """
            )
            unique_twitch = int(cur.fetchone()["c"])
            cur.execute(
                """
                SELECT COUNT(*) AS c FROM (
                    SELECT user_id AS id FROM users
                    UNION
                    SELECT DISTINCT owner_id AS id FROM subscriptions
                ) AS n
                LEFT JOIN users u ON u.user_id = n.id
                WHERE COALESCE(u.receive_bot_updates, TRUE) = TRUE
                  AND COALESCE(u.bot_blocked, FALSE) = FALSE
                """
            )
            sys_updates = int(cur.fetchone()["c"])
            cur.execute(
                """
                SELECT COUNT(*) AS c FROM (
                    SELECT user_id AS id FROM users
                    UNION
                    SELECT DISTINCT owner_id AS id FROM subscriptions
                ) AS n
                LEFT JOIN users u ON u.user_id = n.id
                WHERE COALESCE(u.receive_availability_updates, TRUE) = TRUE
                  AND COALESCE(u.bot_blocked, FALSE) = FALSE
                """
            )
            sys_availability = int(cur.fetchone()["c"])
            cur.execute(
                "SELECT COUNT(*) AS c FROM users WHERE bot_blocked = TRUE"
            )
            blocked_users = int(cur.fetchone()["c"])
            cur.execute(
                """
                SELECT COUNT(*) AS c FROM users
                WHERE locale = 'en' AND COALESCE(bot_blocked, FALSE) = FALSE
                """
            )
            locale_en = int(cur.fetchone()["c"])
            cur.execute(
                """
                SELECT COUNT(*) AS c FROM users
                WHERE locale = 'ru' AND COALESCE(bot_blocked, FALSE) = FALSE
                """
            )
            locale_ru = int(cur.fetchone()["c"])
            cur.execute(
                """
                SELECT COUNT(*) AS c FROM users
                WHERE (locale IS NULL OR locale = '')
                  AND COALESCE(bot_blocked, FALSE) = FALSE
                """
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
            sys_updates=sys_updates,
            sys_availability=sys_availability,
            blocked_users=blocked_users,
            locale_en=locale_en,
            locale_ru=locale_ru,
            locale_unset=locale_unset,
        )


def open_database(path: Path, database_url: str | None = None) -> Database:
    if database_url:
        return PostgresDatabase(database_url)
    return SqliteDatabase(path)
