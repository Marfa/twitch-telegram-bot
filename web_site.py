"""Public marketing site (Stillwater landing) + shared chrome for /guide and /webhook."""

from __future__ import annotations

import html
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
SITE_DIR = (_ROOT / "webapp" / "site").resolve()
ASSETS_DIR = (_ROOT / "assets").resolve()
GUIDE_DIR = (_ROOT / "webapp" / "guide").resolve()

GUIDE_FILES = {
    "ru": (GUIDE_DIR / "ru.html").resolve(),
    "en": (GUIDE_DIR / "en.html").resolve(),
    "uk": (GUIDE_DIR / "uk.html").resolve(),
    "it": (GUIDE_DIR / "it.html").resolve(),
}

_BODY_RE = re.compile(
    r"<body\b[^>]*>(.*)</body>", re.IGNORECASE | re.DOTALL
)
_TITLE_RE = re.compile(
    r"<title\b[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL
)

# Primary Telegram username; landing CTAs append ?start=site for PostHog.
BOT_TELEGRAM_PRIMARY = "https://t.me/twitchbot_game"
BOT_TELEGRAM_FROM_SITE = f"{BOT_TELEGRAM_PRIMARY}?start=site"

_UI = {
    "en": {
        "brand": "Twitch X Telegram",
        "nav_home": "Home",
        "nav_guide": "Guide",
        "nav_webhook": "Webhook",
        "nav_start": "Open bot",
        "footer_blurb": (
            "Multi-platform stream alerts for Telegram and Discord. "
            "Built by Marfa. Not affiliated with Twitch."
        ),
        "footer_contact": "Contact",
        "footer_links": "Site links",
        "footer_support": "Support the project",
        "footer_home": "Home",
        "footer_guide": "Guide",
        "footer_webhook": "Webhook docs",
        "footer_privacy": "Privacy",
        "footer_placeholders": "Placeholders",
        "footer_github": "GitHub",
        "footer_donate": "Donate",
        "footer_crypto": "Crypto donate",
        "footer_copy": "© Marfa — Twitch X Telegram",
        "footer_design": "Design by",
        "guide_hero": "User guide",
        "guide_lead": "How to set up alerts, destinations, Premium, and everyday menus.",
        "webhook_hero": "Outbound webhook",
        "webhook_lead": "HTTPS JSON delivery contract with HMAC for integrators.",
    },
    "ru": {
        "brand": "Twitch X Telegram",
        "nav_home": "Главная",
        "nav_guide": "Инструкция",
        "nav_webhook": "Webhook",
        "nav_start": "Открыть бота",
        "footer_blurb": (
            "Оповещения о стримах с разных площадок для Telegram и Discord. "
            "Сделано Marfa. Не аффилировано с Twitch."
        ),
        "footer_contact": "Контакты",
        "footer_links": "Ссылки",
        "footer_support": "Поддержка проекта",
        "footer_home": "Главная",
        "footer_guide": "Инструкция",
        "footer_webhook": "Документация webhook",
        "footer_privacy": "Конфиденциальность",
        "footer_placeholders": "Плейсхолдеры",
        "footer_github": "GitHub",
        "footer_donate": "Донат",
        "footer_crypto": "Донат криптой",
        "footer_copy": "© Marfa — Twitch X Telegram",
        "footer_design": "Дизайн",
        "guide_hero": "Инструкция",
        "guide_lead": "Как настроить оповещения, куда слать, Premium и повседневные меню.",
        "webhook_hero": "Outbound webhook",
        "webhook_lead": "Контракт HTTPS JSON + HMAC для интеграторов.",
    },
    "uk": {
        "brand": "Twitch X Telegram",
        "nav_home": "Головна",
        "nav_guide": "Інструкція",
        "nav_webhook": "Webhook",
        "nav_start": "Відкрити бота",
        "footer_blurb": (
            "Сповіщення про стріми з різних платформ для Telegram і Discord. "
            "Зроблено Marfa. Не афілійовано з Twitch."
        ),
        "footer_contact": "Контакти",
        "footer_links": "Посилання",
        "footer_support": "Підтримка проєкту",
        "footer_home": "Головна",
        "footer_guide": "Інструкція",
        "footer_webhook": "Документація webhook",
        "footer_privacy": "Конфіденційність",
        "footer_placeholders": "Плейсхолдери",
        "footer_github": "GitHub",
        "footer_donate": "Донат",
        "footer_crypto": "Донат криптою",
        "footer_copy": "© Marfa — Twitch X Telegram",
        "footer_design": "Дизайн",
        "guide_hero": "Інструкція",
        "guide_lead": "Як налаштувати сповіщення, куди надсилати, Premium і меню.",
        "webhook_hero": "Outbound webhook",
        "webhook_lead": "Контракт HTTPS JSON + HMAC для інтеграторів.",
    },
    "it": {
        "brand": "Twitch X Telegram",
        "nav_home": "Home",
        "nav_guide": "Guida",
        "nav_webhook": "Webhook",
        "nav_start": "Apri il bot",
        "footer_blurb": (
            "Avvisi multi-piattaforma per Telegram e Discord. "
            "Fatto da Marfa. Non affiliato a Twitch."
        ),
        "footer_contact": "Contatti",
        "footer_links": "Link",
        "footer_support": "Supporta il progetto",
        "footer_home": "Home",
        "footer_guide": "Guida",
        "footer_webhook": "Docs webhook",
        "footer_privacy": "Privacy",
        "footer_placeholders": "Placeholder",
        "footer_github": "GitHub",
        "footer_donate": "Donazione",
        "footer_crypto": "Donazione crypto",
        "footer_copy": "© Marfa — Twitch X Telegram",
        "footer_design": "Design di",
        "guide_hero": "Guida",
        "guide_lead": "Come configurare avvisi, destinazioni, Premium e i menu.",
        "webhook_hero": "Outbound webhook",
        "webhook_lead": "Contratto HTTPS JSON + HMAC per gli integrator.",
    },
}


def normalize_lang(raw: str | None, *, default: str = "en") -> str:
    from i18n import DEFAULT_LOCALE, SUPPORTED_LOCALES

    fallback = default if default in SUPPORTED_LOCALES else DEFAULT_LOCALE
    if not raw:
        return fallback
    code = raw.strip().lower().replace("_", "-")
    if code in SUPPORTED_LOCALES:
        return code
    primary = code.split("-", 1)[0]
    if primary in SUPPORTED_LOCALES:
        return primary
    return fallback


def lang_from_accept(header: str | None) -> str:
    from i18n import DEFAULT_LOCALE, SUPPORTED_LOCALES

    if not header:
        return DEFAULT_LOCALE
    scored: list[tuple[float, str]] = []
    for item in header.split(","):
        item = item.strip()
        if not item:
            continue
        lang_part, _, rest = item.partition(";")
        q = 1.0
        if rest.startswith("q="):
            try:
                q = float(rest[2:].strip())
            except ValueError:
                q = 0.0
        scored.append((q, lang_part.strip().lower().replace("_", "-")))
    scored.sort(key=lambda pair: -pair[0])
    for _, code in scored:
        if code in SUPPORTED_LOCALES:
            return code
        primary = code.split("-", 1)[0]
        if primary in SUPPORTED_LOCALES:
            return primary
    return DEFAULT_LOCALE


def request_lang(query_lang: str | None, accept_language: str | None) -> str:
    if query_lang and query_lang.strip():
        return normalize_lang(query_lang)
    return lang_from_accept(accept_language)


_SAFE_REL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_CONTENT_TYPES = {
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".eot": "application/vnd.ms-fontobject",
    ".otf": "font/otf",
    ".map": "application/json",
}


def safe_file(root: Path, rel: str) -> Path | None:
    """Resolve a relative path under root; reject traversal / odd chars."""
    raw = (rel or "").replace("\\", "/").lstrip("/")
    if not raw or not _SAFE_REL_RE.fullmatch(raw):
        return None
    parts = [p for p in raw.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        return None
    try:
        root_resolved = root.resolve(strict=True)
    except OSError:
        return None
    candidate = root_resolved.joinpath(*parts)
    try:
        candidate = candidate.resolve(strict=True)
    except OSError:
        return None
    if root_resolved not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


def content_type_for(path: Path) -> str:
    return _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def read_site_file(rel: str) -> tuple[bytes, str] | None:
    path = safe_file(SITE_DIR, rel)
    if path is None:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data, content_type_for(path)


def read_assets_file(rel: str) -> tuple[bytes, str] | None:
    path = safe_file(ASSETS_DIR, rel)
    if path is None:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data, content_type_for(path)


def _file_version(path: Path) -> str | None:
    try:
        return str(int(path.stat().st_mtime))
    except OSError:
        return None


def _resolve_public_asset(url_path: str) -> Path | None:
    """Map /site/… or /assets/… (no query) to a file under the matching root."""
    raw = (url_path or "").split("?", 1)[0].split("#", 1)[0]
    if raw.startswith("/site/"):
        return safe_file(SITE_DIR, raw[len("/site/") :])
    if raw.startswith("/assets/"):
        return safe_file(ASSETS_DIR, raw[len("/assets/") :])
    return None


def versioned_public_url(url: str) -> str:
    """Append ?v=<mtime> so browsers refetch when the file changes."""
    raw = (url or "").strip()
    if not raw or raw.startswith("data:") or raw.startswith("//"):
        return url
    path_part = raw
    prefix = ""
    for origin in ("https://twitchbot.games", "http://twitchbot.games"):
        if raw.startswith(origin + "/"):
            prefix = origin
            path_part = raw[len(origin) :]
            break
    path_only = path_part.split("?", 1)[0].split("#", 1)[0]
    path = _resolve_public_asset(path_only)
    if path is None:
        return url
    ver = _file_version(path)
    if ver is None:
        return url
    return f"{prefix}{path_only}?v={ver}"


_HTML_ASSET_RE = re.compile(
    r'(?P<pre>\b(?:src|href|content|data-image-src)\s*=\s*["\'])'
    r'(?P<url>(?:https?://twitchbot\.games)?/(?:site|assets)/[^"\'?#]+)'
    r'(?:[?#][^"\']*)?'
    r'(?P<post>["\'])',
    re.IGNORECASE,
)

_CSS_URL_RE = re.compile(
    r"url\(\s*(?P<quote>[\"']?)(?P<url>[^)\"'#?\s]+)(?:[?#][^)\"']*)?(?P=quote)\s*\)",
    re.IGNORECASE,
)


def apply_html_asset_versions(html: bytes) -> bytes:
    """Rewrite /site and /assets URLs in HTML with per-file mtime query."""
    try:
        text = html.decode("utf-8")
    except UnicodeDecodeError:
        return html

    def _sub(m: re.Match[str]) -> str:
        return m.group("pre") + versioned_public_url(m.group("url")) + m.group("post")

    return _HTML_ASSET_RE.sub(_sub, text).encode("utf-8")


def apply_css_asset_versions(css: bytes, css_rel: str) -> bytes:
    """Rewrite relative url(...) in a site CSS file to versioned /site/ paths."""
    try:
        text = css.decode("utf-8")
    except UnicodeDecodeError:
        return css
    css_dir = Path(css_rel.replace("\\", "/")).parent

    def _sub(m: re.Match[str]) -> str:
        raw = m.group("url").strip()
        quote = m.group("quote") or ""
        if (
            not raw
            or raw.startswith("data:")
            or raw.startswith("http://")
            or raw.startswith("https://")
            or raw.startswith("//")
        ):
            return m.group(0)
        if raw.startswith("/"):
            versioned = versioned_public_url(raw)
            return f"url({quote}{versioned}{quote})"
        # Relative to the CSS file (e.g. ../images/logo.png → /site/images/…).
        joined = (css_dir / raw).as_posix()
        parts = [p for p in joined.split("/") if p and p != "."]
        resolved: list[str] = []
        for part in parts:
            if part == "..":
                if resolved:
                    resolved.pop()
                continue
            resolved.append(part)
        public = "/site/" + "/".join(resolved)
        versioned = versioned_public_url(public)
        return f"url({quote}{versioned}{quote})"

    return _CSS_URL_RE.sub(_sub, text).encode("utf-8")


def landing_page() -> bytes | None:
    got = read_site_file("index.html")
    if got is None:
        return None
    return apply_html_asset_versions(got[0])


def sitemap_bytes() -> bytes | None:
    got = read_site_file("sitemap.xml")
    return None if got is None else got[0]


def robots_bytes() -> bytes | None:
    got = read_site_file("robots.txt")
    return None if got is None else got[0]


_LEADING_H1_RE = re.compile(r"^\s*<h1\b[^>]*>.*?</h1>\s*", re.IGNORECASE | re.DOTALL)


def extract_body_and_title(raw_html: str) -> tuple[str, str | None]:
    title_m = _TITLE_RE.search(raw_html)
    title = html.unescape(title_m.group(1).strip()) if title_m else None
    body_m = _BODY_RE.search(raw_html)
    body = body_m.group(1).strip() if body_m else raw_html
    return body, title


def strip_leading_h1(content_html: str) -> str:
    return _LEADING_H1_RE.sub("", content_html, count=1)


def _ui(lang: str) -> dict[str, str]:
    return _UI.get(lang) or _UI["en"]


def _lang_switch(path: str, lang: str) -> str:
    bits: list[str] = []
    for code in ("en", "ru", "uk", "it"):
        cls = ' class="is-active"' if code == lang else ""
        href = html.escape(f"{path}?lang={code}", quote=True)
        bits.append(
            f'<a href="{href}" data-lang="{code}"{cls}>{code.upper()}</a>'
        )
    return (
        '<span class="lang-switch" aria-label="Language">'
        + "".join(bits)
        + "</span>"
    )


def wrap_doc_page(
    *,
    lang: str,
    title: str,
    hero: str,
    lead: str,
    content_html: str,
    nav_current: str,
    path: str,
) -> bytes:
    u = _ui(lang)
    esc = html.escape

    def _nav_cls(key: str) -> str:
        return ' class="is-active"' if nav_current == key else ""

    switch = _lang_switch(path, lang)
    page = f"""<!DOCTYPE html>
<html lang="{esc(lang)}">
<head>
<meta charset="utf-8">
<title>{esc(title)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="{esc(lead)}">
<link rel="icon" href="/site/images/logo-square.png" type="image/png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,500;0,600;1,500&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/site/css/stillwater.css">
</head>
<body class="sw" id="top">
<div class="sw-shell">
  <header class="sw-nav">
    <a class="sw-logo" href="/?lang={esc(lang)}">
      <img src="/site/images/logo-square.png" alt="" width="28" height="28">
      {esc(u["brand"])}
    </a>
    <nav class="sw-nav-links" aria-label="Navigation">
      <a href="/?lang={esc(lang)}"{_nav_cls("home")}>{esc(u["nav_home"])}</a>
      <a href="/guide?lang={esc(lang)}"{_nav_cls("guide")}>{esc(u["nav_guide"])}</a>
      <a href="/webhook?lang={esc(lang)}"{_nav_cls("webhook")}>{esc(u["nav_webhook"])}</a>
      <span class="sw-lang">{switch}</span>
      <a class="sw-cta sw-cta-solid" href="{esc(BOT_TELEGRAM_FROM_SITE)}">{esc(u["nav_start"])}</a>
    </nav>
  </header>
  <section class="sw-hero">
    <h1>{esc(hero)}</h1>
    <p>{esc(lead)}</p>
  </section>
  <article class="sw-doc">
{content_html}
  </article>
  <footer class="sw-footer">
    <div>
      <h4>{esc(u["brand"])}</h4>
      <p>{esc(u["footer_blurb"])}</p>
    </div>
    <div>
      <h4>{esc(u["footer_contact"])}</h4>
      <p><a href="https://t.me/immarfa">@immarfa</a><br>
      <a href="mailto:biz@themarfa.name">biz@themarfa.name</a></p>
    </div>
    <div>
      <h4>{esc(u["footer_links"])}</h4>
      <ul>
        <li><a href="/?lang={esc(lang)}">{esc(u["footer_home"])}</a></li>
        <li><a href="/guide?lang={esc(lang)}">{esc(u["footer_guide"])}</a></li>
        <li><a href="/webhook?lang={esc(lang)}">{esc(u["footer_webhook"])}</a></li>
        <li><a href="/privacy?lang={esc(lang)}">{esc(u["footer_privacy"])}</a></li>
        <li><a href="/placeholders?lang={esc(lang)}">{esc(u["footer_placeholders"])}</a></li>
        <li><a href="https://github.com/Marfa/twitch-telegram-bot">{esc(u["footer_github"])}</a></li>
        <li><a href="https://www.donationalerts.com/r/themarfa">{esc(u["footer_donate"])}</a></li>
        <li><a href="https://nowpayments.io/donation/themarfa">{esc(u["footer_crypto"])}</a></li>
      </ul>
    </div>
    <div class="sw-copy">{esc(u["footer_copy"])}</div>
  </footer>
</div>
</body>
</html>
"""
    return apply_html_asset_versions(page.encode("utf-8"))


def guide_page(lang: str) -> bytes | None:
    from i18n import DEFAULT_LOCALE

    loc = normalize_lang(lang, default=DEFAULT_LOCALE)
    path = GUIDE_FILES.get(loc) or GUIDE_FILES[DEFAULT_LOCALE]
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    body, title = extract_body_and_title(raw)
    u = _ui(loc)
    return wrap_doc_page(
        lang=loc,
        title=title or u["guide_hero"],
        hero=u["guide_hero"],
        lead=u["guide_lead"],
        content_html=strip_leading_h1(body),
        nav_current="guide",
        path="/guide",
    )


def webhook_docs_page(lang: str, body_html: str) -> bytes:
    loc = normalize_lang(lang)
    u = _ui(loc)
    return wrap_doc_page(
        lang=loc,
        title=u["webhook_hero"],
        hero=u["webhook_hero"],
        lead=u["webhook_lead"],
        content_html=strip_leading_h1(body_html),
        nav_current="webhook",
        path="/webhook",
    )
