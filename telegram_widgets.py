"""Telegram HTMLBubbles (Bot API 10.4 sendWidget) for admin-facing digests.

User-facing cards (channel stats, alert history) stay on PNG/text — see docs/todo-htmlbubbles-*.md.
Uses the beta Bot API host by default — stable core.telegram.org has no sendWidget yet.
"""
from __future__ import annotations

import json
import logging
import zlib
from dataclasses import dataclass
from typing import Any

import requests
from telegram import InlineKeyboardMarkup

logger = logging.getLogger(__name__)

WIDGET_HTML_MAX = 32_768
WIDGET_ZLIB_MAX = 4_096
WIDGET_DATA_MAX = 1_024


@dataclass(frozen=True)
class WidgetSendResult:
    ok: bool
    message_id: int | None = None
    error: str = ""


def widget_zlib_size(html: str) -> int:
    return len(zlib.compress(html.encode("utf-8"), 9))


def assert_widget_limits(html: str, chat_data: str | None = None) -> None:
    raw = html.encode("utf-8")
    if len(raw) > WIDGET_HTML_MAX:
        raise ValueError(f"widget html {len(raw)} > {WIDGET_HTML_MAX}")
    packed = widget_zlib_size(html)
    if packed > WIDGET_ZLIB_MAX:
        raise ValueError(f"widget zlib {packed} > {WIDGET_ZLIB_MAX}")
    if chat_data is not None:
        n = len(chat_data.encode("utf-8"))
        if n > WIDGET_DATA_MAX:
            raise ValueError(f"chat_data {n} > {WIDGET_DATA_MAX}")


def html_widgets_enabled() -> bool:
    from config import ENABLE_HTML_WIDGETS

    return bool(ENABLE_HTML_WIDGETS)


def _widget_api_url(method: str) -> str:
    from config import TELEGRAM_BOT_TOKEN, TELEGRAM_WIDGET_API_ROOT

    root = (TELEGRAM_WIDGET_API_ROOT or "https://api.telegram.org/beta").rstrip("/")
    return f"{root}/bot{TELEGRAM_BOT_TOKEN}/{method}"


def _markup_payload(reply_markup: InlineKeyboardMarkup | None) -> str | None:
    if reply_markup is None:
        return None
    return json.dumps(reply_markup.to_dict(), ensure_ascii=False, separators=(",", ":"))


def send_widget(
    *,
    chat_id: int,
    html: str,
    chat_data: str | None = None,
    caption: str | None = None,
    parse_mode: str | None = "HTML",
    width: int = 0,
    height: int = 0,
    allow_queries: bool = False,
    reply_markup: InlineKeyboardMarkup | None = None,
    timeout: float = 20.0,
) -> WidgetSendResult:
    """POST sendWidget. Returns ok=False on any API/transport failure (caller falls back)."""
    if not html_widgets_enabled():
        return WidgetSendResult(ok=False, error="disabled")
    try:
        assert_widget_limits(html, chat_data)
    except ValueError as exc:
        logger.warning("sendWidget rejected locally: %s", exc)
        return WidgetSendResult(ok=False, error=str(exc))

    payload: dict[str, Any] = {
        "chat_id": int(chat_id),
        "html": html,
        "width": int(width),
        "height": int(height),
        "allow_queries": bool(allow_queries),
    }
    if chat_data is not None:
        payload["chat_data"] = chat_data
    if caption:
        payload["caption"] = caption
        if parse_mode:
            payload["parse_mode"] = parse_mode
    markup = _markup_payload(reply_markup)
    if markup:
        payload["reply_markup"] = markup

    try:
        resp = requests.post(
            _widget_api_url("sendWidget"),
            data=payload,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        logger.warning("sendWidget transport error: %s", type(exc).__name__)
        return WidgetSendResult(ok=False, error=type(exc).__name__)

    try:
        body = resp.json()
    except ValueError:
        return WidgetSendResult(ok=False, error=f"http_{resp.status_code}")

    if not body.get("ok"):
        desc = str(body.get("description") or f"http_{resp.status_code}")
        logger.info("sendWidget failed: %s", desc)
        return WidgetSendResult(ok=False, error=desc)

    result = body.get("result") or {}
    mid = result.get("message_id")
    try:
        message_id = int(mid) if mid is not None else None
    except (TypeError, ValueError):
        message_id = None
    return WidgetSendResult(ok=True, message_id=message_id)


# Compact shell: labels/numbers come only from chat_data (i18n in Python).

PREMIUM_DIGEST_WIDGET_HTML = (
    "<!DOCTYPE html><html><head><meta charset=utf-8><style>"
    "body{margin:0;padding:12px;font:14px/1.35 system-ui,sans-serif;"
    "color:var(--tg-theme-text-color)}"
    "h1{margin:0 0 10px;font-size:16px;font-weight:600}"
    ".chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}"
    ".chip{background:var(--tg-theme-secondary-bg-color);border-radius:10px;"
    "padding:6px 10px;font-size:13px;font-variant-numeric:tabular-nums}"
    ".chip b{font-weight:700}"
    "ul{margin:0;padding:0;list-style:none}"
    "li{padding:5px 0;border-top:1px solid var(--tg-theme-secondary-bg-color);"
    "font-size:12px;color:var(--tg-theme-hint-color)}"
    "li:first-child{border-top:0}"
    "</style></head><body>"
    "<h1 id=t></h1><div class=chips id=c></div><ul id=l></ul>"
    "<script>"
    "const T=window.TelegramWidget,J=s=>{try{return s?JSON.parse(s):{}}catch(e){return{}}};"
    "function paint(){const d=J(T&&T.chatData);"
    "document.getElementById('t').textContent=d.t||'';"
    "const c=document.getElementById('c');c.textContent='';"
    "(d.ch||[]).forEach(x=>{const e=document.createElement('div');e.className='chip';"
    "e.innerHTML='<b></b> ';e.children[0].textContent=x.v||'';"
    "e.appendChild(document.createTextNode(x.l||''));c.appendChild(e)});"
    "const ul=document.getElementById('l');ul.textContent='';"
    "(d.l||[]).forEach(x=>{const li=document.createElement('li');"
    "li.textContent=x||'';ul.appendChild(li)})}"
    "paint();if(T){T.onEvent('chatDataChanged',paint);T.ready&&T.ready()}"
    "</script></body></html>"
)
