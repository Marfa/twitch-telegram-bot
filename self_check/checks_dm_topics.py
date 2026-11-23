"""Private DM alert topics — keys, defaults off, markup helpers."""

from __future__ import annotations

from types import SimpleNamespace

from dm_topics import TOPIC_KEYS, topic_key_for_subscription
from i18n import btn, topics_intro_keyboard, topics_support_keyboard, t


def run() -> None:
    assert "live" in TOPIC_KEYS and "giveaway_watch" in TOPIC_KEYS
    for loc in ("ru", "en", "uk", "it"):
        assert btn("bot_settings", loc)
        assert t("topics_support_toggle", loc)
        assert t("topics_intro", loc)
        for key in TOPIC_KEYS:
            assert t(f"topic_name_{key}", loc)
        on = topics_support_keyboard(loc, enabled=True)
        off = topics_support_keyboard(loc, enabled=False)
        assert on.inline_keyboard[0][0].callback_data == "topics:toggle"
        assert off.inline_keyboard[0][0].text.startswith("⬜️")
        assert on.inline_keyboard[0][0].text.startswith("✅")
        intro = topics_intro_keyboard(loc)
        assert intro.inline_keyboard[0][0].callback_data == "topics:intro_toggle"
        assert intro.inline_keyboard[0][0].text.startswith("⬜️")
        assert any(
            (b.callback_data or "") == "dm_menu"
            for row in intro.inline_keyboard
            for b in row
        )

    live = SimpleNamespace(
        notify_on_live=True,
        notify_on_end=False,
        notify_on_category_change=False,
        schedule_reminder_minutes=0,
        notify_on_drops=False,
        category_watch_prefs="",
        release_watch_prefs="",
        giveaway_watch_prefs="",
    )
    assert topic_key_for_subscription(live, "live") == "live"
    upcoming = SimpleNamespace(
        notify_on_live=False,
        notify_on_end=False,
        notify_on_category_change=False,
        schedule_reminder_minutes=30,
        notify_on_drops=False,
        category_watch_prefs="",
        release_watch_prefs="",
        giveaway_watch_prefs="",
    )
    assert topic_key_for_subscription(upcoming, "schedule") == "upcoming"
    assert topic_key_for_subscription(upcoming, "schedule_cancel") == "upcoming"
