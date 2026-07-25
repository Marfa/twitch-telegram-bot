"""ponytail: minimal self-check for twitch parsing and templates."""
from pathlib import Path
import os
import tempfile
from datetime import datetime, timedelta, timezone

from config import parse_admin_user_ids
from links import parse_telegram_topic_link, chat_ref_to_id
from twitch import (
    FOLLOWS_SCOPE,
    TwitchClient,
    find_placeholder_typos,
    normalize_ignore_keywords,
    preview_stream_title,
    render_template,
    should_ignore_stream,
)
from translate import build_translations, translate_text
from bot import (
    _is_link_preview_disabled,
    _message_link,
    _parse_sb_edit_f_id,
    import_followed_as_subscriptions,
    live_transitions,
)
from db import SqliteDatabase, _normalize_pg_url, open_database
from i18n import SUPPORTED_LOCALES, btn, t as tr
from health import create_oauth_state, pop_oauth_state
from hf_text import _normalize_template
from telegram import LinkPreviewOptions, Message


def main() -> None:
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_TOKEN_FOR_SELF_CHECK")
    CHANNEL = "marfapr"
    t = TwitchClient()
    assert t.parse_username(CHANNEL) == CHANNEL
    assert t.parse_username("https://www.twitch.tv/marfapr") == CHANNEL
    assert t.parse_username("https://twitch.tv/Marfapr") == CHANNEL
    assert t.parse_username("https://m.twitch.tv/marfapr") == CHANNEL
    assert t.parse_username("@marfapr") == CHANNEL
    assert t.parse_username("not valid!!!") is None
    assert t.is_twitch_url("https://www.twitch.tv/marfapr")
    assert t.is_twitch_url("https://m.twitch.tv/marfapr")
    assert t.is_twitch_url("twitch.tv/marfapr")
    assert not t.is_twitch_url("marfapr")
    assert not t.is_twitch_url("@marfapr")

    out = render_template("{username}: {game} / {name}", CHANNEL, "Just Chatting", "Test")
    assert out == "marfapr: Just Chatting / Test"
    auth_url = t.build_authorize_url(
        redirect_uri="https://example.com/oauth/twitch/callback",
        state="abc",
    )
    assert "response_type=code" in auth_url
    assert "user%3Aread%3Afollows" in auth_url or "user:read:follows" in auth_url
    assert "offline_access" not in auth_url
    assert "state=abc" in auth_url
    state = create_oauth_state(42, "ru")
    assert pop_oauth_state(state) == (42, "ru")
    assert pop_oauth_state(state) is None
    from health import _html_page
    from i18n import t as tr

    html_ru = _html_page(
        tr("oauth_web_done_title", "ru"),
        tr("oauth_web_done_body", "ru"),
    ).decode()
    assert "Готово" in html_ru
    assert "Telegram" in html_ru
    html_en = _html_page(
        tr("oauth_web_done_title", "en"),
        tr("oauth_web_done_body", "en"),
    ).decode()
    assert "Done" in html_en
    title_ru = preview_stream_title("ru", "Elden Ring")
    assert "Elden Ring" in title_ru
    assert "Тестовый" not in title_ru
    title_en = preview_stream_title("en", "Elden Ring")
    assert "Elden Ring" in title_en
    assert "Test stream" not in title_en

    state: dict[str, bool] = {}
    assert live_transitions(state, ["1", "2"], {"1": {}}, primed=False) == []
    assert state == {"1": True, "2": False}
    assert live_transitions(state, ["1", "2"], {"1": {}}, primed=True) == []
    assert live_transitions(state, ["1", "2"], {"1": {}, "2": {}}, primed=True) == ["2"]
    assert state["2"] is True
    assert live_transitions(state, ["1", "2"], {}, primed=True) == []
    assert state == {"1": False, "2": False}
    assert live_transitions(state, ["1"], {"1": {}}, primed=True) == ["1"]

    assert find_placeholder_typos("{username} {game} {name}") == []
    assert find_placeholder_typos("{game)") == [("{game)", "{game}")]
    assert find_placeholder_typos("(game}") == [("(game}", "{game}")]
    assert find_placeholder_typos("{Game}") == [("{Game}", "{game}")]
    assert find_placeholder_typos("{gama}") == [("{gama}", "{game}")]
    assert find_placeholder_typos("{user_name}") == [("{user_name}", "{username}")]
    assert ("{nam}", "{name}") in find_placeholder_typos("hi {nam}!")
    assert find_placeholder_typos("Play (game) tonight") == []
    assert find_placeholder_typos("{username} is live, {game)") == [("{game)", "{game}")]
    assert find_placeholder_typos("{ game }") == [("{ game }", "{game}")]

    assert normalize_ignore_keywords("foo, bar , baz") == "foo, bar, baz"
    assert normalize_ignore_keywords("") == ""
    assert should_ignore_stream("chatting", "Just Chatting", "Playing games")
    assert should_ignore_stream("foo", "Foo Bar", "Playing games")
    assert should_ignore_stream("stream", "Just Chatting", "My Foo stream")
    assert not should_ignore_stream("foo, bar", "Just Chatting", "Playing games")
    assert not should_ignore_stream("", "Just Chatting", "Foo")

    link = parse_telegram_topic_link("https://t.me/c/themarfa_gaming/30")
    assert link is not None
    assert link.chat_ref == "themarfa_gaming"
    assert link.thread_id == 30
    assert chat_ref_to_id("1234567890") == -1001234567890
    assert _message_link(-1001234567890, 42) == "https://t.me/c/1234567890/42"
    assert _message_link(-1001234567890, 42, 7) == "https://t.me/c/1234567890/7/42"

    pg_url = _normalize_pg_url("postgres://user:pass@host:1234/db")
    assert pg_url.startswith("postgresql://")
    assert "sslmode=require" in pg_url

    plain = Message(message_id=1, date=None, chat=None)
    assert not _is_link_preview_disabled(plain)
    no_preview = Message(
        message_id=2,
        date=None,
        chat=None,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    assert _is_link_preview_disabled(no_preview)

    for loc in SUPPORTED_LOCALES:
        assert btn("new", loc)
        assert btn("settings", loc)
        assert btn("sync_subs", loc)
        assert btn("language", loc)
        assert tr("start_welcome", loc)
        assert tr("import_mode_prompt", loc)
        assert tr("sync_menu_off", loc)
        assert tr("lucky_btn", loc)
        assert tr("lucky_hint", loc)
        assert tr("image_ask", loc)
        assert tr("edit_image", loc)
        assert "Изображение можно добавить" in tr("channel_found", "ru", display_name="x") or loc != "ru"
        assert "You can add an image" in tr("channel_found", "en", display_name="x") or loc != "en"
        feedback = tr("feedback", loc, github="https://example.com", user_id=42)
        assert "42" in feedback
        assert "<code>42</code>" in feedback
        assert "bot_version" not in feedback
        assert "Версия бота" not in feedback
        assert "Bot version" not in feedback

    normalized = _normalize_template("Streamer online!")
    assert "{username}" in normalized
    assert "{game}" in normalized
    assert "{name}" in normalized
    assert _normalize_template("{username}\n{game}\n{name}") == "{username}\n{game}\n{name}"
    assert "{name}" in _normalize_template("live!\nТестовый стрим")
    assert "Тестовый" not in _normalize_template("live!\nТестовый стрим")
    assert "Test stream" not in _normalize_template("hi\nTest stream")

    from hf_text import _local_template, generate_alert_template
    local_ru = _local_template("ru")
    assert "{username}" in local_ru and "{game}" in local_ru and "{name}" in local_ru
    # With no cloud tokens, generation must still return a template via fallback.
    import os as _os
    _prev = {
        k: _os.environ.get(k)
        for k in ("HF_TOKEN", "HUGGING_FACE_API", "GROQ_API_KEY", "GROQ_API", "GROK_API")
    }
    for k in _prev:
        _os.environ[k] = ""
    try:
        fallback = generate_alert_template(locale="ru", channel="marfapr")
        assert "{username}" in fallback and "{name}" in fallback
    finally:
        for k, v in _prev.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v

    with tempfile.TemporaryDirectory() as tmp:
        db = SqliteDatabase(Path(tmp) / "test.db")
        db.upsert_user(1)
        assert db.get_user_locale(1) is None
        db.set_user_locale(1, "en")
        assert db.get_user_locale(1) == "en"
        sample = "{username} live!\n{name}\n{game}"
        db.add_lucky_template("ru", sample)
        with db._conn() as conn:
            texts = {
                str(r["text"])
                for r in conn.execute(
                    "SELECT text FROM lucky_templates WHERE locale = 'ru'"
                ).fetchall()
            }
            ru_n = conn.execute(
                "SELECT COUNT(*) AS c FROM lucky_templates WHERE locale = 'ru'"
            ).fetchone()["c"]
            en_n = conn.execute(
                "SELECT COUNT(*) AS c FROM lucky_templates WHERE locale = 'en'"
            ).fetchone()["c"]
        assert sample in texts
        assert db.pick_lucky_template("en") is not None  # seeded on migrate
        assert en_n == 100
        assert ru_n == 100  # seeded full; add_lucky_template trims to 100
        # Extra inserts still capped at 100.
        for i in range(105):
            db.add_lucky_template("ru", f"{{username}}\n{{name}}\n{{game}}\n#{i}")
        with db._conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM lucky_templates WHERE locale = 'ru'"
            ).fetchone()["c"]
        assert count == 100
        from_store = _local_template("ru", db)
        assert "{username}" in from_store
        # Empty cloud tokens → pick from store, not seed.
        for k in _prev:
            _os.environ[k] = ""
        try:
            via_store = generate_alert_template(locale="ru", channel="x", store=db)
            assert "{username}" in via_store
        finally:
            for k, v in _prev.items():
                if v is None:
                    _os.environ.pop(k, None)
                else:
                    _os.environ[k] = v
        sub_id = db.add_subscription(
            owner_id=1,
            twitch_username=CHANNEL,
            twitch_user_id="123",
            message_template="hi",
            dest_type="dm",
            chat_id=1,
            thread_id=None,
        )
        stats = db.get_bot_stats()
        assert stats.users == 1
        assert stats.subscriptions_total == 1
        assert stats.subscriptions_enabled == 1
        assert stats.sys_updates == 1
        paused_id = db.add_subscription(
            owner_id=1,
            twitch_username="other",
            twitch_user_id="999",
            message_template=tr("import_default_template", "ru"),
            dest_type="dm",
            chat_id=1,
            thread_id=None,
            enabled=False,
        )
        paused = db.get_subscription(paused_id, 1)
        assert paused is not None and paused.enabled is False
        assert "{username}" in paused.message_template
        assert "{game}" in paused.message_template
        assert "twitch.tv/" not in paused.message_template
        imported, skipped, limited, removed, new_subs = import_followed_as_subscriptions(
            db,
            1,
            [
                {"broadcaster_id": "123", "broadcaster_login": CHANNEL},
                {"broadcaster_id": "555", "broadcaster_login": "newbie"},
                {"broadcaster_id": "555", "broadcaster_login": "newbie"},
            ],
            template=tr("import_default_template", "en"),
            limit=25,
        )
        assert imported == 1 and skipped == 1 and limited == 0 and removed == 0
        assert len(new_subs) == 1
        assert new_subs[0].twitch_username == "newbie"
        assert new_subs[0].from_twitch_sync is True
        assert paused.from_twitch_sync is False
        # Prune: remove sync-origin "newbie" when follows only keep CHANNEL
        imported2, skipped2, limited2, removed2, _ = import_followed_as_subscriptions(
            db,
            1,
            [{"broadcaster_id": "123", "broadcaster_login": CHANNEL}],
            template=tr("import_default_template", "en"),
            limit=25,
            prune_missing=True,
        )
        assert imported2 == 0 and skipped2 == 1 and removed2 == 1
        assert db.get_subscription(new_subs[0].id, 1) is None
        assert db.get_subscription(paused_id, 1) is not None  # manual kept
        assert db.enable_all_subscriptions(1) >= 1
        assert db.get_subscription(paused_id, 1).enabled is True
        assert db.get_twitch_sync(1) is None
        db.upsert_twitch_sync(
            owner_id=1,
            twitch_user_id="tw1",
            refresh_token="rtok",
            period_days=7,
            next_sync_at="2020-01-01T00:00:00+00:00",
        )
        # Ciphertext at rest
        with db._conn() as conn:
            raw = conn.execute(
                "SELECT refresh_token FROM twitch_sync WHERE owner_id = 1"
            ).fetchone()["refresh_token"]
        assert str(raw).startswith("enc:v1:")
        assert "rtok" not in str(raw)
        sync = db.get_twitch_sync(1)
        assert sync is not None
        assert sync.period_days == 7
        assert sync.refresh_token == "rtok"
        due = db.get_due_twitch_syncs("2020-01-02T00:00:00+00:00")
        assert len(due) == 1 and due[0].owner_id == 1
        assert due[0].refresh_token == "rtok"
        assert db.set_twitch_sync_period(1, 14, "2030-01-01T00:00:00+00:00")
        assert db.get_twitch_sync(1).period_days == 14
        assert db.get_due_twitch_syncs("2020-01-02T00:00:00+00:00") == []
        db.update_twitch_sync_tokens(
            1,
            "rtok2",
            last_sync_at="2026-01-01T00:00:00+00:00",
            next_sync_at="2030-06-01T00:00:00+00:00",
        )
        assert db.get_twitch_sync(1).refresh_token == "rtok2"
        assert db.delete_twitch_sync(1) is True
        assert db.get_twitch_sync(1) is None
        from token_crypto import encrypt_secret, decrypt_secret

        assert decrypt_secret(encrypt_secret("secret-token")) == "secret-token"
        assert stats.sys_availability == 1
        assert stats.blocked_users == 0
        assert db.update_subscription(sub_id, 1, message_template="bye")
        sub = db.get_subscription(sub_id, 1)
        assert sub is not None
        assert sub.message_template == "bye"
        assert sub.delay_minutes == 0
        assert db.update_subscription(sub_id, 1, delay_minutes=10)
        sub = db.get_subscription(sub_id, 1)
        assert sub is not None
        assert sub.delay_minutes == 10
        assert db.update_subscription(sub_id, 1, suppress_repeat_minutes=30)
        sub = db.get_subscription(sub_id, 1)
        assert sub is not None
        assert sub.suppress_repeat_minutes == 30
        assert sub.notify_delete_fail is False
        assert db.update_subscription(sub_id, 1, notify_delete_fail=True)
        sub = db.get_subscription(sub_id, 1)
        assert sub is not None
        assert sub.notify_delete_fail is True
        assert sub.ignore_keywords == ""
        assert db.update_subscription(sub_id, 1, ignore_keywords="foo, bar")
        sub = db.get_subscription(sub_id, 1)
        assert sub is not None
        assert sub.ignore_keywords == "foo, bar"
        assert sub.image_file_id is None
        assert sub.image_position == ""
        assert db.update_subscription(
            sub_id, 1, image_file_id="AgAC_test_file", image_position="before"
        )
        sub = db.get_subscription(sub_id, 1)
        assert sub is not None
        assert sub.image_file_id == "AgAC_test_file"
        assert sub.image_position == "before"
        assert db.update_subscription(sub_id, 1, image_file_id=None, image_position="")
        sub = db.get_subscription(sub_id, 1)
        assert sub is not None
        assert sub.image_file_id is None
        assert sub.image_position == ""
        assert db.count_new_users_since(datetime.now(timezone.utc) - timedelta(days=1)) == 1
        assert db.count_new_users_since(datetime.now(timezone.utc) + timedelta(days=1)) == 0
        db.set_notify_cooldown(sub_id, 5)
        sub = db.get_subscription(sub_id, 1)
        assert sub is not None
        assert sub.notify_cooldown_until is not None
        from db import is_on_notify_cooldown

        assert is_on_notify_cooldown(sub)
        assert db.get_receive_bot_updates(1) is True
        db.set_receive_bot_updates(1, False)
        assert db.get_receive_bot_updates(1) is False
        assert 1 not in db.get_bot_update_recipients()
        assert db.get_receive_availability_updates(1) is True
        db.set_receive_availability_updates(1, False)
        assert db.get_receive_availability_updates(1) is False
        assert 1 not in db.get_availability_recipients()
        db.set_receive_bot_updates(1, True)
        db.set_receive_availability_updates(1, True)
        db.set_bot_blocked(1, True)
        assert db.is_bot_blocked(1) is True
        assert 1 not in db.get_bot_update_recipients()
        assert 1 not in db.get_availability_recipients()
        blocked_stats = db.get_bot_stats()
        assert blocked_stats.blocked_users == 1
        assert blocked_stats.users == 0
        assert blocked_stats.notify_users == 0
        assert blocked_stats.subscriptions_total == 0
        assert blocked_stats.subscriptions_enabled == 0
        assert blocked_stats.unique_owners == 0
        assert blocked_stats.unique_twitch_channels == 0
        assert blocked_stats.sys_updates == 0
        assert blocked_stats.sys_availability == 0
        db.upsert_user(1)
        assert db.is_bot_blocked(1) is False
        restored = db.get_bot_stats()
        assert restored.users == 1
        assert restored.subscriptions_total == 2
        assert restored.blocked_users == 0
        bid = db.add_scheduled_broadcast(
            "bot_update", "hello", "2099-01-01T00:00:00+00:00", 1
        )
        unsent = db.get_unsent_scheduled_broadcasts()
        assert any(b.id == bid for b in unsent)
        item = db.get_scheduled_broadcast(bid)
        assert item is not None
        assert item.text == "hello"
        assert db.update_scheduled_broadcast(bid, text="updated")
        item = db.get_scheduled_broadcast(bid)
        assert item is not None
        assert item.text == "updated"
        assert db.delete_scheduled_broadcast(bid)
        assert db.get_scheduled_broadcast(bid) is None
        bid2 = db.add_scheduled_broadcast(
            "bot_update", "bye", "2099-01-01T00:00:00+00:00", 1
        )
        db.mark_scheduled_broadcast_sent(bid2)
        assert db.get_scheduled_broadcast(bid2) is None
        with db._conn() as conn:
            left = conn.execute(
                "SELECT COUNT(*) AS c FROM scheduled_broadcasts WHERE id = ?",
                (bid2,),
            ).fetchone()["c"]
        assert left == 0
        assert not db.update_subscription(999, 1, message_template="x")

    assert parse_admin_user_ids("") == frozenset()
    assert parse_admin_user_ids("123, 456") == frozenset({123, 456})
    assert _parse_sb_edit_f_id("sb_edit_f:42:text") == 42
    assert _parse_sb_edit_f_id("sb_edit_f:7:time") == 7
    assert "⏸" in tr("sync_disable", "ru")
    assert "⏸" in tr("toggle_off", "ru")

    assert translate_text("hello", target_lang="en", source_lang="en") == "hello"
    assert build_translations("hello", "en", {"en"}) == {"en": "hello"}
    assert build_translations("hello", "en", {"en", "ru"})["en"] == "hello"

    print("ok")


if __name__ == "__main__":
    main()
