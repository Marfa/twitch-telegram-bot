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
    hidden_cbs = [b.callback_data for r in hidden.inline_keyboard for b in r]
    assert hidden_cbs[-2] == "alert_type:game"
    assert hidden_cbs[-1] == "alert_type:cancel"
    markup = alert_type_keyboard("en", show_drops=True)
    rows = markup.inline_keyboard
    assert any(
        (b.callback_data or "") == "alert_type:drops" for r in rows for b in r
    ), "Drops missing from alert type keyboard"
    callbacks = [b.callback_data for r in rows for b in r]
    assert callbacks[-3] == "alert_type:drops"
    assert callbacks[-2] == "alert_type:game"
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
        "Не удалось загрузить список Drops. Попробуйте позже или перепривяжите Twitch."
    )
    assert "re-link" in t("drops_catalog_fetch_failed", "en").lower()
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
    twitch.get_viewer_drop_campaigns.assert_called_once()
    assert twitch.get_viewer_drop_campaigns.call_args.args[0] == "fresh-at"
    assert twitch.get_viewer_drop_campaigns.call_args.kwargs.get("device_id")
    twitch.refresh_drops_gql_token.assert_not_called()


def _check_drops_digest_and_tags() -> None:
    from i18n import drops_catalog_keyboard, t
    from twitch import TwitchClient

    assert TwitchClient.stream_has_drops_tag({"tags": ["Drops Enabled"]})
    assert TwitchClient.stream_has_drops_tag({"tags": ["Drops Включены"]})
    assert not TwitchClient.stream_has_drops_tag({"tags": ["English"]})
    assert TwitchClient.matched_drops_tag({"tags": ["Drops Enabled"]}) == "Drops Enabled"
    assert (
        TwitchClient.matched_drops_tag({"tags": ["Drops Включены"]})
        == "Drops Включены"
    )

    class _Tw(TwitchClient):
        def __init__(self) -> None:
            pass

        def get_streams_by_game(self, game_id, *, language=None, first=100):  # type: ignore[override]
            del game_id, language, first
            return [
                {
                    "id": "1",
                    "user_login": "a",
                    "tags": ["English"],
                    "is_mature": True,
                    "language": "de",
                },
                {
                    "id": "2",
                    "user_login": "b",
                    "tags": ["Drops Включены"],
                    "is_mature": False,
                    "language": "ru",
                },
                {
                    "id": "3",
                    "user_login": "c",
                    "tags": ["Drops Enabled"],
                    "is_mature": True,
                    "language": "en",
                },
                {
                    "id": "4",
                    "user_login": "d",
                    "tags": [],
                    "is_mature": False,
                    "language": "ja",
                },
            ]

    ordered = _Tw().get_streams_with_drops("99", limit=5)
    assert [s["user_login"] for s in ordered] == ["b", "c", "a", "d"]
    promo_first = _Tw().get_streams_with_drops(
        "99", limit=5, promo_logins={"d", "a"}
    )
    assert [s["user_login"] for s in promo_first] == ["a", "d", "b", "c"]

    from handlers.drops import _format_stream_alert
    from types import SimpleNamespace

    body = _format_stream_alert(
        "ru",
        campaign={"game_name": "G", "name": "Camp", "how_to_earn": "watch", "drops": []},
        streams=ordered,
        db=SimpleNamespace(is_premium_channel_login=lambda _l: False),  # type: ignore[arg-type]
    )
    assert "b" in body and "Drops Включены" in body
    assert "c" in body and "Drops Enabled" in body
    assert body.index("b") < body.index("a")
    assert "👁" in body and "https://twitch.tv/" in body
    assert "Условие получения" not in body
    assert "Как зарабатывать" not in t("drops_digest_alert_body", "ru")
    assert "{streams}" in t("drops_stream_alert_body", "ru")
    assert "{drops_tag}" in t("drops_stream_alert_item", "ru")

    kb = drops_catalog_keyboard(
        "ru",
        [{"name": "C", "game_name": "G", "id": "1", "claimed": True}],
        digest_enabled=True,
    )
    labels = [b.text for r in kb.inline_keyboard for b in r]
    assert any("Подписаться на новые Drops" in (x or "") for x in labels)
    empty_kb = drops_catalog_keyboard("ru", [], digest_enabled=False, show_rebind=True)
    empty_labels = [b.text for r in empty_kb.inline_keyboard for b in r]
    assert any("Подписаться на новые Drops" in (x or "") for x in empty_labels)
    assert any("Перепривязать Twitch" in (x or "") for x in empty_labels)
    assert any((b.callback_data or "") == "drops_rebind" for r in empty_kb.inline_keyboard for b in r)
    assert len(empty_kb.inline_keyboard) >= 2
    assert any((x or "").startswith("✅") for x in labels)
    assert any("получено" in (x or "") for x in labels)
    assert "Получать оповещения" in t("drops_get_alerts_btn", "ru")
    assert "Вы получили Drops" in t("drops_claim_alert_body", "ru", name="X")
    assert "{drops_tag}" in t("drops_stream_alert_item", "ru")


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
        assert db.get_drop_stream_alert_at(7, 1) is None
        db.mark_drop_stream_alert(7, 1, at="2026-01-01T00:00:00+00:00")
        assert db.get_drop_stream_alert_at(7, 1) == "2026-01-01T00:00:00+00:00"
        db.upsert_drops_auth(
            7,
            twitch_user_id="1",
            twitch_login="u",
            refresh_token="rt",
            access_token="at",
            access_expires_at=9999999999,
        )
        auth2 = db.get_drops_auth(7)
        assert auth2 is not None and auth2.access_token == "at"
        assert auth2.access_expires_at == 9999999999
        assert auth2.digest_enabled
        db.delete_drops_auth(7)
        cleared = db.get_drops_auth(7)
        assert cleared is not None
        assert not (cleared.refresh_token or "").strip()
        assert cleared.digest_enabled
        assert 7 not in db.list_drops_digest_owner_ids()
        db.upsert_drops_auth(
            7, twitch_user_id="1", twitch_login="u", refresh_token="rt2"
        )
        assert 7 in db.list_drops_digest_owner_ids()
        rebound = db.get_drops_auth(7)
        assert rebound is not None and rebound.digest_enabled


def _check_drops_gql_soft_errors() -> None:
    from unittest.mock import MagicMock

    from twitch import TwitchClient

    client = TwitchClient.__new__(TwitchClient)
    client._drops_device_id = "a" * 32
    client._session = MagicMock()
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "data": {"currentUser": {"dropCampaigns": []}},
        "errors": [{"message": "service error"}],
    }
    client._session.post.return_value = resp
    body = TwitchClient._gql_persisted(
        client,
        operation_name="ViewerDropsDashboard",
        sha256_hash="abc",
        variables={},
        access_token="tok",
    )
    assert body["data"]["currentUser"]["dropCampaigns"] == []

    resp.json.return_value = {"errors": [{"message": "PersistedQueryNotFound"}]}
    try:
        TwitchClient._gql_persisted(
            client,
            operation_name="ViewerDropsDashboard",
            sha256_hash="abc",
            variables={},
            access_token="tok",
        )
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass


def _check_drops_list_label_and_oauth_keep() -> None:
    from unittest.mock import MagicMock

    from handlers.drops import _access_token_for_owner, _drops_list_label, drops_device_id_for
    from i18n import t

    assert len(drops_device_id_for(1)) == 32
    assert drops_device_id_for(1) == drops_device_id_for(1)
    assert drops_device_id_for(1) != drops_device_id_for(2)

    assert _drops_list_label(
        game_name="Hearthstone", campaign_name="Hero Pack", game_id="1"
    ) == "Hearthstone — Hero Pack"
    assert _drops_list_label(
        game_name="Hearthstone", campaign_name="Hearthstone", game_id="1"
    ) == "Hearthstone"
    ru = t(
        "drops_subscribed_ok",
        "ru",
        game="Hearthstone",
        drop="Hero Pack",
    )
    assert "Hearthstone" in ru and "Hero Pack" in ru
    assert "без мастера" not in ru

    db = MagicMock()
    auth = SimpleNamespace(
        refresh_token="rt", access_token="cached", access_expires_at=10**11
    )
    db.get_drops_auth.return_value = auth
    twitch = MagicMock()
    assert _access_token_for_owner(db, twitch, 1) == "cached"
    twitch.refresh_drops_gql_token.assert_not_called()

    auth2 = SimpleNamespace(refresh_token="rt", access_token="", access_expires_at=0)
    db.get_drops_auth.return_value = auth2

    class _Resp:
        status_code = 401

    class _Exc(Exception):
        response = _Resp()

    twitch.refresh_drops_gql_token.side_effect = _Exc()
    assert _access_token_for_owner(db, twitch, 1) is None
    db.delete_drops_auth.assert_not_called()


def _check_drops_subs_list_hides_edit_share() -> None:
    from unittest.mock import MagicMock, patch

    import beta as beta_features
    from handlers.subscriptions import _format_sub_line, _subs_toggle_keyboard
    from i18n import t

    sub = SimpleNamespace(
        id=25,
        enabled=True,
        twitch_username="Hearthstone — Hero Pack",
        chat_id=1,
        dest_type="dm",
        thread_id=None,
        ignore_keywords="",
        use_global_ignore=False,
        notify_on_live=False,
        notify_on_end=False,
        notify_on_category_change=False,
        notify_on_drops=True,
        drops_game_id="99",
        schedule_reminder_minutes=0,
        schedule_reminder_configured=False,
        image_file_id=None,
        image_position="",
        strip_name_mentions=False,
        delay_minutes=0,
        suppress_repeat_minutes=0,
        delete_previous=False,
        notify_delete_fail=False,
        delete_other_alerts=False,
        custom_buttons="[]",
        attach_chat_button=False,
        attach_live_remind_button=False,
        disable_link_preview=True,
        message_template="Drops: Hearthstone — Hero Pack",
        is_demo=False,
    )
    line = _format_sub_line(sub, "ru", 25)  # type: ignore[arg-type]
    assert "Hearthstone — Hero Pack" in line
    assert t("sub_list_alert_drops", "ru") in line

    db = MagicMock()
    db.get_subscriptions_by_owner.return_value = [sub]
    with patch.object(beta_features, "is_enabled", return_value=True):
        rows = _subs_toggle_keyboard(db, 1, "ru", [sub])  # type: ignore[list-item]
    callbacks = [b.callback_data for r in rows for b in r]
    assert any((c or "").startswith("toggle:") for c in callbacks)
    assert any((c or "").startswith("list_del:") for c in callbacks)
    assert not any((c or "").startswith("edit:") for c in callbacks)
    assert not any((c or "").startswith("share_show:") for c in callbacks)


def _check_game_subs_list_hides_edit_share() -> None:
    from unittest.mock import MagicMock, patch

    import beta as beta_features
    from handlers.subscriptions import _subs_toggle_keyboard

    sub = SimpleNamespace(
        id=26,
        enabled=True,
        twitch_username="Just Chatting + tags",
        chat_id=1,
        dest_type="dm",
        thread_id=None,
        ignore_keywords="",
        use_global_ignore=False,
        notify_on_live=True,
        notify_on_end=False,
        notify_on_category_change=False,
        notify_on_drops=False,
        drops_game_id="",
        schedule_reminder_minutes=0,
        schedule_reminder_configured=False,
        image_file_id=None,
        image_position="",
        strip_name_mentions=False,
        delay_minutes=0,
        suppress_repeat_minutes=0,
        delete_previous=False,
        notify_delete_fail=False,
        delete_other_alerts=False,
        custom_buttons="[]",
        attach_chat_button=False,
        attach_live_remind_button=False,
        disable_link_preview=True,
        message_template="hi",
        is_demo=False,
        category_watch_prefs='{"categories":[{"id":"1","name":"Just Chatting"}]}',
        from_watch_suggest=True,
    )
    db = MagicMock()
    db.get_subscriptions_by_owner.return_value = [sub]
    with patch.object(beta_features, "is_enabled", return_value=True):
        rows = _subs_toggle_keyboard(db, 1, "ru", [sub])  # type: ignore[list-item]
    callbacks = [b.callback_data for r in rows for b in r]
    assert any((c or "").startswith("toggle:") for c in callbacks)
    assert any((c or "").startswith("list_del:") for c in callbacks)
    assert not any((c or "").startswith("edit:") for c in callbacks)
    assert not any((c or "").startswith("share_show:") for c in callbacks)


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
    _check_drops_gql_soft_errors()
    _check_drops_list_label_and_oauth_keep()
    _check_drops_subs_list_hides_edit_share()
    _check_game_subs_list_hides_edit_share()


if __name__ == "__main__":
    run()
    print("checks_drops: ok")
