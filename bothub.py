"""BotHub image generation (OpenAI-compatible API).

Same integration pattern as Marfa/ghost-text-prepper:
Nano Banana 2 on BotHub == ``gemini-3.1-flash-image``.
"""
from __future__ import annotations

import base64
import logging
import re
from typing import Any

import requests

from config import BOTHUB_API_KEY, BOTHUB_BASE_URL, BOTHUB_IMAGE_MODEL

logger = logging.getLogger(__name__)

_DATA_URL_RE = re.compile(
    r"data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)", re.I
)
_MAX_TOPIC_CHARS = 900
_HTTP_TIMEOUT = 120

_http = requests.Session()


def bothub_configured() -> bool:
    return bool(BOTHUB_API_KEY)


def build_stream_cover_prompt(
    *,
    game_name: str,
    game_description: str,
    streamer_login: str = "",
) -> str:
    """Cover for a Twitch stream alert: game mood + livestream framing, no text."""
    name = re.sub(r"\s+", " ", (game_name or "").strip()) or "a video game"
    topic = re.sub(r"\s+", " ", (game_description or "").strip())
    if topic in ("—", "-"):
        topic = ""
    topic = topic[:_MAX_TOPIC_CHARS].strip()
    login = re.sub(r"\s+", " ", (streamer_login or "").strip())

    parts = [
        "Create a single widescreen cover image for a Twitch livestream alert in Telegram.",
        f"Game / category subject: {name}.",
        "Mood: live streaming, gaming community, energetic but clean cinematic look "
        "suitable as a stream notification thumbnail.",
    ]
    if login:
        parts.append(
            f"Streamer channel context (atmosphere only, do not depict real people "
            f"or write the name): {login}."
        )
    if topic:
        parts.append(f"Game description (mood and motifs only): {topic}")
    parts.append(
        "Composition: landscape ~16:9, strong focal subject readable as a small "
        "Telegram photo, cohesive color palette, photographic or illustration style."
    )
    parts.append(
        "Strict rules: no text, letters, words, numbers, typography, watermarks, "
        "logos, Twitch UI chrome, captions, or signatures anywhere in the image."
    )
    return " ".join(parts)


def _bytes_from_data_url(url: str) -> bytes | None:
    match = _DATA_URL_RE.search(url or "")
    if not match:
        return None
    try:
        return base64.b64decode(re.sub(r"\s+", "", match.group(2)))
    except Exception:
        return None


def _download_image_url(url: str) -> bytes:
    response = _http.get(url, timeout=60)
    response.raise_for_status()
    return response.content


def _image_bytes_from_generations_payload(data: dict[str, Any]) -> bytes:
    items = data.get("data") or []
    if not items:
        raise RuntimeError(
            f"BotHub images/generations returned no data: {str(data)[:400]}"
        )
    item = items[0]
    b64 = item.get("b64_json")
    if b64:
        return base64.b64decode(b64)
    url = (item.get("url") or "").strip()
    if url.startswith("data:"):
        decoded = _bytes_from_data_url(url)
        if decoded:
            return decoded
    if url:
        return _download_image_url(url)
    raise RuntimeError(f"BotHub image item missing b64_json/url: {str(item)[:400]}")


def _image_bytes_from_chat_payload(data: dict[str, Any]) -> bytes:
    message = (data.get("choices") or [{}])[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url") or part.get("url") or ""
                if url.startswith("data:"):
                    decoded = _bytes_from_data_url(url)
                    if decoded:
                        return decoded
                if url.startswith("http"):
                    return _download_image_url(url)
            b64 = part.get("inline_data") or part.get("b64_json")
            if isinstance(b64, dict):
                b64 = b64.get("data")
            if isinstance(b64, str) and len(b64) > 64:
                try:
                    return base64.b64decode(b64)
                except Exception:
                    pass
    if isinstance(content, str):
        decoded = _bytes_from_data_url(content)
        if decoded:
            return decoded
        urls = re.findall(
            r"https?://[^\s)\"']+\.(?:png|jpe?g|webp)", content, flags=re.I
        )
        if urls:
            return _download_image_url(urls[0])
    raise RuntimeError(f"BotHub chat completion had no image: {str(data)[:500]}")


def generate_cover_image(prompt: str) -> bytes:
    """Generate cover bytes via BotHub (images/generations, chat.completions fallback)."""
    if not BOTHUB_API_KEY:
        raise RuntimeError("Missing BOTHUB_API_KEY")
    headers = {
        "Authorization": f"Bearer {BOTHUB_API_KEY}",
        "Content-Type": "application/json",
    }
    gen_body: dict[str, Any] = {
        "model": BOTHUB_IMAGE_MODEL,
        "prompt": prompt,
        "n": 1,
        "size": "1792x1024",
        "response_format": "b64_json",
        "aspect_ratio": "16:9",
        "output_format": "jpeg",
    }
    response = _http.post(
        f"{BOTHUB_BASE_URL}/images/generations",
        headers=headers,
        json=gen_body,
        timeout=_HTTP_TIMEOUT,
    )
    if response.status_code in (404, 405):
        logger.info("BotHub images/generations unavailable — trying chat.completions")
    elif response.is_error:
        logger.warning(
            "BotHub images/generations → %s %s — trying chat.completions",
            response.status_code,
            response.text[:400],
        )
    else:
        return _image_bytes_from_generations_payload(response.json())

    chat_body = {
        "model": BOTHUB_IMAGE_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1024,
    }
    chat = _http.post(
        f"{BOTHUB_BASE_URL}/chat/completions",
        headers=headers,
        json=chat_body,
        timeout=_HTTP_TIMEOUT,
    )
    if chat.is_error:
        logger.error(
            "BotHub chat.completions → %s %s", chat.status_code, chat.text[:500]
        )
    chat.raise_for_status()
    return _image_bytes_from_chat_payload(chat.json())
