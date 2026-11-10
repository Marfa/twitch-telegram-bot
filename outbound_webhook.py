"""Universal HTTPS webhook destination (JSON + HMAC).

Alert delivery contract for dest_type=webhook. Never log full URLs or secrets —
use mask_webhook_url / mask_secret.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import socket
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-Bot-Signature"
DELIVERY_ID_HEADER = "X-Bot-Delivery-Id"
SCHEMA_VERSION = 1
_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{16,256}$")
_HOST_IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


class OutboundWebhookError(Exception):
    """POST failed; ``status_code`` set when the peer responded."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def generate_secret() -> str:
    """URL-safe secret for HMAC (fits Max-style charset too)."""
    return secrets.token_urlsafe(32)


def normalize_secret(raw: str) -> str | None:
    text = (raw or "").strip()
    if not text or not _SECRET_RE.fullmatch(text):
        return None
    return text


def normalize_webhook_url(raw: str) -> str | None:
    """Return canonical https URL or None if invalid / SSRF-blocked."""
    text = (raw or "").strip()
    if not text or len(text) > 2048:
        return None
    try:
        parts = urlparse(text)
    except Exception:
        return None
    if parts.scheme.lower() != "https":
        return None
    if parts.username or parts.password:
        return None
    host = (parts.hostname or "").strip().lower()
    if not host or host == "localhost" or host.endswith(".localhost"):
        return None
    if parts.port not in (None, 443):
        return None
    if _is_blocked_host(host):
        return None
    # Drop fragment; keep query/path as provided.
    path = parts.path or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"https://{host}{path}{query}"


def _is_blocked_host(host: str) -> bool:
    if host in ("metadata.google.internal", "metadata"):
        return True
    if _HOST_IP_RE.match(host):
        return _is_blocked_ip(host)
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        # Unresolvable now — still allow save; delivery will fail later.
        return False
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        ip = sockaddr[0]
        if _is_blocked_ip(ip):
            return True
    return False


def _is_blocked_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def mask_webhook_url(url: str) -> str:
    """UI/log-safe mask: ``https://host/***`."""
    text = (url or "").strip()
    try:
        parts = urlparse(text)
    except Exception:
        return "Webhook"
    host = (parts.hostname or "").strip() or "?"
    return f"https://{host}/***"


def mask_secret(secret: str) -> str:
    text = (secret or "").strip()
    if len(text) <= 8:
        return "***"
    return f"{text[:4]}…{text[-4:]}"


def sign_body(body: bytes, secret: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return f"sha256={digest}"


def verify_signature(body: bytes, secret: str, header: str) -> bool:
    expected = sign_body(body, secret)
    got = (header or "").strip()
    return hmac.compare_digest(expected, got)


def build_payload(
    *,
    delivery_id: str,
    subscription_id: int,
    owner_id: int,
    alert_type: str,
    twitch_login: str,
    twitch_user_id: str = "",
    stream_id: str = "",
    text: str = "",
    text_format: str = "plain",
    photo_url: str | None = None,
    buttons: list[tuple[str, str]] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fmt = (text_format or "plain").strip().lower()
    if fmt not in ("plain", "html", "markdown"):
        fmt = "plain"
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "delivery_id": delivery_id,
        "subscription_id": int(subscription_id),
        "owner_id": int(owner_id),
        "alert_type": str(alert_type or ""),
        "twitch_login": str(twitch_login or "").strip().lstrip("@").lower(),
        "twitch_user_id": str(twitch_user_id or ""),
        "stream_id": str(stream_id or ""),
        "text": text or "",
        "text_format": fmt,
        "buttons": [
            {"text": (label or "")[:64], "url": url}
            for label, url in (buttons or [])
            if (url or "").startswith(("http://", "https://"))
        ],
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
    if photo_url and str(photo_url).startswith(("http://", "https://")):
        payload["photo_url"] = str(photo_url)
    if meta:
        payload["meta"] = meta
    return payload


def post_webhook(
    webhook_url: str,
    *,
    secret: str,
    payload: dict[str, Any],
    session: requests.Session | None = None,
    timeout: float = 20.0,
) -> None:
    """POST JSON with HMAC. Raises OutboundWebhookError on failure."""
    url = normalize_webhook_url(webhook_url)
    if not url:
        raise OutboundWebhookError("invalid webhook url")
    sec = normalize_secret(secret)
    if not sec:
        raise OutboundWebhookError("invalid webhook secret")
    delivery_id = str(payload.get("delivery_id") or "").strip()
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "twitch-telegram-bot-webhook/1",
        SIGNATURE_HEADER: sign_body(body, sec),
    }
    if delivery_id:
        headers[DELIVERY_ID_HEADER] = delivery_id

    http = session or requests
    try:
        resp = http.post(url, data=body, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise OutboundWebhookError(str(exc)) from exc

    if 200 <= resp.status_code < 300:
        return
    if resp.status_code == 429:
        raise OutboundWebhookError("rate limited", status_code=429)
    raise OutboundWebhookError(
        f"webhook http {resp.status_code}",
        status_code=resp.status_code,
    )


def test_webhook(
    webhook_url: str,
    *,
    secret: str,
    text: str,
    session: requests.Session | None = None,
) -> Exception | None:
    """Probe with a minimal payload. None = ok."""
    from uuid import uuid4

    payload = build_payload(
        delivery_id=str(uuid4()),
        subscription_id=0,
        owner_id=0,
        alert_type="test",
        twitch_login="",
        text=text,
        text_format="plain",
        meta={"test": True},
    )
    try:
        post_webhook(webhook_url, secret=secret, payload=payload, session=session)
    except OutboundWebhookError as exc:
        return exc
    return None
