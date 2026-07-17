from __future__ import annotations

import logging
import re
import time
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
