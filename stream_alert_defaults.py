"""Per-user default settings for stream alerts (bind / lock / apply)."""

from __future__ import annotations

import json
from typing import Any

# Subscription update kwargs stored in users.stream_alert_defaults JSON.
STREAM_ALERT_DEFAULT_KEYS: tuple[str, ...] = (
    "message_template",
    "image_file_id",
    "image_position",
    "strip_name_mentions",
    "ignore_keywords",
    "use_global_ignore",
    "category_filter",
    "delay_minutes",
    "suppress_repeat_minutes",
    "delete_previous",
    "notify_delete_fail",
    "delete_other_alerts",
    "pin_message",
    "custom_buttons",
    "button_style",
    "attach_chat_button",
    "attach_live_remind_button",
    "top_donations",
    "top_donations_template",
    "disable_link_preview",
    "schedule_reminder_minutes",
    "schedule_reminder_configured",
    "notify_on_schedule_cancel",
    "schedule_cancel_template",
    "multistream_channels",
)

# edit_f / edit_set field names that stay locked while use_stream_defaults.
LOCKED_EDIT_FIELDS: frozenset[str] = frozenset(
    {
        "template",
        "image",
        "image_del",
        "strip",
        "ignore_keywords",
        "category_filter",
        "delay",
        "repeat",
        "delete_old",
        "delete_other",
        "pin_message",
        "custom_buttons",
        "chat_button",
        "live_remind",
        "top_donations",
        "preview",
        "button_style",
        "button_style_back",
        "sched_remind",
        "schedule_cancel",
        "multistream",
    }
)

# advopt toggle ids locked while apply_defaults is on (not apply_defaults itself).
LOCKED_ADVOPT_TOGGLES: frozenset[str] = frozenset(
    {
        "image",
        "strip",
        "ignore",
        "categories",
        "delay",
        "repeat",
        "delete",
        "pin",
        "buttons",
        "chat",
        "live_remind",
        "top_donations",
        "preview",
        "schedule_cancel",
        "multistream",
    }
)

_BOOL_KEYS = frozenset(
    {
        "strip_name_mentions",
        "use_global_ignore",
        "delete_previous",
        "notify_delete_fail",
        "delete_other_alerts",
        "pin_message",
        "attach_chat_button",
        "attach_live_remind_button",
        "top_donations",
        "disable_link_preview",
        "schedule_reminder_configured",
        "notify_on_schedule_cancel",
    }
)
_INT_KEYS = frozenset(
    {
        "delay_minutes",
        "suppress_repeat_minutes",
        "schedule_reminder_minutes",
    }
)


def empty_stream_alert_defaults() -> dict[str, Any]:
    return {
        "message_template": "",
        "image_file_id": None,
        "image_position": "",
        "strip_name_mentions": False,
        "ignore_keywords": "",
        "use_global_ignore": False,
        "category_filter": "",
        "delay_minutes": 0,
        "suppress_repeat_minutes": 0,
        "delete_previous": False,
        "notify_delete_fail": False,
        "delete_other_alerts": False,
        "pin_message": False,
        "custom_buttons": "[]",
        "button_style": "",
        "attach_chat_button": False,
        "attach_live_remind_button": False,
        "top_donations": False,
        "top_donations_template": "",
        "disable_link_preview": False,
        "schedule_reminder_minutes": 0,
        "schedule_reminder_configured": False,
        "notify_on_schedule_cancel": False,
        "schedule_cancel_template": "",
        "multistream_channels": "[]",
    }


def normalize_stream_alert_defaults(raw: dict[str, Any] | None) -> dict[str, Any]:
    base = empty_stream_alert_defaults()
    if not isinstance(raw, dict):
        return base
    for key in STREAM_ALERT_DEFAULT_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if key in _BOOL_KEYS:
            base[key] = bool(value)
        elif key in _INT_KEYS:
            try:
                base[key] = max(0, int(value))
            except (TypeError, ValueError):
                base[key] = 0
        elif key == "image_file_id":
            text = str(value).strip() if value else ""
            base[key] = text or None
        else:
            base[key] = str(value or "")
    if not base.get("image_file_id"):
        base["image_position"] = ""
    return base


def parse_stream_alert_defaults(raw: str | None) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        return empty_stream_alert_defaults()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return empty_stream_alert_defaults()
    if not isinstance(data, dict):
        return empty_stream_alert_defaults()
    return normalize_stream_alert_defaults(data)


def dump_stream_alert_defaults(defaults: dict[str, Any] | None) -> str:
    payload = normalize_stream_alert_defaults(defaults)
    # Omit null image for stable compact JSON.
    out = {k: payload[k] for k in STREAM_ALERT_DEFAULT_KEYS}
    if out.get("image_file_id") is None:
        out["image_file_id"] = ""
    return json.dumps(out, ensure_ascii=False, sort_keys=True)


def defaults_from_subscription(sub: Any) -> dict[str, Any]:
    """Build a defaults payload from a Subscription-like object."""
    raw = {key: getattr(sub, key, empty_stream_alert_defaults()[key]) for key in STREAM_ALERT_DEFAULT_KEYS}
    return normalize_stream_alert_defaults(raw)


def filter_defaults_for_alert_type(
    defaults: dict[str, Any], alert_type: str
) -> dict[str, Any]:
    """Copy defaults, clearing fields that do not apply to alert_type."""
    out = normalize_stream_alert_defaults(defaults)
    kind = (alert_type or "live").strip().lower()
    if kind != "live":
        out["multistream_channels"] = "[]"
    if kind != "upcoming":
        out["attach_live_remind_button"] = False
        out["schedule_reminder_minutes"] = 0
        out["schedule_reminder_configured"] = False
        out["notify_on_schedule_cancel"] = False
        out["schedule_cancel_template"] = ""
    if kind != "end":
        out["top_donations"] = False
        out["top_donations_template"] = ""
    if kind == "upcoming":
        out["delay_minutes"] = 0
        out["suppress_repeat_minutes"] = 0
        out["category_filter"] = ""
    if kind == "end":
        out["category_filter"] = ""
        out["delay_minutes"] = 0
    return out


def apply_defaults_to_user_data(
    user_data: dict[str, Any], defaults: dict[str, Any], *, alert_type: str
) -> None:
    """Write filtered defaults into wizard user_data + matching adv_want_* flags."""
    fields = filter_defaults_for_alert_type(defaults, alert_type)
    for key, value in fields.items():
        user_data[key] = value
    user_data["adv_want_image"] = bool(fields.get("image_file_id"))
    user_data["adv_want_strip"] = bool(fields.get("strip_name_mentions"))
    user_data["adv_want_ignore"] = bool(
        (fields.get("ignore_keywords") or "").strip() or fields.get("use_global_ignore")
    )
    user_data["adv_want_categories"] = bool((fields.get("category_filter") or "").strip())
    user_data["adv_want_delay"] = int(fields.get("delay_minutes") or 0) > 0
    user_data["adv_want_repeat"] = int(fields.get("suppress_repeat_minutes") or 0) > 0
    user_data["adv_want_delete"] = bool(fields.get("delete_previous"))
    user_data["adv_want_pin"] = bool(fields.get("pin_message"))
    from custom_buttons import parse_custom_buttons

    user_data["adv_want_buttons"] = bool(
        parse_custom_buttons(fields.get("custom_buttons"))
    )
    user_data["adv_want_chat"] = bool(fields.get("attach_chat_button"))
    user_data["adv_want_live_remind"] = bool(fields.get("attach_live_remind_button"))
    user_data["adv_want_top_donations"] = bool(fields.get("top_donations"))
    user_data["adv_want_preview"] = not bool(fields.get("disable_link_preview"))
    user_data["adv_want_schedule_cancel"] = bool(
        fields.get("notify_on_schedule_cancel")
    ) and bool((fields.get("schedule_cancel_template") or "").strip())
    import multistream as ms

    user_data["adv_want_multistream"] = bool(
        ms.parse_multistream_channels(fields.get("multistream_channels"))
    )
    user_data["custom_buttons_list"] = parse_custom_buttons(
        fields.get("custom_buttons")
    )
    user_data["multistream_list"] = ms.parse_multistream_channels(
        fields.get("multistream_channels")
    )
    user_data["use_stream_defaults"] = True


def subscription_update_from_defaults(
    defaults: dict[str, Any], *, alert_type: str
) -> dict[str, Any]:
    fields = filter_defaults_for_alert_type(defaults, alert_type)
    fields["use_stream_defaults"] = True
    return fields


def draft_as_subscription(draft: dict[str, Any], *, owner_id: int = 0) -> Any:
    """Subscription-shaped object for reusing edit prompts against a draft."""
    from types import SimpleNamespace

    fields = normalize_stream_alert_defaults(draft)
    return SimpleNamespace(
        id=0,
        owner_id=owner_id,
        twitch_username="—",
        twitch_user_id="",
        message_template=str(fields.get("message_template") or ""),
        dest_type="channel",
        chat_id=0,
        thread_id=None,
        enabled=True,
        delete_previous=bool(fields.get("delete_previous")),
        notify_delete_fail=bool(fields.get("notify_delete_fail")),
        disable_link_preview=bool(fields.get("disable_link_preview")),
        strip_name_mentions=bool(fields.get("strip_name_mentions")),
        attach_chat_button=bool(fields.get("attach_chat_button")),
        attach_live_remind_button=bool(fields.get("attach_live_remind_button")),
        custom_buttons=str(fields.get("custom_buttons") or "[]"),
        multistream_channels=str(fields.get("multistream_channels") or "[]"),
        button_style=str(fields.get("button_style") or ""),
        delay_minutes=int(fields.get("delay_minutes") or 0),
        suppress_repeat_minutes=int(fields.get("suppress_repeat_minutes") or 0),
        schedule_reminder_minutes=int(fields.get("schedule_reminder_minutes") or 0),
        schedule_reminder_configured=bool(fields.get("schedule_reminder_configured")),
        notify_on_live=True,
        notify_on_end=False,
        notify_on_category_change=False,
        notify_on_drops=False,
        drops_game_id="",
        ignore_keywords=str(fields.get("ignore_keywords") or ""),
        use_global_ignore=bool(fields.get("use_global_ignore")),
        category_filter=str(fields.get("category_filter") or ""),
        image_file_id=fields.get("image_file_id") or None,
        image_position=str(fields.get("image_position") or ""),
        delete_other_alerts=bool(fields.get("delete_other_alerts")),
        pin_message=bool(fields.get("pin_message")),
        top_donations=bool(fields.get("top_donations")),
        top_donations_template=str(fields.get("top_donations_template") or ""),
        notify_on_schedule_cancel=bool(fields.get("notify_on_schedule_cancel")),
        schedule_cancel_template=str(fields.get("schedule_cancel_template") or ""),
        schedule_cancel_notified_days="[]",
        use_stream_defaults=False,
        category_watch_prefs="",
        release_watch_prefs="",
        giveaway_watch_prefs="",
        from_twitch_sync=False,
        is_demo=False,
    )


def ensure_defaults_draft(user_data: dict[str, Any], db: Any, user_id: int) -> dict[str, Any]:
    draft = user_data.get("stream_defaults_draft")
    if isinstance(draft, dict):
        return normalize_stream_alert_defaults(draft)
    loaded = db.get_stream_alert_defaults(user_id)
    normalized = normalize_stream_alert_defaults(loaded)
    user_data["stream_defaults_draft"] = normalized
    user_data["editing_defaults"] = True
    return normalized


def update_defaults_draft(user_data: dict[str, Any], **fields: Any) -> dict[str, Any]:
    draft = normalize_stream_alert_defaults(user_data.get("stream_defaults_draft"))
    for key, value in fields.items():
        if key in STREAM_ALERT_DEFAULT_KEYS:
            draft[key] = value
    draft = normalize_stream_alert_defaults(draft)
    user_data["stream_defaults_draft"] = draft
    user_data["editing_defaults"] = True
    return draft


def keep_defaults_editor_state(user_data: dict[str, Any]) -> dict[str, Any]:
    """Preserve draft keys across user_data.clear()."""
    return {
        k: user_data[k]
        for k in ("editing_defaults", "stream_defaults_draft")
        if k in user_data
    }
