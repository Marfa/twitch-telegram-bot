"""Hugging Face chat completions for alert templates (same pattern as ghost-text-prepper)."""

from __future__ import annotations

import logging
import os
import random
import re

import requests

from config import HF_TEXT_MODEL, HF_TOKEN

logger = logging.getLogger(__name__)

_HF_CHAT_URL = "https://router.huggingface.co/v1/chat/completions"
_PLACEHOLDERS = ("{username}", "{game}", "{name}")

# Used when HF Inference credits are exhausted (402) or the API is down.
_FALLBACK_RU = (
    "{username} в эфире!\n{name}\nКатегория: {game}",
    "🔴 {username} начал стрим\n{name}\nИгра: {game}",
    "{username} онлайн!\n«{name}»\n{game}",
    "Стрим начался: {username}\n{name}\nКатегория — {game}",
    "Гоу смотреть {username}!\n{name}\n{game}",
)
_FALLBACK_EN = (
    "{username} is live!\n{name}\nCategory: {game}",
    "🔴 {username} started streaming\n{name}\nPlaying: {game}",
    "{username} is online!\n“{name}”\n{game}",
    "Stream started: {username}\n{name}\nCategory — {game}",
    "Come watch {username}!\n{name}\n{game}",
)


def _resolve_token() -> str:
    # Read env at call time so Docker/runtime secrets are picked up.
    return (
        os.getenv("HF_TOKEN", "").strip()
        or os.getenv("HUGGING_FACE_API", "").strip()
        or (HF_TOKEN or "").strip()
    )


def _local_template(locale: str) -> str:
    pool = _FALLBACK_RU if str(locale).lower().startswith("ru") else _FALLBACK_EN
    return random.choice(pool)


def generate_alert_template(*, locale: str, channel: str = "") -> str:
    """Generate a Twitch live-alert template with {username}/{game}/{name}."""
    try:
        return _hf_generate(locale=locale, channel=channel)
    except Exception as exc:
        logger.warning("HF template generation unavailable (%s); using local fallback", exc)
        return _normalize_template(_local_template(locale))


def _hf_generate(*, locale: str, channel: str = "") -> str:
    token = _resolve_token()
    if not token:
        raise RuntimeError("HF_TOKEN / HUGGING_FACE_API is not configured")
    if not token.startswith("hf_"):
        raise RuntimeError("HF token must start with hf_")

    lang_name = "Russian" if str(locale).lower().startswith("ru") else "English"
    system = (
        "You write Telegram notification templates for Twitch live-stream alerts. "
        "Reply with ONLY the template text — no quotes, no markdown fences, no explanation. "
        "You MUST include these placeholders exactly (with curly braces): "
        "{username}, {game}, {name}. "
        "Keep it short: 2–4 lines. Match the requested language. "
        "Do NOT invent sample stream titles or demo labels. "
        "Never write phrases like «Тестовый стрим», «Test stream», «test stream», "
        "«пример», «example», or any fake title text — use {name} instead."
    )
    user = (
        f"Language: {lang_name}\n"
        f"Twitch channel (for context only, still use {{username}} placeholder): {channel or 'streamer'}\n"
        "Write a lively live announcement template. "
        "Use placeholders only — no real or sample stream titles."
    )
    model = (
        os.getenv("HF_TEXT_MODEL", "").strip()
        or HF_TEXT_MODEL
        or "Qwen/Qwen2.5-7B-Instruct"
    )
    response = requests.post(
        _HF_CHAT_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
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
    if not text:
        raise RuntimeError("HF returned an empty template")
    return _normalize_template(text)


def _normalize_template(text: str) -> str:
    text = text.strip().strip("`")
    text = re.sub(r"^```(?:\w+)?\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    text = text.strip().strip('"').strip("'")
    # Drop demo titles the model sometimes hardcodes instead of {name}.
    text = re.sub(r"(?i)\bтестовый\s+стрим\b!?", "{name}", text)
    text = re.sub(r"(?i)\btest\s+stream\b!?", "{name}", text)
    text = re.sub(r"(\{name\}\s*){2,}", "{name}", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    # Ensure required placeholders exist even if the model drops one.
    missing = [p for p in _PLACEHOLDERS if p not in text]
    if missing:
        extras = "\n".join(missing)
        text = f"{text}\n{extras}".strip() if text else "\n".join(_PLACEHOLDERS)
    return text
