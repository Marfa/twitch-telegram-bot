from __future__ import annotations

import logging
import random
import re
import time
from difflib import get_close_matches
from typing import Any
from urllib.parse import urlencode

import requests

from config import TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET

FOLLOWS_SCOPE = "user:read:follows"
SCHEDULE_SCOPE = "channel:manage:schedule"
SUBSCRIPTIONS_SCOPE = "user:read:subscriptions"

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
        self, broadcaster_id: str, *, first: int = 20
    ) -> list[dict[str, Any]]:
        """Upcoming schedule segments; empty if no schedule."""
        resp = self._session.get(
            "https://api.twitch.tv/helix/schedule",
            headers=self._headers(),
            params={"broadcaster_id": broadcaster_id, "first": max(1, min(25, first))},
            timeout=15,
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        segments = data.get("segments") or []
        return [s for s in segments if isinstance(s, dict)]

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

    def search_categories(self, query: str) -> list[dict[str, Any]]:
        """Search Twitch categories/games by name. Returns list of {id, name, ...}."""
        resp = self._session.get(
            "https://api.twitch.tv/helix/search/categories",
            headers=self._headers(),
            params={"query": query, "first": 1},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("data") or []

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
) -> str:
    """Fill template placeholders from channel + optional Helix stream payload."""
    values = _template_values(username, game, name, stream)
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
)
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
