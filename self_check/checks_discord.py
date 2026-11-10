"""Discord webhook destination helpers + DB dest_type=discord."""

from __future__ import annotations

from unittest.mock import MagicMock

import discord_webhook as dwh
from db.sqlite import SqliteDatabase


def check_discord_webhook_validate_mask_markdown() -> None:
    good = (
        "https://discord.com/api/webhooks/123456789012345678/"
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUV"
    )
    assert dwh.normalize_webhook_url(good) == good
    assert dwh.normalize_webhook_url(good + "/") == good
    legacy = good.replace("discord.com", "discordapp.com")
    assert dwh.normalize_webhook_url(legacy) == good
    assert dwh.normalize_webhook_url("https://example.com/hooks/1/x") is None
    assert dwh.normalize_webhook_url("") is None

    masked = dwh.mask_webhook_url(good)
    assert "123456789012345678" in masked
    assert "***" in masked
    assert "abcdefghijklmnopqrstuvwxyz" not in masked

    md = dwh.html_to_discord_markdown(
        '<b>Live</b> <i>now</i> <a href="https://twitch.tv/x">go</a>'
    )
    assert "**Live**" in md
    assert "_now_" in md
    assert "[go](https://twitch.tv/x)" in md

    comps = dwh.link_button_components(
        [("Open", "https://twitch.tv/x"), ("Bad", "javascript:alert(1)")]
    )
    assert len(comps) == 1
    assert comps[0]["components"][0]["style"] == 5
    assert comps[0]["components"][0]["url"] == "https://twitch.tv/x"


def check_discord_webhook_execute_mock() -> None:
    url = (
        "https://discord.com/api/webhooks/111/"
        "tokentokentokentokentokentokentokentoken"
    )
    session = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.content = b'{"id":"999"}'
    resp.json.return_value = {"id": "999"}
    session.post.return_value = resp
    msg = dwh.execute_webhook(url, content="hi", wait=True, session=session)
    assert msg == {"id": "999"}
    assert session.post.called
    called_url = session.post.call_args.args[0]
    assert "wait=true" in called_url.lower()
    # Full token must not appear in logs — we only assert it is in the request URL.
    assert "tokentoken" in called_url

    del_resp = MagicMock()
    del_resp.status_code = 204
    session.delete.return_value = del_resp
    assert dwh.delete_webhook_message(url, 999, session=session) is True


def check_discord_subscription_roundtrip() -> None:
    import os
    import tempfile
    from pathlib import Path

    from token_crypto import try_decrypt_secret

    url = (
        "https://discord.com/api/webhooks/222/"
        "secrettokensecrettokensecrettokensecretto"
    )
    with tempfile.TemporaryDirectory() as tmp:
        os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:selfcheck-discord")
        db = SqliteDatabase(Path(tmp) / "bot.db")
        sid = db.add_subscription(
            owner_id=42,
            twitch_username="marfapr",
            twitch_user_id="123",
            message_template="{username} live",
            dest_type="discord",
            chat_id=99,
            thread_id=7,
            discord_webhook=url,
            pin_message=True,
        )
        sub = db.get_subscription_by_id(sid)
        assert sub is not None
        assert sub.dest_type == "discord"
        assert sub.chat_id == 0
        assert sub.thread_id is None
        assert sub.pin_message is False
        assert sub.discord_webhook.startswith("enc:v1:")
        assert url not in sub.discord_webhook
        plain = try_decrypt_secret(sub.discord_webhook)
        assert plain == url

        ok = db.update_subscription(
            sid, 42, dest_type="dm", chat_id=42, thread_id=None, discord_webhook=""
        )
        assert ok
        sub2 = db.get_subscription_by_id(sid)
        assert sub2 is not None
        assert sub2.dest_type == "dm"
        assert sub2.discord_webhook == ""


def run() -> None:
    check_discord_webhook_validate_mask_markdown()
    check_discord_webhook_execute_mock()
    check_discord_subscription_roundtrip()
