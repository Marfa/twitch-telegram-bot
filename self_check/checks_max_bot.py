"""Self-check: Max thin bot tables, ingest HMAC, subscribe bootstrap (mocked)."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from db.sqlite import SqliteDatabase
from outbound_webhook import SIGNATURE_HEADER, DELIVERY_ID_HEADER, sign_body
from token_crypto import encrypt_secret


def run() -> None:
    _check_crud_and_ingest()
    _check_bootstrap_subscribe()
    _check_parse_ingest_path()


def _check_crud_and_ingest() -> None:
    import max_bot
    from outbound_webhook import generate_secret

    with tempfile.TemporaryDirectory() as tmp:
        db = SqliteDatabase(Path(tmp) / "max.db")
        max_bot.configure(db)
        user = db.upsert_max_user(42, username="alice", lang="ru")
        assert user.max_user_id == 42
        assert db.get_max_user(42) is not None

        plain = generate_secret()
        dest = db.create_max_destination(
            42,
            kind="dm",
            public_id="pubTestId123456",
            webhook_secret=encrypt_secret(plain),
        )
        assert db.get_max_dm_destination(42) is not None
        assert db.get_max_destination_by_public_id("pubTestId123456") is not None

        code = db.create_max_link_code(
            code="ABCD1234",
            max_user_id=42,
            dest_id=dest.id,
            expires_at="2099-01-01T00:00:00+00:00",
        )
        assert db.get_max_link_code("ABCD1234") is not None
        assert code.dest_id == dest.id

        payload = {
            "schema_version": 1,
            "delivery_id": "deliv-1",
            "text": "<b>hi</b>",
            "text_format": "html",
            "buttons": [{"text": "Open", "url": "https://twitch.tv/x"}],
        }
        raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
        sig = sign_body(raw, plain)
        headers = {SIGNATURE_HEADER: sig, DELIVERY_ID_HEADER: "deliv-1"}

        with patch.object(max_bot.max_api, "send_message", return_value={"ok": True}) as send:
            status, body = max_bot.handle_ingest_post(
                "pubTestId123456", headers=headers, body=raw
            )
            assert status == 200
            assert send.called
            sent_text = send.call_args.kwargs.get("text") or send.call_args[1].get("text")
            if sent_text is None and send.call_args.args:
                # keyword-only API
                sent_text = send.call_args.kwargs["text"]
            assert "hi" in (sent_text or "")
            assert "<b>" not in (sent_text or "")

        # Idempotent second delivery
        with patch.object(max_bot.max_api, "send_message", return_value={"ok": True}) as send2:
            status2, _ = max_bot.handle_ingest_post(
                "pubTestId123456", headers=headers, body=raw
            )
            assert status2 == 200
            assert not send2.called

        # Bad signature
        bad_headers = {SIGNATURE_HEADER: "sha256=deadbeef", DELIVERY_ID_HEADER: "deliv-2"}
        status3, _ = max_bot.handle_ingest_post(
            "pubTestId123456", headers=bad_headers, body=raw
        )
        assert status3 == 401

        # Updates: unauthorized without secret
        test_updates_secret = "maxupd_" + ("x" * 24)
        with patch.object(max_bot, "updates_secret", return_value=test_updates_secret):
            st, _ = max_bot.handle_updates_post(
                headers={"X-Max-Bot-Api-Secret": "wrong"},
                body=b"{}",
            )
            assert st == 401

            with patch.object(max_bot, "_onboard_user") as onboard:
                body_u = json.dumps(
                    {
                        "update_type": "bot_started",
                        "user": {"user_id": 99, "username": "bob"},
                        "user_locale": "en",
                    }
                ).encode("utf-8")
                st2, _ = max_bot.handle_updates_post(
                    headers={"X-Max-Bot-Api-Secret": test_updates_secret},
                    body=body_u,
                )
                assert st2 == 200
                assert onboard.called


def _check_bootstrap_subscribe() -> None:
    import max_bot

    with patch.object(max_bot, "updates_callback_url", return_value="https://twitchbot.games/hooks/max/updates"):
        with patch.object(max_bot, "updates_secret", return_value="maxupd_" + ("y" * 24)):
            with patch.dict("os.environ", {"MAX_BOT_TOKEN": "tok"}):
                with patch("config.MAX_BOT_TOKEN", "tok"):
                    with patch.object(
                        max_bot.max_api, "list_subscriptions", return_value=[]
                    ):
                        with patch.object(
                            max_bot.max_api, "subscribe", return_value={"success": True}
                        ) as sub:
                            assert max_bot.bootstrap_max_subscription() is True
                            assert sub.called
                            assert (
                                sub.call_args.args[0]
                                == "https://twitchbot.games/hooks/max/updates"
                            )


def _check_parse_ingest_path() -> None:
    import max_bot

    assert max_bot.parse_ingest_public_id("/hooks/max/d/abc_DEF-12") == "abc_DEF-12"
    assert max_bot.parse_ingest_public_id("/hooks/max/d/bad/extra") is None
    assert max_bot.parse_ingest_public_id("/hooks/max/updates") is None
