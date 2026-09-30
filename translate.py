from __future__ import annotations

import html
import logging
import re

import requests

from config import (
    AZURE_TRANSLATOR_ENDPOINT,
    AZURE_TRANSLATOR_KEY,
    AZURE_TRANSLATOR_REGION,
    DEEPL_API_KEY,
)
from i18n import DEFAULT_LOCALE, SUPPORTED_LOCALES

logger = logging.getLogger(__name__)

_DEEPL_SOURCE = {"en": "EN", "ru": "RU", "uk": "UK", "it": "IT"}
_DEEPL_TARGET = {"en": "EN-US", "ru": "RU", "uk": "UK", "it": "IT"}
_DEEPL_TIMEOUT = 30

_AZURE_LANG = {"en": "en", "ru": "ru", "uk": "uk", "it": "it"}
_AZURE_TIMEOUT = 30
# Azure Text API ~50k chars — keep under with margin (bot texts are usually short).
_AZURE_MAX_CHARS = 45_000

# Sticky for process lifetime: after DeepL 456, stop retrying until restart.
_deepl_quota_exhausted = False


class DeeplQuotaExceeded(RuntimeError):
    """DeepL returned HTTP 456 (quota exhausted)."""


def markdown_to_telegram_html(text: str) -> str:
    """Minimal Markdown → Telegram HTML (bold, links, inline code)."""
    if not text:
        return ""
    slots: list[str] = []

    def put(fragment: str) -> str:
        slots.append(fragment)
        return f"\x00MD{len(slots) - 1}\x00"

    # Bounded character classes — avoid polynomial ReDoS on crafted input.
    text = re.sub(
        r"\[([^\]\n]{1,500})\]\(([^)\n]{1,2000})\)",
        lambda m: put(
            f'<a href="{html.escape(m.group(2), quote=True)}">'
            f"{html.escape(m.group(1))}</a>"
        ),
        text,
    )
    text = re.sub(
        r"\*\*([^*\n]{1,2000})\*\*",
        lambda m: put(f"<b>{html.escape(m.group(1))}</b>"),
        text,
    )
    text = re.sub(
        r"`([^`\n]{1,2000})`",
        lambda m: put(f"<code>{html.escape(m.group(1))}</code>"),
        text,
    )
    text = html.escape(text)
    for i, fragment in enumerate(slots):
        text = text.replace(f"\x00MD{i}\x00", fragment)
    return text


def _deepl_base_url(api_key: str) -> str:
    return "https://api-free.deepl.com" if api_key.endswith(":fx") else "https://api.deepl.com"


def _normalize_locale(locale: str | None) -> str:
    if locale in SUPPORTED_LOCALES:
        return locale
    return DEFAULT_LOCALE


def _azure_configured() -> bool:
    return bool(AZURE_TRANSLATOR_KEY and AZURE_TRANSLATOR_REGION)


def translation_configured() -> bool:
    """True when DeepL and/or Azure Translator can translate."""
    return bool(DEEPL_API_KEY) or _azure_configured()


def _tr_deepl(
    text: str,
    *,
    target: str,
    source: str | None,
    use_html: bool,
) -> str:
    api_key = DEEPL_API_KEY
    payload: dict[str, object] = {
        "text": [text],
        "target_lang": _DEEPL_TARGET[target],
    }
    if use_html:
        payload["tag_handling"] = "html"
    if source:
        payload["source_lang"] = _DEEPL_SOURCE[source]

    response = requests.post(
        f"{_deepl_base_url(api_key)}/v2/translate",
        data=payload,
        headers={"Authorization": f"DeepL-Auth-Key {api_key}"},
        timeout=_DEEPL_TIMEOUT,
    )
    if response.status_code == 456:
        raise DeeplQuotaExceeded(response.text[:200] or "DeepL quota exceeded")
    response.raise_for_status()
    translations = response.json().get("translations") or []
    if not translations:
        return text
    return str(translations[0].get("text") or text)


def _tr_azure_once(
    text: str,
    *,
    target: str,
    source: str | None,
    use_html: bool,
) -> str:
    params: dict[str, str] = {
        "api-version": "3.0",
        "to": _AZURE_LANG[target],
    }
    if source:
        params["from"] = _AZURE_LANG[source]
    if use_html:
        params["textType"] = "html"
    headers = {
        "Ocp-Apim-Subscription-Key": AZURE_TRANSLATOR_KEY,
        "Ocp-Apim-Subscription-Region": AZURE_TRANSLATOR_REGION,
        "Content-Type": "application/json; charset=UTF-8",
    }
    response = requests.post(
        f"{AZURE_TRANSLATOR_ENDPOINT}/translate",
        params=params,
        headers=headers,
        json=[{"Text": text}],
        timeout=_AZURE_TIMEOUT,
    )
    if response.is_error:
        logger.error(
            "azure translate → %s %s",
            response.status_code,
            response.text[:500],
        )
    response.raise_for_status()
    data = response.json()
    try:
        return data[0]["translations"][0]["text"]
    except (IndexError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Unexpected Azure Translator response: {data!r}") from exc


def _tr_azure(
    text: str,
    *,
    target: str,
    source: str | None,
    use_html: bool,
) -> str:
    if len(text) <= _AZURE_MAX_CHARS:
        return _tr_azure_once(
            text, target=target, source=source, use_html=use_html
        )
    if use_html:
        # Bot payloads are Telegram-sized; avoid mid-tag splits.
        return _tr_azure_once(
            text, target=target, source=source, use_html=True
        )
    parts: list[str] = []
    start = 0
    while start < len(text):
        parts.append(
            _tr_azure_once(
                text[start : start + _AZURE_MAX_CHARS],
                target=target,
                source=source,
                use_html=False,
            )
        )
        start += _AZURE_MAX_CHARS
    return "".join(parts)


def translate_text(
    text: str,
    *,
    target_lang: str,
    source_lang: str | None = None,
    preserve_html: bool | None = None,
) -> str:
    global _deepl_quota_exhausted
    target = _normalize_locale(target_lang)
    source = _normalize_locale(source_lang) if source_lang else None
    if source and target == source:
        return text
    if not text.strip():
        return text
    if not translation_configured():
        return text

    # HTML mode preserves <b>/<a>/… for Telegram; on plain text DeepL emits
    # entities like &#x27; which show literally when parse_mode is off.
    use_html = (
        preserve_html
        if preserve_html is not None
        else ("<" in text and ">" in text)
    )

    if DEEPL_API_KEY and not _deepl_quota_exhausted:
        try:
            return _tr_deepl(text, target=target, source=source, use_html=use_html)
        except DeeplQuotaExceeded:
            _deepl_quota_exhausted = True
            logger.warning("DeepL quota exceeded; falling back to Azure Translator")

    if _azure_configured():
        return _tr_azure(text, target=target, source=source, use_html=use_html)

    if _deepl_quota_exhausted:
        raise RuntimeError(
            "DeepL quota exceeded and Azure Translator is not configured "
            "(AZURE_TRANSLATOR_KEY + AZURE_TRANSLATOR_REGION)"
        )
    return text


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
            logger.exception("translation %s → %s failed", source, loc)
            result[loc] = text
    return result
