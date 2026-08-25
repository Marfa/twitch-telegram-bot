from __future__ import annotations

from pathlib import Path

from .models import (
    LUCKY_TEMPLATE_LIMIT,
    WATCH_MAX_FILTERS,
    AlertHistoryEntry,
    BotStats,
    ChatAuth,
    DeletedSubscriptionCartItem,
    PremiumChannel,
    ReferralStats,
    ReferralWithdrawal,
    ScheduledBroadcast,
    Subscription,
    TwitchSync,
    WatchFilter,
    WatchPrefs,
    WhisperAlert,
    alert_type_from_payload,
    build_lucky_seed_templates,
    dump_category_watch_prefs,
    dump_watch_filters,
    dump_watch_prefs,
    is_category_watch_sub,
    is_on_notify_cooldown,
    parse_category_watch_prefs,
    parse_watch_filters,
    parse_watch_prefs,
    watch_filter_auto_name,
)
from .postgres import PostgresDatabase, _normalize_pg_url
from .protocol import Database
from .sqlite import SqliteDatabase

__all__ = [
    "LUCKY_TEMPLATE_LIMIT",
    "WATCH_MAX_FILTERS",
    "AlertHistoryEntry",
    "BotStats",
    "ChatAuth",
    "Database",
    "DeletedSubscriptionCartItem",
    "PostgresDatabase",
    "PremiumChannel",
    "ReferralStats",
    "ReferralWithdrawal",
    "ScheduledBroadcast",
    "SqliteDatabase",
    "Subscription",
    "TwitchSync",
    "WatchFilter",
    "WatchPrefs",
    "WhisperAlert",
    "_normalize_pg_url",
    "alert_type_from_payload",
    "build_lucky_seed_templates",
    "dump_category_watch_prefs",
    "dump_watch_filters",
    "dump_watch_prefs",
    "is_category_watch_sub",
    "is_on_notify_cooldown",
    "open_database",
    "parse_category_watch_prefs",
    "parse_watch_filters",
    "parse_watch_prefs",
    "watch_filter_auto_name",
]


def open_database(path: Path, database_url: str | None = None) -> Database:
    if database_url:
        return PostgresDatabase(database_url)
    return SqliteDatabase(path)
