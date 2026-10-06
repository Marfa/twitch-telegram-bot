"""BotHub image generation (OpenAI-compatible API).

Default model: Gemini 3.1 Flash Lite Image (fast). Covers are cached per
Twitch ``game_id`` in ``ai_game_covers`` so each category is generated once.
"""
from __future__ import annotations

import base64
import logging
import re
import threading
from typing import Any

import requests

from config import (
    BOTHUB_API_KEY,
    BOTHUB_BASE_URL,
    BOTHUB_IMAGE_MODEL,
    BOTHUB_IMAGE_MODEL_FALLBACK,
)

logger = logging.getLogger(__name__)

_DATA_URL_RE = re.compile(
    r"data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)", re.I
)
_BOTHUB_CAPS_RE = re.compile(r"NOT_ENOUGH_TOKENS|Недостаточно CAPS", re.I)
_MAX_TOPIC_CHARS = 900
_HTTP_TIMEOUT = 120
_COVER_SIZE = "1280x720"

_http = requests.Session()
_gen_locks_guard = threading.Lock()
_gen_locks: dict[str, threading.Lock] = {}
# ponytail: after BotHub CAPS 403 on primary image model, use fallback for the rest
# of the process (bot is long-lived; restart clears). Ceiling: no TTL probe back to
# primary — upgrade: sticky window like translate._DEEPL_QUOTA_STICKY_SEC.
_bothub_caps_fallback_run = False


class BotHubInsufficientCapsError(RuntimeError):
    """BotHub CAPS balance too low for image generation (403 NOT_ENOUGH_TOKENS)."""


def bothub_configured() -> bool:
    return bool(BOTHUB_API_KEY)


def is_bothub_insufficient_caps(status_code: int, body: str) -> bool:
    return status_code == 403 and bool(_BOTHUB_CAPS_RE.search(body or ""))


def bothub_image_models() -> list[str]:
    """Primary then cheaper fallback; after a CAPS hit, fallback only for this process."""
    primary = (BOTHUB_IMAGE_MODEL or "").strip()
    fallback = (BOTHUB_IMAGE_MODEL_FALLBACK or "").strip()
    if _bothub_caps_fallback_run and fallback and fallback != primary:
        return [fallback]
    models: list[str] = []
    for model in (primary, fallback):
        if model and model not in models:
            models.append(model)
    return models


def preferred_image_model() -> str:
    models = bothub_image_models()
    return models[0] if models else (BOTHUB_IMAGE_MODEL or "").strip()


def _known_image_models() -> set[str]:
    return {m for m in (BOTHUB_IMAGE_MODEL, BOTHUB_IMAGE_MODEL_FALLBACK) if (m or "").strip()}


def _report_insufficient_caps(
    exc: BotHubInsufficientCapsError, *, models: list[str] | None = None
) -> None:
    try:
        import analytics

        analytics.capture_exception(
            exc,
            properties={
                "handler": "bothub.generate_cover_image",
                "error_kind": "bothub_insufficient_caps",
                "model": preferred_image_model() or BOTHUB_IMAGE_MODEL,
                "models_tried": ",".join(models or bothub_image_models()),
                "size": _COVER_SIZE,
            },
        )
    except Exception:
        logger.exception("PostHog CAPS report failed")


def _raise_insufficient_caps(
    err_snip: str, *, via: str, models: list[str] | None = None
) -> None:
    tried = models or bothub_image_models()
    exc = BotHubInsufficientCapsError(
        f"BotHub CAPS insufficient for cover ({via}, size={_COVER_SIZE}, "
        f"models tried: {', '.join(tried) or 'none'}). "
        f"Top up balance. API: {err_snip}"
    )
    _report_insufficient_caps(exc, models=tried)
    raise exc


def build_stream_cover_prompt(
    *,
    game_name: str,
    game_description: str,
) -> str:
    """Category cover for a Twitch alert: game mood + livestream framing, no text."""
    name = re.sub(r"\s+", " ", (game_name or "").strip()) or "a video game"
    topic = re.sub(r"\s+", " ", (game_description or "").strip())
    if topic in ("—", "-"):
        topic = ""
    topic = topic[:_MAX_TOPIC_CHARS].strip()

    parts = [
        "Create a single widescreen cover image for a Twitch livestream alert in Telegram.",
        f"Game / category subject: {name}.",
        "Mood: live streaming, gaming community, energetic but clean cinematic look "
        "suitable as a stream notification thumbnail.",
    ]
    if topic:
        parts.append(f"Game description (mood and motifs only): {topic}")
    parts.append(
        "Composition: landscape ~16:9, strong focal subject readable as a small "
        "Telegram photo, cohesive color palette, photographic or illustration style."
    )
    parts.append(
        "Strict rules: no text, letters, words, numbers, typography, watermarks, "
        "logos, Twitch UI chrome, captions, or signatures anywhere in the image. "
        "CRITICAL: ZERO readable text of any kind — no words on monitors, no neon "
        "signs with letters, no UI labels, no DEMO/PLAYTEST/LIVE/START badges, "
        "no HUD text, no logos with letters. Screens may show abstract colorful "
        "gameplay shapes only, without any glyphs."
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


def _content_type_for(raw: bytes) -> str:
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "image/jpeg"


def _lock_for_game(game_id: str) -> threading.Lock:
    with _gen_locks_guard:
        lock = _gen_locks.get(game_id)
        if lock is None:
            lock = threading.Lock()
            _gen_locks[game_id] = lock
        return lock


def _bothub_images_generations(
    model: str, prompt: str, headers: dict[str, str]
) -> requests.Response:
    """One images/generations attempt; retries without optional fields on 400."""
    # Do not send output_format — BotHub may reject it as unavailable for the model.
    # Telegram accepts PNG/JPEG from send_photo either way.
    gen_body: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        # ~1K 16:9 — enough for Telegram chat; 1792x1024 was slower for little gain.
        "size": _COVER_SIZE,
        "response_format": "b64_json",
        "aspect_ratio": "16:9",
    }
    response = _http.post(
        f"{BOTHUB_BASE_URL}/images/generations",
        headers=headers,
        json=gen_body,
        timeout=_HTTP_TIMEOUT,
    )
    if response.ok:
        return response

    err_snip = response.text[:400]
    if response.status_code == 400 and any(
        key in err_snip.lower()
        for key in (
            "output_format",
            "aspect_ratio",
            "size",
            "response_format",
            "unvailvable",
            "unavailable",
        )
    ):
        logger.warning(
            "BotHub images/generations → %s %s — retrying without optional fields",
            response.status_code,
            err_snip,
        )
        return _http.post(
            f"{BOTHUB_BASE_URL}/images/generations",
            headers=headers,
            json={
                "model": model,
                "prompt": prompt,
                "n": 1,
                "response_format": "b64_json",
            },
            timeout=_HTTP_TIMEOUT,
        )
    return response


def generate_cover_image(prompt: str) -> bytes:
    """Generate cover bytes via BotHub (images/generations, chat.completions fallback).

    On NOT_ENOUGH_TOKENS, retry with BOTHUB_IMAGE_MODEL_FALLBACK for the rest of the process.
    """
    global _bothub_caps_fallback_run
    if not BOTHUB_API_KEY:
        raise RuntimeError("Missing BOTHUB_API_KEY")
    headers = {
        "Authorization": f"Bearer {BOTHUB_API_KEY}",
        "Content-Type": "application/json",
    }
    models = bothub_image_models()
    if not models:
        raise RuntimeError("No BotHub image model configured")
    last_response: requests.Response | None = None
    for i, model in enumerate(models):
        response = _bothub_images_generations(model, prompt, headers)
        last_response = response
        if response.ok:
            if i > 0 or _bothub_caps_fallback_run:
                logger.info("cover via BotHub fallback model %s", model)
            return _image_bytes_from_generations_payload(response.json())

        err_snip = response.text[:400]
        if is_bothub_insufficient_caps(response.status_code, response.text) and i + 1 < len(
            models
        ):
            _bothub_caps_fallback_run = True
            logger.warning(
                "BotHub CAPS insufficient for %s — falling back to %s",
                model,
                models[i + 1],
            )
            continue

        if response.status_code not in (404, 405):
            if is_bothub_insufficient_caps(response.status_code, response.text):
                _raise_insufficient_caps(
                    err_snip, via="images/generations", models=models
                )
            logger.error(
                "BotHub images/generations → %s %s", response.status_code, err_snip
            )
            response.raise_for_status()

        # Endpoint missing for this model — try chat.completions, then next model.
        logger.info(
            "BotHub images/generations unavailable for %s — trying chat.completions",
            model,
        )
        chat = _http.post(
            f"{BOTHUB_BASE_URL}/chat/completions",
            headers=headers,
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 1024,
            },
            timeout=_HTTP_TIMEOUT,
        )
        if chat.ok:
            if i > 0 or _bothub_caps_fallback_run:
                logger.info("cover via BotHub fallback model %s (chat)", model)
            return _image_bytes_from_chat_payload(chat.json())
        logger.error(
            "BotHub chat.completions → %s %s", chat.status_code, chat.text[:500]
        )
        if is_bothub_insufficient_caps(chat.status_code, chat.text) and i + 1 < len(
            models
        ):
            _bothub_caps_fallback_run = True
            logger.warning(
                "BotHub CAPS insufficient for %s (chat) — falling back to %s",
                model,
                models[i + 1],
            )
            continue
        if is_bothub_insufficient_caps(chat.status_code, chat.text):
            _raise_insufficient_caps(
                chat.text[:400], via="chat.completions", models=models
            )
        last_response = chat

    if last_response is not None:
        last_response.raise_for_status()
    raise RuntimeError("BotHub cover generation failed with no response")


def generate_alert_cover_bytes(
    *,
    stream: dict[str, Any] | None,
    twitch: Any,
    lang: str = "en",
    db: Any = None,
) -> bytes | None:
    """Return AI cover bytes for the stream category (DB cache → generate once)."""
    if not bothub_configured():
        return None
    from twitch import _stream_game_fields

    game_id, game_name = _stream_game_fields(stream or {})
    gid = str(game_id or "").strip()
    model = preferred_image_model()
    known = _known_image_models()

    def _cached() -> bytes | None:
        if not db or not gid or not hasattr(db, "get_ai_game_cover"):
            return None
        row = db.get_ai_game_cover(gid)
        if not row:
            return None
        raw = row.get("image_bytes") if isinstance(row, dict) else None
        stored_model = str((row or {}).get("model") or "").strip()
        if not raw or len(raw) < 256:
            return None
        # Accept primary or fallback covers; miss only for unrelated env models.
        if known and stored_model and stored_model not in known:
            return None
        return bytes(raw)

    hit = _cached()
    if hit:
        return hit

    lock = _lock_for_game(gid) if gid else threading.Lock()
    with lock:
        hit = _cached()
        if hit:
            return hit

        description = ""
        if game_id and twitch is not None:
            try:
                description = str(
                    twitch.resolve_game_description(game_id, lang=lang) or ""
                ).strip()
            except Exception:
                logger.exception(
                    "AI cover: game description failed game_id=%s", game_id
                )
                description = ""
        if description in ("—", "-"):
            description = ""
        if not game_name and not description:
            return None
        prompt = build_stream_cover_prompt(
            game_name=game_name or "video game",
            game_description=description,
        )
        raw = generate_cover_image(prompt)
        if not raw or len(raw) < 256:
            return None
        # After CAPS sticky, preferred is the model that actually produced the cover.
        store_model = preferred_image_model() or model
        if db and gid and hasattr(db, "upsert_ai_game_cover"):
            try:
                db.upsert_ai_game_cover(
                    gid,
                    game_name=game_name or "",
                    image_bytes=raw,
                    content_type=_content_type_for(raw),
                    model=store_model,
                )
            except Exception:
                logger.exception("AI cover: failed to cache game_id=%s", gid)
        return raw
