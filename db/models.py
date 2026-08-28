from __future__ import annotations

import json
import random
import secrets
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

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
    attach_chat_button: bool
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


@dataclass(frozen=True)
class DeletedSubscriptionCartItem:
    cart_id: int
    deleted_at: str
    twitch_username: str
    twitch_user_id: str
    alert_type: str = "live"


@dataclass(frozen=True)
class PremiumChannel:
    twitch_user_id: str
    twitch_login: str
    display_name: str
    owner_telegram_id: int
    charge_id: str
    paid_at: str


def alert_type_from_payload(payload: dict[str, Any]) -> str:
    if payload.get("notify_on_category_change"):
        return "category"
    if payload.get("notify_on_end"):
        return "end"
    reminder = int(payload.get("schedule_reminder_minutes") or 0)
    if reminder > 0 and not payload.get("notify_on_live"):
        return "upcoming"
    return "live"


def _cart_item_from_row(row_id: int, deleted_at: object, subscription_json: object) -> DeletedSubscriptionCartItem:
    try:
        payload = json.loads(subscription_json or "{}")
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return DeletedSubscriptionCartItem(
        cart_id=int(row_id),
        deleted_at=str(deleted_at),
        twitch_username=str(payload.get("twitch_username") or ""),
        twitch_user_id=str(payload.get("twitch_user_id") or ""),
        alert_type=alert_type_from_payload(payload),
    )


def is_category_watch_sub(sub: Subscription) -> bool:
    return bool((sub.category_watch_prefs or "").strip())


def _subscription_cart_snapshot(sub: Subscription) -> dict[str, Any]:
    """Snapshot payload for restore.

    Kept intentionally limited to what `add_subscription()` can materialize.
    """

    return {
        "twitch_username": sub.twitch_username,
        "twitch_user_id": sub.twitch_user_id,
        "message_template": sub.message_template,
        "dest_type": sub.dest_type,
        "chat_id": sub.chat_id,
        "thread_id": sub.thread_id,
        "delete_previous": bool(sub.delete_previous),
        "notify_delete_fail": bool(sub.notify_delete_fail),
        "disable_link_preview": bool(sub.disable_link_preview),
        "strip_name_mentions": bool(sub.strip_name_mentions),
        "attach_chat_button": bool(sub.attach_chat_button),
        "delay_minutes": int(sub.delay_minutes),
        "suppress_repeat_minutes": int(sub.suppress_repeat_minutes),
        "schedule_reminder_minutes": int(sub.schedule_reminder_minutes),
        "schedule_reminder_configured": bool(sub.schedule_reminder_configured),
        "ignore_keywords": sub.ignore_keywords or "",
        "use_global_ignore": bool(sub.use_global_ignore),
        "image_file_id": sub.image_file_id,
        "image_position": sub.image_position or "",
        "enabled": bool(sub.enabled),
        "from_twitch_sync": bool(sub.from_twitch_sync),
        "from_watch_suggest": bool(sub.from_watch_suggest),
        "category_watch_prefs": sub.category_watch_prefs or "",
        "notify_on_live": bool(sub.notify_on_live),
        "notify_on_end": bool(sub.notify_on_end),
        "notify_on_category_change": bool(sub.notify_on_category_change),
        "delete_other_alerts": bool(sub.delete_other_alerts),
        "is_demo": bool(sub.is_demo),
    }


@dataclass
class ScheduledBroadcast:
    id: int
    msg_type: str
    text: str
    scheduled_at: str
    created_by: int
    recipient_ids: str = ""
    sent_utc_offsets: str = ""
    sent_count: int = 0
    sent_at: str | None = None


BROADCAST_RETENTION_DAYS = 30


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


@dataclass
class ChatAuth:
    owner_id: int
    twitch_user_id: str
    twitch_login: str
    refresh_token: str


def _scheduled_broadcast_from_row(row: Any) -> ScheduledBroadcast:
    scheduled_at = row["scheduled_at"]
    if scheduled_at is not None and not isinstance(scheduled_at, str):
        scheduled_at = scheduled_at.isoformat()
    try:
        recipient_ids = str(row["recipient_ids"] or "")
    except (KeyError, IndexError, TypeError):
        recipient_ids = ""
    try:
        sent_utc_offsets = str(row["sent_utc_offsets"] or "")
    except (KeyError, IndexError, TypeError):
        sent_utc_offsets = ""
    try:
        sent_count = int(row["sent_count"] or 0)
    except (KeyError, IndexError, TypeError):
        sent_count = 0
    try:
        sent_at = row["sent_at"]
        if sent_at is not None and not isinstance(sent_at, str):
            sent_at = sent_at.isoformat()
        sent_at = str(sent_at) if sent_at else None
    except (KeyError, IndexError, TypeError):
        sent_at = None
    return ScheduledBroadcast(
        id=int(row["id"]),
        msg_type=str(row["msg_type"]),
        text=str(row["text"]),
        scheduled_at=str(scheduled_at),
        created_by=int(row["created_by"]),
        recipient_ids=recipient_ids,
        sent_utc_offsets=sent_utc_offsets,
        sent_count=sent_count,
        sent_at=sent_at,
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


def _row_to_chat_auth(row: Any) -> ChatAuth:
    return ChatAuth(
        owner_id=int(row["owner_id"]),
        twitch_user_id=str(row["twitch_user_id"] or ""),
        twitch_login=str(row["twitch_login"] or ""),
        refresh_token=str(row["refresh_token"] or ""),
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
        attach_chat_button=bool(row["attach_chat_button"])
        if "attach_chat_button" in keys
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
