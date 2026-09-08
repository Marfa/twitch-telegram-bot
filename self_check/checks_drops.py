"""Drops alerts: type picker, parse campaigns, premium gate, active cap."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import premium as prem
from db import open_database
from db.models import (
    alert_type_from_payload,
    is_drops_sub,
    migrate_sub_fields_for_alert_type,
)
from i18n import alert_type_keyboard
from twitch import TwitchClient


def _check_drops_alert_type_keyboard_last() -> None:
    hidden = alert_type_keyboard("en")
    assert not any(
        (b.callback_data or "") == "alert_type:drops"
        for r in hidden.inline_keyboard
        for b in r
    ), "Drops must be hidden when show_drops=False"
    markup = alert_type_keyboard("en", show_drops=True)
    rows = markup.inline_keyboard
    assert any(
        (b.callback_data or "") == "alert_type:drops" for r in rows for b in r
    ), "Drops missing from alert type keyboard"
    callbacks = [b.callback_data for r in rows for b in r]
    assert callbacks[-2] == "alert_type:drops"
    assert callbacks[-1] == "alert_type:cancel"


def _check_drops_payload_and_migrate() -> None:
    assert alert_type_from_payload({"notify_on_drops": True}) == "drops"
    fields = migrate_sub_fields_for_alert_type(
        {
            "notify_on_live": True,
            "notify_on_end": False,
            "notify_on_category_change": False,
            "dest_type": "dm",
        },
        "drops",
    )
    assert fields["notify_on_drops"] is True
    assert fields["notify_on_live"] is False


def _check_drops_parse_campaign() -> None:
    raw = {
        "id": "camp-1",
        "name": "Test Drop",
        "status": "ACTIVE",
        "game": {"id": "123", "displayName": "Cool Game"},
        "startAt": "2026-01-01T00:00:00Z",
        "endAt": "2026-02-01T00:00:00Z",
        "timeBasedDrops": [
            {
                "id": "d1",
                "name": "Skin",
                "requiredMinutesWatched": 120,
                "benefitEdges": [{"benefit": {"name": "Cool Skin"}}],
            }
        ],
    }
    parsed = TwitchClient._parse_drop_campaign(raw)
    assert parsed is not None
    assert parsed["id"] == "camp-1"
    assert parsed["game_id"] == "123"
    assert parsed["drops"][0]["required_minutes"] == 120


def _check_drops_sub_helper() -> None:
    sub = SimpleNamespace(notify_on_drops=True, drops_game_id="99")
    assert is_drops_sub(sub)  # type: ignore[arg-type]
    sub2 = SimpleNamespace(notify_on_drops=True, drops_game_id="")
    assert not is_drops_sub(sub2)  # type: ignore[arg-type]


def _check_drops_premium_gate() -> None:
    live = SimpleNamespace(
        notify_on_live=True,
        notify_on_end=False,
        notify_on_category_change=False,
        schedule_reminder_configured=False,
        notify_on_drops=False,
        twitch_username="x",
    )
    assert prem.is_live_only_alert(live)
    drops = SimpleNamespace(
        notify_on_live=False,
        notify_on_end=False,
        notify_on_category_change=False,
        schedule_reminder_configured=False,
        notify_on_drops=True,
        twitch_username="Game",
    )
    assert not prem.is_live_only_alert(drops)


def _check_drops_active_cap_on_bulk() -> None:
    """Creating live subs from drops button must respect may_enable_subscription."""
    with tempfile.TemporaryDirectory() as tmp:
        db = open_database(Path(tmp) / "t.db")
        uid = 4242
        db.upsert_user(uid)
        for i in range(prem.free_active_limit()):
            db.add_subscription(
                owner_id=uid,
                twitch_username=f"streamer{i}",
                twitch_user_id=str(1000 + i),
                message_template="hi",
                dest_type="dm",
                chat_id=uid,
                thread_id=None,
                enabled=True,
                notify_on_live=True,
            )
        assert not prem.may_enable_subscription(db, uid)
        enabled = prem.may_enable_subscription(db, uid)
        db.add_subscription(
            owner_id=uid,
            twitch_username="extra",
            twitch_user_id="9999",
            message_template="hi",
            dest_type="dm",
            chat_id=uid,
            thread_id=None,
            enabled=enabled,
            notify_on_live=True,
        )
        subs = db.get_subscriptions_by_owner(uid)
        assert sum(1 for s in subs if s.enabled) == prem.free_active_limit()
        assert any(s.twitch_username == "extra" and not s.enabled for s in subs)


def run() -> None:
    _check_drops_alert_type_keyboard_last()
    _check_drops_payload_and_migrate()
    _check_drops_parse_campaign()
    _check_drops_sub_helper()
    _check_drops_premium_gate()
    _check_drops_active_cap_on_bulk()


if __name__ == "__main__":
    run()
    print("checks_drops: ok")
