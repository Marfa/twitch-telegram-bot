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
    _check_ca_bundle()
    _check_crud_and_ingest()
    _check_bootstrap_subscribe()
    _check_help_and_commands_payload()
    _check_payload_plain_text_no_redos()
    _check_parse_ingest_path()
    _check_onboard_menu_and_callbacks()
    with tempfile.TemporaryDirectory() as tmp:
        db = SqliteDatabase(Path(tmp) / "max_link.db")
        _check_resolve_link_code(db)
        _check_chat_destination_and_permissions(db)


def _check_ca_bundle() -> None:
    from pathlib import Path

    import max_api

    root = Path(max_api.__file__).resolve().parent / "certs" / "russian_trusted_root_ca.pem"
    assert root.is_file(), f"missing {root}"
    text = root.read_text(encoding="utf-8")
    assert "BEGIN CERTIFICATE" in text
    verify = max_api._tls_verify()
    assert isinstance(verify, str) and Path(verify).is_file()



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
                            with patch.object(
                                max_bot.max_api,
                                "set_my_commands",
                                return_value={"commands": []},
                            ) as cmds:
                                assert max_bot.bootstrap_max_subscription() is True
                                assert sub.called
                                assert cmds.called
                                names = [
                                    c["name"] for c in cmds.call_args.args[0]
                                ]
                                assert names == [
                                    "start",
                                    "link",
                                    "list",
                                    "unlink",
                                    "help",
                                ]
                                assert (
                                    sub.call_args.args[0]
                                    == "https://twitchbot.games/hooks/max/updates"
                                )
                                types = sub.call_args.kwargs.get("update_types") or []
                                assert "message_callback" in types


def _check_help_and_commands_payload() -> None:
    import max_bot

    cmds = max_bot._bot_commands_payload(lang="ru")
    assert [c["name"] for c in cmds] == [
        "start",
        "link",
        "list",
        "unlink",
        "help",
    ]
    assert all(c.get("description") for c in cmds)
    help_ru = max_bot._help_text("ru")
    assert "/start" in help_ru and "/link" in help_ru and "/list" in help_ru
    assert "/unlink" in help_ru and "/help" in help_ru
    assert "Меню:" in help_ru
    help_en = max_bot._help_text("en")
    assert "Available commands:" in help_en
    assert "Menu:" in help_en


def _check_payload_plain_text_no_redos() -> None:
    """HTML/markdown → plain must stay linear (CodeQL py/polynomial-redos #76/#77)."""
    import max_bot

    html_plain = max_bot._payload_to_plain_text(
        {"text": "<b>hi</b> &amp; you", "text_format": "html"}
    )
    assert html_plain == "hi & you"
    # Many '<' without '>' must not hang (old <[^>]+> ReDoS shape).
    nasty = "<" * 50_000
    assert max_bot._payload_to_plain_text(
        {"text": nasty, "text_format": "html"}
    ) == nasty
    md_plain = max_bot._payload_to_plain_text(
        {"text": "**bold** and *italic* and `code`", "text_format": "markdown"}
    )
    assert md_plain == "bold and italic and code"
    # Long '*'+'a'*n with no closer — old italic regex ReDoS shape; must finish fast.
    star_bomb = "*" + ("a" * 20_000)
    assert (
        max_bot._payload_to_plain_text(
            {"text": star_bomb, "text_format": "markdown"}
        )
        == star_bomb
    )


def _check_onboard_menu_and_callbacks() -> None:
    import max_bot

    assert "message_callback" in max_bot._UPDATE_TYPES

    text, atts = max_bot._menu_start("ru")
    assert "Выберите" in text
    assert "Код связи" not in text
    payloads = {
        b.get("payload")
        for att in atts
        for row in (att.get("payload") or {}).get("buttons") or []
        for b in row
        if isinstance(b, dict)
    }
    assert max_bot._CB_HAVE in payloads
    assert max_bot._CB_NEW in payloads

    new_text, new_atts = max_bot._menu_new("ru")
    assert "Telegram" in new_text
    assert any(
        b.get("type") == "link"
        for att in new_atts
        for row in (att.get("payload") or {}).get("buttons") or []
        for b in row
        if isinstance(b, dict)
    )

    with tempfile.TemporaryDirectory() as tmp:
        db = SqliteDatabase(Path(tmp) / "max_onboard.db")
        max_bot.configure(db)

        with patch.object(
            max_bot.max_api, "send_message", return_value={"ok": True}
        ) as send:
            max_bot._onboard_user({"user_id": 77, "username": "neo"}, locale="ru")
            assert send.called
            kwargs = send.call_args.kwargs
            assert "attachments" in kwargs
            assert db.get_max_dm_destination(77) is None

        with patch.object(
            max_bot.max_api, "answer_callback", return_value={"success": True}
        ) as ans:
            max_bot._on_message_callback(
                {
                    "update_type": "message_callback",
                    "user_locale": "ru",
                    "callback": {
                        "callback_id": "cb-1",
                        "payload": max_bot._CB_HAVE,
                        "user": {"user_id": 77, "username": "neo"},
                    },
                }
            )
            assert ans.called
            msg = ans.call_args.kwargs.get("message") or {}
            assert "код связи" in (msg.get("text") or "").lower()
            assert db.get_max_dm_destination(77) is not None
            assert db.list_max_destinations(77)


def _check_parse_ingest_path() -> None:
    import max_bot

    assert max_bot.parse_ingest_public_id("/hooks/max/d/abc_DEF-12") == "abc_DEF-12"
    assert max_bot.parse_ingest_public_id("/hooks/max/d/bad/extra") is None
    assert max_bot.parse_ingest_public_id("/hooks/max/updates") is None

    with patch("config.PUBLIC_BASE_URL", "https://twitchbot.games"):
        assert (
            max_bot.our_ingest_public_id_from_url(
                "https://twitchbot.games/hooks/max/d/abc_DEF-12"
            )
            == "abc_DEF-12"
        )
        assert (
            max_bot.our_ingest_public_id_from_url(
                "https://evil.example/hooks/max/d/abc_DEF-12"
            )
            is None
        )


def _check_resolve_link_code(db) -> None:
    import max_bot
    from datetime import datetime, timedelta, timezone
    from token_crypto import encrypt_secret

    db.upsert_max_user(4242, username="u", lang="ru")
    dest = db.create_max_destination(
        4242,
        kind="dm",
        chat_id=None,
        public_id="pidLinkCode01",
        webhook_secret=encrypt_secret("secret_ok_link_code_abc"),
        enabled=True,
    )
    expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    db.create_max_link_code(
        code="ABCD1234",
        max_user_id=4242,
        dest_id=dest.id,
        expires_at=expires,
    )
    got, err = max_bot.resolve_link_code(db, "abcd1234")
    assert err is None and got is not None
    assert int(got.max_user_id) == 4242
    assert db.consume_max_link_code("ABCD1234") is True
    got2, err2 = max_bot.resolve_link_code(db, "ABCD1234")
    assert got2 is None and err2 == "max_code_used"


def _check_chat_destination_and_permissions(db) -> None:
    import max_bot
    from token_crypto import encrypt_secret

    db.upsert_max_user(777, username="adder", lang="ru")
    chat = db.create_max_destination(
        777,
        kind="chat",
        chat_id=-100500,
        public_id="pidChatDest01xx",
        webhook_secret=encrypt_secret("secret_ok_chat_dest_xyz"),
        enabled=True,
    )
    assert db.get_max_destination_by_chat_id(-100500) is not None
    uid, cid = max_bot.peer_ids_for_destination(chat)
    assert uid is None and cid == -100500
    assert db.set_max_destination_enabled(chat.id, False) is True
    assert db.get_max_destination_by_chat_id(-100500).enabled is False

    import max_api

    assert max_api.bot_can_post({"is_admin": False}, is_channel=False) is True
    assert (
        max_api.bot_can_post(
            {"is_admin": True, "permissions": ["write"]}, is_channel=True
        )
        is True
    )
    assert (
        max_api.bot_can_post(
            {"is_admin": False, "permissions": []}, is_channel=True
        )
        is False
    )
