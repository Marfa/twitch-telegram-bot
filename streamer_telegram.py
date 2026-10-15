"""Twitch login → possible Telegram channel links (Helix bio, curated, crowd dests).

Sources are best-effort. Never treat the list as official streamer affiliation.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from db.protocol import Database
    from twitch import TwitchClient

logger = logging.getLogger(__name__)

SOURCE_HELIX = "helix"
SOURCE_CURATED = "curated"
SOURCE_SUBSCRIPTION = "subscription"

# Public t.me / telegram.me usernames in free text (Helix description).
_TG_IN_TEXT = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?(@?[A-Za-z0-9_]{4,32})\b",
    re.IGNORECASE,
)
_TG_SKIP_SEGMENTS = frozenset(
    {
        "s",
        "joinchat",
        "addstickers",
        "share",
        "proxy",
        "socks",
        "iv",
        "c",
        "boost",
        "nft",
    }
)


def normalize_telegram_public_url(raw: str) -> str | None:
    """Return canonical https://t.me/<username> or None if not a public username link."""
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("@"):
        user = text[1:].strip()
        if _ok_username(user):
            return f"https://t.me/{user.lower()}"
        return None
    if "://" not in text and "/" not in text and _ok_username(text):
        return f"https://t.me/{text.lower()}"
    try:
        parsed = urlparse(text if "://" in text else f"https://{text}")
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if host not in ("t.me", "telegram.me", "www.t.me", "www.telegram.me"):
        return None
    parts = [p for p in (parsed.path or "").split("/") if p]
    if not parts:
        return None
    if parts[0].lower() == "s" and len(parts) >= 2:
        user = parts[1]
    else:
        user = parts[0]
    user = user.lstrip("@")
    if user.lower() in _TG_SKIP_SEGMENTS:
        return None
    if not _ok_username(user):
        return None
    return f"https://t.me/{user.lower()}"


def _ok_username(user: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_]{4,32}", user or ""))


def extract_telegram_urls_from_text(text: str) -> list[str]:
    """Deduped canonical t.me URLs found in free text."""
    found: list[str] = []
    seen: set[str] = set()
    for m in _TG_IN_TEXT.finditer(text or ""):
        url = normalize_telegram_public_url(m.group(0))
        if url and url not in seen:
            seen.add(url)
            found.append(url)
    return found


def telegram_url_from_chat_username(username: str | None) -> str | None:
    if not username:
        return None
    return normalize_telegram_public_url(f"@{username.lstrip('@')}")


def collect_helix_description_links(
    db: Database,
    twitch: TwitchClient,
    twitch_login: str,
) -> list[str]:
    """Parse Helix user.description; upsert helix-sourced links. Returns new canonical URLs."""
    login = (twitch_login or "").strip().lower()
    if not login:
        return []
    try:
        user = twitch.get_user(login)
    except Exception:
        logger.warning("Helix user lookup failed for %s", login, exc_info=True)
        return []
    if not user:
        return []
    urls = extract_telegram_urls_from_text(str(user.get("description") or ""))
    for url in urls:
        db.upsert_streamer_telegram_link(
            twitch_login=login,
            url=url,
            source=SOURCE_HELIX,
        )
    return urls


def record_subscription_dest_link(
    db: Database,
    *,
    twitch_login: str,
    dest_type: str,
    chat_username: str | None,
    chat_id: int | None = None,
) -> str | None:
    """If dest is a public channel/group, record as crowd subscription link."""
    login = (twitch_login or "").strip().lower()
    if not login:
        return None
    if (dest_type or "").strip().lower() in ("", "dm"):
        return None
    url = telegram_url_from_chat_username(chat_username)
    if not url:
        return None
    db.upsert_streamer_telegram_link(
        twitch_login=login,
        url=url,
        source=SOURCE_SUBSCRIPTION,
        chat_id=chat_id,
    )
    return url


def list_possible_telegram_urls(db: Database, twitch_login: str) -> list[str]:
    login = (twitch_login or "").strip().lower()
    if not login:
        return []
    return db.list_streamer_telegram_urls(login)


def format_links_block(urls: list[str]) -> str:
    return "\n".join(f"• {u}" for u in urls)


async def after_subscription_created_telegram_hint(
    *,
    bot,
    db: Database,
    twitch: TwitchClient | None,
    owner_id: int,
    lang: str,
    twitch_login: str,
    dest_type: str,
    chat_id: int,
) -> None:
    """Collect links (Helix + non-DM dest) and send a disclaimer notice if any exist."""
    from i18n import t

    login = (twitch_login or "").strip().lower()
    if not login:
        return

    if twitch is not None:
        try:
            await asyncio.to_thread(
                collect_helix_description_links, db, twitch, login
            )
        except Exception:
            logger.warning("Helix TG collect failed for %s", login, exc_info=True)

    if (dest_type or "").strip().lower() not in ("", "dm"):
        chat_username = None
        try:
            chat = await bot.get_chat(chat_id)
            chat_username = getattr(chat, "username", None)
        except Exception:
            logger.info(
                "Cannot resolve dest username for chat_id=%s (twitch=%s)",
                chat_id,
                login,
            )
        record_subscription_dest_link(
            db,
            twitch_login=login,
            dest_type=dest_type,
            chat_username=chat_username,
            chat_id=chat_id,
        )

    urls = list_possible_telegram_urls(db, login)
    if not urls:
        return
    text = t(
        "streamer_telegram_notice",
        lang,
        twitch_username=login,
        links=format_links_block(urls),
    )
    try:
        await bot.send_message(
            owner_id,
            text,
            disable_web_page_preview=True,
        )
    except Exception:
        logger.warning(
            "Failed to send streamer TG notice to %s for %s",
            owner_id,
            login,
            exc_info=True,
        )
