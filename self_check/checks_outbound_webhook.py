"""Universal outbound webhook destination + Max subscriptions API client."""

from __future__ import annotations

from unittest.mock import MagicMock
from urllib.parse import urlparse

import max_api
import outbound_webhook as owh
from db.sqlite import SqliteDatabase


def check_outbound_webhook_validate_sign_ssrf() -> None:
    good = "https://hooks.example.com/alerts?x=1"
    assert owh.normalize_webhook_url(good) == good
    assert owh.normalize_webhook_url("http://hooks.example.com/x") is None
    assert owh.normalize_webhook_url("https://127.0.0.1/x") is None
    assert owh.normalize_webhook_url("https://192.168.1.1/x") is None
    assert owh.normalize_webhook_url("https://localhost/x") is None
    assert owh.normalize_webhook_url("") is None

    masked = owh.mask_webhook_url(good)
    assert urlparse(masked).hostname == "hooks.example.com"
    assert "***" in masked
    assert urlparse(masked).path != "/alerts"

    secret = owh.generate_secret()
    assert owh.normalize_secret(secret) == secret
    body = b'{"delivery_id":"d1"}'
    sig = owh.sign_body(body, secret)
    assert sig.startswith("sha256=")
    assert owh.verify_signature(body, secret, sig)
    assert not owh.verify_signature(body, secret, "sha256=deadbeef")

    payload = owh.build_payload(
        delivery_id="d1",
        subscription_id=7,
        owner_id=42,
        alert_type="live",
        twitch_login="MarfaPR",
        text="<b>hi</b>",
        text_format="html",
        photo_url="https://cdn.example/cover.jpg",
        buttons=[("Open", "https://twitch.tv/marfapr"), ("Bad", "javascript:x")],
    )
    assert payload["schema_version"] == owh.SCHEMA_VERSION
    assert payload["twitch_login"] == "marfapr"
    assert payload["photo_url"].startswith("https://")
    assert len(payload["buttons"]) == 1


def check_outbound_webhook_post_mock() -> None:
    url = "https://hooks.example.com/alerts"
    secret = owh.generate_secret()
    session = MagicMock()
    resp = MagicMock()
    resp.status_code = 204
    session.post.return_value = resp
    payload = owh.build_payload(
        delivery_id="abc",
        subscription_id=1,
        owner_id=2,
        alert_type="test",
        twitch_login="x",
        text="ping",
    )
    owh.post_webhook(url, secret=secret, payload=payload, session=session)
    assert session.post.called
    args, kwargs = session.post.call_args
    assert args[0] == url
    headers = kwargs["headers"]
    assert headers[owh.SIGNATURE_HEADER].startswith("sha256=")
    assert headers[owh.DELIVERY_ID_HEADER] == "abc"
    body = kwargs["data"]
    assert owh.verify_signature(body, secret, headers[owh.SIGNATURE_HEADER])


def check_outbound_webhook_subscription_roundtrip() -> None:
    import os
    import tempfile
    from pathlib import Path

    from token_crypto import try_decrypt_secret

    url = "https://hooks.example.com/twitch"
    secret = owh.generate_secret()
    with tempfile.TemporaryDirectory() as tmp:
        os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:selfcheck-webhook")
        db = SqliteDatabase(Path(tmp) / "bot.db")
        sid = db.add_subscription(
            owner_id=42,
            twitch_username="marfapr",
            twitch_user_id="123",
            message_template="{username} live",
            dest_type="webhook",
            chat_id=99,
            thread_id=7,
            webhook_url=url,
            webhook_secret=secret,
            pin_message=True,
            delete_previous=True,
        )
        sub = db.get_subscription_by_id(sid)
        assert sub is not None
        assert sub.dest_type == "webhook"
        assert sub.chat_id == 0
        assert sub.thread_id is None
        assert sub.pin_message is False
        assert sub.delete_previous is False
        assert sub.webhook_url.startswith("enc:v1:")
        assert sub.webhook_secret.startswith("enc:v1:")
        assert url not in sub.webhook_url
        assert secret not in sub.webhook_secret
        assert try_decrypt_secret(sub.webhook_url) == url
        assert try_decrypt_secret(sub.webhook_secret) == secret

        ok = db.update_subscription(
            sid,
            42,
            dest_type="dm",
            chat_id=42,
            thread_id=None,
            webhook_url="",
            webhook_secret="",
        )
        assert ok
        sub2 = db.get_subscription_by_id(sid)
        assert sub2 is not None
        assert sub2.dest_type == "dm"
        assert sub2.webhook_url == ""
        assert sub2.webhook_secret == ""


def check_outbound_webhook_docs_and_example_aligned() -> None:
    """Phase 2: sample receiver + docs stay on the same header/contract names."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    docs = (root / "docs" / "outbound-webhook.md").read_text(encoding="utf-8")
    example = (root / "examples" / "outbound_webhook_receiver.py").read_text(
        encoding="utf-8"
    )
    for token in (
        owh.SIGNATURE_HEADER,
        owh.DELIVERY_ID_HEADER,
        "schema_version",
        "delivery_id",
        "sha256=",
    ):
        assert token in docs, token
        assert token in example, token
    assert "hmac.compare_digest" in example
    assert str(owh.SCHEMA_VERSION) in docs

    from health import _markdown_doc_to_html, _webhook_docs_page

    rendered = _markdown_doc_to_html(docs)
    assert "<h1>" in rendered and "Outbound webhook" in rendered
    assert "<table>" in rendered and "<pre><code>" in rendered
    assert "<strong>Webhook</strong>" in rendered
    page = (_webhook_docs_page("en") or b"").decode("utf-8")
    assert "Outbound webhook" in page and "<table>" in page
    assert 'class="page-docs"' in page
    assert "/site/css/main.css" in page
    assert "white-space:pre-wrap" not in page
    assert (
        "github.com/Marfa/twitch-telegram-bot/blob/main/outbound_webhook.py"
        in page
    )


def check_max_api_subscribe_mock() -> None:
    session = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"success": True}
    session.post.return_value = resp
    out = max_api.subscribe(
        "https://bot.example.com/hooks/max",
        update_types=["message_created", "bot_started"],
        secret="abcDE-12",
        token="test-token",
        session=session,
    )
    assert out.get("success") is True
    assert session.post.called
    called_url = session.post.call_args.args[0]
    assert called_url.endswith("/subscriptions")
    headers = session.post.call_args.kwargs["headers"]
    assert headers["Authorization"] == "test-token"
    body = session.post.call_args.kwargs["json"]
    assert body["url"].startswith("https://")
    assert body["secret"] == "abcDE-12"

    bad = MagicMock()
    bad_resp = MagicMock()
    bad_resp.status_code = 200
    bad_resp.json.return_value = {
        "success": False,
        "code": "proto.payload",
        "message": "bad",
    }
    bad.post.return_value = bad_resp
    try:
        max_api.subscribe(
            "https://bot.example.com/hooks/max",
            token="t",
            session=bad,
        )
        assert False, "expected MaxApiError"
    except max_api.MaxApiError as exc:
        assert exc.code == "proto.payload"


def check_web_site_locale_helpers() -> None:
    import web_site

    assert web_site.lang_from_accept("ru-RU,ru;q=0.9,en;q=0.8") == "ru"
    assert web_site.lang_from_accept("it-IT,it;q=0.9") == "it"
    assert web_site.request_lang("uk", "en-US") == "uk"
    assert web_site.request_lang(None, "uk-UA,uk;q=0.8") == "uk"
    assert web_site.landing_page() is not None
    assert web_site.sitemap_bytes() is not None
    assert web_site.robots_bytes() is not None
    assert web_site.safe_file(web_site.SITE_DIR, "../health.py") is None
    assert web_site.safe_file(web_site.SITE_DIR, "css/main.css") is not None
    assert web_site.safe_file(web_site.SITE_DIR, "css/../../health.py") is None
    assert web_site.safe_file(web_site.SITE_DIR, "/etc/passwd") is None


def run() -> None:
    check_outbound_webhook_validate_sign_ssrf()
    check_outbound_webhook_post_mock()
    check_outbound_webhook_subscription_roundtrip()
    check_outbound_webhook_docs_and_example_aligned()
    check_web_site_locale_helpers()
    check_max_api_subscribe_mock()
