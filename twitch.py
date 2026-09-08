from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone
from difflib import get_close_matches
from typing import Any
from urllib.parse import urlencode, urlparse

import requests

from config import TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET

FOLLOWS_SCOPE = "user:read:follows"
SCHEDULE_SCOPE = "channel:manage:schedule"
SUBSCRIPTIONS_SCOPE = "user:read:subscriptions"
WHISPERS_SCOPE = "user:read:whispers"
CHAT_READ_SCOPE = "user:read:chat"
CHAT_WRITE_SCOPE = "user:write:chat"
CHAT_OAUTH_SCOPES = f"{CHAT_READ_SCOPE} {CHAT_WRITE_SCOPE}"
# Schedule publish may overwrite twitch_sync used by follow import — keep both.
SCHEDULE_OAUTH_SCOPES = f"{SCHEDULE_SCOPE} {FOLLOWS_SCOPE}"
# Drops GQL needs a user session; Helix scopes are unused by gql but authorize requires one.
DROPS_OAUTH_SCOPES = FOLLOWS_SCOPE

logger = logging.getLogger(__name__)

TWITCH_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.)?twitch\.tv/([a-zA-Z0-9_]{4,25})",
    re.IGNORECASE,
)
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{4,25}$")

# ponytail: public Twitch web player Client-ID for anonymous gql only (channel about).
_TWITCH_GQL_WEB_CLIENT_ID = "kimne78kx3ncx6brgo4" "mv6wki5h1ko"
# Android app Client-ID — device-code tokens work with ViewerDropsDashboard GQL
# (Helix tokens from our confidential app do not). Same approach as TwitchDropsMiner.
_TWITCH_DROPS_GQL_CLIENT_ID = "kd1unb4b3q4t58fwlpcbzcbnm76a8fp"
_TWITCH_DROPS_GQL_USER_AGENT = (
    "Dalvik/2.1.0 (Linux; U; Android 16; SM-S911B Build/TP1A.220624.014) "
    "tv.twitch.android.app/25.3.0/2503006"
)
# Persisted-query hashes from TwitchDropsMiner; update if GQL breaks.
_GQL_VIEWER_DROPS_DASHBOARD_HASH = (
    "c16bb890cc8ce7647a96ee69cd313d423a378a3dedadf630a1017cde18975feb"
)
_GQL_DROP_CAMPAIGN_DETAILS_HASH = (
    "039277bf98f3130929262cc7c6efd9c141ca3749cb6dca442fc8ead9a53f77c1"
)
_GQL_INVENTORY_HASH = (
    "8337eb8541b314040b0edde0c09c5c7a2783ba1960aa9edfbf3bac16d0fec404"
)
# Helix stream tags for Drops-enabled (EN + RU), compared casefold.
_DROPS_ENABLED_TAG_NEEDLES = ("drops enabled", "drops включены")
_DROPS_ENABLED_TAG = "Drops Enabled"  # back-compat label
_CHANNEL_ABOUT_GQL = """
query ChannelAboutLinks($login: String!) {
  user(login: $login) {
    panels {
      id
      type
      ... on DefaultPanel {
        title
        description
        imageURL
        linkURL
      }
    }
    channel {
      socialMedias {
        name
        title
        url
      }
    }
  }
}
"""

_IGDB_GAMES_URL = "https://api.igdb.com/v4/games"
_IGDB_COUNT_URL = "https://api.igdb.com/v4/games/count"
_IGDB_EXTERNAL_GAMES_URL = "https://api.igdb.com/v4/external_games"
_IGDB_WHERE = "version_parent = null & name != null"
# Games that exist as Twitch categories (external_game_source / legacy category = 14).
_IGDB_WHERE_TWITCH = (
    f"{_IGDB_WHERE} & (external_games.external_game_source = 14 "
    f"| external_games.category = 14)"
)
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
BOX_ART_WIDTH = 1920
BOX_ART_HEIGHT = 2560


def is_game_cover_image(image_file_id: str | None) -> bool:
    return (image_file_id or "") == GAME_COVER_IMAGE_ID


def template_has_game_placeholder(template: str) -> bool:
    return "{game}" in (template or "")


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
    if is_game_cover_image(fid):
        if twitch is None:
            return None
        game_id, game_name = _stream_game_fields(stream or {})
        return twitch.resolve_box_art_url(game_id=game_id, game_name=game_name)
    return fid


class TwitchClient:
    def __init__(self) -> None:
        self._session = requests.Session()
        self._token = ""
        self._token_expires = 0.0
        # Stable fake device id for Android-app GQL (TwitchDropsMiner pattern).
        self._drops_device_id = "".join(
            random.choice("0123456789abcdef") for _ in range(32)
        )

    def _drops_gql_headers(
        self, access_token: str, *, client_id: str, device_id: str | None = None
    ) -> dict[str, str]:
        did = (device_id or self._drops_device_id or "").strip() or self._drops_device_id
        session_id = "".join(random.choice("0123456789abcdef") for _ in range(16))
        return {
            "Accept": "*/*",
            "Accept-Encoding": "gzip",
            "Accept-Language": "en-US",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "Client-Id": client_id,
            "Client-Session-Id": session_id,
            "Authorization": f"OAuth {access_token}",
            "Content-Type": "application/json",
            "Origin": "https://www.twitch.tv",
            "Referer": "https://www.twitch.tv/",
            "User-Agent": _TWITCH_DROPS_GQL_USER_AGENT,
            "X-Device-Id": did,
        }

    def _drops_oauth_headers(self, *, device_id: str | None = None) -> dict[str, str]:
        did = (device_id or self._drops_device_id or "").strip() or self._drops_device_id
        return {
            "Accept": "application/json",
            "Client-Id": _TWITCH_DROPS_GQL_CLIENT_ID,
            "Origin": "https://www.twitch.tv",
            "Referer": "https://www.twitch.tv/",
            "User-Agent": _TWITCH_DROPS_GQL_USER_AGENT,
            "X-Device-Id": did,
        }

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
    def _about_link_key(url: str) -> str:
        parsed = urlparse((url or "").strip().lower().rstrip("/"))
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return ""
        path = parsed.path.rstrip("/")
        return f"{parsed.scheme}://{parsed.netloc}{path}"

    def get_channel_about_links(self, login: str) -> list[dict[str, str]]:
        """Panels + social links from twitch.tv/{login}/about (via internal GQL)."""
        login = (login or "").strip().lower()
        if not login:
            return []
        resp = self._session.post(
            "https://gql.twitch.tv/gql",
            headers={
                "Client-ID": _TWITCH_GQL_WEB_CLIENT_ID,
                "Referer": "https://www.twitch.tv/",
                "Content-Type": "application/json",
            },
            json={"query": _CHANNEL_ABOUT_GQL, "variables": {"login": login}},
            timeout=15,
        )
        resp.raise_for_status()
        user = (resp.json().get("data") or {}).get("user") or {}
        if not user:
            return []

        seen: set[str] = set()
        links: list[dict[str, str]] = []

        def add(*, url: str, label: str, image_url: str = "", kind: str) -> None:
            raw = (url or "").strip()
            key = self._about_link_key(raw)
            if not key or key in seen:
                return
            seen.add(key)
            parsed = urlparse(raw)
            text = (label or "").strip()
            if not text:
                text = parsed.netloc.removeprefix("www.")
            links.append(
                {
                    "url": raw,
                    "label": text,
                    "image_url": (image_url or "").strip(),
                    "kind": kind,
                }
            )

        for panel in user.get("panels") or []:
            if not isinstance(panel, dict):
                continue
            link_url = str(panel.get("linkURL") or "")
            if not link_url:
                continue
            title = str(panel.get("title") or "").strip()
            desc = str(panel.get("description") or "").strip()
            label = title or (desc.split("\n", 1)[0][:120] if desc else "")
            add(
                url=link_url,
                label=label,
                image_url=str(panel.get("imageURL") or ""),
                kind="panel",
            )

        channel = user.get("channel") or {}
        for item in channel.get("socialMedias") or []:
            if not isinstance(item, dict):
                continue
            link_url = str(item.get("url") or "")
            if link_url.startswith("mailto:"):
                continue
            label = str(item.get("title") or item.get("name") or "").strip()
            add(url=link_url, label=label, kind="social")

        return links

    def _gql_persisted(
        self,
        *,
        operation_name: str,
        sha256_hash: str,
        variables: dict[str, Any],
        access_token: str,
        client_id: str | None = None,
        device_id: str | None = None,
    ) -> dict[str, Any]:
        """Undocumented gql.twitch.tv persisted query (may break without notice).

        Drops catalog requires a token issued for ``_TWITCH_DROPS_GQL_CLIENT_ID``
        (device-code), not a Helix token from our confidential app Client-ID.
        """
        cid = (client_id or _TWITCH_DROPS_GQL_CLIENT_ID).strip()
        headers = self._drops_gql_headers(
            access_token, client_id=cid, device_id=device_id
        )
        payload = {
            "operationName": operation_name,
            "variables": variables,
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": sha256_hash,
                }
            },
        }
        resp = self._session.post(
            "https://gql.twitch.tv/gql",
            headers=headers,
            json=payload,
            timeout=20,
        )
        resp.raise_for_status()
        body = resp.json()
        if isinstance(body, list):
            body = body[0] if body else {}
        if not isinstance(body, dict):
            return {}
        errors = body.get("errors")
        data = body.get("data")
        if errors:
            msg = ""
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                msg = str(errors[0].get("message") or "")[:120]
            # Twitch often returns soft errors alongside usable data — only fail hard
            # when there is nothing to read (TwitchDropsMiner does the same).
            if not isinstance(data, dict) or not data:
                logger.warning(
                    "Twitch GQL %s failed: %s", operation_name, msg or "error"
                )
                raise RuntimeError(f"twitch gql {operation_name} failed")
            logger.warning(
                "Twitch GQL %s soft error (using data): %s",
                operation_name,
                msg or "error",
            )
        return body

    @staticmethod
    def _parse_drop_campaign(raw: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        campaign_id = str(raw.get("id") or "").strip()
        if not campaign_id:
            return None
        game = raw.get("game") or {}
        if not isinstance(game, dict):
            game = {}
        game_id = str(game.get("id") or raw.get("gameId") or "").strip()
        game_name = str(game.get("displayName") or game.get("name") or "").strip()
        status = str(raw.get("status") or "").strip().upper()
        details = str(
            raw.get("details")
            or raw.get("description")
            or raw.get("detailsURL")
            or ""
        ).strip()
        drops_raw = raw.get("timeBasedDrops") or raw.get("timeBasedDrop") or []
        if isinstance(drops_raw, dict):
            drops_raw = [drops_raw]
        drops: list[dict[str, Any]] = []
        how_parts: list[str] = []
        all_claimed = True
        any_drop = False
        for d in drops_raw if isinstance(drops_raw, list) else []:
            if not isinstance(d, dict):
                continue
            any_drop = True
            required = d.get("requiredMinutesWatched")
            try:
                minutes = int(required) if required is not None else None
            except (TypeError, ValueError):
                minutes = None
            benefit = d.get("benefitEdges") or d.get("benefits") or []
            names: list[str] = []
            if isinstance(benefit, list):
                for edge in benefit:
                    node = edge.get("benefit") if isinstance(edge, dict) else None
                    if isinstance(node, dict) and node.get("name"):
                        names.append(str(node["name"]))
                    elif isinstance(edge, dict) and edge.get("name"):
                        names.append(str(edge["name"]))
            self_edge = d.get("self") if isinstance(d.get("self"), dict) else {}
            is_claimed = bool(self_edge.get("isClaimed")) if self_edge else False
            if not is_claimed:
                all_claimed = False
            drop_name = str(d.get("name") or "")
            drops.append(
                {
                    "id": str(d.get("id") or ""),
                    "name": drop_name,
                    "required_minutes": minutes,
                    "benefit_names": names,
                    "is_claimed": is_claimed,
                }
            )
            bit = drop_name or (", ".join(names) if names else "")
            if minutes is not None and bit:
                how_parts.append(f"{bit}: {minutes} min")
            elif bit:
                how_parts.append(bit)
        how_to_earn = details or "; ".join(how_parts)
        return {
            "id": campaign_id,
            "name": str(raw.get("name") or "").strip(),
            "status": status,
            "game_id": game_id,
            "game_name": game_name,
            "starts_at": str(raw.get("startAt") or raw.get("startsAt") or ""),
            "ends_at": str(raw.get("endAt") or raw.get("endsAt") or ""),
            "how_to_earn": how_to_earn,
            "claimed": bool(any_drop and all_claimed),
            "drops": drops,
        }

    def get_viewer_drop_campaigns(
        self, access_token: str, *, device_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Active drop campaigns visible to the authenticated user (GQL)."""
        body = self._gql_persisted(
            operation_name="ViewerDropsDashboard",
            sha256_hash=_GQL_VIEWER_DROPS_DASHBOARD_HASH,
            variables={"fetchRewardCampaigns": False},
            access_token=access_token,
            device_id=device_id,
        )
        data = body.get("data") or {}
        current = data.get("currentUser")
        if not isinstance(current, dict):
            logger.warning("Twitch GQL ViewerDropsDashboard: currentUser missing")
            raise RuntimeError("twitch gql ViewerDropsDashboard unauthorized")
        raw_list = current.get("dropCampaigns") or []
        out: list[dict[str, Any]] = []
        for raw in raw_list if isinstance(raw_list, list) else []:
            parsed = self._parse_drop_campaign(raw if isinstance(raw, dict) else {})
            if parsed:
                out.append(parsed)
        return out

    def get_drop_campaign_details(
        self, access_token: str, *, campaign_id: str, channel_login: str = ""
    ) -> dict[str, Any] | None:
        body = self._gql_persisted(
            operation_name="DropCampaignDetails",
            sha256_hash=_GQL_DROP_CAMPAIGN_DETAILS_HASH,
            variables={
                "dropID": campaign_id,
                "channelLogin": channel_login or "",
            },
            access_token=access_token,
        )
        user = ((body.get("data") or {}).get("user") or {})
        raw = user.get("dropCampaign")
        if not isinstance(raw, dict):
            return None
        return self._parse_drop_campaign(raw)

    def get_inventory_claimed_drops(
        self, access_token: str, *, device_id: str | None = None
    ) -> dict[str, dict[str, Any]]:
        """Map drop_id -> {name, campaign_id, game_id, is_claimed} from Inventory GQL."""
        body = self._gql_persisted(
            operation_name="Inventory",
            sha256_hash=_GQL_INVENTORY_HASH,
            variables={"fetchRewardCampaigns": False},
            access_token=access_token,
            device_id=device_id,
        )
        current = ((body.get("data") or {}).get("currentUser") or {})
        inventory = current.get("inventory") or {}
        if not isinstance(inventory, dict):
            inventory = {}
        out: dict[str, dict[str, Any]] = {}
        campaigns = inventory.get("dropCampaignsInProgress") or []
        for camp in campaigns if isinstance(campaigns, list) else []:
            if not isinstance(camp, dict):
                continue
            campaign_id = str(camp.get("id") or "")
            game = camp.get("game") if isinstance(camp.get("game"), dict) else {}
            game_id = str(game.get("id") or "")
            for d in camp.get("timeBasedDrops") or []:
                if not isinstance(d, dict):
                    continue
                drop_id = str(d.get("id") or "").strip()
                if not drop_id:
                    continue
                self_edge = d.get("self") if isinstance(d.get("self"), dict) else {}
                out[drop_id] = {
                    "id": drop_id,
                    "name": str(d.get("name") or ""),
                    "campaign_id": campaign_id,
                    "game_id": game_id,
                    "is_claimed": bool(self_edge.get("isClaimed")),
                }
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
        if not uid:
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
        self, broadcaster_id: str, *, first: int = 20, start_time: str | None = None
    ) -> dict[str, Any]:
        """Schedule payload: segments list + optional vacation window."""
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
            return {"segments": [], "vacation": None}
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        segments = [s for s in (data.get("segments") or []) if isinstance(s, dict)]
        vacation = data.get("vacation")
        if not isinstance(vacation, dict):
            vacation = None
        return {"segments": segments, "vacation": vacation}

    def get_schedule_segments(
        self, broadcaster_id: str, *, first: int = 20, start_time: str | None = None
    ) -> list[dict[str, Any]]:
        """Upcoming schedule segments; empty if no schedule."""
        return list(
            self.get_channel_schedule(
                broadcaster_id, first=first, start_time=start_time
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

    def delete_overlapping_schedule_segments(
        self,
        user_access_token: str,
        broadcaster_id: str,
        *,
        start_time: str,
        duration: int = 120,
        exclude_ids: tuple[str, ...] | list[str] = (),
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
        skipped = {str(x) for x in exclude_ids if x}
        deleted = 0
        for sid in ids:
            if sid in skipped:
                continue
            try:
                self.delete_schedule_segment(user_access_token, broadcaster_id, sid)
                deleted += 1
            except Exception as exc:
                logger.warning(
                    "Failed to delete overlapping schedule segment %s: %s", sid, exc
                )
        return deleted

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
    ) -> str:
        return "https://id.twitch.tv/oauth2/authorize?" + urlencode(
            {
                "client_id": TWITCH_CLIENT_ID,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": FOLLOWS_SCOPE if scopes is None else scopes,
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

    def start_drops_device_code(
        self, *, device_id: str | None = None
    ) -> dict[str, Any]:
        """Device-code login for Drops GQL (Android public Client-ID, no secret)."""
        resp = self._session.post(
            "https://id.twitch.tv/oauth2/device",
            headers=self._drops_oauth_headers(device_id=device_id),
            data={
                "client_id": _TWITCH_DROPS_GQL_CLIENT_ID,
                "scopes": "",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "device_code": str(data.get("device_code") or ""),
            "user_code": str(data.get("user_code") or ""),
            "verification_uri": str(
                data.get("verification_uri") or "https://www.twitch.tv/activate"
            ),
            "interval": max(3, int(data.get("interval") or 5)),
            "expires_in": max(60, int(data.get("expires_in") or 1800)),
        }

    def poll_drops_device_code(
        self, device_code: str, *, device_id: str | None = None
    ) -> dict[str, Any] | None:
        """Return token payload when authorized; None while pending; raise on hard fail."""
        resp = self._session.post(
            "https://id.twitch.tv/oauth2/token",
            headers=self._drops_oauth_headers(device_id=device_id),
            data={
                "client_id": _TWITCH_DROPS_GQL_CLIENT_ID,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
        try:
            err = resp.json()
        except Exception:
            err = {}
        code = str(err.get("message") or err.get("error") or "")
        if code in ("authorization_pending", "slow_down"):
            return None
        resp.raise_for_status()
        return None

    def refresh_drops_gql_token(self, refresh_token: str) -> dict[str, Any]:
        """Refresh a Drops device-code token (public client — no secret)."""
        # Plain form POST — extra Android headers have caused refresh to fail
        # immediately after device-code exchange, wiping auth and re-prompting.
        resp = self._session.post(
            "https://id.twitch.tv/oauth2/token",
            data={
                "client_id": _TWITCH_DROPS_GQL_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def get_drops_token_user(self, user_access_token: str) -> dict[str, Any] | None:
        resp = self._session.get(
            "https://api.twitch.tv/helix/users",
            headers={
                "Client-ID": _TWITCH_DROPS_GQL_CLIENT_ID,
                "Authorization": f"Bearer {user_access_token}",
                "User-Agent": _TWITCH_DROPS_GQL_USER_AGENT,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get("data") or []
        return data[0] if data else None

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
        gid = str(game_id or "").strip()
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
        tpl = str(pick.get("box_art_url") or "").strip()
        if tpl:
            return format_box_art_url(tpl, width=width, height=height)
        cid = str(pick.get("id") or "").strip()
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
                duration=duration,
                title=title,
                category_id=category_id,
            )

        try:
            return _update(), False
        except Exception as exc:
            if self.is_overlapping_schedule(exc):
                self.delete_overlapping_schedule_segments(
                    user_access_token,
                    broadcaster_id,
                    start_time=start_time,
                    duration=duration,
                    exclude_ids=(segment_id,),
                )
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

    def igdb_random_games(self, n: int = 5) -> list[dict[str, Any]]:
        """n random main games from IGDB (like igdb.com/random), Twitch-mapped."""
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _ in range(max(1, n) * 4):
            if len(out) >= n:
                break
            try:
                rows = self._igdb_random_game_rows(limit=1, twitch_only=True)
            except Exception as exc:
                logger.warning("IGDB random games failed (%s)", exc)
                break
            for row in rows:
                name = str(row.get("name") or "").strip()
                key = name.lower()
                if not name or key in seen:
                    continue
                seen.add(key)
                out.append(row)
                if len(out) >= n:
                    break
        while len(out) < n:
            name = random.choice(_FALLBACK_GAMES)
            key = name.lower()
            if key in seen:
                break
            seen.add(key)
            out.append({"name": name})
        return out[:n]

    def igdb_recently_released_games(self, n: int = 5) -> list[dict[str, Any]]:
        """Random sample from recent IGDB releases (like igdb.com/games/recently_released)."""
        headers = self._igdb_headers()
        want = max(1, n)
        # Wider window so each lucky run can pick different titles.
        window = max(want * 10, 50)
        body = (
            f"fields name, external_games.external_game_source, "
            f"external_games.category, external_games.uid, external_games.url;\n"
            f"where {_IGDB_WHERE_TWITCH} & first_release_date != null;\n"
            f"sort first_release_date desc;\n"
            f"limit {min(100, window)};"
        )
        try:
            resp = self._session.post(
                _IGDB_GAMES_URL, headers=headers, data=body, timeout=15
            )
            resp.raise_for_status()
            rows = resp.json()
            if isinstance(rows, list) and rows:
                pool = list(rows)
                if len(pool) <= want:
                    return pool
                return random.sample(pool, want)
        except Exception as exc:
            logger.warning("IGDB recently released failed (%s)", exc)
        return self.igdb_random_games(n)

    def igdb_top100_games(self, n: int = 5) -> list[dict[str, Any]]:
        """Random sample from IGDB top ~100 (like igdb.com/top-100/games)."""
        headers = self._igdb_headers()
        want = max(1, n)
        body = (
            f"fields name, external_games.external_game_source, "
            f"external_games.category, external_games.uid, external_games.url;\n"
            f"where {_IGDB_WHERE_TWITCH};\n"
            f"sort total_rating_count desc;\n"
            f"limit 100;"
        )
        try:
            resp = self._session.post(
                _IGDB_GAMES_URL, headers=headers, data=body, timeout=15
            )
            resp.raise_for_status()
            rows = resp.json()
            if isinstance(rows, list) and rows:
                pool = list(rows)
                if len(pool) <= want:
                    return pool
                return random.sample(pool, want)
        except Exception as exc:
            logger.warning("IGDB top-100 failed (%s)", exc)
        return self.igdb_random_games(n)

    def _igdb_twitch_uid(self, game: dict[str, Any]) -> str:
        """Twitch category id from IGDB game row (expanded or via external_games ids)."""
        eg_list = game.get("external_games") or []
        pending_ids: list[int] = []

        def _is_twitch(row: dict[str, Any]) -> bool:
            src = row.get("external_game_source")
            if src is None:
                src = row.get("category")
            try:
                return int(src or 0) == _IGDB_EXTERNAL_TWITCH
            except (TypeError, ValueError):
                return False

        for eg in eg_list:
            if isinstance(eg, dict):
                uid = str(eg.get("uid") or "").strip()
                if _is_twitch(eg) and uid:
                    return uid
                # Nested expansion often returns {id, uid} without source/category.
                eid = eg.get("id")
                if eid is not None:
                    try:
                        pending_ids.append(int(eid))
                    except (TypeError, ValueError):
                        pass
                # Twitch directory URL in expanded payload.
                url = str(eg.get("url") or "")
                if uid and "twitch.tv" in url:
                    return uid
            else:
                try:
                    pending_ids.append(int(eg))
                except (TypeError, ValueError):
                    continue
        if not pending_ids:
            return ""
        headers = self._igdb_headers()
        ids = ",".join(str(i) for i in pending_ids[:50])
        body = (
            f"fields external_game_source, category, uid, url;\n"
            f"where id = ({ids}) & (external_game_source = {_IGDB_EXTERNAL_TWITCH} "
            f"| category = {_IGDB_EXTERNAL_TWITCH});\n"
            f"limit 50;"
        )
        try:
            resp = self._session.post(
                _IGDB_EXTERNAL_GAMES_URL, headers=headers, data=body, timeout=15
            )
            resp.raise_for_status()
            rows = resp.json()
            if isinstance(rows, list):
                for row in rows:
                    if not _is_twitch(row) and "twitch.tv" not in str(row.get("url") or ""):
                        continue
                    uid = str(row.get("uid") or "").strip()
                    if uid:
                        return uid
        except Exception as exc:
            logger.warning("IGDB external_games lookup failed (%s)", exc)
        # Fallback: fetch without source filter and pick Twitch by url/source.
        body2 = (
            f"fields external_game_source, category, uid, url;\n"
            f"where id = ({ids});\n"
            f"limit 50;"
        )
        try:
            resp = self._session.post(
                _IGDB_EXTERNAL_GAMES_URL, headers=headers, data=body2, timeout=15
            )
            resp.raise_for_status()
            rows = resp.json()
            if isinstance(rows, list):
                for row in rows:
                    if _is_twitch(row) or "twitch.tv" in str(row.get("url") or ""):
                        uid = str(row.get("uid") or "").strip()
                        if uid:
                            return uid
        except Exception as exc:
            logger.warning("IGDB external_games fallback failed (%s)", exc)
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

    def _igdb_random_game_rows(
        self, *, limit: int = 1, twitch_only: bool = False
    ) -> list[dict[str, Any]]:
        headers = self._igdb_headers()
        where = _IGDB_WHERE_TWITCH if twitch_only else _IGDB_WHERE
        count = 0
        try:
            count_resp = self._session.post(
                _IGDB_COUNT_URL,
                headers=headers,
                data=f"where {where};",
                timeout=15,
            )
            if count_resp.ok:
                count = int(count_resp.json().get("count") or 0)
        except Exception as exc:
            logger.warning("IGDB count failed (%s)", exc)

        max_offset = max(0, min(count - 1, 200_000)) if count else 20_000
        lim = max(1, min(10, int(limit)))
        for _ in range(4):
            offset = random.randint(0, max_offset)
            body = (
                f"fields name, external_games.external_game_source, "
                f"external_games.category, external_games.uid, external_games.url;\n"
                f"where {where};\n"
                f"sort id asc;\n"
                f"limit {lim};\n"
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
                return list(rows)
            if offset == 0:
                break
            max_offset = max(0, offset // 2)
        return []

    def _random_igdb_game_name(self) -> str:
        rows = self._igdb_random_game_rows(limit=1)
        if rows:
            name = str(rows[0].get("name") or "").strip()
            if name:
                return name
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
    return bool(_TEMPLATE_HTML_RE.search(template or ""))


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
) -> str:
    """Fill template placeholders from channel + optional Helix stream payload.

    When escape_html=True, placeholder values are HTML-escaped so user HTML tags
    in the template skeleton stay intact under ParseMode.HTML.
    """
    if strip_name_mentions and "{name}" in template:
        name = strip_name_mentions_and_commands(name, twitch)
    values = _template_values(username, game, name, stream)
    if extra:
        values.update(extra)
    if escape_html:
        import html as _html

        values = {k: _html.escape(v) for k, v in values.items()}
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
    "duration": "minutes",
    "length": "minutes",
    "streamid": "id",
    "stream_id": "id",
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


def stream_duration_minutes(stream: dict[str, Any] | None) -> str:
    started = (stream or {}).get("started_at")
    if not started:
        return "—"
    try:
        start = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - start
        return str(max(1, int(delta.total_seconds() // 60)))
    except (TypeError, ValueError):
        return "—"


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
