"""Multistream gate: send Twitch live alert only if all linked platforms are online."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import requests

from config import YOUTUBE_API_KEY

logger = logging.getLogger(__name__)

FEATURE_ID = "multistream"
MULTISTREAM_MAX = 5
PLATFORMS = ("goodgame", "vkplay", "youtube")

# Telegram equivalent of Chat Mini App status dots (live #eb0400 / offline #adadb8).
STATUS_ONLINE = "🔴"
STATUS_OFFLINE = "⚪"
STATUS_PLACEHOLDERS = (
    "goodgame_status",
    "vkplay_status",
    "youtube_status",
)
_PLATFORM_STATUS_KEY = {
    "goodgame": "goodgame_status",
    "vkplay": "vkplay_status",
    "youtube": "youtube_status",
}

_GG_HOSTS = frozenset({"goodgame.ru", "www.goodgame.ru", "goodgame.com", "www.goodgame.com"})
_VK_HOSTS = frozenset(
    {
        "live.vkvideo.ru",
        "www.live.vkvideo.ru",
        "vkplay.live",
        "www.vkplay.live",
        "live.vkplay.ru",
        "www.live.vkplay.ru",
    }
)
_YT_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }
)


def error_line_looks_like_youtube(line: str) -> bool:
    """True when a failed multistream line is a YouTube URL (hostname check)."""
    raw = (line or "").strip()
    if not raw:
        return False
    candidate = raw if "://" in raw else f"https://{raw}"
    host = (urlparse(candidate).hostname or "").lower()
    return host in _YT_HOSTS

_SESSION = requests.Session()
_SESSION.headers.update(
    {
        "User-Agent": "Mozilla/5.0 (compatible; MarfaTwitchTelegramBot/1.0)",
        "Accept": "application/json,text/html,*/*",
    }
)


@dataclass(frozen=True)
class MultistreamChannel:
    platform: str
    url: str
    channel_id: str
    label: str = ""


def parse_multistream_channels(raw: str | None) -> list[MultistreamChannel]:
    if not raw or not str(raw).strip():
        return []
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    out: list[MultistreamChannel] = []
    seen: set[tuple[str, str]] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        platform = str(item.get("platform") or "").strip().lower()
        url = str(item.get("url") or "").strip()
        channel_id = str(item.get("channel_id") or item.get("id") or "").strip()
        label = str(item.get("label") or "").strip()
        if platform not in PLATFORMS or not url or not channel_id:
            continue
        key = (platform, channel_id.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(
            MultistreamChannel(
                platform=platform,
                url=url,
                channel_id=channel_id,
                label=label or channel_id,
            )
        )
        if len(out) >= MULTISTREAM_MAX:
            break
    return out


def dump_multistream_channels(channels: list[MultistreamChannel] | None) -> str:
    cleaned = parse_multistream_channels(
        json.dumps(
            [
                {
                    "platform": c.platform,
                    "url": c.url,
                    "channel_id": c.channel_id,
                    "label": c.label,
                }
                for c in (channels or [])
            ],
            ensure_ascii=False,
        )
    )
    if not cleaned:
        return "[]"
    return json.dumps(
        [
            {
                "platform": c.platform,
                "url": c.url,
                "channel_id": c.channel_id,
                "label": c.label,
            }
            for c in cleaned
        ],
        ensure_ascii=False,
    )


def parse_multistream_url(text: str) -> MultistreamChannel | None:
    """Parse a single platform URL into a MultistreamChannel (may hit YouTube API)."""
    raw = (text or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
    except Exception:
        return None
    host = (parsed.hostname or "").lower()
    if host in _GG_HOSTS:
        return _parse_goodgame(parsed, raw)
    if host in _VK_HOSTS:
        return _parse_vkplay(parsed, raw)
    if host in _YT_HOSTS:
        return _parse_youtube(parsed, raw)
    return None


def parse_multistream_lines(text: str) -> tuple[list[MultistreamChannel], list[str]]:
    """Parse multiline input. Returns (ok_channels, error_lines)."""
    channels: list[MultistreamChannel] = []
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        ch = parse_multistream_url(line)
        if ch is None:
            errors.append(line)
            continue
        key = (ch.platform, ch.channel_id.lower())
        if key in seen:
            continue
        seen.add(key)
        channels.append(ch)
        if len(channels) >= MULTISTREAM_MAX:
            break
    return channels, errors


def all_channels_online(channels: list[MultistreamChannel]) -> bool:
    """Synchronous one-shot check: True iff every channel is currently live."""
    if not channels:
        return True
    for ch in channels:
        try:
            if not _is_online(ch):
                return False
        except Exception:
            logger.exception(
                "Multistream check failed platform=%s id=%s", ch.platform, ch.channel_id
            )
            return False
    return True


def status_placeholders(
    raw: str | None,
    template: str | None = None,
) -> dict[str, str]:
    """Map {goodgame,vkplay,youtube}_status → 🔴 / ⚪ / — for the alert template.

    Unconfigured platforms stay «—». Only platforms mentioned in the template
    (or all three if template is omitted) are live-checked.
    """
    text = template or ""
    needed = [
        key
        for key in STATUS_PLACEHOLDERS
        if not text or f"{{{key}}}" in text
    ]
    out = {key: "—" for key in STATUS_PLACEHOLDERS}
    if not needed:
        return out
    by_platform = {c.platform: c for c in parse_multistream_channels(raw)}
    for platform, key in _PLATFORM_STATUS_KEY.items():
        if key not in needed:
            continue
        ch = by_platform.get(platform)
        if ch is None:
            continue
        try:
            out[key] = STATUS_ONLINE if _is_online(ch) else STATUS_OFFLINE
        except Exception:
            logger.exception(
                "Multistream status placeholder failed platform=%s id=%s",
                ch.platform,
                ch.channel_id,
            )
            out[key] = STATUS_OFFLINE
    return out


def _is_online(ch: MultistreamChannel) -> bool:
    if ch.platform == "goodgame":
        return _goodgame_online(ch.channel_id)
    if ch.platform == "vkplay":
        return _vkplay_online(ch.channel_id)
    if ch.platform == "youtube":
        return _youtube_online(ch.channel_id)
    return False


def _parse_goodgame(parsed, raw: str) -> MultistreamChannel | None:
    parts = [unquote(p) for p in (parsed.path or "").split("/") if p]
    # /channel/login or /login or /api/... skip
    login = ""
    if parts and parts[0].lower() == "channel" and len(parts) >= 2:
        login = parts[1]
    elif parts and parts[0].lower() not in ("api", "embed", "clip", "clips"):
        login = parts[0]
    login = login.strip()
    if not login or len(login) > 64:
        return None
    canon = f"https://goodgame.ru/{login}"
    return MultistreamChannel(
        platform="goodgame", url=canon, channel_id=login, label=login
    )


def _parse_vkplay(parsed, raw: str) -> MultistreamChannel | None:
    parts = [unquote(p) for p in (parsed.path or "").split("/") if p]
    if not parts:
        return None
    slug = parts[0].strip()
    if not slug or slug.lower() in ("app", "api", "record", "clip"):
        return None
    if not re.fullmatch(r"[\w.\-]{2,64}", slug, re.UNICODE):
        return None
    host = (parsed.hostname or "live.vkvideo.ru").lower()
    if host.startswith("www."):
        host = host[4:]
    canon = f"https://{host}/{slug}"
    return MultistreamChannel(
        platform="vkplay", url=canon, channel_id=slug, label=slug
    )


def _parse_youtube(parsed, raw: str) -> MultistreamChannel | None:
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    parts = [unquote(p) for p in path.split("/") if p]
    channel_id = ""
    label = ""

    if host in ("youtu.be", "www.youtu.be"):
        # Short links are videos, not channels — reject.
        return None

    if parts:
        head = parts[0].lower()
        if head == "channel" and len(parts) >= 2:
            channel_id = parts[1]
            label = channel_id
        elif head == "@" or (parts[0].startswith("@") and len(parts[0]) > 1):
            handle = parts[0].lstrip("@")
            resolved = _youtube_resolve_handle(handle)
            if not resolved:
                return None
            channel_id, label = resolved, f"@{handle}"
        elif head == "c" and len(parts) >= 2:
            resolved = _youtube_resolve_for_custom(parts[1])
            if not resolved:
                return None
            channel_id, label = resolved, parts[1]
        elif head == "user" and len(parts) >= 2:
            resolved = _youtube_resolve_username(parts[1])
            if not resolved:
                return None
            channel_id, label = resolved, parts[1]
        elif parts[0].startswith("UC") and len(parts[0]) >= 20:
            channel_id = parts[0]
            label = channel_id

    if not channel_id:
        qs = parse_qs(parsed.query or "")
        # /watch?v= is a video — not accepted as a channel link.
        return None

    if not re.fullmatch(r"UC[\w-]{20,}", channel_id):
        return None
    canon = f"https://www.youtube.com/channel/{channel_id}"
    return MultistreamChannel(
        platform="youtube",
        url=canon,
        channel_id=channel_id,
        label=label or channel_id,
    )


def _youtube_api_get(path: str, params: dict[str, Any]) -> dict[str, Any] | None:
    key = (YOUTUBE_API_KEY or "").strip()
    if not key:
        logger.warning("YOUTUBE_API_KEY unset — cannot resolve/check YouTube multistream")
        return None
    params = dict(params)
    params["key"] = key
    try:
        resp = _SESSION.get(
            f"https://www.googleapis.com/youtube/v3/{path}",
            params=params,
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning(
                "YouTube API %s HTTP %s: %s",
                path,
                resp.status_code,
                (resp.text or "")[:200],
            )
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:
        logger.exception("YouTube API %s failed", path)
        return None


def _youtube_resolve_handle(handle: str) -> str | None:
    data = _youtube_api_get(
        "channels",
        {"part": "id", "forHandle": handle.lstrip("@")},
    )
    items = (data or {}).get("items") or []
    if items and isinstance(items[0], dict):
        cid = str(items[0].get("id") or "").strip()
        return cid or None
    return None


def _youtube_resolve_username(username: str) -> str | None:
    data = _youtube_api_get(
        "channels",
        {"part": "id", "forUsername": username},
    )
    items = (data or {}).get("items") or []
    if items and isinstance(items[0], dict):
        cid = str(items[0].get("id") or "").strip()
        return cid or None
    return None


def _youtube_resolve_for_custom(custom: str) -> str | None:
    # Best-effort: search channels by exact custom URL name.
    data = _youtube_api_get(
        "search",
        {
            "part": "snippet",
            "type": "channel",
            "q": custom,
            "maxResults": 5,
        },
    )
    for item in (data or {}).get("items") or []:
        if not isinstance(item, dict):
            continue
        cid = str((item.get("id") or {}).get("channelId") or "").strip()
        title = str((item.get("snippet") or {}).get("title") or "")
        if cid and custom.lower() in title.lower().replace(" ", ""):
            return cid
    items = (data or {}).get("items") or []
    if items and isinstance(items[0], dict):
        cid = str((items[0].get("id") or {}).get("channelId") or "").strip()
        return cid or None
    return None


def _goodgame_online(login: str) -> bool:
    url = f"https://goodgame.ru/api/4/users/{login}/stream"
    try:
        resp = _SESSION.get(url, timeout=12)
        if resp.status_code == 404:
            # Fallback to legacy status API.
            return _goodgame_online_legacy(login)
        if resp.status_code != 200:
            logger.warning("GoodGame status HTTP %s for %s", resp.status_code, login)
            return False
        data = resp.json()
        if isinstance(data, dict) and "online" in data:
            return bool(data.get("online"))
    except Exception:
        logger.exception("GoodGame status failed for %s", login)
    return _goodgame_online_legacy(login)


def _goodgame_online_legacy(login: str) -> bool:
    try:
        resp = _SESSION.get(
            "https://goodgame.ru/api/getchannelstatus",
            params={"id": login, "fmt": "json"},
            timeout=12,
        )
        if resp.status_code != 200:
            return False
        data = resp.json()
        # Response shape: { "<id>": { "status": "Live"|..., ... } } or list-like.
        if isinstance(data, dict):
            for val in data.values():
                if not isinstance(val, dict):
                    continue
                status = str(val.get("status") or "").lower()
                if status in ("live", "online", "1", "true"):
                    return True
                if val.get("online") is True:
                    return True
        return False
    except Exception:
        logger.exception("GoodGame legacy status failed for %s", login)
        return False


def _vkplay_online(slug: str) -> bool:
    url = f"https://api.live.vkvideo.ru/v1/blog/{slug}/public_video_stream"
    try:
        resp = _SESSION.get(
            url,
            headers={"Referer": f"https://live.vkvideo.ru/{slug}"},
            timeout=12,
        )
        if resp.status_code == 404:
            return False
        if resp.status_code != 200:
            logger.warning("VK Play status HTTP %s for %s", resp.status_code, slug)
            return False
        data = resp.json()
        if not isinstance(data, dict):
            return False
        if data.get("error"):
            return False
        stream_data = data.get("data")
        if isinstance(stream_data, list):
            return len(stream_data) > 0
        if isinstance(stream_data, dict):
            return bool(stream_data)
        return False
    except Exception:
        logger.exception("VK Play status failed for %s", slug)
        return False


def _youtube_online(channel_id: str) -> bool:
    data = _youtube_api_get(
        "search",
        {
            "part": "snippet",
            "channelId": channel_id,
            "type": "video",
            "eventType": "live",
            "maxResults": 1,
        },
    )
    if data is None:
        return False
    items = data.get("items") or []
    return bool(items)
