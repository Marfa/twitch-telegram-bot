from __future__ import annotations

import json
import logging
import os
import queue
import random
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .models import (
    WATCH_MAX_FILTERS,
    AlertHistoryEntry,
    BotStats,
    ChatAuth,
    DeletedSubscriptionCartItem,
    DonationAlertsAuth,
    DropsAuth,
    FollowMonitor,
    FollowMonitorEvent,
    FollowMonitorFollower,
    GiveawaysPrefs,
    GiveawayCatalogEntry,
    PremiumChannel,
    PremiumGift,
    PremiumPurchase,
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
    _row_to_follow_monitor,
    _row_to_follow_monitor_event,
    _row_to_follow_monitor_follower,
    _row_to_referral_withdrawal,
    _row_to_sub,
    _row_to_twitch_sync,
    _row_to_whisper_alert,
    _scheduled_broadcast_from_row,
    _subscription_cart_snapshot,
    dump_watch_filters,
    parse_watch_filters,
    watch_filter_auto_name,
)

logger = logging.getLogger(__name__)

# Concurrent queries without one stuck PG wait holding a process-wide lock
# (that pattern froze Telegram handlers during IGDB merge / premium scans).
_DEFAULT_POOL_SIZE = 8
_POOL_ACQUIRE_TIMEOUT_SEC = 60.0


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
        try:
            size = int(os.getenv("POSTGRES_POOL_SIZE", str(_DEFAULT_POOL_SIZE)) or _DEFAULT_POOL_SIZE)
        except ValueError:
            size = _DEFAULT_POOL_SIZE
        self._pool_size = max(2, min(32, size))
        self._pool: queue.Queue[Any] = queue.Queue(maxsize=self._pool_size)
        self._pool_created = 0
        self._pool_create_lock = threading.Lock()
        self._init_schema()
        logger.info(
            "Database: PostgreSQL (DATABASE_URL) pool_size=%s", self._pool_size
        )

    def _new_connection(self) -> Any:
        return self._psycopg.connect(self._dsn, connect_timeout=30)

    def _acquire(self) -> Any:
        """Borrow a pooled connection; never blocks other borrowers on PG wait."""
        end = time.monotonic() + _POOL_ACQUIRE_TIMEOUT_SEC
        while True:
            try:
                conn = self._pool.get_nowait()
            except queue.Empty:
                conn = None
            if conn is not None:
                if getattr(conn, "closed", True):
                    with self._pool_create_lock:
                        self._pool_created = max(0, self._pool_created - 1)
                    continue
                return conn
            with self._pool_create_lock:
                if self._pool_created < self._pool_size:
                    self._pool_created += 1
                    try:
                        return self._new_connection()
                    except Exception:
                        self._pool_created -= 1
                        raise
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"PostgreSQL pool exhausted (size={self._pool_size})"
                )
            try:
                conn = self._pool.get(timeout=min(remaining, 5.0))
            except queue.Empty:
                continue
            if getattr(conn, "closed", True):
                with self._pool_create_lock:
                    self._pool_created = max(0, self._pool_created - 1)
                continue
            return conn

    def _release(self, conn: Any, *, discard: bool = False) -> None:
        if discard or getattr(conn, "closed", True):
            try:
                if conn is not None and not getattr(conn, "closed", True):
                    conn.close()
            except Exception:
                pass
            with self._pool_create_lock:
                self._pool_created = max(0, self._pool_created - 1)
            return
        try:
            self._pool.put_nowait(conn)
        except queue.Full:
            try:
                conn.close()
            except Exception:
                pass
            with self._pool_create_lock:
                self._pool_created = max(0, self._pool_created - 1)

    @contextmanager
    def _conn(self) -> Iterator[Any]:
        conn = self._acquire()
        discard = False
        try:
            yield conn
            conn.commit()
        except Exception:
            discard = True
            try:
                if not getattr(conn, "closed", True):
                    conn.rollback()
            except Exception:
                pass
            raise
        finally:
            self._release(conn, discard=discard)

    @contextmanager
    def _bulk_conn(self) -> Iterator[Any]:
        # Dedicated connection outside the pool — long IGDB / follower rewrites
        # must not occupy a pool slot for minutes.
        conn = self._new_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

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
                ADD COLUMN IF NOT EXISTS paused_for_reauth BOOLEAN NOT NULL DEFAULT FALSE
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
                ADD COLUMN IF NOT EXISTS notify_on_drops
                BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS drops_game_id
                TEXT NOT NULL DEFAULT ''
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS release_watch_prefs
                TEXT NOT NULL DEFAULT ''
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS giveaway_watch_prefs
                TEXT NOT NULL DEFAULT ''
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
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS attach_live_remind_button
                BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS notify_on_schedule_cancel
                BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS schedule_cancel_template
                TEXT NOT NULL DEFAULT ''
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS schedule_cancel_notified_days
                TEXT NOT NULL DEFAULT '[]'
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS custom_buttons
                TEXT NOT NULL DEFAULT '[]'
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS multistream_channels
                TEXT NOT NULL DEFAULT '[]'
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS button_style
                TEXT NOT NULL DEFAULT ''
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS pin_message
                BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS pinned_message_id BIGINT
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS top_donations
                BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            cur.execute(
                """
                ALTER TABLE subscriptions
                ADD COLUMN IF NOT EXISTS top_donations_template
                TEXT NOT NULL DEFAULT ''
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS donationalerts_auth (
                    owner_id BIGINT PRIMARY KEY,
                    da_user_id TEXT NOT NULL DEFAULT '',
                    da_code TEXT NOT NULL DEFAULT '',
                    refresh_token TEXT NOT NULL DEFAULT '',
                    access_token TEXT NOT NULL DEFAULT '',
                    access_expires_at BIGINT NOT NULL DEFAULT 0
                )
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
                ADD COLUMN IF NOT EXISTS receive_beta_updates BOOLEAN NOT NULL DEFAULT FALSE
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
                ADD COLUMN IF NOT EXISTS vacation_auto_exit_at TEXT
                """
            )
            cur.execute(
                """
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS vacation_ends_at TEXT
                """
            )
            cur.execute(
                """
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS schedule_twitch_user_id TEXT NOT NULL DEFAULT ''
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
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS global_ignore_igdb TEXT NOT NULL DEFAULT '[]'
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
                "premium_refund_surcharge TEXT NOT NULL DEFAULT ''",
                "advanced_mode INTEGER",
                "message_draft INTEGER",
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
                CREATE TABLE IF NOT EXISTS premium_purchases (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    charge_id TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    stars INTEGER NOT NULL DEFAULT 0,
                    features TEXT NOT NULL DEFAULT '',
                    until_unix BIGINT NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT '',
                    source_feature TEXT NOT NULL DEFAULT '',
                    paid_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    digest_sent_at TIMESTAMPTZ
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_premium_purchases_digest
                ON premium_purchases(digest_sent_at)
                """
            )
            cur.execute(
                """
                ALTER TABLE premium_purchases
                ADD COLUMN IF NOT EXISTS is_renewal BOOLEAN NOT NULL DEFAULT FALSE
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
            cur.execute("DROP TABLE IF EXISTS lucky_templates")
            # Legacy Render/Aiven status RSS dedupe — removed after VPS migration.
            cur.execute("DROP TABLE IF EXISTS render_status_seen")
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
                ALTER TABLE twitch_sync
                ADD COLUMN IF NOT EXISTS needs_reauth BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            cur.execute(
                """
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS premium_twitch_needs_reauth
                BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            cur.execute(
                """
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS twitch_reauth_notified_at TIMESTAMPTZ
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
                ALTER TABLE whisper_alerts
                ADD COLUMN IF NOT EXISTS paused_for_reauth BOOLEAN NOT NULL DEFAULT FALSE
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
                CREATE TABLE IF NOT EXISTS follow_monitor (
                    owner_id BIGINT PRIMARY KEY,
                    enabled BOOLEAN NOT NULL DEFAULT FALSE,
                    twitch_user_id TEXT NOT NULL DEFAULT '',
                    twitch_login TEXT NOT NULL DEFAULT '',
                    refresh_token TEXT NOT NULL DEFAULT '',
                    next_sync_at TIMESTAMPTZ,
                    last_sync_at TIMESTAMPTZ,
                    needs_reauth BOOLEAN NOT NULL DEFAULT FALSE,
                    baseline_done BOOLEAN NOT NULL DEFAULT FALSE,
                    notify_follow BOOLEAN NOT NULL DEFAULT FALSE,
                    notify_unfollow BOOLEAN NOT NULL DEFAULT FALSE
                )
                """
            )
            cur.execute(
                """
                ALTER TABLE follow_monitor
                ADD COLUMN IF NOT EXISTS notify_follow BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            cur.execute(
                """
                ALTER TABLE follow_monitor
                ADD COLUMN IF NOT EXISTS notify_unfollow BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS follow_monitor_followers (
                    owner_id BIGINT NOT NULL,
                    twitch_user_id TEXT NOT NULL,
                    login TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    followed_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (owner_id, twitch_user_id)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_follow_monitor_followers_login
                ON follow_monitor_followers(owner_id, lower(login))
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS follow_monitor_events (
                    id BIGSERIAL PRIMARY KEY,
                    owner_id BIGINT NOT NULL,
                    event_type TEXT NOT NULL,
                    twitch_user_id TEXT NOT NULL DEFAULT '',
                    login TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    detected_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_follow_monitor_events_owner
                ON follow_monitor_events(owner_id, event_type, detected_at DESC)
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
                CREATE TABLE IF NOT EXISTS drops_auth (
                    owner_id BIGINT PRIMARY KEY,
                    twitch_user_id TEXT NOT NULL DEFAULT '',
                    twitch_login TEXT NOT NULL DEFAULT '',
                    refresh_token TEXT NOT NULL DEFAULT '',
                    digest_enabled BOOLEAN NOT NULL DEFAULT FALSE
                )
                """
            )
            cur.execute(
                """
                ALTER TABLE drops_auth
                ADD COLUMN IF NOT EXISTS digest_enabled BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            cur.execute(
                """
                ALTER TABLE drops_auth
                ADD COLUMN IF NOT EXISTS access_token TEXT NOT NULL DEFAULT ''
                """
            )
            cur.execute(
                """
                ALTER TABLE drops_auth
                ADD COLUMN IF NOT EXISTS access_expires_at BIGINT NOT NULL DEFAULT 0
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS drop_campaign_seen (
                    owner_id BIGINT NOT NULL,
                    campaign_id TEXT NOT NULL,
                    subscription_id BIGINT NOT NULL,
                    first_seen_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (owner_id, campaign_id, subscription_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS drop_claim_seen (
                    owner_id BIGINT NOT NULL,
                    drop_id TEXT NOT NULL,
                    first_seen_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (owner_id, drop_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS drop_stream_seen (
                    owner_id BIGINT NOT NULL,
                    subscription_id BIGINT NOT NULL,
                    stream_id TEXT NOT NULL,
                    first_seen_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (owner_id, subscription_id, stream_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS giveaways_prefs (
                    owner_id BIGINT PRIMARY KEY,
                    stores_json TEXT NOT NULL DEFAULT '[]',
                    platforms_json TEXT NOT NULL DEFAULT '[]',
                    digest_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                    first_digest_sent BOOLEAN NOT NULL DEFAULT FALSE,
                    last_digest_at BIGINT NOT NULL DEFAULT 0
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS giveaway_seen (
                    owner_id BIGINT NOT NULL,
                    source TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    seen_at BIGINT NOT NULL,
                    PRIMARY KEY (owner_id, source, external_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS giveaways_catalog (
                    source TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    store_id TEXT NOT NULL DEFAULT '',
                    platforms_json TEXT NOT NULL DEFAULT '[]',
                    claim_url TEXT NOT NULL DEFAULT '',
                    start_at TEXT NOT NULL DEFAULT '',
                    end_at TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    image_url TEXT NOT NULL DEFAULT '',
                    dedupe_key TEXT NOT NULL DEFAULT '',
                    igdb_id BIGINT,
                    name TEXT NOT NULL DEFAULT '',
                    year TEXT NOT NULL DEFAULT '',
                    publisher TEXT NOT NULL DEFAULT '',
                    developer TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    cover_url TEXT NOT NULL DEFAULT '',
                    refreshed_at BIGINT NOT NULL DEFAULT 0,
                    PRIMARY KEY (source, external_id)
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
                CREATE TABLE IF NOT EXISTS beta_feature_announcements (
                    feature_id TEXT PRIMARY KEY,
                    announced_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )


            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS igdb_games (
                    id BIGINT PRIMARY KEY,
                    name TEXT NOT NULL,
                    version_parent BIGINT,
                    first_release_date BIGINT,
                    total_rating_count INTEGER NOT NULL DEFAULT 0,
                    genres TEXT NOT NULL DEFAULT '',
                    game_modes TEXT NOT NULL DEFAULT '',
                    cover_id BIGINT,
                    summary TEXT NOT NULL DEFAULT '',
                    slug TEXT NOT NULL DEFAULT ''
                )
                """
            )
            cur.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'igdb_games'
                  AND column_name IN ('summary', 'slug')
                """
            )
            igdb_game_cols = {str(r["column_name"]) for r in cur.fetchall()}
            if "summary" not in igdb_game_cols:
                cur.execute(
                    "ALTER TABLE igdb_games ADD COLUMN summary TEXT NOT NULL DEFAULT ''"
                )
                # Force games dump re-import so summaries fill after schema bump.
                cur.execute("DELETE FROM igdb_dump_state WHERE endpoint = 'games'")
            if "slug" not in igdb_game_cols:
                cur.execute(
                    "ALTER TABLE igdb_games ADD COLUMN slug TEXT NOT NULL DEFAULT ''"
                )
                cur.execute("DELETE FROM igdb_dump_state WHERE endpoint = 'games'")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_igdb_games_name_lower ON igdb_games (LOWER(name))"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_igdb_games_release ON igdb_games(first_release_date)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_igdb_games_rating ON igdb_games(total_rating_count)"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS igdb_companies (
                    id BIGINT PRIMARY KEY,
                    name TEXT NOT NULL
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_igdb_companies_name_lower ON igdb_companies (LOWER(name))"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS igdb_genres (
                    id BIGINT PRIMARY KEY,
                    name TEXT NOT NULL
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_igdb_genres_name_lower ON igdb_genres (LOWER(name))"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS igdb_game_modes (
                    id BIGINT PRIMARY KEY,
                    name TEXT NOT NULL
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_igdb_game_modes_name_lower ON igdb_game_modes (LOWER(name))"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS igdb_external_twitch (
                    twitch_uid TEXT PRIMARY KEY,
                    game_id BIGINT NOT NULL
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_igdb_ext_twitch_game ON igdb_external_twitch(game_id)"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS igdb_external_steam (
                    steam_uid TEXT PRIMARY KEY,
                    game_id BIGINT NOT NULL
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_igdb_ext_steam_game ON igdb_external_steam(game_id)"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS igdb_involved (
                    game_id BIGINT NOT NULL,
                    company_id BIGINT NOT NULL,
                    is_developer BOOLEAN NOT NULL DEFAULT FALSE,
                    is_publisher BOOLEAN NOT NULL DEFAULT FALSE
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_igdb_involved_game ON igdb_involved(game_id)"
            )
            # Dedupe before unique index (legacy rows may lack a PK).
            cur.execute(
                """
                DELETE FROM igdb_involved a
                USING igdb_involved b
                WHERE a.ctid < b.ctid
                  AND a.game_id = b.game_id
                  AND a.company_id = b.company_id
                """
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_igdb_involved_uniq
                ON igdb_involved(game_id, company_id)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS igdb_covers (
                    id BIGINT PRIMARY KEY,
                    game_id BIGINT,
                    image_id TEXT NOT NULL
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_igdb_covers_game ON igdb_covers(game_id)"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS igdb_artworks (
                    id BIGINT PRIMARY KEY,
                    game_id BIGINT,
                    image_id TEXT NOT NULL
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_igdb_artworks_game ON igdb_artworks(game_id)"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS igdb_platforms (
                    id BIGINT PRIMARY KEY,
                    name TEXT NOT NULL
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_igdb_platforms_name_lower ON igdb_platforms (LOWER(name))"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS igdb_release_dates (
                    id BIGINT PRIMARY KEY,
                    game_id BIGINT NOT NULL,
                    platform_id BIGINT NOT NULL DEFAULT 0,
                    date BIGINT NOT NULL,
                    human TEXT NOT NULL DEFAULT ''
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_igdb_release_dates_game ON igdb_release_dates(game_id)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_igdb_release_dates_date ON igdb_release_dates(date)"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS igdb_dump_state (
                    endpoint TEXT PRIMARY KEY,
                    dump_updated_at BIGINT NOT NULL DEFAULT 0,
                    synced_at BIGINT NOT NULL DEFAULT 0,
                    row_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # DeepL cache for IGDB EN summaries → bot locales (not a partner dump).
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS igdb_summary_translations (
                    source_hash TEXT NOT NULL,
                    lang TEXT NOT NULL,
                    translated TEXT NOT NULL,
                    PRIMARY KEY (source_hash, lang)
                )
                """
            )
            cur.execute("SELECT COUNT(*) AS n FROM igdb_external_steam")
            steam_n = cur.fetchone()
            if int((steam_n["n"] if steam_n else 0) or 0) <= 0:
                cur.execute(
                    "DELETE FROM igdb_dump_state WHERE endpoint = 'external_games'"
                )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS stream_poll_snapshot (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    payload TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schedule_day_snapshot (
                    twitch_user_id TEXT PRIMARY KEY,
                    days_json TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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
                    purpose TEXT NOT NULL DEFAULT 'share',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                ALTER TABLE alert_share_tokens
                ADD COLUMN IF NOT EXISTS purpose TEXT NOT NULL DEFAULT 'share'
                """
            )
            cur.execute("DROP INDEX IF EXISTS idx_alert_share_tokens_sub")
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_alert_share_tokens_sub_purpose
                ON alert_share_tokens(source_sub_id, purpose)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_alert_jobs (
                    job_name TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    sub_id BIGINT NOT NULL,
                    due_at TIMESTAMPTZ NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_pending_alert_jobs_due
                ON pending_alert_jobs(due_at)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS premium_gifts (
                    token TEXT PRIMARY KEY,
                    buyer_id BIGINT NOT NULL,
                    kind TEXT NOT NULL,
                    charge_id TEXT NOT NULL UNIQUE,
                    stars INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    image_file_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    recipient_id BIGINT NOT NULL DEFAULT 0,
                    until_unix BIGINT NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    redeemed_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_premium_gifts_charge
                ON premium_gifts(charge_id)
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
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_flags (
                    name TEXT PRIMARY KEY
                )
                """
            )
            cur.execute(
                "SELECT 1 FROM schema_flags WHERE name = 'digest_cd_minutes_v1'"
            )
            if cur.fetchone() is None:
                cur.execute(
                    """
                    UPDATE subscriptions
                    SET suppress_repeat_minutes = 60
                    WHERE suppress_repeat_minutes = 0
                      AND (
                        COALESCE(notify_on_drops, FALSE) = TRUE
                        OR COALESCE(category_watch_prefs, '') != ''
                      )
                    """
                )
                cur.execute(
                    "INSERT INTO schema_flags(name) VALUES ('digest_cd_minutes_v1')"
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
        strip_name_mentions: bool = False,
        attach_chat_button: bool = False,
        attach_live_remind_button: bool = False,
        custom_buttons: str = "[]",
        multistream_channels: str = "[]",
        button_style: str = "",
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
        release_watch_prefs: str = "",
        giveaway_watch_prefs: str = "",
        notify_on_live: bool = True,
        notify_on_end: bool = False,
        notify_on_category_change: bool = False,
        notify_on_drops: bool = False,
        drops_game_id: str = "",
        delete_other_alerts: bool = False,
        pin_message: bool = False,
        top_donations: bool = False,
        top_donations_template: str = "",
        is_demo: bool = False,
        notify_on_schedule_cancel: bool = False,
        schedule_cancel_template: str = "",
    ) -> int:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO subscriptions (
owner_id, twitch_username, twitch_user_id,
                    message_template, dest_type, chat_id, thread_id,
                    delete_previous, notify_delete_fail, disable_link_preview,
                    strip_name_mentions, attach_chat_button, attach_live_remind_button,
                    custom_buttons, multistream_channels, button_style,
                    delay_minutes, suppress_repeat_minutes, schedule_reminder_minutes,
                    schedule_reminder_configured, ignore_keywords, use_global_ignore,
                    image_file_id, image_position, enabled, from_twitch_sync,
                    from_watch_suggest, category_watch_prefs, release_watch_prefs, giveaway_watch_prefs,
                    notify_on_live, notify_on_end, notify_on_category_change,
                    notify_on_drops, drops_game_id,
                    delete_other_alerts, pin_message,
                    top_donations, top_donations_template, is_demo,
                    notify_on_schedule_cancel, schedule_cancel_template
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                    bool(attach_live_remind_button),
                    custom_buttons if str(custom_buttons or "").strip() else "[]",
                    (
                        multistream_channels
                        if str(multistream_channels or "").strip()
                        else "[]"
                    ),
                    (
                        str(button_style or "").strip().lower()
                        if str(button_style or "").strip().lower()
                        in ("primary", "success", "danger")
                        else ""
                    ),
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
                    str(release_watch_prefs or ""),
                    str(giveaway_watch_prefs or ""),
                    bool(notify_on_live),
                    bool(notify_on_end),
                    bool(notify_on_category_change),
                    bool(notify_on_drops),
                    str(drops_game_id or ""),
                    bool(delete_other_alerts),
                    bool(pin_message),
                    bool(top_donations),
                    str(top_donations_template or ""),
                    bool(is_demo),
                    bool(notify_on_schedule_cancel),
                    str(schedule_cancel_template or ""),
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

    def set_pinned_message_id(self, sub_id: int, message_id: int | None) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                UPDATE subscriptions
                SET pinned_message_id = %s
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

    def clear_notify_cooldown(self, sub_id: int) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "UPDATE subscriptions SET notify_cooldown_until = NULL WHERE id = %s",
                (sub_id,),
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
                    notify_on_drops=bool(payload.get("notify_on_drops")),
                    schedule_reminder_configured=bool(
                        payload.get("schedule_reminder_configured")
                    ),
                    release_watch_prefs=str(payload.get("release_watch_prefs") or ""),
                    giveaway_watch_prefs=str(
                        payload.get("giveaway_watch_prefs") or ""
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
            # Drop keys add_subscription does not accept.
            payload.pop("sync_user_edited", None)
            payload.pop("category_watch_live_ids", None)
            payload.pop("category_watch_primed", None)
            self.add_subscription(owner_id=owner_id, **{
                k: payload[k]
                for k in (
                    "twitch_username",
                    "twitch_user_id",
                    "message_template",
                    "dest_type",
                    "chat_id",
                    "thread_id",
                    "delete_previous",
                    "notify_delete_fail",
                    "disable_link_preview",
                    "strip_name_mentions",
                    "attach_chat_button",
                    "attach_live_remind_button",
                    "custom_buttons",
                    "multistream_channels",
                    "button_style",
                    "delay_minutes",
                    "suppress_repeat_minutes",
                    "schedule_reminder_minutes",
                    "schedule_reminder_configured",
                    "ignore_keywords",
                    "use_global_ignore",
                    "image_file_id",
                    "image_position",
                    "enabled",
                    "from_twitch_sync",
                    "from_watch_suggest",
                    "category_watch_prefs",
                    "release_watch_prefs",
                    "giveaway_watch_prefs",
                    "notify_on_live",
                    "notify_on_end",
                    "notify_on_category_change",
                    "notify_on_drops",
                    "drops_game_id",
                    "delete_other_alerts",
                    "pin_message",
                    "top_donations",
                    "top_donations_template",
                    "is_demo",
                    "notify_on_schedule_cancel",
                    "schedule_cancel_template",
                )
                if k in payload
            })
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

    def discard_deleted_subscriptions(
        self,
        owner_id: int,
        cart_ids: list[int],
        *,
        is_demo: bool,
    ) -> int:
        cart_ids = [int(i) for i in cart_ids if str(i)]
        if not cart_ids:
            return 0
        with self._conn() as conn:
            cur = self._cursor(conn)
            placeholders = ",".join(["%s" for _ in cart_ids])
            cur.execute(
                f"""
                DELETE FROM deleted_subscriptions_cart
                WHERE owner_id = %s
                  AND is_demo = %s
                  AND id IN ({placeholders})
                """,
                (owner_id, bool(is_demo), *cart_ids),
            )
            return int(cur.rowcount or 0)

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
            "attach_live_remind_button",
            "custom_buttons",
            "multistream_channels",
            "button_style",
            "delay_minutes",
            "suppress_repeat_minutes",
            "schedule_reminder_minutes",
            "schedule_reminder_configured",
            "notify_on_live",
            "notify_on_end",
            "notify_on_category_change",
            "notify_on_drops",
            "drops_game_id",
            "delete_other_alerts",
            "pin_message",
            "top_donations",
            "top_donations_template",
            "ignore_keywords",
            "use_global_ignore",
            "image_file_id",
            "image_position",
            "twitch_username",
            "twitch_user_id",
            "category_watch_prefs",
            "release_watch_prefs",
            "giveaway_watch_prefs",
            "notify_on_schedule_cancel",
            "schedule_cancel_template",
            "schedule_cancel_notified_days",
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
                "attach_live_remind_button",
                "schedule_reminder_configured",
                "notify_on_live",
                "notify_on_end",
                "notify_on_category_change",
                "notify_on_drops",
                "delete_other_alerts",
                "pin_message",
                "top_donations",
                "use_global_ignore",
                "notify_on_schedule_cancel",
            ):
                values.append(bool(value))
            elif key in (
                "delay_minutes",
                "suppress_repeat_minutes",
                "schedule_reminder_minutes",
            ):
                values.append(max(0, int(value)))
            elif key in (
                "ignore_keywords",
                "drops_game_id",
                "twitch_username",
                "twitch_user_id",
                "category_watch_prefs",
                "release_watch_prefs",
                "giveaway_watch_prefs",
                "custom_buttons",
                "multistream_channels",
                "button_style",
                "schedule_cancel_template",
                "schedule_cancel_notified_days",
                "top_donations_template",
            ):
                if key == "button_style":
                    from custom_buttons import normalize_button_style

                    values.append(normalize_button_style(str(value or "")))
                else:
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
                  AND COALESCE(release_watch_prefs, '') = ''
                  AND COALESCE(giveaway_watch_prefs, '') = ''
                  AND twitch_user_id NOT LIKE 'cw:%'
                  AND twitch_user_id NOT LIKE 'drops:%'
                  AND twitch_user_id NOT LIKE 'rel:%'
                  AND twitch_user_id NOT LIKE 'gvw:%'
                  AND COALESCE(notify_on_drops, FALSE) = FALSE
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

    def get_enabled_drops_subscriptions(self) -> list[Subscription]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT * FROM subscriptions
                WHERE enabled = TRUE
                  AND notify_on_drops = TRUE
                  AND COALESCE(drops_game_id, '') != ''
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
                WHERE enabled = TRUE
                  AND (
                    schedule_reminder_minutes > 0
                    OR (
                      notify_on_schedule_cancel = TRUE
                      AND COALESCE(NULLIF(TRIM(schedule_cancel_template), ''), '')
                          <> ''
                    )
                  )
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

    def get_subs_with_pinned_message(
        self, twitch_user_id: str | None = None
    ) -> list[Subscription]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            if twitch_user_id is None:
                cur.execute(
                    """
                    SELECT * FROM subscriptions
                    WHERE pinned_message_id IS NOT NULL
                    ORDER BY id
                    """
                )
            else:
                cur.execute(
                    """
                    SELECT * FROM subscriptions
                    WHERE twitch_user_id = %s
                      AND pinned_message_id IS NOT NULL
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

    def get_owners_with_enabled_subscriptions(self) -> list[int]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT DISTINCT owner_id FROM subscriptions
                WHERE enabled = TRUE AND is_demo = FALSE
                ORDER BY owner_id
                """
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

    def ping(self) -> bool:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute("SELECT 1")
            return cur.fetchone() is not None

    def count_users(self) -> int:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute("SELECT COUNT(*) AS n FROM users")
            row = cur.fetchone()
        return int(row["n"]) if row else 0

    def list_lucky_monthly_candidate_ids(self) -> list[int]:
        """Non-blocked users without lifetime or active Stars (features filtered in app)."""
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT user_id FROM users
                WHERE COALESCE(bot_blocked, FALSE) = FALSE
                  AND COALESCE(premium_permanent, FALSE) = FALSE
                  AND COALESCE(premium_stars_until, 0)
                      <= EXTRACT(EPOCH FROM NOW())::BIGINT
                ORDER BY user_id
                """
            )
            rows = cur.fetchall()
        return [int(r["user_id"]) for r in rows]

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

    def count_new_users_between(self, since: datetime, until: datetime) -> int:
        since_utc = since.astimezone(timezone.utc) if since.tzinfo else since.replace(tzinfo=timezone.utc)
        until_utc = until.astimezone(timezone.utc) if until.tzinfo else until.replace(tzinfo=timezone.utc)
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT COUNT(*) AS n FROM users WHERE first_seen >= %s AND first_seen < %s",
                (since_utc, until_utc),
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

    def count_stars_payers_between(self, since: datetime, until: datetime) -> int:
        since_utc = since.astimezone(timezone.utc) if since.tzinfo else since.replace(tzinfo=timezone.utc)
        until_utc = until.astimezone(timezone.utc) if until.tzinfo else until.replace(tzinfo=timezone.utc)
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT COUNT(*) AS n FROM users
                WHERE premium_stars_paid_at IS NOT NULL
                  AND premium_stars_paid_at >= %s
                  AND premium_stars_paid_at < %s
                """,
                (since_utc, until_utc),
            )
            row = cur.fetchone()
        return int(row["n"]) if row else 0

    def record_premium_purchase(
        self,
        *,
        user_id: int,
        charge_id: str,
        kind: str,
        stars: int,
        features: str = "",
        until_unix: int = 0,
        source: str = "",
        source_feature: str = "",
        is_renewal: bool = False,
    ) -> bool:
        cid = str(charge_id or "").strip()
        if int(user_id) <= 0 or not cid:
            return False
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO premium_purchases (
                    user_id, charge_id, kind, stars, features, until_unix,
                    source, source_feature, is_renewal
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (charge_id) DO NOTHING
                """,
                (
                    int(user_id),
                    cid,
                    str(kind or "").strip() or "unknown",
                    max(0, int(stars or 0)),
                    str(features or "").strip(),
                    max(0, int(until_unix or 0)),
                    str(source or "").strip(),
                    str(source_feature or "").strip(),
                    bool(is_renewal),
                ),
            )
            return int(cur.rowcount or 0) > 0

    def get_premium_purchase_by_charge(self, charge_id: str) -> PremiumPurchase | None:
        cid = str(charge_id or "").strip()
        if not cid:
            return None
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT id, user_id, charge_id, kind, stars, features, until_unix,
                       source, source_feature, paid_at, is_renewal
                FROM premium_purchases
                WHERE charge_id = %s
                """,
                (cid,),
            )
            r = cur.fetchone()
        if not r:
            return None
        paid = r["paid_at"]
        paid_s = (
            paid.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            if hasattr(paid, "astimezone")
            else str(paid or "")
        )
        return PremiumPurchase(
            id=int(r["id"]),
            user_id=int(r["user_id"]),
            charge_id=str(r["charge_id"] or ""),
            kind=str(r["kind"] or ""),
            stars=int(r["stars"] or 0),
            features=str(r["features"] or ""),
            until_unix=int(r["until_unix"] or 0),
            source=str(r["source"] or ""),
            source_feature=str(r["source_feature"] or ""),
            paid_at=paid_s,
            is_renewal=bool(r["is_renewal"]) if "is_renewal" in r.keys() else False,
        )

    def get_premium_refund_surcharge(self, user_id: int) -> dict[str, int]:
        import json

        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT COALESCE(premium_refund_surcharge, '') AS premium_refund_surcharge
                FROM users WHERE user_id = %s
                """,
                (int(user_id),),
            )
            row = cur.fetchone()
        if not row:
            return {}
        raw = str(row["premium_refund_surcharge"] or "").strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        out: dict[str, int] = {}
        for k, v in data.items():
            key = str(k or "").strip()
            try:
                n = int(v)
            except (TypeError, ValueError):
                continue
            if key and n > 0:
                out[key] = n
        return out

    def set_premium_refund_surcharge(
        self, user_id: int, surcharge: dict[str, int]
    ) -> None:
        import json

        clean = {
            str(k).strip(): int(v)
            for k, v in (surcharge or {}).items()
            if str(k).strip() and int(v) > 0
        }
        blob = json.dumps(clean, separators=(",", ":"), sort_keys=True) if clean else ""
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO users (user_id, premium_refund_surcharge)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    premium_refund_surcharge = EXCLUDED.premium_refund_surcharge
                """,
                (int(user_id), blob),
            )

    def list_undigested_premium_purchases(self) -> list[PremiumPurchase]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT id, user_id, charge_id, kind, stars, features, until_unix,
                       source, source_feature, paid_at, is_renewal
                FROM premium_purchases
                WHERE digest_sent_at IS NULL
                ORDER BY paid_at, id
                """
            )
            rows = cur.fetchall()
        out: list[PremiumPurchase] = []
        for r in rows:
            paid = r["paid_at"]
            paid_s = (
                paid.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                if hasattr(paid, "astimezone")
                else str(paid or "")
            )
            out.append(
                PremiumPurchase(
                    id=int(r["id"]),
                    user_id=int(r["user_id"]),
                    charge_id=str(r["charge_id"] or ""),
                    kind=str(r["kind"] or ""),
                    stars=int(r["stars"] or 0),
                    features=str(r["features"] or ""),
                    until_unix=int(r["until_unix"] or 0),
                    source=str(r["source"] or ""),
                    source_feature=str(r["source_feature"] or ""),
                    paid_at=paid_s,
                    is_renewal=bool(r["is_renewal"]) if "is_renewal" in r.keys() else False,
                )
            )
        return out

    def mark_premium_purchases_digested(self, purchase_ids: list[int]) -> int:
        ids = [int(i) for i in purchase_ids if int(i) > 0]
        if not ids:
            return 0
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                UPDATE premium_purchases
                SET digest_sent_at = NOW()
                WHERE digest_sent_at IS NULL AND id = ANY(%s)
                """,
                (ids,),
            )
            return int(cur.rowcount or 0)

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
            cur.execute("DELETE FROM follow_monitor_events WHERE owner_id = %s", (uid,))
            cur.execute(
                "DELETE FROM follow_monitor_followers WHERE owner_id = %s", (uid,)
            )
            cur.execute("DELETE FROM follow_monitor WHERE owner_id = %s", (uid,))
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

    def get_enabled_subscriptions_by_chat_id(
        self, chat_id: int
    ) -> list[Subscription]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT * FROM subscriptions
                WHERE chat_id = %s AND enabled = TRUE
                ORDER BY id
                """,
                (chat_id,),
            )
            rows = cur.fetchall()
        return [_row_to_sub(r) for r in rows]

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

    def get_receive_beta_updates(self, user_id: int) -> bool:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT receive_beta_updates FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        if not row:
            return False
        return bool(row["receive_beta_updates"])

    def set_receive_beta_updates(self, user_id: int, enabled: bool) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO users (user_id, receive_beta_updates) VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    receive_beta_updates = EXCLUDED.receive_beta_updates
                """,
                (user_id, enabled),
            )

    def get_beta_update_recipients(self) -> list[int]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT DISTINCT ids.uid AS user_id
                FROM (
                    SELECT user_id AS uid FROM users
                    UNION
                    SELECT owner_id AS uid FROM subscriptions
                ) AS ids
                LEFT JOIN users u ON u.user_id = ids.uid
                WHERE COALESCE(u.bot_blocked, FALSE) = FALSE
                  AND COALESCE(u.receive_beta_updates, FALSE) = TRUE
                """
            )
            rows = cur.fetchall()
        return [int(r["user_id"]) for r in rows]

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

    def get_global_ignore_igdb(self, user_id: int) -> list[dict]:
        from twitch import parse_ignore_igdb_entries

        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT global_ignore_igdb FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        if not row:
            return []
        return parse_ignore_igdb_entries(row["global_ignore_igdb"])

    def set_global_ignore_igdb(self, user_id: int, entries: list[dict]) -> None:
        from twitch import dump_ignore_igdb_entries

        payload = dump_ignore_igdb_entries(entries)
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO users (user_id, global_ignore_igdb) VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    global_ignore_igdb = EXCLUDED.global_ignore_igdb
                """,
                (user_id, payload),
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

    def is_message_draft_enabled(self, user_id: int) -> bool:
        """Progressive sendMessageDraft; default on when unset."""
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT message_draft FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        if row is None or row["message_draft"] is None:
            return True
        return bool(row["message_draft"])

    def set_message_draft_enabled(self, user_id: int, enabled: bool) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO users (user_id, message_draft) VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    message_draft = EXCLUDED.message_draft
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
                    OR COALESCE(pin_message, FALSE)
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

    def set_vacation_auto_exit_at(self, user_id: int, exit_at: str | None) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO users (user_id, vacation_auto_exit_at)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    vacation_auto_exit_at = EXCLUDED.vacation_auto_exit_at
                """,
                (user_id, exit_at),
            )

    def get_vacation_auto_exit_at(self, user_id: int) -> str | None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT vacation_auto_exit_at FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        val = row["vacation_auto_exit_at"]
        return str(val) if val else None

    def set_vacation_ends_at(self, user_id: int, ends_at: str | None) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO users (user_id, vacation_ends_at)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    vacation_ends_at = EXCLUDED.vacation_ends_at
                """,
                (user_id, ends_at),
            )

    def get_vacation_ends_at(self, user_id: int) -> str | None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT vacation_ends_at FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        val = row["vacation_ends_at"]
        return str(val) if val else None

    def set_schedule_twitch_user_id(self, user_id: int, twitch_user_id: str | None) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO users (user_id, schedule_twitch_user_id)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    schedule_twitch_user_id = EXCLUDED.schedule_twitch_user_id
                """,
                (user_id, (twitch_user_id or "").strip()),
            )

    def get_schedule_twitch_user_id(self, user_id: int) -> str:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT schedule_twitch_user_id FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        if not row:
            return ""
        return str(row["schedule_twitch_user_id"] or "").strip()

    def get_due_vacation_auto_exits(self, now_iso: str) -> list[int]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT user_id FROM users
                WHERE vacation_auto_exit_at IS NOT NULL
                  AND vacation_auto_exit_at != ''
                  AND vacation_auto_exit_at <= %s
                """,
                (now_iso,),
            )
            rows = cur.fetchall()
        return [int(r["user_id"]) for r in rows]

    def has_any_vacation_auto_exit(self) -> bool:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT 1 FROM users
                WHERE vacation_auto_exit_at IS NOT NULL
                  AND vacation_auto_exit_at != ''
                LIMIT 1
                """
            )
            return cur.fetchone() is not None

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
        touch_paid_at: bool = True,
    ) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            if touch_paid_at:
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
            else:
                cur.execute(
                    """
                    INSERT INTO users (
                        user_id, premium_stars_charge_id, premium_stars_until,
                        premium_stars_canceled
                    )
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (user_id) DO UPDATE SET
                        premium_stars_charge_id = EXCLUDED.premium_stars_charge_id,
                        premium_stars_until = EXCLUDED.premium_stars_until,
                        premium_stars_canceled = EXCLUDED.premium_stars_canceled
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
        from token_crypto import try_decrypt_secret

        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT premium_twitch_refresh FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        if not row or not row["premium_twitch_refresh"]:
            return None
        plain = try_decrypt_secret(row["premium_twitch_refresh"])
        if plain is None:
            self.set_premium_twitch_refresh(user_id, "")
            self.set_premium_twitch_needs_reauth(user_id, True)
            return None
        return plain or None

    def set_premium_twitch_needs_reauth(self, user_id: int, needs: bool) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO users (user_id, premium_twitch_needs_reauth)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    premium_twitch_needs_reauth = EXCLUDED.premium_twitch_needs_reauth
                """,
                (user_id, bool(needs)),
            )

    def get_premium_twitch_needs_reauth(self, user_id: int) -> bool:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT premium_twitch_needs_reauth FROM users WHERE user_id = %s
                """,
                (user_id,),
            )
            row = cur.fetchone()
        if not row:
            return False
        try:
            return bool(row["premium_twitch_needs_reauth"])
        except (KeyError, IndexError, TypeError):
            return False

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

    def purge_stale_log_tables(self) -> dict[str, int]:
        from config import (
            CHAT_SEND_DAILY_RETENTION_DAYS,
            DROP_SEEN_RETENTION_DAYS,
            FOLLOW_MONITOR_EVENTS_RETENTION_DAYS,
        )
        from premium import ALERT_HISTORY_PREMIUM_DAYS, DELETED_SUBSCRIPTIONS_CART_MAX_DAYS

        removed: dict[str, int] = {}
        with self._conn() as conn:
            cur = self._cursor(conn)

            cur.execute(
                """
                DELETE FROM alert_history
                WHERE sent_at < NOW() - make_interval(days => %s)
                """,
                (int(ALERT_HISTORY_PREMIUM_DAYS),),
            )
            removed["alert_history"] = int(cur.rowcount or 0)

            cur.execute(
                """
                DELETE FROM deleted_subscriptions_cart
                WHERE deleted_at < NOW() - make_interval(days => %s)
                """,
                (int(DELETED_SUBSCRIPTIONS_CART_MAX_DAYS),),
            )
            removed["deleted_subscriptions_cart"] = int(cur.rowcount or 0)

            cur.execute(
                """
                DELETE FROM follow_monitor_events
                WHERE detected_at < NOW() - make_interval(days => %s)
                """,
                (int(FOLLOW_MONITOR_EVENTS_RETENTION_DAYS),),
            )
            removed["follow_monitor_events"] = int(cur.rowcount or 0)

            for table in (
                "drop_campaign_seen",
                "drop_claim_seen",
                "drop_stream_seen",
            ):
                cur.execute(
                    f"""
                    DELETE FROM {table}
                    WHERE first_seen_at < NOW() - make_interval(days => %s)
                    """,
                    (int(DROP_SEEN_RETENTION_DAYS),),
                )
                removed[table] = int(cur.rowcount or 0)

            # day is ISO date text YYYY-MM-DD (UTC calendar day, same as sqlite)
            day_cutoff = (
                datetime.now(timezone.utc)
                - timedelta(days=int(CHAT_SEND_DAILY_RETENTION_DAYS))
            ).date().isoformat()
            cur.execute(
                "DELETE FROM chat_send_daily WHERE day < %s",
                (day_cutoff,),
            )
            removed["chat_send_daily"] = int(cur.rowcount or 0)

        return {k: v for k, v in removed.items() if v > 0}

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
                WHERE locale = 'uk' AND COALESCE(bot_blocked, FALSE) = FALSE
                """
            )
            locale_uk = int(cur.fetchone()["c"])
            cur.execute(
                """
                SELECT COUNT(*) AS c FROM users
                WHERE locale = 'it' AND COALESCE(bot_blocked, FALSE) = FALSE
                """
            )
            locale_it = int(cur.fetchone()["c"])
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
            locale_uk=locale_uk,
            locale_it=locale_it,
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
                    period_days, next_sync_at, last_sync_at, needs_reauth
                ) VALUES (%s, %s, %s, %s, %s::timestamptz, %s::timestamptz, FALSE)
                ON CONFLICT (owner_id) DO UPDATE SET
                    twitch_user_id = EXCLUDED.twitch_user_id,
                    refresh_token = EXCLUDED.refresh_token,
                    period_days = EXCLUDED.period_days,
                    next_sync_at = EXCLUDED.next_sync_at,
                    last_sync_at = EXCLUDED.last_sync_at,
                    needs_reauth = FALSE
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
        from token_crypto import try_decrypt_secret

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
        plain = try_decrypt_secret(sync.refresh_token)
        if plain is None:
            self.set_twitch_sync_needs_reauth(owner_id, True)
            with self._conn() as conn:
                cur = self._cursor(conn)
                cur.execute(
                    "UPDATE twitch_sync SET refresh_token = '' WHERE owner_id = %s",
                    (owner_id,),
                )
            sync.refresh_token = ""
            sync.needs_reauth = True
            return sync
        sync.refresh_token = plain
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
                    next_sync_at = %s::timestamptz,
                    needs_reauth = FALSE
                WHERE owner_id = %s
                """,
                (enc, last_sync_at, next_sync_at, owner_id),
            )

    def get_due_twitch_syncs(self, now_iso: str) -> list[TwitchSync]:
        from token_crypto import try_decrypt_secret

        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT * FROM twitch_sync
                WHERE next_sync_at <= %s::timestamptz
                  AND COALESCE(needs_reauth, FALSE) = FALSE
                ORDER BY next_sync_at
                """,
                (now_iso,),
            )
            rows = cur.fetchall()
        out: list[TwitchSync] = []
        for r in rows:
            sync = _row_to_twitch_sync(r)
            plain = try_decrypt_secret(sync.refresh_token)
            if plain is None:
                self.set_twitch_sync_needs_reauth(sync.owner_id, True)
                with self._conn() as conn:
                    cur = self._cursor(conn)
                    cur.execute(
                        "UPDATE twitch_sync SET refresh_token = '' WHERE owner_id = %s",
                        (sync.owner_id,),
                    )
                sync.refresh_token = ""
                sync.needs_reauth = True
            else:
                sync.refresh_token = plain
            out.append(sync)
        return out

    def has_any_periodic_twitch_sync(self) -> bool:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT 1 FROM twitch_sync
                WHERE period_days > 0
                  AND COALESCE(refresh_token, '') != ''
                  AND COALESCE(needs_reauth, FALSE) = FALSE
                LIMIT 1
                """
            )
            return cur.fetchone() is not None

    def set_twitch_sync_needs_reauth(self, owner_id: int, needs: bool) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                UPDATE twitch_sync
                SET needs_reauth = %s
                WHERE owner_id = %s
                """,
                (bool(needs), owner_id),
            )

    def get_whisper_alert(self, owner_id: int) -> WhisperAlert | None:
        from token_crypto import try_decrypt_secret

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
        plain = try_decrypt_secret(alert.refresh_token)
        if plain is None:
            with self._conn() as conn:
                cur = self._cursor(conn)
                cur.execute(
                    "UPDATE whisper_alerts SET refresh_token = '' WHERE owner_id = %s",
                    (owner_id,),
                )
            alert.refresh_token = ""
        else:
            alert.refresh_token = plain
        return alert

    def get_whisper_alerts_by_twitch_user_id(
        self, twitch_user_id: str
    ) -> list[WhisperAlert]:
        from token_crypto import try_decrypt_secret

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
            plain = try_decrypt_secret(alert.refresh_token)
            if plain is None:
                with self._conn() as conn:
                    cur = self._cursor(conn)
                    cur.execute(
                        "UPDATE whisper_alerts SET refresh_token = '' WHERE owner_id = %s",
                        (alert.owner_id,),
                    )
                alert.refresh_token = ""
            else:
                alert.refresh_token = plain
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

    def get_follow_monitor(self, owner_id: int) -> FollowMonitor | None:
        from token_crypto import try_decrypt_secret

        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT * FROM follow_monitor WHERE owner_id = %s",
                (owner_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        mon = _row_to_follow_monitor(row)
        plain = try_decrypt_secret(mon.refresh_token)
        if plain is None:
            self.set_follow_monitor_needs_reauth(owner_id, True)
            with self._conn() as conn:
                cur = self._cursor(conn)
                cur.execute(
                    "UPDATE follow_monitor SET refresh_token = '' WHERE owner_id = %s",
                    (owner_id,),
                )
            mon.refresh_token = ""
            mon.needs_reauth = True
        else:
            mon.refresh_token = plain
        return mon

    def upsert_follow_monitor(
        self,
        owner_id: int,
        *,
        enabled: bool,
        twitch_user_id: str,
        twitch_login: str,
        refresh_token: str,
        next_sync_at: str | None = None,
        last_sync_at: str | None = None,
        needs_reauth: bool = False,
        baseline_done: bool | None = None,
    ) -> None:
        from token_crypto import encrypt_secret

        enc = encrypt_secret(refresh_token) if refresh_token else ""
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT baseline_done FROM follow_monitor WHERE owner_id = %s",
                (owner_id,),
            )
            existing = cur.fetchone()
            keep_baseline = (
                bool(existing["baseline_done"])
                if existing is not None and baseline_done is None
                else bool(baseline_done) if baseline_done is not None else False
            )
            cur.execute(
                """
                INSERT INTO follow_monitor (
                    owner_id, enabled, twitch_user_id, twitch_login,
                    refresh_token, next_sync_at, last_sync_at,
                    needs_reauth, baseline_done
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s::timestamptz, %s::timestamptz, %s, %s
                )
                ON CONFLICT(owner_id) DO UPDATE SET
                    enabled = EXCLUDED.enabled,
                    twitch_user_id = EXCLUDED.twitch_user_id,
                    twitch_login = EXCLUDED.twitch_login,
                    refresh_token = EXCLUDED.refresh_token,
                    next_sync_at = COALESCE(
                        EXCLUDED.next_sync_at, follow_monitor.next_sync_at
                    ),
                    last_sync_at = COALESCE(
                        EXCLUDED.last_sync_at, follow_monitor.last_sync_at
                    ),
                    needs_reauth = EXCLUDED.needs_reauth,
                    baseline_done = EXCLUDED.baseline_done
                """,
                (
                    owner_id,
                    bool(enabled),
                    twitch_user_id,
                    twitch_login,
                    enc,
                    next_sync_at,
                    last_sync_at,
                    bool(needs_reauth),
                    keep_baseline,
                ),
            )

    def set_follow_monitor_enabled(self, owner_id: int, enabled: bool) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            if enabled:
                now = datetime.now(timezone.utc).isoformat()
                cur.execute(
                    """
                    UPDATE follow_monitor
                    SET enabled = TRUE,
                        next_sync_at = COALESCE(next_sync_at, %s::timestamptz)
                    WHERE owner_id = %s
                    """,
                    (now, owner_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE follow_monitor
                    SET enabled = FALSE,
                        notify_follow = FALSE,
                        notify_unfollow = FALSE
                    WHERE owner_id = %s
                    """,
                    (owner_id,),
                )

    def set_follow_monitor_notify(
        self,
        owner_id: int,
        *,
        notify_follow: bool | None = None,
        notify_unfollow: bool | None = None,
    ) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            if notify_follow is not None:
                cur.execute(
                    "UPDATE follow_monitor SET notify_follow = %s WHERE owner_id = %s",
                    (bool(notify_follow), owner_id),
                )
            if notify_unfollow is not None:
                cur.execute(
                    "UPDATE follow_monitor SET notify_unfollow = %s WHERE owner_id = %s",
                    (bool(notify_unfollow), owner_id),
                )

    def update_follow_monitor_sync(
        self,
        owner_id: int,
        *,
        last_sync_at: str,
        next_sync_at: str,
        refresh_token: str | None = None,
        baseline_done: bool = True,
        needs_reauth: bool = False,
    ) -> None:
        from token_crypto import encrypt_secret

        with self._conn() as conn:
            cur = self._cursor(conn)
            if refresh_token is not None:
                enc = encrypt_secret(refresh_token) if refresh_token else ""
                cur.execute(
                    """
                    UPDATE follow_monitor
                    SET last_sync_at = %s::timestamptz,
                        next_sync_at = %s::timestamptz,
                        refresh_token = %s,
                        baseline_done = %s,
                        needs_reauth = %s
                    WHERE owner_id = %s
                    """,
                    (
                        last_sync_at,
                        next_sync_at,
                        enc,
                        bool(baseline_done),
                        bool(needs_reauth),
                        owner_id,
                    ),
                )
            else:
                cur.execute(
                    """
                    UPDATE follow_monitor
                    SET last_sync_at = %s::timestamptz,
                        next_sync_at = %s::timestamptz,
                        baseline_done = %s,
                        needs_reauth = %s
                    WHERE owner_id = %s
                    """,
                    (
                        last_sync_at,
                        next_sync_at,
                        bool(baseline_done),
                        bool(needs_reauth),
                        owner_id,
                    ),
                )

    def set_follow_monitor_needs_reauth(self, owner_id: int, needs: bool) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "UPDATE follow_monitor SET needs_reauth = %s WHERE owner_id = %s",
                (bool(needs), owner_id),
            )

    def fan_out_twitch_oauth_token(
        self,
        owner_id: int,
        *,
        twitch_user_id: str,
        twitch_login: str,
        refresh_token: str,
    ) -> None:
        from token_crypto import encrypt_secret

        enc = encrypt_secret(refresh_token) if refresh_token else ""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO twitch_sync (
                    owner_id, twitch_user_id, refresh_token,
                    period_days, next_sync_at, last_sync_at, needs_reauth
                ) VALUES (%s, %s, %s, 0, %s::timestamptz, NULL, FALSE)
                ON CONFLICT (owner_id) DO UPDATE SET
                    twitch_user_id = EXCLUDED.twitch_user_id,
                    refresh_token = EXCLUDED.refresh_token,
                    needs_reauth = FALSE
                """,
                (owner_id, twitch_user_id, enc, now),
            )
            cur.execute(
                """
                INSERT INTO follow_monitor (
                    owner_id, enabled, twitch_user_id, twitch_login,
                    refresh_token, needs_reauth
                ) VALUES (%s, FALSE, %s, %s, %s, FALSE)
                ON CONFLICT (owner_id) DO UPDATE SET
                    twitch_user_id = EXCLUDED.twitch_user_id,
                    twitch_login = EXCLUDED.twitch_login,
                    refresh_token = EXCLUDED.refresh_token,
                    needs_reauth = FALSE
                """,
                (owner_id, twitch_user_id, twitch_login, enc),
            )
            cur.execute(
                """
                INSERT INTO whisper_alerts (
                    owner_id, enabled, twitch_user_id, twitch_login,
                    refresh_token, eventsub_id, paused_for_reauth
                ) VALUES (%s, FALSE, %s, %s, %s, '', FALSE)
                ON CONFLICT (owner_id) DO UPDATE SET
                    twitch_user_id = EXCLUDED.twitch_user_id,
                    twitch_login = EXCLUDED.twitch_login,
                    refresh_token = EXCLUDED.refresh_token
                """,
                (owner_id, twitch_user_id, twitch_login, enc),
            )
            cur.execute(
                """
                INSERT INTO chat_auth (
                    owner_id, twitch_user_id, twitch_login, refresh_token
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (owner_id) DO UPDATE SET
                    twitch_user_id = EXCLUDED.twitch_user_id,
                    twitch_login = EXCLUDED.twitch_login,
                    refresh_token = EXCLUDED.refresh_token
                """,
                (owner_id, twitch_user_id, twitch_login, enc),
            )
            cur.execute(
                """
                INSERT INTO users (user_id, premium_twitch_refresh, premium_twitch_needs_reauth)
                VALUES (%s, %s, FALSE)
                ON CONFLICT (user_id) DO UPDATE SET
                    premium_twitch_refresh = EXCLUDED.premium_twitch_refresh,
                    premium_twitch_needs_reauth = FALSE
                """,
                (owner_id, enc),
            )
            cur.execute(
                """
                UPDATE users
                SET premium_twitch_user_id = %s
                WHERE user_id = %s
                  AND COALESCE(premium_twitch_user_id, '') = ''
                """,
                (twitch_user_id, owner_id),
            )
            cur.execute(
                """
                UPDATE users SET twitch_reauth_notified_at = NULL WHERE user_id = %s
                """,
                (owner_id,),
            )

    def revoke_user_twitch_oauth_tokens(self, owner_id: int) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute("DELETE FROM twitch_sync WHERE owner_id = %s", (owner_id,))
            cur.execute(
                "DELETE FROM follow_monitor_events WHERE owner_id = %s", (owner_id,)
            )
            cur.execute(
                "DELETE FROM follow_monitor_followers WHERE owner_id = %s", (owner_id,)
            )
            cur.execute("DELETE FROM follow_monitor WHERE owner_id = %s", (owner_id,))
            cur.execute("DELETE FROM whisper_alerts WHERE owner_id = %s", (owner_id,))
            cur.execute("DELETE FROM chat_auth WHERE owner_id = %s", (owner_id,))
            cur.execute(
                """
                UPDATE drops_auth
                SET refresh_token = '', access_token = '', access_expires_at = 0
                WHERE owner_id = %s
                """,
                (owner_id,),
            )
            cur.execute(
                """
                UPDATE users
                SET premium_twitch_refresh = '',
                    premium_twitch_needs_reauth = FALSE,
                    twitch_reauth_notified_at = NULL
                WHERE user_id = %s
                """,
                (owner_id,),
            )

    def revoke_user_donationalerts_oauth_tokens(self, owner_id: int) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "DELETE FROM donationalerts_auth WHERE owner_id = %s", (owner_id,)
            )

    def revoke_user_oauth_tokens(self, owner_id: int) -> None:
        self.revoke_user_twitch_oauth_tokens(owner_id)
        self.revoke_user_donationalerts_oauth_tokens(owner_id)

    def list_owners_needing_twitch_reauth(self) -> list[int]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT owner_id AS uid FROM twitch_sync
                WHERE COALESCE(needs_reauth, FALSE) = TRUE
                UNION
                SELECT owner_id AS uid FROM follow_monitor
                WHERE COALESCE(needs_reauth, FALSE) = TRUE
                UNION
                SELECT user_id AS uid FROM users
                WHERE COALESCE(premium_twitch_needs_reauth, FALSE) = TRUE
                """
            )
            rows = cur.fetchall()
        return sorted({int(r["uid"]) for r in rows})

    def mark_twitch_stores_needs_reauth(self, owner_id: int) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "UPDATE twitch_sync SET needs_reauth = TRUE WHERE owner_id = %s",
                (owner_id,),
            )
            cur.execute(
                "UPDATE follow_monitor SET needs_reauth = TRUE WHERE owner_id = %s",
                (owner_id,),
            )
            cur.execute(
                """
                INSERT INTO users (user_id, premium_twitch_needs_reauth)
                VALUES (%s, TRUE)
                ON CONFLICT (user_id) DO UPDATE SET premium_twitch_needs_reauth = TRUE
                """,
                (owner_id,),
            )

    def pause_for_twitch_reauth(self, owner_id: int) -> tuple[int, bool]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                UPDATE subscriptions
                SET enabled = FALSE, paused_for_reauth = TRUE
                WHERE owner_id = %s
                  AND from_twitch_sync = TRUE
                  AND enabled = TRUE
                """,
                (owner_id,),
            )
            subs = int(cur.rowcount)
            cur.execute(
                "SELECT enabled FROM whisper_alerts WHERE owner_id = %s",
                (owner_id,),
            )
            row = cur.fetchone()
            whisper_paused = False
            if row and bool(row["enabled"]):
                cur.execute(
                    """
                    UPDATE whisper_alerts
                    SET enabled = FALSE, eventsub_id = '', paused_for_reauth = TRUE
                    WHERE owner_id = %s
                    """,
                    (owner_id,),
                )
                whisper_paused = True
        return subs, whisper_paused

    def list_subscriptions_paused_for_reauth(self, owner_id: int) -> list[Subscription]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT * FROM subscriptions
                WHERE owner_id = %s AND COALESCE(paused_for_reauth, FALSE) = TRUE
                ORDER BY id
                """,
                (owner_id,),
            )
            rows = cur.fetchall()
        return [_row_to_sub(r) for r in rows]

    def clear_subscription_paused_for_reauth(
        self, sub_id: int, owner_id: int, *, enabled: bool
    ) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                UPDATE subscriptions
                SET paused_for_reauth = FALSE, enabled = %s
                WHERE id = %s AND owner_id = %s
                """,
                (bool(enabled), sub_id, owner_id),
            )

    def take_whisper_paused_for_reauth(self, owner_id: int) -> WhisperAlert | None:
        from token_crypto import try_decrypt_secret

        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT * FROM whisper_alerts
                WHERE owner_id = %s AND COALESCE(paused_for_reauth, FALSE) = TRUE
                """,
                (owner_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            cur.execute(
                """
                UPDATE whisper_alerts SET paused_for_reauth = FALSE WHERE owner_id = %s
                """,
                (owner_id,),
            )
        alert = _row_to_whisper_alert(row)
        plain = try_decrypt_secret(alert.refresh_token)
        if plain is None:
            with self._conn() as conn:
                cur = self._cursor(conn)
                cur.execute(
                    "UPDATE whisper_alerts SET refresh_token = '' WHERE owner_id = %s",
                    (owner_id,),
                )
            alert.refresh_token = ""
        else:
            alert.refresh_token = plain
        return alert

    def get_twitch_reauth_notified_at(self, owner_id: int) -> str | None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT twitch_reauth_notified_at FROM users WHERE user_id = %s",
                (owner_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        try:
            val = row["twitch_reauth_notified_at"]
        except (KeyError, IndexError, TypeError):
            return None
        if val is None:
            return None
        return val.isoformat() if hasattr(val, "isoformat") else str(val)

    def set_twitch_reauth_notified_at(
        self, owner_id: int, notified_at: str | None
    ) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            if notified_at is None:
                cur.execute(
                    """
                    INSERT INTO users (user_id, twitch_reauth_notified_at)
                    VALUES (%s, NULL)
                    ON CONFLICT (user_id) DO UPDATE SET
                        twitch_reauth_notified_at = NULL
                    """,
                    (owner_id,),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO users (user_id, twitch_reauth_notified_at)
                    VALUES (%s, %s::timestamptz)
                    ON CONFLICT (user_id) DO UPDATE SET
                        twitch_reauth_notified_at = EXCLUDED.twitch_reauth_notified_at
                    """,
                    (owner_id, notified_at),
                )

    def get_due_follow_monitors(self, now_iso: str) -> list[FollowMonitor]:
        from token_crypto import try_decrypt_secret

        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT * FROM follow_monitor
                WHERE enabled = TRUE
                  AND COALESCE(needs_reauth, FALSE) = FALSE
                  AND next_sync_at IS NOT NULL
                  AND next_sync_at <= %s::timestamptz
                ORDER BY next_sync_at
                """,
                (now_iso,),
            )
            rows = cur.fetchall()
        out: list[FollowMonitor] = []
        for row in rows:
            mon = _row_to_follow_monitor(row)
            plain = try_decrypt_secret(mon.refresh_token)
            if plain is None:
                self.set_follow_monitor_needs_reauth(mon.owner_id, True)
                with self._conn() as conn:
                    cur = self._cursor(conn)
                    cur.execute(
                        "UPDATE follow_monitor SET refresh_token = '' WHERE owner_id = %s",
                        (mon.owner_id,),
                    )
                mon.refresh_token = ""
                mon.needs_reauth = True
            else:
                mon.refresh_token = plain
            out.append(mon)
        return out

    def has_any_enabled_follow_monitor(self) -> bool:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT 1 FROM follow_monitor
                WHERE enabled = TRUE AND COALESCE(needs_reauth, FALSE) = FALSE
                LIMIT 1
                """
            )
            return cur.fetchone() is not None

    def list_follow_monitor_followers(
        self, owner_id: int, *, limit: int = 5000, offset: int = 0
    ) -> list[FollowMonitorFollower]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT * FROM follow_monitor_followers
                WHERE owner_id = %s
                ORDER BY LOWER(login)
                LIMIT %s OFFSET %s
                """,
                (owner_id, max(1, limit), max(0, offset)),
            )
            rows = cur.fetchall()
        return [_row_to_follow_monitor_follower(r) for r in rows]

    def count_follow_monitor_followers(self, owner_id: int) -> int:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT COUNT(*) AS c FROM follow_monitor_followers WHERE owner_id = %s",
                (owner_id,),
            )
            row = cur.fetchone()
        return int(row["c"] if row else 0)

    def replace_follow_monitor_followers(
        self,
        owner_id: int,
        followers: list[tuple[str, str, str, str]],
    ) -> None:
        with self._bulk_conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "DELETE FROM follow_monitor_followers WHERE owner_id = %s",
                (owner_id,),
            )
            if followers:
                cur.executemany(
                    """
                    INSERT INTO follow_monitor_followers (
                        owner_id, twitch_user_id, login, display_name, followed_at
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    [
                        (owner_id, tid, login, display, followed)
                        for tid, login, display, followed in followers
                    ],
                )

    def list_follow_monitor_events(
        self,
        owner_id: int,
        *,
        event_type: str | None = None,
        since: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[FollowMonitorEvent]:
        clauses = ["owner_id = %s"]
        params: list[Any] = [owner_id]
        if event_type:
            clauses.append("event_type = %s")
            params.append(event_type)
        if since:
            clauses.append("detected_at >= %s::timestamptz")
            params.append(since)
        where = " AND ".join(clauses)
        params.extend([max(1, limit), max(0, offset)])
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                f"""
                SELECT * FROM follow_monitor_events
                WHERE {where}
                ORDER BY detected_at DESC, id DESC
                LIMIT %s OFFSET %s
                """,
                params,
            )
            rows = cur.fetchall()
        return [_row_to_follow_monitor_event(r) for r in rows]

    def count_follow_monitor_events(
        self,
        owner_id: int,
        *,
        event_type: str | None = None,
        since: str | None = None,
    ) -> int:
        clauses = ["owner_id = %s"]
        params: list[Any] = [owner_id]
        if event_type:
            clauses.append("event_type = %s")
            params.append(event_type)
        if since:
            clauses.append("detected_at >= %s::timestamptz")
            params.append(since)
        where = " AND ".join(clauses)
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                f"""
                SELECT COUNT(*) AS c FROM follow_monitor_events
                WHERE {where}
                """,
                params,
            )
            row = cur.fetchone()
        return int(row["c"] if row else 0)

    def add_follow_monitor_events(
        self,
        owner_id: int,
        events: list[tuple[str, str, str, str]],
        *,
        detected_at: str,
    ) -> None:
        if not events:
            return
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.executemany(
                """
                INSERT INTO follow_monitor_events (
                    owner_id, event_type, twitch_user_id, login,
                    display_name, detected_at
                ) VALUES (%s, %s, %s, %s, %s, %s::timestamptz)
                """,
                [
                    (owner_id, etype, tid, login, display, detected_at)
                    for etype, tid, login, display in events
                ],
            )

    def search_follow_monitor(
        self, owner_id: int, query: str, *, limit: int = 50
    ) -> list[dict[str, str]]:
        q = (query or "").strip().lower()
        if not q:
            return []
        like = f"%{q}%"
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT source, twitch_user_id, login, display_name, at FROM (
                    SELECT 'current'::text AS source, twitch_user_id, login,
                           display_name, followed_at AS at
                    FROM follow_monitor_followers
                    WHERE owner_id = %s
                      AND (LOWER(login) LIKE %s OR LOWER(display_name) LIKE %s)
                    UNION ALL
                    SELECT event_type AS source, twitch_user_id, login,
                           display_name, detected_at::text AS at
                    FROM follow_monitor_events
                    WHERE owner_id = %s
                      AND (LOWER(login) LIKE %s OR LOWER(display_name) LIKE %s)
                ) q
                ORDER BY at DESC
                LIMIT %s
                """,
                (owner_id, like, like, owner_id, like, like, max(1, limit)),
            )
            rows = cur.fetchall()
        return [
            {
                "source": str(r["source"] or ""),
                "twitch_user_id": str(r["twitch_user_id"] or ""),
                "login": str(r["login"] or ""),
                "display_name": str(r["display_name"] or ""),
                "at": str(r["at"] or ""),
            }
            for r in rows
        ]

    def get_chat_auth(self, owner_id: int) -> ChatAuth | None:
        from token_crypto import try_decrypt_secret

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
        plain = try_decrypt_secret(auth.refresh_token)
        if plain is None:
            with self._conn() as conn:
                cur = self._cursor(conn)
                cur.execute(
                    "UPDATE chat_auth SET refresh_token = '' WHERE owner_id = %s",
                    (owner_id,),
                )
            auth.refresh_token = ""
        else:
            auth.refresh_token = plain
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

    def get_drops_auth(self, owner_id: int) -> DropsAuth | None:
        from token_crypto import try_decrypt_secret

        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT * FROM drops_auth WHERE owner_id = %s",
                (owner_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        auth = DropsAuth(
            owner_id=int(row["owner_id"]),
            twitch_user_id=str(row["twitch_user_id"] or ""),
            twitch_login=str(row["twitch_login"] or ""),
            refresh_token=str(row["refresh_token"] or ""),
            digest_enabled=bool(row["digest_enabled"])
            if "digest_enabled" in row.keys()
            else False,
            access_token=str(row["access_token"] or "")
            if "access_token" in row.keys()
            else "",
            access_expires_at=int(row["access_expires_at"] or 0)
            if "access_expires_at" in row.keys()
            else 0,
        )
        plain = try_decrypt_secret(auth.refresh_token)
        access_plain = (
            try_decrypt_secret(auth.access_token) if auth.access_token else ""
        )
        if plain is None or (auth.access_token and access_plain is None):
            with self._conn() as conn:
                cur = self._cursor(conn)
                cur.execute(
                    """
                    UPDATE drops_auth
                    SET refresh_token = '', access_token = '', access_expires_at = 0
                    WHERE owner_id = %s
                    """,
                    (owner_id,),
                )
            auth.refresh_token = ""
            auth.access_token = ""
            auth.access_expires_at = 0
        else:
            auth.refresh_token = plain
            auth.access_token = access_plain or ""
        return auth

    def upsert_drops_auth(
        self,
        owner_id: int,
        *,
        twitch_user_id: str,
        twitch_login: str,
        refresh_token: str,
        access_token: str = "",
        access_expires_at: int = 0,
    ) -> None:
        from token_crypto import encrypt_secret

        enc = encrypt_secret(refresh_token) if refresh_token else ""
        enc_at = encrypt_secret(access_token) if access_token else ""
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO drops_auth (
                    owner_id, twitch_user_id, twitch_login, refresh_token,
                    access_token, access_expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT(owner_id) DO UPDATE SET
                    twitch_user_id = EXCLUDED.twitch_user_id,
                    twitch_login = EXCLUDED.twitch_login,
                    refresh_token = EXCLUDED.refresh_token,
                    access_token = EXCLUDED.access_token,
                    access_expires_at = EXCLUDED.access_expires_at
                """,
                (
                    owner_id,
                    twitch_user_id,
                    twitch_login,
                    enc,
                    enc_at,
                    int(access_expires_at or 0),
                ),
            )

    def set_drops_digest_enabled(self, owner_id: int, enabled: bool) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "UPDATE drops_auth SET digest_enabled = %s WHERE owner_id = %s",
                (bool(enabled), owner_id),
            )

    def list_drops_digest_owner_ids(self) -> list[int]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT owner_id FROM drops_auth
                WHERE digest_enabled = TRUE
                """
            )
            rows = cur.fetchall()
        return [int(r["owner_id"]) for r in rows]

    def has_any_drops_work(self) -> bool:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT 1 WHERE EXISTS (
                    SELECT 1 FROM drops_auth WHERE digest_enabled = TRUE
                ) OR EXISTS (
                    SELECT 1 FROM subscriptions
                    WHERE enabled = TRUE
                      AND COALESCE(notify_on_drops, FALSE) = TRUE
                      AND COALESCE(drops_game_id, '') != ''
                )
                """
            )
            return cur.fetchone() is not None

    def update_drops_auth_refresh(self, owner_id: int, refresh_token: str) -> None:
        from token_crypto import encrypt_secret

        enc = encrypt_secret(refresh_token) if refresh_token else ""
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "UPDATE drops_auth SET refresh_token = %s WHERE owner_id = %s",
                (enc, owner_id),
            )

    def update_drops_auth_access(
        self,
        owner_id: int,
        *,
        access_token: str,
        access_expires_at: int,
        refresh_token: str | None = None,
    ) -> None:
        from token_crypto import encrypt_secret

        enc_at = encrypt_secret(access_token) if access_token else ""
        with self._conn() as conn:
            cur = self._cursor(conn)
            if refresh_token is not None:
                enc_rt = encrypt_secret(refresh_token) if refresh_token else ""
                cur.execute(
                    """
                    UPDATE drops_auth
                    SET access_token = %s, access_expires_at = %s, refresh_token = %s
                    WHERE owner_id = %s
                    """,
                    (enc_at, int(access_expires_at or 0), enc_rt, owner_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE drops_auth
                    SET access_token = %s, access_expires_at = %s
                    WHERE owner_id = %s
                    """,
                    (enc_at, int(access_expires_at or 0), owner_id),
                )

    def delete_drops_auth(self, owner_id: int) -> None:
        # Keep the row so digest_enabled survives re-link / token wipe.
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                UPDATE drops_auth
                SET refresh_token = '', access_token = '', access_expires_at = 0
                WHERE owner_id = %s
                """,
                (owner_id,),
            )

    @staticmethod
    def _row_to_giveaways_prefs(row: Any) -> GiveawaysPrefs:
        stores: list[str] = []
        platforms: list[str] = []
        try:
            raw_s = json.loads(str(row["stores_json"] or "[]"))
            if isinstance(raw_s, list):
                stores = [str(x) for x in raw_s if str(x)]
        except Exception:
            stores = []
        try:
            raw_p = json.loads(str(row["platforms_json"] or "[]"))
            if isinstance(raw_p, list):
                platforms = [str(x) for x in raw_p if str(x)]
        except Exception:
            platforms = []
        return GiveawaysPrefs(
            owner_id=int(row["owner_id"]),
            stores=stores,
            platforms=platforms,
            digest_enabled=bool(row["digest_enabled"]),
            first_digest_sent=bool(row["first_digest_sent"]),
            last_digest_at=int(row["last_digest_at"] or 0),
        )

    def get_giveaways_prefs(self, owner_id: int) -> GiveawaysPrefs | None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT * FROM giveaways_prefs WHERE owner_id = %s",
                (owner_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return self._row_to_giveaways_prefs(row)

    def upsert_giveaways_prefs(
        self,
        owner_id: int,
        *,
        stores: list[str],
        platforms: list[str],
        digest_enabled: bool | None = None,
        first_digest_sent: bool | None = None,
        last_digest_at: int | None = None,
    ) -> GiveawaysPrefs:
        existing = self.get_giveaways_prefs(owner_id)
        en = (
            bool(digest_enabled)
            if digest_enabled is not None
            else (existing.digest_enabled if existing else False)
        )
        first = (
            bool(first_digest_sent)
            if first_digest_sent is not None
            else (existing.first_digest_sent if existing else False)
        )
        last = (
            int(last_digest_at)
            if last_digest_at is not None
            else (existing.last_digest_at if existing else 0)
        )
        stores_json = json.dumps(list(stores), ensure_ascii=False)
        platforms_json = json.dumps(list(platforms), ensure_ascii=False)
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO giveaways_prefs (
                    owner_id, stores_json, platforms_json,
                    digest_enabled, first_digest_sent, last_digest_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (owner_id) DO UPDATE SET
                    stores_json = EXCLUDED.stores_json,
                    platforms_json = EXCLUDED.platforms_json,
                    digest_enabled = EXCLUDED.digest_enabled,
                    first_digest_sent = EXCLUDED.first_digest_sent,
                    last_digest_at = EXCLUDED.last_digest_at
                """,
                (owner_id, stores_json, platforms_json, en, first, last),
            )
        return GiveawaysPrefs(
            owner_id=owner_id,
            stores=list(stores),
            platforms=list(platforms),
            digest_enabled=en,
            first_digest_sent=first,
            last_digest_at=last,
        )

    def set_giveaways_digest_enabled(self, owner_id: int, enabled: bool) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO giveaways_prefs (owner_id, digest_enabled)
                VALUES (%s, %s)
                ON CONFLICT (owner_id) DO UPDATE SET digest_enabled = EXCLUDED.digest_enabled
                """,
                (owner_id, bool(enabled)),
            )

    def list_giveaways_digest_owner_ids(self) -> list[int]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT owner_id FROM giveaways_prefs
                WHERE digest_enabled = TRUE
                  AND stores_json != '[]'
                  AND platforms_json != '[]'
                """
            )
            rows = cur.fetchall()
        return [int(r["owner_id"]) for r in rows]

    def has_any_giveaways_work(self) -> bool:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT 1 FROM giveaways_prefs
                WHERE stores_json != '[]'
                  AND platforms_json != '[]'
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if row is not None:
                return True
            cur.execute(
                """
                SELECT 1 FROM subscriptions
                WHERE enabled = TRUE
                  AND COALESCE(giveaway_watch_prefs, '') != ''
                LIMIT 1
                """
            )
            row = cur.fetchone()
        return row is not None

    def has_seen_giveaway(
        self, owner_id: int, source: str, external_id: str
    ) -> bool:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT 1 FROM giveaway_seen
                WHERE owner_id = %s AND source = %s AND external_id = %s
                """,
                (owner_id, source, external_id),
            )
            row = cur.fetchone()
        return row is not None

    def mark_giveaway_seen(
        self,
        owner_id: int,
        source: str,
        external_id: str,
        *,
        seen_at: int,
    ) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO giveaway_seen (
                    owner_id, source, external_id, seen_at
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (owner_id, source, external_id) DO NOTHING
                """,
                (owner_id, source, external_id, int(seen_at)),
            )

    @staticmethod
    def _row_to_giveaways_catalog(row: Any) -> GiveawayCatalogEntry:
        plats_raw = json.loads(str(row["platforms_json"] or "[]"))
        plats = tuple(
            str(p) for p in (plats_raw if isinstance(plats_raw, list) else [])
        )
        igdb_raw = row["igdb_id"]
        igdb_id = int(igdb_raw) if igdb_raw is not None else None
        if igdb_id is not None and igdb_id <= 0:
            igdb_id = None
        return GiveawayCatalogEntry(
            source=str(row["source"] or ""),
            external_id=str(row["external_id"] or ""),
            title=str(row["title"] or ""),
            store_id=str(row["store_id"] or ""),
            platform_ids=plats,
            claim_url=str(row["claim_url"] or ""),
            start_at=str(row["start_at"] or ""),
            end_at=str(row["end_at"] or ""),
            description=str(row["description"] or ""),
            image_url=str(row["image_url"] or ""),
            dedupe_key=str(row["dedupe_key"] or ""),
            igdb_id=igdb_id,
            name=str(row["name"] or ""),
            year=str(row["year"] or ""),
            publisher=str(row["publisher"] or ""),
            developer=str(row["developer"] or ""),
            summary=str(row["summary"] or ""),
            cover_url=str(row["cover_url"] or ""),
            refreshed_at=int(row["refreshed_at"] or 0),
        )

    def replace_giveaways_catalog(
        self, entries: list[GiveawayCatalogEntry]
    ) -> None:
        with self._bulk_conn() as conn:
            cur = self._cursor(conn)
            cur.execute("DELETE FROM giveaways_catalog")
            for e in entries:
                cur.execute(
                    """
                    INSERT INTO giveaways_catalog (
                        source, external_id, title, store_id, platforms_json,
                        claim_url, start_at, end_at, description, image_url,
                        dedupe_key, igdb_id, name, year, publisher, developer,
                        summary, cover_url, refreshed_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        e.source,
                        e.external_id,
                        e.title,
                        e.store_id,
                        json.dumps(list(e.platform_ids), ensure_ascii=False),
                        e.claim_url,
                        e.start_at,
                        e.end_at,
                        e.description,
                        e.image_url,
                        e.dedupe_key,
                        e.igdb_id,
                        e.name,
                        e.year,
                        e.publisher,
                        e.developer,
                        e.summary,
                        e.cover_url,
                        int(e.refreshed_at),
                    ),
                )

    def list_giveaways_catalog(self) -> list[GiveawayCatalogEntry]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT * FROM giveaways_catalog
                ORDER BY start_at DESC, title ASC
                """
            )
            rows = cur.fetchall()
        return [self._row_to_giveaways_catalog(r) for r in rows]

    def giveaways_catalog_refreshed_at(self) -> int:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute("SELECT MAX(refreshed_at) AS ts FROM giveaways_catalog")
            row = cur.fetchone()
        if not row or row["ts"] is None:
            return 0
        return int(row["ts"] or 0)

    def get_donationalerts_auth(self, owner_id: int) -> DonationAlertsAuth | None:
        from token_crypto import try_decrypt_secret

        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT * FROM donationalerts_auth WHERE owner_id = %s",
                (owner_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        auth = DonationAlertsAuth(
            owner_id=int(row["owner_id"]),
            da_user_id=str(row["da_user_id"] or ""),
            da_code=str(row["da_code"] or ""),
            refresh_token=str(row["refresh_token"] or ""),
            access_token=str(row["access_token"] or ""),
            access_expires_at=int(row["access_expires_at"] or 0),
        )
        plain = try_decrypt_secret(auth.refresh_token)
        access_plain = (
            try_decrypt_secret(auth.access_token) if auth.access_token else ""
        )
        if plain is None or (auth.access_token and access_plain is None):
            with self._conn() as conn:
                cur = self._cursor(conn)
                cur.execute(
                    """
                    UPDATE donationalerts_auth
                    SET refresh_token = '', access_token = '', access_expires_at = 0
                    WHERE owner_id = %s
                    """,
                    (owner_id,),
                )
            auth.refresh_token = ""
            auth.access_token = ""
            auth.access_expires_at = 0
        else:
            auth.refresh_token = plain
            auth.access_token = access_plain or ""
        return auth

    def upsert_donationalerts_auth(
        self,
        owner_id: int,
        *,
        da_user_id: str,
        da_code: str,
        refresh_token: str,
        access_token: str = "",
        access_expires_at: int = 0,
    ) -> None:
        from token_crypto import encrypt_secret

        enc = encrypt_secret(refresh_token) if refresh_token else ""
        enc_at = encrypt_secret(access_token) if access_token else ""
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO donationalerts_auth (
                    owner_id, da_user_id, da_code, refresh_token,
                    access_token, access_expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT(owner_id) DO UPDATE SET
                    da_user_id = EXCLUDED.da_user_id,
                    da_code = EXCLUDED.da_code,
                    refresh_token = EXCLUDED.refresh_token,
                    access_token = EXCLUDED.access_token,
                    access_expires_at = EXCLUDED.access_expires_at
                """,
                (
                    owner_id,
                    da_user_id,
                    da_code,
                    enc,
                    enc_at,
                    int(access_expires_at or 0),
                ),
            )

    def update_donationalerts_auth_tokens(
        self,
        owner_id: int,
        *,
        refresh_token: str | None = None,
        access_token: str | None = None,
        access_expires_at: int | None = None,
    ) -> None:
        from token_crypto import encrypt_secret

        updates: list[str] = []
        values: list[object] = []
        if refresh_token is not None:
            updates.append("refresh_token = %s")
            values.append(encrypt_secret(refresh_token) if refresh_token else "")
        if access_token is not None:
            updates.append("access_token = %s")
            values.append(encrypt_secret(access_token) if access_token else "")
        if access_expires_at is not None:
            updates.append("access_expires_at = %s")
            values.append(int(access_expires_at or 0))
        if not updates:
            return
        values.append(owner_id)
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                f"UPDATE donationalerts_auth SET {', '.join(updates)} "
                "WHERE owner_id = %s",
                values,
            )

    def delete_donationalerts_auth(self, owner_id: int) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "DELETE FROM donationalerts_auth WHERE owner_id = %s",
                (owner_id,),
            )

    def has_seen_drop_campaign(
        self, owner_id: int, campaign_id: str, subscription_id: int
    ) -> bool:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT 1 FROM drop_campaign_seen
                WHERE owner_id = %s AND campaign_id = %s AND subscription_id = %s
                """,
                (owner_id, campaign_id, subscription_id),
            )
            return cur.fetchone() is not None

    def mark_drop_campaign_seen(
        self,
        owner_id: int,
        campaign_id: str,
        subscription_id: int,
        *,
        first_seen_at: str,
    ) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO drop_campaign_seen (
                    owner_id, campaign_id, subscription_id, first_seen_at
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (owner_id, campaign_id, subscription_id, first_seen_at),
            )

    def has_seen_drop_claim(self, owner_id: int, drop_id: str) -> bool:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT 1 FROM drop_claim_seen
                WHERE owner_id = %s AND drop_id = %s
                """,
                (owner_id, drop_id),
            )
            return cur.fetchone() is not None

    def mark_drop_claim_seen(
        self, owner_id: int, drop_id: str, *, first_seen_at: str
    ) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO drop_claim_seen (
                    owner_id, drop_id, first_seen_at
                ) VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (owner_id, drop_id, first_seen_at),
            )

    def has_seen_drop_stream(
        self, owner_id: int, subscription_id: int, stream_id: str
    ) -> bool:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT 1 FROM drop_stream_seen
                WHERE owner_id = %s AND subscription_id = %s AND stream_id = %s
                """,
                (owner_id, subscription_id, stream_id),
            )
            return cur.fetchone() is not None

    def mark_drop_stream_seen(
        self,
        owner_id: int,
        subscription_id: int,
        stream_id: str,
        *,
        first_seen_at: str,
    ) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO drop_stream_seen (
                    owner_id, subscription_id, stream_id, first_seen_at
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (owner_id, subscription_id, stream_id, first_seen_at),
            )

    _DROP_STREAM_ALERT_ID = "__alert__"

    def get_drop_stream_alert_at(
        self, owner_id: int, subscription_id: int
    ) -> str | None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT first_seen_at FROM drop_stream_seen
                WHERE owner_id = %s AND subscription_id = %s AND stream_id = %s
                """,
                (owner_id, subscription_id, self._DROP_STREAM_ALERT_ID),
            )
            row = cur.fetchone()
        return str(row["first_seen_at"]) if row else None

    def mark_drop_stream_alert(
        self, owner_id: int, subscription_id: int, *, at: str
    ) -> None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO drop_stream_seen (
                    owner_id, subscription_id, stream_id, first_seen_at
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (owner_id, subscription_id, stream_id)
                DO UPDATE SET first_seen_at = EXCLUDED.first_seen_at
                """,
                (owner_id, subscription_id, self._DROP_STREAM_ALERT_ID, at),
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
        """Real streamers not in follows that still have manual stream alerts.

        Excludes synthetic rows (game/category watch, Drops, release alerts).
        """
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT twitch_user_id, twitch_username FROM subscriptions
                WHERE owner_id = %s AND is_demo = %s
                  AND COALESCE(category_watch_prefs, '') = ''
                  AND COALESCE(release_watch_prefs, '') = ''
                  AND COALESCE(giveaway_watch_prefs, '') = ''
                  AND twitch_user_id NOT LIKE 'cw:%%'
                  AND twitch_user_id NOT LIKE 'drops:%%'
                  AND twitch_user_id NOT LIKE 'rel:%%'
                  AND twitch_user_id NOT LIKE 'gvw:%%'
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

    def user_has_beta_enrollment(self, user_id: int) -> bool:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT 1 FROM user_beta_enrollments
                WHERE user_id = %s AND enrolled = TRUE
                LIMIT 1
                """,
                (user_id,),
            )
            row = cur.fetchone()
        return row is not None

    def ensure_beta_announce_baseline(self, feature_ids: list[str]) -> None:
        unique = list(dict.fromkeys(str(fid) for fid in feature_ids if str(fid)))
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT 1 FROM schema_flags WHERE name = %s",
                ("beta_announce_baseline_v1",),
            )
            if cur.fetchone():
                return
            for fid in unique:
                cur.execute(
                    """
                    INSERT INTO beta_feature_announcements (feature_id, announced_at)
                    VALUES (%s, NOW())
                    ON CONFLICT (feature_id) DO NOTHING
                    """,
                    (fid,),
                )
            cur.execute(
                "INSERT INTO schema_flags(name) VALUES ('beta_announce_baseline_v1')"
            )

    def list_announced_beta_feature_ids(self) -> list[str]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute("SELECT feature_id FROM beta_feature_announcements")
            rows = cur.fetchall()
        return [str(r["feature_id"]) for r in rows]

    def mark_beta_feature_announced(self, feature_id: str) -> None:
        fid = str(feature_id or "").strip()
        if not fid:
            return
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO beta_feature_announcements (feature_id, announced_at)
                VALUES (%s, NOW())
                ON CONFLICT (feature_id) DO NOTHING
                """,
                (fid,),
            )

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
                SELECT buyer_id AS user_id
                FROM premium_gifts WHERE charge_id = %s
                """,
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
            cur.execute(
                "SELECT user_id FROM premium_purchases WHERE charge_id = %s",
                (cid,),
            )
            row = cur.fetchone()
            if row:
                return int(row["user_id"])
        return None

    @staticmethod
    def _row_to_premium_gift(row) -> PremiumGift:
        return PremiumGift(
            token=str(row["token"] or ""),
            buyer_id=int(row["buyer_id"] or 0),
            kind=str(row["kind"] or ""),
            charge_id=str(row["charge_id"] or ""),
            stars=int(row["stars"] or 0),
            message=str(row["message"] or ""),
            image_file_id=str(row["image_file_id"] or ""),
            status=str(row["status"] or "pending"),
            recipient_id=int(row["recipient_id"] or 0),
            until_unix=int(row["until_unix"] or 0),
            created_at=str(row["created_at"] or ""),
            redeemed_at=str(row["redeemed_at"] or ""),
        )

    def create_premium_gift(
        self,
        *,
        buyer_id: int,
        kind: str,
        charge_id: str,
        stars: int,
    ) -> PremiumGift:
        cid = str(charge_id or "").strip()
        kind_s = str(kind or "").strip()
        if not cid or kind_s not in ("month", "year", "life"):
            raise ValueError("invalid gift params")
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT * FROM premium_gifts WHERE charge_id = %s",
                (cid,),
            )
            existing = cur.fetchone()
            if existing:
                return self._row_to_premium_gift(existing)
            for _ in range(8):
                token = secrets.token_urlsafe(12)
                try:
                    cur.execute(
                        """
                        INSERT INTO premium_gifts (
                            token, buyer_id, kind, charge_id, stars
                        ) VALUES (%s, %s, %s, %s, %s)
                        """,
                        (token, int(buyer_id), kind_s, cid, int(stars)),
                    )
                    cur.execute(
                        "SELECT * FROM premium_gifts WHERE token = %s",
                        (token,),
                    )
                    row = cur.fetchone()
                    return self._row_to_premium_gift(row)
                except Exception:
                    conn.rollback()
                    cur = self._cursor(conn)
                    continue
            raise RuntimeError("failed to allocate premium gift token")

    def get_premium_gift(self, token: str) -> PremiumGift | None:
        raw = (token or "").strip()
        if not raw:
            return None
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT * FROM premium_gifts WHERE token = %s",
                (raw,),
            )
            row = cur.fetchone()
        return self._row_to_premium_gift(row) if row else None

    def find_premium_gift_by_charge(self, charge_id: str) -> PremiumGift | None:
        cid = str(charge_id or "").strip()
        if not cid:
            return None
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT * FROM premium_gifts WHERE charge_id = %s",
                (cid,),
            )
            row = cur.fetchone()
        return self._row_to_premium_gift(row) if row else None

    def update_premium_gift_customize(
        self,
        token: str,
        *,
        message: str | None = None,
        image_file_id: str | None = None,
    ) -> PremiumGift | None:
        raw = (token or "").strip()
        if not raw:
            return None
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT * FROM premium_gifts WHERE token = %s",
                (raw,),
            )
            row = cur.fetchone()
            if not row or str(row["status"] or "") not in ("pending", "ready"):
                return None
            msg = str(row["message"] or "") if message is None else str(message)
            img = (
                str(row["image_file_id"] or "")
                if image_file_id is None
                else str(image_file_id)
            )
            cur.execute(
                """
                UPDATE premium_gifts
                SET message = %s, image_file_id = %s
                WHERE token = %s
                """,
                (msg, img, raw),
            )
            cur.execute(
                "SELECT * FROM premium_gifts WHERE token = %s",
                (raw,),
            )
            row = cur.fetchone()
        return self._row_to_premium_gift(row) if row else None

    def mark_premium_gift_ready(self, token: str) -> PremiumGift | None:
        raw = (token or "").strip()
        if not raw:
            return None
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                UPDATE premium_gifts SET status = 'ready'
                WHERE token = %s AND status = 'pending'
                """,
                (raw,),
            )
            if (cur.rowcount or 0) <= 0:
                cur.execute(
                    "SELECT * FROM premium_gifts WHERE token = %s",
                    (raw,),
                )
                row = cur.fetchone()
                if row and str(row["status"] or "") == "ready":
                    return self._row_to_premium_gift(row)
                return None
            cur.execute(
                "SELECT * FROM premium_gifts WHERE token = %s",
                (raw,),
            )
            row = cur.fetchone()
        return self._row_to_premium_gift(row) if row else None

    def redeem_premium_gift(
        self, token: str, recipient_id: int, *, until_unix: int
    ) -> PremiumGift | None:
        raw = (token or "").strip()
        rid = int(recipient_id or 0)
        if not raw or rid <= 0:
            return None
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                UPDATE premium_gifts
                SET status = 'redeemed',
                    recipient_id = %s,
                    until_unix = %s,
                    redeemed_at = to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')
                WHERE token = %s AND status = 'ready'
                """,
                (rid, int(until_unix), raw),
            )
            if (cur.rowcount or 0) <= 0:
                return None
            cur.execute(
                "SELECT * FROM premium_gifts WHERE token = %s",
                (raw,),
            )
            row = cur.fetchone()
        return self._row_to_premium_gift(row) if row else None

    def revoke_premium_gift_by_charge(self, charge_id: str) -> PremiumGift | None:
        cid = str(charge_id or "").strip()
        if not cid:
            return None
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT * FROM premium_gifts WHERE charge_id = %s",
                (cid,),
            )
            row = cur.fetchone()
            if not row:
                return None
            cur.execute(
                """
                UPDATE premium_gifts SET status = 'revoked'
                WHERE charge_id = %s AND status <> 'revoked'
                """,
                (cid,),
            )
            cur.execute(
                "SELECT * FROM premium_gifts WHERE charge_id = %s",
                (cid,),
            )
            row = cur.fetchone()
        return self._row_to_premium_gift(row) if row else None

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
        self,
        owner_id: int,
        source_sub_id: int,
        snapshot: dict[str, Any],
        *,
        purpose: str = "share",
    ) -> str:
        purpose = (purpose or "share").strip() or "share"
        payload = json.dumps(snapshot, ensure_ascii=False)
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT token FROM alert_share_tokens
                WHERE source_sub_id = %s AND purpose = %s
                """,
                (int(source_sub_id), purpose),
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
                            token, owner_id, source_sub_id, snapshot_json, purpose
                        ) VALUES (%s, %s, %s, %s, %s)
                        """,
                        (token, int(owner_id), int(source_sub_id), payload, purpose),
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

    def upsert_pending_alert_job(
        self,
        job_name: str,
        *,
        kind: str,
        sub_id: int,
        due_at: str,
        payload: dict | None = None,
    ) -> None:
        name = (job_name or "").strip()
        if not name:
            return
        blob = json.dumps(payload or {}, ensure_ascii=False, default=str)
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO pending_alert_jobs (
                    job_name, kind, sub_id, due_at, payload_json
                ) VALUES (%s, %s, %s, %s::timestamptz, %s)
                ON CONFLICT (job_name) DO UPDATE SET
                    kind = EXCLUDED.kind,
                    sub_id = EXCLUDED.sub_id,
                    due_at = EXCLUDED.due_at,
                    payload_json = EXCLUDED.payload_json
                """,
                (name, kind, int(sub_id), due_at, blob),
            )

    def delete_pending_alert_job(self, job_name: str) -> None:
        name = (job_name or "").strip()
        if not name:
            return
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "DELETE FROM pending_alert_jobs WHERE job_name = %s",
                (name,),
            )

    def has_pending_alert_job(self, job_name: str) -> bool:
        name = (job_name or "").strip()
        if not name:
            return False
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT 1 FROM pending_alert_jobs WHERE job_name = %s LIMIT 1",
                (name,),
            )
            return cur.fetchone() is not None

    def list_pending_alert_jobs(self) -> list:
        from db.models import PendingAlertJob

        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT job_name, kind, sub_id, due_at, payload_json
                FROM pending_alert_jobs
                ORDER BY due_at
                """
            )
            rows = cur.fetchall()
        out: list[PendingAlertJob] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            due = row["due_at"]
            out.append(
                PendingAlertJob(
                    job_name=str(row["job_name"]),
                    kind=str(row["kind"] or ""),
                    sub_id=int(row["sub_id"] or 0),
                    due_at=due.isoformat() if hasattr(due, "isoformat") else str(due),
                    payload=payload,
                )
            )
        return out


    _IGDB_TABLES = frozenset({
        "igdb_games",
        "igdb_companies",
        "igdb_genres",
        "igdb_game_modes",
        "igdb_external_twitch",
        "igdb_external_steam",
        "igdb_involved",
        "igdb_covers",
        "igdb_artworks",
        "igdb_release_dates",
        "igdb_platforms",
    })
    _IGDB_PK: dict[str, tuple[str, ...]] = {
        "igdb_games": ("id",),
        "igdb_companies": ("id",),
        "igdb_genres": ("id",),
        "igdb_game_modes": ("id",),
        "igdb_external_twitch": ("twitch_uid",),
        "igdb_external_steam": ("steam_uid",),
        "igdb_involved": ("game_id", "company_id"),
        "igdb_covers": ("id",),
        "igdb_artworks": ("id",),
        "igdb_release_dates": ("id",),
        "igdb_platforms": ("id",),
    }

    def has_any_igdb_ignore_users(self) -> bool:
        from twitch import IGNORE_IGDB_BETA_ID

        if self.list_beta_enrolled_user_ids([IGNORE_IGDB_BETA_ID]):
            return True
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT 1 AS ok FROM users
                WHERE COALESCE(global_ignore_igdb, '[]') NOT IN ('', '[]')
                LIMIT 1
                """
            )
            return bool(cur.fetchone())

    def has_any_game_cover_subs(self) -> bool:
        from twitch import GAME_COVER_IMAGE_ID

        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT 1 AS ok FROM subscriptions
                WHERE image_file_id = %s
                LIMIT 1
                """,
                (GAME_COVER_IMAGE_ID,),
            )
            return bool(cur.fetchone())

    def igdb_table_count(self, table: str) -> int:
        if table not in self._IGDB_TABLES:
            raise ValueError(table)
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
            row = cur.fetchone()
        return int(row["n"] or 0)

    def igdb_replace_rows(
        self,
        table: str,
        columns: tuple[str, ...],
        row_batches,
    ) -> int:
        """Merge dump batches: upsert by PK, delete rows missing from the dump."""
        if table not in self._IGDB_TABLES:
            raise ValueError(table)
        pk = self._IGDB_PK[table]
        if any(c not in columns for c in pk):
            raise ValueError(f"pk {pk} not in columns for {table}")
        placeholders = ", ".join("%s" for _ in columns)
        cols = ", ".join(columns)
        pk_list = ", ".join(pk)
        non_pk = [c for c in columns if c not in pk]
        if non_pk:
            set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in non_pk)
            distinct = " OR ".join(
                f"{table}.{c} IS DISTINCT FROM EXCLUDED.{c}" for c in non_pk
            )
            upsert_sql = (
                f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) "
                f"ON CONFLICT ({pk_list}) DO UPDATE SET {set_clause} "
                f"WHERE {distinct}"
            )
        else:
            upsert_sql = (
                f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) "
                f"ON CONFLICT ({pk_list}) DO NOTHING"
            )
        if len(pk) == 1:
            pk0 = pk[0]
            # twitch_uid / steam_uid are TEXT PKs; numeric ids are BIGINT.
            pk_type = "TEXT" if pk0 in ("twitch_uid", "steam_uid") else "BIGINT"
            temp_ddl = (
                f"CREATE TEMP TABLE _igdb_dump_ids "
                f"({pk0} {pk_type} PRIMARY KEY) ON COMMIT DROP"
            )
            temp_ins = (
                f"INSERT INTO _igdb_dump_ids ({pk0}) VALUES (%s) "
                f"ON CONFLICT DO NOTHING"
            )
            delete_sql = (
                f"DELETE FROM {table} t WHERE NOT EXISTS "
                f"(SELECT 1 FROM _igdb_dump_ids d WHERE d.{pk0} = t.{pk0})"
            )
            pk_idx = columns.index(pk0)

            def pk_tuples(batch: list[tuple]) -> list[tuple]:
                return [(row[pk_idx],) for row in batch]

        else:
            temp_ddl = (
                "CREATE TEMP TABLE _igdb_dump_ids ("
                "game_id BIGINT NOT NULL, company_id BIGINT NOT NULL, "
                "PRIMARY KEY (game_id, company_id)"
                ") ON COMMIT DROP"
            )
            temp_ins = (
                "INSERT INTO _igdb_dump_ids (game_id, company_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING"
            )
            delete_sql = (
                f"DELETE FROM {table} t WHERE NOT EXISTS ("
                "SELECT 1 FROM _igdb_dump_ids d "
                "WHERE d.game_id = t.game_id AND d.company_id = t.company_id)"
            )
            gi, ci = columns.index("game_id"), columns.index("company_id")

            def pk_tuples(batch: list[tuple]) -> list[tuple]:
                return [(row[gi], row[ci]) for row in batch]

        seen = 0
        with self._bulk_conn() as conn:
            cur = self._cursor(conn)
            cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
            before = int((cur.fetchone() or {}).get("n") or 0)
            cur.execute(temp_ddl)
            for batch in row_batches:
                if not batch:
                    continue
                if table == "igdb_involved":
                    batch = [(g, c, bool(d), bool(p)) for g, c, d, p in batch]
                cur.executemany(upsert_sql, batch)
                cur.executemany(temp_ins, pk_tuples(batch))
                seen += len(batch)
            cur.execute(delete_sql)
            deleted = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
            cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
            after = int((cur.fetchone() or {}).get("n") or 0)
        logger.info(
            "IGDB merge table=%s seen=%s before=%s after=%s deleted~=%s",
            table,
            seen,
            before,
            after,
            deleted,
        )
        return after

    def igdb_set_dump_state(
        self, endpoint: str, dump_updated_at: int, row_count: int
    ) -> None:
        import time as _time

        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO igdb_dump_state
                    (endpoint, dump_updated_at, synced_at, row_count)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (endpoint) DO UPDATE SET
                    dump_updated_at = EXCLUDED.dump_updated_at,
                    synced_at = EXCLUDED.synced_at,
                    row_count = EXCLUDED.row_count
                """,
                (
                    str(endpoint),
                    int(dump_updated_at or 0),
                    int(_time.time()),
                    int(row_count or 0),
                ),
            )

    def igdb_get_dump_state(self, endpoint: str) -> dict[str, int] | None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT dump_updated_at, synced_at, row_count
                FROM igdb_dump_state WHERE endpoint = %s
                """,
                (str(endpoint),),
            )
            row = cur.fetchone()
        if not row:
            return None
        return {
            "dump_updated_at": int(row["dump_updated_at"] or 0),
            "synced_at": int(row["synced_at"] or 0),
            "row_count": int(row["row_count"] or 0),
        }

    def get_stream_poll_snapshot(self) -> dict[str, Any] | None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute("SELECT payload FROM stream_poll_snapshot WHERE id = 1")
            row = cur.fetchone()
        if not row:
            return None
        raw = row["payload"]
        if not raw:
            return None
        if isinstance(raw, dict):
            return raw
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def set_stream_poll_snapshot(self, payload: dict[str, Any]) -> None:
        blob = json.dumps(payload if isinstance(payload, dict) else {})
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO stream_poll_snapshot (id, payload, updated_at)
                VALUES (1, %s, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    payload = EXCLUDED.payload,
                    updated_at = NOW()
                """,
                (blob,),
            )

    def get_schedule_day_snapshot(
        self, twitch_user_id: str
    ) -> dict[str, list[dict[str, str]]] | None:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT days_json FROM schedule_day_snapshot WHERE twitch_user_id = %s",
                (str(twitch_user_id),),
            )
            row = cur.fetchone()
        if not row:
            return None
        try:
            data = json.loads(row["days_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        out: dict[str, list[dict[str, str]]] = {}
        for day, segs in data.items():
            if not isinstance(segs, list):
                continue
            cleaned: list[dict[str, str]] = []
            for seg in segs:
                if isinstance(seg, dict) and seg.get("id"):
                    cleaned.append(
                        {
                            "id": str(seg.get("id") or ""),
                            "start": str(seg.get("start") or ""),
                            "title": str(seg.get("title") or ""),
                            "game": str(seg.get("game") or ""),
                        }
                    )
            out[str(day)] = cleaned
        return out

    def set_schedule_day_snapshot(
        self, twitch_user_id: str, days: dict[str, list[dict[str, str]]]
    ) -> None:
        blob = json.dumps(days if isinstance(days, dict) else {})
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO schedule_day_snapshot (twitch_user_id, days_json, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (twitch_user_id) DO UPDATE SET
                    days_json = EXCLUDED.days_json,
                    updated_at = NOW()
                """,
                (str(twitch_user_id), blob),
            )

    def mark_schedule_cancel_notified(self, sub_id: int, day: str) -> None:
        from datetime import date, timedelta

        from schedule_cancel import prune_notified_days

        sub = self.get_subscription_by_id(sub_id)
        if not sub:
            return
        try:
            current = json.loads(sub.schedule_cancel_notified_days or "[]")
        except (TypeError, json.JSONDecodeError):
            current = []
        if not isinstance(current, list):
            current = []
        days = [str(x) for x in current if str(x or "").strip()]
        key = str(day or "").strip()
        if key and key not in days:
            days.append(key)
        keep_after = date.today() - timedelta(days=14)
        days = prune_notified_days(days, keep_after=keep_after)
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "UPDATE subscriptions SET schedule_cancel_notified_days = %s WHERE id = %s",
                (json.dumps(days), sub_id),
            )

    def igdb_search_by_name(
        self, table: str, query: str, *, limit: int = 5
    ) -> list[dict[str, Any]]:
        from search_normalize import search_tokens

        allowed = {
            "igdb_companies",
            "igdb_genres",
            "igdb_game_modes",
        }
        if table not in allowed:
            raise ValueError(table)
        tokens = search_tokens(query)
        if not tokens:
            return []
        lim = max(1, min(20, int(limit)))
        where = " AND ".join(["name ILIKE %s"] * len(tokens))
        params: list[Any] = [f"%{t}%" for t in tokens]
        params.append(lim)
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                f"""
                SELECT id, name FROM {table}
                WHERE {where}
                ORDER BY LENGTH(name) ASC, name ASC
                LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall()
        return [{"id": int(r["id"]), "name": str(r["name"])} for r in rows]

    def igdb_game_meta_for_twitch(self, twitch_uid: str) -> dict[str, Any] | None:
        uid = str(twitch_uid or "").strip()
        if not uid:
            return None
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT game_id FROM igdb_external_twitch WHERE twitch_uid = %s",
                (uid,),
            )
            ext = cur.fetchone()
            if not ext:
                return None
            game_id = int(ext["game_id"])
            cur.execute(
                "SELECT genres, game_modes FROM igdb_games WHERE id = %s",
                (game_id,),
            )
            game = cur.fetchone()
            if not game:
                return None
            cur.execute(
                """
                SELECT company_id, is_developer, is_publisher
                FROM igdb_involved WHERE game_id = %s
                """,
                (game_id,),
            )
            inv = cur.fetchall()

        def _ids(raw: str) -> list[int]:
            return [int(x) for x in str(raw or "").split(",") if x.isdigit()]

        developers: list[int] = []
        publishers: list[int] = []
        for row in inv:
            cid = int(row["company_id"])
            if row["is_developer"]:
                developers.append(cid)
            if row["is_publisher"]:
                publishers.append(cid)
        return {
            "genres": _ids(game["genres"]),
            "game_modes": _ids(game["game_modes"]),
            "developers": developers,
            "publishers": publishers,
        }

    def _igdb_twitch_game_rows(
        self, sql: str, params: tuple, n: int
    ) -> list[dict[str, Any]]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(sql, params)
            rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            name = str(r["name"] or "").strip()
            uid = str(r["twitch_uid"] or "").strip()
            if not name or not uid:
                continue
            out.append(
                {
                    "id": int(r["id"]),
                    "name": name,
                    "twitch_uid": uid,
                    "external_games": [
                        {
                            "uid": uid,
                            "external_game_source": 14,
                            "category": 14,
                        }
                    ],
                }
            )
            if len(out) >= n:
                break
        return out

    def igdb_random_twitch_games(self, n: int = 5) -> list[dict[str, Any]]:
        want = max(1, min(20, int(n)))
        return self._igdb_twitch_game_rows(
            """
            SELECT g.id, g.name, e.twitch_uid
            FROM igdb_games g
            JOIN igdb_external_twitch e ON e.game_id = g.id
            WHERE g.version_parent IS NULL AND BTRIM(g.name) != ''
            ORDER BY RANDOM()
            LIMIT %s
            """,
            (want,),
            want,
        )

    def igdb_recent_twitch_games(
        self, n: int = 5, *, window: int = 50
    ) -> list[dict[str, Any]]:
        import random as _random

        want = max(1, min(20, int(n)))
        pool_n = max(want, min(100, int(window)))
        rows = self._igdb_twitch_game_rows(
            """
            SELECT g.id, g.name, e.twitch_uid
            FROM igdb_games g
            JOIN igdb_external_twitch e ON e.game_id = g.id
            WHERE g.version_parent IS NULL
              AND BTRIM(g.name) != ''
              AND g.first_release_date IS NOT NULL
            ORDER BY g.first_release_date DESC
            LIMIT %s
            """,
            (pool_n,),
            pool_n,
        )
        if len(rows) <= want:
            return rows
        return _random.sample(rows, want)

    def igdb_top_twitch_games(self, n: int = 5) -> list[dict[str, Any]]:
        import random as _random

        want = max(1, min(20, int(n)))
        rows = self._igdb_twitch_game_rows(
            """
            SELECT g.id, g.name, e.twitch_uid
            FROM igdb_games g
            JOIN igdb_external_twitch e ON e.game_id = g.id
            WHERE g.version_parent IS NULL AND BTRIM(g.name) != ''
            ORDER BY g.total_rating_count DESC
            LIMIT 100
            """,
            (),
            100,
        )
        if len(rows) <= want:
            return rows
        return _random.sample(rows, want)

    def igdb_cover_image_id_for_twitch(self, twitch_uid: str) -> str | None:
        uid = str(twitch_uid or "").strip()
        if not uid:
            return None
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT game_id FROM igdb_external_twitch WHERE twitch_uid = %s",
                (uid,),
            )
            ext = cur.fetchone()
            if not ext:
                return None
            game_id = int(ext["game_id"])
            cur.execute(
                "SELECT cover_id FROM igdb_games WHERE id = %s",
                (game_id,),
            )
            game = cur.fetchone()
            cover_id = (
                int(game["cover_id"])
                if game and game["cover_id"] is not None
                else None
            )
            if cover_id:
                cur.execute(
                    "SELECT image_id FROM igdb_covers WHERE id = %s",
                    (cover_id,),
                )
                row = cur.fetchone()
                if row and row["image_id"]:
                    return str(row["image_id"]).strip() or None
            cur.execute(
                """
                SELECT image_id FROM igdb_covers
                WHERE game_id = %s AND BTRIM(image_id) != ''
                LIMIT 1
                """,
                (game_id,),
            )
            row = cur.fetchone()
            if row and row["image_id"]:
                return str(row["image_id"]).strip() or None
            cur.execute(
                """
                SELECT image_id FROM igdb_artworks
                WHERE game_id = %s AND BTRIM(image_id) != ''
                LIMIT 1
                """,
                (game_id,),
            )
            row = cur.fetchone()
            if row and row["image_id"]:
                return str(row["image_id"]).strip() or None
        return None

    def igdb_summary_for_twitch(self, twitch_uid: str) -> str | None:
        uid = str(twitch_uid or "").strip()
        if not uid:
            return None
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT game_id FROM igdb_external_twitch WHERE twitch_uid = %s",
                (uid,),
            )
            ext = cur.fetchone()
            if not ext:
                return None
            cur.execute(
                "SELECT summary FROM igdb_games WHERE id = %s",
                (int(ext["game_id"]),),
            )
            game = cur.fetchone()
        if not game:
            return None
        text = str(game["summary"] or "").strip()
        return text or None

    def get_igdb_summary_translation(
        self, source_hash: str, lang: str
    ) -> str | None:
        h = str(source_hash or "").strip()
        loc = str(lang or "").strip()
        if not h or not loc:
            return None
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT translated FROM igdb_summary_translations
                WHERE source_hash = %s AND lang = %s
                """,
                (h, loc),
            )
            row = cur.fetchone()
        if not row:
            return None
        text = str(row["translated"] or "").strip()
        return text or None

    def set_igdb_summary_translation(
        self, source_hash: str, lang: str, translated: str
    ) -> None:
        h = str(source_hash or "").strip()
        loc = str(lang or "").strip()
        text = str(translated or "").strip()
        if not h or not loc or not text:
            return
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                INSERT INTO igdb_summary_translations (source_hash, lang, translated)
                VALUES (%s, %s, %s)
                ON CONFLICT (source_hash, lang) DO UPDATE SET translated = EXCLUDED.translated
                """,
                (h, loc, text),
            )

    def igdb_store_links_for_twitch(self, twitch_uid: str) -> dict[str, str | None]:
        """Helix category id → IGDB slug + Steam app id (local dumps)."""
        uid = str(twitch_uid or "").strip()
        out: dict[str, str | None] = {"slug": None, "steam_app_id": None}
        if not uid:
            return out
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT game_id FROM igdb_external_twitch WHERE twitch_uid = %s",
                (uid,),
            )
            ext = cur.fetchone()
            if not ext:
                return out
            game_id = int(ext["game_id"])
            cur.execute(
                "SELECT slug FROM igdb_games WHERE id = %s",
                (game_id,),
            )
            game = cur.fetchone()
            if game:
                slug = str(game["slug"] or "").strip().lower()
                if slug:
                    out["slug"] = slug
            cur.execute(
                """
                SELECT steam_uid FROM igdb_external_steam
                WHERE game_id = %s
                ORDER BY LENGTH(steam_uid) ASC, steam_uid ASC
                LIMIT 1
                """,
                (game_id,),
            )
            steam = cur.fetchone()
            if steam:
                app_id = str(steam["steam_uid"] or "").strip()
                if app_id.isdigit():
                    out["steam_app_id"] = app_id
        return out

    @staticmethod
    def _igdb_game_row(r: Any) -> dict[str, Any]:
        return {
            "id": int(r["id"]),
            "name": str(r["name"]),
            "first_release_date": (
                int(r["first_release_date"])
                if r["first_release_date"] is not None
                else None
            ),
            "cover_id": int(r["cover_id"]) if r["cover_id"] is not None else None,
            "summary": str(r["summary"] or "").strip(),
        }

    def igdb_search_games_by_name(
        self, query: str, *, limit: int = 5
    ) -> list[dict[str, Any]]:
        from search_normalize import (
            expand_search_token_sets,
            name_matches_any_token_set,
            rank_igdb_game_hit,
        )

        token_sets = expand_search_token_sets(query)
        if not token_sets:
            return []
        lim = max(1, min(100, int(limit)))
        q_exact = (query or "").strip()
        pool_cap = max(120, lim)
        with self._conn() as conn:
            cur = self._cursor(conn)
            # Exact title hits first (all of them, up to 20) so duplicate names
            # like several "The Cube" are not truncated by LIMIT before Mundfish.
            cur.execute(
                """
                SELECT id, name, first_release_date, cover_id, summary,
                       total_rating_count
                FROM igdb_games
                WHERE lower(name) = lower(%s)
                ORDER BY lower(name) ASC, id ASC
                LIMIT 20
                """,
                (q_exact,),
            )
            exact = [self._igdb_game_row(r) for r in cur.fetchall()]
            if len(exact) >= lim:
                return exact
            exclude = {int(g["id"]) for g in exact}
            by_id: dict[int, dict[str, Any]] = {}
            for tokens in token_sets:
                where = " AND ".join(["name ILIKE %s"] * len(tokens))
                params: list[Any] = [f"%{t}%" for t in tokens]
                if exclude:
                    where += f" AND id NOT IN ({','.join(['%s'] * len(exclude))})"
                    params.extend(sorted(exclude))
                params.append(pool_cap)
                cur.execute(
                    f"""
                    SELECT id, name, first_release_date, cover_id, summary,
                           total_rating_count
                    FROM igdb_games
                    WHERE {where}
                    LIMIT %s
                    """,
                    params,
                )
                for r in cur.fetchall():
                    gid = int(r["id"])
                    if gid in by_id or gid in exclude:
                        continue
                    name = str(r["name"] or "")
                    if not name_matches_any_token_set(name, token_sets):
                        continue
                    by_id[gid] = {
                        **self._igdb_game_row(r),
                        "total_rating_count": int(r["total_rating_count"] or 0),
                        "first_release_date": (
                            int(r["first_release_date"])
                            if r["first_release_date"] is not None
                            else None
                        ),
                    }
        fuzzy = list(by_id.values())
        fuzzy.sort(key=lambda g: rank_igdb_game_hit(g, query=q_exact, token_sets=token_sets))
        out = exact + fuzzy[: max(0, lim - len(exact))]
        for g in out:
            g.pop("total_rating_count", None)
        return out

    def igdb_twitch_uids_for_game(self, game_id: int) -> list[str]:
        gid = int(game_id or 0)
        if gid <= 0:
            return []
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT twitch_uid FROM igdb_external_twitch
                WHERE game_id = %s
                ORDER BY twitch_uid
                """,
                (gid,),
            )
            rows = cur.fetchall()
        return [
            str(r["twitch_uid"]).strip()
            for r in rows
            if str(r["twitch_uid"] or "").strip()
        ]

    def igdb_company_labels_for_games(
        self, game_ids: list[int]
    ) -> dict[int, str]:
        """Publisher name per game, else developer (search-hit disambiguation)."""
        ids = sorted({int(g) for g in game_ids if int(g or 0) > 0})
        if not ids:
            return {}
        placeholders = ",".join(["%s"] * len(ids))
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                f"""
                SELECT i.game_id, c.name, i.is_publisher, i.is_developer
                FROM igdb_involved i
                JOIN igdb_companies c ON c.id = i.company_id
                WHERE i.game_id IN ({placeholders})
                  AND (i.is_publisher IS TRUE OR i.is_developer IS TRUE)
                  AND TRIM(c.name) != ''
                ORDER BY i.game_id ASC,
                         i.is_publisher DESC,
                         LOWER(c.name) ASC
                """,
                ids,
            )
            rows = cur.fetchall()
        out: dict[int, str] = {}
        for r in rows:
            gid = int(r["game_id"])
            if gid not in out:
                out[gid] = str(r["name"]).strip()
        return out

    def igdb_publisher_developer_names(self, game_id: int) -> tuple[str, str]:
        gid = int(game_id or 0)
        if gid <= 0:
            return ("", "")
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT c.name, i.is_publisher, i.is_developer
                FROM igdb_involved i
                JOIN igdb_companies c ON c.id = i.company_id
                WHERE i.game_id = %s
                  AND (i.is_publisher IS TRUE OR i.is_developer IS TRUE)
                  AND TRIM(c.name) != ''
                ORDER BY i.is_publisher DESC, LOWER(c.name) ASC
                """,
                (gid,),
            )
            rows = cur.fetchall()
        pubs: list[str] = []
        devs: list[str] = []
        for r in rows:
            name = str(r["name"] or "").strip()
            if not name:
                continue
            if bool(r["is_publisher"]) and name not in pubs:
                pubs.append(name)
            if bool(r["is_developer"]) and name not in devs:
                devs.append(name)
        return (", ".join(pubs), ", ".join(devs))

    def igdb_company_labels_for_twitch_uids(
        self, twitch_uids: list[str]
    ) -> dict[str, str]:
        """Publisher (else developer) label keyed by Twitch category id."""
        uids = sorted({str(u).strip() for u in twitch_uids if str(u or "").strip()})
        if not uids:
            return {}
        placeholders = ",".join(["%s"] * len(uids))
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                f"""
                SELECT e.twitch_uid, c.name, i.is_publisher, i.is_developer
                FROM igdb_external_twitch e
                JOIN igdb_involved i ON i.game_id = e.game_id
                JOIN igdb_companies c ON c.id = i.company_id
                WHERE e.twitch_uid IN ({placeholders})
                  AND (i.is_publisher IS TRUE OR i.is_developer IS TRUE)
                  AND TRIM(c.name) != ''
                ORDER BY e.twitch_uid ASC,
                         i.is_publisher DESC,
                         LOWER(c.name) ASC
                """,
                uids,
            )
            rows = cur.fetchall()
        out: dict[str, str] = {}
        for r in rows:
            uid = str(r["twitch_uid"]).strip()
            if uid and uid not in out:
                out[uid] = str(r["name"]).strip()
        return out

    def igdb_game_by_id(self, game_id: int) -> dict[str, Any] | None:
        gid = int(game_id or 0)
        if gid <= 0:
            return None
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT id, name, slug, first_release_date, cover_id, summary
                FROM igdb_games WHERE id = %s
                """,
                (gid,),
            )
            r = cur.fetchone()
        if not r:
            return None
        return {
            "id": int(r["id"]),
            "name": str(r["name"]),
            "slug": str(r["slug"] or "").strip() or None,
            "first_release_date": (
                int(r["first_release_date"])
                if r["first_release_date"] is not None
                else None
            ),
            "cover_id": int(r["cover_id"]) if r["cover_id"] is not None else None,
            "summary": str(r["summary"] or "").strip(),
        }

    def igdb_cover_image_id_for_game(self, game_id: int) -> str | None:
        gid = int(game_id or 0)
        if gid <= 0:
            return None
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                "SELECT cover_id FROM igdb_games WHERE id = %s",
                (gid,),
            )
            game = cur.fetchone()
            cover_id = (
                int(game["cover_id"]) if game and game["cover_id"] is not None else None
            )
            if cover_id:
                cur.execute(
                    "SELECT image_id FROM igdb_covers WHERE id = %s",
                    (cover_id,),
                )
                row = cur.fetchone()
                if row and row["image_id"]:
                    return str(row["image_id"]).strip() or None
            cur.execute(
                """
                SELECT image_id FROM igdb_covers
                WHERE game_id = %s AND BTRIM(image_id) != ''
                LIMIT 1
                """,
                (gid,),
            )
            row = cur.fetchone()
            if row and row["image_id"]:
                return str(row["image_id"]).strip() or None
            cur.execute(
                """
                SELECT image_id FROM igdb_artworks
                WHERE game_id = %s AND BTRIM(image_id) != ''
                LIMIT 1
                """,
                (gid,),
            )
            row = cur.fetchone()
            if row and row["image_id"]:
                return str(row["image_id"]).strip() or None
        return None

    def igdb_release_dates_for_game(self, game_id: int) -> list[dict[str, Any]]:
        gid = int(game_id or 0)
        if gid <= 0:
            return []
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT rd.id, rd.game_id, rd.platform_id, rd.date, rd.human,
                       COALESCE(p.name, '') AS platform_name
                FROM igdb_release_dates rd
                LEFT JOIN igdb_platforms p ON p.id = rd.platform_id
                WHERE rd.game_id = %s AND rd.date IS NOT NULL
                ORDER BY rd.date ASC, platform_name ASC
                """,
                (gid,),
            )
            rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            pid = int(r["platform_id"] or 0)
            pname = str(r["platform_name"] or "").strip()
            if not pname and pid:
                pname = f"#{pid}"
            out.append(
                {
                    "id": int(r["id"]),
                    "game_id": int(r["game_id"]),
                    "platform_id": pid,
                    "platform_name": pname or "—",
                    "date": int(r["date"]),
                    "human": str(r["human"] or "").strip(),
                }
            )
        return out

    def get_release_watch_subscriptions(self) -> list[Subscription]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT * FROM subscriptions
                WHERE COALESCE(release_watch_prefs, '') != ''
                ORDER BY id
                """
            )
            rows = cur.fetchall()
        return [_row_to_sub(r) for r in rows]

    def get_giveaway_watch_subscriptions(self) -> list[Subscription]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT * FROM subscriptions
                WHERE COALESCE(giveaway_watch_prefs, '') != ''
                ORDER BY id
                """
            )
            rows = cur.fetchall()
        return [_row_to_sub(r) for r in rows]

    def igdb_platforms_for_game(self, game_id: int) -> list[dict[str, Any]]:
        gid = int(game_id or 0)
        if gid <= 0:
            return []
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT DISTINCT rd.platform_id,
                       COALESCE(p.name, '') AS platform_name
                FROM igdb_release_dates rd
                LEFT JOIN igdb_platforms p ON p.id = rd.platform_id
                WHERE rd.game_id = %s
                ORDER BY platform_name ASC
                """,
                (gid,),
            )
            rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        seen: set[int] = set()
        for r in rows:
            pid = int(r["platform_id"] or 0)
            if pid <= 0 or pid in seen:
                continue
            seen.add(pid)
            pname = str(r["platform_name"] or "").strip() or f"#{pid}"
            out.append({"platform_id": pid, "platform_name": pname})
        return out
