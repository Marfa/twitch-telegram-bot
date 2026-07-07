"""ponytail: minimal self-check for twitch parsing and templates."""
from links import parse_telegram_topic_link, chat_ref_to_id
from render_status import fetch_render_status, is_planned_maintenance
from twitch import TwitchClient, render_template


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

    items = fetch_render_status("https://status.render.com/history.rss")
    assert items
    assert any(is_planned_maintenance(i) for i in items[:3])

    print("ok")


if __name__ == "__main__":
    main()
