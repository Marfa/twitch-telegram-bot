from __future__ import annotations

import html
import logging
import re
import time

import requests

from i18n import DEFAULT_LOCALE, SUPPORTED_LOCALES

logger = logging.getLogger(__name__)

_DEEPL_SOURCE = {"en": "EN", "ru": "RU", "uk": "UK", "it": "IT"}
_DEEPL_TARGET = {"en": "EN-US", "ru": "RU", "uk": "UK", "it": "IT"}
_DEEPL_TIMEOUT = 30

# RapidAPI Deep Translate (Google Translate v2-shaped). Fallback after DeepL 456.
_RAPID_LANG = {"en": "en", "ru": "ru", "uk": "uk", "it": "it"}
_RAPID_TIMEOUT = 30
_RAPID_HOST = "deep-translate1.p.rapidapi.com"
_RAPID_URL = f"https://{_RAPID_HOST}/language/translate/v2"

# After DeepL 456, skip DeepL for a day (RapidAPI fallback), then probe again.
_DEEPL_QUOTA_STICKY_SEC = 24 * 3600
_deepl_quota_exhausted_at: float | None = None


class DeeplQuotaExceeded(RuntimeError):
    """DeepL returned HTTP 456 (quota exhausted)."""


def _deepl_quota_blocked() -> bool:
    """True while sticky RapidAPI-only window after DeepL 456 is still active."""
    global _deepl_quota_exhausted_at
    if _deepl_quota_exhausted_at is None:
        return False
    if time.monotonic() - _deepl_quota_exhausted_at >= _DEEPL_QUOTA_STICKY_SEC:
        _deepl_quota_exhausted_at = None
        logger.info("DeepL quota sticky expired; retrying DeepL")
        return False
    return True


def _mark_deepl_quota_exhausted() -> None:
    global _deepl_quota_exhausted_at
    _deepl_quota_exhausted_at = time.monotonic()


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


def _rapidapi_configured() -> bool:
    import config as cfg

    return bool(cfg.RAPIDAPI_KEY)


def translation_configured() -> bool:
    """True when DeepL and/or RapidAPI Deep Translate can translate."""
    import config as cfg

    return bool(cfg.DEEPL_API_KEY) or _rapidapi_configured()


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
        raise DeeplQuotaExceeded(response.text[:200] or "DeepL quota exceeded")
    response.raise_for_status()
    translations = response.json().get("translations") or []
    if not translations:
        return text
    return str(translations[0].get("text") or text)


def _parse_rapid_translation(data: object, fallback: str) -> str:
    """Accept Deep Translate dict shape and Google-native list shape."""
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected RapidAPI Deep Translate response: {data!r}")
    translations = (data.get("data") or {}).get("translations")
    if isinstance(translations, dict):
        texts = translations.get("translatedText")
        if isinstance(texts, list) and texts:
            return str(texts[0] or fallback)
        if isinstance(texts, str) and texts.strip():
            return texts
    if isinstance(translations, list) and translations:
        first = translations[0] or {}
        if isinstance(first, dict):
            return str(first.get("translatedText") or fallback)
    raise RuntimeError(f"Unexpected RapidAPI Deep Translate response: {data!r}")


def _tr_rapidapi(
    text: str,
    *,
    target: str,
    source: str | None,
    use_html: bool,
) -> str:
    import config as cfg

    payload: dict[str, object] = {
        "q": text,
        "target": _RAPID_LANG[target],
        "format": "html" if use_html else "text",
    }
    if source:
        payload["source"] = _RAPID_LANG[source]
    headers = {
        "X-RapidAPI-Key": cfg.RAPIDAPI_KEY,
        "X-RapidAPI-Host": _RAPID_HOST,
        "Content-Type": "application/json",
    }
    try:
        response = requests.post(
            _RAPID_URL,
            json=payload,
            headers=headers,
            timeout=_RAPID_TIMEOUT,
        )
    except requests.RequestException as exc:
        # Same visibility as HTTP errors → PostHog via ERROR logs.
        logger.error(
            "rapidapi deep translate → %s %s",
            type(exc).__name__,
            str(exc)[:500],
        )
        raise
    if not response.ok:
        logger.error(
            "rapidapi deep translate → %s %s",
            response.status_code,
            response.text[:500],
        )
    response.raise_for_status()
    return _parse_rapid_translation(response.json(), text)


def translate_text(
    text: str,
    *,
    target_lang: str,
    source_lang: str | None = None,
    preserve_html: bool | None = None,
) -> str:
    import config as cfg

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

    if cfg.DEEPL_API_KEY and not _deepl_quota_blocked():
        try:
            return _tr_deepl(text, target=target, source=source, use_html=use_html)
        except DeeplQuotaExceeded:
            _mark_deepl_quota_exhausted()
            logger.warning(
                "DeepL quota exceeded; falling back to RapidAPI Deep Translate"
            )

    if _rapidapi_configured():
        return _tr_rapidapi(text, target=target, source=source, use_html=use_html)

    if _deepl_quota_blocked():
        raise RuntimeError(
            "DeepL quota exceeded and RapidAPI Deep Translate is not configured "
            "(RAPIDAPI_KEY)"
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
