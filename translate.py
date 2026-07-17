from __future__ import annotations

import logging

import requests

from config import DEEPL_API_KEY
from i18n import DEFAULT_LOCALE, SUPPORTED_LOCALES

logger = logging.getLogger(__name__)

_DEEPL_SOURCE = {"en": "EN", "ru": "RU"}
_DEEPL_TARGET = {"en": "EN-US", "ru": "RU"}
_DEEPL_TIMEOUT = 30


def _deepl_base_url(api_key: str) -> str:
    return "https://api-free.deepl.com" if api_key.endswith(":fx") else "https://api.deepl.com"


def _normalize_locale(locale: str | None) -> str:
    if locale in SUPPORTED_LOCALES:
        return locale
    return DEFAULT_LOCALE


def translate_text(text: str, *, target_lang: str, source_lang: str | None = None) -> str:
    target = _normalize_locale(target_lang)
    source = _normalize_locale(source_lang) if source_lang else None
    if source and target == source:
        return text
    if not text.strip():
        return text
    api_key = DEEPL_API_KEY
    if not api_key:
        return text

    payload: dict[str, object] = {
        "text": [text],
        "target_lang": _DEEPL_TARGET[target],
        # Preserve Telegram HTML from message.text_html (bold/italic/links).
        "tag_handling": "html",
    }
    if source:
        payload["source_lang"] = _DEEPL_SOURCE[source]

    response = requests.post(
        f"{_deepl_base_url(api_key)}/v2/translate",
        data=payload,
        headers={"Authorization": f"DeepL-Auth-Key {api_key}"},
        timeout=_DEEPL_TIMEOUT,
    )
    response.raise_for_status()
    translations = response.json().get("translations") or []
    if not translations:
        return text
    return str(translations[0].get("text") or text)


def build_translations(
    text: str,
    source_lang: str,
    target_locales: set[str],
) -> dict[str, str]:
    source = _normalize_locale(source_lang)
    result = {source: text}
    for locale in target_locales:
        loc = _normalize_locale(locale)
        if loc in result:
            continue
        try:
            result[loc] = translate_text(text, target_lang=loc, source_lang=source)
        except Exception:
            logger.exception("DeepL translation %s → %s failed", source, loc)
            result[loc] = text
    return result
