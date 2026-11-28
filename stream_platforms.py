"""Standalone live alerts for non-Twitch platforms (YouTube, TikTok, VK Play, GG, Kick)."""
from __future__ import annotations

import logging
import re
from urllib.parse import unquote, urlparse

from multistream import (
    MultistreamChannel,
    _GG_HOSTS,
    _SESSION,
    _TT_HOSTS,
    _VK_HOSTS,
    _YT_HOSTS,
    _canonical_stream_url,
    _live_state as _multistream_live_state,
    parse_multistream_url,
)

logger = logging.getLogger(__name__)

EXTERNAL_PLATFORMS = (
    "youtube",
    "tiktok",
    "vkplay",
    "goodgame",
    "kick",
)

_KICK_HOSTS = frozenset({"kick.com", "www.kick.com"})

_EXTERNAL_HOSTS = (
    _GG_HOSTS
    | _VK_HOSTS
    | _YT_HOSTS
    | _TT_HOSTS
    | _KICK_HOSTS
)


def external_user_id(platform: str, channel_id: str) -> str:
    plat = (platform or "").strip().lower()
    cid = (channel_id or "").strip()
    return f"ext:{plat}:{cid}"


def parse_external_user_id(uid: str) -> tuple[str, str] | None:
    raw = (uid or "").strip()
    if not raw.startswith("ext:"):
        return None
    parts = raw.split(":", 2)
    if len(parts) != 3:
        return None
    platform, channel_id = parts[1].strip().lower(), parts[2].strip()
    if platform not in EXTERNAL_PLATFORMS or not channel_id:
        return None
    return platform, channel_id


def is_external_subscription(
    *,
    stream_platform: str | None = None,
    twitch_user_id: str | None = None,
) -> bool:
    plat = (stream_platform or "").strip().lower()
    if plat == "twitch_team":
        return False
    if plat and plat != "twitch":
        return True
    return bool(parse_external_user_id(twitch_user_id or ""))


def is_twitch_team_subscription(*, stream_platform: str | None = None) -> bool:
    return (stream_platform or "").strip().lower() == "twitch_team"


def looks_like_external_stream_url(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    candidate = raw if "://" in raw else f"https://{raw}"
    try:
        host = (urlparse(candidate).hostname or "").lower()
    except Exception:
        return False
    return host in _EXTERNAL_HOSTS


def parse_external_stream_url(text: str) -> MultistreamChannel | None:
    """Parse a single non-Twitch stream URL (YouTube / TikTok / VK / GG / Kick)."""
    raw = (text or "").strip()
    if not raw:
        return None
    # Reuse multistream parsers for GG / VK / YouTube / TikTok.
    ch = parse_multistream_url(raw)
    if ch is not None:
        return ch
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
    except Exception:
        return None
    host = (parsed.hostname or "").lower()
    if host in _KICK_HOSTS:
        return _parse_kick(parsed)
    return None


def external_live_state(ch: MultistreamChannel) -> tuple[bool, str]:
    """Return (online, stream_url) for an external channel."""
    if ch.platform in ("goodgame", "vkplay", "youtube", "tiktok"):
        return _multistream_live_state(ch)
    if ch.platform == "kick":
        online, live_url = _kick_live_state(ch.channel_id)
        return online, live_url or _canonical_external_url(ch)
    return False, _canonical_external_url(ch)


def is_external_online(ch: MultistreamChannel) -> bool:
    try:
        online, _ = external_live_state(ch)
        return online
    except Exception:
        logger.exception(
            "External live check failed platform=%s id=%s", ch.platform, ch.channel_id
        )
        return False


def channel_from_subscription(
    *,
    stream_platform: str,
    twitch_username: str,
    twitch_user_id: str,
    stream_url: str = "",
) -> MultistreamChannel | None:
    parsed = parse_external_user_id(twitch_user_id)
    platform = (stream_platform or "").strip().lower()
    channel_id = ""
    if parsed:
        platform, channel_id = parsed
    elif platform in EXTERNAL_PLATFORMS:
        channel_id = (twitch_username or "").strip().lstrip("@")
    if platform not in EXTERNAL_PLATFORMS or not channel_id:
        return None
    url = (stream_url or "").strip() or _default_url(platform, channel_id)
    label = (twitch_username or "").strip() or channel_id
    return MultistreamChannel(
        platform=platform, url=url, channel_id=channel_id, label=label
    )


def _canonical_external_url(ch: MultistreamChannel) -> str:
    if ch.platform in ("goodgame", "vkplay", "youtube", "tiktok"):
        return _canonical_stream_url(ch)
    return _default_url(ch.platform, ch.channel_id) or (ch.url or "")


def _default_url(platform: str, channel_id: str) -> str:
    cid = (channel_id or "").strip().lstrip("@")
    if platform == "kick":
        return f"https://kick.com/{cid}"
    if platform == "tiktok":
        return f"https://www.tiktok.com/@{cid}/live"
    if platform == "youtube":
        return f"https://www.youtube.com/channel/{cid}/live"
    if platform == "goodgame":
        return f"https://goodgame.ru/{cid}"
    if platform == "vkplay":
        return f"https://live.vkvideo.ru/{cid}"
    return ""


def _parse_kick(parsed) -> MultistreamChannel | None:
    parts = [unquote(p) for p in (parsed.path or "").split("/") if p]
    if not parts:
        return None
    slug = parts[0].strip()
    if not slug or slug.lower() in (
        "api",
        "categories",
        "category",
        "clips",
        "video",
        "videos",
        "search",
        "settings",
        "emotes",
    ):
        return None
    if not re.fullmatch(r"[\w.\-]{2,64}", slug, re.UNICODE):
        return None
    canon = f"https://kick.com/{slug}"
    return MultistreamChannel(
        platform="kick", url=canon, channel_id=slug, label=slug
    )


def _kick_live_state(slug: str) -> tuple[bool, str]:
    """Kick public channel API: livestream object present when live.

    Raises on transport / unexpected HTTP errors so callers can treat the
    result as unknown (do not flip live→offline on a blip).
    """
    name = (slug or "").strip()
    if not name:
        return False, ""
    resp = _SESSION.get(
        f"https://kick.com/api/v2/channels/{name}",
        headers={"Accept": "application/json", "Referer": f"https://kick.com/{name}"},
        timeout=12,
    )
    if resp.status_code == 404:
        return False, f"https://kick.com/{name}"
    if resp.status_code != 200:
        logger.warning("Kick status HTTP %s for %s", resp.status_code, name)
        raise RuntimeError(f"Kick HTTP {resp.status_code}")
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError("Kick response is not an object")
    live = data.get("livestream")
    if live and isinstance(live, dict):
        return True, f"https://kick.com/{name}"
    return False, f"https://kick.com/{name}"
