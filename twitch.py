from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone
from difflib import get_close_matches
from typing import Any
from urllib.parse import urlencode

import requests

from config import TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET

FOLLOWS_SCOPE = "user:read:follows"
SCHEDULE_SCOPE = "channel:manage:schedule"
SUBSCRIPTIONS_SCOPE = "user:read:subscriptions"
# Schedule publish may overwrite twitch_sync used by follow import — keep both.
SCHEDULE_OAUTH_SCOPES = f"{SCHEDULE_SCOPE} {FOLLOWS_SCOPE}"

logger = logging.getLogger(__name__)

TWITCH_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.)?twitch\.tv/([a-zA-Z0-9_]{4,25})",
    re.IGNORECASE,
)
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{4,25}$")

_IGDB_GAMES_URL = "https://api.igdb.com/v4/games"
_IGDB_COUNT_URL = "https://api.igdb.com/v4/games/count"
_IGDB_WHERE = "version_parent = null & name != null"
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


class TwitchClient:
    def __init__(self) -> None:
        self._session = requests.Session()
        self._token = ""
        self._token_expires = 0.0

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
        resp = self._session.get(
            "https://api.twitch.tv/helix/users",
            headers=self._headers(),
            params={"login": login.lower()},
            timeout=15,
        )
        resp.raise_for_status()
        users = resp.json().get("data", [])
        return users[0] if users else None

    def get_live_streams(self, user_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not user_ids:
            return {}
        params: list[tuple[str, str]] = [("user_id", uid) for uid in user_ids]
        resp = self._session.get(
            "https://api.twitch.tv/helix/streams",
            headers=self._headers(),
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        return {s["user_id"]: s for s in resp.json().get("data", [])}

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

    def get_schedule_segments(
        self, broadcaster_id: str, *, first: int = 20, start_time: str | None = None
    ) -> list[dict[str, Any]]:
        """Upcoming schedule segments; empty if no schedule."""
        params: dict[str, str | int] = {
            "broadcaster_id": broadcaster_id,
            "first": max(1, min(25, first)),
        }
        if start_time:
            params["start_time"] = start_time
        resp = self._session.get(
            "https://api.twitch.tv/helix/schedule",
            headers=self._headers(),
            params=params,
            timeout=15,
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        segments = data.get("segments") or []
        return [s for s in segments if isinstance(s, dict)]

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

    def delete_overlapping_schedule_segments(
        self,
        user_access_token: str,
        broadcaster_id: str,
        *,
        start_time: str,
        duration: int = 120,
    ) -> int:
        """Delete existing segments that would overlap a new one. Returns deleted count."""
        day_start = self._parse_schedule_time(start_time).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        existing = self.get_schedule_segments(
            broadcaster_id,
            first=25,
            start_time=day_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        ids = self.overlapping_schedule_segment_ids(
            existing, start_time=start_time, duration=duration
        )
        for sid in ids:
            try:
                self.delete_schedule_segment(user_access_token, broadcaster_id, sid)
            except Exception as exc:
                logger.warning(
                    "Failed to delete overlapping schedule segment %s: %s", sid, exc
                )
        return len(ids)

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
    ) -> str:
        return "https://id.twitch.tv/oauth2/authorize?" + urlencode(
            {
                "client_id": TWITCH_CLIENT_ID,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": scopes or FOLLOWS_SCOPE,
                "state": state,
            }
        )

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

    def search_categories(self, query: str, *, first: int = 1) -> list[dict[str, Any]]:
        """Search Twitch categories/games by name. Returns list of {id, name, ...}."""
        resp = self._session.get(
            "https://api.twitch.tv/helix/search/categories",
            headers=self._headers(),
            params={"query": query, "first": max(1, min(20, first))},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("data") or []

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
        On overlap, deletes conflicting segments and retries once when replace_overlap.
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
                self.delete_overlapping_schedule_segments(
                    user_access_token,
                    broadcaster_id,
                    start_time=start_time,
                    duration=duration,
                )
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
        """Pick a random main-game title from IGDB (Twitch credentials)."""
        try:
            return self._random_igdb_game_name()
        except Exception as exc:
            logger.warning("IGDB random game unavailable (%s)", exc)
            return random.choice(_FALLBACK_GAMES)

    def _random_igdb_game_name(self) -> str:
        headers = self._igdb_headers()
        count = 0
        try:
            count_resp = self._session.post(
                _IGDB_COUNT_URL,
                headers=headers,
                data=f"where {_IGDB_WHERE};",
                timeout=15,
            )
            if count_resp.ok:
                count = int(count_resp.json().get("count") or 0)
        except Exception as exc:
            logger.warning("IGDB count failed (%s)", exc)

        max_offset = max(0, min(count - 1, 200_000)) if count else 20_000
        for _ in range(4):
            offset = random.randint(0, max_offset)
            body = (
                f"fields name;\n"
                f"where {_IGDB_WHERE};\n"
                f"sort id asc;\n"
                f"limit 1;\n"
                f"offset {offset};"
            )
            resp = self._session.post(
                _IGDB_GAMES_URL,
                headers=headers,
                data=body,
                timeout=15,
            )
            if resp.status_code == 400 and offset > 0:
                max_offset = max(0, offset // 2)
                continue
            resp.raise_for_status()
            rows = resp.json()
            if isinstance(rows, list) and rows:
                name = str(rows[0].get("name") or "").strip()
                if name:
                    return name
            if offset == 0:
                break
            max_offset = max(0, offset // 2)
        return random.choice(_FALLBACK_GAMES)


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


def render_template(
    template: str,
    username: str,
    game: str = "",
    name: str = "",
    stream: dict[str, Any] | None = None,
    extra: dict[str, str] | None = None,
) -> str:
    """Fill template placeholders from channel + optional Helix stream payload."""
    values = _template_values(username, game, name, stream)
    if extra:
        values.update(extra)
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
        "thumbnail_url": "—",
        "tags": "—",
        "language": "—",
        "is_mature": "—",
        "game_id": "—",
        "id": "—",
        "type": "—",
        "minutes": "—",
    }
    if not stream:
        return values
    started = stream.get("started_at")
    if started:
        values["started_at"] = str(started)
    if stream.get("viewer_count") is not None:
        values["viewer_count"] = str(stream.get("viewer_count"))
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
    "thumbnail_url",
    "tags",
    "language",
    "is_mature",
    "game_id",
    "id",
    "type",
    "minutes",
)
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
        inner = match.group(1).lower().replace("-", "").replace("_", "")
        if inner in _TEMPLATE_PLACEHOLDERS:
            suggested = f"{{{inner}}}"
        else:
            close = get_close_matches(inner, list(_TEMPLATE_PLACEHOLDERS), n=1, cutoff=0.7)
            if not close:
                continue
            suggested = f"{{{close[0]}}}"
        seen.add(token)
        results.append((token, suggested))
    return results


def normalize_ignore_keywords(text: str) -> str:
    parts = [part.strip() for part in text.split(",")]
    return ", ".join(part for part in parts if part)


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
    streams: list[dict[str, Any]], n: int = 5
) -> list[dict[str, Any]]:
    """Dedupe by user_id, then sample up to n streams."""
    by_user: dict[str, dict[str, Any]] = {}
    for s in streams:
        uid = str(s.get("user_id") or "")
        if uid and uid not in by_user:
            by_user[uid] = s
    unique = list(by_user.values())
    if len(unique) <= n:
        random.shuffle(unique)
        return unique
    return random.sample(unique, n)


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
