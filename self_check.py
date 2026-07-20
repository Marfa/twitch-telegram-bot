"""ponytail: minimal self-check for twitch parsing and templates."""
from pathlib import Path
import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from config import parse_admin_user_ids
from links import parse_telegram_topic_link, chat_ref_to_id
from render_deploy import _service_id
from render_status import (
    assert_safe_rss_url,
    fetch_render_status,
    is_aiven_outage,
    is_planned_maintenance,
)
from twitch import TwitchClient, render_template
from translate import build_translations, translate_text
from bot import _is_link_preview_disabled, _message_link
from db import SqliteDatabase, _normalize_pg_url, open_database
from i18n import SUPPORTED_LOCALES, btn, t as tr
from telegram import LinkPreviewOptions, Message


def main() -> None:
    CHANNEL = "marfapr"
    t = TwitchClient()
    assert t.parse_username(CHANNEL) == CHANNEL
    assert t.parse_username("https://www.twitch.tv/marfapr") == CHANNEL
    assert t.parse_username("https://twitch.tv/Marfapr") == CHANNEL
    assert t.parse_username("https://m.twitch.tv/marfapr") == CHANNEL
    assert t.parse_username("@marfapr") == CHANNEL
    assert t.parse_username("not valid!!!") is None

    out = render_template("{username}: {game} / {name}", CHANNEL, "Just Chatting", "Test")
    assert out == "marfapr: Just Chatting / Test"

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
        assert btn("language", loc)
        assert tr("start_welcome", loc)
        feedback = tr("feedback", loc, github="https://example.com", bot_version="abc1234", user_id=42)
        assert "abc1234" in feedback
        assert "42" in feedback
        assert "<code>abc1234</code>" in feedback
        assert "<code>42</code>" in feedback

    with tempfile.TemporaryDirectory() as tmp:
        db = SqliteDatabase(Path(tmp) / "test.db")
        db.upsert_user(1)
        assert db.get_user_locale(1) is None
        db.set_user_locale(1, "en")
        assert db.get_user_locale(1) == "en"
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
        assert restored.subscriptions_total == 1
        assert restored.blocked_users == 0
        bid = db.add_scheduled_broadcast(
            "bot_update", "hello", "2099-01-01T00:00:00+00:00", 1
        )
        unsent = db.get_unsent_scheduled_broadcasts()
        assert any(b.id == bid for b in unsent)
        assert not db.update_subscription(999, 1, message_template="x")

    items = fetch_render_status("https://status.render.com/history.rss")
    assert items
    # Live feed content varies; only assert parser helpers on fixtures when present.
    for item in items:
        is_planned_maintenance(item)

    aiven_items = fetch_render_status("https://status.aiven.io/feed.rss")
    assert aiven_items
    for item in aiven_items:
        is_aiven_outage(item)

    assert_safe_rss_url("https://status.render.com/history.rss")
    try:
        assert_safe_rss_url("http://status.render.com/history.rss")
        raise AssertionError("expected http RSS URL rejection")
    except ValueError:
        pass
    try:
        assert_safe_rss_url("https://evil.example/feed.rss")
        raise AssertionError("expected host allowlist rejection")
    except ValueError:
        pass
    try:
        assert_safe_rss_url("https://169.254.169.254/latest/meta-data/")
        raise AssertionError("expected metadata URL rejection")
    except ValueError:
        pass

    assert parse_admin_user_ids("") == frozenset()
    assert parse_admin_user_ids("123, 456") == frozenset({123, 456})

    assert translate_text("hello", target_lang="en", source_lang="en") == "hello"
    assert build_translations("hello", "en", {"en"}) == {"en": "hello"}
    assert build_translations("hello", "en", {"en", "ru"})["en"] == "hello"

    with patch.dict(os.environ, {"RENDER_SERVICE_ID": "srv-test123"}, clear=False):
        assert _service_id("") == "srv-test123"
    assert _service_id("srv-cli456") == "srv-cli456"
    with patch("render_deploy._load_dotenv", lambda path=Path(".env"): None):
        with patch.dict(os.environ, {}, clear=True):
            try:
                _service_id("")
                raise AssertionError("expected missing RENDER_SERVICE_ID")
            except RuntimeError:
                pass

    print("ok")


if __name__ == "__main__":
    main()
