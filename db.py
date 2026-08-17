from __future__ import annotations

import json
import logging
import random
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Protocol
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

logger = logging.getLogger(__name__)

LUCKY_TEMPLATE_LIMIT = 100


def build_lucky_seed_templates(locale: str) -> list[str]:
    """Build a full fallback pool (100) for a locale — used to seed empty DBs."""
    if str(locale).lower().startswith("ru"):
        heads = (
            "{username} в эфире!",
            "🔴 {username} начал стрим",
            "{username} онлайн!",
            "Стрим начался: {username}",
            "Гоу смотреть {username}!",
            "⚡ {username} запустил трансляцию",
            "{username} уже в эфире",
            "🔴 LIVE — {username}",
            "Залетай к {username}",
            "Не пропусти стрим {username}",
        )
        middles = (
            "{name}",
            "«{name}»",
            "Название: {name}",
            "Стрим: {name}",
            "Сейчас: {name}",
        )
        tails = (
            "Категория: {game}",
            "Игра: {game}",
            "{game}",
            "Категория — {game}",
            "Играет в {game}",
        )
    else:
        heads = (
            "{username} is live!",
            "🔴 {username} started streaming",
            "{username} is online!",
            "Stream started: {username}",
            "Come watch {username}!",
            "⚡ {username} just went live",
            "{username} is already live",
            "🔴 LIVE — {username}",
            "Jump in with {username}",
            "Don't miss {username}'s stream",
        )
        middles = (
            "{name}",
            "“{name}”",
            "Title: {name}",
            "Stream: {name}",
            "Now: {name}",
        )
        tails = (
            "Category: {game}",
            "Playing: {game}",
            "{game}",
            "Category — {game}",
            "Playing {game}",
        )
    out: list[str] = []
    seen: set[str] = set()
    for head in heads:
        for middle in middles:
            for tail in tails:
                text = f"{head}\n{middle}\n{tail}"
                if text in seen:
                    continue
                seen.add(text)
                out.append(text)
                if len(out) >= LUCKY_TEMPLATE_LIMIT:
                    return out
    return out


def _seed_lucky_templates_sqlite(conn: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc).isoformat()
    for loc in ("ru", "en"):
        rows = conn.execute(
            "SELECT text FROM lucky_templates WHERE locale = ?",
            (loc,),
        ).fetchall()
        existing = {str(r["text"]) for r in rows}
        count = len(existing)
        if count >= LUCKY_TEMPLATE_LIMIT:
            continue
        for text in build_lucky_seed_templates(loc):
            if count >= LUCKY_TEMPLATE_LIMIT:
                break
            if text in existing:
                continue
            conn.execute(
                "INSERT INTO lucky_templates (locale, text, created_at) VALUES (?, ?, ?)",
                (loc, text, now),
            )
            existing.add(text)
            count += 1


def _seed_lucky_templates_pg(cur: Any) -> None:
    for loc in ("ru", "en"):
        cur.execute(
            "SELECT text FROM lucky_templates WHERE locale = %s",
            (loc,),
        )
        existing = {str(r["text"]) for r in cur.fetchall()}
        count = len(existing)
        if count >= LUCKY_TEMPLATE_LIMIT:
            continue
        for text in build_lucky_seed_templates(loc):
            if count >= LUCKY_TEMPLATE_LIMIT:
                break
            if text in existing:
                continue
            cur.execute(
                """
                INSERT INTO lucky_templates (locale, text, created_at)
                VALUES (%s, %s, NOW())
                """,
                (loc, text),
            )
            existing.add(text)
            count += 1


@dataclass
class BotStats:
    users: int
    notify_users: int
    subscriptions_total: int
    subscriptions_enabled: int
    subscriptions_disabled: int
    unique_owners: int
    unique_twitch_channels: int
    premium_paid: int
    sys_updates: int
    sys_availability: int
    sys_other: int
    blocked_users: int
    locale_en: int
    locale_ru: int
    locale_unset: int


@dataclass
class ReferralStats:
    invited: int
    payments: int
    available_stars: int


@dataclass
class ReferralWithdrawal:
    id: int
    user_id: int
    amount: int
    status: str
    created_at: str
    resolved_at: str | None


@dataclass
class AlertHistoryEntry:
    id: int
    owner_id: int
    subscription_id: int | None
    twitch_username: str
    alert_type: str
    message_text: str
    sent_at: str
    twitch_user_id: str = ""
    stream_id: str = ""
    vod_id: str = ""
    vod_offset_seconds: int | None = None


@dataclass
class WatchPrefs:
    categories: list[dict[str, str]] = field(default_factory=list)
    min_viewers: int = 0
    max_viewers: int | None = None
    language: str | None = None
    tags: list[str] = field(default_factory=list)
    exclude_mature: bool = True


@dataclass
class WatchFilter:
    id: str
    name: str
    prefs: WatchPrefs


WATCH_MAX_FILTERS = 5


def watch_filter_auto_name(prefs: WatchPrefs) -> str:
    cats = ", ".join(c["name"] for c in prefs.categories) or "Filter"
    return cats[:60]


def _parse_watch_prefs_dict(data: dict[str, Any]) -> WatchPrefs | None:
    cats_raw = data.get("categories") or []
    categories: list[dict[str, str]] = []
    if isinstance(cats_raw, list):
        for c in cats_raw:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("id") or "").strip()
            name = str(c.get("name") or "").strip()
            if cid and name:
                categories.append({"id": cid, "name": name})
    if not categories:
        return None
    min_v = int(data.get("min_viewers") or 0)
    max_raw = data.get("max_viewers")
    max_v = int(max_raw) if max_raw is not None and str(max_raw).strip() != "" else None
    lang = data.get("language")
    language = str(lang).strip().lower() if lang else None
    if language == "":
        language = None
    tags: list[str] = []
    tags_raw = data.get("tags") or []
    if isinstance(tags_raw, list):
        seen: set[str] = set()
        for item in tags_raw:
            tag = str(item or "").strip()
            if not tag:
                continue
            key = tag.lower()
            if key in seen:
                continue
            seen.add(key)
            tags.append(tag)
    return WatchPrefs(
        categories=categories,
        min_viewers=max(0, min_v),
        max_viewers=max_v if max_v is None else max(0, max_v),
        language=language,
        tags=tags,
        exclude_mature=bool(data.get("exclude_mature", True)),
    )


def parse_watch_prefs(raw: str | None) -> WatchPrefs | None:
    """Legacy single-filter parse (first saved filter, or old JSON shape)."""
    filters = parse_watch_filters(raw)
    return filters[0].prefs if filters else None


def parse_watch_filters(raw: str | None) -> list[WatchFilter]:
    if not raw or not str(raw).strip():
        return []
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    items: list[Any]
    if isinstance(data, dict) and isinstance(data.get("filters"), list):
        items = data["filters"]
    elif isinstance(data, dict) and data.get("categories"):
        # Migrate old single-prefs blob.
        items = [data]
    else:
        return []
    out: list[WatchFilter] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        prefs = _parse_watch_prefs_dict(item)
        if not prefs:
            continue
        fid = str(item.get("id") or "").strip() or secrets.token_hex(4)
        name = str(item.get("name") or "").strip() or watch_filter_auto_name(prefs)
        out.append(WatchFilter(id=fid, name=name[:60], prefs=prefs))
        if len(out) >= WATCH_MAX_FILTERS:
            break
    return out


def dump_watch_prefs(prefs: WatchPrefs) -> str:
    """Legacy: store as a one-item filter list."""
    return dump_watch_filters(
        [WatchFilter(id=secrets.token_hex(4), name=watch_filter_auto_name(prefs), prefs=prefs)]
    )


def dump_category_watch_prefs(prefs: WatchPrefs) -> str:
    """Stable JSON for category-watch subscriptions (no random filter id)."""
    return json.dumps(
        {
            "categories": prefs.categories,
            "min_viewers": prefs.min_viewers,
            "max_viewers": prefs.max_viewers,
            "language": prefs.language,
            "tags": list(prefs.tags),
            "exclude_mature": prefs.exclude_mature,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def parse_category_watch_prefs(raw: str | None) -> WatchPrefs | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return _parse_watch_prefs_dict(data)


def dump_watch_filters(filters: list[WatchFilter]) -> str:
    payload = {
        "filters": [
            {
                "id": f.id,
                "name": f.name,
                **asdict(f.prefs),
            }
            for f in filters[:WATCH_MAX_FILTERS]
        ]
    }
    return json.dumps(payload, ensure_ascii=False)


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
    strip_name_mentions: bool
    delay_minutes: int
    suppress_repeat_minutes: int
    schedule_reminder_minutes: int
    schedule_reminder_configured: bool
    notify_on_live: bool
    notify_on_end: bool
    notify_on_category_change: bool
    ignore_keywords: str
    use_global_ignore: bool
    image_file_id: str | None
    image_position: str
    notify_cooldown_until: str | None
    last_message_id: int | None
    last_schedule_reminder_segment_id: str | None
    from_twitch_sync: bool
    from_watch_suggest: bool
    sync_user_edited: bool
    category_watch_prefs: str
    category_watch_live_ids: str
    category_watch_primed: bool
    delete_other_alerts: bool
    is_demo: bool
    trial_paused: bool


def is_category_watch_sub(sub: Subscription) -> bool:
    return bool((sub.category_watch_prefs or "").strip())


@dataclass
class ScheduledBroadcast:
    id: int
    msg_type: str
    text: str
    scheduled_at: str
    created_by: int
    recipient_ids: str = ""


@dataclass
class TwitchSync:
    owner_id: int
    twitch_user_id: str
    refresh_token: str
    period_days: int
    next_sync_at: str
    last_sync_at: str | None


@dataclass
class WhisperAlert:
    owner_id: int
    enabled: bool
    twitch_user_id: str
    twitch_login: str
    refresh_token: str
    eventsub_id: str


def _scheduled_broadcast_from_row(row: Any) -> ScheduledBroadcast:
    scheduled_at = row["scheduled_at"]
    if scheduled_at is not None and not isinstance(scheduled_at, str):
        scheduled_at = scheduled_at.isoformat()
    try:
        recipient_ids = str(row["recipient_ids"] or "")
    except (KeyError, IndexError, TypeError):
        recipient_ids = ""
    return ScheduledBroadcast(
        id=int(row["id"]),
        msg_type=str(row["msg_type"]),
        text=str(row["text"]),
        scheduled_at=str(scheduled_at),
        created_by=int(row["created_by"]),
        recipient_ids=recipient_ids,
    )


def _row_to_twitch_sync(row: Any) -> TwitchSync:
    last = row["last_sync_at"]
    if last is not None and not isinstance(last, str):
        last = last.isoformat()
    next_at = row["next_sync_at"]
    if next_at is not None and not isinstance(next_at, str):
        next_at = next_at.isoformat()
    return TwitchSync(
        owner_id=int(row["owner_id"]),
        twitch_user_id=str(row["twitch_user_id"]),
        refresh_token=str(row["refresh_token"]),
        period_days=int(row["period_days"]),
        next_sync_at=str(next_at),
        last_sync_at=str(last) if last else None,
    )


def _row_to_whisper_alert(row: Any) -> WhisperAlert:
    return WhisperAlert(
        owner_id=int(row["owner_id"]),
        enabled=bool(row["enabled"]),
        twitch_user_id=str(row["twitch_user_id"] or ""),
        twitch_login=str(row["twitch_login"] or ""),
        refresh_token=str(row["refresh_token"] or ""),
        eventsub_id=str(row["eventsub_id"] or ""),
    )


def _row_to_referral_withdrawal(row: Any) -> ReferralWithdrawal:
    created = row["created_at"]
    if created is not None and not isinstance(created, str):
        created = created.isoformat()
    resolved = row["resolved_at"]
    if resolved is not None and not isinstance(resolved, str):
        resolved = resolved.isoformat()
    return ReferralWithdrawal(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        amount=int(row["amount"]),
        status=str(row["status"]),
        created_at=str(created or ""),
        resolved_at=str(resolved) if resolved else None,
    )


def _row_to_alert_history(row: Any) -> AlertHistoryEntry:
    sent = row["sent_at"]
    if sent is not None and not isinstance(sent, str):
        sent = sent.isoformat()
    sub_id = row["subscription_id"]
    keys = set(row.keys())
    message_text = ""
    if "message_text" in keys and row["message_text"] is not None:
        message_text = str(row["message_text"])
    offset = None
    if "vod_offset_seconds" in keys and row["vod_offset_seconds"] is not None:
        offset = int(row["vod_offset_seconds"])
    return AlertHistoryEntry(
        id=int(row["id"]),
        owner_id=int(row["owner_id"]),
        subscription_id=int(sub_id) if sub_id is not None else None,
        twitch_username=str(row["twitch_username"] or ""),
        alert_type=str(row["alert_type"] or ""),
        message_text=message_text,
        sent_at=str(sent or ""),
        twitch_user_id=str(row["twitch_user_id"] or "") if "twitch_user_id" in keys else "",
        stream_id=str(row["stream_id"] or "") if "stream_id" in keys else "",
        vod_id=str(row["vod_id"] or "") if "vod_id" in keys else "",
        vod_offset_seconds=offset,
    )


def _row_to_sub(row: Any) -> Subscription:
    keys = set(row.keys())
    image_file_id = None
    if "image_file_id" in keys and row["image_file_id"]:
        image_file_id = str(row["image_file_id"])
    image_position = ""
    if "image_position" in keys and row["image_position"]:
        image_position = str(row["image_position"])
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
        strip_name_mentions=bool(row["strip_name_mentions"])
        if "strip_name_mentions" in keys
        else False,
        delay_minutes=int(row["delay_minutes"] or 0),
        suppress_repeat_minutes=int(row["suppress_repeat_minutes"] or 0),
        schedule_reminder_minutes=int(row["schedule_reminder_minutes"] or 0)
        if "schedule_reminder_minutes" in keys
        else 0,
        schedule_reminder_configured=bool(row["schedule_reminder_configured"])
        if "schedule_reminder_configured" in keys
        else False,
        notify_on_live=bool(row["notify_on_live"]) if "notify_on_live" in keys else True,
        notify_on_end=bool(row["notify_on_end"]) if "notify_on_end" in keys else False,
        notify_on_category_change=bool(row["notify_on_category_change"])
        if "notify_on_category_change" in keys
        else False,
        ignore_keywords=str(row["ignore_keywords"] or ""),
        use_global_ignore=bool(row["use_global_ignore"])
        if "use_global_ignore" in keys
        else False,
        image_file_id=image_file_id,
        image_position=image_position if image_file_id else "",
        notify_cooldown_until=(
            row["notify_cooldown_until"].isoformat()
            if row["notify_cooldown_until"] is not None
            and not isinstance(row["notify_cooldown_until"], str)
            else row["notify_cooldown_until"]
        ),
        last_message_id=row["last_message_id"],
        last_schedule_reminder_segment_id=(
            str(row["last_schedule_reminder_segment_id"])
            if "last_schedule_reminder_segment_id" in keys
            and row["last_schedule_reminder_segment_id"]
            else None
        ),
        from_twitch_sync=bool(row["from_twitch_sync"])
        if "from_twitch_sync" in keys
        else False,
        from_watch_suggest=bool(row["from_watch_suggest"])
        if "from_watch_suggest" in keys
        else False,
        sync_user_edited=bool(row["sync_user_edited"])
        if "sync_user_edited" in keys
        else False,
        category_watch_prefs=str(row["category_watch_prefs"] or "")
        if "category_watch_prefs" in keys
        else "",
        category_watch_live_ids=str(row["category_watch_live_ids"] or "")
        if "category_watch_live_ids" in keys
        else "",
        category_watch_primed=bool(row["category_watch_primed"])
        if "category_watch_primed" in keys
        else False,
        delete_other_alerts=bool(row["delete_other_alerts"])
        if "delete_other_alerts" in keys
        else False,
        is_demo=bool(row["is_demo"]) if "is_demo" in keys else False,
        trial_paused=bool(row["trial_paused"]) if "trial_paused" in keys else False,
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
        strip_name_mentions: bool = False,
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
    ) -> int: ...

    def set_last_message_id(self, sub_id: int, message_id: int | None) -> None: ...

    def set_notify_cooldown(self, sub_id: int, minutes: int) -> None: ...

    def set_last_schedule_reminder_segment(
        self, sub_id: int, segment_id: str
    ) -> None: ...

    def get_subscription_by_id(self, sub_id: int) -> Subscription | None: ...

    def get_subscriptions_by_owner(self, owner_id: int) -> list[Subscription]: ...

    def get_subscription(self, sub_id: int, owner_id: int) -> Subscription | None: ...

    def get_unique_schedule_reminder_twitch_ids(self) -> list[str]: ...

    def toggle_subscription(self, sub_id: int, owner_id: int) -> bool | None: ...

    def enable_all_subscriptions(self, owner_id: int, *, demo: bool = False) -> int: ...

    def delete_subscription(self, sub_id: int, owner_id: int) -> bool: ...

    def update_subscription(self, sub_id: int, owner_id: int, **fields: object) -> bool: ...

    def get_user_locale(self, user_id: int) -> str | None: ...

    def get_user_locales(self, user_ids: list[int]) -> dict[int, str | None]: ...

    def set_user_locale(self, user_id: int, locale: str) -> None: ...

    def get_unique_twitch_user_ids(self) -> list[str]: ...

    def get_enabled_category_watch_subscriptions(self) -> list[Subscription]: ...

    def set_category_watch_live_state(
        self, sub_id: int, live_ids: list[str], *, primed: bool
    ) -> None: ...

    def get_enabled_by_twitch_user_id(self, twitch_user_id: str) -> list[Subscription]: ...

    def get_all_owner_ids(self) -> list[int]: ...

    def upsert_user(self, user_id: int) -> None: ...

    def count_new_users_since(self, since: datetime) -> int: ...

    def count_stars_payers_since(self, since: datetime) -> int: ...

    def set_referred_by(self, user_id: int, referrer_id: int) -> bool: ...

    def get_referred_by(self, user_id: int) -> int | None: ...

    def add_referral_credit(
        self,
        *,
        referrer_id: int,
        invitee_id: int,
        charge_id: str,
        stars_paid: int,
        commission_stars: int,
    ) -> bool: ...

    def get_referral_stats(self, user_id: int) -> ReferralStats: ...

    def request_referral_withdrawal(self, user_id: int, amount: int) -> int | None: ...

    def get_referral_withdrawal(self, withdrawal_id: int) -> ReferralWithdrawal | None: ...

    def list_referral_withdrawals(
        self, user_id: int, *, limit: int = 20
    ) -> list[ReferralWithdrawal]: ...

    def list_pending_referral_withdrawals(self) -> list[ReferralWithdrawal]: ...

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
    ) -> None: ...

    def set_alert_history_vod_id(self, history_id: int, vod_id: str) -> None: ...

    def list_alert_history(
        self,
        owner_id: int,
        *,
        since: datetime | None = None,
        limit: int = 500,
    ) -> list[AlertHistoryEntry]: ...

    def resolve_referral_withdrawal(
        self, withdrawal_id: int, status: str
    ) -> ReferralWithdrawal | None: ...

    def set_bot_blocked(self, user_id: int, blocked: bool) -> None: ...

    def is_bot_blocked(self, user_id: int) -> bool: ...

    def get_notify_user_ids(self) -> list[int]: ...

    def get_bot_update_recipients(self) -> list[int]: ...

    def get_availability_recipients(self) -> list[int]: ...

    def get_other_recipients(self) -> list[int]: ...

    def get_receive_bot_updates(self, user_id: int) -> bool: ...

    def set_receive_bot_updates(self, user_id: int, enabled: bool) -> None: ...

    def get_receive_availability_updates(self, user_id: int) -> bool: ...

    def set_receive_availability_updates(self, user_id: int, enabled: bool) -> None: ...

    def get_receive_other_updates(self, user_id: int) -> bool: ...

    def set_receive_other_updates(self, user_id: int, enabled: bool) -> None: ...

    def get_receive_sync_updates(self, user_id: int) -> bool: ...

    def set_receive_sync_updates(self, user_id: int, enabled: bool) -> None: ...

    def get_global_ignore_keywords(self, user_id: int) -> str: ...

    def set_global_ignore_keywords(self, user_id: int, keywords: str) -> None: ...

    def get_advanced_mode_setting(self, user_id: int) -> bool | None: ...

    def set_advanced_mode_setting(self, user_id: int, enabled: bool) -> None: ...

    def owner_has_advanced_subscription_options(self, owner_id: int) -> bool: ...

    def get_saved_schedule(self, user_id: int) -> tuple[int | None, int | None]: ...

    def set_saved_schedule(self, user_id: int, hour: int, minute: int) -> None: ...

    def get_watch_prefs(self, user_id: int) -> WatchPrefs | None: ...

    def set_watch_prefs(self, user_id: int, prefs: WatchPrefs) -> None: ...

    def clear_watch_prefs(self, user_id: int) -> None: ...

    def get_watch_filters(self, user_id: int) -> list[WatchFilter]: ...

    def set_watch_filters(self, user_id: int, filters: list[WatchFilter]) -> None: ...

    def add_watch_filter(
        self, user_id: int, prefs: WatchPrefs, *, name: str | None = None
    ) -> WatchFilter: ...

    def delete_watch_filter(self, user_id: int, filter_id: str) -> bool: ...

    def count_enabled_subscriptions(self, owner_id: int, *, demo: bool = False) -> int: ...

    def delete_demo_subscriptions(self, owner_id: int) -> int: ...

    def get_premium_status(self, user_id: int) -> Any: ...

    def set_premium_stars(
        self,
        user_id: int,
        *,
        charge_id: str,
        until_unix: int,
        canceled: bool,
    ) -> None: ...

    def set_premium_stars_canceled(self, user_id: int, canceled: bool) -> None: ...

    def set_premium_permanent(self, user_id: int, permanent: bool) -> None: ...

    def clear_premium(self, user_id: int) -> None: ...

    def set_premium_trial(
        self, user_id: int, *, until_unix: int, used: bool = True
    ) -> None: ...

    def expire_premium_trial(self, user_id: int) -> int: ...

    def extend_premium_features(
        self,
        user_id: int,
        feature_ids: list[str],
        *,
        until_unix: int,
        charge_id: str = "",
    ) -> None: ...

    def clear_premium_feature(self, user_id: int, feature_id: str) -> None: ...

    def set_premium_feature_canceled(self, user_id: int, feature_id: str) -> None: ...

    def set_premium_twitch(
        self,
        user_id: int,
        *,
        active: bool,
        twitch_user_id: str | None = None,
        refresh_token: str | None = None,
    ) -> None: ...

    def set_premium_twitch_refresh(self, user_id: int, refresh_token: str) -> None: ...

    def get_premium_twitch_refresh(self, user_id: int) -> str | None: ...

    def list_premium_twitch_user_ids(self) -> list[int]: ...

    def add_scheduled_broadcast(
        self,
        msg_type: str,
        text: str,
        scheduled_at: str,
        created_by: int,
        recipient_ids: str = "",
    ) -> int: ...

    def get_pending_scheduled_broadcasts(self) -> list[ScheduledBroadcast]: ...

    def get_unsent_scheduled_broadcasts(self) -> list[ScheduledBroadcast]: ...

    def get_scheduled_broadcast(self, broadcast_id: int) -> ScheduledBroadcast | None: ...

    def update_scheduled_broadcast(self, broadcast_id: int, **fields: object) -> bool: ...

    def delete_scheduled_broadcast(self, broadcast_id: int) -> bool: ...

    def mark_scheduled_broadcast_sent(self, broadcast_id: int) -> None: ...

    def add_lucky_template(self, locale: str, text: str) -> None: ...

    def pick_lucky_template(self, locale: str) -> str | None: ...

    def get_bot_stats(self) -> BotStats: ...

    def upsert_twitch_sync(
        self,
        owner_id: int,
        twitch_user_id: str,
        refresh_token: str,
        period_days: int,
        next_sync_at: str,
        last_sync_at: str | None = None,
    ) -> None: ...

    def get_twitch_sync(self, owner_id: int) -> TwitchSync | None: ...

    def delete_twitch_sync(self, owner_id: int) -> bool: ...

    def set_twitch_sync_period(
        self, owner_id: int, period_days: int, next_sync_at: str
    ) -> bool: ...

    def update_twitch_sync_tokens(
        self,
        owner_id: int,
        refresh_token: str,
        *,
        last_sync_at: str,
        next_sync_at: str,
    ) -> None: ...

    def get_due_twitch_syncs(self, now_iso: str) -> list[TwitchSync]: ...

    def get_whisper_alert(self, owner_id: int) -> WhisperAlert | None: ...

    def get_whisper_alerts_by_twitch_user_id(
        self, twitch_user_id: str
    ) -> list[WhisperAlert]: ...

    def upsert_whisper_alert(
        self,
        owner_id: int,
        *,
        enabled: bool,
        twitch_user_id: str,
        twitch_login: str,
        refresh_token: str,
        eventsub_id: str = "",
    ) -> None: ...

    def set_whisper_alert_enabled(
        self,
        owner_id: int,
        enabled: bool,
        *,
        eventsub_id: str | None = None,
    ) -> None: ...

    def disable_whisper_alerts_for_twitch_user(self, twitch_user_id: str) -> list[int]: ...

    def delete_synced_subscriptions_missing(
        self, owner_id: int, keep_twitch_user_ids: set[str]
    ) -> int: ...

    def get_unfollowed_manual_alert_streamers(
        self,
        owner_id: int,
        keep_twitch_user_ids: set[str],
        *,
        is_demo: bool = False,
    ) -> list[dict[str, str]]: ...

    def delete_subscriptions_for_twitch_users(
        self,
        owner_id: int,
        twitch_user_ids: set[str],
        *,
        is_demo: bool = False,
    ) -> int: ...

    def is_beta_enrolled(self, user_id: int, feature_id: str) -> bool: ...

    def set_beta_enrollment(
        self, user_id: int, feature_id: str, enrolled: bool
    ) -> None: ...

    def list_beta_enrollments(self, user_id: int) -> set[str]: ...


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
        if "strip_name_mentions" not in cols:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN strip_name_mentions "
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
        # Sent broadcasts are deleted on send; purge any leftover marked-sent rows.
        conn.execute("DELETE FROM scheduled_broadcasts WHERE sent_at IS NOT NULL")
        sb_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(scheduled_broadcasts)")
        }
        if "recipient_ids" not in sb_cols:
            conn.execute(
                "ALTER TABLE scheduled_broadcasts "
                "ADD COLUMN recipient_ids TEXT NOT NULL DEFAULT ''"
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
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_alert_history_owner_sent
            ON alert_history(owner_id, sent_at DESC)
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
                    strip_name_mentions,
                    delay_minutes, suppress_repeat_minutes, schedule_reminder_minutes,
                    schedule_reminder_configured, ignore_keywords, use_global_ignore,
                    image_file_id, image_position, enabled, from_twitch_sync,
                    from_watch_suggest, category_watch_prefs,
                    notify_on_live, notify_on_end, notify_on_category_change,
                    delete_other_alerts, is_demo
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            conn.execute(
                "UPDATE subscriptions SET enabled = ? WHERE id = ? AND owner_id = ?",
                (new_state, sub_id, owner_id),
            )
        return bool(new_state)

    def enable_all_subscriptions(self, owner_id: int, *, demo: bool = False) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE subscriptions SET enabled = 1
                WHERE owner_id = ? AND enabled = 0 AND is_demo = ?
                """,
                (owner_id, int(bool(demo))),
            )
            return int(cur.rowcount)


    def delete_subscription(self, sub_id: int, owner_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM subscriptions WHERE id = ? AND owner_id = ?",
                (sub_id, owner_id),
            )
        return cur.rowcount > 0

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
                           twitch_user_id, stream_id, vod_id, vod_offset_seconds
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
                           twitch_user_id, stream_id, vod_id, vod_offset_seconds
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
                       COALESCE(recipient_ids, '') AS recipient_ids
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
                       COALESCE(recipient_ids, '') AS recipient_ids
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
                       COALESCE(recipient_ids, '') AS recipient_ids
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
        with self._conn() as conn:
            conn.execute("DELETE FROM scheduled_broadcasts WHERE id = ?", (broadcast_id,))

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

    def delete_synced_subscriptions_missing(
        self, owner_id: int, keep_twitch_user_ids: set[str]
    ) -> int:
        """Delete pristine (unedited) sync-origin subs not in keep set."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, twitch_user_id FROM subscriptions
                WHERE owner_id = ? AND from_twitch_sync = 1
                  AND COALESCE(sync_user_edited, 0) = 0
                """,
                (owner_id,),
            ).fetchall()
            to_delete = [
                int(r["id"])
                for r in rows
                if str(r["twitch_user_id"]) not in keep_twitch_user_ids
            ]
            for sub_id in to_delete:
                conn.execute(
                    "DELETE FROM subscriptions WHERE id = ? AND owner_id = ?",
                    (sub_id, owner_id),
                )
            return len(to_delete)

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
    ) -> int:
        ids = {str(u).strip() for u in twitch_user_ids if str(u).strip()}
        if not ids:
            return 0
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, twitch_user_id FROM subscriptions
                WHERE owner_id = ? AND is_demo = ?
                """,
                (owner_id, int(bool(is_demo))),
            ).fetchall()
            to_delete = [
                int(r["id"])
                for r in rows
                if str(r["twitch_user_id"]) in ids
            ]
            for sub_id in to_delete:
                conn.execute(
                    "DELETE FROM subscriptions WHERE id = ? AND owner_id = ?",
                    (sub_id, owner_id),
                )
            return len(to_delete)

    def is_beta_enrolled(self, user_id: int, feature_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT enrolled FROM user_beta_enrollments
                WHERE user_id = ? AND feature_id = ?
                """,
                (user_id, feature_id),
            ).fetchone()
        return row is not None and bool(row["enrolled"])

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
                    UPDATE user_beta_enrollments
                    SET enrolled = 0, opted_out_at = ?
                    WHERE user_id = ? AND feature_id = ?
                    """,
                    (now, user_id, feature_id),
                )

    def list_beta_enrollments(self, user_id: int) -> set[str]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT feature_id FROM user_beta_enrollments
                WHERE user_id = ? AND enrolled = 1
                """,
                (user_id,),
            ).fetchall()
        return {str(r["feature_id"]) for r in rows}


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
                ADD COLUMN IF NOT EXISTS strip_name_mentions
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
            cur.execute("DELETE FROM scheduled_broadcasts WHERE sent_at IS NOT NULL")
            cur.execute(
                """
                ALTER TABLE scheduled_broadcasts
                ADD COLUMN IF NOT EXISTS recipient_ids TEXT NOT NULL DEFAULT ''
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
                CREATE INDEX IF NOT EXISTS idx_alert_history_owner_sent
                ON alert_history(owner_id, sent_at DESC)
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
                    strip_name_mentions,
                    delay_minutes, suppress_repeat_minutes, schedule_reminder_minutes,
                    schedule_reminder_configured, ignore_keywords, use_global_ignore,
                    image_file_id, image_position, enabled, from_twitch_sync,
                    from_watch_suggest, category_watch_prefs,
                    notify_on_live, notify_on_end, notify_on_category_change,
                    delete_other_alerts, is_demo
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
            cur.execute(
                "UPDATE subscriptions SET enabled = %s WHERE id = %s AND owner_id = %s",
                (new_state, sub_id, owner_id),
            )
        return new_state

    def enable_all_subscriptions(self, owner_id: int, *, demo: bool = False) -> int:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                UPDATE subscriptions SET enabled = TRUE
                WHERE owner_id = %s AND enabled = FALSE AND is_demo = %s
                """,
                (owner_id, bool(demo)),
            )
            return int(cur.rowcount)

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
                           twitch_user_id, stream_id, vod_id, vod_offset_seconds
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
                           twitch_user_id, stream_id, vod_id, vod_offset_seconds
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
                       COALESCE(recipient_ids, '') AS recipient_ids
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
                       COALESCE(recipient_ids, '') AS recipient_ids
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
                       COALESCE(recipient_ids, '') AS recipient_ids
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
            cur.execute("DELETE FROM scheduled_broadcasts WHERE id = %s", (broadcast_id,))

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

    def delete_synced_subscriptions_missing(
        self, owner_id: int, keep_twitch_user_ids: set[str]
    ) -> int:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT id, twitch_user_id FROM subscriptions
                WHERE owner_id = %s AND from_twitch_sync = TRUE
                  AND COALESCE(sync_user_edited, FALSE) = FALSE
                """,
                (owner_id,),
            )
            rows = cur.fetchall()
            to_delete = [
                int(r["id"])
                for r in rows
                if str(r["twitch_user_id"]) not in keep_twitch_user_ids
            ]
            for sub_id in to_delete:
                cur.execute(
                    "DELETE FROM subscriptions WHERE id = %s AND owner_id = %s",
                    (sub_id, owner_id),
                )
            return len(to_delete)

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
    ) -> int:
        ids = {str(u).strip() for u in twitch_user_ids if str(u).strip()}
        if not ids:
            return 0
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT id, twitch_user_id FROM subscriptions
                WHERE owner_id = %s AND is_demo = %s
                """,
                (owner_id, bool(is_demo)),
            )
            rows = cur.fetchall()
            to_delete = [
                int(r["id"])
                for r in rows
                if str(r["twitch_user_id"]) in ids
            ]
            for sub_id in to_delete:
                cur.execute(
                    "DELETE FROM subscriptions WHERE id = %s AND owner_id = %s",
                    (sub_id, owner_id),
                )
            return len(to_delete)

    def is_beta_enrolled(self, user_id: int, feature_id: str) -> bool:
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
        return row is not None and bool(row["enrolled"])

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
                    UPDATE user_beta_enrollments
                    SET enrolled = FALSE, opted_out_at = NOW()
                    WHERE user_id = %s AND feature_id = %s
                    """,
                    (user_id, feature_id),
                )

    def list_beta_enrollments(self, user_id: int) -> set[str]:
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                """
                SELECT feature_id FROM user_beta_enrollments
                WHERE user_id = %s AND enrolled = TRUE
                """,
                (user_id,),
            )
            rows = cur.fetchall()
        return {str(r["feature_id"]) for r in rows}


def open_database(path: Path, database_url: str | None = None) -> Database:
    if database_url:
        return PostgresDatabase(database_url)
    return SqliteDatabase(path)
