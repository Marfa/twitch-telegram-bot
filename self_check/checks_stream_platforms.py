"""External stream platform URL parse / identity helpers."""

from __future__ import annotations

import tempfile
from pathlib import Path

from stream_platforms import (
    channel_from_subscription,
    external_user_id,
    looks_like_external_stream_url,
    parse_external_stream_url,
    parse_external_user_id,
)


def check_external_stream_parse_urls() -> None:
    gg = parse_external_stream_url("https://goodgame.ru/Abver")
    assert gg is not None and gg.platform == "goodgame"

    vk = parse_external_stream_url("https://live.vkvideo.ru/play_code")
    assert vk is not None and vk.platform == "vkplay"

    yt = parse_external_stream_url(
        "https://www.youtube.com/channel/UCXuqSBlHAE6Xw-yeJA0Tunw"
    )
    assert yt is not None and yt.platform == "youtube"

    tt = parse_external_stream_url("https://www.tiktok.com/@someone/live")
    assert tt is not None and tt.platform == "tiktok" and tt.channel_id == "someone"

    kick = parse_external_stream_url("https://kick.com/xqc")
    assert kick is not None
    assert kick.platform == "kick"
    assert kick.channel_id == "xqc"
    assert kick.url == "https://kick.com/xqc"

    # Trovo is shut down — URLs must not parse as an external platform.
    assert parse_external_stream_url("https://trovo.live/SomeStreamer") is None
    assert parse_external_stream_url("https://trovo.live/s/SomeStreamer/123") is None
    assert not looks_like_external_stream_url("trovo.live/foo")

    assert parse_external_stream_url("https://example.com/nope") is None
    assert parse_external_stream_url("just-a-username") is None

    assert looks_like_external_stream_url("https://kick.com/xqc")
    assert not looks_like_external_stream_url("https://twitch.tv/marfapr")
    assert not looks_like_external_stream_url("marfapr")


def check_external_user_id_roundtrip() -> None:
    uid = external_user_id("kick", "xqc")
    assert uid == "ext:kick:xqc"
    parsed = parse_external_user_id(uid)
    assert parsed == ("kick", "xqc")
    assert parse_external_user_id("12345") is None
    assert parse_external_user_id("ext:unknown:x") is None
    assert parse_external_user_id("ext:trovo:x") is None

    ch = channel_from_subscription(
        stream_platform="kick",
        twitch_username="xqc",
        twitch_user_id="ext:kick:xqc",
        stream_url="https://kick.com/xqc",
    )
    assert ch is not None
    assert ch.platform == "kick"
    assert ch.channel_id == "xqc"


def check_external_live_stream_id_stable() -> None:
    """External go-live must use a stable stream id (no minute buckets / restarts)."""
    import ast
    import inspect

    from handlers import notifications as notif

    src = inspect.getsource(notif._check_external_live_alerts)
    # Regression: uid:{epoch//60} looked like a Helix restart every minute.
    assert "time.time()" not in src
    assert "// 60" not in src
    assert "stream_id_restart_uids" not in src
    # Stable id while online.
    assert '"id": uid' in src or "'id': uid" in src
    # Probe failures must not flip last_live (unknown set).
    assert "unknown" in src
    # Sanity: function still parses.
    ast.parse(src)


def check_external_subscription_db() -> None:
    from db import open_database

    with tempfile.TemporaryDirectory() as tmp:
        db = open_database(Path(tmp) / "ext.db")
        try:
            sub_id = db.add_subscription(
                owner_id=42,
                twitch_username="xqc",
                twitch_user_id="ext:kick:xqc",
                message_template="{username} live {url}",
                dest_type="dm",
                chat_id=42,
                thread_id=None,
                notify_on_live=True,
                stream_platform="kick",
                stream_url="https://kick.com/xqc",
            )
            sub = db.get_subscription_by_id(sub_id)
            assert sub is not None
            assert sub.stream_platform == "kick"
            assert sub.stream_url == "https://kick.com/xqc"
            assert "ext:kick:xqc" not in db.get_unique_twitch_user_ids()
            ext = db.get_enabled_external_live_subscriptions()
            assert any(s.id == sub_id for s in ext)
        finally:
            close = getattr(db, "close", None)
            if callable(close):
                close()
