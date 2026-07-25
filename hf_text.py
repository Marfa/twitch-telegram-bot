"""LLM chat completions for alert templates (Groq → HF → local fallback)."""

from __future__ import annotations

import logging
import os
import random
import re
from typing import Protocol

import requests

from config import GROQ_API_KEY, GROQ_TEXT_MODEL, HF_TEXT_MODEL, HF_TOKEN

logger = logging.getLogger(__name__)

_GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
_HF_CHAT_URL = "https://router.huggingface.co/v1/chat/completions"
_PLACEHOLDERS = ("{username}", "{game}", "{name}")

# Cold-start only: used when DB has no previously generated lucky templates yet.
_SEED_RU = (
    "{username} в эфире!\n{name}\nКатегория: {game}",
    "🔴 {username} начал стрим\n{name}\nИгра: {game}",
    "{username} онлайн!\n«{name}»\n{game}",
)
_SEED_EN = (
    "{username} is live!\n{name}\nCategory: {game}",
    "🔴 {username} started streaming\n{name}\nPlaying: {game}",
    "{username} is online!\n“{name}”\n{game}",
)


class LuckyTemplateStore(Protocol):
    def add_lucky_template(self, locale: str, text: str) -> None: ...

    def pick_lucky_template(self, locale: str) -> str | None: ...


def _resolve_groq_token() -> str:
    return (
        os.getenv("GROQ_API_KEY", "").strip()
        or os.getenv("GROQ_API", "").strip()
        or os.getenv("GROK_API", "").strip()  # common typo alias
        or (GROQ_API_KEY or "").strip()
    )


def _resolve_hf_token() -> str:
    return (
        os.getenv("HF_TOKEN", "").strip()
        or os.getenv("HUGGING_FACE_API", "").strip()
        or (HF_TOKEN or "").strip()
    )


def _seed_template(locale: str) -> str:
    pool = _SEED_RU if str(locale).lower().startswith("ru") else _SEED_EN
    return random.choice(pool)


def _local_template(locale: str, store: LuckyTemplateStore | None = None) -> str:
    if store is not None:
        picked = store.pick_lucky_template(locale)
        if picked:
            return picked
    return _seed_template(locale)


def _prompt_messages(*, locale: str, channel: str) -> list[dict[str, str]]:
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
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def generate_alert_template(
    *,
    locale: str,
    channel: str = "",
    store: LuckyTemplateStore | None = None,
) -> str:
    """Generate a Twitch live-alert template with {username}/{game}/{name}."""
    providers = (
        ("Groq", _groq_generate),
        ("HF", _hf_generate),
    )
    for name, fn in providers:
        try:
            text = fn(locale=locale, channel=channel)
            if store is not None:
                store.add_lucky_template(locale, text)
            return text
        except Exception as exc:
            logger.warning("%s template generation unavailable (%s)", name, exc)
    return _normalize_template(_local_template(locale, store))


def _chat_completion(
    *,
    url: str,
    token: str,
    model: str,
    locale: str,
    channel: str,
) -> str:
    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": _prompt_messages(locale=locale, channel=channel),
            "max_tokens": 200,
            "temperature": 0.8,
        },
        timeout=90,
    )
    if response.status_code >= 400:
        logger.warning(
            "chat completions failed (%s): %s %s",
            model,
            response.status_code,
            response.text[:300],
        )
        response.raise_for_status()

    data = response.json()
    text = (
        ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    ).strip()
    if not text:
        raise RuntimeError("LLM returned an empty template")
    return _normalize_template(text)


def _groq_generate(*, locale: str, channel: str = "") -> str:
    token = _resolve_groq_token()
    if not token:
        raise RuntimeError("GROQ_API_KEY / GROK_API is not configured")
    model = (
        os.getenv("GROQ_TEXT_MODEL", "").strip()
        or GROQ_TEXT_MODEL
        or "llama-3.1-8b-instant"
    )
    return _chat_completion(
        url=_GROQ_CHAT_URL,
        token=token,
        model=model,
        locale=locale,
        channel=channel,
    )


def _hf_generate(*, locale: str, channel: str = "") -> str:
    token = _resolve_hf_token()
    if not token:
        raise RuntimeError("HF_TOKEN / HUGGING_FACE_API is not configured")
    if not token.startswith("hf_"):
        raise RuntimeError("HF token must start with hf_")
    model = (
        os.getenv("HF_TEXT_MODEL", "").strip()
        or HF_TEXT_MODEL
        or "Qwen/Qwen2.5-7B-Instruct"
    )
    return _chat_completion(
        url=_HF_CHAT_URL,
        token=token,
        model=model,
        locale=locale,
        channel=channel,
    )


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
