"""Twitch login → possible Telegram channel links (Helix bio, curated, crowd dests).

Helix bio + manual seeds are stored as curated. Crowd subscription destinations
are best-effort; the unofficial disclaimer applies only when no curated URLs exist.

Notice is sent only after manual stream-alert create (live/end/category/upcoming).
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

if TYPE_CHECKING:
    from db.protocol import Database
    from twitch import TwitchClient

logger = logging.getLogger(__name__)

SOURCE_HELIX = "helix"  # legacy; new Helix sync writes curated and drops helix rows
SOURCE_CURATED = "curated"
SOURCE_SUBSCRIPTION = "subscription"

# Manual wizard stream alerts only (not game / drops / import).
STREAM_ALERT_NOTICE_TYPES = frozenset({"live", "end", "category", "upcoming", ""})

# Manual ToS-safe seeds (public channel usernames known to belong to the streamer).
CURATED_LINKS: tuple[tuple[str, str], ...] = (
    ("marfapr", "https://t.me/themarfa"),
    ("marfapr", "https://t.me/themarfa_gaming"),
    ("marfapr", "https://t.me/aipchat"),
)

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


def seed_urls_for_login(twitch_login: str) -> list[str]:
    login = (twitch_login or "").strip().lower()
    if not login:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for seed_login, url in CURATED_LINKS:
        if seed_login != login:
            continue
        canon = normalize_telegram_public_url(url)
        if canon and canon not in seen:
            seen.add(canon)
            out.append(canon)
    return out


def sync_curated_from_helix(
    db: Database,
    twitch: TwitchClient,
    twitch_login: str,
) -> list[str]:
    """Fetch Helix bio TG links, merge with seeds, replace curated set for login.

    Returns the curated URL list after sync (bio ∪ seeds). Helix lookup failure
    keeps existing curated and only re-applies seeds.
    """
    login = (twitch_login or "").strip().lower()
    if not login:
        return []
    seeds = seed_urls_for_login(login)
    bio_urls: list[str] = []
    helix_ok = False
    try:
        user = twitch.get_user(login)
        helix_ok = True
        if user:
            bio_urls = extract_telegram_urls_from_text(
                str(user.get("description") or "")
            )
    except Exception:
        logger.warning("Helix user lookup failed for %s", login, exc_info=True)

    if helix_ok:
        wanted = list(dict.fromkeys([*bio_urls, *seeds]))
        db.replace_curated_streamer_telegram_links(login, wanted)
        return wanted

    # Helix unavailable: do not wipe curated; ensure seeds still present.
    for url in seeds:
        db.upsert_streamer_telegram_link(
            twitch_login=login,
            url=url,
            source=SOURCE_CURATED,
        )
    return db.list_streamer_telegram_urls(login, source=SOURCE_CURATED)


def collect_helix_description_links(
    db: Database,
    twitch: TwitchClient,
    twitch_login: str,
) -> list[str]:
    """Sync Helix bio (+ seeds) into curated; returns curated URLs after sync."""
    return sync_curated_from_helix(db, twitch, twitch_login)


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
    """URLs for the notice: curated only when any exist, else all sources."""
    login = (twitch_login or "").strip().lower()
    if not login:
        return []
    curated = db.list_streamer_telegram_urls(login, source=SOURCE_CURATED)
    if curated:
        return curated
    return db.list_streamer_telegram_urls(login)


def format_links_block(urls: list[str]) -> str:
    return "\n".join(f"• {u}" for u in urls)


def should_send_streamer_telegram_notice(alert_type: str | None) -> bool:
    kind = str(alert_type or "").strip().lower()
    return kind in STREAM_ALERT_NOTICE_TYPES


def streamer_telegram_notice_suppressed(
    db: Database, user_id: int, twitch_login: str
) -> bool:
    if db.get_hide_streamer_telegram_all(int(user_id)):
        return True
    return db.is_streamer_telegram_hidden_for(int(user_id), twitch_login)


def streamer_telegram_notice_keyboard(
    lang: str,
    *,
    twitch_login: str,
    hide_streamer: bool,
    hide_all: bool,
) -> InlineKeyboardMarkup:
    from i18n import t

    login = (twitch_login or "").strip().lower()
    s_mark = "✅ " if hide_streamer else "⬜️ "
    a_mark = "✅ " if hide_all else "⬜️ "
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    s_mark + t("streamer_telegram_hide_streamer", lang),
                    callback_data=f"stg_pref:s:{login}",
                )
            ],
            [
                InlineKeyboardButton(
                    a_mark + t("streamer_telegram_hide_all", lang),
                    callback_data=f"stg_pref:a:{login}",
                )
            ],
        ]
    )


def seed_curated_links(db: Database) -> int:
    """Upsert CURATED_LINKS. Returns how many URLs were newly listed for their login."""
    added = 0
    for login, url in CURATED_LINKS:
        canon = normalize_telegram_public_url(url)
        if not canon:
            continue
        before = set(db.list_streamer_telegram_urls(login, source=SOURCE_CURATED))
        db.upsert_streamer_telegram_link(
            twitch_login=login,
            url=canon,
            source=SOURCE_CURATED,
        )
        after = set(db.list_streamer_telegram_urls(login, source=SOURCE_CURATED))
        added += len(after - before)
    return added


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
    alert_type: str | None = None,
) -> None:
    """Sync curated from Helix, record non-DM dest, send notice if any URLs exist."""
    from bot_helpers import _menu, _pulse_reply_keyboard
    from i18n import t

    if not should_send_streamer_telegram_notice(alert_type):
        return

    login = (twitch_login or "").strip().lower()
    if not login:
        return

    # Always sync/record so curated table stays fresh even when notice is hidden.
    if twitch is not None:
        try:
            await asyncio.to_thread(sync_curated_from_helix, db, twitch, login)
        except Exception:
            logger.warning("Helix TG collect failed for %s", login, exc_info=True)
    else:
        for url in seed_urls_for_login(login):
            db.upsert_streamer_telegram_link(
                twitch_login=login,
                url=url,
                source=SOURCE_CURATED,
            )

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

    if streamer_telegram_notice_suppressed(db, owner_id, login):
        return

    urls = list_possible_telegram_urls(db, login)
    if not urls:
        return
    curated = db.list_streamer_telegram_urls(login, source=SOURCE_CURATED)
    notice_key = (
        "streamer_telegram_notice_curated"
        if curated
        else "streamer_telegram_notice"
    )
    text = t(
        notice_key,
        lang,
        twitch_username=login,
        links=format_links_block(urls),
    )
    hide_streamer = db.is_streamer_telegram_hidden_for(owner_id, login)
    hide_all = db.get_hide_streamer_telegram_all(owner_id)
    markup = streamer_telegram_notice_keyboard(
        lang,
        twitch_login=login,
        hide_streamer=hide_streamer,
        hide_all=hide_all,
    )
    try:
        await bot.send_message(
            owner_id,
            text,
            disable_web_page_preview=True,
            reply_markup=markup,
        )
        await _pulse_reply_keyboard(bot, owner_id, _menu(lang, owner_id))
    except Exception:
        logger.warning(
            "Failed to send streamer TG notice to %s for %s",
            owner_id,
            login,
            exc_info=True,
        )
