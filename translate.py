from __future__ import annotations

import html
import logging
import re

import requests

from i18n import DEFAULT_LOCALE, SUPPORTED_LOCALES

logger = logging.getLogger(__name__)

_DEEPL_SOURCE = {"en": "EN", "ru": "RU", "uk": "UK", "it": "IT"}
_DEEPL_TARGET = {"en": "EN-US", "ru": "RU", "uk": "UK", "it": "IT"}
_DEEPL_TIMEOUT = 30


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


def translation_configured() -> bool:
    """True when DeepL can translate."""
    import config as cfg

    return bool(cfg.DEEPL_API_KEY)


def _tr_deepl(
    text: str,
    *,
    target: str,
    source: str | None,
    use_html: bool,
) -> str:
    import config as cfg

    api_key = cfg.DEEPL_API_KEY
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
        logger.warning("DeepL quota exceeded (HTTP 456); leaving text untranslated")
        return text
    response.raise_for_status()
    translations = response.json().get("translations") or []
    if not translations:
        return text
    return str(translations[0].get("text") or text)


def translate_text(
    text: str,
    *,
    target_lang: str,
    source_lang: str | None = None,
    preserve_html: bool | None = None,
) -> str:
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

    return _tr_deepl(text, target=target, source=source, use_html=use_html)


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
