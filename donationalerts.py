"""DonationAlerts OAuth + donations list (official public API).

Docs: https://www.donationalerts.com/apidoc
Scopes used: oauth-user-show oauth-donation-index
Rate limit: 60 req/min per application — one list fetch per end-alert.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import requests

from config import (
    DONATIONALERTS_CLIENT_ID,
    DONATIONALERTS_CLIENT_SECRET,
    PUBLIC_BASE_URL,
)

logger = logging.getLogger(__name__)

API_BASE = "https://www.donationalerts.com/api/v1"
OAUTH_AUTHORIZE = "https://www.donationalerts.com/oauth/authorize"
OAUTH_TOKEN = "https://www.donationalerts.com/oauth/token"
SCOPES = "oauth-user-show oauth-donation-index"
TOP_DONATIONS_DEFAULT = 5
# ponytail: mixed-currency top-N sorts by raw amount (no FX). Upgrade: convert via rates.
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "twitch-telegram-bot/donationalerts"})


@dataclass(frozen=True)
class Donation:
    id: int
    username: str
    amount: float
    currency: str
    created_at: datetime

    def sum_label(self) -> str:
        amt = self.amount
        if amt == int(amt):
            return f"{int(amt)} {self.currency}"
        return f"{amt:g} {self.currency}"


def configured() -> bool:
    return bool(DONATIONALERTS_CLIENT_ID and DONATIONALERTS_CLIENT_SECRET and PUBLIC_BASE_URL)


def oauth_redirect_uri() -> str:
    base = (PUBLIC_BASE_URL or "").rstrip("/")
    return f"{base}/oauth/donationalerts/callback"


def build_authorize_url(*, state: str) -> str:
    if not configured():
        raise RuntimeError("DonationAlerts OAuth is not configured")
    q = urlencode(
        {
            "client_id": DONATIONALERTS_CLIENT_ID,
            "redirect_uri": oauth_redirect_uri(),
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
        }
    )
    return f"{OAUTH_AUTHORIZE}?{q}"


def exchange_code(code: str) -> dict[str, Any]:
    return _token_request(
        {
            "grant_type": "authorization_code",
            "client_id": DONATIONALERTS_CLIENT_ID,
            "client_secret": DONATIONALERTS_CLIENT_SECRET,
            "redirect_uri": oauth_redirect_uri(),
            "code": code,
        }
    )


def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    return _token_request(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": DONATIONALERTS_CLIENT_ID,
            "client_secret": DONATIONALERTS_CLIENT_SECRET,
            "scope": SCOPES,
        }
    )


def _token_request(data: dict[str, str]) -> dict[str, Any]:
    resp = _SESSION.post(OAUTH_TOKEN, data=data, timeout=30)
    if resp.status_code >= 400:
        logger.warning("DonationAlerts token HTTP %s", resp.status_code)
        resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise RuntimeError("DonationAlerts token response missing access_token")
    return payload


def fetch_user(access_token: str) -> dict[str, Any]:
    resp = _SESSION.get(
        f"{API_BASE}/user/oauth",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json().get("data") or {}
    return data if isinstance(data, dict) else {}


def _parse_created_at(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H.%M.%S"):
        try:
            # DA docs: local wall time without TZ — treat as UTC for window compare.
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def list_donations(
    access_token: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    max_pages: int = 5,
) -> list[Donation]:
    """Paginate donations newest-first; stop after `since` or max_pages."""
    out: list[Donation] = []
    page = 1
    while page <= max_pages:
        resp = _SESSION.get(
            f"{API_BASE}/alerts/donations",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"page": page},
            timeout=30,
        )
        if resp.status_code == 429:
            time.sleep(1.2)
            resp = _SESSION.get(
                f"{API_BASE}/alerts/donations",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"page": page},
                timeout=30,
            )
        resp.raise_for_status()
        payload = resp.json()
        rows = payload.get("data") or []
        if not isinstance(rows, list) or not rows:
            break
        stop = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            created = _parse_created_at(str(row.get("created_at") or ""))
            if created is None:
                continue
            if until is not None and created > until:
                continue
            if since is not None and created < since:
                stop = True
                break
            try:
                amount = float(row.get("amount") or 0)
            except (TypeError, ValueError):
                amount = 0.0
            out.append(
                Donation(
                    id=int(row.get("id") or 0),
                    username=str(row.get("username") or "").strip() or "—",
                    amount=amount,
                    currency=str(row.get("currency") or "").strip() or "?",
                    created_at=created,
                )
            )
        links = payload.get("links") or {}
        if stop or not links.get("next"):
            break
        page += 1
    return out


def top_donations(
    donations: list[Donation], *, limit: int = TOP_DONATIONS_DEFAULT
) -> list[Donation]:
    ranked = sorted(donations, key=lambda d: d.amount, reverse=True)
    return ranked[: max(0, int(limit))]


def render_donation_line(template: str, donation: Donation) -> str:
    text = template or ""
    return text.replace("{donate_user}", donation.username).replace(
        "{donate_sum}", donation.sum_label()
    )


def format_top_donations_block(
    template: str, donations: list[Donation], *, limit: int = TOP_DONATIONS_DEFAULT
) -> str:
    """Render one line per top donation; empty if none."""
    top = top_donations(donations, limit=limit)
    if not top or not (template or "").strip():
        return ""
    lines = [render_donation_line(template, d) for d in top]
    return "\n".join(line for line in lines if line.strip())


def stream_started_at(stream: dict[str, Any] | None) -> datetime | None:
    raw = str((stream or {}).get("started_at") or "").strip()
    if not raw:
        return None
    try:
        # Helix: 2024-01-01T12:00:00Z
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


BETA_FEATURE_ID = "top-donations"
DEFAULT_TOP_DONATIONS_TEMPLATE = "• {donate_user} — {donate_sum}"


def ensure_access_token(db: Any, owner_id: int) -> str | None:
    """Return a usable access token; refresh+persist when near expiry."""
    auth = db.get_donationalerts_auth(owner_id)
    if auth is None:
        return None
    now = int(time.time())
    token = str(auth.access_token or "")
    expires = int(auth.access_expires_at or 0)
    if token and expires > now + 60:
        return token
    refresh = str(auth.refresh_token or "")
    if not refresh or not configured():
        return token or None
    try:
        payload = refresh_access_token(refresh)
    except Exception:
        logger.exception("DonationAlerts refresh failed for owner %s", owner_id)
        return token or None
    new_access = str(payload.get("access_token") or "")
    new_refresh = str(payload.get("refresh_token") or refresh)
    expires_in = int(payload.get("expires_in") or 0)
    new_exp = now + expires_in if expires_in > 0 else 0
    try:
        db.update_donationalerts_auth_tokens(
            owner_id,
            refresh_token=new_refresh,
            access_token=new_access,
            access_expires_at=new_exp,
        )
    except Exception:
        logger.exception("DonationAlerts token persist failed for owner %s", owner_id)
    return new_access or None


def build_top_donations_suffix(
    db: Any,
    owner_id: int,
    template: str,
    stream: dict[str, Any] | None,
) -> str:
    """Fetch stream-window donations and render the top-N block (or '')."""
    access = ensure_access_token(db, owner_id)
    if not access:
        return ""
    since = stream_started_at(stream)
    until = datetime.now(timezone.utc)
    try:
        donations = list_donations(access, since=since, until=until)
    except Exception:
        logger.exception("DonationAlerts list failed for owner %s", owner_id)
        return ""
    return format_top_donations_block(template, donations)
