"""Hugging Face chat completions for alert templates (same pattern as ghost-text-prepper)."""

from __future__ import annotations

import logging
import re

import requests

from config import HF_TEXT_MODEL, HF_TOKEN

logger = logging.getLogger(__name__)

_HF_CHAT_URL = "https://router.huggingface.co/v1/chat/completions"
_PLACEHOLDERS = ("{username}", "{game}", "{name}")


def generate_alert_template(*, locale: str, channel: str = "") -> str:
    """Generate a Twitch live-alert template with {username}/{game}/{name}."""
    if not HF_TOKEN:
        raise RuntimeError("HF_TOKEN / HUGGING_FACE_API is not configured")
    if not HF_TOKEN.startswith("hf_"):
        raise RuntimeError("HF token must start with hf_")

    lang_name = "Russian" if str(locale).lower().startswith("ru") else "English"
    system = (
        "You write Telegram notification templates for Twitch live-stream alerts. "
        "Reply with ONLY the template text — no quotes, no markdown fences, no explanation. "
        "You MUST include these placeholders exactly (with curly braces): "
        "{username}, {game}, {name}. "
        "Keep it short: 2–4 lines. Match the requested language."
    )
    user = (
        f"Language: {lang_name}\n"
        f"Twitch channel (for context only, still use {{username}} placeholder): {channel or 'streamer'}\n"
        "Write a lively live announcement template."
    )
    response = requests.post(
        _HF_CHAT_URL,
        headers={
            "Authorization": f"Bearer {HF_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "model": HF_TEXT_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": 200,
            "temperature": 0.8,
        },
        timeout=90,
    )
    if response.status_code >= 400:
        logger.warning(
            "HF chat completions failed: %s %s",
            response.status_code,
            response.text[:300],
        )
        response.raise_for_status()

    data = response.json()
    text = (
        ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    ).strip()
    return _normalize_template(text)


def _normalize_template(text: str) -> str:
    text = text.strip().strip("`")
    text = re.sub(r"^```(?:\w+)?\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    text = text.strip().strip('"').strip("'")
    # Ensure required placeholders exist even if the model drops one.
    missing = [p for p in _PLACEHOLDERS if p not in text]
    if missing:
        extras = "\n".join(missing)
        text = f"{text}\n{extras}".strip() if text else "\n".join(_PLACEHOLDERS)
    return text
