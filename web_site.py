"""Public marketing site (Dazzle landing) + shared chrome for /guide and /webhook."""

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

_UI = {
    "en": {
        "brand": "Twitch → Telegram",
        "nav_home": "Home",
        "nav_guide": "Guide",
        "nav_webhook": "Webhook",
        "nav_start": "Open bot",
        "footer_blurb": (
            "Twitch stream alerts for Telegram and Discord. "
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
        "footer_copy": "© Marfa — Twitch → Telegram bot",
        "footer_design": "Design by",
        "guide_hero": "User guide",
        "guide_lead": "How to set up alerts, destinations, Premium, and everyday menus.",
        "webhook_hero": "Outbound webhook",
        "webhook_lead": "HTTPS JSON delivery contract with HMAC for integrators.",
    },
    "ru": {
        "brand": "Twitch → Telegram",
        "nav_home": "Главная",
        "nav_guide": "Инструкция",
        "nav_webhook": "Webhook",
        "nav_start": "Открыть бота",
        "footer_blurb": (
            "Оповещения о стримах Twitch для Telegram и Discord. "
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
        "footer_copy": "© Marfa — Twitch → Telegram бот",
        "footer_design": "Дизайн",
        "guide_hero": "Инструкция",
        "guide_lead": "Как настроить оповещения, куда слать, Premium и повседневные меню.",
        "webhook_hero": "Outbound webhook",
        "webhook_lead": "Контракт HTTPS JSON + HMAC для интеграторов.",
    },
    "uk": {
        "brand": "Twitch → Telegram",
        "nav_home": "Головна",
        "nav_guide": "Інструкція",
        "nav_webhook": "Webhook",
        "nav_start": "Відкрити бота",
        "footer_blurb": (
            "Сповіщення про стріми Twitch для Telegram і Discord. "
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
        "footer_copy": "© Marfa — Twitch → Telegram бот",
        "footer_design": "Дизайн",
        "guide_hero": "Інструкція",
        "guide_lead": "Як налаштувати сповіщення, куди надсилати, Premium і меню.",
        "webhook_hero": "Outbound webhook",
        "webhook_lead": "Контракт HTTPS JSON + HMAC для інтеграторів.",
    },
    "it": {
        "brand": "Twitch → Telegram",
        "nav_home": "Home",
        "nav_guide": "Guida",
        "nav_webhook": "Webhook",
        "nav_start": "Apri il bot",
        "footer_blurb": (
            "Avvisi Twitch per Telegram e Discord. "
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
        "footer_copy": "© Marfa — bot Twitch → Telegram",
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


def landing_page() -> bytes | None:
    got = read_site_file("index.html")
    return None if got is None else got[0]


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
    nav = {
        "home": "",
        "guide": "",
        "webhook": "",
    }
    if nav_current in nav:
        nav[nav_current] = ' class="current"'
    switch = _lang_switch(path, lang)
    page = f"""<!DOCTYPE html>
<html class="no-js" lang="{esc(lang)}">
<head>
<meta charset="utf-8">
<title>{esc(title)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="{esc(lead)}">
<link rel="stylesheet" href="/site/css/base.css">
<link rel="stylesheet" href="/site/css/vendor.css">
<link rel="stylesheet" href="/site/css/main.css">
<link rel="stylesheet" href="/site/css/site.css">
<script src="/site/js/modernizr.js"></script>
<script src="/site/js/pace.min.js"></script>
<link rel="shortcut icon" href="/site/favicon.ico" type="image/x-icon">
</head>
<body id="top" class="page-docs">
<header id="header" class="row">
  <div class="header-logo"><a href="/?lang={esc(lang)}">{esc(u["brand"])}</a></div>
  <nav id="header-nav-wrap">
    <ul class="header-main-nav">
      <li{nav["home"]}><a href="/?lang={esc(lang)}">{esc(u["nav_home"])}</a></li>
      <li{nav["guide"]}><a href="/guide?lang={esc(lang)}">{esc(u["nav_guide"])}</a></li>
      <li{nav["webhook"]}><a href="/webhook?lang={esc(lang)}">{esc(u["nav_webhook"])}</a></li>
    </ul>
    {switch}
    <a href="https://t.me/twitch2telegram_bot" class="button button-primary cta">{esc(u["nav_start"])}</a>
  </nav>
  <a class="header-menu-toggle" href="#"><span>Menu</span></a>
</header>
<section class="doc-hero">
  <div class="row">
    <div class="col-twelve">
      <h1>{esc(hero)}</h1>
      <p>{esc(lead)}</p>
    </div>
  </div>
</section>
<section class="doc-wrap">
  <div class="row">
    <div class="col-twelve">
      <article class="doc-content">
{content_html}
      </article>
    </div>
  </div>
</section>
<footer>
  <div class="footer-main">
    <div class="row">
      <div class="col-four tab-full footer-info">
        <div class="footer-logo"></div>
        <p>{esc(u["footer_blurb"])}</p>
      </div>
      <div class="col-four tab-full footer-contact">
        <h4>{esc(u["footer_contact"])}</h4>
        <p><a href="https://t.me/immarfa">@immarfa</a><br>
        <a href="mailto:biz@themarfa.name">biz@themarfa.name</a></p>
      </div>
      <div class="col-four tab-full footer-site-links">
        <h4>{esc(u["footer_links"])}</h4>
        <ul class="list-links">
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
    </div>
  </div>
  <div class="footer-bottom">
    <div class="row">
      <div class="col-twelve">
        <div class="copyright">
          <span>{esc(u["footer_copy"])}</span>
          <span>{esc(u["footer_design"])} <a href="https://www.styleshout.com/">styleshout</a> (Dazzle)</span>
        </div>
        <div id="go-top"><a class="smoothscroll" title="Back to Top" href="#top"><i class="icon-arrow-up"></i></a></div>
      </div>
    </div>
  </div>
</footer>
<div id="preloader"><div id="loader"></div></div>
<script src="/site/js/jquery-2.1.3.min.js"></script>
<script src="/site/js/plugins.js"></script>
<script src="/site/js/main.js"></script>
<script src="/site/js/i18n.js"></script>
</body>
</html>
"""
    return page.encode("utf-8")


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
