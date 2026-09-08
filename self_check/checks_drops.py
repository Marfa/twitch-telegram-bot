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


def _check_drops_oauth_prompt_html() -> None:
    from i18n import t

    ru = t("drops_oauth_prompt", "ru", code="ABCD1234", url="https://www.twitch.tv/activate")
    en = t("drops_oauth_prompt", "en", code="ABCD1234", url="https://www.twitch.tv/activate")
    assert "ABCD1234" in ru and "activate" in ru
    assert "<b>" in ru and "</b>" in ru
    assert "неофициальный API" in ru
    assert "<code>" in ru
    assert "<b>" in en and "unofficial API" in en
    assert t("drops_catalog_fetch_failed", "ru") == (
        "Не удалось загрузить список Drops. Попробуйте позже."
    )
    assert "вручную" not in t("drops_catalog_empty", "ru")
    assert "manually" not in t("drops_catalog_fetch_failed", "en").lower()


def _check_drops_device_oauth_only() -> None:
    from handlers.drops import user_has_twitch_oauth
    from types import SimpleNamespace

    class _NoDrops:
        def get_drops_auth(self, _uid):
            return None

    class _WithDrops:
        def get_drops_auth(self, _uid):
            return SimpleNamespace(refresh_token="rt")

    assert not user_has_twitch_oauth(_NoDrops(), 1)  # type: ignore[arg-type]
    assert user_has_twitch_oauth(_WithDrops(), 1)  # type: ignore[arg-type]


def _check_drops_catalog_uses_access_token() -> None:
    """Fresh device-code access token must be used without an immediate refresh."""
    from handlers.drops import list_active_drop_campaigns
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    db = MagicMock()
    db.get_drops_auth.return_value = SimpleNamespace(refresh_token="rt")
    twitch = MagicMock()
    twitch.get_viewer_drop_campaigns.return_value = [
        {
            "id": "c1",
            "name": "Camp",
            "status": "ACTIVE",
            "game_id": "1",
            "game_name": "Game",
            "drops": [],
        }
    ]
    twitch.get_inventory_claimed_drops.return_value = {}
    out = list_active_drop_campaigns(db, twitch, 7, access_token="fresh-at")
    assert out and out[0]["id"] == "c1"
    twitch.get_viewer_drop_campaigns.assert_called_once_with("fresh-at")
    twitch.refresh_drops_gql_token.assert_not_called()


def _check_drops_digest_and_tags() -> None:
    from i18n import drops_catalog_keyboard, t
    from twitch import TwitchClient

    assert TwitchClient.stream_has_drops_tag({"tags": ["Drops Enabled"]})
    assert TwitchClient.stream_has_drops_tag({"tags": ["Drops Включены"]})
    assert not TwitchClient.stream_has_drops_tag({"tags": ["English"]})

    kb = drops_catalog_keyboard(
        "ru",
        [{"name": "C", "game_name": "G", "id": "1", "claimed": True}],
        digest_enabled=True,
    )
    labels = [b.text for r in kb.inline_keyboard for b in r]
    assert any("Подписаться на новые Drops" in (x or "") for x in labels)
    assert any((x or "").startswith("✅") for x in labels)
    assert any("получено" in (x or "") for x in labels)
    assert "Получать оповещения" in t("drops_get_alerts_btn", "ru")
    assert "Вы получили Drops" in t("drops_claim_alert_body", "ru", name="X")


def _check_drops_digest_db() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = open_database(Path(tmp) / "t.db")
        db.upsert_user(7)
        db.upsert_drops_auth(
            7, twitch_user_id="1", twitch_login="u", refresh_token="rt"
        )
        assert 7 not in db.list_drops_digest_owner_ids()
        db.set_drops_digest_enabled(7, True)
        auth = db.get_drops_auth(7)
        assert auth is not None and auth.digest_enabled
        assert 7 in db.list_drops_digest_owner_ids()
        db.mark_drop_claim_seen(7, "d1", first_seen_at="2026-01-01T00:00:00Z")
        assert db.has_seen_drop_claim(7, "d1")
        db.mark_drop_stream_seen(
            7, 1, "s1", first_seen_at="2026-01-01T00:00:00Z"
        )
        assert db.has_seen_drop_stream(7, 1, "s1")


def run() -> None:
    _check_drops_alert_type_keyboard_last()
    _check_drops_payload_and_migrate()
    _check_drops_parse_campaign()
    _check_drops_sub_helper()
    _check_drops_premium_gate()
    _check_drops_active_cap_on_bulk()
    _check_drops_oauth_prompt_html()
    _check_drops_device_oauth_only()
    _check_drops_catalog_uses_access_token()
    _check_drops_digest_and_tags()
    _check_drops_digest_db()


if __name__ == "__main__":
    run()
    print("checks_drops: ok")
