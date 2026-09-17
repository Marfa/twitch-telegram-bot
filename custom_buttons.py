"""Custom URL buttons under alert messages (Premium advanced_mode)."""
from __future__ import annotations

import json
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, WebAppInfo

CUSTOM_BUTTONS_MAX = 10
CUSTOM_BUTTON_TEXT_MAX = 64
BETA_FEATURE_ID = "custom-buttons"
FEATURE_ID = "custom_buttons"

# Bot API style values (empty = client default). Callback ids use "default" for empty.
BUTTON_STYLE_API = ("", "primary", "success", "danger")
BUTTON_STYLE_CHOICES = ("default", "primary", "success", "danger")
BUTTON_STYLE_LABEL_KEYS = {
    "default": "button_style_default",
    "primary": "button_style_primary",
    "success": "button_style_success",
    "danger": "button_style_danger",
}


def normalize_button_style(raw: str | None) -> str:
    """Return Bot API style ('primary'|'success'|'danger') or '' for default."""
    v = (raw or "").strip().lower()
    if v in ("primary", "success", "danger"):
        return v
    return ""


def button_style_choice_id(raw: str | None) -> str:
    style = normalize_button_style(raw)
    return style if style else "default"


def button_style_from_choice(choice: str | None) -> str:
    v = (choice or "").strip().lower()
    if v in ("primary", "success", "danger"):
        return v
    return ""


def styled_inline_button(
    text: str,
    *,
    url: str | None = None,
    web_app: WebAppInfo | None = None,
    style: str = "",
) -> InlineKeyboardButton:
    """Build an InlineKeyboardButton with optional Bot API 9.4 style."""
    kwargs: dict = {"text": text}
    if url is not None:
        kwargs["url"] = url
    if web_app is not None:
        kwargs["web_app"] = web_app
    api_style = normalize_button_style(style)
    if api_style:
        # api_kwargs works on PTB 21+; first-class style= arrived later.
        kwargs["api_kwargs"] = {"style": api_style}
    return InlineKeyboardButton(**kwargs)


def parse_custom_buttons(raw: str | None) -> list[dict[str, str]]:
    """Parse stored JSON into up to CUSTOM_BUTTONS_MAX {text, url} dicts."""
    if not raw or not str(raw).strip():
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        url = str(item.get("url") or "").strip()
        if not text or not url or not is_valid_button_url(url):
            continue
        out.append({"text": text[:CUSTOM_BUTTON_TEXT_MAX], "url": url})
        if len(out) >= CUSTOM_BUTTONS_MAX:
            break
    return out


def dump_custom_buttons(buttons: list[dict[str, str]] | None) -> str:
    cleaned = parse_custom_buttons(json.dumps(buttons or [], ensure_ascii=False))
    if not cleaned:
        return "[]"
    return json.dumps(cleaned, ensure_ascii=False)


def is_valid_button_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.netloc:
        return False
    return True


def parse_button_line(text: str) -> tuple[str, str] | None:
    """Parse 'Anchor | https://…' → (text, url) or None if invalid."""
    raw = (text or "").strip()
    if "|" not in raw:
        return None
    anchor, _, link = raw.partition("|")
    anchor = anchor.strip()
    link = link.strip()
    if not anchor or not link:
        return None
    if len(anchor) > CUSTOM_BUTTON_TEXT_MAX:
        anchor = anchor[:CUSTOM_BUTTON_TEXT_MAX]
    if not is_valid_button_url(link):
        return None
    return anchor, link


def chunk_buttons(items: list, *, per_row: int = 2) -> list[list]:
    """Pack buttons into fixed-width rows (default 2), last row may be short."""
    if per_row < 1:
        per_row = 2
    return [items[i : i + per_row] for i in range(0, len(items), per_row)]
