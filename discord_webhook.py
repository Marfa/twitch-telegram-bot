"""Discord Incoming Webhook helpers (alert destination).

Never log or return full webhook URLs — use mask_webhook_url.
"""
from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

logger = logging.getLogger(__name__)

_WEBHOOK_RE = re.compile(
    r"^https://(?:discord|discordapp)\.com/api/webhooks/"
    r"(?P<id>\d+)/(?P<token>[A-Za-z0-9_-]+)(?:\?.*)?/?$",
    re.IGNORECASE,
)
_MAX_CONTENT = 2000
MAX_FILE_BYTES = 25 * 1024 * 1024
_MAX_FILE_BYTES = MAX_FILE_BYTES
_MAX_BUTTONS = 25
_BUTTONS_PER_ROW = 5


class DiscordWebhookError(Exception):
    """Webhook call failed; ``status_code`` set when Discord responded."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def normalize_webhook_url(raw: str) -> str | None:
    """Return canonical webhook URL or None if invalid."""
    text = (raw or "").strip()
    if not text:
        return None
    m = _WEBHOOK_RE.match(text)
    if not m:
        return None
    return (
        f"https://discord.com/api/webhooks/{m.group('id')}/{m.group('token')}"
    )


def mask_webhook_url(url: str) -> str:
    """UI/log-safe mask: ``…/webhooks/{id}/***``."""
    m = _WEBHOOK_RE.match((url or "").strip())
    if not m:
        return "Discord"
    return f"…/webhooks/{m.group('id')}/***"


def html_to_discord_markdown(html: str) -> str:
    """Convert Telegram HTML alert text to Discord-flavoured markdown."""

    class _Converter(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.parts: list[str] = []
            self._href: list[str | None] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            t = tag.lower()
            if t in ("b", "strong"):
                self.parts.append("**")
            elif t in ("i", "em"):
                self.parts.append("_")
            elif t == "u":
                self.parts.append("__")
            elif t == "s" or t == "strike" or t == "del":
                self.parts.append("~~")
            elif t == "code":
                self.parts.append("`")
            elif t == "pre":
                self.parts.append("```\n")
            elif t == "a":
                href = None
                for k, v in attrs:
                    if k.lower() == "href" and v:
                        href = v
                        break
                self._href.append(href)
                self.parts.append("[")
            elif t == "br":
                self.parts.append("\n")

        def handle_endtag(self, tag: str) -> None:
            t = tag.lower()
            if t in ("b", "strong"):
                self.parts.append("**")
            elif t in ("i", "em"):
                self.parts.append("_")
            elif t == "u":
                self.parts.append("__")
            elif t == "s" or t == "strike" or t == "del":
                self.parts.append("~~")
            elif t == "code":
                self.parts.append("`")
            elif t == "pre":
                self.parts.append("\n```")
            elif t == "a":
                href = self._href.pop() if self._href else None
                self.parts.append("]")
                if href:
                    self.parts.append(f"({href})")

        def handle_data(self, data: str) -> None:
            self.parts.append(data)

    parser = _Converter()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:
        # ponytail: broken HTML → strip tags naively
        return re.sub(r"<[^>]+>", "", html or "")
    text = "".join(parser.parts)
    if len(text) > _MAX_CONTENT:
        text = text[: _MAX_CONTENT - 1] + "…"
    return text


def link_button_components(
    buttons: list[tuple[str, str]],
) -> list[dict[str, Any]]:
    """Build Discord action rows of link buttons (style 5)."""
    rows: list[dict[str, Any]] = []
    chunk: list[dict[str, Any]] = []
    for label, url in buttons[:_MAX_BUTTONS]:
        text = (label or "")[:80].strip() or "Link"
        href = (url or "").strip()
        if not href.startswith(("http://", "https://", "discord://")):
            continue
        chunk.append(
            {
                "type": 2,
                "style": 5,
                "label": text,
                "url": href,
            }
        )
        if len(chunk) >= _BUTTONS_PER_ROW:
            rows.append({"type": 1, "components": chunk})
            chunk = []
    if chunk:
        rows.append({"type": 1, "components": chunk})
    return rows


def _with_query(url: str, **params: Any) -> str:
    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key, value in params.items():
        if value is None:
            q.pop(key, None)
        else:
            q[key] = str(value).lower() if isinstance(value, bool) else str(value)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment)
    )


def execute_webhook(
    webhook_url: str,
    *,
    content: str = "",
    embeds: list[dict[str, Any]] | None = None,
    components: list[dict[str, Any]] | None = None,
    file_bytes: bytes | None = None,
    filename: str = "file.bin",
    wait: bool = True,
    session: requests.Session | None = None,
) -> dict[str, Any] | None:
    """POST Execute Webhook. Returns message dict when wait=True."""
    url = normalize_webhook_url(webhook_url)
    if not url:
        raise DiscordWebhookError("invalid webhook url")
    if file_bytes is not None and len(file_bytes) > _MAX_FILE_BYTES:
        raise DiscordWebhookError("file too large", status_code=413)

    query: dict[str, Any] = {}
    if wait:
        query["wait"] = True
    if components:
        query["with_components"] = True
    endpoint = _with_query(url, **query) if query else url

    payload: dict[str, Any] = {}
    text = (content or "")[:_MAX_CONTENT]
    if text:
        payload["content"] = text
    if embeds:
        payload["embeds"] = embeds[:10]
    if components:
        payload["components"] = components

    if not payload.get("content") and not payload.get("embeds") and not file_bytes:
        payload["content"] = "·"

    http = session or requests
    try:
        if file_bytes is not None:
            import json as _json

            resp = http.post(
                endpoint,
                data={"payload_json": _json.dumps(payload)},
                files={"files[0]": (filename, file_bytes)},
                timeout=30,
            )
        else:
            resp = http.post(endpoint, json=payload, timeout=30)
    except requests.RequestException as exc:
        raise DiscordWebhookError(str(exc)) from exc

    if resp.status_code == 429:
        retry_after = 1.0
        try:
            retry_after = float(resp.json().get("retry_after") or 1.0)
        except Exception:
            pass
        raise DiscordWebhookError(
            f"rate limited retry_after={retry_after}",
            status_code=429,
        )

    if resp.status_code in (401, 404):
        raise DiscordWebhookError("webhook unreachable", status_code=resp.status_code)

    if resp.status_code >= 400:
        raise DiscordWebhookError(
            f"discord http {resp.status_code}",
            status_code=resp.status_code,
        )

    if not wait or resp.status_code == 204 or not resp.content:
        return None
    try:
        data = resp.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def delete_webhook_message(
    webhook_url: str,
    message_id: int | str,
    *,
    session: requests.Session | None = None,
) -> bool:
    """DELETE previous webhook message. True if gone or already missing."""
    url = normalize_webhook_url(webhook_url)
    if not url or not message_id:
        return False
    endpoint = f"{url}/messages/{message_id}"
    http = session or requests
    try:
        resp = http.delete(endpoint, timeout=20)
    except requests.RequestException as exc:
        logger.warning("Discord delete failed: %s", type(exc).__name__)
        return False
    if resp.status_code in (204, 404):
        return True
    if resp.status_code in (401,):
        return False
    logger.warning("Discord delete status=%s", resp.status_code)
    return False


def test_webhook(
    webhook_url: str,
    *,
    text: str,
    session: requests.Session | None = None,
) -> Exception | None:
    """Probe webhook with a short message then delete it. None = ok."""
    try:
        msg = execute_webhook(
            webhook_url, content=text, wait=True, session=session
        )
    except DiscordWebhookError as exc:
        return exc
    mid = (msg or {}).get("id") if isinstance(msg, dict) else None
    if mid:
        delete_webhook_message(webhook_url, mid, session=session)
    return None
