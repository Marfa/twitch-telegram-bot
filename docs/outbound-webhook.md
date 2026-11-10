# Outbound webhook (alert destination)

Destination **Webhook** in the Telegram bot: after the alert is rendered, the bot `POST`s JSON to your HTTPS URL with an HMAC signature.

Source of truth for signing/payload shape: [`outbound_webhook.py`](../outbound_webhook.py).  
Product plan (Max thin bot): [`todo-max-messenger-bot.md`](todo-max-messenger-bot.md).

Runnable sample: [`examples/outbound_webhook_receiver.py`](../examples/outbound_webhook_receiver.py).

---

## Setup in the bot

1. Create / edit an alert → **Webhook**.
2. Paste a public `https://…` URL (no localhost / private IP).
3. Bot generates a secret, sends a test `POST`, then shows the secret **once** — store it on your side.
4. Live alerts use the same URL + secret.

Pin and delete-previous are not available for this destination.

---

## Request

| | |
|---|---|
| Method | `POST` |
| URL | your HTTPS endpoint (port 443) |
| Body | UTF-8 JSON (compact: no extra spaces) |
| Content-Type | `application/json; charset=utf-8` |
| Signature | `X-Bot-Signature: sha256=<hex>` — HMAC-SHA256 of the **raw body** with your secret |
| Delivery id | `X-Bot-Delivery-Id: <uuid>` (same as `delivery_id` in JSON) |
| User-Agent | `twitch-telegram-bot-webhook/1` |

Success: respond **`2xx` within ~20s**. Anything else is a delivery failure (bot may retry once on `429`; pauses destination on `401`/`403`/`404`/`410`).

### Signature

```text
X-Bot-Signature = "sha256=" + hex(HMAC_SHA256(secret, raw_body_bytes))
```

Verify with a constant-time compare. Reject if the header is missing or wrong.

### Idempotency

Treat `delivery_id` (header and/or JSON field) as unique. If you already accepted that id, return `200` again without re-sending to users.

---

## Payload (`schema_version: 1`)

```json
{
  "schema_version": 1,
  "delivery_id": "550e8400-e29b-41d4-a716-446655440000",
  "subscription_id": 42,
  "owner_id": 123456789,
  "alert_type": "live",
  "twitch_login": "marfapr",
  "twitch_user_id": "12345",
  "stream_id": "5123456789",
  "text": "<b>marfapr</b> is live — Just Chatting",
  "text_format": "html",
  "photo_url": "https://static-cdn.jtvnw.net/…",
  "buttons": [
    { "text": "Open", "url": "https://twitch.tv/marfapr" }
  ],
  "sent_at": "2026-11-10T15:00:00+00:00",
  "meta": {
    "vod_offset_seconds": null,
    "is_scheduled_repeat": false
  }
}
```

| Field | Notes |
|---|---|
| `alert_type` | e.g. `live`, `end`, `category`, `upcoming`, `test` (setup probe) |
| `text_format` | `plain` \| `html` \| `markdown` — matches how the Telegram template was rendered |
| `photo_url` | optional public image URL only (no multipart upload) |
| `buttons` | link buttons only (no callbacks) |
| `meta.test` | `true` on the connectivity probe before save |

---

## Verify (Python)

```python
import hashlib
import hmac
import json

SIGNATURE_HEADER = "X-Bot-Signature"
DELIVERY_ID_HEADER = "X-Bot-Delivery-Id"

def verify(raw_body: bytes, secret: str, signature_header: str) -> bool:
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, (signature_header or "").strip())

# In your handler:
# raw = request.get_data()          # raw bytes, not re-serialized JSON
# sig = request.headers.get(SIGNATURE_HEADER)
# if not verify(raw, WEBHOOK_SECRET, sig):
#     return "", 401
# payload = json.loads(raw)
# delivery_id = payload["delivery_id"]  # or DELIVERY_ID_HEADER
# if already_seen(delivery_id):
#     return "", 200
# process(payload); mark_seen(delivery_id)
# return "", 200
```

Or run the sample receiver:

```bash
export WEBHOOK_SECRET='paste-secret-from-bot'
python examples/outbound_webhook_receiver.py
# listens on http://127.0.0.1:8787/hooks/alerts — use a public HTTPS tunnel for real tests
```

---

## Not in this contract

- Raw Twitch EventSub / Helix events (alerts are already rendered)
- Callback / “manage in bot” buttons
- Discord Incoming Webhook format (`dest_type=discord` is separate)
