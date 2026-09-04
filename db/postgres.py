from __future__ import annotations

import json
import logging
import random
import secrets
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

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
    _seed_lucky_templates_pg,
    _subscription_cart_snapshot,
    dump_watch_filters,
    parse_watch_filters,
    watch_filter_auto_name,
)

logger = logging.getLogger(__name__)

def _normalize_pg_url(database_url: str) -> str:
    url = database_url.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("sslmode", "require")
    return urlunparse(parsed._replace(query=urlencode(query)))


class PostgresDatabase:
    def __init__(self, database_url: str) -> None:
        import psycopg  # noqa: PLC0415

        self._psycopg = psycopg
        self._dsn = _normalize_pg_url(database_url)
        self._lock = threading.Lock()
        self._pooled: Any | None = None
        self._init_schema()
        logger.info("Database: PostgreSQL (DATABASE_URL)")

    def _ensure_pooled(self) -> Any:
        conn = self._pooled
        if conn is not None and not conn.closed:
            return conn
        self._pooled = self._psycopg.connect(self._dsn, connect_timeout=30)
        return self._pooled

    @contextmanager
    def _conn(self) -> Iterator[Any]:
        # Reuse one connection under a lock — avoids TCP handshake per query on VPS.
        with self._lock:
            conn = self._ensure_pooled()
            try:
                yield conn
                conn.commit()
            except Exception:
                try:
                    if not conn.closed:
                        conn.rollback()
                except Exception:
                    self._pooled = None
                if getattr(conn, "closed", False):
                    self._pooled = None
                raise

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
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS use_global_ignore BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS image_file_id TEXT
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS image_position TEXT NOT NULL DEFAULT ''
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS from_twitch_sync BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS from_watch_suggest BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS sync_user_edited BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS category_watch_prefs TEXT NOT NULL DEFAULT ''
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS category_watch_live_ids TEXT NOT NULL DEFAULT ''
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS category_watch_primed BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS schedule_reminder_minutes INTEGER NOT NULL DEFAULT 0
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS last_schedule_reminder_segment_id TEXT
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS schedule_reminder_configured
                BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            cur.execute(
                """
                UPDATE subscriptions
                SET schedule_reminder_configured = TRUE
                WHERE schedule_reminder_minutes > 0
                  AND schedule_reminder_configured = FALSE
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS notify_on_live BOOLEAN NOT NULL DEFAULT TRUE
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS notify_on_end BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS notify_on_category_change
                BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS delete_other_alerts
                BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS last_message_at TIMESTAMPTZ
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS is_demo BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS trial_paused BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS delivery_paused BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS strip_name_mentions
                BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS attach_chat_button
                BOOLEAN NOT NULL DEFAULT FALSE
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
                ADD COLUMN IF NOT EXISTS receive_other_updates BOOLEAN NOT NULL DEFAULT TRUE
                """
            )
            cur.execute(
                """
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS receive_sync_updates BOOLEAN NOT NULL DEFAULT TRUE
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
                ADD COLUMN IF NOT EXISTS bot_blocked_at BIGINT
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_users_bot_blocked_at
                ON users(bot_blocked_at)
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
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS schedule_utc_offset_minutes INTEGER
                """
            )
            cur.execute(
                """
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS watch_prefs TEXT NOT NULL DEFAULT ''
                """
            )
            cur.execute(
                """
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS global_ignore_keywords TEXT NOT NULL DEFAULT ''
                """
            )
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'users'
                      AND column_name = 'premium_permanent'
                ) AS had_col
                """
            )
            had_premium = bool(cur.fetchone()["had_col"])
            cur.execute(
                """
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS premium_permanent BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            for col_sql in (
                "premium_stars_charge_id TEXT NOT NULL DEFAULT ''",
                "premium_stars_until BIGINT NOT NULL DEFAULT 0",
                "premium_stars_canceled BOOLEAN NOT NULL DEFAULT FALSE",
                "premium_twitch_user_id TEXT NOT NULL DEFAULT ''",
                "premium_twitch_refresh TEXT NOT NULL DEFAULT ''",
                "premium_twitch_active BOOLEAN NOT NULL DEFAULT FALSE",
                "premium_twitch_checked_at TIMESTAMPTZ",
                "premium_stars_paid_at TIMESTAMPTZ",
                "referred_by BIGINT",
                "premium_trial_until BIGINT NOT NULL DEFAULT 0",
                "premium_trial_used BOOLEAN NOT NULL DEFAULT FALSE",
                "premium_features TEXT NOT NULL DEFAULT ''",
                "advanced_mode INTEGER",
                "notifications_paused_until BIGINT NOT NULL DEFAULT 0",
                "template_typo_notice_sent BOOLEAN NOT NULL DEFAULT FALSE",
            ):
                cur.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col_sql}")
            if not had_premium:
                cur.execute("UPDATE users SET premium_permanent = TRUE")
                cur.execute(
                    """
                    INSERT INTO users (user_id, premium_permanent)
                    SELECT DISTINCT s.owner_id, TRUE
                    FROM subscriptions s
                    WHERE NOT EXISTS (
                        SELECT 1 FROM users u WHERE u.user_id = s.owner_id
                    )
                    ON CONFLICT (user_id) DO UPDATE SET premium_permanent = TRUE
                    """
                )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS referral_credits (
                    id SERIAL PRIMARY KEY,
                    referrer_id BIGINT NOT NULL,
                    invitee_id BIGINT NOT NULL,
                    charge_id TEXT NOT NULL UNIQUE,
                    stars_paid INTEGER NOT NULL,
                    commission_stars INTEGER NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_referral_credits_referrer
                ON referral_credits(referrer_id)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS referral_withdrawals (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    amount INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    resolved_at TIMESTAMPTZ
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_referral_withdrawals_user
                ON referral_withdrawals(user_id)
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
            cur.execute(
                """
                ALTER TABLE scheduled_broadcasts
                ADD COLUMN IF NOT EXISTS recipient_ids TEXT NOT NULL DEFAULT ''
                """
            )
            cur.execute(
                """
                ALTER TABLE scheduled_broadcasts
                ADD COLUMN IF NOT EXISTS sent_utc_offsets TEXT NOT NULL DEFAULT ''
                """
            )
            cur.execute(
                """
                ALTER TABLE scheduled_broadcasts
                ADD COLUMN IF NOT EXISTS sent_count INTEGER NOT NULL DEFAULT 0
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS broadcast_deliveries (
                    broadcast_id INTEGER NOT NULL REFERENCES scheduled_broadcasts(id) ON DELETE CASCADE,
                    user_id BIGINT NOT NULL,
                    message_id BIGINT NOT NULL,
                    PRIMARY KEY (broadcast_id, user_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS broadcast_feedback (
                    broadcast_id INTEGER NOT NULL REFERENCES scheduled_broadcasts(id) ON DELETE CASCADE,
                    user_id BIGINT NOT NULL,
                    vote SMALLINT NOT NULL CHECK (vote IN (1, -1)),
                    PRIMARY KEY (broadcast_id, user_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS lucky_templates (
                    id SERIAL PRIMARY KEY,
                    locale TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS twitch_sync (
                    owner_id BIGINT PRIMARY KEY,
                    twitch_user_id TEXT NOT NULL,
                    refresh_token TEXT NOT NULL,
                    period_days INTEGER NOT NULL,
                    next_sync_at TIMESTAMPTZ NOT NULL,
                    last_sync_at TIMESTAMPTZ
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS whisper_alerts (
                    owner_id BIGINT PRIMARY KEY,
                    enabled BOOLEAN NOT NULL DEFAULT FALSE,
                    twitch_user_id TEXT NOT NULL DEFAULT '',
                    twitch_login TEXT NOT NULL DEFAULT '',
                    refresh_token TEXT NOT NULL DEFAULT '',
                    eventsub_id TEXT NOT NULL DEFAULT ''
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_whisper_alerts_twitch_user
                ON whisper_alerts(twitch_user_id)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_auth (
                    owner_id BIGINT PRIMARY KEY,
                    twitch_user_id TEXT NOT NULL DEFAULT '',
                    twitch_login TEXT NOT NULL DEFAULT '',
                    refresh_token TEXT NOT NULL DEFAULT ''
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_send_daily (
                    owner_id BIGINT NOT NULL,
                    day TEXT NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (owner_id, day)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS alert_history (
                    id SERIAL PRIMARY KEY,
                    owner_id BIGINT NOT NULL,
                    subscription_id BIGINT,
                    twitch_username TEXT NOT NULL,
                    alert_type TEXT NOT NULL,
                    message_text TEXT NOT NULL DEFAULT '',
                    twitch_user_id TEXT NOT NULL DEFAULT '',
                    stream_id TEXT NOT NULL DEFAULT '',
                    vod_id TEXT NOT NULL DEFAULT '',
                    vod_offset_seconds INTEGER,
                    viewed BOOLEAN NOT NULL DEFAULT FALSE,
                    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                ALTER TABLE alert_history
                ADD COLUMN IF NOT EXISTS message_text TEXT NOT NULL DEFAULT ''
                """
            )
            cur.execute(
                """
                ALTER TABLE alert_history
                ADD COLUMN IF NOT EXISTS twitch_user_id TEXT NOT NULL DEFAULT ''
                """
            )
            cur.execute(
                """
                ALTER TABLE alert_history
                ADD COLUMN IF NOT EXISTS stream_id TEXT NOT NULL DEFAULT ''
                """
            )
            cur.execute(
                """
                ALTER TABLE alert_history
                ADD COLUMN IF NOT EXISTS vod_id TEXT NOT NULL DEFAULT ''
                """
            )
            cur.execute(
                """
                ALTER TABLE alert_history
                ADD COLUMN IF NOT EXISTS vod_offset_seconds INTEGER
                """
            )
            cur.execute(
                """
                ALTER TABLE alert_history
                ADD COLUMN IF NOT EXISTS viewed BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_alert_history_owner_sent
                ON alert_history(owner_id, sent_at DESC)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS deleted_subscriptions_cart (
                    id SERIAL PRIMARY KEY,
                    owner_id BIGINT NOT NULL,
                    is_demo BOOLEAN NOT NULL DEFAULT FALSE,
                    deleted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    subscription_json TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_del_subs_cart_owner_demo_deleted
                ON deleted_subscriptions_cart(owner_id, is_demo, deleted_at DESC)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_beta_enrollments (
                    user_id BIGINT NOT NULL,
                    feature_id TEXT NOT NULL,
                    enrolled BOOLEAN NOT NULL DEFAULT TRUE,
                    opted_in_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    opted_out_at TIMESTAMPTZ,
                    PRIMARY KEY (user_id, feature_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS premium_channels (
                    twitch_user_id TEXT PRIMARY KEY,
                    twitch_login TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    owner_telegram_id BIGINT NOT NULL,
                    charge_id TEXT NOT NULL DEFAULT '',
                    paid_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_premium_channels_login
                ON premium_channels(twitch_login)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS alert_share_tokens (
                    token TEXT PRIMARY KEY,
                    owner_id BIGINT NOT NULL,
                    source_sub_id BIGINT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_alert_share_tokens_sub
                ON alert_share_tokens(source_sub_id)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS unreachable_chats (
                    chat_id BIGINT PRIMARY KEY,
                    marked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            _seed_lucky_templates_pg(cur)

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
            cur = self._cursor(conn)
            cur.execute(
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
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                    bool(strip_name_mentions),
                    bool(attach_chat_button),
                    max(0, int(delay_minutes)),
                    max(0, int(suppress_repeat_minutes)),
                    max(0, int(schedule_reminder_minutes)),
                    bool(schedule_reminder_configured) or int(schedule_reminder_minutes) > 0,
                    ignore_keywords,
                    bool(use_global_ignore),
                    image_file_id or None,
                    (image_position or "") if image_file_id else "",
                    enabled,
                    from_twitch_sync,
                    bool(from_watch_suggest),
                    str(category_watch_prefs or ""),
                    bool(notify_on_live),
                    bool(notify_on_end),
                    bool(notify_on_category_change),
                    bool(delete_other_alerts),
                    bool(is_demo),
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

    def set_last_message_id(self, sub_id: int, message_id: int | None) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            if message_id is None:
                cur.execute(
                    """
                    UPDATE subscriptions
                    SET last_message_id = NULL, last_message_at = NULL
                    WHERE id = %s
                    """,
                    (sub_id,),
                )
            else:
                cur.execute(
                    """
                    UPDATE subscriptions
                    SET last_message_id = %s, last_message_at = NOW()
                    WHERE id = %s
                    """,
                    (message_id, sub_id),
                )

    def get_subs_due_previous_message_purge(
        self, older_than: datetime
    ) -> list[Subscription]:
        cutoff = older_than.astimezone(timezone.utc)
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT * FROM subscriptions
                WHERE COALESCE(delete_previous, FALSE)
                  AND last_message_id IS NOT NULL
                  AND dest_type != 'dm'
                  AND (
                    last_message_at IS NULL
                    OR last_message_at <= %s
                  )
                ORDER BY id
                """,
                (cutoff,),
            )
            rows = cur.fetchall()
        return [_row_to_sub(r) for r in rows]

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

    def set_last_schedule_reminder_segment(self, sub_id: int, segment_id: str) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "UPDATE subscriptions SET last_schedule_reminder_segment_id = %s WHERE id = %s",
                (segment_id, sub_id),
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
            if new_state:
                cur.execute(
                    """
                    UPDATE subscriptions
                    SET enabled = TRUE, delivery_paused = FALSE
                    WHERE id = %s AND owner_id = %s
                    """,
                    (sub_id, owner_id),
                )
            else:
                cur.execute(
                    "UPDATE subscriptions SET enabled = FALSE WHERE id = %s AND owner_id = %s",
                    (sub_id, owner_id),
                )
        return new_state

    def enable_all_subscriptions(
        self, owner_id: int, *, demo: bool = False, max_count: int | None = None
    ) -> int:
        sub = """
            SELECT id FROM subscriptions
            WHERE owner_id = %s AND enabled = FALSE AND is_demo = %s
              AND trial_paused = FALSE
              AND COALESCE(delivery_paused, FALSE) = FALSE
            ORDER BY id
        """
        params: list[object] = [owner_id, bool(demo)]
        if max_count is not None:
            sub += " LIMIT %s"
            params.append(max(0, int(max_count)))
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                f"UPDATE subscriptions SET enabled = TRUE WHERE id IN ({sub})",
                params,
            )
            return int(cur.rowcount)

    def delete_subscription(self, sub_id: int, owner_id: int, *, to_cart: bool = True) -> bool:
        with self._conn() as conn:
            cur = self._cursor(conn)
            row = cur.execute(
                "SELECT * FROM subscriptions WHERE id = %s AND owner_id = %s",
                (sub_id, owner_id),
            ).fetchone()
            if not row:
                return False
            sub = _row_to_sub(row)
            if to_cart:
                payload = _subscription_cart_snapshot(sub)
                deleted_at = datetime.now(timezone.utc)
                cur.execute(
                    """
                    INSERT INTO deleted_subscriptions_cart (
                        owner_id, is_demo, deleted_at, subscription_json
                    ) VALUES (%s, %s, %s, %s)
                    """,
                    (
                        owner_id,
                        bool(sub.is_demo),
                        deleted_at,
                        json.dumps(payload, ensure_ascii=False),
                    ),
                )
            cur.execute(
                "DELETE FROM subscriptions WHERE id = %s AND owner_id = %s",
                (sub_id, owner_id),
            )
            deleted = cur.rowcount > 0
        return deleted

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

        max_cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        effective_cutoff = max(cutoff, max_cutoff)

        with self._conn() as conn:
            cur = self._cursor(conn)
            rows = cur.execute(
                """
                SELECT id, deleted_at, subscription_json
                FROM deleted_subscriptions_cart
                WHERE owner_id = %s
                  AND is_demo = %s
                  AND deleted_at >= %s
                ORDER BY deleted_at DESC
                LIMIT %s
                """,
                (owner_id, bool(is_demo), effective_cutoff, int(limit)),
            ).fetchall()

        return [
            _cart_item_from_row(int(r["id"]), r.get("deleted_at"), r.get("subscription_json"))
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
        cart_ids = [int(i) for i in cart_ids if str(i)]
        if not cart_ids:
            return 0, 0

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        max_cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        effective_cutoff = max(cutoff, max_cutoff)

        with self._conn() as conn:
            cur = self._cursor(conn)
            placeholders = ",".join(["%s" for _ in cart_ids])
            rows = cur.execute(
                f"""
                SELECT id, subscription_json
                FROM deleted_subscriptions_cart
                WHERE owner_id = %s
                  AND is_demo = %s
                  AND id IN ({placeholders})
                  AND deleted_at >= %s
                """,
                (owner_id, bool(is_demo), *cart_ids, effective_cutoff),
            ).fetchall()

        restored_ids: list[int] = []
        enabled_restored = 0
        slots_used = 0
        for r in rows:
            try:
                payload = json.loads(r.get("subscription_json") or "{}")
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
                self.add_subscription(owner_id=owner_id, **payload)
            restored_ids.append(int(r["id"]))

        if not restored_ids:
            return 0, 0
        with self._conn() as conn:
            cur = self._cursor(conn)
            del_placeholders = ",".join(["%s" for _ in restored_ids])
            cur.execute(
                f"""
                DELETE FROM deleted_subscriptions_cart
                WHERE owner_id = %s
                  AND is_demo = %s
                  AND id IN ({del_placeholders})
                """,
                (owner_id, bool(is_demo), *restored_ids),
            )

        return len(restored_ids), enabled_restored

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
            updates.append(f"{key} = %s")
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
                values.append(bool(value))
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
            cur = self._cursor(conn)
            cur.execute(
                f"UPDATE subscriptions SET {', '.join(updates)} "
                "WHERE id = %s AND owner_id = %s",
                values,
            )
            updated = cur.rowcount > 0
            if updated and mark_sync_edited:
                cur.execute(
                    """
                    UPDATE subscriptions SET sync_user_edited = TRUE
                    WHERE id = %s AND owner_id = %s AND from_twitch_sync = TRUE
                    """,
                    (sub_id, owner_id),
                )
        return updated

    def get_user_locale(self, user_id: int) -> str | None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute("SELECT locale FROM users WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
        if not row or not row["locale"]:
            return None
        return str(row["locale"])

    def get_user_locales(self, user_ids: list[int]) -> dict[int, str | None]:
        if not user_ids:
            return {}
        unique = list(dict.fromkeys(int(uid) for uid in user_ids))
        out: dict[int, str | None] = {uid: None for uid in unique}
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT user_id, locale FROM users WHERE user_id = ANY(%s)",
                (unique,),
            )
            rows = cur.fetchall()
        for row in rows:
            loc = row["locale"]
            out[int(row["user_id"])] = str(loc) if loc else None
        return out

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
                  AND COALESCE(category_watch_prefs, '') = ''
                  AND twitch_user_id NOT LIKE 'cw:%'
                  AND (
                    notify_on_live = TRUE
                    OR notify_on_end = TRUE
                    OR notify_on_category_change = TRUE
                )
                """
            )
            rows = cur.fetchall()
        return [r["twitch_user_id"] for r in rows]

    def get_enabled_category_watch_subscriptions(self) -> list[Subscription]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT * FROM subscriptions
                WHERE enabled = TRUE
                  AND notify_on_live = TRUE
                  AND COALESCE(category_watch_prefs, '') != ''
                ORDER BY id
                """
            )
            rows = cur.fetchall()
        return [_row_to_sub(r) for r in rows]

    def set_category_watch_live_state(
        self, sub_id: int, live_ids: list[str], *, primed: bool
    ) -> None:
        payload = json.dumps(list(live_ids), ensure_ascii=False)
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                UPDATE subscriptions
                SET category_watch_live_ids = %s, category_watch_primed = %s
                WHERE id = %s
                """,
                (payload, bool(primed), sub_id),
            )

    def get_unique_schedule_reminder_twitch_ids(self) -> list[str]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT DISTINCT twitch_user_id
                FROM subscriptions
                WHERE enabled = TRUE AND schedule_reminder_minutes > 0
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

    def upsert_user(self, user_id: int) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO users (user_id, bot_blocked, bot_blocked_at)
                VALUES (%s, FALSE, NULL)
                ON CONFLICT (user_id) DO UPDATE SET
                    bot_blocked = FALSE,
                    bot_blocked_at = NULL
                """,
                (user_id,),
            )

    def user_exists(self, user_id: int) -> bool:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT 1 FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        return row is not None

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

    def count_stars_payers_since(self, since: datetime) -> int:
        since_utc = since.astimezone(timezone.utc) if since.tzinfo else since.replace(tzinfo=timezone.utc)
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT COUNT(*) AS n FROM users
                WHERE premium_stars_paid_at IS NOT NULL
                  AND premium_stars_paid_at >= %s
                """,
                (since_utc,),
            )
            row = cur.fetchone()
        return int(row["n"]) if row else 0

    def list_active_trial_users(self, *, now_unix: int | None = None) -> list[tuple[int, int]]:
        now = int(now_unix if now_unix is not None else datetime.now(timezone.utc).timestamp())
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT user_id, COALESCE(premium_trial_until, 0) AS premium_trial_until
                FROM users
                WHERE COALESCE(premium_trial_until, 0) > %s
                ORDER BY premium_trial_until, user_id
                """,
                (now,),
            )
            rows = cur.fetchall()
        return [(int(r["user_id"]), int(r["premium_trial_until"])) for r in rows]

    def list_expired_trial_users(self, *, now_unix: int | None = None) -> list[tuple[int, int]]:
        now = int(now_unix if now_unix is not None else datetime.now(timezone.utc).timestamp())
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT user_id, COALESCE(premium_trial_until, 0) AS premium_trial_until
                FROM users
                WHERE COALESCE(premium_trial_until, 0) > 0
                  AND COALESCE(premium_trial_until, 0) <= %s
                ORDER BY premium_trial_until, user_id
                """,
                (now,),
            )
            rows = cur.fetchall()
        return [(int(r["user_id"]), int(r["premium_trial_until"])) for r in rows]

    def set_referred_by(self, user_id: int, referrer_id: int) -> bool:
        if user_id == referrer_id or referrer_id <= 0:
            return False
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO users (user_id) VALUES (%s)
                ON CONFLICT (user_id) DO NOTHING
                """,
                (user_id,),
            )
            cur.execute(
                """
                UPDATE users
                SET referred_by = %s
                WHERE user_id = %s
                  AND (referred_by IS NULL OR referred_by = 0)
                """,
                (referrer_id, user_id),
            )
            return cur.rowcount > 0

    def get_referred_by(self, user_id: int) -> int | None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT referred_by FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
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
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO referral_credits (
                    referrer_id, invitee_id, charge_id, stars_paid, commission_stars
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (charge_id) DO NOTHING
                RETURNING id
                """,
                (
                    referrer_id,
                    invitee_id,
                    charge_id,
                    int(stars_paid),
                    int(commission_stars),
                ),
            )
            return cur.fetchone() is not None

    def get_referral_stats(self, user_id: int) -> ReferralStats:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT COUNT(*) AS n FROM users WHERE referred_by = %s",
                (user_id,),
            )
            invited = cur.fetchone()["n"]
            cur.execute(
                "SELECT COUNT(*) AS n FROM referral_credits WHERE referrer_id = %s",
                (user_id,),
            )
            payments = cur.fetchone()["n"]
            cur.execute(
                """
                SELECT COALESCE(SUM(commission_stars), 0) AS n
                FROM referral_credits WHERE referrer_id = %s
                """,
                (user_id,),
            )
            earned = cur.fetchone()["n"]
            cur.execute(
                """
                SELECT COALESCE(SUM(amount), 0) AS n
                FROM referral_withdrawals
                WHERE user_id = %s AND status IN ('pending', 'paid')
                """,
                (user_id,),
            )
            withdrawn = cur.fetchone()["n"]
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
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT COALESCE(SUM(commission_stars), 0) AS n
                FROM referral_credits WHERE referrer_id = %s
                """,
                (user_id,),
            )
            earned = cur.fetchone()["n"]
            cur.execute(
                """
                SELECT COALESCE(SUM(amount), 0) AS n
                FROM referral_withdrawals
                WHERE user_id = %s AND status IN ('pending', 'paid')
                """,
                (user_id,),
            )
            withdrawn = cur.fetchone()["n"]
            available = int(earned) - int(withdrawn)
            if amount > available:
                return None
            cur.execute(
                """
                INSERT INTO referral_withdrawals (user_id, amount, status)
                VALUES (%s, %s, 'pending')
                RETURNING id
                """,
                (user_id, amount),
            )
            row = cur.fetchone()
            return int(row["id"]) if row else None

    def get_referral_withdrawal(self, withdrawal_id: int) -> ReferralWithdrawal | None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT id, user_id, amount, status, created_at, resolved_at
                FROM referral_withdrawals WHERE id = %s
                """,
                (withdrawal_id,),
            )
            row = cur.fetchone()
        return _row_to_referral_withdrawal(row) if row else None

    def list_referral_withdrawals(
        self, user_id: int, *, limit: int = 20
    ) -> list[ReferralWithdrawal]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT id, user_id, amount, status, created_at, resolved_at
                FROM referral_withdrawals
                WHERE user_id = %s
                ORDER BY id DESC
                LIMIT %s
                """,
                (user_id, int(limit)),
            )
            rows = cur.fetchall()
        return [_row_to_referral_withdrawal(r) for r in rows]

    def list_pending_referral_withdrawals(self) -> list[ReferralWithdrawal]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT id, user_id, amount, status, created_at, resolved_at
                FROM referral_withdrawals
                WHERE status = 'pending'
                ORDER BY id ASC
                """
            )
            rows = cur.fetchall()
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

        cutoff = datetime.now(timezone.utc) - timedelta(days=ALERT_HISTORY_PREMIUM_DAYS)
        body = (message_text or "").strip()
        if len(body) > 4096:
            body = body[:4096]
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO alert_history (
                    owner_id, subscription_id, twitch_username, alert_type, message_text,
                    twitch_user_id, stream_id, vod_id, vod_offset_seconds
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
            cur.execute(
                """
                DELETE FROM alert_history
                WHERE owner_id = %s AND sent_at < %s
                """,
                (owner_id, cutoff),
            )

    def set_alert_history_vod_id(self, history_id: int, vod_id: str) -> None:
        vid = (vod_id or "").strip()
        if not vid:
            return
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "UPDATE alert_history SET vod_id = %s WHERE id = %s",
                (vid, int(history_id)),
            )

    def set_alert_history_viewed(
        self, owner_id: int, history_id: int, *, viewed: bool
    ) -> bool:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                UPDATE alert_history
                SET viewed = %s
                WHERE id = %s AND owner_id = %s
                """,
                (bool(viewed), int(history_id), int(owner_id)),
            )
            return cur.rowcount > 0

    def set_alert_history_viewed_below(
        self, owner_id: int, history_id: int, *, viewed: bool = True
    ) -> int:
        # History UI is newest-first (id DESC); "below" = this row and older (id <=).
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                UPDATE alert_history
                SET viewed = %s
                WHERE owner_id = %s AND id <= %s
                """,
                (bool(viewed), int(owner_id), int(history_id)),
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
            cur = self._cursor(conn)
            if since is not None:
                if since.tzinfo is None:
                    since = since.replace(tzinfo=timezone.utc)
                cur.execute(
                    """
                    SELECT id, owner_id, subscription_id, twitch_username,
                           alert_type, message_text, sent_at,
                           twitch_user_id, stream_id, vod_id, vod_offset_seconds,
                           viewed
                    FROM alert_history
                    WHERE owner_id = %s AND sent_at >= %s
                    ORDER BY id DESC
                    LIMIT %s
                    """,
                    (owner_id, since.astimezone(timezone.utc), int(limit)),
                )
            else:
                cur.execute(
                    """
                    SELECT id, owner_id, subscription_id, twitch_username,
                           alert_type, message_text, sent_at,
                           twitch_user_id, stream_id, vod_id, vod_offset_seconds,
                           viewed
                    FROM alert_history
                    WHERE owner_id = %s
                    ORDER BY id DESC
                    LIMIT %s
                    """,
                    (owner_id, int(limit)),
                )
            rows = cur.fetchall()
        return [_row_to_alert_history(r) for r in rows]

    def resolve_referral_withdrawal(
        self, withdrawal_id: int, status: str
    ) -> ReferralWithdrawal | None:
        if status not in ("paid", "rejected"):
            return None
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                UPDATE referral_withdrawals
                SET status = %s, resolved_at = NOW()
                WHERE id = %s AND status = 'pending'
                RETURNING id, user_id, amount, status, created_at, resolved_at
                """,
                (status, withdrawal_id),
            )
            row = cur.fetchone()
        return _row_to_referral_withdrawal(row) if row else None

    def set_bot_blocked(self, user_id: int, blocked: bool) -> None:
        now = int(datetime.now(timezone.utc).timestamp())
        with self._conn() as conn:
            cur = self._cursor(conn)
            if blocked:
                cur.execute(
                    """
                    INSERT INTO users (user_id, bot_blocked, bot_blocked_at)
                    VALUES (%s, TRUE, %s)
                    ON CONFLICT (user_id) DO UPDATE SET
                        bot_blocked = TRUE,
                        bot_blocked_at = CASE
                            WHEN COALESCE(users.bot_blocked, FALSE) = TRUE
                             AND users.bot_blocked_at IS NOT NULL
                            THEN users.bot_blocked_at
                            ELSE EXCLUDED.bot_blocked_at
                        END
                    """,
                    (user_id, now),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO users (user_id, bot_blocked, bot_blocked_at)
                    VALUES (%s, FALSE, NULL)
                    ON CONFLICT (user_id) DO UPDATE SET
                        bot_blocked = FALSE,
                        bot_blocked_at = NULL
                    """,
                    (user_id,),
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

    def get_bot_blocked_at(self, user_id: int) -> int | None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT bot_blocked_at FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        if not row or row["bot_blocked_at"] is None:
            return None
        return int(row["bot_blocked_at"])

    def set_bot_blocked_at(self, user_id: int, blocked_at_unix: int) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                UPDATE users SET bot_blocked_at = %s
                WHERE user_id = %s AND COALESCE(bot_blocked, FALSE) = TRUE
                """,
                (int(blocked_at_unix), user_id),
            )

    def list_blocked_user_ids(self) -> list[int]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT user_id FROM users WHERE COALESCE(bot_blocked, FALSE) = TRUE"
            )
            rows = cur.fetchall()
        return [int(r["user_id"]) for r in rows]

    def delete_user_data(self, user_id: int) -> bool:
        uid = int(user_id)
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute("SELECT 1 FROM users WHERE user_id = %s", (uid,))
            if not cur.fetchone():
                return False
            cur.execute(
                "SELECT id FROM subscriptions WHERE owner_id = %s", (uid,)
            )
            sub_ids = [int(r["id"]) for r in cur.fetchall()]
            if sub_ids:
                cur.execute(
                    "DELETE FROM alert_share_tokens WHERE source_sub_id = ANY(%s)",
                    (sub_ids,),
                )
                cur.execute(
                    "DELETE FROM alert_history WHERE subscription_id = ANY(%s)",
                    (sub_ids,),
                )
            cur.execute("DELETE FROM alert_history WHERE owner_id = %s", (uid,))
            cur.execute(
                "DELETE FROM deleted_subscriptions_cart WHERE owner_id = %s", (uid,)
            )
            cur.execute("DELETE FROM subscriptions WHERE owner_id = %s", (uid,))
            cur.execute("DELETE FROM twitch_sync WHERE owner_id = %s", (uid,))
            cur.execute("DELETE FROM whisper_alerts WHERE owner_id = %s", (uid,))
            cur.execute("DELETE FROM chat_auth WHERE owner_id = %s", (uid,))
            cur.execute("DELETE FROM chat_send_daily WHERE owner_id = %s", (uid,))
            cur.execute(
                "DELETE FROM user_beta_enrollments WHERE user_id = %s", (uid,)
            )
            cur.execute(
                """
                DELETE FROM referral_credits
                WHERE referrer_id = %s OR invitee_id = %s
                """,
                (uid, uid),
            )
            cur.execute(
                "DELETE FROM referral_withdrawals WHERE user_id = %s", (uid,)
            )
            cur.execute(
                "DELETE FROM broadcast_feedback WHERE user_id = %s", (uid,)
            )
            cur.execute(
                "DELETE FROM broadcast_deliveries WHERE user_id = %s", (uid,)
            )
            cur.execute(
                "DELETE FROM premium_channels WHERE owner_telegram_id = %s", (uid,)
            )
            cur.execute("DELETE FROM unreachable_chats WHERE chat_id = %s", (uid,))
            cur.execute(
                "UPDATE users SET referred_by = NULL WHERE referred_by = %s", (uid,)
            )
            cur.execute("DELETE FROM users WHERE user_id = %s", (uid,))
        return True

    def purge_expired_blocked_users(
        self, *, now_unix: int | None = None, retention_days: int | None = None
    ) -> int:
        from config import BLOCKED_USER_RETENTION_DAYS

        now = int(
            now_unix
            if now_unix is not None
            else datetime.now(timezone.utc).timestamp()
        )
        days = int(
            BLOCKED_USER_RETENTION_DAYS if retention_days is None else retention_days
        )
        cutoff = now - max(0, days) * 86400
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT user_id FROM users
                WHERE COALESCE(bot_blocked, FALSE) = TRUE
                  AND bot_blocked_at IS NOT NULL
                  AND bot_blocked_at <= %s
                """,
                (cutoff,),
            )
            ids = [int(r["user_id"]) for r in cur.fetchall()]
        removed = 0
        for uid in ids:
            if self.delete_user_data(uid):
                removed += 1
        return removed

    def set_chat_unreachable(self, chat_id: int, unreachable: bool) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            if unreachable:
                cur.execute(
                    """
                    INSERT INTO unreachable_chats (chat_id) VALUES (%s)
                    ON CONFLICT (chat_id) DO NOTHING
                    """,
                    (chat_id,),
                )
            else:
                cur.execute(
                    "DELETE FROM unreachable_chats WHERE chat_id = %s",
                    (chat_id,),
                )

    def is_chat_unreachable(self, chat_id: int) -> bool:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT 1 FROM unreachable_chats WHERE chat_id = %s",
                (chat_id,),
            )
            row = cur.fetchone()
        return row is not None

    def pause_delivery_for_chat(self, chat_id: int) -> int:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                UPDATE subscriptions
                SET enabled = FALSE, delivery_paused = TRUE
                WHERE chat_id = %s AND enabled = TRUE
                """,
                (chat_id,),
            )
            return int(cur.rowcount)

    def list_delivery_paused_for_chat(self, chat_id: int) -> list[Subscription]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT * FROM subscriptions
                WHERE chat_id = %s AND COALESCE(delivery_paused, FALSE) = TRUE
                ORDER BY id
                """,
                (chat_id,),
            )
            rows = cur.fetchall()
        return [_row_to_sub(r) for r in rows]

    def clear_delivery_paused(self, sub_id: int, *, enabled: bool) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                UPDATE subscriptions
                SET delivery_paused = FALSE, enabled = %s
                WHERE id = %s
                """,
                (bool(enabled), sub_id),
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

    def _update_recipients(self, pref_column: str) -> list[int]:
        # Missing users row → opt-in defaults (receive=true, blocked=false).
        if pref_column not in (
            "receive_bot_updates",
            "receive_availability_updates",
            "receive_other_updates",
        ):
            raise ValueError(f"invalid recipient pref: {pref_column}")
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                f"""
                SELECT DISTINCT ids.uid AS user_id
                FROM (
                    SELECT user_id AS uid FROM users
                    UNION
                    SELECT owner_id AS uid FROM subscriptions
                ) AS ids
                LEFT JOIN users u ON u.user_id = ids.uid
                WHERE COALESCE(u.bot_blocked, FALSE) = FALSE
                  AND COALESCE(u.{pref_column}, TRUE) = TRUE
                """
            )
            rows = cur.fetchall()
        return [int(r["user_id"]) for r in rows]

    def get_bot_update_recipients(self) -> list[int]:
        return self._update_recipients("receive_bot_updates")

    def get_availability_recipients(self) -> list[int]:
        return self._update_recipients("receive_availability_updates")

    def get_other_recipients(self) -> list[int]:
        return self._update_recipients("receive_other_updates")

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

    def get_receive_other_updates(self, user_id: int) -> bool:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT receive_other_updates FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        if not row:
            return True
        return bool(row["receive_other_updates"])

    def set_receive_other_updates(self, user_id: int, enabled: bool) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO users (user_id, receive_other_updates) VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    receive_other_updates = EXCLUDED.receive_other_updates
                """,
                (user_id, enabled),
            )

    def get_receive_sync_updates(self, user_id: int) -> bool:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT receive_sync_updates FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        if not row:
            return True
        return bool(row["receive_sync_updates"])

    def set_receive_sync_updates(self, user_id: int, enabled: bool) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO users (user_id, receive_sync_updates) VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    receive_sync_updates = EXCLUDED.receive_sync_updates
                """,
                (user_id, enabled),
            )

    def get_notifications_paused_until(self, user_id: int) -> int:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT notifications_paused_until FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        if not row:
            return 0
        return int(row["notifications_paused_until"] or 0)

    def set_notifications_paused_until(self, user_id: int, until_ts: int) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO users (user_id, notifications_paused_until) VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    notifications_paused_until = EXCLUDED.notifications_paused_until
                """,
                (user_id, int(until_ts)),
            )

    def mark_template_typo_notice_sent(self, user_id: int) -> bool:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING",
                (user_id,),
            )
            cur.execute(
                """
                UPDATE users
                SET template_typo_notice_sent = TRUE
                WHERE user_id = %s
                  AND COALESCE(template_typo_notice_sent, FALSE) = FALSE
                """,
                (user_id,),
            )
            return bool(cur.rowcount)

    def get_global_ignore_keywords(self, user_id: int) -> str:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT global_ignore_keywords FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        if not row:
            return ""
        return str(row["global_ignore_keywords"] or "")

    def set_global_ignore_keywords(self, user_id: int, keywords: str) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO users (user_id, global_ignore_keywords) VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    global_ignore_keywords = EXCLUDED.global_ignore_keywords
                """,
                (user_id, str(keywords or "")),
            )

    def get_advanced_mode_setting(self, user_id: int) -> bool | None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT advanced_mode FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        if row is None or row["advanced_mode"] is None:
            return None
        return bool(row["advanced_mode"])

    def set_advanced_mode_setting(self, user_id: int, enabled: bool) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO users (user_id, advanced_mode) VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    advanced_mode = EXCLUDED.advanced_mode
                """,
                (user_id, 1 if enabled else 0),
            )

    def owner_has_advanced_subscription_options(self, owner_id: int) -> bool:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT 1 AS ok FROM subscriptions
                WHERE owner_id = %s
                  AND (
                    TRIM(COALESCE(ignore_keywords, '')) != ''
                    OR COALESCE(use_global_ignore, FALSE)
                    OR COALESCE(delay_minutes, 0) > 0
                    OR COALESCE(suppress_repeat_minutes, 0) > 0
                    OR COALESCE(delete_previous, FALSE)
                  )
                LIMIT 1
                """,
                (owner_id,),
            )
            row = cur.fetchone()
        return row is not None

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

    def get_schedule_utc_offset_minutes(self, user_id: int) -> int | None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT schedule_utc_offset_minutes FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        if not row or row["schedule_utc_offset_minutes"] is None:
            return None
        return int(row["schedule_utc_offset_minutes"])

    def set_schedule_utc_offset_minutes(self, user_id: int, offset_minutes: int) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO users (user_id, schedule_utc_offset_minutes)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    schedule_utc_offset_minutes = EXCLUDED.schedule_utc_offset_minutes
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
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT user_id, schedule_utc_offset_minutes
                FROM users WHERE user_id = ANY(%s)
                """,
                (unique,),
            )
            rows = cur.fetchall()
        for row in rows:
            val = row["schedule_utc_offset_minutes"]
            out[int(row["user_id"])] = int(val) if val is not None else None
        return out

    def record_broadcast_offset_sent(
        self, broadcast_id: int, utc_offset_minutes: int, sent: int
    ) -> None:
        off = int(utc_offset_minutes)
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT sent_utc_offsets, sent_count
                FROM scheduled_broadcasts
                WHERE id = %s AND sent_at IS NULL
                """,
                (broadcast_id,),
            )
            row = cur.fetchone()
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
            cur.execute(
                """
                UPDATE scheduled_broadcasts
                SET sent_utc_offsets = %s,
                    sent_count = COALESCE(sent_count, 0) + %s
                WHERE id = %s
                """,
                (",".join(parts), int(sent), broadcast_id),
            )

    def get_broadcast_sent_offsets(self, broadcast_id: int) -> set[int]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT sent_utc_offsets FROM scheduled_broadcasts WHERE id = %s",
                (broadcast_id,),
            )
            row = cur.fetchone()
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
            cur = self._cursor(conn)
            cur.execute(
                """
                UPDATE scheduled_broadcasts
                SET sent_utc_offsets = '', sent_count = 0
                WHERE id = %s AND sent_at IS NULL
                """,
                (broadcast_id,),
            )

    def get_broadcast_sent_count(self, broadcast_id: int) -> int:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT sent_count FROM scheduled_broadcasts WHERE id = %s",
                (broadcast_id,),
            )
            row = cur.fetchone()
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
            cur = self._cursor(conn)
            cur.execute(
                "SELECT watch_prefs FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        if not row:
            return []
        return parse_watch_filters(row["watch_prefs"])

    def set_watch_filters(self, user_id: int, filters: list[WatchFilter]) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO users (user_id, watch_prefs)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET watch_prefs = EXCLUDED.watch_prefs
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
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT COUNT(*) AS c FROM subscriptions
                WHERE owner_id = %s AND enabled = TRUE AND is_demo = %s
                """,
                (owner_id, bool(demo)),
            )
            return int(cur.fetchone()["c"])

    def delete_demo_subscriptions(self, owner_id: int) -> int:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT id FROM subscriptions WHERE owner_id = %s AND is_demo = TRUE",
                (owner_id,),
            )
            ids = [int(r["id"]) for r in cur.fetchall()]
            if ids:
                cur.execute(
                    "DELETE FROM alert_share_tokens WHERE source_sub_id = ANY(%s)",
                    (ids,),
                )
                cur.execute(
                    "DELETE FROM alert_history WHERE subscription_id = ANY(%s)",
                    (ids,),
                )
            cur.execute(
                "DELETE FROM deleted_subscriptions_cart WHERE owner_id = %s AND is_demo = TRUE",
                (owner_id,),
            )
            cur.execute(
                "DELETE FROM subscriptions WHERE owner_id = %s AND is_demo = TRUE",
                (owner_id,),
            )
            return int(cur.rowcount)

    def get_premium_status(self, user_id: int):
        from premium import PremiumStatus, parse_premium_features_blob

        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT premium_permanent, premium_stars_until, premium_stars_charge_id,
                       premium_stars_canceled, premium_twitch_active, premium_twitch_user_id,
                       COALESCE(premium_trial_until, 0) AS premium_trial_until,
                       COALESCE(premium_trial_used, FALSE) AS premium_trial_used,
                       COALESCE(premium_features, '') AS premium_features
                FROM users WHERE user_id = %s
                """,
                (user_id,),
            )
            row = cur.fetchone()
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
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO users (
                    user_id, premium_stars_charge_id, premium_stars_until,
                    premium_stars_canceled, premium_stars_paid_at
                )
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    premium_stars_charge_id = EXCLUDED.premium_stars_charge_id,
                    premium_stars_until = EXCLUDED.premium_stars_until,
                    premium_stars_canceled = EXCLUDED.premium_stars_canceled,
                    premium_stars_paid_at = NOW()
                """,
                (user_id, charge_id, int(until_unix), bool(canceled)),
            )

    def set_premium_stars_canceled(self, user_id: int, canceled: bool) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO users (user_id, premium_stars_canceled)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    premium_stars_canceled = EXCLUDED.premium_stars_canceled
                """,
                (user_id, bool(canceled)),
            )

    def set_premium_permanent(self, user_id: int, permanent: bool) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO users (user_id, premium_permanent)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    premium_permanent = EXCLUDED.premium_permanent
                """,
                (user_id, bool(permanent)),
            )

    def clear_premium(self, user_id: int) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                UPDATE users SET
                    premium_permanent = FALSE,
                    premium_stars_charge_id = '',
                    premium_stars_until = 0,
                    premium_stars_canceled = TRUE,
                    premium_trial_until = 0,
                    premium_features = '',
                    premium_twitch_active = FALSE,
                    premium_twitch_user_id = '',
                    premium_twitch_refresh = '',
                    premium_twitch_checked_at = NULL
                WHERE user_id = %s
                """,
                (user_id,),
            )

    def set_premium_trial(
        self, user_id: int, *, until_unix: int, used: bool = True
    ) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO users (user_id, premium_trial_until, premium_trial_used)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    premium_trial_until = EXCLUDED.premium_trial_until,
                    premium_trial_used = EXCLUDED.premium_trial_used
                """,
                (user_id, int(until_unix), bool(used)),
            )

    def expire_premium_trial(self, user_id: int) -> int:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                UPDATE users SET premium_trial_until = 0
                WHERE user_id = %s
                """,
                (user_id,),
            )
            cur.execute(
                """
                UPDATE subscriptions
                SET enabled = FALSE, trial_paused = TRUE
                WHERE owner_id = %s AND enabled = TRUE AND COALESCE(is_demo, FALSE) = FALSE
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
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO users (user_id, premium_features, premium_stars_paid_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    premium_features = EXCLUDED.premium_features,
                    premium_stars_paid_at = NOW()
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
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO users (user_id, premium_features)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    premium_features = EXCLUDED.premium_features
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
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO users (user_id, premium_features)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    premium_features = EXCLUDED.premium_features
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

        checked = datetime.now(timezone.utc)
        enc = encrypt_secret(refresh_token) if refresh_token else None
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT premium_twitch_user_id, premium_twitch_refresh FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
            uid = twitch_user_id if twitch_user_id is not None else (
                (row["premium_twitch_user_id"] if row else "") or ""
            )
            ref = enc if enc is not None else ((row["premium_twitch_refresh"] if row else "") or "")
            cur.execute(
                """
                INSERT INTO users (
                    user_id, premium_twitch_active, premium_twitch_user_id,
                    premium_twitch_refresh, premium_twitch_checked_at
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    premium_twitch_active = EXCLUDED.premium_twitch_active,
                    premium_twitch_user_id = EXCLUDED.premium_twitch_user_id,
                    premium_twitch_refresh = EXCLUDED.premium_twitch_refresh,
                    premium_twitch_checked_at = EXCLUDED.premium_twitch_checked_at
                """,
                (user_id, bool(active), uid, ref, checked),
            )

    def set_premium_twitch_refresh(self, user_id: int, refresh_token: str) -> None:
        from token_crypto import encrypt_secret

        enc = encrypt_secret(refresh_token)
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO users (user_id, premium_twitch_refresh)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    premium_twitch_refresh = EXCLUDED.premium_twitch_refresh
                """,
                (user_id, enc),
            )

    def get_premium_twitch_refresh(self, user_id: int) -> str | None:
        from token_crypto import decrypt_secret

        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT premium_twitch_refresh FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        if not row or not row["premium_twitch_refresh"]:
            return None
        try:
            return decrypt_secret(row["premium_twitch_refresh"])
        except Exception:
            return None

    def list_premium_twitch_user_ids(self) -> list[int]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT user_id FROM users
                WHERE COALESCE(premium_twitch_refresh, '') != ''
                   OR COALESCE(premium_twitch_active, FALSE) = TRUE
                """
            )
            rows = cur.fetchall()
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
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO scheduled_broadcasts
                    (msg_type, text, scheduled_at, created_by, recipient_ids)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (msg_type, text, scheduled_at, created_by, recipient_ids or ""),
            )
            row = cur.fetchone()
            return int(row["id"])

    def get_unsent_scheduled_broadcasts(self) -> list[ScheduledBroadcast]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT id, msg_type, text, scheduled_at, created_by,
                       COALESCE(recipient_ids, '') AS recipient_ids,
                       COALESCE(sent_utc_offsets, '') AS sent_utc_offsets,
                       COALESCE(sent_count, 0) AS sent_count
                FROM scheduled_broadcasts
                WHERE sent_at IS NULL
                ORDER BY scheduled_at
                """
            )
            rows = cur.fetchall()
        return [_scheduled_broadcast_from_row(r) for r in rows]

    def get_pending_scheduled_broadcasts(self) -> list[ScheduledBroadcast]:
        now = datetime.now(timezone.utc)
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT id, msg_type, text, scheduled_at, created_by,
                       COALESCE(recipient_ids, '') AS recipient_ids,
                       COALESCE(sent_utc_offsets, '') AS sent_utc_offsets,
                       COALESCE(sent_count, 0) AS sent_count
                FROM scheduled_broadcasts
                WHERE sent_at IS NULL AND scheduled_at <= %s
                ORDER BY scheduled_at
                """,
                (now,),
            )
            rows = cur.fetchall()
        return [_scheduled_broadcast_from_row(r) for r in rows]

    def get_scheduled_broadcast(self, broadcast_id: int) -> ScheduledBroadcast | None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT id, msg_type, text, scheduled_at, created_by,
                       COALESCE(recipient_ids, '') AS recipient_ids,
                       COALESCE(sent_utc_offsets, '') AS sent_utc_offsets,
                       COALESCE(sent_count, 0) AS sent_count
                FROM scheduled_broadcasts
                WHERE id = %s AND sent_at IS NULL
                """,
                (broadcast_id,),
            )
            row = cur.fetchone()
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
            updates.append(f"{key} = %s")
            values.append(value)
        if not updates:
            return self.get_scheduled_broadcast(broadcast_id) is not None
        values.append(broadcast_id)
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                f"UPDATE scheduled_broadcasts SET {', '.join(updates)} "
                "WHERE id = %s AND sent_at IS NULL",
                values,
            )
            updated = cur.rowcount > 0
        return updated

    def delete_scheduled_broadcast(self, broadcast_id: int) -> bool:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "DELETE FROM scheduled_broadcasts WHERE id = %s AND sent_at IS NULL",
                (broadcast_id,),
            )
            deleted = cur.rowcount > 0
        return deleted

    def mark_scheduled_broadcast_sent(self, broadcast_id: int) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "UPDATE scheduled_broadcasts SET sent_at = NOW() WHERE id = %s",
                (broadcast_id,),
            )

    def get_sent_broadcasts(self, *, retention_days: int = 30) -> list[ScheduledBroadcast]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT id, msg_type, text, scheduled_at, created_by,
                       COALESCE(recipient_ids, '') AS recipient_ids,
                       COALESCE(sent_utc_offsets, '') AS sent_utc_offsets,
                       COALESCE(sent_count, 0) AS sent_count,
                       sent_at
                FROM scheduled_broadcasts
                WHERE sent_at IS NOT NULL
                  AND sent_at >= NOW() - make_interval(days => %s)
                ORDER BY sent_at ASC
                """,
                (int(retention_days),),
            )
            rows = cur.fetchall()
        return [_scheduled_broadcast_from_row(r) for r in rows]

    def purge_old_sent_broadcasts(self, *, retention_days: int = 30) -> int:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                DELETE FROM scheduled_broadcasts
                WHERE sent_at IS NOT NULL
                  AND sent_at < NOW() - make_interval(days => %s)
                """,
                (int(retention_days),),
            )
            deleted = int(cur.rowcount)
        return deleted

    def add_broadcast_delivery(
        self, broadcast_id: int, user_id: int, message_id: int
    ) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO broadcast_deliveries (broadcast_id, user_id, message_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (broadcast_id, user_id) DO UPDATE
                SET message_id = EXCLUDED.message_id
                """,
                (broadcast_id, user_id, message_id),
            )

    def get_broadcast_deliveries(
        self, broadcast_id: int
    ) -> list[tuple[int, int]]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT user_id, message_id FROM broadcast_deliveries
                WHERE broadcast_id = %s
                """,
                (broadcast_id,),
            )
            rows = cur.fetchall()
        return [(int(r["user_id"]), int(r["message_id"])) for r in rows]

    def get_broadcast_feedback_vote(
        self, broadcast_id: int, user_id: int
    ) -> int | None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT vote FROM broadcast_feedback
                WHERE broadcast_id = %s AND user_id = %s
                """,
                (broadcast_id, user_id),
            )
            row = cur.fetchone()
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
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO broadcast_feedback (broadcast_id, user_id, vote)
                VALUES (%s, %s, %s)
                ON CONFLICT (broadcast_id, user_id) DO UPDATE
                SET vote = EXCLUDED.vote
                """,
                (broadcast_id, user_id, vote),
            )

    def clear_broadcast_feedback(self, broadcast_id: int, user_id: int) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "DELETE FROM broadcast_feedback WHERE broadcast_id = %s AND user_id = %s",
                (broadcast_id, user_id),
            )

    def get_broadcast_feedback_counts(self, broadcast_id: int) -> tuple[int, int]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT vote, COUNT(*) AS c FROM broadcast_feedback
                WHERE broadcast_id = %s
                GROUP BY vote
                """,
                (broadcast_id,),
            )
            rows = cur.fetchall()
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
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO lucky_templates (locale, text, created_at)
                VALUES (%s, %s, NOW())
                """,
                (loc, body),
            )
            cur.execute(
                "SELECT id FROM lucky_templates WHERE locale = %s ORDER BY id DESC",
                (loc,),
            )
            rows = cur.fetchall()
            if len(rows) > LUCKY_TEMPLATE_LIMIT:
                old_ids = [int(r["id"]) for r in rows[LUCKY_TEMPLATE_LIMIT:]]
                cur.execute(
                    "DELETE FROM lucky_templates WHERE id = ANY(%s)",
                    (list(old_ids),),
                )

    def pick_lucky_template(self, locale: str) -> str | None:
        loc = self._lucky_locale(locale)
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT text FROM lucky_templates
                WHERE locale = %s
                ORDER BY id DESC
                LIMIT %s
                """,
                (loc, LUCKY_TEMPLATE_LIMIT),
            )
            rows = cur.fetchall()
        if not rows:
            return None
        return str(random.choice(rows)["text"])

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
                """
                SELECT COUNT(*) AS c FROM (
                    SELECT user_id AS id FROM users
                    UNION
                    SELECT DISTINCT owner_id AS id FROM subscriptions
                ) AS n
                LEFT JOIN users u ON u.user_id = n.id
                WHERE COALESCE(u.receive_other_updates, TRUE) = TRUE
                  AND COALESCE(u.bot_blocked, FALSE) = FALSE
                """
            )
            sys_other = int(cur.fetchone()["c"])
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
            cur.execute(
                """
                SELECT COUNT(*) AS c FROM users
                WHERE COALESCE(bot_blocked, FALSE) = FALSE
                  AND (
                    COALESCE(premium_stars_until, 0)
                      > EXTRACT(EPOCH FROM NOW())::BIGINT
                    OR COALESCE(premium_twitch_active, FALSE) = TRUE
                    OR (
                      COALESCE(premium_features, '') NOT IN ('', '{}')
                    )
                  )
                """
            )
            premium_paid = int(cur.fetchone()["c"])
        return BotStats(
            users=users,
            notify_users=notify,
            subscriptions_total=subs_total,
            subscriptions_enabled=subs_enabled,
            subscriptions_disabled=subs_total - subs_enabled,
            unique_owners=unique_owners,
            unique_twitch_channels=unique_twitch,
            premium_paid=premium_paid,
            sys_updates=sys_updates,
            sys_availability=sys_availability,
            sys_other=sys_other,
            blocked_users=blocked_users,
            locale_en=locale_en,
            locale_ru=locale_ru,
            locale_unset=locale_unset,
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
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO twitch_sync (
                    owner_id, twitch_user_id, refresh_token,
                    period_days, next_sync_at, last_sync_at
                ) VALUES (%s, %s, %s, %s, %s::timestamptz, %s::timestamptz)
                ON CONFLICT (owner_id) DO UPDATE SET
                    twitch_user_id = EXCLUDED.twitch_user_id,
                    refresh_token = EXCLUDED.refresh_token,
                    period_days = EXCLUDED.period_days,
                    next_sync_at = EXCLUDED.next_sync_at,
                    last_sync_at = EXCLUDED.last_sync_at
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
            cur = self._cursor(conn)
            cur.execute(
                "SELECT * FROM twitch_sync WHERE owner_id = %s",
                (owner_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        sync = _row_to_twitch_sync(row)
        sync.refresh_token = decrypt_secret(sync.refresh_token)
        return sync

    def delete_twitch_sync(self, owner_id: int) -> bool:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "DELETE FROM twitch_sync WHERE owner_id = %s",
                (owner_id,),
            )
            return cur.rowcount > 0

    def set_twitch_sync_period(
        self, owner_id: int, period_days: int, next_sync_at: str
    ) -> bool:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                UPDATE twitch_sync
                SET period_days = %s, next_sync_at = %s::timestamptz
                WHERE owner_id = %s
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
            cur = self._cursor(conn)
            cur.execute(
                """
                UPDATE twitch_sync
                SET refresh_token = %s,
                    last_sync_at = %s::timestamptz,
                    next_sync_at = %s::timestamptz
                WHERE owner_id = %s
                """,
                (enc, last_sync_at, next_sync_at, owner_id),
            )

    def get_due_twitch_syncs(self, now_iso: str) -> list[TwitchSync]:
        from token_crypto import decrypt_secret

        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT * FROM twitch_sync
                WHERE next_sync_at <= %s::timestamptz
                ORDER BY next_sync_at
                """,
                (now_iso,),
            )
            rows = cur.fetchall()
        out: list[TwitchSync] = []
        for r in rows:
            sync = _row_to_twitch_sync(r)
            sync.refresh_token = decrypt_secret(sync.refresh_token)
            out.append(sync)
        return out

    def get_whisper_alert(self, owner_id: int) -> WhisperAlert | None:
        from token_crypto import decrypt_secret

        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT * FROM whisper_alerts WHERE owner_id = %s",
                (owner_id,),
            )
            row = cur.fetchone()
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
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT * FROM whisper_alerts
                WHERE twitch_user_id = %s AND enabled = TRUE
                ORDER BY owner_id
                """,
                (twitch_user_id,),
            )
            rows = cur.fetchall()
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
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO whisper_alerts (
                    owner_id, enabled, twitch_user_id, twitch_login,
                    refresh_token, eventsub_id
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (owner_id) DO UPDATE SET
                    enabled = EXCLUDED.enabled,
                    twitch_user_id = EXCLUDED.twitch_user_id,
                    twitch_login = EXCLUDED.twitch_login,
                    refresh_token = EXCLUDED.refresh_token,
                    eventsub_id = EXCLUDED.eventsub_id
                """,
                (
                    owner_id,
                    bool(enabled),
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
            cur = self._cursor(conn)
            if eventsub_id is None:
                cur.execute(
                    "UPDATE whisper_alerts SET enabled = %s WHERE owner_id = %s",
                    (bool(enabled), owner_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE whisper_alerts
                    SET enabled = %s, eventsub_id = %s
                    WHERE owner_id = %s
                    """,
                    (bool(enabled), eventsub_id, owner_id),
                )

    def disable_whisper_alerts_for_twitch_user(self, twitch_user_id: str) -> list[int]:
        if not twitch_user_id:
            return []
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT owner_id FROM whisper_alerts
                WHERE twitch_user_id = %s AND enabled = TRUE
                """,
                (twitch_user_id,),
            )
            rows = cur.fetchall()
            cur.execute(
                """
                UPDATE whisper_alerts
                SET enabled = FALSE, eventsub_id = ''
                WHERE twitch_user_id = %s
                """,
                (twitch_user_id,),
            )
        return [int(r["owner_id"]) for r in rows]

    def get_chat_auth(self, owner_id: int) -> ChatAuth | None:
        from token_crypto import decrypt_secret

        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT * FROM chat_auth WHERE owner_id = %s",
                (owner_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        auth = _row_to_chat_auth(row)
        auth.refresh_token = decrypt_secret(auth.refresh_token)
        return auth

    def delete_whisper_alert(self, owner_id: int) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute("DELETE FROM whisper_alerts WHERE owner_id = %s", (owner_id,))

    def delete_chat_auth(self, owner_id: int) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute("DELETE FROM chat_auth WHERE owner_id = %s", (owner_id,))

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
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO chat_auth (
                    owner_id, twitch_user_id, twitch_login, refresh_token
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT(owner_id) DO UPDATE SET
                    twitch_user_id = EXCLUDED.twitch_user_id,
                    twitch_login = EXCLUDED.twitch_login,
                    refresh_token = EXCLUDED.refresh_token
                """,
                (owner_id, twitch_user_id, twitch_login, enc),
            )

    def get_chat_send_count(self, owner_id: int, day: str) -> int:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT count FROM chat_send_daily WHERE owner_id = %s AND day = %s",
                (owner_id, day),
            )
            row = cur.fetchone()
        return int(row["count"]) if row else 0

    def increment_chat_send_count(self, owner_id: int, day: str) -> int:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO chat_send_daily (owner_id, day, count)
                VALUES (%s, %s, 1)
                ON CONFLICT(owner_id, day) DO UPDATE SET
                    count = chat_send_daily.count + 1
                """,
                (owner_id, day),
            )
            cur.execute(
                "SELECT count FROM chat_send_daily WHERE owner_id = %s AND day = %s",
                (owner_id, day),
            )
            row = cur.fetchone()
        return int(row["count"]) if row else 1

    def delete_synced_subscriptions_missing(
        self, owner_id: int, keep_twitch_user_ids: set[str], *, to_cart: bool = True
    ) -> list[str]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT * FROM subscriptions
                WHERE owner_id = %s AND from_twitch_sync = TRUE
                  AND COALESCE(sync_user_edited, FALSE) = FALSE
                """,
                (owner_id,),
            )
            rows = cur.fetchall()
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
                    deleted_at = datetime.now(timezone.utc)
                    cur.execute(
                        """
                        INSERT INTO deleted_subscriptions_cart (
                            owner_id, is_demo, deleted_at, subscription_json
                        ) VALUES (%s, %s, %s, %s)
                        """,
                        (
                            owner_id,
                            bool(sub.is_demo),
                            deleted_at,
                            json.dumps(payload, ensure_ascii=False),
                        ),
                    )
                cur.execute(
                    "DELETE FROM subscriptions WHERE id = %s AND owner_id = %s",
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
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT twitch_user_id, twitch_username FROM subscriptions
                WHERE owner_id = %s AND is_demo = %s
                  AND COALESCE(category_watch_prefs, '') = ''
                  AND twitch_user_id NOT LIKE 'cw:%%'
                """,
                (owner_id, bool(is_demo)),
            )
            rows = cur.fetchall()
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
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT * FROM subscriptions
                WHERE owner_id = %s AND is_demo = %s
                """,
                (owner_id, bool(is_demo)),
            )
            rows = cur.fetchall()
            removed = 0
            for r in rows:
                if str(r["twitch_user_id"]) not in ids:
                    continue
                sub = _row_to_sub(r)
                if to_cart:
                    payload = _subscription_cart_snapshot(sub)
                    deleted_at = datetime.now(timezone.utc)
                    cur.execute(
                        """
                        INSERT INTO deleted_subscriptions_cart (
                            owner_id, is_demo, deleted_at, subscription_json
                        ) VALUES (%s, %s, %s, %s)
                        """,
                        (
                            owner_id,
                            bool(sub.is_demo),
                            deleted_at,
                            json.dumps(payload, ensure_ascii=False),
                        ),
                    )
                cur.execute(
                    "DELETE FROM subscriptions WHERE id = %s AND owner_id = %s",
                    (int(sub.id), owner_id),
                )
                removed += 1
            return removed

    def beta_enrollment_explicit(self, user_id: int, feature_id: str) -> bool | None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT enrolled FROM user_beta_enrollments
                WHERE user_id = %s AND feature_id = %s
                """,
                (user_id, feature_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return bool(row["enrolled"])

    def set_beta_enrollment(
        self, user_id: int, feature_id: str, enrolled: bool
    ) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            if enrolled:
                cur.execute(
                    """
                    INSERT INTO user_beta_enrollments
                        (user_id, feature_id, enrolled, opted_in_at, opted_out_at)
                    VALUES (%s, %s, TRUE, NOW(), NULL)
                    ON CONFLICT (user_id, feature_id) DO UPDATE SET
                        enrolled = TRUE,
                        opted_in_at = EXCLUDED.opted_in_at,
                        opted_out_at = NULL
                    """,
                    (user_id, feature_id),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO user_beta_enrollments
                        (user_id, feature_id, enrolled, opted_in_at, opted_out_at)
                    VALUES (%s, %s, FALSE, NOW(), NOW())
                    ON CONFLICT (user_id, feature_id) DO UPDATE SET
                        enrolled = FALSE,
                        opted_out_at = EXCLUDED.opted_out_at
                    """,
                    (user_id, feature_id),
                )

    def clear_beta_enrollment(self, user_id: int, feature_id: str) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "DELETE FROM user_beta_enrollments WHERE user_id = %s AND feature_id = %s",
                (user_id, feature_id),
            )

    def list_beta_enrolled_user_ids(self, feature_ids: list[str]) -> list[int]:
        unique = list(dict.fromkeys(str(fid) for fid in feature_ids if str(fid)))
        if not unique:
            return []
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT DISTINCT e.user_id AS user_id
                FROM user_beta_enrollments e
                LEFT JOIN users u ON u.user_id = e.user_id
                WHERE e.enrolled = TRUE
                  AND e.feature_id = ANY(%s)
                  AND COALESCE(u.bot_blocked, FALSE) = FALSE
                """,
                (unique,),
            )
            rows = cur.fetchall()
        return sorted(int(r["user_id"]) for r in rows)

    def is_premium_channel_login(self, login: str) -> bool:
        key = (login or "").strip().lower()
        if not key:
            return False
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT 1 FROM premium_channels WHERE twitch_login = %s",
                (key,),
            )
            row = cur.fetchone()
        return row is not None

    def list_premium_channel_logins(self) -> list[str]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT twitch_login FROM premium_channels ORDER BY twitch_login"
            )
            rows = cur.fetchall()
        return [str(r["twitch_login"]).lower() for r in rows]

    def list_premium_channels(self) -> list[PremiumChannel]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT twitch_user_id, twitch_login, display_name,
                       owner_telegram_id, charge_id, paid_at
                FROM premium_channels
                ORDER BY paid_at DESC
                """
            )
            rows = cur.fetchall()
        out: list[PremiumChannel] = []
        for r in rows:
            paid = r["paid_at"]
            paid_s = paid.isoformat() if hasattr(paid, "isoformat") else str(paid or "")
            out.append(
                PremiumChannel(
                    twitch_user_id=str(r["twitch_user_id"]),
                    twitch_login=str(r["twitch_login"]).lower(),
                    display_name=str(r["display_name"] or r["twitch_login"]),
                    owner_telegram_id=int(r["owner_telegram_id"]),
                    charge_id=str(r["charge_id"] or ""),
                    paid_at=paid_s,
                )
            )
        return out

    def get_premium_channel(self, twitch_user_id: str) -> PremiumChannel | None:
        uid = str(twitch_user_id or "").strip()
        if not uid:
            return None
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT twitch_user_id, twitch_login, display_name,
                       owner_telegram_id, charge_id, paid_at
                FROM premium_channels WHERE twitch_user_id = %s
                """,
                (uid,),
            )
            row = cur.fetchone()
        if not row:
            return None
        paid = row["paid_at"]
        paid_s = paid.isoformat() if hasattr(paid, "isoformat") else str(paid or "")
        return PremiumChannel(
            twitch_user_id=str(row["twitch_user_id"]),
            twitch_login=str(row["twitch_login"]).lower(),
            display_name=str(row["display_name"] or row["twitch_login"]),
            owner_telegram_id=int(row["owner_telegram_id"]),
            charge_id=str(row["charge_id"] or ""),
            paid_at=paid_s,
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
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO premium_channels (
                    twitch_user_id, twitch_login, display_name,
                    owner_telegram_id, charge_id, paid_at
                ) VALUES (%s, %s, %s, %s, %s, NOW())
                ON CONFLICT (twitch_user_id) DO UPDATE SET
                    twitch_login = EXCLUDED.twitch_login,
                    display_name = EXCLUDED.display_name,
                    owner_telegram_id = EXCLUDED.owner_telegram_id,
                    charge_id = EXCLUDED.charge_id,
                    paid_at = NOW()
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
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT twitch_user_id, twitch_login, display_name,
                       owner_telegram_id, charge_id, paid_at
                FROM premium_channels
                WHERE charge_id = %s
                """,
                (cid,),
            )
            row = cur.fetchone()
        if not row:
            return None
        paid = row["paid_at"]
        paid_s = paid.isoformat() if hasattr(paid, "isoformat") else str(paid or "")
        return PremiumChannel(
            twitch_user_id=str(row["twitch_user_id"]),
            twitch_login=str(row["twitch_login"]).lower(),
            display_name=str(row["display_name"] or row["twitch_login"]),
            owner_telegram_id=int(row["owner_telegram_id"]),
            charge_id=str(row["charge_id"] or ""),
            paid_at=paid_s,
        )

    def delete_premium_channel_by_charge(self, charge_id: str) -> bool:
        cid = str(charge_id or "").strip()
        if not cid:
            return False
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "DELETE FROM premium_channels WHERE charge_id = %s",
                (cid,),
            )
            return int(cur.rowcount or 0) > 0

    def find_user_id_by_premium_charge(self, charge_id: str) -> int | None:
        from premium import parse_premium_features_blob

        cid = str(charge_id or "").strip()
        if not cid:
            return None
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT user_id FROM users WHERE premium_stars_charge_id = %s",
                (cid,),
            )
            row = cur.fetchone()
            if row:
                return int(row["user_id"])
            cur.execute(
                """
                SELECT owner_telegram_id AS user_id
                FROM premium_channels WHERE charge_id = %s
                """,
                (cid,),
            )
            row = cur.fetchone()
            if row:
                return int(row["user_id"])
            cur.execute(
                "SELECT invitee_id AS user_id FROM referral_credits WHERE charge_id = %s",
                (cid,),
            )
            row = cur.fetchone()
            if row:
                return int(row["user_id"])
            cur.execute(
                """
                SELECT user_id, premium_features FROM users
                WHERE position(%s in premium_features) > 0
                """,
                (cid,),
            )
            for r in cur.fetchall():
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
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT referrer_id, invitee_id, charge_id, stars_paid, commission_stars
                FROM referral_credits WHERE charge_id = %s
                """,
                (cid,),
            )
            row = cur.fetchone()
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
            cur = self._cursor(conn)
            cur.execute(
                "DELETE FROM referral_credits WHERE charge_id = %s",
                (cid,),
            )
            return int(cur.rowcount or 0) > 0

    def ensure_alert_share_token(
        self, owner_id: int, source_sub_id: int, snapshot: dict[str, Any]
    ) -> str:
        payload = json.dumps(snapshot, ensure_ascii=False)
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT token FROM alert_share_tokens
                WHERE source_sub_id = %s
                """,
                (int(source_sub_id),),
            )
            row = cur.fetchone()
            if row:
                token = str(row["token"])
                cur.execute(
                    """
                    UPDATE alert_share_tokens
                    SET owner_id = %s, snapshot_json = %s
                    WHERE token = %s
                    """,
                    (int(owner_id), payload, token),
                )
                return token
            for _ in range(8):
                token = secrets.token_urlsafe(12)
                try:
                    cur.execute(
                        """
                        INSERT INTO alert_share_tokens (
                            token, owner_id, source_sub_id, snapshot_json
                        ) VALUES (%s, %s, %s, %s)
                        """,
                        (token, int(owner_id), int(source_sub_id), payload),
                    )
                    return token
                except Exception:
                    conn.rollback()
                    cur = self._cursor(conn)
                    continue
            raise RuntimeError("failed to allocate alert share token")

    def get_alert_share_snapshot(self, token: str) -> dict[str, Any] | None:
        raw = (token or "").strip()
        if not raw:
            return None
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT snapshot_json FROM alert_share_tokens
                WHERE token = %s
                """,
                (raw,),
            )
            row = cur.fetchone()
        if not row:
            return None
        try:
            data = json.loads(row["snapshot_json"] or "{}")
        except Exception:
            return None
        return data if isinstance(data, dict) else None
