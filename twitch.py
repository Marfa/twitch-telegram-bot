from __future__ import annotations

import logging
import re
import time
from difflib import get_close_matches
from typing import Any

import requests

from config import TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET

logger = logging.getLogger(__name__)

TWITCH_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.)?twitch\.tv/([a-zA-Z0-9_]{4,25})",
    re.IGNORECASE,
)
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{4,25}$")


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


def render_template(
    template: str,
    username: str,
    game: str,
    name: str,
) -> str:
    return (
        template.replace("{username}", username)
        .replace("{game}", game or "—")
        .replace("{name}", name or "—")
    )


_TEMPLATE_PLACEHOLDERS = ("username", "game", "name")
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
        if token in ("{username}", "{game}", "{name}"):
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
    game_lower = (game or "").lower()
    title_lower = (title or "").lower()
    for raw in ignore_keywords.split(","):
        keyword = raw.strip().lower()
        if keyword and (keyword in game_lower or keyword in title_lower):
            return True
    return False
