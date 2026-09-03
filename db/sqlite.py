from __future__ import annotations

import json
import logging
import random
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import (
    LUCKY_TEMPLATE_LIMIT,
    WATCH_MAX_FILTERS,
    AlertHistoryEntry,
    BotStats,
    ChatAuth,
    DeletedSubscriptionCartItem,
    PremiumChannel,
    ReferralCreditRef,
    ReferralStats,
    ReferralWithdrawal,
    ScheduledBroadcast,
    Subscription,
    TwitchSync,
    WatchFilter,
    WatchPrefs,
    WhisperAlert,
    _cart_item_from_row,
    _row_to_alert_history,
    _row_to_chat_auth,
    _row_to_referral_withdrawal,
    _row_to_sub,
    _row_to_twitch_sync,
    _row_to_whisper_alert,
    _scheduled_broadcast_from_row,
    _seed_lucky_templates_sqlite,
    _subscription_cart_snapshot,
    dump_watch_filters,
    parse_watch_filters,
    watch_filter_auto_name,
)

logger = logging.getLogger(__name__)

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
        if "use_global_ignore" not in cols:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN use_global_ignore "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "image_file_id" not in cols:
            conn.execute("ALTER TABLE subscriptions ADD COLUMN image_file_id TEXT")
        if "image_position" not in cols:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN image_position TEXT NOT NULL DEFAULT ''"
            )
        if "from_twitch_sync" not in cols:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN from_twitch_sync INTEGER NOT NULL DEFAULT 0"
            )
        if "from_watch_suggest" not in cols:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN from_watch_suggest INTEGER NOT NULL DEFAULT 0"
            )
        if "sync_user_edited" not in cols:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN sync_user_edited INTEGER NOT NULL DEFAULT 0"
            )
        if "category_watch_prefs" not in cols:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN category_watch_prefs TEXT NOT NULL DEFAULT ''"
            )
        if "category_watch_live_ids" not in cols:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN category_watch_live_ids TEXT NOT NULL DEFAULT ''"
            )
        if "category_watch_primed" not in cols:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN category_watch_primed INTEGER NOT NULL DEFAULT 0"
            )
        if "schedule_reminder_minutes" not in cols:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN schedule_reminder_minutes "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "last_schedule_reminder_segment_id" not in cols:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN last_schedule_reminder_segment_id TEXT"
            )
        if "schedule_reminder_configured" not in cols:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN schedule_reminder_configured "
                "INTEGER NOT NULL DEFAULT 0"
            )
            conn.execute(
                """
                UPDATE subscriptions
                SET schedule_reminder_configured = 1
                WHERE schedule_reminder_minutes > 0
                """
            )
        if "notify_on_live" not in cols:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN notify_on_live INTEGER NOT NULL DEFAULT 1"
            )
        if "notify_on_end" not in cols:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN notify_on_end INTEGER NOT NULL DEFAULT 0"
            )
        if "notify_on_category_change" not in cols:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN notify_on_category_change "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "delete_other_alerts" not in cols:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN delete_other_alerts "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "is_demo" not in cols:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN is_demo INTEGER NOT NULL DEFAULT 0"
            )
        if "trial_paused" not in cols:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN trial_paused INTEGER NOT NULL DEFAULT 0"
            )
        if "delivery_paused" not in cols:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN delivery_paused INTEGER NOT NULL DEFAULT 0"
            )
        if "strip_name_mentions" not in cols:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN strip_name_mentions "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "attach_chat_button" not in cols:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN attach_chat_button "
                "INTEGER NOT NULL DEFAULT 0"
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
        if "receive_other_updates" not in user_cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN receive_other_updates INTEGER NOT NULL DEFAULT 1"
            )
        if "receive_sync_updates" not in user_cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN receive_sync_updates INTEGER NOT NULL DEFAULT 1"
            )
        if "bot_blocked" not in user_cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN bot_blocked INTEGER NOT NULL DEFAULT 0"
            )
        if "saved_schedule_hour" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN saved_schedule_hour INTEGER")
        if "saved_schedule_minute" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN saved_schedule_minute INTEGER")
        if "schedule_utc_offset_minutes" not in user_cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN schedule_utc_offset_minutes INTEGER"
            )
        if "watch_prefs" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN watch_prefs TEXT NOT NULL DEFAULT ''")
        if "global_ignore_keywords" not in user_cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN global_ignore_keywords "
                "TEXT NOT NULL DEFAULT ''"
            )
        if "premium_permanent" not in user_cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN premium_permanent INTEGER NOT NULL DEFAULT 0"
            )
            # Grandfather everyone who already used the bot before Premium existed.
            conn.execute("UPDATE users SET premium_permanent = 1")
            conn.execute(
                """
                INSERT OR IGNORE INTO users (user_id, premium_permanent)
                SELECT DISTINCT owner_id, 1 FROM subscriptions
                """
            )
        for col, decl in (
            ("premium_stars_charge_id", "TEXT NOT NULL DEFAULT ''"),
            ("premium_stars_until", "INTEGER NOT NULL DEFAULT 0"),
            ("premium_stars_canceled", "INTEGER NOT NULL DEFAULT 0"),
            ("premium_twitch_user_id", "TEXT NOT NULL DEFAULT ''"),
            ("premium_twitch_refresh", "TEXT NOT NULL DEFAULT ''"),
            ("premium_twitch_active", "INTEGER NOT NULL DEFAULT 0"),
            ("premium_twitch_checked_at", "TEXT"),
            ("premium_stars_paid_at", "TEXT"),
            ("referred_by", "INTEGER"),
            ("premium_trial_until", "INTEGER NOT NULL DEFAULT 0"),
            ("premium_trial_used", "INTEGER NOT NULL DEFAULT 0"),
            ("premium_features", "TEXT NOT NULL DEFAULT ''"),
            ("advanced_mode", "INTEGER"),
            ("notifications_paused_until", "INTEGER NOT NULL DEFAULT 0"),
            ("template_typo_notice_sent", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in {row[1] for row in conn.execute("PRAGMA table_info(users)")}:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} {decl}")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS referral_credits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                invitee_id INTEGER NOT NULL,
                charge_id TEXT NOT NULL UNIQUE,
                stars_paid INTEGER NOT NULL,
                commission_stars INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_referral_credits_referrer
            ON referral_credits(referrer_id)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS referral_withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                resolved_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_referral_withdrawals_user
            ON referral_withdrawals(user_id)
            """
        )
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
        sb_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(scheduled_broadcasts)")
        }
        if "recipient_ids" not in sb_cols:
            conn.execute(
                "ALTER TABLE scheduled_broadcasts "
                "ADD COLUMN recipient_ids TEXT NOT NULL DEFAULT ''"
            )
        if "sent_utc_offsets" not in sb_cols:
            conn.execute(
                "ALTER TABLE scheduled_broadcasts "
                "ADD COLUMN sent_utc_offsets TEXT NOT NULL DEFAULT ''"
            )
        if "sent_count" not in sb_cols:
            conn.execute(
                "ALTER TABLE scheduled_broadcasts "
                "ADD COLUMN sent_count INTEGER NOT NULL DEFAULT 0"
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS broadcast_deliveries (
                broadcast_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                PRIMARY KEY (broadcast_id, user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS broadcast_feedback (
                broadcast_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                vote INTEGER NOT NULL CHECK (vote IN (1, -1)),
                PRIMARY KEY (broadcast_id, user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS lucky_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                locale TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS twitch_sync (
                owner_id INTEGER PRIMARY KEY,
                twitch_user_id TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                period_days INTEGER NOT NULL,
                next_sync_at TEXT NOT NULL,
                last_sync_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS whisper_alerts (
                owner_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                twitch_user_id TEXT NOT NULL DEFAULT '',
                twitch_login TEXT NOT NULL DEFAULT '',
                refresh_token TEXT NOT NULL DEFAULT '',
                eventsub_id TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_whisper_alerts_twitch_user
            ON whisper_alerts(twitch_user_id)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_auth (
                owner_id INTEGER PRIMARY KEY,
                twitch_user_id TEXT NOT NULL DEFAULT '',
                twitch_login TEXT NOT NULL DEFAULT '',
                refresh_token TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_send_daily (
                owner_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (owner_id, day)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                subscription_id INTEGER,
                twitch_username TEXT NOT NULL,
                alert_type TEXT NOT NULL,
                message_text TEXT NOT NULL DEFAULT '',
                twitch_user_id TEXT NOT NULL DEFAULT '',
                stream_id TEXT NOT NULL DEFAULT '',
                vod_id TEXT NOT NULL DEFAULT '',
                vod_offset_seconds INTEGER,
                viewed INTEGER NOT NULL DEFAULT 0,
                sent_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        ah_cols = {row[1] for row in conn.execute("PRAGMA table_info(alert_history)")}
        if "message_text" not in ah_cols:
            conn.execute(
                "ALTER TABLE alert_history ADD COLUMN message_text TEXT NOT NULL DEFAULT ''"
            )
        if "twitch_user_id" not in ah_cols:
            conn.execute(
                "ALTER TABLE alert_history ADD COLUMN twitch_user_id TEXT NOT NULL DEFAULT ''"
            )
        if "stream_id" not in ah_cols:
            conn.execute(
                "ALTER TABLE alert_history ADD COLUMN stream_id TEXT NOT NULL DEFAULT ''"
            )
        if "vod_id" not in ah_cols:
            conn.execute(
                "ALTER TABLE alert_history ADD COLUMN vod_id TEXT NOT NULL DEFAULT ''"
            )
        if "vod_offset_seconds" not in ah_cols:
            conn.execute("ALTER TABLE alert_history ADD COLUMN vod_offset_seconds INTEGER")
        if "viewed" not in ah_cols:
            conn.execute(
                "ALTER TABLE alert_history ADD COLUMN viewed INTEGER NOT NULL DEFAULT 0"
            )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_alert_history_owner_sent
            ON alert_history(owner_id, sent_at DESC)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deleted_subscriptions_cart (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                is_demo INTEGER NOT NULL DEFAULT 0,
                deleted_at TEXT NOT NULL,
                subscription_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_del_subs_cart_owner_demo_deleted
            ON deleted_subscriptions_cart(owner_id, is_demo, deleted_at DESC)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_beta_enrollments (
                user_id INTEGER NOT NULL,
                feature_id TEXT NOT NULL,
                enrolled INTEGER NOT NULL DEFAULT 1,
                opted_in_at TEXT NOT NULL DEFAULT (datetime('now')),
                opted_out_at TEXT,
                PRIMARY KEY (user_id, feature_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS premium_channels (
                twitch_user_id TEXT PRIMARY KEY,
                twitch_login TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                owner_telegram_id INTEGER NOT NULL,
                charge_id TEXT NOT NULL DEFAULT '',
                paid_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_premium_channels_login
            ON premium_channels(twitch_login)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_share_tokens (
                token TEXT PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                source_sub_id INTEGER NOT NULL,
                snapshot_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_alert_share_tokens_sub
            ON alert_share_tokens(source_sub_id)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS unreachable_chats (
                chat_id INTEGER PRIMARY KEY,
                marked_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        _seed_lucky_templates_sqlite(conn)

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
        strip_name_mentions: bool = False,
        attach_chat_button: bool = False,
        delay_minutes: int = 0,
        suppress_repeat_minutes: int = 0,
        schedule_reminder_minutes: int = 0,
        schedule_reminder_configured: bool = False,
        ignore_keywords: str = "",
        use_global_ignore: bool = False,
        image_file_id: str | None = None,
        image_position: str = "",
        enabled: bool = True,
        from_twitch_sync: bool = False,
        from_watch_suggest: bool = False,
        category_watch_prefs: str = "",
        notify_on_live: bool = True,
        notify_on_end: bool = False,
        notify_on_category_change: bool = False,
        delete_other_alerts: bool = False,
        is_demo: bool = False,
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO subscriptions (
                    owner_id, twitch_username, twitch_user_id,
                    message_template, dest_type, chat_id, thread_id,
                    delete_previous, notify_delete_fail, disable_link_preview,
                    strip_name_mentions, attach_chat_button,
                    delay_minutes, suppress_repeat_minutes, schedule_reminder_minutes,
                    schedule_reminder_configured, ignore_keywords, use_global_ignore,
                    image_file_id, image_position, enabled, from_twitch_sync,
                    from_watch_suggest, category_watch_prefs,
                    notify_on_live, notify_on_end, notify_on_category_change,
                    delete_other_alerts, is_demo
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    int(bool(strip_name_mentions)),
                    int(bool(attach_chat_button)),
                    max(0, int(delay_minutes)),
                    max(0, int(suppress_repeat_minutes)),
                    max(0, int(schedule_reminder_minutes)),
                    int(bool(schedule_reminder_configured) or int(schedule_reminder_minutes) > 0),
                    ignore_keywords,
                    int(bool(use_global_ignore)),
                    image_file_id or None,
                    (image_position or "") if image_file_id else "",
                    int(enabled),
                    int(from_twitch_sync),
                    int(bool(from_watch_suggest)),
                    str(category_watch_prefs or ""),
                    int(bool(notify_on_live)),
                    int(bool(notify_on_end)),
                    int(bool(notify_on_category_change)),
                    int(bool(delete_other_alerts)),
                    int(bool(is_demo)),
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

    def set_last_message_id(self, sub_id: int, message_id: int | None) -> None:
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

    def set_last_schedule_reminder_segment(self, sub_id: int, segment_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE subscriptions SET last_schedule_reminder_segment_id = ? WHERE id = ?",
                (segment_id, sub_id),
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
            if new_state:
                conn.execute(
                    """
                    UPDATE subscriptions
                    SET enabled = 1, delivery_paused = 0
                    WHERE id = ? AND owner_id = ?
                    """,
                    (sub_id, owner_id),
                )
            else:
                conn.execute(
                    "UPDATE subscriptions SET enabled = 0 WHERE id = ? AND owner_id = ?",
                    (sub_id, owner_id),
                )
        return bool(new_state)

    def enable_all_subscriptions(
        self, owner_id: int, *, demo: bool = False, max_count: int | None = None
    ) -> int:
        sub = """
            SELECT id FROM subscriptions
            WHERE owner_id = ? AND enabled = 0 AND is_demo = ? AND trial_paused = 0
              AND COALESCE(delivery_paused, 0) = 0
            ORDER BY id
        """
        params: list[object] = [owner_id, int(bool(demo))]
        if max_count is not None:
            sub += " LIMIT ?"
            params.append(max(0, int(max_count)))
        with self._conn() as conn:
            cur = conn.execute(
                f"UPDATE subscriptions SET enabled = 1 WHERE id IN ({sub})",
                params,
            )
            return int(cur.rowcount)


    def delete_subscription(self, sub_id: int, owner_id: int, *, to_cart: bool = True) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM subscriptions WHERE id = ? AND owner_id = ?",
                (sub_id, owner_id),
            ).fetchone()
            if not row:
                return False
            sub = _row_to_sub(row)
            if to_cart:
                payload = _subscription_cart_snapshot(sub)
                deleted_at = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """
                    INSERT INTO deleted_subscriptions_cart (
                        owner_id, is_demo, deleted_at, subscription_json
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        owner_id,
                        int(bool(sub.is_demo)),
                        deleted_at,
                        json.dumps(payload, ensure_ascii=False),
                    ),
                )
            cur = conn.execute(
                "DELETE FROM subscriptions WHERE id = ? AND owner_id = ?",
                (sub_id, owner_id),
            )
        return cur.rowcount > 0

    def list_deleted_subscriptions(
        self,
        owner_id: int,
        *,
        days: int,
        is_demo: bool,
        limit: int = 100,
    ) -> list[DeletedSubscriptionCartItem]:
        days = max(1, int(days))
        limit = max(1, int(limit))
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_iso = cutoff.astimezone(timezone.utc).isoformat()
        with self._conn() as conn:
            # Always cap physical storage to avoid an unbounded cart.
            # UI may request 10/30 days, but we never keep >30 days overall.
            max_cutoff = datetime.now(timezone.utc) - timedelta(days=30)
            effective_cutoff = max(cutoff, max_cutoff)
            effective_cutoff_iso = effective_cutoff.astimezone(timezone.utc).isoformat()
            rows = conn.execute(
                """
                SELECT id, deleted_at, subscription_json
                FROM deleted_subscriptions_cart
                WHERE owner_id = ?
                  AND is_demo = ?
                  AND deleted_at >= ?
                ORDER BY deleted_at DESC
                LIMIT ?
                """,
                (owner_id, int(bool(is_demo)), effective_cutoff_iso, limit),
            ).fetchall()
        return [
            _cart_item_from_row(int(r["id"]), r["deleted_at"], r["subscription_json"])
            for r in rows
        ]

    def restore_deleted_subscriptions(
        self,
        owner_id: int,
        cart_ids: list[int],
        *,
        days: int,
        is_demo: bool,
        max_enabled: int | None = None,
    ) -> tuple[int, int]:
        if not cart_ids:
            return 0, 0
        days = max(1, int(days))
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_iso = cutoff.astimezone(timezone.utc).isoformat()
        cart_ids = [int(i) for i in cart_ids if str(i)]
        if not cart_ids:
            return 0, 0

        placeholders = ",".join("?" for _ in cart_ids)
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT id, subscription_json
                FROM deleted_subscriptions_cart
                WHERE owner_id = ?
                  AND is_demo = ?
                  AND id IN ({placeholders})
                  AND deleted_at >= ?
                """,
                (owner_id, int(bool(is_demo)), *cart_ids, cutoff_iso),
            ).fetchall()
            restored = enabled_restored = 0
            slots_used = 0
            for r in rows:
                try:
                    payload = json.loads(r["subscription_json"] or "{}")
                except Exception:
                    continue
                from premium import alert_type_entitled_sync, is_promo_channel
                from types import SimpleNamespace

                login = str(payload.get("twitch_username") or "")
                promo = is_promo_channel(login, self)
                type_ok = alert_type_entitled_sync(
                    self,
                    owner_id,
                    SimpleNamespace(
                        notify_on_live=bool(payload.get("notify_on_live", True)),
                        notify_on_end=bool(payload.get("notify_on_end")),
                        notify_on_category_change=bool(
                            payload.get("notify_on_category_change")
                        ),
                        schedule_reminder_configured=bool(
                            payload.get("schedule_reminder_configured")
                        ),
                        twitch_username=login,
                    ),
                )
                if not type_ok:
                    sub_enabled = False
                elif promo:
                    sub_enabled = True
                elif max_enabled is not None:
                    sub_enabled = slots_used < max(0, int(max_enabled))
                else:
                    sub_enabled = True
                payload["enabled"] = sub_enabled
                if sub_enabled:
                    enabled_restored += 1
                    if not promo:
                        slots_used += 1
                conn.execute(
                    """
                    INSERT INTO subscriptions (
                        owner_id, twitch_username, twitch_user_id,
                        message_template, dest_type, chat_id, thread_id,
                        delete_previous, notify_delete_fail, disable_link_preview,
                        strip_name_mentions, attach_chat_button,
                        delay_minutes, suppress_repeat_minutes,
                        schedule_reminder_minutes, schedule_reminder_configured,
                        ignore_keywords, use_global_ignore,
                        image_file_id, image_position, enabled,
                        from_twitch_sync, from_watch_suggest,
                        category_watch_prefs,
                        notify_on_live, notify_on_end, notify_on_category_change,
                        delete_other_alerts, is_demo
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        owner_id,
                        payload.get("twitch_username") or "",
                        payload.get("twitch_user_id") or "",
                        payload.get("message_template") or "",
                        payload.get("dest_type") or "dm",
                        int(payload.get("chat_id") or owner_id),
                        payload.get("thread_id"),
                        int(bool(payload.get("delete_previous"))),
                        int(bool(payload.get("notify_delete_fail"))),
                        int(bool(payload.get("disable_link_preview"))),
                        int(bool(payload.get("strip_name_mentions"))),
                        int(bool(payload.get("attach_chat_button"))),
                        int(payload.get("delay_minutes") or 0),
                        int(payload.get("suppress_repeat_minutes") or 0),
                        int(payload.get("schedule_reminder_minutes") or 0),
                        int(bool(payload.get("schedule_reminder_configured"))),
                        payload.get("ignore_keywords") or "",
                        int(bool(payload.get("use_global_ignore"))),
                        payload.get("image_file_id"),
                        payload.get("image_position") or "",
                        int(bool(payload.get("enabled"))),
                        int(bool(payload.get("from_twitch_sync"))),
                        int(bool(payload.get("from_watch_suggest"))),
                        payload.get("category_watch_prefs") or "",
                        int(bool(payload.get("notify_on_live"))),
                        int(bool(payload.get("notify_on_end"))),
                        int(bool(payload.get("notify_on_category_change"))),
                        int(bool(payload.get("delete_other_alerts"))),
                        int(bool(payload.get("is_demo"))),
                    ),
                )
                sub_id = int(
                    conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                )
                # Materialize `subscriptions` row through existing invariants:
                # restore snapshots only (not sync caches).
                conn.execute("DELETE FROM deleted_subscriptions_cart WHERE id = ? AND owner_id = ?",
                    (int(r["id"]), owner_id),
                )
                restored += 1
            return restored, enabled_restored

    def update_subscription(self, sub_id: int, owner_id: int, **fields: object) -> bool:
        mark_sync_edited = bool(fields.pop("mark_sync_edited", True))
        allowed = {
            "message_template",
            "dest_type",
            "chat_id",
            "thread_id",
            "delete_previous",
            "notify_delete_fail",
            "disable_link_preview",
            "strip_name_mentions",
            "attach_chat_button",
            "delay_minutes",
            "suppress_repeat_minutes",
            "schedule_reminder_minutes",
            "schedule_reminder_configured",
            "notify_on_live",
            "notify_on_end",
            "notify_on_category_change",
            "delete_other_alerts",
            "ignore_keywords",
            "use_global_ignore",
            "image_file_id",
            "image_position",
        }
        updates: list[str] = []
        values: list[object] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            updates.append(f"{key} = ?")
            if key in (
                "delete_previous",
                "notify_delete_fail",
                "disable_link_preview",
                "strip_name_mentions",
                "attach_chat_button",
                "schedule_reminder_configured",
                "notify_on_live",
                "notify_on_end",
                "notify_on_category_change",
                "delete_other_alerts",
                "use_global_ignore",
            ):
                values.append(int(bool(value)))
            elif key in (
                "delay_minutes",
                "suppress_repeat_minutes",
                "schedule_reminder_minutes",
            ):
                values.append(max(0, int(value)))
            elif key == "ignore_keywords":
                values.append(str(value or ""))
            elif key == "image_file_id":
                values.append(str(value) if value else None)
            elif key == "image_position":
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
            updated = cur.rowcount > 0
            if updated and mark_sync_edited:
                conn.execute(
                    """
                    UPDATE subscriptions SET sync_user_edited = 1
                    WHERE id = ? AND owner_id = ? AND from_twitch_sync = 1
                    """,
                    (sub_id, owner_id),
                )
        return updated

    def get_user_locale(self, user_id: int) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT locale FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        if not row or not row["locale"]:
            return None
        return str(row["locale"])

    def get_user_locales(self, user_ids: list[int]) -> dict[int, str | None]:
        if not user_ids:
            return {}
        unique = list(dict.fromkeys(int(uid) for uid in user_ids))
        out: dict[int, str | None] = {uid: None for uid in unique}
        placeholders = ",".join("?" for _ in unique)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT user_id, locale FROM users WHERE user_id IN ({placeholders})",
                unique,
            ).fetchall()
        for row in rows:
            loc = row["locale"]
            out[int(row["user_id"])] = str(loc) if loc else None
        return out

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
                  AND COALESCE(category_watch_prefs, '') = ''
                  AND twitch_user_id NOT LIKE 'cw:%'
                  AND (
                    notify_on_live = 1
                    OR notify_on_end = 1
                    OR notify_on_category_change = 1
                )
                """
            ).fetchall()
        return [r["twitch_user_id"] for r in rows]

    def get_enabled_category_watch_subscriptions(self) -> list[Subscription]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM subscriptions
                WHERE enabled = 1
                  AND notify_on_live = 1
                  AND COALESCE(category_watch_prefs, '') != ''
                ORDER BY id
                """
            ).fetchall()
        return [_row_to_sub(r) for r in rows]

    def set_category_watch_live_state(
        self, sub_id: int, live_ids: list[str], *, primed: bool
    ) -> None:
        payload = json.dumps(list(live_ids), ensure_ascii=False)
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE subscriptions
                SET category_watch_live_ids = ?, category_watch_primed = ?
                WHERE id = ?
                """,
                (payload, int(bool(primed)), sub_id),
            )

    def get_unique_schedule_reminder_twitch_ids(self) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT twitch_user_id
                FROM subscriptions
                WHERE enabled = 1 AND schedule_reminder_minutes > 0
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

    def upsert_user(self, user_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, bot_blocked) VALUES (?, 0)
                ON CONFLICT(user_id) DO UPDATE SET bot_blocked = 0
                """,
                (user_id,),
            )

    def user_exists(self, user_id: int) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return row is not None

    def count_new_users_since(self, since: datetime) -> int:
        since_utc = since.astimezone(timezone.utc) if since.tzinfo else since.replace(tzinfo=timezone.utc)
        since_s = since_utc.strftime("%Y-%m-%d %H:%M:%S")
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE first_seen >= ?",
                (since_s,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def count_stars_payers_since(self, since: datetime) -> int:
        since_utc = since.astimezone(timezone.utc) if since.tzinfo else since.replace(tzinfo=timezone.utc)
        since_s = since_utc.strftime("%Y-%m-%d %H:%M:%S")
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM users
                WHERE premium_stars_paid_at IS NOT NULL
                  AND premium_stars_paid_at >= ?
                """,
                (since_s,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def list_active_trial_users(self, *, now_unix: int | None = None) -> list[tuple[int, int]]:
        now = int(now_unix if now_unix is not None else datetime.now(timezone.utc).timestamp())
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT user_id, COALESCE(premium_trial_until, 0) AS premium_trial_until
                FROM users
                WHERE COALESCE(premium_trial_until, 0) > ?
                ORDER BY premium_trial_until, user_id
                """,
                (now,),
            ).fetchall()
        return [(int(r["user_id"]), int(r["premium_trial_until"])) for r in rows]

    def list_expired_trial_users(self, *, now_unix: int | None = None) -> list[tuple[int, int]]:
        now = int(now_unix if now_unix is not None else datetime.now(timezone.utc).timestamp())
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT user_id, COALESCE(premium_trial_until, 0) AS premium_trial_until
                FROM users
                WHERE COALESCE(premium_trial_until, 0) > 0
                  AND COALESCE(premium_trial_until, 0) <= ?
                ORDER BY premium_trial_until, user_id
                """,
                (now,),
            ).fetchall()
        return [(int(r["user_id"]), int(r["premium_trial_until"])) for r in rows]

    def set_referred_by(self, user_id: int, referrer_id: int) -> bool:
        if user_id == referrer_id or referrer_id <= 0:
            return False
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO users (user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING",
                (user_id,),
            )
            cur = conn.execute(
                """
                UPDATE users
                SET referred_by = ?
                WHERE user_id = ?
                  AND (referred_by IS NULL OR referred_by = 0)
                """,
                (referrer_id, user_id),
            )
            return cur.rowcount > 0

    def get_referred_by(self, user_id: int) -> int | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT referred_by FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if not row or row["referred_by"] is None:
            return None
        value = int(row["referred_by"])
        return value if value > 0 else None

    def add_referral_credit(
        self,
        *,
        referrer_id: int,
        invitee_id: int,
        charge_id: str,
        stars_paid: int,
        commission_stars: int,
    ) -> bool:
        if referrer_id <= 0 or invitee_id <= 0 or not charge_id:
            return False
        if commission_stars <= 0 or stars_paid <= 0:
            return False
        with self._conn() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO referral_credits (
                        referrer_id, invitee_id, charge_id, stars_paid, commission_stars
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        referrer_id,
                        invitee_id,
                        charge_id,
                        int(stars_paid),
                        int(commission_stars),
                    ),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def get_referral_stats(self, user_id: int) -> ReferralStats:
        with self._conn() as conn:
            invited = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE referred_by = ?",
                (user_id,),
            ).fetchone()["n"]
            payments = conn.execute(
                "SELECT COUNT(*) AS n FROM referral_credits WHERE referrer_id = ?",
                (user_id,),
            ).fetchone()["n"]
            earned = conn.execute(
                """
                SELECT COALESCE(SUM(commission_stars), 0) AS n
                FROM referral_credits WHERE referrer_id = ?
                """,
                (user_id,),
            ).fetchone()["n"]
            withdrawn = conn.execute(
                """
                SELECT COALESCE(SUM(amount), 0) AS n
                FROM referral_withdrawals
                WHERE user_id = ? AND status IN ('pending', 'paid')
                """,
                (user_id,),
            ).fetchone()["n"]
        available = max(0, int(earned) - int(withdrawn))
        return ReferralStats(
            invited=int(invited),
            payments=int(payments),
            available_stars=available,
        )

    def request_referral_withdrawal(self, user_id: int, amount: int) -> int | None:
        amount = int(amount)
        if amount <= 0:
            return None
        with self._conn() as conn:
            earned = conn.execute(
                """
                SELECT COALESCE(SUM(commission_stars), 0) AS n
                FROM referral_credits WHERE referrer_id = ?
                """,
                (user_id,),
            ).fetchone()["n"]
            withdrawn = conn.execute(
                """
                SELECT COALESCE(SUM(amount), 0) AS n
                FROM referral_withdrawals
                WHERE user_id = ? AND status IN ('pending', 'paid')
                """,
                (user_id,),
            ).fetchone()["n"]
            available = int(earned) - int(withdrawn)
            if amount > available:
                return None
            cur = conn.execute(
                """
                INSERT INTO referral_withdrawals (user_id, amount, status)
                VALUES (?, ?, 'pending')
                """,
                (user_id, amount),
            )
            return int(cur.lastrowid)

    def get_referral_withdrawal(self, withdrawal_id: int) -> ReferralWithdrawal | None:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT id, user_id, amount, status, created_at, resolved_at
                FROM referral_withdrawals WHERE id = ?
                """,
                (withdrawal_id,),
            ).fetchone()
        return _row_to_referral_withdrawal(row) if row else None

    def list_referral_withdrawals(
        self, user_id: int, *, limit: int = 20
    ) -> list[ReferralWithdrawal]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, user_id, amount, status, created_at, resolved_at
                FROM referral_withdrawals
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (user_id, int(limit)),
            ).fetchall()
        return [_row_to_referral_withdrawal(r) for r in rows]

    def list_pending_referral_withdrawals(self) -> list[ReferralWithdrawal]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, user_id, amount, status, created_at, resolved_at
                FROM referral_withdrawals
                WHERE status = 'pending'
                ORDER BY id ASC
                """
            ).fetchall()
        return [_row_to_referral_withdrawal(r) for r in rows]

    def add_alert_history(
        self,
        owner_id: int,
        *,
        subscription_id: int | None,
        twitch_username: str,
        alert_type: str,
        message_text: str = "",
        twitch_user_id: str = "",
        stream_id: str = "",
        vod_id: str = "",
        vod_offset_seconds: int | None = None,
    ) -> None:
        from premium import ALERT_HISTORY_PREMIUM_DAYS

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=ALERT_HISTORY_PREMIUM_DAYS)
        ).strftime("%Y-%m-%d %H:%M:%S")
        body = (message_text or "").strip()
        if len(body) > 4096:
            body = body[:4096]
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO alert_history (
                    owner_id, subscription_id, twitch_username, alert_type, message_text,
                    twitch_user_id, stream_id, vod_id, vod_offset_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    owner_id,
                    subscription_id,
                    (twitch_username or "").strip() or "—",
                    (alert_type or "").strip() or "live",
                    body,
                    (twitch_user_id or "").strip(),
                    (stream_id or "").strip(),
                    (vod_id or "").strip(),
                    vod_offset_seconds,
                ),
            )
            conn.execute(
                """
                DELETE FROM alert_history
                WHERE owner_id = ? AND sent_at < ?
                """,
                (owner_id, cutoff),
            )

    def set_alert_history_vod_id(self, history_id: int, vod_id: str) -> None:
        vid = (vod_id or "").strip()
        if not vid:
            return
        with self._conn() as conn:
            conn.execute(
                "UPDATE alert_history SET vod_id = ? WHERE id = ?",
                (vid, int(history_id)),
            )

    def set_alert_history_viewed(
        self, owner_id: int, history_id: int, *, viewed: bool
    ) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE alert_history
                SET viewed = ?
                WHERE id = ? AND owner_id = ?
                """,
                (1 if viewed else 0, int(history_id), int(owner_id)),
            )
            return cur.rowcount > 0

    def set_alert_history_viewed_below(
        self, owner_id: int, history_id: int, *, viewed: bool = True
    ) -> int:
        # History UI is newest-first (id DESC); "below" = this row and older (id <=).
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE alert_history
                SET viewed = ?
                WHERE owner_id = ? AND id <= ?
                """,
                (1 if viewed else 0, int(owner_id), int(history_id)),
            )
            return int(cur.rowcount)

    def list_alert_history(
        self,
        owner_id: int,
        *,
        since: datetime | None = None,
        limit: int = 500,
    ) -> list[AlertHistoryEntry]:
        with self._conn() as conn:
            if since is not None:
                if since.tzinfo is None:
                    since = since.replace(tzinfo=timezone.utc)
                since_s = since.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                rows = conn.execute(
                    """
                    SELECT id, owner_id, subscription_id, twitch_username,
                           alert_type, message_text, sent_at,
                           twitch_user_id, stream_id, vod_id, vod_offset_seconds,
                           viewed
                    FROM alert_history
                    WHERE owner_id = ? AND sent_at >= ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (owner_id, since_s, int(limit)),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, owner_id, subscription_id, twitch_username,
                           alert_type, message_text, sent_at,
                           twitch_user_id, stream_id, vod_id, vod_offset_seconds,
                           viewed
                    FROM alert_history
                    WHERE owner_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (owner_id, int(limit)),
                ).fetchall()
        return [_row_to_alert_history(r) for r in rows]

    def resolve_referral_withdrawal(
        self, withdrawal_id: int, status: str
    ) -> ReferralWithdrawal | None:
        if status not in ("paid", "rejected"):
            return None
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE referral_withdrawals
                SET status = ?, resolved_at = datetime('now')
                WHERE id = ? AND status = 'pending'
                """,
                (status, withdrawal_id),
            )
            if cur.rowcount <= 0:
                return None
            row = conn.execute(
                """
                SELECT id, user_id, amount, status, created_at, resolved_at
                FROM referral_withdrawals WHERE id = ?
                """,
                (withdrawal_id,),
            ).fetchone()
        return _row_to_referral_withdrawal(row) if row else None

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

    def set_chat_unreachable(self, chat_id: int, unreachable: bool) -> None:
        with self._conn() as conn:
            if unreachable:
                conn.execute(
                    """
                    INSERT INTO unreachable_chats (chat_id) VALUES (?)
                    ON CONFLICT(chat_id) DO NOTHING
                    """,
                    (chat_id,),
                )
            else:
                conn.execute(
                    "DELETE FROM unreachable_chats WHERE chat_id = ?",
                    (chat_id,),
                )

    def is_chat_unreachable(self, chat_id: int) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM unreachable_chats WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        return row is not None

    def pause_delivery_for_chat(self, chat_id: int) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE subscriptions
                SET enabled = 0, delivery_paused = 1
                WHERE chat_id = ? AND enabled = 1
                """,
                (chat_id,),
            )
            return int(cur.rowcount)

    def list_delivery_paused_for_chat(self, chat_id: int) -> list[Subscription]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM subscriptions
                WHERE chat_id = ? AND COALESCE(delivery_paused, 0) = 1
                ORDER BY id
                """,
                (chat_id,),
            ).fetchall()
        return [_row_to_sub(r) for r in rows]

    def clear_delivery_paused(self, sub_id: int, *, enabled: bool) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE subscriptions
                SET delivery_paused = 0, enabled = ?
                WHERE id = ?
                """,
                (int(bool(enabled)), sub_id),
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

    def _update_recipients(self, pref_column: str) -> list[int]:
        # Missing users row → opt-in defaults (receive=1, blocked=0).
        if pref_column not in (
            "receive_bot_updates",
            "receive_availability_updates",
            "receive_other_updates",
        ):
            raise ValueError(f"invalid recipient pref: {pref_column}")
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT ids.uid AS user_id
                FROM (
                    SELECT user_id AS uid FROM users
                    UNION
                    SELECT owner_id AS uid FROM subscriptions
                ) AS ids
                LEFT JOIN users u ON u.user_id = ids.uid
                WHERE COALESCE(u.bot_blocked, 0) = 0
                  AND COALESCE(u.{pref_column}, 1) = 1
                """
            ).fetchall()
        return [int(r["user_id"]) for r in rows]

    def get_bot_update_recipients(self) -> list[int]:
        return self._update_recipients("receive_bot_updates")

    def get_availability_recipients(self) -> list[int]:
        return self._update_recipients("receive_availability_updates")

    def get_other_recipients(self) -> list[int]:
        return self._update_recipients("receive_other_updates")

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

    def get_receive_other_updates(self, user_id: int) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT receive_other_updates FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if not row:
            return True
        return bool(row["receive_other_updates"])

    def set_receive_other_updates(self, user_id: int, enabled: bool) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, receive_other_updates) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    receive_other_updates = excluded.receive_other_updates
                """,
                (user_id, int(enabled)),
            )

    def get_receive_sync_updates(self, user_id: int) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT receive_sync_updates FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if not row:
            return True
        return bool(row["receive_sync_updates"])

    def set_receive_sync_updates(self, user_id: int, enabled: bool) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, receive_sync_updates) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    receive_sync_updates = excluded.receive_sync_updates
                """,
                (user_id, int(enabled)),
            )

    def get_notifications_paused_until(self, user_id: int) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT notifications_paused_until FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if not row:
            return 0
        return int(row["notifications_paused_until"] or 0)

    def set_notifications_paused_until(self, user_id: int, until_ts: int) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, notifications_paused_until) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    notifications_paused_until = excluded.notifications_paused_until
                """,
                (user_id, int(until_ts)),
            )

    def mark_template_typo_notice_sent(self, user_id: int) -> bool:
        """Return True when this call newly marks the owner (first notice)."""
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
                (user_id,),
            )
            cur = conn.execute(
                """
                UPDATE users
                SET template_typo_notice_sent = 1
                WHERE user_id = ?
                  AND COALESCE(template_typo_notice_sent, 0) = 0
                """,
                (user_id,),
            )
            return bool(cur.rowcount)

    def get_global_ignore_keywords(self, user_id: int) -> str:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT global_ignore_keywords FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if not row:
            return ""
        return str(row["global_ignore_keywords"] or "")

    def set_global_ignore_keywords(self, user_id: int, keywords: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, global_ignore_keywords) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    global_ignore_keywords = excluded.global_ignore_keywords
                """,
                (user_id, str(keywords or "")),
            )

    def get_advanced_mode_setting(self, user_id: int) -> bool | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT advanced_mode FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None or row["advanced_mode"] is None:
            return None
        return bool(row["advanced_mode"])

    def set_advanced_mode_setting(self, user_id: int, enabled: bool) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, advanced_mode) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    advanced_mode = excluded.advanced_mode
                """,
                (user_id, 1 if enabled else 0),
            )

    def owner_has_advanced_subscription_options(self, owner_id: int) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT 1 AS ok FROM subscriptions
                WHERE owner_id = ?
                  AND (
                    TRIM(COALESCE(ignore_keywords, '')) != ''
                    OR COALESCE(use_global_ignore, 0) != 0
                    OR COALESCE(delay_minutes, 0) > 0
                    OR COALESCE(suppress_repeat_minutes, 0) > 0
                    OR COALESCE(delete_previous, 0) != 0
                  )
                LIMIT 1
                """,
                (owner_id,),
            ).fetchone()
        return row is not None

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

    def get_schedule_utc_offset_minutes(self, user_id: int) -> int | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT schedule_utc_offset_minutes FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if not row or row["schedule_utc_offset_minutes"] is None:
            return None
        return int(row["schedule_utc_offset_minutes"])

    def set_schedule_utc_offset_minutes(self, user_id: int, offset_minutes: int) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, schedule_utc_offset_minutes)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    schedule_utc_offset_minutes = excluded.schedule_utc_offset_minutes
                """,
                (user_id, int(offset_minutes)),
            )

    def get_schedule_utc_offsets_for_users(
        self, user_ids: list[int]
    ) -> dict[int, int | None]:
        if not user_ids:
            return {}
        unique = list(dict.fromkeys(int(uid) for uid in user_ids))
        out: dict[int, int | None] = {uid: None for uid in unique}
        placeholders = ",".join("?" for _ in unique)
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT user_id, schedule_utc_offset_minutes
                FROM users WHERE user_id IN ({placeholders})
                """,
                unique,
            ).fetchall()
        for row in rows:
            val = row["schedule_utc_offset_minutes"]
            out[int(row["user_id"])] = int(val) if val is not None else None
        return out

    def record_broadcast_offset_sent(
        self, broadcast_id: int, utc_offset_minutes: int, sent: int
    ) -> None:
        off = int(utc_offset_minutes)
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT sent_utc_offsets, sent_count
                FROM scheduled_broadcasts
                WHERE id = ? AND sent_at IS NULL
                """,
                (broadcast_id,),
            ).fetchone()
            if not row:
                return
            parts = [
                p.strip()
                for p in str(row["sent_utc_offsets"] or "").split(",")
                if p.strip()
            ]
            token = str(off)
            if token not in parts:
                parts.append(token)
            conn.execute(
                """
                UPDATE scheduled_broadcasts
                SET sent_utc_offsets = ?,
                    sent_count = COALESCE(sent_count, 0) + ?
                WHERE id = ?
                """,
                (",".join(parts), int(sent), broadcast_id),
            )

    def get_broadcast_sent_offsets(self, broadcast_id: int) -> set[int]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT sent_utc_offsets FROM scheduled_broadcasts WHERE id = ?",
                (broadcast_id,),
            ).fetchone()
        if not row:
            return set()
        out: set[int] = set()
        for part in str(row["sent_utc_offsets"] or "").split(","):
            part = part.strip()
            if part:
                out.add(int(part))
        return out

    def reset_broadcast_send_progress(self, broadcast_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE scheduled_broadcasts
                SET sent_utc_offsets = '', sent_count = 0
                WHERE id = ? AND sent_at IS NULL
                """,
                (broadcast_id,),
            )

    def get_broadcast_sent_count(self, broadcast_id: int) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT sent_count FROM scheduled_broadcasts WHERE id = ?",
                (broadcast_id,),
            ).fetchone()
        if not row:
            return 0
        return int(row["sent_count"] or 0)

    def get_watch_prefs(self, user_id: int) -> WatchPrefs | None:
        filters = self.get_watch_filters(user_id)
        return filters[0].prefs if filters else None

    def set_watch_prefs(self, user_id: int, prefs: WatchPrefs) -> None:
        self.add_watch_filter(user_id, prefs)

    def clear_watch_prefs(self, user_id: int) -> None:
        self.set_watch_filters(user_id, [])

    def get_watch_filters(self, user_id: int) -> list[WatchFilter]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT watch_prefs FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if not row:
            return []
        return parse_watch_filters(row["watch_prefs"])

    def set_watch_filters(self, user_id: int, filters: list[WatchFilter]) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, watch_prefs)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET watch_prefs = excluded.watch_prefs
                """,
                (user_id, dump_watch_filters(filters)),
            )

    def add_watch_filter(
        self, user_id: int, prefs: WatchPrefs, *, name: str | None = None
    ) -> WatchFilter:
        filters = self.get_watch_filters(user_id)
        filt = WatchFilter(
            id=secrets.token_hex(4),
            name=(name or watch_filter_auto_name(prefs))[:60],
            prefs=prefs,
        )
        filters.append(filt)
        if len(filters) > WATCH_MAX_FILTERS:
            filters = filters[-WATCH_MAX_FILTERS:]
        self.set_watch_filters(user_id, filters)
        return filt

    def delete_watch_filter(self, user_id: int, filter_id: str) -> bool:
        filters = self.get_watch_filters(user_id)
        kept = [f for f in filters if f.id != filter_id]
        if len(kept) == len(filters):
            return False
        self.set_watch_filters(user_id, kept)
        return True

    def count_enabled_subscriptions(self, owner_id: int, *, demo: bool = False) -> int:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM subscriptions
                WHERE owner_id = ? AND enabled = 1 AND is_demo = ?
                """,
                (owner_id, int(bool(demo))),
            ).fetchone()
        return int(row["c"])

    def delete_demo_subscriptions(self, owner_id: int) -> int:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id FROM subscriptions WHERE owner_id = ? AND is_demo = 1",
                (owner_id,),
            ).fetchall()
            ids = [int(r["id"]) for r in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"DELETE FROM alert_share_tokens WHERE source_sub_id IN ({placeholders})",
                    ids,
                )
                conn.execute(
                    f"DELETE FROM alert_history WHERE subscription_id IN ({placeholders})",
                    ids,
                )
            conn.execute(
                "DELETE FROM deleted_subscriptions_cart WHERE owner_id = ? AND is_demo = 1",
                (owner_id,),
            )
            cur = conn.execute(
                "DELETE FROM subscriptions WHERE owner_id = ? AND is_demo = 1",
                (owner_id,),
            )
        return int(cur.rowcount)

    def get_premium_status(self, user_id: int):
        from premium import PremiumStatus, parse_premium_features_blob

        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT premium_permanent, premium_stars_until, premium_stars_charge_id,
                       premium_stars_canceled, premium_twitch_active, premium_twitch_user_id,
                       COALESCE(premium_trial_until, 0) AS premium_trial_until,
                       COALESCE(premium_trial_used, 0) AS premium_trial_used,
                       COALESCE(premium_features, '') AS premium_features
                FROM users WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        if not row:
            return PremiumStatus(False, 0, "", False, False, "")
        features, charges, canceled = parse_premium_features_blob(
            row["premium_features"] or ""
        )
        return PremiumStatus(
            permanent=bool(row["premium_permanent"]),
            stars_until=int(row["premium_stars_until"] or 0),
            stars_charge_id=row["premium_stars_charge_id"] or "",
            stars_canceled=bool(row["premium_stars_canceled"]),
            twitch_active=bool(row["premium_twitch_active"]),
            twitch_user_id=row["premium_twitch_user_id"] or "",
            trial_until=int(row["premium_trial_until"] or 0),
            trial_used=bool(row["premium_trial_used"]),
            features=features,
            feature_charges=charges,
            feature_canceled=canceled,
        )

    def set_premium_stars(
        self,
        user_id: int,
        *,
        charge_id: str,
        until_unix: int,
        canceled: bool,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO users (
                    user_id, premium_stars_charge_id, premium_stars_until,
                    premium_stars_canceled, premium_stars_paid_at
                )
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(user_id) DO UPDATE SET
                    premium_stars_charge_id = excluded.premium_stars_charge_id,
                    premium_stars_until = excluded.premium_stars_until,
                    premium_stars_canceled = excluded.premium_stars_canceled,
                    premium_stars_paid_at = datetime('now')
                """,
                (user_id, charge_id, int(until_unix), int(bool(canceled))),
            )

    def set_premium_stars_canceled(self, user_id: int, canceled: bool) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, premium_stars_canceled)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    premium_stars_canceled = excluded.premium_stars_canceled
                """,
                (user_id, int(bool(canceled))),
            )

    def set_premium_permanent(self, user_id: int, permanent: bool) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, premium_permanent)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    premium_permanent = excluded.premium_permanent
                """,
                (user_id, int(bool(permanent))),
            )

    def clear_premium(self, user_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE users SET
                    premium_permanent = 0,
                    premium_stars_charge_id = '',
                    premium_stars_until = 0,
                    premium_stars_canceled = 1,
                    premium_trial_until = 0,
                    premium_features = '',
                    premium_twitch_active = 0,
                    premium_twitch_user_id = '',
                    premium_twitch_refresh = '',
                    premium_twitch_checked_at = NULL
                WHERE user_id = ?
                """,
                (user_id,),
            )

    def set_premium_trial(
        self, user_id: int, *, until_unix: int, used: bool = True
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, premium_trial_until, premium_trial_used)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    premium_trial_until = excluded.premium_trial_until,
                    premium_trial_used = excluded.premium_trial_used
                """,
                (user_id, int(until_unix), int(bool(used))),
            )

    def expire_premium_trial(self, user_id: int) -> int:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE users SET premium_trial_until = 0
                WHERE user_id = ?
                """,
                (user_id,),
            )
            cur = conn.execute(
                """
                UPDATE subscriptions
                SET enabled = 0, trial_paused = 1
                WHERE owner_id = ? AND enabled = 1 AND COALESCE(is_demo, 0) = 0
                """,
                (user_id,),
            )
            return int(cur.rowcount)

    def extend_premium_features(
        self,
        user_id: int,
        feature_ids: list[str],
        *,
        until_unix: int,
        charge_id: str = "",
    ) -> None:
        from premium import dump_premium_features_blob

        st = self.get_premium_status(user_id)
        features = dict(st.features)
        charges = dict(st.feature_charges)
        canceled = dict(st.feature_canceled)
        until = int(until_unix)
        for fid in feature_ids:
            features[fid] = max(int(features.get(fid) or 0), until)
            if charge_id:
                charges[fid] = charge_id
            canceled.pop(fid, None)
        raw = dump_premium_features_blob(features, charges, canceled)
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, premium_features, premium_stars_paid_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(user_id) DO UPDATE SET
                    premium_features = excluded.premium_features,
                    premium_stars_paid_at = datetime('now')
                """,
                (user_id, raw),
            )

    def clear_premium_feature(self, user_id: int, feature_id: str) -> None:
        from premium import dump_premium_features_blob

        st = self.get_premium_status(user_id)
        features = dict(st.features)
        charges = dict(st.feature_charges)
        canceled = dict(st.feature_canceled)
        features.pop(feature_id, None)
        charges.pop(feature_id, None)
        canceled.pop(feature_id, None)
        raw = dump_premium_features_blob(features, charges, canceled)
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, premium_features)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    premium_features = excluded.premium_features
                """,
                (user_id, raw),
            )

    def set_premium_feature_canceled(self, user_id: int, feature_id: str) -> None:
        from premium import dump_premium_features_blob

        st = self.get_premium_status(user_id)
        if not st.feature_active(feature_id):
            return
        features = dict(st.features)
        charges = dict(st.feature_charges)
        canceled = dict(st.feature_canceled)
        canceled[feature_id] = True
        raw = dump_premium_features_blob(features, charges, canceled)
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, premium_features)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    premium_features = excluded.premium_features
                """,
                (user_id, raw),
            )

    def set_premium_twitch(
        self,
        user_id: int,
        *,
        active: bool,
        twitch_user_id: str | None = None,
        refresh_token: str | None = None,
    ) -> None:
        from datetime import datetime, timezone

        from token_crypto import encrypt_secret

        checked = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        enc = encrypt_secret(refresh_token) if refresh_token else None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT premium_twitch_user_id, premium_twitch_refresh FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            uid = twitch_user_id if twitch_user_id is not None else (
                (row["premium_twitch_user_id"] if row else "") or ""
            )
            ref = enc if enc is not None else ((row["premium_twitch_refresh"] if row else "") or "")
            conn.execute(
                """
                INSERT INTO users (
                    user_id, premium_twitch_active, premium_twitch_user_id,
                    premium_twitch_refresh, premium_twitch_checked_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    premium_twitch_active = excluded.premium_twitch_active,
                    premium_twitch_user_id = excluded.premium_twitch_user_id,
                    premium_twitch_refresh = excluded.premium_twitch_refresh,
                    premium_twitch_checked_at = excluded.premium_twitch_checked_at
                """,
                (user_id, int(bool(active)), uid, ref, checked),
            )

    def set_premium_twitch_refresh(self, user_id: int, refresh_token: str) -> None:
        from token_crypto import encrypt_secret

        enc = encrypt_secret(refresh_token)
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, premium_twitch_refresh)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    premium_twitch_refresh = excluded.premium_twitch_refresh
                """,
                (user_id, enc),
            )

    def get_premium_twitch_refresh(self, user_id: int) -> str | None:
        from token_crypto import decrypt_secret

        with self._conn() as conn:
            row = conn.execute(
                "SELECT premium_twitch_refresh FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if not row or not row["premium_twitch_refresh"]:
            return None
        try:
            return decrypt_secret(row["premium_twitch_refresh"])
        except Exception:
            return None

    def list_premium_twitch_user_ids(self) -> list[int]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT user_id FROM users
                WHERE COALESCE(premium_twitch_refresh, '') != ''
                   OR COALESCE(premium_twitch_active, 0) = 1
                """
            ).fetchall()
        return [int(r["user_id"]) for r in rows]

    def add_scheduled_broadcast(
        self,
        msg_type: str,
        text: str,
        scheduled_at: str,
        created_by: int,
        recipient_ids: str = "",
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO scheduled_broadcasts
                    (msg_type, text, scheduled_at, created_by, recipient_ids)
                VALUES (?, ?, ?, ?, ?)
                """,
                (msg_type, text, scheduled_at, created_by, recipient_ids or ""),
            )
            return int(cur.lastrowid)

    def get_unsent_scheduled_broadcasts(self) -> list[ScheduledBroadcast]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, msg_type, text, scheduled_at, created_by,
                       COALESCE(recipient_ids, '') AS recipient_ids,
                       COALESCE(sent_utc_offsets, '') AS sent_utc_offsets,
                       COALESCE(sent_count, 0) AS sent_count
                FROM scheduled_broadcasts
                WHERE sent_at IS NULL
                ORDER BY scheduled_at
                """
            ).fetchall()
        return [_scheduled_broadcast_from_row(r) for r in rows]

    def get_pending_scheduled_broadcasts(self) -> list[ScheduledBroadcast]:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, msg_type, text, scheduled_at, created_by,
                       COALESCE(recipient_ids, '') AS recipient_ids,
                       COALESCE(sent_utc_offsets, '') AS sent_utc_offsets,
                       COALESCE(sent_count, 0) AS sent_count
                FROM scheduled_broadcasts
                WHERE sent_at IS NULL AND scheduled_at <= ?
                ORDER BY scheduled_at
                """,
                (now,),
            ).fetchall()
        return [_scheduled_broadcast_from_row(r) for r in rows]

    def get_scheduled_broadcast(self, broadcast_id: int) -> ScheduledBroadcast | None:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT id, msg_type, text, scheduled_at, created_by,
                       COALESCE(recipient_ids, '') AS recipient_ids,
                       COALESCE(sent_utc_offsets, '') AS sent_utc_offsets,
                       COALESCE(sent_count, 0) AS sent_count
                FROM scheduled_broadcasts
                WHERE id = ? AND sent_at IS NULL
                """,
                (broadcast_id,),
            ).fetchone()
        if not row:
            return None
        return _scheduled_broadcast_from_row(row)

    def update_scheduled_broadcast(self, broadcast_id: int, **fields: object) -> bool:
        allowed = {"text", "scheduled_at"}
        updates: list[str] = []
        values: list[object] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            updates.append(f"{key} = ?")
            values.append(value)
        if not updates:
            return self.get_scheduled_broadcast(broadcast_id) is not None
        values.append(broadcast_id)
        with self._conn() as conn:
            cur = conn.execute(
                f"UPDATE scheduled_broadcasts SET {', '.join(updates)} "
                "WHERE id = ? AND sent_at IS NULL",
                values,
            )
        return cur.rowcount > 0

    def delete_scheduled_broadcast(self, broadcast_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM scheduled_broadcasts WHERE id = ? AND sent_at IS NULL",
                (broadcast_id,),
            )
        return cur.rowcount > 0

    def mark_scheduled_broadcast_sent(self, broadcast_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                "UPDATE scheduled_broadcasts SET sent_at = ? WHERE id = ?",
                (now, broadcast_id),
            )

    def get_sent_broadcasts(self, *, retention_days: int = 30) -> list[ScheduledBroadcast]:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=int(retention_days))
        ).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, msg_type, text, scheduled_at, created_by,
                       COALESCE(recipient_ids, '') AS recipient_ids,
                       COALESCE(sent_utc_offsets, '') AS sent_utc_offsets,
                       COALESCE(sent_count, 0) AS sent_count,
                       sent_at
                FROM scheduled_broadcasts
                WHERE sent_at IS NOT NULL AND sent_at >= ?
                ORDER BY sent_at ASC
                """,
                (cutoff,),
            ).fetchall()
        return [_scheduled_broadcast_from_row(r) for r in rows]

    def purge_old_sent_broadcasts(self, *, retention_days: int = 30) -> int:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=int(retention_days))
        ).isoformat()
        with self._conn() as conn:
            old_ids = [
                int(r["id"])
                for r in conn.execute(
                    "SELECT id FROM scheduled_broadcasts WHERE sent_at IS NOT NULL AND sent_at < ?",
                    (cutoff,),
                ).fetchall()
            ]
            if not old_ids:
                return 0
            placeholders = ",".join("?" * len(old_ids))
            conn.execute(
                f"DELETE FROM broadcast_feedback WHERE broadcast_id IN ({placeholders})",
                old_ids,
            )
            conn.execute(
                f"DELETE FROM broadcast_deliveries WHERE broadcast_id IN ({placeholders})",
                old_ids,
            )
            cur = conn.execute(
                f"DELETE FROM scheduled_broadcasts WHERE id IN ({placeholders})",
                old_ids,
            )
        return int(cur.rowcount)

    def add_broadcast_delivery(
        self, broadcast_id: int, user_id: int, message_id: int
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO broadcast_deliveries (broadcast_id, user_id, message_id)
                VALUES (?, ?, ?)
                ON CONFLICT(broadcast_id, user_id) DO UPDATE SET message_id = excluded.message_id
                """,
                (broadcast_id, user_id, message_id),
            )

    def get_broadcast_deliveries(
        self, broadcast_id: int
    ) -> list[tuple[int, int]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT user_id, message_id FROM broadcast_deliveries
                WHERE broadcast_id = ?
                """,
                (broadcast_id,),
            ).fetchall()
        return [(int(r["user_id"]), int(r["message_id"])) for r in rows]

    def get_broadcast_feedback_vote(
        self, broadcast_id: int, user_id: int
    ) -> int | None:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT vote FROM broadcast_feedback
                WHERE broadcast_id = ? AND user_id = ?
                """,
                (broadcast_id, user_id),
            ).fetchone()
        if not row:
            return None
        vote = int(row["vote"])
        return vote if vote in (1, -1) else None

    def set_broadcast_feedback(
        self, broadcast_id: int, user_id: int, vote: int
    ) -> None:
        if vote not in (1, -1):
            return
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO broadcast_feedback (broadcast_id, user_id, vote)
                VALUES (?, ?, ?)
                ON CONFLICT(broadcast_id, user_id) DO UPDATE SET vote = excluded.vote
                """,
                (broadcast_id, user_id, vote),
            )

    def clear_broadcast_feedback(self, broadcast_id: int, user_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM broadcast_feedback WHERE broadcast_id = ? AND user_id = ?",
                (broadcast_id, user_id),
            )

    def get_broadcast_feedback_counts(self, broadcast_id: int) -> tuple[int, int]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT vote, COUNT(*) AS c FROM broadcast_feedback
                WHERE broadcast_id = ?
                GROUP BY vote
                """,
                (broadcast_id,),
            ).fetchall()
        up = down = 0
        for row in rows:
            if int(row["vote"]) == 1:
                up = int(row["c"])
            elif int(row["vote"]) == -1:
                down = int(row["c"])
        return up, down

    @staticmethod
    def _lucky_locale(locale: str) -> str:
        return "ru" if str(locale).lower().startswith("ru") else "en"

    def add_lucky_template(self, locale: str, text: str) -> None:
        loc = self._lucky_locale(locale)
        body = (text or "").strip()
        if not body:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO lucky_templates (locale, text, created_at) VALUES (?, ?, ?)",
                (loc, body, now),
            )
            rows = conn.execute(
                "SELECT id FROM lucky_templates WHERE locale = ? ORDER BY id DESC",
                (loc,),
            ).fetchall()
            if len(rows) > LUCKY_TEMPLATE_LIMIT:
                old_ids = [(int(r["id"]),) for r in rows[LUCKY_TEMPLATE_LIMIT:]]
                conn.executemany("DELETE FROM lucky_templates WHERE id = ?", old_ids)

    def pick_lucky_template(self, locale: str) -> str | None:
        loc = self._lucky_locale(locale)
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT text FROM lucky_templates
                WHERE locale = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (loc, LUCKY_TEMPLATE_LIMIT),
            ).fetchall()
        if not rows:
            return None
        return str(random.choice(rows)["text"])

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
            sys_other = conn.execute(
                """
                SELECT COUNT(*) AS c FROM (
                    SELECT user_id AS id FROM users
                    UNION
                    SELECT DISTINCT owner_id AS id FROM subscriptions
                ) AS n
                LEFT JOIN users u ON u.user_id = n.id
                WHERE COALESCE(u.receive_other_updates, 1) = 1
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
            premium_paid = conn.execute(
                """
                SELECT COUNT(*) AS c FROM users
                WHERE COALESCE(bot_blocked, 0) = 0
                  AND (
                    COALESCE(premium_stars_until, 0)
                      > CAST(strftime('%s', 'now') AS INTEGER)
                    OR COALESCE(premium_twitch_active, 0) = 1
                    OR (
                      COALESCE(premium_features, '') NOT IN ('', '{}')
                    )
                  )
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
            premium_paid=int(premium_paid),
            sys_updates=int(sys_updates),
            sys_availability=int(sys_availability),
            sys_other=int(sys_other),
            blocked_users=int(blocked_users),
            locale_en=int(locale_en),
            locale_ru=int(locale_ru),
            locale_unset=int(locale_unset),
        )

    def upsert_twitch_sync(
        self,
        owner_id: int,
        twitch_user_id: str,
        refresh_token: str,
        period_days: int,
        next_sync_at: str,
        last_sync_at: str | None = None,
    ) -> None:
        from token_crypto import encrypt_secret

        enc = encrypt_secret(refresh_token)
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO twitch_sync (
                    owner_id, twitch_user_id, refresh_token,
                    period_days, next_sync_at, last_sync_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_id) DO UPDATE SET
                    twitch_user_id = excluded.twitch_user_id,
                    refresh_token = excluded.refresh_token,
                    period_days = excluded.period_days,
                    next_sync_at = excluded.next_sync_at,
                    last_sync_at = excluded.last_sync_at
                """,
                (
                    owner_id,
                    twitch_user_id,
                    enc,
                    period_days,
                    next_sync_at,
                    last_sync_at,
                ),
            )

    def get_twitch_sync(self, owner_id: int) -> TwitchSync | None:
        from token_crypto import decrypt_secret

        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM twitch_sync WHERE owner_id = ?",
                (owner_id,),
            ).fetchone()
        if not row:
            return None
        sync = _row_to_twitch_sync(row)
        sync.refresh_token = decrypt_secret(sync.refresh_token)
        return sync

    def delete_twitch_sync(self, owner_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM twitch_sync WHERE owner_id = ?",
                (owner_id,),
            )
            return cur.rowcount > 0

    def set_twitch_sync_period(
        self, owner_id: int, period_days: int, next_sync_at: str
    ) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE twitch_sync
                SET period_days = ?, next_sync_at = ?
                WHERE owner_id = ?
                """,
                (period_days, next_sync_at, owner_id),
            )
            return cur.rowcount > 0

    def update_twitch_sync_tokens(
        self,
        owner_id: int,
        refresh_token: str,
        *,
        last_sync_at: str,
        next_sync_at: str,
    ) -> None:
        from token_crypto import encrypt_secret

        enc = encrypt_secret(refresh_token)
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE twitch_sync
                SET refresh_token = ?, last_sync_at = ?, next_sync_at = ?
                WHERE owner_id = ?
                """,
                (enc, last_sync_at, next_sync_at, owner_id),
            )

    def get_due_twitch_syncs(self, now_iso: str) -> list[TwitchSync]:
        from token_crypto import decrypt_secret

        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM twitch_sync
                WHERE next_sync_at <= ?
                ORDER BY next_sync_at
                """,
                (now_iso,),
            ).fetchall()
        out: list[TwitchSync] = []
        for r in rows:
            sync = _row_to_twitch_sync(r)
            sync.refresh_token = decrypt_secret(sync.refresh_token)
            out.append(sync)
        return out

    def get_whisper_alert(self, owner_id: int) -> WhisperAlert | None:
        from token_crypto import decrypt_secret

        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM whisper_alerts WHERE owner_id = ?",
                (owner_id,),
            ).fetchone()
        if not row:
            return None
        alert = _row_to_whisper_alert(row)
        alert.refresh_token = decrypt_secret(alert.refresh_token)
        return alert

    def get_whisper_alerts_by_twitch_user_id(
        self, twitch_user_id: str
    ) -> list[WhisperAlert]:
        from token_crypto import decrypt_secret

        if not twitch_user_id:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM whisper_alerts
                WHERE twitch_user_id = ? AND enabled = 1
                ORDER BY owner_id
                """,
                (twitch_user_id,),
            ).fetchall()
        out: list[WhisperAlert] = []
        for row in rows:
            alert = _row_to_whisper_alert(row)
            alert.refresh_token = decrypt_secret(alert.refresh_token)
            out.append(alert)
        return out

    def upsert_whisper_alert(
        self,
        owner_id: int,
        *,
        enabled: bool,
        twitch_user_id: str,
        twitch_login: str,
        refresh_token: str,
        eventsub_id: str = "",
    ) -> None:
        from token_crypto import encrypt_secret

        enc = encrypt_secret(refresh_token) if refresh_token else ""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO whisper_alerts (
                    owner_id, enabled, twitch_user_id, twitch_login,
                    refresh_token, eventsub_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    twitch_user_id = excluded.twitch_user_id,
                    twitch_login = excluded.twitch_login,
                    refresh_token = excluded.refresh_token,
                    eventsub_id = excluded.eventsub_id
                """,
                (
                    owner_id,
                    1 if enabled else 0,
                    twitch_user_id,
                    twitch_login,
                    enc,
                    eventsub_id,
                ),
            )

    def set_whisper_alert_enabled(
        self,
        owner_id: int,
        enabled: bool,
        *,
        eventsub_id: str | None = None,
    ) -> None:
        with self._conn() as conn:
            if eventsub_id is None:
                conn.execute(
                    "UPDATE whisper_alerts SET enabled = ? WHERE owner_id = ?",
                    (1 if enabled else 0, owner_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE whisper_alerts
                    SET enabled = ?, eventsub_id = ?
                    WHERE owner_id = ?
                    """,
                    (1 if enabled else 0, eventsub_id, owner_id),
                )

    def disable_whisper_alerts_for_twitch_user(self, twitch_user_id: str) -> list[int]:
        if not twitch_user_id:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT owner_id FROM whisper_alerts
                WHERE twitch_user_id = ? AND enabled = 1
                """,
                (twitch_user_id,),
            ).fetchall()
            conn.execute(
                """
                UPDATE whisper_alerts
                SET enabled = 0, eventsub_id = ''
                WHERE twitch_user_id = ?
                """,
                (twitch_user_id,),
            )
        return [int(r["owner_id"]) for r in rows]

    def get_chat_auth(self, owner_id: int) -> ChatAuth | None:
        from token_crypto import decrypt_secret

        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM chat_auth WHERE owner_id = ?",
                (owner_id,),
            ).fetchone()
        if not row:
            return None
        auth = _row_to_chat_auth(row)
        auth.refresh_token = decrypt_secret(auth.refresh_token)
        return auth

    def delete_whisper_alert(self, owner_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM whisper_alerts WHERE owner_id = ?", (owner_id,))

    def delete_chat_auth(self, owner_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM chat_auth WHERE owner_id = ?", (owner_id,))

    def upsert_chat_auth(
        self,
        owner_id: int,
        *,
        twitch_user_id: str,
        twitch_login: str,
        refresh_token: str,
    ) -> None:
        from token_crypto import encrypt_secret

        enc = encrypt_secret(refresh_token) if refresh_token else ""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO chat_auth (
                    owner_id, twitch_user_id, twitch_login, refresh_token
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(owner_id) DO UPDATE SET
                    twitch_user_id = excluded.twitch_user_id,
                    twitch_login = excluded.twitch_login,
                    refresh_token = excluded.refresh_token
                """,
                (owner_id, twitch_user_id, twitch_login, enc),
            )

    def get_chat_send_count(self, owner_id: int, day: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT count FROM chat_send_daily WHERE owner_id = ? AND day = ?",
                (owner_id, day),
            ).fetchone()
        return int(row["count"]) if row else 0

    def increment_chat_send_count(self, owner_id: int, day: str) -> int:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO chat_send_daily (owner_id, day, count)
                VALUES (?, ?, 1)
                ON CONFLICT(owner_id, day) DO UPDATE SET
                    count = chat_send_daily.count + 1
                """,
                (owner_id, day),
            )
            row = conn.execute(
                "SELECT count FROM chat_send_daily WHERE owner_id = ? AND day = ?",
                (owner_id, day),
            ).fetchone()
        return int(row["count"]) if row else 1

    def delete_synced_subscriptions_missing(
        self, owner_id: int, keep_twitch_user_ids: set[str], *, to_cart: bool = True
    ) -> list[str]:
        """Delete pristine (unedited) sync-origin subs not in keep set."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM subscriptions
                WHERE owner_id = ? AND from_twitch_sync = 1
                  AND COALESCE(sync_user_edited, 0) = 0
                """,
                (owner_id,),
            ).fetchall()
            removed_logins: dict[str, str] = {}
            for r in rows:
                if str(r["twitch_user_id"]) in keep_twitch_user_ids:
                    continue
                sub = _row_to_sub(r)
                login = str(sub.twitch_username or sub.twitch_user_id or "").strip()
                if login:
                    removed_logins.setdefault(login.lower(), login)
                if to_cart:
                    payload = _subscription_cart_snapshot(sub)
                    deleted_at = datetime.now(timezone.utc).isoformat()
                    conn.execute(
                        """
                        INSERT INTO deleted_subscriptions_cart (
                            owner_id, is_demo, deleted_at, subscription_json
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            owner_id,
                            int(bool(sub.is_demo)),
                            deleted_at,
                            json.dumps(payload, ensure_ascii=False),
                        ),
                    )
                conn.execute(
                    "DELETE FROM subscriptions WHERE id = ? AND owner_id = ?",
                    (int(sub.id), owner_id),
                )
            return [removed_logins[k] for k in sorted(removed_logins)]

    def get_unfollowed_manual_alert_streamers(
        self,
        owner_id: int,
        keep_twitch_user_ids: set[str],
        *,
        is_demo: bool = False,
    ) -> list[dict[str, str]]:
        """Streamers not in follows that still have non-category-watch alerts."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT twitch_user_id, twitch_username FROM subscriptions
                WHERE owner_id = ? AND is_demo = ?
                  AND COALESCE(category_watch_prefs, '') = ''
                  AND twitch_user_id NOT LIKE 'cw:%'
                """,
                (owner_id, int(bool(is_demo))),
            ).fetchall()
        by_uid: dict[str, str] = {}
        for r in rows:
            uid = str(r["twitch_user_id"] or "").strip()
            if not uid or uid in keep_twitch_user_ids:
                continue
            login = str(r["twitch_username"] or "").strip().lower() or uid
            by_uid.setdefault(uid, login)
        return [
            {"user_id": uid, "user_login": login}
            for uid, login in sorted(by_uid.items(), key=lambda x: x[1])
        ]

    def delete_subscriptions_for_twitch_users(
        self,
        owner_id: int,
        twitch_user_ids: set[str],
        *,
        is_demo: bool = False,
        to_cart: bool = True,
    ) -> int:
        ids = {str(u).strip() for u in twitch_user_ids if str(u).strip()}
        if not ids:
            return 0
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM subscriptions
                WHERE owner_id = ? AND is_demo = ?
                """,
                (owner_id, int(bool(is_demo))),
            ).fetchall()
            removed = 0
            for r in rows:
                if str(r["twitch_user_id"]) not in ids:
                    continue
                sub = _row_to_sub(r)
                if to_cart:
                    payload = _subscription_cart_snapshot(sub)
                    deleted_at = datetime.now(timezone.utc).isoformat()
                    conn.execute(
                        """
                        INSERT INTO deleted_subscriptions_cart (
                            owner_id, is_demo, deleted_at, subscription_json
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            owner_id,
                            int(bool(sub.is_demo)),
                            deleted_at,
                            json.dumps(payload, ensure_ascii=False),
                        ),
                    )
                conn.execute(
                    "DELETE FROM subscriptions WHERE id = ? AND owner_id = ?",
                    (int(sub.id), owner_id),
                )
                removed += 1
            return removed

    def beta_enrollment_explicit(self, user_id: int, feature_id: str) -> bool | None:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT enrolled FROM user_beta_enrollments
                WHERE user_id = ? AND feature_id = ?
                """,
                (user_id, feature_id),
            ).fetchone()
        if row is None:
            return None
        return bool(row["enrolled"])

    def set_beta_enrollment(
        self, user_id: int, feature_id: str, enrolled: bool
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            if enrolled:
                conn.execute(
                    """
                    INSERT INTO user_beta_enrollments
                        (user_id, feature_id, enrolled, opted_in_at, opted_out_at)
                    VALUES (?, ?, 1, ?, NULL)
                    ON CONFLICT(user_id, feature_id) DO UPDATE SET
                        enrolled = 1,
                        opted_in_at = excluded.opted_in_at,
                        opted_out_at = NULL
                    """,
                    (user_id, feature_id, now),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO user_beta_enrollments
                        (user_id, feature_id, enrolled, opted_in_at, opted_out_at)
                    VALUES (?, ?, 0, ?, ?)
                    ON CONFLICT(user_id, feature_id) DO UPDATE SET
                        enrolled = 0,
                        opted_out_at = excluded.opted_out_at
                    """,
                    (user_id, feature_id, now, now),
                )

    def clear_beta_enrollment(self, user_id: int, feature_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM user_beta_enrollments WHERE user_id = ? AND feature_id = ?",
                (user_id, feature_id),
            )

    def list_beta_enrolled_user_ids(self, feature_ids: list[str]) -> list[int]:
        unique = list(dict.fromkeys(str(fid) for fid in feature_ids if str(fid)))
        if not unique:
            return []
        placeholders = ",".join("?" for _ in unique)
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT e.user_id AS user_id
                FROM user_beta_enrollments e
                LEFT JOIN users u ON u.user_id = e.user_id
                WHERE e.enrolled = 1
                  AND e.feature_id IN ({placeholders})
                  AND COALESCE(u.bot_blocked, 0) = 0
                """,
                unique,
            ).fetchall()
        return sorted(int(r["user_id"]) for r in rows)

    def is_premium_channel_login(self, login: str) -> bool:
        key = (login or "").strip().lower()
        if not key:
            return False
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM premium_channels WHERE twitch_login = ?",
                (key,),
            ).fetchone()
        return row is not None

    def list_premium_channel_logins(self) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT twitch_login FROM premium_channels ORDER BY twitch_login"
            ).fetchall()
        return [str(r["twitch_login"]).lower() for r in rows]

    def list_premium_channels(self) -> list[PremiumChannel]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT twitch_user_id, twitch_login, display_name,
                       owner_telegram_id, charge_id, paid_at
                FROM premium_channels
                ORDER BY paid_at DESC
                """
            ).fetchall()
        return [
            PremiumChannel(
                twitch_user_id=str(r["twitch_user_id"]),
                twitch_login=str(r["twitch_login"]).lower(),
                display_name=str(r["display_name"] or r["twitch_login"]),
                owner_telegram_id=int(r["owner_telegram_id"]),
                charge_id=str(r["charge_id"] or ""),
                paid_at=str(r["paid_at"] or ""),
            )
            for r in rows
        ]

    def get_premium_channel(self, twitch_user_id: str) -> PremiumChannel | None:
        uid = str(twitch_user_id or "").strip()
        if not uid:
            return None
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT twitch_user_id, twitch_login, display_name,
                       owner_telegram_id, charge_id, paid_at
                FROM premium_channels WHERE twitch_user_id = ?
                """,
                (uid,),
            ).fetchone()
        if not row:
            return None
        return PremiumChannel(
            twitch_user_id=str(row["twitch_user_id"]),
            twitch_login=str(row["twitch_login"]).lower(),
            display_name=str(row["display_name"] or row["twitch_login"]),
            owner_telegram_id=int(row["owner_telegram_id"]),
            charge_id=str(row["charge_id"] or ""),
            paid_at=str(row["paid_at"] or ""),
        )

    def upsert_premium_channel(
        self,
        *,
        twitch_user_id: str,
        twitch_login: str,
        display_name: str,
        owner_telegram_id: int,
        charge_id: str,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO premium_channels (
                    twitch_user_id, twitch_login, display_name,
                    owner_telegram_id, charge_id, paid_at
                ) VALUES (?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(twitch_user_id) DO UPDATE SET
                    twitch_login = excluded.twitch_login,
                    display_name = excluded.display_name,
                    owner_telegram_id = excluded.owner_telegram_id,
                    charge_id = excluded.charge_id,
                    paid_at = datetime('now')
                """,
                (
                    str(twitch_user_id),
                    str(twitch_login).strip().lower(),
                    str(display_name or twitch_login),
                    int(owner_telegram_id),
                    str(charge_id or ""),
                ),
            )

    def get_premium_channel_by_charge(self, charge_id: str) -> PremiumChannel | None:
        cid = str(charge_id or "").strip()
        if not cid:
            return None
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT twitch_user_id, twitch_login, display_name,
                       owner_telegram_id, charge_id, paid_at
                FROM premium_channels
                WHERE charge_id = ?
                """,
                (cid,),
            ).fetchone()
        if not row:
            return None
        return PremiumChannel(
            twitch_user_id=str(row["twitch_user_id"]),
            twitch_login=str(row["twitch_login"]).lower(),
            display_name=str(row["display_name"] or row["twitch_login"]),
            owner_telegram_id=int(row["owner_telegram_id"]),
            charge_id=str(row["charge_id"] or ""),
            paid_at=str(row["paid_at"] or ""),
        )

    def delete_premium_channel_by_charge(self, charge_id: str) -> bool:
        cid = str(charge_id or "").strip()
        if not cid:
            return False
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM premium_channels WHERE charge_id = ?",
                (cid,),
            )
            return int(cur.rowcount) > 0

    def find_user_id_by_premium_charge(self, charge_id: str) -> int | None:
        from premium import parse_premium_features_blob

        cid = str(charge_id or "").strip()
        if not cid:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT user_id FROM users WHERE premium_stars_charge_id = ?",
                (cid,),
            ).fetchone()
            if row:
                return int(row["user_id"])
            row = conn.execute(
                """
                SELECT owner_telegram_id AS user_id
                FROM premium_channels WHERE charge_id = ?
                """,
                (cid,),
            ).fetchone()
            if row:
                return int(row["user_id"])
            row = conn.execute(
                "SELECT invitee_id AS user_id FROM referral_credits WHERE charge_id = ?",
                (cid,),
            ).fetchone()
            if row:
                return int(row["user_id"])
            for r in conn.execute(
                """
                SELECT user_id, premium_features FROM users
                WHERE instr(premium_features, ?) > 0
                """,
                (cid,),
            ).fetchall():
                _features, charges, _canceled = parse_premium_features_blob(
                    r["premium_features"] or ""
                )
                if cid in charges.values():
                    return int(r["user_id"])
        return None

    def get_referral_credit_by_charge(
        self, charge_id: str
    ) -> ReferralCreditRef | None:
        cid = str(charge_id or "").strip()
        if not cid:
            return None
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT referrer_id, invitee_id, charge_id, stars_paid, commission_stars
                FROM referral_credits WHERE charge_id = ?
                """,
                (cid,),
            ).fetchone()
        if not row:
            return None
        return ReferralCreditRef(
            referrer_id=int(row["referrer_id"]),
            invitee_id=int(row["invitee_id"]),
            charge_id=str(row["charge_id"] or ""),
            stars_paid=int(row["stars_paid"] or 0),
            commission_stars=int(row["commission_stars"] or 0),
        )

    def delete_referral_credit_by_charge(self, charge_id: str) -> bool:
        cid = str(charge_id or "").strip()
        if not cid:
            return False
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM referral_credits WHERE charge_id = ?",
                (cid,),
            )
            return int(cur.rowcount) > 0

    def ensure_alert_share_token(
        self, owner_id: int, source_sub_id: int, snapshot: dict[str, Any]
    ) -> str:
        payload = json.dumps(snapshot, ensure_ascii=False)
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT token FROM alert_share_tokens
                WHERE source_sub_id = ?
                """,
                (int(source_sub_id),),
            ).fetchone()
            if row:
                token = str(row["token"])
                conn.execute(
                    """
                    UPDATE alert_share_tokens
                    SET owner_id = ?, snapshot_json = ?
                    WHERE token = ?
                    """,
                    (int(owner_id), payload, token),
                )
                return token
            for _ in range(8):
                token = secrets.token_urlsafe(12)
                try:
                    conn.execute(
                        """
                        INSERT INTO alert_share_tokens (
                            token, owner_id, source_sub_id, snapshot_json
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (token, int(owner_id), int(source_sub_id), payload),
                    )
                    return token
                except sqlite3.IntegrityError:
                    continue
            raise RuntimeError("failed to allocate alert share token")

    def get_alert_share_snapshot(self, token: str) -> dict[str, Any] | None:
        raw = (token or "").strip()
        if not raw:
            return None
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT snapshot_json FROM alert_share_tokens
                WHERE token = ?
                """,
                (raw,),
            ).fetchone()
        if not row:
            return None
        try:
            data = json.loads(row["snapshot_json"] or "{}")
        except Exception:
            return None
        return data if isinstance(data, dict) else None
