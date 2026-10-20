"""Characterization checks for alert repeats + overlap helpers."""

from __future__ import annotations

from types import SimpleNamespace

from alert_repeats import (
    BETA_FEATURE_ID,
    format_repeat_notify_minutes,
    is_last_alert_in_chat,
    parse_repeat_notify_minutes,
    repeat_notify_job_name,
    sub_tracks_last_message_for_overlap,
)


def check_alert_repeats() -> None:
    assert BETA_FEATURE_ID == "alert-repeats-overlap"
    assert parse_repeat_notify_minutes("0") == []
    assert parse_repeat_notify_minutes("") == []
    assert parse_repeat_notify_minutes("5, 15, 5, 30") == [5, 15, 30]
    assert parse_repeat_notify_minutes("abc,0,-1,2000") == []
    assert format_repeat_notify_minutes([30, 5]) == "5,30"
    assert repeat_notify_job_name(9, 15) == "repeat_9_15"

    # Interval semantics: each tick re-arms the same minute value while live.
    from pathlib import Path

    notif_src = Path("handlers/notifications.py").read_text(encoding="utf-8")
    assert "def schedule_repeat_interval_job" in notif_src
    assert "schedule_repeat_interval_job" in notif_src.split(
        "async def _send_repeat_notification"
    )[1].split("async def _send_delayed_end_notification")[0]

    sub = SimpleNamespace(
        dest_type="channel",
        delete_previous=False,
        block_overlap=True,
        block_repeat_when_last=False,
        repeat_notify_minutes="",
    )
    assert sub_tracks_last_message_for_overlap(sub) is True
    sub.block_overlap = False
    sub.repeat_notify_minutes = "10"
    assert sub_tracks_last_message_for_overlap(sub) is True
    sub.repeat_notify_minutes = ""
    assert sub_tracks_last_message_for_overlap(sub) is False
    sub.dest_type = "dm"
    sub.block_overlap = True
    assert sub_tracks_last_message_for_overlap(sub) is False

    class _Db:
        def __init__(self, rows):
            self._rows = rows

        def get_subscriptions_by_owner(self, owner_id):
            return [r for r in self._rows if r.owner_id == owner_id]

    a = SimpleNamespace(
        id=1,
        owner_id=7,
        chat_id=-100,
        thread_id=None,
        dest_type="channel",
        last_message_id=10,
    )
    b = SimpleNamespace(
        id=2,
        owner_id=7,
        chat_id=-100,
        thread_id=None,
        dest_type="channel",
        last_message_id=20,
    )
    db = _Db([a, b])
    assert is_last_alert_in_chat(db, a) is False
    assert is_last_alert_in_chat(db, b) is True
