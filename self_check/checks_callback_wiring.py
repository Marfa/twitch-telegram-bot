"""Level C: inline callback_data and menu Reply buttons wired in bot.py."""
from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from i18n import (
    admin_other_audience_keyboard,
    admin_type_keyboard,
    alert_type_keyboard,
    channel_dup_keyboard,
    chat_button_keyboard,
    delay_keyboard,
    delete_fail_notify_keyboard,
    delete_old_keyboard,
    delete_sibling_keyboard,
    dest_keyboard,
    edit_bool_keyboard,
    edit_options_keyboard,
    ignored_words_keyboard,
    ignore_keywords_keyboard,
    import_mode_keyboard,
    language_keyboard,
    link_preview_keyboard,
    lucky_preview_keyboard,
    premium_actions_keyboard,
    premium_gate_keyboard,
    premium_owned_keyboard,
    schedule_calendar_days_keyboard,
    schedule_keyboard,
    schedule_month_keyboard,
    stream_schedule_confirm_keyboard,
    stream_schedule_day_keyboard,
    stream_schedule_duration_keyboard,
    stream_schedule_fix_day_keyboard,
    stream_schedule_mode_keyboard,
    stream_schedule_more_keyboard,
    stream_schedule_publish_keyboard,
    sync_settings_keyboard,
    sys_notifications_keyboard,
    template_strip_keyboard,
    watch_cats_nav_keyboard,
    whisper_alerts_keyboard,
    advanced_mode_keyboard,
)
from telegram import InlineKeyboardMarkup

_ROOT = Path(__file__).resolve().parents[1]
_SAMPLE_SUB = 42
_SAMPLE_WD = 7

# Same keys as i18n.all_menu_buttons() source tuple.
_MENU_BTN_KEYS = (
    "new",
    "import_twitch",
    "manage",
    "list",
    "edit",
    "delete",
    "cart",
    "pause_notifications",
    "feedback",
    "create_schedule",
    "alert_history",
    "other",
    "settings",
    "language",
    "admin",
    "demo",
    "broadcast",
    "broadcast_new",
    "scheduled_broadcasts",
    "sent_broadcasts",
    "stats",
    "back",
    "sys_notifications",
    "ignored_words",
    "whisper_alerts",
    "advanced_mode",
    "beta_mode",
    "sync_subs",
    "premium",
    "partner",
    "partner_stats",
    "partner_link",
    "partner_withdraw",
    "partner_withdrawals",
    "back_settings",
    "admin_withdrawals",
    "watch",
    "chat",
)

_WIZARD_BTN_KEYS = ("wizard_back", "wizard_cancel")

# Hand-picked samples for dynamic callback_data templates.
_EXTRA_CALLBACKS: tuple[tuple[str, str], ...] = (
    ("edit_pick", f"edit:{_SAMPLE_SUB}"),
    ("edit_type", "edit_type:live"),
    ("list_type", "list_type:live"),
    ("list_page", "list_page:1"),
    ("list_page_noop", "list_page:noop"),
    ("edit_page", "edit_page:1"),
    ("edit_page_noop", "edit_page:noop"),
    ("delete_page", "delete_page:1"),
    ("delete_page_noop", "delete_page:noop"),
    ("share_show", f"share_show:{_SAMPLE_SUB}"),
    ("share_accept", "share_accept:abc123XYZ_-"),
    ("share_decline", "share_decline"),
    ("list_del", f"list_del:{_SAMPLE_SUB}"),
    ("list_del_ok", f"list_del_ok:{_SAMPLE_SUB}"),
    ("list_del_no", f"list_del_no:{_SAMPLE_SUB}"),
    ("toggle", f"toggle:{_SAMPLE_SUB}"),
    ("enable_all", "enable_all"),
    ("delete_go", "delete_go"),
    ("delete_all", "delete_all"),
    ("delete_all_yes", "delete_all:yes"),
    ("delete_all_no", "delete_all:no"),
    ("delete_sel", f"delete_sel:{_SAMPLE_SUB}"),
    ("watch_again", "watch:again"),
    ("watch_change", "watch:change"),
    ("watch_cat", "watch_cat:lucky"),
    ("watch_nav", "watch_nav:back"),
    ("alert_history_page", "alert_history:page:1"),
    ("alert_history_more", "alert_history:more"),
    ("alert_history_menu", "alert_history:menu"),
    ("sched_apply", "sched:date:0"),
    ("sched_calendar", "sched:calendar"),
    ("sched_month", "sched:month:2026-09"),
    ("sb_sched_calendar", "sb_sched:calendar"),
    ("sb_edit", f"sb_edit:{_SAMPLE_SUB}"),
    ("bcf_up", f"bcf:up:{_SAMPLE_SUB}"),
    ("bcf_down", f"bcf:down:{_SAMPLE_SUB}"),
    ("premium_month", "premium:month"),
    ("premium_gate", "premium_gate:skip"),
    ("beta_toggle", "beta:toggle:demo_feat"),
    ("ref_wd", f"ref_wd:paid:{_SAMPLE_WD}"),
    ("welcome_del", f"welcome_del:{_SAMPLE_SUB}"),
    ("delivery_fail", f"delivery_fail_del:{_SAMPLE_SUB}"),
    ("edit_set", f"edit_set:{_SAMPLE_SUB}:preview:1"),
    ("edit_f", f"edit_f:{_SAMPLE_SUB}:template"),
    ("edit_change_type", f"edit_f:{_SAMPLE_SUB}:change_type"),
    ("edit_copy", f"edit_f:{_SAMPLE_SUB}:copy"),
    ("edit_copy_change", f"edit_f:{_SAMPLE_SUB}:copy_change"),
    ("edit_type_pick", f"edit_type_pick:change:{_SAMPLE_SUB}:category"),
    ("edit_type_pick_cancel", f"edit_type_pick_cancel:change:{_SAMPLE_SUB}"),
    ("stream_sched_mode", "stream_sched:mode:week"),
    ("stream_sched_more", "stream_sched:more:0"),
    ("stream_sched_duration", "stream_sched:duration:2"),
    ("stream_sched_fix", "stream_sched:fix_day:0"),
    ("lang", "lang:ru"),
    ("lang_cancel", "lang:cancel"),
    ("import_mode", "import_mode:once"),
    ("import_oauth_cancel", "import_oauth:cancel"),
    ("sched_save_token", "sched_save_token:1"),
    ("image_ask_game_cover", "image_ask:game_cover"),
)


def _bot_source() -> str:
    return (_ROOT / "bot.py").read_text(encoding="utf-8")


def _callback_patterns(src: str) -> list[re.Pattern[str]]:
    raw = re.findall(r"pattern=r\"([^\"]+)\"", src)
    return [re.compile(p) for p in raw]


def _callbacks_from_markup(markup: InlineKeyboardMarkup | None) -> list[str]:
    if markup is None:
        return []
    out: list[str] = []
    for row in markup.inline_keyboard:
        for key in row:
            if key.callback_data:
                out.append(key.callback_data)
    return out


def _keyboard_samples() -> list[tuple[str, InlineKeyboardMarkup]]:
    loc = "ru"
    today = date(2026, 8, 26)
    fix_dates = [today, today + timedelta(days=1)]
    samples: list[tuple[str, Any, dict[str, Any]]] = [
        ("alert_type", alert_type_keyboard, {"lang": loc}),
        ("premium_gate_first", premium_gate_keyboard, {"lang": loc, "first_step": True}),
        ("premium_gate_later", premium_gate_keyboard, {"lang": loc, "first_step": False}),
        ("dest", dest_keyboard, {"lang": loc}),
        ("delete_old", delete_old_keyboard, {"lang": loc}),
        ("delete_fail", delete_fail_notify_keyboard, {"lang": loc}),
        ("delete_sibling", delete_sibling_keyboard, {"lang": loc}),
        ("link_preview", link_preview_keyboard, {"lang": loc}),
        ("chat_button", chat_button_keyboard, {"lang": loc}),
        ("delay", delay_keyboard, {"lang": loc}),
        ("import_mode", import_mode_keyboard, {"lang": loc}),
        ("sync_settings", sync_settings_keyboard, {"lang": loc}),
        ("language", language_keyboard, {"lang": loc}),
        ("admin_type", admin_type_keyboard, {"lang": loc}),
        ("admin_audience", admin_other_audience_keyboard, {"lang": loc}),
        (
            "schedule",
            schedule_keyboard,
            {"lang": loc, "schedule": {"date_page": 0, "date_offset": 0}},
        ),
        ("schedule_months", schedule_month_keyboard, {"lang": loc}),
        (
            "schedule_days",
            schedule_calendar_days_keyboard,
            {
                "lang": loc,
                "year": 2026,
                "month": 8,
                "schedule": {"date_offset": 0},
            },
        ),
        (
            "sb_schedule",
            schedule_keyboard,
            {
                "lang": loc,
                "schedule": {"date_page": 0, "date_offset": 0},
                "prefix": "sb_sched",
                "show_send_now": False,
            },
        ),
        ("stream_sched_confirm", stream_schedule_confirm_keyboard, {"lang": loc}),
        ("stream_sched_mode", stream_schedule_mode_keyboard, {"lang": loc}),
        ("stream_sched_publish", stream_schedule_publish_keyboard, {"lang": loc}),
        ("stream_sched_duration", stream_schedule_duration_keyboard, {"lang": loc}),
        ("stream_sched_day", stream_schedule_day_keyboard, {"lang": loc, "show_finish": True}),
        ("stream_sched_more", stream_schedule_more_keyboard, {"lang": loc}),
        (
            "stream_sched_fix_day",
            stream_schedule_fix_day_keyboard,
            {"lang": loc, "dates": fix_dates},
        ),
        ("watch_cats_nav", watch_cats_nav_keyboard, {"lang": loc, "has_cats": False}),
        ("whisper_alerts", whisper_alerts_keyboard, {"lang": loc, "enabled": False}),
        ("advanced_mode", advanced_mode_keyboard, {"lang": loc, "enabled": False}),
        ("ignored_words", ignored_words_keyboard, {"lang": loc, "has_words": False}),
        (
            "ignore_keywords",
            ignore_keywords_keyboard,
            {"lang": loc, "show_back": True, "show_cancel": True},
        ),
        (
            "template_strip",
            template_strip_keyboard,
            {"lang": loc, "show_back": True, "show_cancel": True},
        ),
        ("lucky_preview", lucky_preview_keyboard, {"lang": loc}),
        ("channel_dup", channel_dup_keyboard, {"lang": loc, "sub_id": _SAMPLE_SUB}),
        (
            "sys_notifications",
            sys_notifications_keyboard,
            {
                "lang": loc,
                "updates_enabled": True,
                "availability_enabled": False,
                "other_enabled": True,
                "sync_enabled": False,
            },
        ),
        (
            "premium_actions",
            premium_actions_keyboard,
            {"lang": loc, "user_id": 1},
        ),
        (
            "premium_owned",
            premium_owned_keyboard,
            {"lang": loc, "stars_cancelable": True, "feature_ids": ["alert_types"]},
        ),
        (
            "edit_options",
            edit_options_keyboard,
            {
                "sub_id": _SAMPLE_SUB,
                "lang": loc,
                "show_advanced": True,
                "schedule_reminder_configured": True,
                "notify_on_category_change": True,
            },
        ),
        ("edit_bool_preview", edit_bool_keyboard, {"sub_id": _SAMPLE_SUB, "field": "preview", "lang": loc}),
    ]
    out: list[tuple[str, InlineKeyboardMarkup]] = []
    for name, fn, kwargs in samples:
        out.append((name, fn(**kwargs)))
    return out


def _matches(patterns: list[re.Pattern[str]], callback: str) -> bool:
    return any(p.search(callback) for p in patterns)


def check_menu_button_handlers() -> None:
    src = _bot_source()
    missing: list[str] = []
    for key in _MENU_BTN_KEYS:
        if key == "beta_mode":
            if "_btn_filter(\"beta_mode\")" not in src:
                missing.append(key)
            continue
        if f'_btn_filter("{key}")' not in src:
            missing.append(key)
    for key in _WIZARD_BTN_KEYS:
        if f'_btn_filter("{key}")' not in src:
            missing.append(key)
    assert not missing, f"menu buttons without MessageHandler in bot.py: {missing}"


def check_callback_handlers() -> None:
    patterns = _callback_patterns(_bot_source())
    assert patterns, "no CallbackQueryHandler patterns found in bot.py"

    unmatched: list[str] = []
    seen: set[str] = set()

    for name, markup in _keyboard_samples():
        for cb in _callbacks_from_markup(markup):
            if cb in seen:
                continue
            seen.add(cb)
            if not _matches(patterns, cb):
                unmatched.append(f"{name}:{cb}")

    for name, cb in _EXTRA_CALLBACKS:
        if cb in seen:
            continue
        seen.add(cb)
        if not _matches(patterns, cb):
            unmatched.append(f"{name}:{cb}")

    assert not unmatched, (
        "callback_data with no matching CallbackQueryHandler pattern in bot.py:\n"
        + "\n".join(f"  - {line}" for line in sorted(unmatched))
    )


def check_callback_wiring() -> None:
    check_menu_button_handlers()
    check_callback_handlers()
