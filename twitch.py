from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone
from difflib import get_close_matches
from typing import Any
from urllib.parse import urlencode, urlparse

import requests

from config import (
    TWITCH_CLIENT_ID,
    TWITCH_CLIENT_SECRET,
)

FOLLOWS_SCOPE = "user:read:follows"
# Channel followers list (own channel / mod) — Follow/Unfollow monitor.
FOLLOWERS_SCOPE = "moderator:read:followers"
SCHEDULE_SCOPE = "channel:manage:schedule"
SUBSCRIPTIONS_SCOPE = "user:read:subscriptions"
WHISPERS_SCOPE = "user:read:whispers"
CHAT_READ_SCOPE = "user:read:chat"
CHAT_WRITE_SCOPE = "user:write:chat"
CHAT_OAUTH_SCOPES = f"{CHAT_READ_SCOPE} {CHAT_WRITE_SCOPE}"
# Schedule publish may overwrite twitch_sync used by follow import — keep both.
SCHEDULE_OAUTH_SCOPES = f"{SCHEDULE_SCOPE} {FOLLOWS_SCOPE}"
# One authorize → one refresh covers every Twitch feature that stores a user token.
BOT_TWITCH_OAUTH_SCOPES = " ".join(
    [
        FOLLOWS_SCOPE,
        FOLLOWERS_SCOPE,
        SCHEDULE_SCOPE,
        SUBSCRIPTIONS_SCOPE,
        WHISPERS_SCOPE,
        CHAT_READ_SCOPE,
        CHAT_WRITE_SCOPE,
    ]
)

logger = logging.getLogger(__name__)

TWITCH_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.)?twitch\.tv/([a-zA-Z0-9_]{4,25})",
    re.IGNORECASE,
)
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{4,25}$")

# Helix stream tags for Drops-enabled (EN + RU + short "Drops"), compared casefold.
_DROPS_ENABLED_TAG_NEEDLES = ("drops enabled", "drops включены", "drops")
_DROPS_ENABLED_TAG = "Drops Enabled"  # back-compat label
_TWITCHDROPS_APP_BASE = "https://twitchdrops.app"
_TWITCHDROPS_APP_UA = "Mozilla/5.0 (compatible; MarfaTwitchTelegramBot/1.0)"
# ponytail: in-process game_id→slug for catalog links; ceiling = process lifetime.
_twitchdrops_app_slug_by_game_id: dict[str, str] = {}
# IGDB ExternalGameSource / legacy ExternalGameCategory: Twitch = 14
_IGDB_EXTERNAL_TWITCH = 14
_FALLBACK_GAMES = (
    "Elden Ring",
    "Cyberpunk 2077",
    "Counter-Strike 2",
    "Dota 2",
    "League of Legends",
    "Minecraft",
    "GTA V",
    "The Witcher 3",
    "Baldur's Gate 3",
    "Just Chatting",
)


GAME_COVER_IMAGE_ID = "__game_cover__"
STREAM_PREVIEW_IMAGE_ID = "__stream_preview__"
# Legacy id: muted MP4 sent as Telegram Animation (GIF-like autoplay).
STREAM_VIDEO_PREVIEW_IMAGE_ID = "__stream_video_preview__"
# Muted MP4 sent as Telegram Video (client may autoplay if enabled).
STREAM_FILE_VIDEO_PREVIEW_IMAGE_ID = "__stream_file_video_preview__"
BOX_ART_WIDTH = 1920
BOX_ART_HEIGHT = 2560
STREAM_THUMB_WIDTH = 1280
STREAM_THUMB_HEIGHT = 720


def is_game_cover_image(image_file_id: str | None) -> bool:
    return (image_file_id or "") == GAME_COVER_IMAGE_ID


def is_stream_preview_image(image_file_id: str | None) -> bool:
    return (image_file_id or "") == STREAM_PREVIEW_IMAGE_ID


def is_stream_video_preview_image(image_file_id: str | None) -> bool:
    """GIF-like Animation preview (autoplay)."""
    return (image_file_id or "") == STREAM_VIDEO_PREVIEW_IMAGE_ID


def is_stream_file_video_preview_image(image_file_id: str | None) -> bool:
    """Muted Video message preview (not Animation)."""
    return (image_file_id or "") == STREAM_FILE_VIDEO_PREVIEW_IMAGE_ID


def is_stream_capture_preview_image(image_file_id: str | None) -> bool:
    """Either GIF Animation or muted Video — both need streamlink capture."""
    return is_stream_video_preview_image(image_file_id) or is_stream_file_video_preview_image(
        image_file_id
    )


def is_dynamic_alert_image(image_file_id: str | None) -> bool:
    """Sentinel ids resolved at send time (not a Telegram file_id)."""
    return (
        is_game_cover_image(image_file_id)
        or is_stream_preview_image(image_file_id)
        or is_stream_capture_preview_image(image_file_id)
    )


def format_stream_thumbnail_url(
    thumbnail_template: str,
    *,
    width: int = STREAM_THUMB_WIDTH,
    height: int = STREAM_THUMB_HEIGHT,
    cache_bust: bool = False,
) -> str | None:
    thumb = str(thumbnail_template or "").strip()
    if not thumb:
        return None
    url = thumb.replace("{width}", str(width)).replace("{height}", str(height))
    if cache_bust:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}t={int(time.time())}"
    return url


def template_has_game_placeholder(template: str) -> bool:
    text = template or ""
    return (
        "{game}" in text
        or "{game_igdb}" in text
        or "{game_steam}" in text
    )


def format_box_art_url(
    box_art_template: str,
    *,
    width: int = BOX_ART_WIDTH,
    height: int = BOX_ART_HEIGHT,
) -> str:
    return (
        str(box_art_template)
        .replace("{width}", str(width))
        .replace("{height}", str(height))
    )


def _stream_game_fields(payload: dict[str, Any]) -> tuple[str, str]:
    """game_id/game_name from a Helix stream, or category{id,name} from a schedule segment."""
    game_id = str(payload.get("game_id") or "").strip()
    game_name = str(payload.get("game_name") or "").strip()
    if game_id or (game_name and game_name != "—"):
        return game_id, game_name
    cat = payload.get("category")
    if isinstance(cat, dict):
        return (
            str(cat.get("id") or "").strip(),
            str(cat.get("name") or "").strip(),
        )
    return game_id, game_name


def box_art_cdn_url(
    game_id: str,
    *,
    width: int = BOX_ART_WIDTH,
    height: int = BOX_ART_HEIGHT,
    igdb: bool = False,
) -> str:
    gid = str(game_id or "").strip()
    mid = f"{gid}_IGDB" if igdb else gid
    return f"https://static-cdn.jtvnw.net/ttv-boxart/{mid}-{width}x{height}.jpg"


def resolve_sub_image_photo(
    sub,
    stream: dict[str, Any] | None,
    twitch: "TwitchClient | None",
) -> str | None:
    fid = sub.image_file_id
    if not fid:
        return None
    if is_stream_preview_image(fid) or is_stream_capture_preview_image(fid):
        return format_stream_thumbnail_url(
            str((stream or {}).get("thumbnail_url") or ""),
            cache_bust=True,
        )
    if is_game_cover_image(fid):
        if twitch is None:
            return None
        game_id, game_name = _stream_game_fields(stream or {})
        return twitch.resolve_box_art_url(game_id=game_id, game_name=game_name)
    return fid


_RATE_LIMIT_MAX_RETRIES = 4
_RATE_LIMIT_MAX_WAIT_SEC = 60.0


def _retry_after_seconds(resp: requests.Response, attempt: int) -> float:
    """Seconds to sleep after HTTP 429 (Retry-After / Ratelimit-Reset / backoff)."""
    ra = (resp.headers.get("Retry-After") or "").strip()
    if ra:
        try:
            return min(_RATE_LIMIT_MAX_WAIT_SEC, max(0.5, float(ra)))
        except ValueError:
            pass
    reset = (
        resp.headers.get("Ratelimit-Reset")
        or resp.headers.get("RateLimit-Reset")
        or ""
    ).strip()
    if reset:
        try:
            wait = float(reset) - time.time()
            if wait > 0:
                return min(_RATE_LIMIT_MAX_WAIT_SEC, wait + 0.25)
        except ValueError:
            pass
    return min(_RATE_LIMIT_MAX_WAIT_SEC, float(2 ** attempt))


def _install_rate_limit_backoff(
    session: requests.Session,
    on_unauthorized: Any | None = None,
) -> None:
    """Wrap Session.request: refresh a rejected token once on 401, then retry on
    429 with Retry-After / exponential backoff.

    `on_unauthorized` takes the request headers and returns rebuilt headers to
    retry with (after refreshing the token), or None to leave the 401 as-is.
    """
    orig = session.request

    def request(method: str, url: str, **kwargs: Any) -> requests.Response:
        last: requests.Response | None = None
        token_refreshed = False
        for attempt in range(_RATE_LIMIT_MAX_RETRIES + 1):
            last = orig(method, url, **kwargs)
            if (
                last.status_code == 401
                and not token_refreshed
                and on_unauthorized is not None
            ):
                new_headers = on_unauthorized(kwargs.get("headers"))
                if new_headers is not None:
                    kwargs["headers"] = new_headers
                    token_refreshed = True
                    logger.warning(
                        "HTTP 401 method=%s url=%s — refreshed token, retrying",
                        method,
                        url,
                    )
                    continue
            if last.status_code != 429 or attempt >= _RATE_LIMIT_MAX_RETRIES:
                return last
            wait = _retry_after_seconds(last, attempt)
            logger.warning(
                "HTTP 429 method=%s url=%s wait=%.1fs attempt=%s",
                method,
                url,
                wait,
                attempt + 1,
            )
            time.sleep(wait)
        assert last is not None
        return last

    session.request = request  # type: ignore[method-assign]



class TwitchClient:
    def __init__(self) -> None:
        self._session = requests.Session()
        _install_rate_limit_backoff(self._session, self._refresh_app_token_headers)
        self._igdb_db: Any | None = None
        self._token = ""
        self._token_expires = 0.0

    def bind_igdb_db(self, db: Any) -> None:
        """Attach bot DB for local IGDB dump queries."""
        self._igdb_db = db

    def _refresh_app_token_headers(
        self, headers: Any | None
    ) -> dict[str, str] | None:
        """Force a new app token after Twitch rejects the cached one (HTTP 401).

        Returns rebuilt headers to retry with, or None when the request did not
        carry our cached app token (clips user token, token fetch, IGDB, ...).
        """
        if not headers or not self._token:
            return None
        if headers.get("Authorization") != f"Bearer {self._token}":
            return None
        self._token = ""
        self._token_expires = 0.0
        try:
            token = self._ensure_token()
        except Exception:
            logger.exception("Failed to refresh Twitch app token after 401")
            return None
        return {**headers, "Authorization": f"Bearer {token}"}

    def parse_username(self, text: str) -> str | None:
        text = text.strip()
        if not text:
            return None
        match = TWITCH_URL_RE.search(text)
        if match:
            return match.group(1).lower()
        cleaned = text.lstrip("@").lower()
        if USERNAME_RE.match(cleaned):
            return cleaned
        return None

    @staticmethod
    def is_twitch_url(text: str) -> bool:
        return bool(TWITCH_URL_RE.search((text or "").strip()))

    @staticmethod
    def is_standalone_twitch_url(text: str) -> bool:
        """True if the whole message is a channel URL (not embedded in other text)."""
        text = (text or "").strip().rstrip("/")
        return bool(text) and TWITCH_URL_RE.fullmatch(text) is not None

    def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        resp = self._session.post(
            "https://id.twitch.tv/oauth2/token",
            data={
                "client_id": TWITCH_CLIENT_ID,
                "client_secret": TWITCH_CLIENT_SECRET,
                "grant_type": "client_credentials",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires = time.time() + int(data.get("expires_in", 3600))
        return self._token

    def _headers(self) -> dict[str, str]:
        return {
            "Client-ID": TWITCH_CLIENT_ID,
            "Authorization": f"Bearer {self._ensure_token()}",
        }

    def get_user(self, login: str) -> dict[str, Any] | None:
        return self.get_users_by_login([login]).get(login.lower())

    def get_users_by_login(self, logins: list[str]) -> dict[str, dict[str, Any]]:
        """Map lowercase login -> Helix user dict (max 100 logins per request)."""
        clean = []
        seen: set[str] = set()
        for raw in logins:
            login = (raw or "").strip().lower()
            if not login or login in seen:
                continue
            seen.add(login)
            clean.append(login)
        if not clean:
            return {}
        out: dict[str, dict[str, Any]] = {}
        chunk_size = 100
        for i in range(0, len(clean), chunk_size):
            chunk = clean[i : i + chunk_size]
            params: list[tuple[str, str]] = [("login", login) for login in chunk]
            resp = self._session.get(
                "https://api.twitch.tv/helix/users",
                headers=self._headers(),
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
            for user in resp.json().get("data", []):
                login = str(user.get("login") or "").lower()
                if login:
                    out[login] = user
        return out

    @staticmethod
    def matched_drops_tag(stream: dict[str, Any]) -> str | None:
        """Return the stream's Drops tag as stored (EN or RU), if any."""
        tags = stream.get("tags") or []
        if not isinstance(tags, list):
            return None
        needles = set(_DROPS_ENABLED_TAG_NEEDLES)
        for tag in tags:
            raw = str(tag or "").strip()
            if raw and raw.casefold() in needles:
                return raw
        return None

    @staticmethod
    def stream_has_drops_tag(stream: dict[str, Any]) -> bool:
        return TwitchClient.matched_drops_tag(stream) is not None

    def get_streams_with_drops(
        self,
        game_id: str,
        *,
        language: str | None = None,
        first: int = 40,
        limit: int = 5,
        promo_logins: set[str] | frozenset[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Live streams for a game: promo first, then Drops-tagged, then rest.

        Any language/mature. Promo only if already in Helix results (online).
        Untagged streams are included after tagged ones.
        """
        del language  # always any language for Drops alerts
        streams = self.get_streams_by_game(game_id, language=None, first=first)
        promo_set = {
            str(x).strip().casefold() for x in (promo_logins or ()) if str(x).strip()
        }

        def _login(s: dict[str, Any]) -> str:
            return str(s.get("user_login") or "").strip().casefold()

        promo = [s for s in streams if _login(s) in promo_set]
        rest = [s for s in streams if _login(s) not in promo_set]
        promo_tagged = [s for s in promo if self.stream_has_drops_tag(s)]
        promo_untagged = [s for s in promo if not self.stream_has_drops_tag(s)]
        tagged = [s for s in rest if self.stream_has_drops_tag(s)]
        untagged = [s for s in rest if not self.stream_has_drops_tag(s)]
        return (promo_tagged + promo_untagged + tagged + untagged)[: max(0, limit)]

    def fetch_twitchdrops_app_campaigns(
        self, *, sort: str = "new"
    ) -> list[dict[str, Any]]:
        """Public catalog from twitchdrops.app (no OAuth)."""
        return fetch_twitchdrops_app_campaigns(sort=sort, session=self._session)

    @staticmethod
    def twitchdrops_app_slug_for_game_id(game_id: str) -> str | None:
        gid = str(game_id or "").strip()
        if not gid:
            return None
        slug = _twitchdrops_app_slug_by_game_id.get(gid)
        return slug or None

    @staticmethod
    def twitchdrops_app_game_url(game_slug: str) -> str:
        """Public game page URL on twitchdrops.app (no HTML scrape)."""
        return twitchdrops_app_game_url(game_slug)

    def get_live_streams(self, user_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Helix allows at most 100 user_id params per /streams request."""
        if not user_ids:
            return {}
        out: dict[str, dict[str, Any]] = {}
        chunk_size = 100
        for i in range(0, len(user_ids), chunk_size):
            chunk = user_ids[i : i + chunk_size]
            params: list[tuple[str, str]] = [("user_id", uid) for uid in chunk]
            resp = self._session.get(
                "https://api.twitch.tv/helix/streams",
                headers=self._headers(),
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
            for stream in resp.json().get("data", []):
                out[stream["user_id"]] = stream
        return out

    def get_videos_by_user(
        self,
        user_id: str,
        *,
        video_type: str = "archive",
        first: int = 100,
    ) -> list[dict[str, Any]]:
        """Helix VODs/highlights/uploads for a broadcaster. Max first=100."""
        uid = (user_id or "").strip()
        # Category-watch / drops rows use synthetic ids (cw:…, drops:…); Helix 400s on those.
        if not uid.isdigit():
            return []
        resp = self._session.get(
            "https://api.twitch.tv/helix/videos",
            headers=self._headers(),
            params={
                "user_id": uid,
                "type": video_type,
                "first": max(1, min(100, int(first))),
            },
            timeout=15,
        )
        resp.raise_for_status()
        return list(resp.json().get("data") or [])

    def get_videos_by_game(
        self,
        game_id: str,
        *,
        language: str | None = None,
        video_type: str = "archive",
        period: str = "month",
        first: int = 100,
    ) -> list[dict[str, Any]]:
        """Helix VODs for a category. Cap ~500 total when paging; one page here."""
        gid = (game_id or "").strip()
        if not gid:
            return []
        params: dict[str, str | int] = {
            "game_id": gid,
            "type": video_type,
            "period": period,
            "sort": "time",
            "first": max(1, min(100, int(first))),
        }
        if language:
            params["language"] = language.lower()
        resp = self._session.get(
            "https://api.twitch.tv/helix/videos",
            headers=self._headers(),
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        return list(resp.json().get("data") or [])

    def get_streams_by_game(
        self,
        game_id: str,
        *,
        language: str | None = None,
        first: int = 100,
    ) -> list[dict[str, Any]]:
        """Live streams in a category. Helix orders by viewer_count desc."""
        params: dict[str, str | int] = {
            "game_id": game_id,
            "first": max(1, min(100, first)),
        }
        if language:
            params["language"] = language.lower()
        resp = self._session.get(
            "https://api.twitch.tv/helix/streams",
            headers=self._headers(),
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        return list(resp.json().get("data") or [])

    def has_channel_schedule(self, broadcaster_id: str) -> bool:
        """True if the broadcaster has a Twitch stream schedule (404 = none)."""
        resp = self._session.get(
            "https://api.twitch.tv/helix/schedule",
            headers=self._headers(),
            params={"broadcaster_id": broadcaster_id, "first": 1},
            timeout=15,
        )
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return True

    def get_channel_schedule(
        self,
        broadcaster_id: str,
        *,
        first: int = 25,
        start_time: str | None = None,
        stop_before: str | None = None,
    ) -> dict[str, Any]:
        """Schedule payload: upcoming segments (paginated) + vacation window.

        A single Helix page is at most 25 segments. Without pagination, adding a
        slot can push later days out of the first page and look like cancellations.

        When ``stop_before`` (UTC ISO) is set, stop paging once a segment's
        ``start_time`` is at or after that instant (segments are chronological).
        """
        segments: list[dict[str, Any]] = []
        vacation: dict[str, Any] | None = None
        tz = "UTC"
        cursor: str | None = None
        page_size = max(1, min(25, int(first)))
        stop_at: datetime | None = None
        if stop_before:
            try:
                stop_at = self._parse_schedule_time(str(stop_before))
            except ValueError:
                stop_at = None
        for _ in range(40):
            params: dict[str, str | int] = {
                "broadcaster_id": broadcaster_id,
                "first": page_size,
            }
            if start_time:
                params["start_time"] = start_time
            if cursor:
                params["after"] = cursor
            resp = self._session.get(
                "https://api.twitch.tv/helix/schedule",
                headers=self._headers(),
                params=params,
                timeout=15,
            )
            if resp.status_code == 404:
                return {"segments": [], "vacation": None, "broadcaster_timezone": "UTC"}
            resp.raise_for_status()
            payload = resp.json() or {}
            data = payload.get("data") or {}
            if not isinstance(data, dict):
                data = {}
            reached_stop = False
            for seg in data.get("segments") or []:
                if not isinstance(seg, dict):
                    continue
                if stop_at is not None:
                    raw = seg.get("start_time") or ""
                    try:
                        if self._parse_schedule_time(str(raw)) >= stop_at:
                            reached_stop = True
                            break
                    except ValueError:
                        pass
                segments.append(seg)
            if vacation is None:
                vac = data.get("vacation")
                if isinstance(vac, dict):
                    vacation = vac
            page_tz = str(data.get("broadcaster_timezone") or "").strip()
            if page_tz:
                tz = page_tz
            if reached_stop:
                break
            cursor = (payload.get("pagination") or {}).get("cursor") or None
            if not cursor:
                break
        return {
            "segments": segments,
            "vacation": vacation,
            "broadcaster_timezone": tz or "UTC",
        }

    def get_schedule_segments(
        self,
        broadcaster_id: str,
        *,
        first: int = 20,
        start_time: str | None = None,
        stop_before: str | None = None,
    ) -> list[dict[str, Any]]:
        """Upcoming schedule segments; empty if no schedule."""
        return list(
            self.get_channel_schedule(
                broadcaster_id,
                first=first,
                start_time=start_time,
                stop_before=stop_before,
            ).get("segments")
            or []
        )

    @staticmethod
    def vacation_active(
        vacation: dict[str, Any] | None, *, now: datetime | None = None
    ) -> bool:
        """True when Twitch vacation window covers `now` (UTC)."""
        if not vacation:
            return False
        start_raw = vacation.get("start_time")
        end_raw = vacation.get("end_time")
        if not start_raw or not end_raw:
            return False
        try:
            start = TwitchClient._parse_schedule_time(str(start_raw))
            end = TwitchClient._parse_schedule_time(str(end_raw))
        except ValueError:
            return False
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return start <= current.astimezone(timezone.utc) <= end

    def update_schedule_vacation(
        self,
        user_access_token: str,
        broadcaster_id: str,
        *,
        enabled: bool,
        start_time: str | None = None,
        end_time: str | None = None,
        timezone: str | None = None,
    ) -> None:
        """Enable or disable Twitch schedule vacation mode."""
        body: dict[str, Any] = {"is_vacation_enabled": bool(enabled)}
        if enabled:
            if not start_time or not end_time or not timezone:
                raise ValueError("vacation requires start_time, end_time, timezone")
            body["vacation_start_time"] = start_time
            body["vacation_end_time"] = end_time
            body["timezone"] = timezone
        resp = self._session.patch(
            "https://api.twitch.tv/helix/schedule/settings",
            headers={
                "Client-ID": TWITCH_CLIENT_ID,
                "Authorization": f"Bearer {user_access_token}",
                "Content-Type": "application/json",
            },
            params={"broadcaster_id": broadcaster_id},
            json=body,
            timeout=15,
        )
        if not resp.ok:
            detail = resp.text
            try:
                err = resp.json()
                detail = err.get("message") or err.get("error") or detail
            except Exception:
                pass
            raise requests.HTTPError(
                f"{resp.status_code} Client Error: {detail} for url: {resp.url}",
                response=resp,
            )

    def delete_schedule_segment(
        self,
        user_access_token: str,
        broadcaster_id: str,
        segment_id: str,
    ) -> None:
        """Delete a schedule segment (entire series if recurring). Ignores 404."""
        resp = self._session.delete(
            "https://api.twitch.tv/helix/schedule/segment",
            headers={
                "Client-ID": TWITCH_CLIENT_ID,
                "Authorization": f"Bearer {user_access_token}",
            },
            params={"broadcaster_id": broadcaster_id, "id": segment_id},
            timeout=15,
        )
        if resp.status_code in (204, 404):
            return
        if not resp.ok:
            detail = resp.text
            try:
                err = resp.json()
                detail = err.get("message") or err.get("error") or detail
            except Exception:
                pass
            raise requests.HTTPError(
                f"{resp.status_code} Client Error: {detail} for url: {resp.url}",
                response=resp,
            )

    @staticmethod
    def _parse_schedule_time(value: str) -> datetime:
        raw = (value or "").strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @classmethod
    def overlapping_schedule_segment_ids(
        cls,
        segments: list[dict[str, Any]],
        *,
        start_time: str,
        duration: int,
    ) -> list[str]:
        """Segment ids whose time window overlaps [start, start+duration).

        Recurring series also match by weekday + time-of-day (Twitch overlap rule).
        """
        start = cls._parse_schedule_time(start_time)
        end = start + timedelta(minutes=max(1, int(duration)))
        new_tod0 = start.hour * 60 + start.minute
        new_tod1 = new_tod0 + max(1, int(duration))
        out: list[str] = []
        seen: set[str] = set()
        for seg in segments:
            sid = str(seg.get("id") or "")
            if not sid or sid in seen:
                continue
            ss = seg.get("start_time")
            if not ss:
                continue
            s0 = cls._parse_schedule_time(str(ss))
            ee = seg.get("end_time")
            e0 = (
                cls._parse_schedule_time(str(ee))
                if ee
                else s0 + timedelta(minutes=max(1, int(duration)))
            )
            absolute = start < e0 and s0 < end
            recurring_tod = False
            if seg.get("is_recurring") and s0.weekday() == start.weekday():
                tod0 = s0.hour * 60 + s0.minute
                tod1 = tod0 + max(1, int((e0 - s0).total_seconds() // 60) or duration)
                recurring_tod = new_tod0 < tod1 and tod0 < new_tod1
            if absolute or recurring_tod:
                seen.add(sid)
                out.append(sid)
        return out

    @classmethod
    def plan_overlap_resolutions(
        cls,
        segments: list[dict[str, Any]],
        *,
        start_time: str,
        duration: int,
    ) -> list[tuple[str, str, int | None]]:
        """How to clear room for [start, start+duration) without erasing later slots.

        Returns ``(segment_id, action, new_duration_or_None)``:
        - ``shorten`` — earlier neighbor ends at the new start
        - ``delete`` — same start time only (cannot coexist)

        Later neighbors are never deleted; callers must cap the new duration via
        ``cap_duration_before_later_segments``.
        """
        start = cls._parse_schedule_time(start_time)
        overlap_ids = set(
            cls.overlapping_schedule_segment_ids(
                segments, start_time=start_time, duration=duration
            )
        )
        out: list[tuple[str, str, int | None]] = []
        for seg in segments:
            sid = str(seg.get("id") or "")
            if not sid or sid not in overlap_ids:
                continue
            ss = seg.get("start_time")
            if not ss:
                continue
            s0 = cls._parse_schedule_time(str(ss))
            if s0 < start:
                minutes = max(1, int((start - s0).total_seconds() // 60))
                ee = seg.get("end_time")
                if ee:
                    try:
                        e0 = cls._parse_schedule_time(str(ee))
                        current = max(1, int((e0 - s0).total_seconds() // 60))
                        if minutes >= current:
                            continue
                    except ValueError:
                        pass
                out.append((sid, "shorten", minutes))
            elif s0 == start:
                out.append((sid, "delete", None))
        return out

    @classmethod
    def cap_duration_before_later_segments(
        cls,
        segments: list[dict[str, Any]],
        *,
        start_time: str,
        duration: int,
        exclude_ids: tuple[str, ...] | list[str] = (),
    ) -> int:
        """Cap duration so [start, start+dur) ends at the next later segment start."""
        start = cls._parse_schedule_time(start_time)
        chosen = max(1, int(duration))
        skipped = {str(x) for x in exclude_ids if x}
        for seg in segments:
            sid = str(seg.get("id") or "")
            if sid and sid in skipped:
                continue
            ss = seg.get("start_time")
            if not ss:
                continue
            try:
                s0 = cls._parse_schedule_time(str(ss))
            except ValueError:
                continue
            if s0 <= start:
                continue
            gap = int((s0 - start).total_seconds() // 60)
            if 0 < gap < chosen:
                chosen = gap
        return chosen

    def delete_overlapping_schedule_segments(
        self,
        user_access_token: str,
        broadcaster_id: str,
        *,
        start_time: str,
        duration: int = 120,
        exclude_ids: tuple[str, ...] | list[str] = (),
    ) -> int:
        """Resolve overlap for a create/update: shorten earlier, never erase later.

        Shortens earlier overlapping neighbors, deletes same-start only, and returns
        the duration to retry with (capped so the new window ends at the next later
        segment start). Later overlapping slots are kept.
        """
        day_start = self._parse_schedule_time(start_time).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        stop_before = (day_start + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        existing = self.get_schedule_segments(
            broadcaster_id,
            first=25,
            start_time=day_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            stop_before=stop_before,
        )
        skipped = {str(x) for x in exclude_ids if x}
        for sid, action, new_duration in self.plan_overlap_resolutions(
            existing, start_time=start_time, duration=duration
        ):
            if sid in skipped:
                continue
            try:
                if action == "shorten" and new_duration is not None:
                    self.update_schedule_segment(
                        user_access_token,
                        broadcaster_id,
                        sid,
                        duration=new_duration,
                    )
                else:
                    self.delete_schedule_segment(
                        user_access_token, broadcaster_id, sid
                    )
            except Exception as exc:
                logger.warning(
                    "Failed to %s overlapping schedule segment %s: %s",
                    action,
                    sid,
                    exc,
                )
        return self.cap_duration_before_later_segments(
            existing,
            start_time=start_time,
            duration=duration,
            exclude_ids=exclude_ids,
        )

    def clear_channel_schedule(
        self,
        user_access_token: str,
        broadcaster_id: str,
        *,
        max_rounds: int = 50,
    ) -> int:
        """Delete all upcoming schedule segments. Returns successful delete calls.

        Refetches after each round because deleting a recurring segment removes the
        whole series (subsequent occurrence ids 404).
        """
        deleted = 0
        for _ in range(max(1, max_rounds)):
            segments = self.get_schedule_segments(broadcaster_id, first=25)
            if not segments:
                break
            ids: list[str] = []
            seen: set[str] = set()
            for seg in segments:
                sid = str(seg.get("id") or "")
                if not sid or sid in seen:
                    continue
                seen.add(sid)
                ids.append(sid)
            if not ids:
                break
            progress = False
            for sid in ids:
                try:
                    self.delete_schedule_segment(
                        user_access_token, broadcaster_id, sid
                    )
                    deleted += 1
                    progress = True
                except Exception as exc:
                    logger.warning(
                        "Failed to delete schedule segment %s: %s", sid, exc
                    )
            if not progress:
                break
        return deleted

    def clear_schedule_for_day(
        self,
        user_access_token: str,
        broadcaster_id: str,
        *,
        start_time: str,
        duration: int = 1440,
        max_rounds: int = 50,
    ) -> int:
        """Delete existing schedule segments overlapping a single local day.

        `start_time` must be the day start (00:00) in schedule time converted to UTC
        in Helix ISO format (e.g. `YYYY-MM-DDT00:00:00Z`).
        """
        deleted = 0
        day_start = self._parse_schedule_time(start_time).strftime("%Y-%m-%dT%H:%M:%SZ")
        for _ in range(max(1, max_rounds)):
            segments = self.get_schedule_segments(
                broadcaster_id, first=25, start_time=day_start
            )
            if not segments:
                break
            ids = self.overlapping_schedule_segment_ids(
                segments, start_time=day_start, duration=duration
            )
            if not ids:
                break
            progress = False
            for sid in ids:
                try:
                    self.delete_schedule_segment(user_access_token, broadcaster_id, sid)
                    deleted += 1
                    progress = True
                except Exception as exc:
                    logger.warning(
                        "Failed to delete schedule segment %s (day clear): %s", sid, exc
                    )
            if not progress:
                break
        return deleted

    def validate_user_token(self, user_access_token: str) -> dict[str, Any]:
        """Validate a user access token; returns Twitch payload (scopes, user_id, …)."""
        resp = self._session.get(
            "https://id.twitch.tv/oauth2/validate",
            headers={"Authorization": f"OAuth {user_access_token}"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def token_has_scope(self, user_access_token: str, scope: str) -> bool:
        try:
            info = self.validate_user_token(user_access_token)
        except Exception:
            return False
        scopes = info.get("scopes") or []
        return scope in scopes

    def build_authorize_url(
        self,
        *,
        redirect_uri: str,
        state: str,
        scopes: str | None = None,
        force_verify: bool = False,
    ) -> str:
        params: dict[str, str] = {
            "client_id": TWITCH_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": BOT_TWITCH_OAUTH_SCOPES if scopes is None else scopes,
            "state": state,
        }
        if force_verify:
            # Re-consent so newly requested scopes (e.g. followers) are granted.
            params["force_verify"] = "true"
        return "https://id.twitch.tv/oauth2/authorize?" + urlencode(params)

    def exchange_code(self, code: str, *, redirect_uri: str) -> dict[str, Any]:
        resp = self._session.post(
            "https://id.twitch.tv/oauth2/token",
            data={
                "client_id": TWITCH_CLIENT_ID,
                "client_secret": TWITCH_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def refresh_user_token(self, refresh_token: str) -> dict[str, Any]:
        resp = self._session.post(
            "https://id.twitch.tv/oauth2/token",
            data={
                "client_id": TWITCH_CLIENT_ID,
                "client_secret": TWITCH_CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def get_token_user(self, user_access_token: str) -> dict[str, Any] | None:
        resp = self._session.get(
            "https://api.twitch.tv/helix/users",
            headers={
                "Client-ID": TWITCH_CLIENT_ID,
                "Authorization": f"Bearer {user_access_token}",
            },
            timeout=15,
        )
        resp.raise_for_status()
        users = resp.json().get("data", [])
        return users[0] if users else None

    def send_chat_message(
        self,
        user_access_token: str,
        *,
        broadcaster_id: str,
        sender_id: str,
        message: str,
    ) -> dict[str, Any]:
        """Send a chat message as the authorized user (Helix)."""
        resp = self._session.post(
            "https://api.twitch.tv/helix/chat/messages",
            headers={
                "Client-ID": TWITCH_CLIENT_ID,
                "Authorization": f"Bearer {user_access_token}",
                "Content-Type": "application/json",
            },
            json={
                "broadcaster_id": str(broadcaster_id),
                "sender_id": str(sender_id),
                "message": message,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get("data") or []
        return data[0] if data else {}

    def create_whisper_eventsub(
        self,
        *,
        user_id: str,
        callback: str,
        secret: str,
    ) -> str:
        """Subscribe to user.whisper.message; returns EventSub id.

        Webhook transport must use an app access token. The user in
        condition.user_id must already have authorized user:read:whispers.
        """
        body = {
            "type": "user.whisper.message",
            "version": "1",
            "condition": {"user_id": str(user_id)},
            "transport": {
                "method": "webhook",
                "callback": callback,
                "secret": secret,
            },
        }
        headers = {**self._headers(), "Content-Type": "application/json"}
        resp = self._session.post(
            "https://api.twitch.tv/helix/eventsub/subscriptions",
            headers=headers,
            json=body,
            timeout=20,
        )
        if resp.status_code == 409:
            existing = self.find_whisper_eventsub_id(str(user_id))
            if existing:
                return existing
            try:
                eid = str((resp.json() or {}).get("id") or "")
            except Exception:
                eid = ""
            if eid:
                return eid
        if not resp.ok:
            logger.warning(
                "Create whisper EventSub failed: %s %s",
                resp.status_code,
                (resp.text or "")[:300],
            )
        resp.raise_for_status()
        rows = resp.json().get("data") or []
        if not rows:
            raise RuntimeError("eventsub_empty")
        return str(rows[0]["id"])

    def find_whisper_eventsub_id(self, twitch_user_id: str) -> str:
        cursor: str | None = None
        target = str(twitch_user_id)
        while True:
            params: dict[str, str] = {
                "type": "user.whisper.message",
                "first": "100",
            }
            if cursor:
                params["after"] = cursor
            resp = self._session.get(
                "https://api.twitch.tv/helix/eventsub/subscriptions",
                headers=self._headers(),
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
            payload = resp.json()
            for row in payload.get("data") or []:
                if not isinstance(row, dict):
                    continue
                cond = row.get("condition") or {}
                if not isinstance(cond, dict):
                    continue
                if str(cond.get("user_id") or "") != target:
                    continue
                status = str(row.get("status") or "")
                if status in (
                    "enabled",
                    "webhook_callback_verification_pending",
                ):
                    return str(row.get("id") or "")
            cursor = (payload.get("pagination") or {}).get("cursor") or None
            if not cursor:
                return ""

    def delete_eventsub_subscription(self, subscription_id: str) -> None:
        if not subscription_id:
            return
        resp = self._session.delete(
            "https://api.twitch.tv/helix/eventsub/subscriptions",
            headers=self._headers(),
            params={"id": subscription_id},
            timeout=15,
        )
        if resp.status_code in (404, 204):
            return
        resp.raise_for_status()

    def check_user_subscription(
        self,
        user_access_token: str,
        *,
        broadcaster_id: str,
        user_id: str,
    ) -> bool:
        """True if user_id has an active paid Twitch sub to broadcaster_id."""
        resp = self._session.get(
            "https://api.twitch.tv/helix/subscriptions/user",
            headers={
                "Client-ID": TWITCH_CLIENT_ID,
                "Authorization": f"Bearer {user_access_token}",
            },
            params={"broadcaster_id": broadcaster_id, "user_id": user_id},
            timeout=15,
        )
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return bool(resp.json().get("data"))

    def get_followed_channels(
        self, user_access_token: str, user_id: str
    ) -> list[dict[str, Any]]:
        headers = {
            "Client-ID": TWITCH_CLIENT_ID,
            "Authorization": f"Bearer {user_access_token}",
        }
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, str | int] = {"user_id": user_id, "first": 100}
            if cursor:
                params["after"] = cursor
            resp = self._session.get(
                "https://api.twitch.tv/helix/channels/followed",
                headers=headers,
                params=params,
                timeout=20,
            )
            resp.raise_for_status()
            payload = resp.json()
            out.extend(payload.get("data") or [])
            cursor = (payload.get("pagination") or {}).get("cursor")
            if not cursor:
                break
        return out

    def get_channel_followers(
        self, user_access_token: str, broadcaster_id: str
    ) -> list[dict[str, Any]]:
        """Followers of a channel. Needs moderator:read:followers (broadcaster OK)."""
        headers = {
            "Client-ID": TWITCH_CLIENT_ID,
            "Authorization": f"Bearer {user_access_token}",
        }
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, str | int] = {
                "broadcaster_id": broadcaster_id,
                "first": 100,
            }
            if cursor:
                params["after"] = cursor
            resp = self._session.get(
                "https://api.twitch.tv/helix/channels/followers",
                headers=headers,
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            payload = resp.json()
            out.extend(payload.get("data") or [])
            cursor = (payload.get("pagination") or {}).get("cursor")
            if not cursor:
                break
        return out

    def search_categories(self, query: str, *, first: int = 1) -> list[dict[str, Any]]:
        """Search Twitch categories/games by name. Returns list of {id, name, ...}."""
        from search_normalize import normalize_search_query

        q = normalize_search_query(query) or (query or "").strip()
        if not q:
            return []
        resp = self._session.get(
            "https://api.twitch.tv/helix/search/categories",
            headers=self._headers(),
            params={"query": q, "first": max(1, min(20, first))},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("data") or []

    def get_games(self, game_ids: list[str]) -> list[dict[str, Any]]:
        ids = [str(i).strip() for i in game_ids if str(i).strip()]
        if not ids:
            return []
        resp = self._session.get(
            "https://api.twitch.tv/helix/games",
            headers=self._headers(),
            params=[("id", i) for i in ids[:100]],
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("data") or []

    def resolve_box_art_url(
        self,
        *,
        game_id: str = "",
        game_name: str = "",
        width: int = BOX_ART_WIDTH,
        height: int = BOX_ART_HEIGHT,
    ) -> str | None:
        from igdb_dumps import igdb_image_url

        gid = str(game_id or "").strip()
        # Prefer IGDB cover/artwork from local dumps when available.
        if gid and self._igdb_db is not None:
            try:
                image_id = self._igdb_db.igdb_cover_image_id_for_twitch(gid)
            except Exception:
                logger.exception("IGDB local cover lookup failed for %s", gid)
                image_id = None
            if image_id:
                return igdb_image_url(image_id)
        if gid:
            try:
                rows = self.get_games([gid])
                if rows:
                    tpl = str(rows[0].get("box_art_url") or "").strip()
                    if tpl:
                        return format_box_art_url(tpl, width=width, height=height)
            except Exception:
                logger.exception("Helix games lookup failed for %s", gid)
            # Stream/segment already has game_id — don't drop the cover if Helix blips.
            return box_art_cdn_url(gid, width=width, height=height)
        name = str(game_name or "").strip()
        if not name or name == "—":
            return None
        try:
            found = self.search_categories(name, first=10)
        except Exception:
            logger.exception("Twitch category search failed for %s", name)
            return None
        if not found:
            return None
        want = name.casefold()
        exact = [
            c
            for c in found
            if str(c.get("name") or "").strip().casefold() == want
        ]
        pick = exact[0] if exact else found[0]
        cid = str(pick.get("id") or "").strip()
        if cid and self._igdb_db is not None:
            try:
                image_id = self._igdb_db.igdb_cover_image_id_for_twitch(cid)
            except Exception:
                logger.exception("IGDB local cover lookup failed for name=%s", name)
                image_id = None
            if image_id:
                return igdb_image_url(image_id)
        tpl = str(pick.get("box_art_url") or "").strip()
        if tpl:
            return format_box_art_url(tpl, width=width, height=height)
        if cid:
            return box_art_cdn_url(cid, width=width, height=height)
        return None

    @staticmethod
    def _schedule_error_detail(exc: BaseException) -> str:
        resp = getattr(exc, "response", None)
        detail = ""
        if resp is not None:
            try:
                detail = (resp.json() or {}).get("message") or ""
            except Exception:
                detail = resp.text or ""
        return (detail or str(exc)).lower()

    @classmethod
    def is_one_off_schedule_forbidden(cls, exc: BaseException) -> bool:
        """True when Twitch rejects non-recurring segments (non Partner/Affiliate)."""
        return "single segment creation not authorized" in cls._schedule_error_detail(exc)

    @classmethod
    def is_overlapping_schedule(cls, exc: BaseException) -> bool:
        """True when Twitch rejects a segment that overlaps an existing one."""
        return "overlapping segment" in cls._schedule_error_detail(exc)

    @classmethod
    def is_recurring_start_forbidden(cls, exc: BaseException) -> bool:
        """True when Twitch rejects start_time on a recurring segment."""
        detail = cls._schedule_error_detail(exc)
        return "firstoccurrencedate" in detail or (
            "recurring" in detail and "start" in detail
        )

    def create_schedule_segment(
        self,
        user_access_token: str,
        broadcaster_id: str,
        *,
        start_time: str,
        timezone: str,
        duration: int = 120,
        title: str = "",
        category_id: str = "",
        is_recurring: bool = False,
    ) -> dict[str, Any]:
        """Create a schedule segment. Raises on error."""
        # Twitch requires duration as a string and timezone in IANA form.
        body: dict[str, Any] = {
            "start_time": start_time,
            "timezone": timezone,
            "duration": str(duration),
            "is_recurring": is_recurring,
        }
        if title:
            body["title"] = title[:140]
        if category_id:
            body["category_id"] = category_id
        resp = self._session.post(
            "https://api.twitch.tv/helix/schedule/segment",
            headers={
                "Client-ID": TWITCH_CLIENT_ID,
                "Authorization": f"Bearer {user_access_token}",
                "Content-Type": "application/json",
            },
            params={"broadcaster_id": broadcaster_id},
            json=body,
            timeout=15,
        )
        if not resp.ok:
            detail = resp.text
            try:
                err = resp.json()
                detail = err.get("message") or err.get("error") or detail
            except Exception:
                pass
            raise requests.HTTPError(
                f"{resp.status_code} Client Error: {detail} for url: {resp.url}",
                response=resp,
            )
        return resp.json()

    def update_schedule_segment(
        self,
        user_access_token: str,
        broadcaster_id: str,
        segment_id: str,
        *,
        start_time: str = "",
        timezone: str = "",
        duration: int | None = None,
        title: str | None = None,
        category_id: str | None = None,
    ) -> dict[str, Any]:
        """Update a schedule segment. Raises on error."""
        body: dict[str, Any] = {}
        if start_time:
            body["start_time"] = start_time
        if timezone:
            body["timezone"] = timezone
        if duration is not None:
            body["duration"] = str(max(1, int(duration)))
        if title is not None:
            body["title"] = title[:140]
        if category_id:
            body["category_id"] = category_id
        resp = self._session.patch(
            "https://api.twitch.tv/helix/schedule/segment",
            headers={
                "Client-ID": TWITCH_CLIENT_ID,
                "Authorization": f"Bearer {user_access_token}",
                "Content-Type": "application/json",
            },
            params={"broadcaster_id": broadcaster_id, "id": segment_id},
            json=body,
            timeout=15,
        )
        if not resp.ok:
            detail = resp.text
            try:
                err = resp.json()
                detail = err.get("message") or err.get("error") or detail
            except Exception:
                pass
            raise requests.HTTPError(
                f"{resp.status_code} Client Error: {detail} for url: {resp.url}",
                response=resp,
            )
        return resp.json()

    def update_schedule_segment_with_overlap_replace(
        self,
        user_access_token: str,
        broadcaster_id: str,
        segment_id: str,
        *,
        start_time: str,
        timezone: str,
        duration: int = 120,
        title: str = "",
        category_id: str = "",
    ) -> tuple[dict[str, Any], bool]:
        """Update a segment; on overlap or recurring-time restriction, replace.

        Recurring segments cannot get a new start_time (Twitch 400 FirstOccurrenceDate).
        In that case the old segment is deleted and a new one is created.
        Returns (response_json, used_recurring_create).
        """
        kwargs = dict(
            user_access_token=user_access_token,
            broadcaster_id=broadcaster_id,
            segment_id=segment_id,
            start_time=start_time,
            timezone=timezone,
            duration=duration,
            title=title,
            category_id=category_id,
        )

        def _update() -> dict[str, Any]:
            return self.update_schedule_segment(**kwargs)

        def _recreate() -> tuple[dict[str, Any], bool]:
            self.delete_schedule_segment(
                user_access_token, broadcaster_id, segment_id
            )
            return self.create_schedule_segment_with_fallback(
                user_access_token,
                broadcaster_id,
                start_time=start_time,
                timezone=timezone,
                duration=int(kwargs.get("duration") or duration),
                title=title,
                category_id=category_id,
            )

        try:
            return _update(), False
        except Exception as exc:
            if self.is_overlapping_schedule(exc):
                capped = self.delete_overlapping_schedule_segments(
                    user_access_token,
                    broadcaster_id,
                    start_time=start_time,
                    duration=duration,
                    exclude_ids=(segment_id,),
                )
                kwargs["duration"] = capped
                try:
                    return _update(), False
                except Exception as retry_exc:
                    if self.is_recurring_start_forbidden(retry_exc):
                        return _recreate()
                    raise
            if self.is_recurring_start_forbidden(exc):
                return _recreate()
            raise

    def create_schedule_segment_with_fallback(
        self,
        user_access_token: str,
        broadcaster_id: str,
        *,
        start_time: str,
        timezone: str,
        duration: int = 120,
        title: str = "",
        category_id: str = "",
        prefer_recurring: bool = False,
        replace_overlap: bool = True,
    ) -> tuple[dict[str, Any], bool]:
        """Create one-off segment; on Partner/Affiliate restriction retry as recurring.

        Returns (response_json, used_recurring). If prefer_recurring is True, skips
        the one-off attempt (sticky after first fallback in a batch).
        On overlap, shortens earlier neighbors / caps this duration and retries once
        when replace_overlap (later neighbors are kept).
        """
        kwargs = dict(
            user_access_token=user_access_token,
            broadcaster_id=broadcaster_id,
            start_time=start_time,
            timezone=timezone,
            duration=duration,
            title=title,
            category_id=category_id,
        )

        def _create(recurring: bool) -> dict[str, Any]:
            try:
                return self.create_schedule_segment(**kwargs, is_recurring=recurring)
            except Exception as exc:
                if not replace_overlap or not self.is_overlapping_schedule(exc):
                    raise
                capped = self.delete_overlapping_schedule_segments(
                    user_access_token,
                    broadcaster_id,
                    start_time=start_time,
                    duration=int(kwargs["duration"]),
                )
                kwargs["duration"] = capped
                return self.create_schedule_segment(**kwargs, is_recurring=recurring)

        if prefer_recurring:
            return _create(True), True
        try:
            return _create(False), False
        except Exception as exc:
            if not self.is_one_off_schedule_forbidden(exc):
                raise
            return _create(True), True

    def _igdb_headers(self) -> dict[str, str]:
        return {
            "Client-ID": TWITCH_CLIENT_ID,
            "Authorization": f"Bearer {self._ensure_token()}",
            "Accept": "application/json",
        }

    def random_igdb_game_name(self) -> str:
        """Pick a random main-game title from local IGDB dumps."""
        try:
            rows = self.igdb_random_games(1)
            name = str((rows[0] or {}).get("name") or "").strip() if rows else ""
            if name:
                return name
        except Exception as exc:
            logger.warning("IGDB random game unavailable (%s)", exc)
        return random.choice(_FALLBACK_GAMES)

    def igdb_random_games(self, n: int = 5) -> list[dict[str, Any]]:
        """n random main games with Twitch mapping from local dumps."""
        db = self._igdb_db
        if db is None:
            return [{"name": random.choice(_FALLBACK_GAMES)} for _ in range(max(1, n))]
        try:
            rows = db.igdb_random_twitch_games(n)
        except Exception as exc:
            logger.warning("IGDB local random games failed (%s)", exc)
            rows = []
        out = list(rows or [])
        seen = {str(r.get("name") or "").strip().lower() for r in out}
        while len(out) < max(1, n):
            name = random.choice(_FALLBACK_GAMES)
            key = name.lower()
            if key in seen:
                break
            seen.add(key)
            out.append({"name": name})
        return out[: max(1, n)]

    def igdb_recently_released_games(self, n: int = 5) -> list[dict[str, Any]]:
        """Recent IGDB releases from local dumps (Twitch-mapped)."""
        db = self._igdb_db
        if db is None:
            return self.igdb_random_games(n)
        try:
            rows = db.igdb_recent_twitch_games(n, window=max(n * 10, 50))
            if rows:
                return rows
        except Exception as exc:
            logger.warning("IGDB local recently released failed (%s)", exc)
        return self.igdb_random_games(n)

    def igdb_top100_games(self, n: int = 5) -> list[dict[str, Any]]:
        """Sample from IGDB top-rated (local dumps, Twitch-mapped)."""
        db = self._igdb_db
        if db is None:
            return self.igdb_random_games(n)
        try:
            rows = db.igdb_top_twitch_games(n)
            if rows:
                return rows
        except Exception as exc:
            logger.warning("IGDB local top-100 failed (%s)", exc)
        return self.igdb_random_games(n)

    def _igdb_twitch_uid(self, game: dict[str, Any]) -> str:
        """Twitch category id from IGDB game row (local dump shape or nested)."""
        direct = str(game.get("twitch_uid") or "").strip()
        if direct:
            return direct
        eg_list = game.get("external_games") or []
        for eg in eg_list:
            if not isinstance(eg, dict):
                continue
            uid = str(eg.get("uid") or "").strip()
            if not uid:
                continue
            src = eg.get("external_game_source")
            if src is None:
                src = eg.get("category")
            try:
                if int(src or 0) == _IGDB_EXTERNAL_TWITCH:
                    return uid
            except (TypeError, ValueError):
                pass
            if "twitch.tv" in str(eg.get("url") or ""):
                return uid
        # Local dump map by IGDB game id when present.
        db = self._igdb_db
        if db is not None:
            try:
                gid = int(game.get("id"))
            except (TypeError, ValueError):
                gid = 0
            if gid > 0:
                try:
                    # Reverse lookup is rare; use random join path via cover helper tables.
                    # Prefer stored twitch_uid on row — already handled above.
                    pass
                except Exception:
                    pass
        return ""

    def resolve_igdb_games_to_twitch_categories(
        self, games: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        """Map IGDB game rows to Twitch categories {id, name}."""
        out: list[dict[str, str]] = []
        seen: set[str] = set()
        for game in games:
            name = str(game.get("name") or "").strip()
            twitch_id = self._igdb_twitch_uid(game)
            if twitch_id:
                if twitch_id in seen:
                    continue
                seen.add(twitch_id)
                out.append({"id": twitch_id, "name": name or twitch_id})
                continue
            if not name:
                continue
            try:
                found = self.search_categories(name, first=10)
            except Exception:
                logger.exception("Twitch category search failed for %s", name)
                continue
            if not found:
                continue
            want = name.casefold()
            exact = [
                c
                for c in found
                if str(c.get("name") or "").strip().casefold() == want
            ]
            pick = exact[0] if exact else found[0]
            cid = str(pick.get("id") or "").strip()
            cname = str(pick.get("name") or name).strip()
            if not cid or cid in seen:
                continue
            seen.add(cid)
            out.append({"id": cid, "name": cname})
        return out

    def igdb_search_ignore_entities(
        self, query: str, *, limit_per: int = 5
    ) -> list[dict[str, Any]]:
        """Search local companies/genres/game_modes; companies as developer + publisher."""
        db = self._igdb_db
        if db is None:
            return []
        try:
            companies = db.igdb_search_by_name(
                "igdb_companies", query, limit=limit_per
            )
            genres = db.igdb_search_by_name("igdb_genres", query, limit=limit_per)
            modes = db.igdb_search_by_name(
                "igdb_game_modes", query, limit=limit_per
            )
        except Exception as exc:
            logger.warning("IGDB local ignore search failed (%s)", exc)
            return []
        out: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()

        def _add(kind: str, row: dict[str, Any]) -> None:
            try:
                igdb_id = int(row["id"])
            except (KeyError, TypeError, ValueError):
                return
            name = str(row.get("name") or "").strip()
            if not name or igdb_id <= 0:
                return
            key = (kind, igdb_id)
            if key in seen:
                return
            seen.add(key)
            out.append({"kind": kind, "id": igdb_id, "name": name})

        for row in companies:
            _add("developer", row)
            _add("publisher", row)
        for row in genres:
            _add("genre", row)
        for row in modes:
            _add("game_mode", row)
        return out

    def igdb_game_meta_for_twitch_category(
        self, twitch_game_id: str | int | None
    ) -> dict[str, Any] | None:
        """Twitch category id → IGDB genres/modes/companies from local dumps."""
        gid = str(twitch_game_id or "").strip()
        if not gid:
            return None
        now = time.monotonic()
        cached = _igdb_twitch_meta_cache.get(gid)
        if cached is not None:
            ts, meta = cached
            if now - ts < _IGDB_TWITCH_META_TTL_SEC:
                return meta
        meta = None
        db = self._igdb_db
        if db is not None:
            try:
                meta = db.igdb_game_meta_for_twitch(gid)
            except Exception as exc:
                logger.warning("IGDB local Twitch→meta failed for %s (%s)", gid, exc)
                meta = None
        _igdb_twitch_meta_cache[gid] = (now, meta)
        return meta

    def resolve_game_description(
        self, twitch_game_id: str | int | None, *, lang: str = "en"
    ) -> str:
        """Helix category id → IGDB summary, localized to bot UI language."""
        gid = str(twitch_game_id or "").strip()
        if not gid or self._igdb_db is None:
            return "—"
        try:
            summary = self._igdb_db.igdb_summary_for_twitch(gid)
        except Exception as exc:
            logger.warning("IGDB summary lookup failed for %s (%s)", gid, exc)
            return "—"
        if not summary:
            return "—"
        return localize_igdb_summary(summary, lang, db=self._igdb_db)

    def resolve_game_store_links(
        self, twitch_game_id: str | int | None
    ) -> dict[str, str | None]:
        """Helix category id → IGDB slug + Steam app id from local dumps."""
        empty: dict[str, str | None] = {"slug": None, "steam_app_id": None}
        gid = str(twitch_game_id or "").strip()
        if not gid or self._igdb_db is None:
            return empty
        try:
            links = self._igdb_db.igdb_store_links_for_twitch(gid)
        except Exception as exc:
            logger.warning("IGDB store-link lookup failed for %s (%s)", gid, exc)
            return empty
        if not isinstance(links, dict):
            return empty
        return {
            "slug": (str(links.get("slug") or "").strip() or None),
            "steam_app_id": (str(links.get("steam_app_id") or "").strip() or None),
        }



def localize_igdb_summary(
    summary: str, lang: str, db: Any | None = None
) -> str:
    """IGDB summaries are US English; translate for non-en bot locales when DeepL is set."""
    import html as _html

    text = (summary or "").strip()
    if not text:
        return "—"
    from i18n import DEFAULT_LOCALE, SUPPORTED_LOCALES

    locale = lang if lang in SUPPORTED_LOCALES else DEFAULT_LOCALE
    if locale == "en":
        return _html.unescape(text)
    from config import DEEPL_API_KEY

    if not DEEPL_API_KEY:
        return _html.unescape(text)
    cache_key = (text, locale)
    cached = _IGDB_SUMMARY_TR_CACHE.get(cache_key)
    if cached is not None:
        return cached
    source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if db is not None:
        try:
            stored = db.get_igdb_summary_translation(source_hash, locale)
            if stored:
                _igdb_summary_tr_cache_put(cache_key, stored)
                return stored
        except Exception:
            logger.exception(
                "IGDB summary translation DB read failed lang=%s", locale
            )
    try:
        from translate import translate_text

        # Plain prose — never DeepL HTML mode (avoids Baldur&#x27;s in alerts).
        out = (
            translate_text(
                text,
                target_lang=locale,
                source_lang="en",
                preserve_html=False,
            ).strip()
            or text
        )
        out = _html.unescape(out)
    except Exception:
        logger.exception("IGDB summary translate failed lang=%s", locale)
        return _html.unescape(text)
    if db is not None:
        try:
            db.set_igdb_summary_translation(source_hash, locale, out)
        except Exception:
            logger.exception(
                "IGDB summary translation DB write failed lang=%s", locale
            )
    _igdb_summary_tr_cache_put(cache_key, out)
    return out


def _igdb_summary_tr_cache_put(cache_key: tuple[str, str], value: str) -> None:
    if len(_IGDB_SUMMARY_TR_CACHE) >= _IGDB_SUMMARY_TR_CACHE_MAX:
        _IGDB_SUMMARY_TR_CACHE.clear()
    _IGDB_SUMMARY_TR_CACHE[cache_key] = value


def preview_stream_title(locale: str, game: str) -> str:
    """Build a sample stream title from an IGDB/Twitch game name (not 'Test stream')."""
    g = (game or "").strip() or "Just Chatting"
    if str(locale).lower().startswith("ru"):
        return random.choice(
            (
                f"Играю в {g}",
                f"{g} — прохождение",
                f"Стрим по {g}",
                f"{g}: первый взгляд",
                f"Залетаем в {g}",
            )
        )
    return random.choice(
        (
            f"Playing {g}",
            f"{g} playthrough",
            f"Streaming {g}",
            f"First look at {g}",
            f"Chilling with {g}",
        )
    )


_STREAM_NAME_MENTION_RE = re.compile(r"@([A-Za-z0-9_]{1,25})")
_STREAM_NAME_COMMAND_RE = re.compile(r"!\w+", re.UNICODE)


def strip_name_mentions_and_commands(
    title: str,
    twitch: "TwitchClient | None" = None,
) -> str:
    """Remove Twitch !commands and @logins that resolve to real Helix users."""
    if not title:
        return title
    out = _STREAM_NAME_COMMAND_RE.sub("", title)
    mentions = _STREAM_NAME_MENTION_RE.findall(out)
    if not mentions or twitch is None:
        return _tidy_stream_title(out)
    exists: dict[str, bool] = {}
    for login in {m.lower() for m in mentions}:
        try:
            exists[login] = bool(twitch.get_user(login))
        except Exception:
            exists[login] = False

    def _keep_or_drop(match: re.Match[str]) -> str:
        return "" if exists.get(match.group(1).lower()) else match.group(0)

    out = _STREAM_NAME_MENTION_RE.sub(_keep_or_drop, out)
    return _tidy_stream_title(out)


def _tidy_stream_title(text: str) -> str:
    text = re.sub(r"[^\S\n]{2,}", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


# Allowed Telegram HTML tags users may put in alert templates (not full rich blocks).
_TEMPLATE_HTML_RE = re.compile(
    r"</?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|tg-spoiler)\b"
    r"|<a\s+href\s*=",
    re.IGNORECASE,
)


def template_uses_html(template: str) -> bool:
    """True when the template includes Telegram HTML formatting tags."""
    text = template or ""
    if _TEMPLATE_HTML_RE.search(text):
        return True
    # These placeholders expand to <a href="…">…</a>.
    return "{game_igdb}" in text or "{game_steam}" in text


_IGDB_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def igdb_game_page_url(slug: str | None) -> str | None:
    s = (slug or "").strip().lower()
    if not s or not _IGDB_SLUG_RE.fullmatch(s):
        return None
    return f"https://www.igdb.com/games/{s}"


def steam_store_app_url(app_id: str | None) -> str | None:
    aid = (app_id or "").strip()
    if not aid.isdigit():
        return None
    return f"https://store.steampowered.com/app/{aid}"


def _game_name_anchor(label: str, url: str | None, *, label_escaped: bool) -> str:
    """Game name as Telegram HTML link, or plain label when url is missing."""
    import html as _html

    text = label if label_escaped else _html.escape(label or "—")
    if not url:
        return text
    return f'<a href="{_html.escape(url, quote=True)}">{text}</a>'


def twitch_profile_url(login: str) -> str:
    """Channel page URL; strips a leading @. Empty login → empty string."""
    bare = (login or "").strip().lstrip("@")
    return f"https://www.twitch.tv/{bare}" if bare else ""


def twitch_login_link_html(
    login: str,
    *,
    label: str | None = None,
    label_escaped: bool = False,
) -> str:
    """Login without @ as a Telegram HTML link to the Twitch profile."""
    bare = (login or "").strip().lstrip("@")
    if not bare:
        return ""
    return _game_name_anchor(
        label if label is not None else bare,
        twitch_profile_url(bare),
        label_escaped=label_escaped,
    )


def render_template(
    template: str,
    username: str,
    game: str = "",
    name: str = "",
    stream: dict[str, Any] | None = None,
    extra: dict[str, str] | None = None,
    *,
    strip_name_mentions: bool = False,
    twitch: "TwitchClient | None" = None,
    escape_html: bool = False,
    lang: str | None = None,
) -> str:
    """Fill template placeholders from channel + optional Helix stream payload.

    When escape_html=True, placeholder values are HTML-escaped so user HTML tags
    in the template skeleton stay intact under ParseMode.HTML.
    """
    if strip_name_mentions and "{name}" in template:
        name = strip_name_mentions_and_commands(name, twitch)
    values = _template_values(username, game, name, stream)
    provided_extra = set(extra or ())
    if extra:
        values.update(extra)
    text = template or ""
    need_description = (
        "{game_description}" in text and "game_description" not in provided_extra
    )
    need_igdb = "{game_igdb}" in text and "game_igdb" not in provided_extra
    need_steam = "{game_steam}" in text and "game_steam" not in provided_extra
    twitch_game_id = (stream or {}).get("game_id")
    if need_description and twitch is not None:
        values["game_description"] = twitch.resolve_game_description(
            twitch_game_id,
            lang=lang or "en",
        )
    igdb_url: str | None = None
    steam_url: str | None = None
    if (need_igdb or need_steam) and twitch is not None:
        links = twitch.resolve_game_store_links(twitch_game_id)
        if need_igdb:
            igdb_url = igdb_game_page_url(links.get("slug"))
        if need_steam:
            steam_url = steam_store_app_url(links.get("steam_app_id"))
    # Plain fallbacks before HTML wrap (same visible text as {game}).
    if need_igdb:
        values["game_igdb"] = values.get("game") or "—"
    if need_steam:
        values["game_steam"] = values.get("game") or "—"
    if escape_html:
        import html as _html

        values = {k: _html.escape(v) for k, v in values.items()}
    if need_igdb:
        values["game_igdb"] = _game_name_anchor(
            values.get("game_igdb") or "—",
            igdb_url,
            label_escaped=escape_html,
        )
    if need_steam:
        values["game_steam"] = _game_name_anchor(
            values.get("game_steam") or "—",
            steam_url,
            label_escaped=escape_html,
        )
    out = template
    # Longer keys first so {game_id} is not partially eaten by {game}.
    for key in sorted(values, key=len, reverse=True):
        out = out.replace(f"{{{key}}}", values[key])
    return out


def _template_values(
    username: str,
    game: str,
    name: str,
    stream: dict[str, Any] | None,
) -> dict[str, str]:
    values: dict[str, str] = {
        "username": username or "",
        "game": game or "—",
        "name": name or "—",
        "started_at": "—",
        "viewer_count": "—",
        "viewer_avg": "—",
        "viewer_peak": "—",
        "thumbnail_url": "—",
        "tags": "—",
        "language": "—",
        "is_mature": "—",
        "game_id": "—",
        "id": "—",
        "type": "—",
        "minutes": "—",
        "duration": "—",
        "game_description": "—",
        "game_igdb": "—",
        "game_steam": "—",
        "goodgame_status": "—",
        "vkplay_status": "—",
        "youtube_status": "—",
    }
    if not stream:
        return values
    started = stream.get("started_at")
    if started:
        values["started_at"] = str(started)
    if stream.get("viewer_count") is not None:
        values["viewer_count"] = str(stream.get("viewer_count"))
    if stream.get("viewer_avg") is not None:
        values["viewer_avg"] = str(stream.get("viewer_avg"))
    if stream.get("viewer_peak") is not None:
        values["viewer_peak"] = str(stream.get("viewer_peak"))
    thumb = str(stream.get("thumbnail_url") or "")
    if thumb:
        values["thumbnail_url"] = thumb.replace("{width}", "480").replace(
            "{height}", "270"
        )
    tags = stream.get("tags") or []
    if isinstance(tags, list) and tags:
        values["tags"] = ", ".join(str(t) for t in tags if t)
    lang = stream.get("language")
    if lang:
        values["language"] = str(lang)
    if "is_mature" in stream:
        values["is_mature"] = "18+" if stream.get("is_mature") else "—"
    if stream.get("game_id"):
        values["game_id"] = str(stream.get("game_id"))
    if stream.get("id"):
        values["id"] = str(stream.get("id"))
    if stream.get("type"):
        values["type"] = str(stream.get("type"))
    if stream.get("game_name") and not game:
        values["game"] = str(stream.get("game_name"))
    if stream.get("title") and not name:
        values["name"] = str(stream.get("title"))
    if stream.get("user_login") and not username:
        values["username"] = str(stream.get("user_login"))
    return values


_TEMPLATE_PLACEHOLDERS = (
    "username",
    "game",
    "name",
    "started_at",
    "viewer_count",
    "viewer_avg",
    "viewer_peak",
    "thumbnail_url",
    "tags",
    "language",
    "is_mature",
    "game_id",
    "id",
    "type",
    "minutes",
    "duration",
    "game_description",
    "game_igdb",
    "game_steam",
    "goodgame_status",
    "vkplay_status",
    "youtube_status",
)
_STREAM_SNAPSHOT_KEYS = (
    "user_login",
    "user_name",
    "game_id",
    "game_name",
    "title",
    "viewer_count",
    "started_at",
    "thumbnail_url",
    "tags",
    "language",
    "is_mature",
    "id",
    "type",
)
# Common wrong placeholder names → canonical token (without braces).
_PLACEHOLDER_ALIASES: dict[str, str] = {
    "title": "name",
    "streamtitle": "name",
    "stream_title": "name",
    "streamname": "name",
    "stream_name": "name",
    "streamer": "username",
    "channel": "username",
    "login": "username",
    "user": "username",
    "viewers": "viewer_count",
    "viewercount": "viewer_count",
    "viewer": "viewer_count",
    "avgviewers": "viewer_avg",
    "avg_viewers": "viewer_avg",
    "average_viewers": "viewer_avg",
    "viewers_avg": "viewer_avg",
    "vieweravg": "viewer_avg",
    "peakviewers": "viewer_peak",
    "peak_viewers": "viewer_peak",
    "viewers_peak": "viewer_peak",
    "viewerpeak": "viewer_peak",
    "gamename": "game",
    "game_name": "game",
    "category": "game",
    "started": "started_at",
    "start": "started_at",
    "starttime": "started_at",
    "start_time": "started_at",
    "thumb": "thumbnail_url",
    "thumbnail": "thumbnail_url",
    "preview": "thumbnail_url",
    "mature": "is_mature",
    "lang": "language",
    "length": "duration",
    "streamid": "id",
    "stream_id": "id",
    "gamedescription": "game_description",
    "game_desc": "game_description",
    "game_descriotion": "game_description",
    "igdb_summary": "game_description",
    "gameigdb": "game_igdb",
    "igdb_game": "game_igdb",
    "game_igdb_link": "game_igdb",
    "gamesteam": "game_steam",
    "steam_game": "game_steam",
    "game_steam_link": "game_steam",
    "gg_status": "goodgame_status",
    "vk_status": "vkplay_status",
    "yt_status": "youtube_status",
}
# Telegram may linkify these even without a scheme; used to decide link-preview UI.
_TEMPLATE_LINK_RE = re.compile(
    r"(https?://|www\.|t\.me/|telegram\.me/|twitch\.tv/)", re.IGNORECASE
)


def template_has_link(template: str) -> bool:
    """True if the template text is likely to produce a Telegram link preview."""
    return bool(_TEMPLATE_LINK_RE.search(template or ""))
_KNOWN_PLACEHOLDER_TOKENS = frozenset(f"{{{p}}}" for p in _TEMPLATE_PLACEHOLDERS)
# Brace-ish tokens: {game}, {game), (game}, [name}, {User_Name}, …
_PLACEHOLDER_CANDIDATE_RE = re.compile(
    r"[{(\[]\s*([A-Za-z_][A-Za-z0-9_\-]*)\s*[})\]]"
)


def _placeholder_typo_suggestion(inner: str) -> str | None:
    raw = inner.lower()
    compact = raw.replace("-", "").replace("_", "")
    if raw in _PLACEHOLDER_ALIASES:
        return f"{{{_PLACEHOLDER_ALIASES[raw]}}}"
    if compact in _PLACEHOLDER_ALIASES:
        return f"{{{_PLACEHOLDER_ALIASES[compact]}}}"
    if compact in _TEMPLATE_PLACEHOLDERS:
        return f"{{{compact}}}"
    close = get_close_matches(compact, list(_TEMPLATE_PLACEHOLDERS), n=1, cutoff=0.7)
    if close:
        return f"{{{close[0]}}}"
    return None


def find_placeholder_typos(template: str) -> list[tuple[str, str]]:
    """Return [(found_token, suggested_placeholder), ...] for likely typos."""
    results: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in _PLACEHOLDER_CANDIDATE_RE.finditer(template):
        token = match.group(0)
        if "{" not in token and "}" not in token:
            continue
        if token in _KNOWN_PLACEHOLDER_TOKENS:
            continue
        if token in seen:
            continue
        suggested = _placeholder_typo_suggestion(match.group(1))
        if not suggested:
            continue
        seen.add(token)
        results.append((token, suggested))
    return results


def fix_placeholder_typos(template: str) -> str:
    """Replace likely placeholder typos with canonical {key} tokens."""
    typos = find_placeholder_typos(template)
    if not typos:
        return template
    out = template
    for found, suggested in sorted(typos, key=lambda item: len(item[0]), reverse=True):
        out = out.replace(found, suggested)
    return out


def stream_end_snapshot(stream: dict[str, Any]) -> dict[str, Any] | None:
    """Helix stream fields to reuse when the channel goes offline."""
    if not stream:
        return None
    out: dict[str, Any] = {}
    for key in _STREAM_SNAPSHOT_KEYS:
        if key not in stream:
            continue
        val = stream[key]
        if val is None:
            continue
        if key == "tags" and isinstance(val, list):
            tags = [str(tag) for tag in val if tag]
            if tags:
                out[key] = tags
            continue
        out[key] = val
    if not out:
        return None
    gid = str(out.get("game_id") or "").strip()
    gname = str(out.get("game_name") or "").strip()
    if (
        not gid
        and not gname
        and not out.get("title")
        and not out.get("user_login")
    ):
        return None
    return out


def _same_live_stream(prev: dict[str, Any] | None, snap: dict[str, Any]) -> bool:
    if not prev:
        return False
    prev_id, snap_id = prev.get("id"), snap.get("id")
    if prev_id or snap_id:
        return bool(prev_id) and bool(snap_id) and str(prev_id) == str(snap_id)
    prev_start, snap_start = prev.get("started_at"), snap.get("started_at")
    if prev_start and snap_start:
        return str(prev_start) == str(snap_start)
    return False


def accumulate_viewer_stats(
    prev: dict[str, Any] | None,
    snap: dict[str, Any],
) -> dict[str, Any]:
    """Fold Helix viewer_count into running avg/peak on the end-alert snapshot.

    Average is over bot poll samples for this stream id (not Twitch Analytics).
    """
    out = dict(snap)
    raw = out.get("viewer_count")
    if raw is None:
        return out
    try:
        viewers = int(raw)
    except (TypeError, ValueError):
        return out
    if viewers < 0:
        return out
    if _same_live_stream(prev, out):
        total = int(prev.get("_viewer_sum") or 0) + viewers  # type: ignore[union-attr]
        n = int(prev.get("_viewer_n") or 0) + 1  # type: ignore[union-attr]
        peak = max(int(prev.get("viewer_peak") or 0), viewers)  # type: ignore[union-attr]
    else:
        total, n, peak = viewers, 1, viewers
    out["_viewer_sum"] = total
    out["_viewer_n"] = n
    out["viewer_peak"] = peak
    out["viewer_avg"] = int(round(total / n)) if n else viewers
    return out


def stream_duration_minutes(stream: dict[str, Any] | None) -> str:
    """Whole minutes from started_at to ended_at (or now). «—» if unknown."""
    started = (stream or {}).get("started_at")
    if not started:
        return "—"
    try:
        start = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        ended_raw = (stream or {}).get("ended_at")
        if ended_raw:
            end = datetime.fromisoformat(str(ended_raw).replace("Z", "+00:00"))
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
        else:
            end = datetime.now(timezone.utc)
        delta = end - start
        return str(max(1, int(delta.total_seconds() // 60)))
    except (TypeError, ValueError):
        return "—"


# ponytail: Twitch category → IGDB meta TTL; ceiling = process memory / stale genres.
_igdb_twitch_meta_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_IGDB_TWITCH_META_TTL_SEC = 6 * 3600
# ponytail: process-local L1 for DeepL IGDB summaries; DB is durable L2.
_IGDB_SUMMARY_TR_CACHE: dict[tuple[str, str], str] = {}
_IGDB_SUMMARY_TR_CACHE_MAX = 512
IGNORE_IGDB_KINDS = frozenset({"developer", "publisher", "genre", "game_mode"})
IGNORE_IGDB_MAX = 20
IGNORE_IGDB_BETA_ID = "ignore-igdb-categories"


def parse_ignore_igdb_entries(raw: Any) -> list[dict[str, Any]]:
    """Normalize stored JSON list of {kind, id, name}."""
    if raw is None or raw == "":
        return []
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        if kind not in IGNORE_IGDB_KINDS:
            continue
        try:
            igdb_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if igdb_id <= 0:
            continue
        key = (kind, igdb_id)
        if key in seen:
            continue
        seen.add(key)
        name = str(item.get("name") or "").strip() or str(igdb_id)
        out.append({"kind": kind, "id": igdb_id, "name": name})
        if len(out) >= IGNORE_IGDB_MAX:
            break
    return out


def dump_ignore_igdb_entries(entries: list[dict[str, Any]] | None) -> str:
    return json.dumps(parse_ignore_igdb_entries(entries or []), ensure_ascii=False)


def should_ignore_igdb_categories(
    meta: dict[str, Any] | None,
    stored: list[dict[str, Any]] | None,
) -> bool:
    """True when IGDB game meta intersects stored ignore entities."""
    if not meta or not stored:
        return False
    genre_ids = {int(x) for x in (meta.get("genres") or []) if x is not None}
    mode_ids = {int(x) for x in (meta.get("game_modes") or []) if x is not None}
    dev_ids = {int(x) for x in (meta.get("developers") or []) if x is not None}
    pub_ids = {int(x) for x in (meta.get("publishers") or []) if x is not None}
    for entry in parse_ignore_igdb_entries(stored):
        kind = entry["kind"]
        igdb_id = int(entry["id"])
        if kind == "genre" and igdb_id in genre_ids:
            return True
        if kind == "game_mode" and igdb_id in mode_ids:
            return True
        if kind == "developer" and igdb_id in dev_ids:
            return True
        if kind == "publisher" and igdb_id in pub_ids:
            return True
    return False


def normalize_ignore_keywords(text: str) -> str:
    parts = [part.strip() for part in text.split(",")]
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        key = part.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(part)
    return ", ".join(out)


def merge_ignore_keywords(*parts: str) -> str:
    return normalize_ignore_keywords(", ".join(parts))


def should_ignore_stream(ignore_keywords: str, game: str, title: str) -> bool:
    if not ignore_keywords.strip():
        return False
    game_text = game or ""
    title_text = title or ""
    for raw in ignore_keywords.split(","):
        keyword = raw.strip()
        if not keyword:
            continue
        try:
            pattern = re.compile(keyword, re.IGNORECASE)
        except re.error:
            # ponytail: invalid regex → literal substring; ceiling: no user-facing validation
            needle = keyword.lower()
            if needle in game_text.lower() or needle in title_text.lower():
                return True
            continue
        if pattern.search(game_text) or pattern.search(title_text):
            return True
    return False


def filter_streams_for_watch(
    streams: list[dict[str, Any]],
    *,
    min_viewers: int = 0,
    max_viewers: int | None = None,
    exclude_mature: bool = False,
    tags: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Client-side filters Helix does not support as query params."""
    need_tags = [t.strip().lower() for t in (tags or []) if t and str(t).strip()]
    out: list[dict[str, Any]] = []
    for s in streams:
        viewers = int(s.get("viewer_count") or 0)
        if viewers < min_viewers:
            continue
        if max_viewers is not None and viewers > max_viewers:
            continue
        if exclude_mature and bool(s.get("is_mature")):
            continue
        if need_tags:
            stream_tags = {
                str(t).strip().lower()
                for t in (s.get("tags") or [])
                if t is not None and str(t).strip()
            }
            if not all(tag in stream_tags for tag in need_tags):
                continue
        out.append(s)
    return out


def normalize_watch_tags(text: str, *, limit: int = 10) -> list[str]:
    """Parse comma/semicolon-separated Twitch tags; preserve first-seen casing."""
    out: list[str] = []
    seen: set[str] = set()
    for part in text.replace(";", ",").split(","):
        tag = part.strip()
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
        if len(out) >= limit:
            break
    return out


def pick_random_streams(
    streams: list[dict[str, Any]],
    n: int = 5,
    *,
    prefer_language: str | None = None,
) -> list[dict[str, Any]]:
    """Dedupe by user_id, then sample up to n streams (optional language priority)."""
    by_user: dict[str, dict[str, Any]] = {}
    for s in streams:
        uid = str(s.get("user_id") or "")
        if uid and uid not in by_user:
            by_user[uid] = s
    unique = list(by_user.values())
    prefer = (prefer_language or "").strip().lower()
    if prefer:
        preferred = [
            s
            for s in unique
            if str(s.get("language") or "").strip().lower() == prefer
        ]
        others = [
            s
            for s in unique
            if str(s.get("language") or "").strip().lower() != prefer
        ]
        random.shuffle(preferred)
        random.shuffle(others)
        ordered = preferred + others
        return ordered[:n]
    if len(unique) <= n:
        random.shuffle(unique)
        return unique
    return random.sample(unique, n)


def _parse_twitchdrops_app_campaign(raw: dict[str, Any]) -> dict[str, Any] | None:
    campaign_id = str(raw.get("id") or "").strip()
    if not campaign_id:
        return None
    game_id = str(raw.get("gameId") or "").strip()
    game_name = str(raw.get("game") or "").strip()
    game_slug = str(raw.get("gameSlug") or "").strip()
    if game_id and game_slug:
        _twitchdrops_app_slug_by_game_id[game_id] = game_slug
    drops_raw = raw.get("drops") or []
    drops: list[dict[str, Any]] = []
    how_parts: list[str] = []
    for d in drops_raw if isinstance(drops_raw, list) else []:
        if not isinstance(d, dict):
            continue
        minutes = d.get("requiredMinutes")
        try:
            required = int(minutes) if minutes is not None else None
        except (TypeError, ValueError):
            required = None
        drop_name = str(d.get("name") or d.get("rewardName") or "").strip()
        reward = str(d.get("rewardName") or "").strip()
        names = [reward] if reward and reward != drop_name else []
        drops.append(
            {
                "id": str(d.get("id") or ""),
                "name": drop_name,
                "required_minutes": required,
                "benefit_names": names,
            }
        )
        bit = drop_name or reward
        if required is not None and bit:
            how_parts.append(f"{bit}: {required} min")
        elif bit:
            how_parts.append(bit)
    details = str(raw.get("description") or "").strip()
    how_to_earn = details or "; ".join(how_parts)
    return {
        "id": campaign_id,
        "name": str(raw.get("name") or "").strip(),
        "status": str(raw.get("status") or "").strip(),
        "game_id": game_id,
        "game_name": game_name,
        "game_slug": game_slug,
        "starts_at": str(raw.get("startAt") or ""),
        "ends_at": str(raw.get("endAt") or ""),
        "how_to_earn": how_to_earn,
        "drops": drops,
    }


def fetch_twitchdrops_app_campaigns(
    *, sort: str = "new", session: requests.Session | None = None
) -> list[dict[str, Any]]:
    """Active campaigns from https://twitchdrops.app/?sort=new (public JSON)."""
    http = session if session is not None else requests
    resp = http.get(
        f"{_TWITCHDROPS_APP_BASE}/api/drops",
        params={"sort": sort or "new"},
        headers={"User-Agent": _TWITCHDROPS_APP_UA, "Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    raw_list = body.get("campaigns") if isinstance(body, dict) else body
    out: list[dict[str, Any]] = []
    for raw in raw_list if isinstance(raw_list, list) else []:
        if not isinstance(raw, dict):
            continue
        parsed = _parse_twitchdrops_app_campaign(raw)
        if parsed:
            out.append(parsed)
    return out


def twitchdrops_app_game_url(game_slug: str) -> str:
    """Canonical public game page on twitchdrops.app (link only — no scrape)."""
    slug = re.sub(r"[^a-z0-9\-]", "", str(game_slug or "").strip().lower())
    if not slug:
        return ""
    return f"{_TWITCHDROPS_APP_BASE}/game/{slug}"



TWITCH_STATUS_URL = "https://status.twitch.com/api/v2/summary.json"


def fetch_twitch_status_summary(session: requests.Session | None = None) -> dict[str, Any]:
    """Fetch Twitch Statuspage summary JSON (status.twitch.com)."""
    http = session if session is not None else requests
    response = http.get(TWITCH_STATUS_URL, timeout=15)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Twitch status summary is not an object")
    return data


def twitch_status_fingerprint(
    summary: dict[str, Any],
) -> tuple[str, tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]:
    """Comparable snapshot: indicator, components, active incidents."""
    status = summary.get("status") or {}
    indicator = str(status.get("indicator") or "none")
    components: list[tuple[str, str]] = []
    for comp in summary.get("components") or []:
        if not isinstance(comp, dict) or comp.get("group"):
            continue
        name = str(comp.get("name") or "").strip()
        if not name:
            continue
        components.append((name, str(comp.get("status") or "operational")))
    components.sort(key=lambda item: item[0].lower())
    incidents: list[tuple[str, str]] = []
    for incident in summary.get("incidents") or []:
        if not isinstance(incident, dict):
            continue
        incident_id = str(incident.get("id") or "").strip()
        if not incident_id:
            continue
        incidents.append((incident_id, str(incident.get("status") or "")))
    incidents.sort(key=lambda item: item[0])
    return indicator, tuple(components), tuple(incidents)
