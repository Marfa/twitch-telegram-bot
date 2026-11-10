#!/usr/bin/env python3
"""Minimal HTTPS-ready sample for dest_type=webhook.

Verifies X-Bot-Signature (HMAC-SHA256 of raw body) and dedupes delivery_id.
Stdlib only. For local demos use http://127.0.0.1 — production needs public HTTPS
(e.g. ngrok / Caddy) matching the URL you paste in the Telegram bot.

  export WEBHOOK_SECRET='secret-from-bot'
  python examples/outbound_webhook_receiver.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

SIGNATURE_HEADER = "X-Bot-Signature"
DELIVERY_ID_HEADER = "X-Bot-Delivery-Id"
# Same constants as outbound_webhook.py — keep in sync.
_SEEN: set[str] = set()
_SECRET = (os.getenv("WEBHOOK_SECRET") or "").strip()
_HOST = (os.getenv("WEBHOOK_LISTEN_HOST") or "127.0.0.1").strip()
_PORT = int(os.getenv("WEBHOOK_LISTEN_PORT") or "8787")
_PATH = (os.getenv("WEBHOOK_PATH") or "/hooks/alerts").strip() or "/hooks/alerts"


def _verify(raw: bytes, header: str) -> bool:
    if not _SECRET:
        return False
    expected = "sha256=" + hmac.new(
        _SECRET.encode("utf-8"), raw, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, (header or "").strip())


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # quieter default
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != _PATH:
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        sig = self.headers.get(SIGNATURE_HEADER) or ""
        if not _verify(raw, sig):
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"invalid signature")
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"invalid json")
            return
        delivery_id = str(
            payload.get("delivery_id")
            or self.headers.get(DELIVERY_ID_HEADER)
            or ""
        ).strip()
        if delivery_id and delivery_id in _SEEN:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"duplicate ok")
            return
        if delivery_id:
            _SEEN.add(delivery_id)
            # ponytail: in-memory set only — ceiling ~process lifetime; upgrade → Redis/DB.
        # schema_version is part of the bot contract (see docs/outbound-webhook.md).
        _ = payload.get("schema_version")
        login = payload.get("twitch_login") or "?"
        alert = payload.get("alert_type") or "?"
        text = (payload.get("text") or "")[:120]
        print(f"accepted delivery_id={delivery_id!r} {alert} @{login}: {text}")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"outbound webhook receiver; POST " + _PATH.encode())


def main() -> int:
    if not _SECRET:
        print("Set WEBHOOK_SECRET to the secret shown by the bot.", file=sys.stderr)
        return 1
    server = HTTPServer((_HOST, _PORT), Handler)
    print(f"listening on http://{_HOST}:{_PORT}{_PATH}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
