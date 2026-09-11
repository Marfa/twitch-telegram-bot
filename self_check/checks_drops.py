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
from twitch import _parse_twitchdrops_app_campaign


def _check_drops_alert_type_keyboard_last() -> None:
    hidden = alert_type_keyboard("en")
    assert not any(
        (b.callback_data or "") == "alert_type:drops"
        for r in hidden.inline_keyboard
        for b in r
    ), "Drops must be hidden when show_drops=False"
    hidden_cbs = [b.callback_data for r in hidden.inline_keyboard for b in r]
    assert hidden_cbs[-3] == "alert_type:game"
    assert hidden_cbs[-2] == "alert_type:cancel"
    assert hidden_cbs[-1] == "alert_type:other"
    markup = alert_type_keyboard("en", show_drops=True)
    rows = markup.inline_keyboard
    assert any(
        (b.callback_data or "") == "alert_type:drops" for r in rows for b in r
    ), "Drops missing from alert type keyboard"
    callbacks = [b.callback_data for r in rows for b in r]
    assert callbacks[-4] == "alert_type:drops"
    assert callbacks[-3] == "alert_type:game"
    assert callbacks[-2] == "alert_type:cancel"
    assert callbacks[-1] == "alert_type:other"


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
        "gameId": "123",
        "game": "Cool Game",
        "gameSlug": "cool-game",
        "startAt": "2026-01-01T00:00:00Z",
        "endAt": "2026-02-01T00:00:00Z",
        "drops": [
            {
                "id": "d1",
                "name": "Skin",
                "requiredMinutes": 120,
                "rewardName": "Cool Skin",
            }
        ],
    }
    parsed = _parse_twitchdrops_app_campaign(raw)
    assert parsed is not None
    assert parsed["id"] == "camp-1"
    assert parsed["game_id"] == "123"
    assert parsed["drops"][0]["required_minutes"] == 120
    assert "claimed" not in parsed
    assert "is_claimed" not in parsed["drops"][0]


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


def _check_drops_catalog_fetch_copy() -> None:
    from i18n import t

    assert "перепривяз" not in t("drops_catalog_fetch_failed", "ru").lower()
    assert "re-link" not in t("drops_catalog_fetch_failed", "en").lower()
    assert "вручную" not in t("drops_catalog_empty", "ru")
    assert "twitchdrops.app" not in t("drops_catalog_fetch_failed", "en").lower()


def _check_drops_catalog_from_app_only() -> None:
    """Catalog comes only from twitchdrops.app — no OAuth enrichment."""
    from handlers.drops import list_active_drop_campaigns
    from twitch import TwitchClient
    from unittest.mock import MagicMock

    db = MagicMock()
    twitch = MagicMock()
    twitch.fetch_twitchdrops_app_campaigns.return_value = [
        {
            "id": "c2",
            "name": "Beta Camp",
            "status": "ACTIVE",
            "game_id": "2",
            "game_name": "Zebra",
            "game_slug": "zebra",
            "drops": [{"id": "d2", "name": "Drop"}],
        },
        {
            "id": "c1",
            "name": "Alpha Camp",
            "status": "ACTIVE",
            "game_id": "1",
            "game_name": "Apex",
            "game_slug": "apex",
            "drops": [{"id": "d1", "name": "Drop"}],
        },
    ]
    out = list_active_drop_campaigns(db, twitch, 7)
    assert out and [c["id"] for c in out] == ["c1", "c2"]
    assert "claimed" not in out[0]
    twitch.fetch_twitchdrops_app_campaigns.assert_called_once()
    assert not hasattr(TwitchClient, "get_viewer_drop_campaigns")
    assert not hasattr(TwitchClient, "get_inventory_claimed_drops")


def _check_drops_tags_and_catalog_keyboard() -> None:
    from i18n import drops_catalog_keyboard, t
    from twitch import TwitchClient, _parse_twitchdrops_app_how_to_html

    assert TwitchClient.stream_has_drops_tag({"tags": ["Drops Enabled"]})
    assert TwitchClient.stream_has_drops_tag({"tags": ["Drops Включены"]})
    assert TwitchClient.stream_has_drops_tag({"tags": ["Drops"]})
    assert not TwitchClient.stream_has_drops_tag({"tags": ["English"]})
    assert TwitchClient.matched_drops_tag({"tags": ["Drops Enabled"]}) == "Drops Enabled"
    assert (
        TwitchClient.matched_drops_tag({"tags": ["Drops Включены"]})
        == "Drops Включены"
    )
    assert TwitchClient.matched_drops_tag({"tags": ["Drops"]}) == "Drops"

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
                {
                    "id": "5",
                    "user_login": "e",
                    "tags": ["Drops"],
                    "is_mature": False,
                    "language": "en",
                },
            ]

    ordered = _Tw().get_streams_with_drops("99", limit=5)
    assert [s["user_login"] for s in ordered] == ["b", "c", "e", "a", "d"]
    promo_first = _Tw().get_streams_with_drops(
        "99", limit=5, promo_logins={"d", "a"}
    )
    assert [s["user_login"] for s in promo_first] == ["a", "d", "b", "c", "e"]

    how_html = """
    <h2>How to get these drops</h2>
    <ol class="how-to-steps">
      <li><div class="hts-text"><strong>Link account</strong> —
          <a href="https://albiononline.com/twitch" target="_blank">Connect ↗</a>,
          or via <a href="https://www.twitch.tv/settings/connections">Twitch Settings</a>.
      </div></li>
      <li><div class="hts-text"><strong>Watch</strong> — streams with
          <a href="/directory">Drops</a>.</div></li>
    </ol>
    """
    how_text = _parse_twitchdrops_app_how_to_html(how_html)
    assert "1. Link account" in how_text and "2. Watch" in how_text
    assert "https://albiononline.com/twitch" in how_text
    assert "https://www.twitch.tv/settings/connections" in how_text
    assert "https://twitchdrops.app/directory" in how_text

    from handlers.drops import _digest_alert_keyboard, _format_stream_alert
    from types import SimpleNamespace
    from unittest.mock import patch

    with patch("translate.translate_text", side_effect=lambda text, **_: text):
        body = _format_stream_alert(
            "ru",
            campaign={
                "game_name": "G",
                "name": "Camp",
                "how_to_earn": how_text,
                "drops": [],
            },
            streams=ordered,
            db=SimpleNamespace(is_premium_channel_login=lambda _l: False),  # type: ignore[arg-type]
        )
    assert "b" in body and "Drops Включены" in body
    assert "c" in body and "Drops Enabled" in body
    assert "e" in body and "Drops" in body
    assert body.index("b") < body.index("a")
    assert "👁" in body
    assert any(ln.strip().startswith("https://twitch.tv/") for ln in body.splitlines())
    assert "Как получить" in body
    assert "Link account" in body
    assert "{streams}" in t("drops_stream_alert_body", "ru")
    assert "{drops_tag}" in t("drops_stream_alert_item", "ru")
    assert "{text}" in t("drops_how_to_get", "ru")
    assert "{n}" in t("drops_digest_alert_header", "ru")

    kb = drops_catalog_keyboard(
        "ru",
        [{"name": f"C{i}", "game_name": "G", "id": str(i)} for i in range(10)],
        digest_enabled=True,
        page=0,
        page_size=8,
    )
    labels = [b.text for r in kb.inline_keyboard for b in r]
    assert any("Подписаться на новые Drops" in (x or "") for x in labels)
    assert any((x or "").startswith("✅") for x in labels)
    assert any((b.callback_data or "") == "drops_camp:page:1" for r in kb.inline_keyboard for b in r)
    assert any((b.callback_data or "") == "drops_camp:pick:0" for r in kb.inline_keyboard for b in r)
    page1 = drops_catalog_keyboard(
        "ru",
        [{"name": f"C{i}", "game_name": "G", "id": str(i)} for i in range(10)],
        page=1,
        page_size=8,
    )
    assert any(
        (b.callback_data or "") == "drops_camp:pick:8" for r in page1.inline_keyboard for b in r
    )
    empty_kb = drops_catalog_keyboard("ru", [], digest_enabled=False)
    empty_labels = [b.text for r in empty_kb.inline_keyboard for b in r]
    assert any("Подписаться на новые Drops" in (x or "") for x in empty_labels)
    assert "Новые Drops" in t("drops_digest_alert_header", "ru", n=2)
    assert "Отключить оповещения о новых Drops" in t(
        "drops_digest_disable_btn", "ru"
    )
    assert "Перейти в Drops" in t("drops_digest_open_btn", "ru")
    dkb = _digest_alert_keyboard("ru")
    dlabels = [b.text for r in dkb.inline_keyboard for b in r]
    assert any("Перейти в Drops" in (x or "") for x in dlabels)
    assert any(
        (b.callback_data or "") == "drops_digest:off"
        for r in dkb.inline_keyboard
        for b in r
    )
    assert any(
        (b.callback_data or "") == "drops_digest:open"
        for r in dkb.inline_keyboard
        for b in r
    )


def _check_drops_digest_clears_without_premium() -> None:
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    import premium as prem
    from handlers.drops import (
        DROPS_BETA_ID,
        DROPS_FEATURE_ID,
        maybe_clear_drops_digest_after_beta_exit,
    )

    with tempfile.TemporaryDirectory() as tmp:
        db = open_database(Path(tmp) / "t.db")
        db.upsert_user(7)
        db.upsert_drops_auth(
            7, twitch_user_id="1", twitch_login="u", refresh_token="rt"
        )
        db.set_beta_enrollment(7, DROPS_BETA_ID, True)
        db.set_drops_digest_enabled(7, True)
        assert 7 in db.list_drops_digest_owner_ids()
        db.set_beta_enrollment(7, DROPS_BETA_ID, False)
        assert not prem.has_feature_sync(db, 7, DROPS_FEATURE_ID)
        bot = MagicMock()
        with patch.object(prem, "has_feature", new=AsyncMock(return_value=False)):
            asyncio.run(maybe_clear_drops_digest_after_beta_exit(bot, db, 7))
        assert 7 not in db.list_drops_digest_owner_ids()
        db.set_drops_digest_enabled(7, True)
        with patch.object(prem, "has_feature", new=AsyncMock(return_value=True)):
            asyncio.run(maybe_clear_drops_digest_after_beta_exit(bot, db, 7))
        assert 7 in db.list_drops_digest_owner_ids()


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
        db.mark_drop_stream_seen(
            7, 1, "s1", first_seen_at="2026-01-01T00:00:00Z"
        )
        assert db.has_seen_drop_stream(7, 1, "s1")
        db.mark_drop_campaign_seen(
            7, "c1", 0, first_seen_at="2026-01-01T00:00:00Z"
        )
        assert db.has_seen_drop_campaign(7, "c1", 0)
        db.delete_drops_auth(7)
        cleared = db.get_drops_auth(7)
        assert cleared is not None
        assert not (cleared.refresh_token or "").strip()
        assert cleared.digest_enabled
        # Digest does not need OAuth — survives token wipe.
        assert 7 in db.list_drops_digest_owner_ids()
        db.upsert_drops_auth(
            7, twitch_user_id="", twitch_login="", refresh_token=""
        )
        db.set_drops_digest_enabled(7, True)
        assert 7 in db.list_drops_digest_owner_ids()


def _check_drops_auth_seen_db() -> None:
    """Stream/campaign seen tables; drops_auth token columns remain in schema."""
    with tempfile.TemporaryDirectory() as tmp:
        db = open_database(Path(tmp) / "t.db")
        db.upsert_user(7)
        db.upsert_drops_auth(
            7, twitch_user_id="1", twitch_login="u", refresh_token="rt"
        )
        auth = db.get_drops_auth(7)
        assert auth is not None and (auth.refresh_token or "").strip()
        assert not db.has_seen_drop_stream(7, 1, "s1")
        db.mark_drop_stream_seen(7, 1, "s1", first_seen_at="2026-01-01T00:00:00+00:00")
        assert db.has_seen_drop_stream(7, 1, "s1")
        assert not db.has_seen_drop_campaign(7, "c1", 0)
        db.mark_drop_campaign_seen(
            7, "c1", 0, first_seen_at="2026-01-01T00:00:00+00:00"
        )
        assert db.has_seen_drop_campaign(7, "c1", 0)
        db.update_drops_auth_access(
            7, access_token="at", access_expires_at=10**11, refresh_token="rt2"
        )
        auth2 = db.get_drops_auth(7)
        assert auth2 is not None
        assert auth2.access_token == "at"
        assert auth2.refresh_token == "rt2"
        db.delete_drops_auth(7)
        cleared = db.get_drops_auth(7)
        assert cleared is not None
        assert not (cleared.refresh_token or "").strip()


def _check_drops_list_label() -> None:
    from handlers.drops import _drops_list_label
    from i18n import t

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


def _check_drops_subs_list_edit_no_share() -> None:
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
        suppress_repeat_minutes=60,
        delete_previous=False,
        notify_delete_fail=False,
        delete_other_alerts=False,
        custom_buttons="[]",
        attach_chat_button=False,
        attach_live_remind_button=False,
        disable_link_preview=True,
        message_template="Drops: Hearthstone — Hero Pack",
        is_demo=False,
        notify_cooldown_until=None,
    )
    line = _format_sub_line(sub, "ru", 25)  # type: ignore[arg-type]
    assert "Hearthstone — Hero Pack" in line
    assert t("sub_list_alert_drops", "ru") in line
    assert t("sub_list_game_cooldown", "ru", minutes=60) in line

    db = MagicMock()
    db.get_subscriptions_by_owner.return_value = [sub]
    with patch.object(beta_features, "is_enabled", return_value=True):
        rows = _subs_toggle_keyboard(db, 1, "ru", [sub])  # type: ignore[list-item]
    callbacks = [b.callback_data for r in rows for b in r]
    assert any((c or "").startswith("toggle:") for c in callbacks)
    assert any((c or "").startswith("list_del:") for c in callbacks)
    assert any((c or "").startswith("edit:") for c in callbacks)
    assert not any((c or "").startswith("share_show:") for c in callbacks)
    assert "частота обновлений" in t("drops_subscribed_ok", "ru", game="G", drop="D")
    assert "стримам" in t("drops_catalog_prompt", "ru")


def _check_drops_stream_alert_cooldown() -> None:
    """only_new must no-op while notify_cooldown_until is in the future."""
    import asyncio
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from handlers.drops import send_drops_stream_alert

    future = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    sub = SimpleNamespace(
        id=1,
        owner_id=7,
        enabled=True,
        drops_game_id="99",
        notify_on_drops=True,
        chat_id=7,
        thread_id=None,
        twitch_username="HS",
        notify_cooldown_until=future,
        suppress_repeat_minutes=120,
    )
    bot = AsyncMock()
    db = MagicMock()
    twitch = MagicMock()
    n = asyncio.run(
        send_drops_stream_alert(
            bot, db, twitch, sub, "ru", only_new=True  # type: ignore[arg-type]
        )
    )
    assert n == 0
    bot.send_message.assert_not_called()
    twitch.get_streams_with_drops.assert_not_called()


def _check_game_subs_list_edit_no_share() -> None:
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
    assert any((c or "").startswith("edit:") for c in callbacks)
    assert not any((c or "").startswith("share_show:") for c in callbacks)


def run() -> None:
    _check_drops_alert_type_keyboard_last()
    _check_drops_payload_and_migrate()
    _check_drops_parse_campaign()
    _check_drops_sub_helper()
    _check_drops_premium_gate()
    _check_drops_active_cap_on_bulk()
    _check_drops_catalog_fetch_copy()
    _check_drops_catalog_from_app_only()
    _check_drops_tags_and_catalog_keyboard()
    _check_drops_digest_db()
    _check_drops_digest_clears_without_premium()
    _check_drops_auth_seen_db()
    _check_drops_list_label()
    _check_drops_subs_list_edit_no_share()
    _check_drops_stream_alert_cooldown()
    _check_game_subs_list_edit_no_share()


if __name__ == "__main__":
    run()
    print("checks_drops: ok")
