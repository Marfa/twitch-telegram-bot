"""ponytail: minimal self-check for twitch parsing and templates."""
from pathlib import Path
import tempfile

from links import parse_telegram_topic_link, chat_ref_to_id
from render_status import fetch_render_status, is_planned_maintenance
from twitch import TwitchClient, render_template
from bot import _is_link_preview_disabled
from db import SqliteDatabase, _normalize_pg_url, open_database
from i18n import SUPPORTED_LOCALES, btn, t as tr
from telegram import LinkPreviewOptions, Message


def main() -> None:
    t = TwitchClient()
    assert t.parse_username("ninja") == "ninja"
    assert t.parse_username("https://twitch.tv/Ninja") == "ninja"
    assert t.parse_username("https://m.twitch.tv/ninja") == "ninja"
    assert t.parse_username("@ninja") == "ninja"
    assert t.parse_username("not valid!!!") is None

    out = render_template("{username}: {game} / {name}", "ninja", "Fortnite", "Test")
    assert out == "ninja: Fortnite / Test"

    link = parse_telegram_topic_link("https://t.me/c/themarfa_gaming/30")
    assert link is not None
    assert link.chat_ref == "themarfa_gaming"
    assert link.thread_id == 30
    assert chat_ref_to_id("1234567890") == -1001234567890

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
        assert tr("start_welcome", loc)

    with tempfile.TemporaryDirectory() as tmp:
        db = SqliteDatabase(Path(tmp) / "test.db")
        db.upsert_user(1)
        assert db.get_user_locale(1) is None
        db.set_user_locale(1, "en")
        assert db.get_user_locale(1) == "en"
        sub_id = db.add_subscription(
            owner_id=1,
            twitch_username="ninja",
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
        assert db.update_subscription(sub_id, 1, message_template="bye")
        sub = db.get_subscription(sub_id, 1)
        assert sub is not None
        assert sub.message_template == "bye"
        assert not db.update_subscription(999, 1, message_template="x")

    items = fetch_render_status("https://status.render.com/history.rss")
    assert items
    assert any(is_planned_maintenance(i) for i in items[:3])

    print("ok")


if __name__ == "__main__":
    main()
