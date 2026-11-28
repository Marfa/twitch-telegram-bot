"""Twitch Teams: URL parse, placeholders, subscription platform gate."""

from __future__ import annotations


def check_twitch_team_url_parse() -> None:
    from twitch import TwitchClient, render_template

    client = TwitchClient.__new__(TwitchClient)
    assert TwitchClient.parse_team_name("https://www.twitch.tv/team/livecoders") == (
        "livecoders"
    )
    assert TwitchClient.parse_team_name("https://m.twitch.tv/team/Foo_Bar") == "foo_bar"
    assert TwitchClient.parse_team_name("https://www.twitch.tv/marfapr") is None
    assert TwitchClient.parse_team_name("livecoders") is None

    # /team/… must not be mistaken for channel login "team".
    assert client.parse_username("https://www.twitch.tv/team/livecoders") is None
    assert client.parse_username("https://www.twitch.tv/marfapr") == "marfapr"
    assert client.parse_username("team") is None

    assert TwitchClient.team_subscription_user_id("6358") == "team:6358"
    assert TwitchClient.is_team_user_id("team:6358")
    assert not TwitchClient.is_team_user_id("12345")
    assert TwitchClient.team_page_url("livecoders") == (
        "https://www.twitch.tv/team/livecoders"
    )

    text = render_template(
        "{team_name}: {team_member} ({username}) {game}",
        "shroud",
        "Valorant",
        "ranked",
        extra={"team_name": "Live Coders", "team_member": "shroud"},
    )
    assert text == "Live Coders: shroud (shroud) Valorant"


def check_twitch_team_not_external() -> None:
    from stream_platforms import is_external_subscription, is_twitch_team_subscription

    assert is_twitch_team_subscription(stream_platform="twitch_team")
    assert not is_external_subscription(
        stream_platform="twitch_team", twitch_user_id="team:1"
    )
    assert is_external_subscription(stream_platform="kick", twitch_user_id="ext:kick:x")


def check_twitch_team_db_roundtrip() -> None:
    import tempfile
    from pathlib import Path

    from db.sqlite import SqliteDatabase

    with tempfile.TemporaryDirectory() as tmp:
        db = SqliteDatabase(Path(tmp) / "t.db")
        try:
            sub_id = db.add_subscription(
                owner_id=7,
                twitch_username="LiveCoders",
                twitch_user_id="team:6358",
                message_template="{team_name} → {team_member}",
                dest_type="dm",
                chat_id=7,
                thread_id=None,
                notify_on_live=True,
                stream_platform="twitch_team",
                stream_url="https://www.twitch.tv/team/livecoders",
            )
            sub = db.get_subscription_by_id(sub_id)
            assert sub is not None
            assert sub.stream_platform == "twitch_team"
            assert sub.twitch_username == "livecoders"
            assert "team:6358" not in db.get_unique_twitch_user_ids()
            assert not any(s.id == sub_id for s in db.get_enabled_external_live_subscriptions())
            teams = db.get_enabled_team_subscriptions()
            assert any(s.id == sub_id for s in teams)
        finally:
            close = getattr(db, "close", None)
            if callable(close):
                close()
