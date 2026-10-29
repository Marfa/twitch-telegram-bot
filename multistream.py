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
PLATFORMS = ("goodgame", "vkplay", "youtube", "tiktok")

# Telegram equivalent of Chat Mini App status dots (live #eb0400 / offline #adadb8).
STATUS_ONLINE = "🔴"
STATUS_OFFLINE = "⚪"
STATUS_PLACEHOLDERS = (
    "goodgame_status",
    "vkplay_status",
    "youtube_status",
    "tiktok_status",
)
URL_PLACEHOLDERS = (
    "goodgame_url",
    "vkplay_url",
    "youtube_url",
    "tiktok_url",
)
MULTISTREAM_PLACEHOLDERS = STATUS_PLACEHOLDERS + URL_PLACEHOLDERS
_PLATFORM_STATUS_KEY = {
    "goodgame": "goodgame_status",
    "vkplay": "vkplay_status",
    "youtube": "youtube_status",
    "tiktok": "tiktok_status",
}
_PLATFORM_URL_KEY = {
    "goodgame": "goodgame_url",
    "vkplay": "vkplay_url",
    "youtube": "youtube_url",
    "tiktok": "tiktok_url",
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
_TT_HOSTS = frozenset(
    {
        "tiktok.com",
        "www.tiktok.com",
        "m.tiktok.com",
    }
)
# TikTok api-live status: 4 = offline (same convention as Streamlink / TikTok web).
_TT_STATUS_OFFLINE = 4


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
    if host in _TT_HOSTS:
        return _parse_tiktok(parsed, raw)
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
    """Alias for multistream_placeholders (status + stream URL tokens)."""
    return multistream_placeholders(raw, template)


def multistream_placeholders(
    raw: str | None,
    template: str | None = None,
) -> dict[str, str]:
    """Map status (🔴/⚪/—) and current-stream URL placeholders for the alert template.

    Unconfigured platforms stay «—». Only platforms mentioned in the template
    (or all if template is omitted) are resolved / live-checked.
    """
    text = template or ""
    needed_status = [
        key
        for key in STATUS_PLACEHOLDERS
        if not text or f"{{{key}}}" in text
    ]
    needed_url = [
        key for key in URL_PLACEHOLDERS if not text or f"{{{key}}}" in text
    ]
    out = {key: "—" for key in MULTISTREAM_PLACEHOLDERS}
    if not needed_status and not needed_url:
        return out
    by_platform = {c.platform: c for c in parse_multistream_channels(raw)}
    for platform in PLATFORMS:
        status_key = _PLATFORM_STATUS_KEY[platform]
        url_key = _PLATFORM_URL_KEY[platform]
        want_status = status_key in needed_status
        want_url = url_key in needed_url
        if not want_status and not want_url:
            continue
        ch = by_platform.get(platform)
        if ch is None:
            continue
        # URL-only for GG/VK/TikTok: channel live page is the stream link — no probe.
        if want_url and not want_status and platform != "youtube":
            out[url_key] = _canonical_stream_url(ch)
            continue
        try:
            online, stream_url = _live_state(ch)
        except Exception:
            logger.exception(
                "Multistream placeholder failed platform=%s id=%s",
                ch.platform,
                ch.channel_id,
            )
            online, stream_url = False, _canonical_stream_url(ch)
        if want_status:
            out[status_key] = STATUS_ONLINE if online else STATUS_OFFLINE
        if want_url:
            out[url_key] = stream_url or _canonical_stream_url(ch)
    return out


def _canonical_stream_url(ch: MultistreamChannel) -> str:
    if ch.platform == "tiktok":
        return f"https://www.tiktok.com/@{ch.channel_id}/live"
    if ch.platform == "youtube":
        return f"https://www.youtube.com/channel/{ch.channel_id}/live"
    return (ch.url or "").strip()


def _live_state(ch: MultistreamChannel) -> tuple[bool, str]:
    """Return (online, current_stream_url). URL falls back to canonical live page."""
    canon = _canonical_stream_url(ch)
    if ch.platform == "goodgame":
        return _goodgame_online(ch.channel_id), canon
    if ch.platform == "vkplay":
        return _vkplay_online(ch.channel_id), canon
    if ch.platform == "youtube":
        video_id = _youtube_live_video_id(ch.channel_id)
        if video_id:
            return True, f"https://www.youtube.com/watch?v={video_id}"
        return False, canon
    if ch.platform == "tiktok":
        online = _tiktok_online(ch.channel_id)
        return online, canon
    return False, canon


def _is_online(ch: MultistreamChannel) -> bool:
    try:
        online, _ = _live_state(ch)
        return online
    except Exception:
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


def _parse_tiktok(parsed, raw: str) -> MultistreamChannel | None:
    parts = [unquote(p) for p in (parsed.path or "").split("/") if p]
    if not parts:
        return None
    handle = parts[0]
    if not handle.startswith("@") or len(handle) < 2:
        return None
    unique = handle.lstrip("@").strip()
    if not re.fullmatch(r"[\w.\-]{2,64}", unique, re.UNICODE):
        return None
    # Skip reserved path segments mistaken for handles.
    if unique.lower() in ("live", "video", "music", "tag", "explore", "foryou"):
        return None
    canon = f"https://www.tiktok.com/@{unique}/live"
    return MultistreamChannel(
        platform="tiktok",
        url=canon,
        channel_id=unique,
        label=f"@{unique}",
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


def _youtube_live_video_id(channel_id: str) -> str | None:
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
        return None
    items = data.get("items") or []
    if not items or not isinstance(items[0], dict):
        return None
    vid = str((items[0].get("id") or {}).get("videoId") or "").strip()
    return vid or None


def _youtube_online(channel_id: str) -> bool:
    return bool(_youtube_live_video_id(channel_id))


def _tiktok_online(unique_id: str) -> bool:
    """True if the TikTok account is currently live.

    Gray area: TikTok has no public Live Status API. We call the same
    ``/api-live/user/room/`` JSON endpoint the TikTok web live page (and
    Streamlink) uses. Upgrade path: official TikTok Live API if shipped.
    """
    uid = (unique_id or "").strip().lstrip("@")
    if not uid:
        return False
    try:
        resp = _SESSION.get(
            "https://www.tiktok.com/api-live/user/room/",
            params={"aid": "1988", "sourceType": "54", "uniqueId": uid},
            headers={"Referer": f"https://www.tiktok.com/@{uid}/live"},
            timeout=12,
        )
        if resp.status_code != 200:
            logger.warning("TikTok status HTTP %s for %s", resp.status_code, uid)
            return False
        payload = resp.json()
        if not isinstance(payload, dict):
            return False
        data = payload.get("data")
        if not isinstance(data, dict):
            return False
        live_room = data.get("liveRoom")
        status = None
        if isinstance(live_room, dict) and "status" in live_room:
            status = live_room.get("status")
        elif "status" in (data.get("user") or {}):
            status = (data.get("user") or {}).get("status")
        if status is None:
            return False
        try:
            return int(status) != _TT_STATUS_OFFLINE
        except (TypeError, ValueError):
            return False
    except Exception:
        logger.exception("TikTok status failed for %s", uid)
        return False
