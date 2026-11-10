"""MAX Messenger Bot API client (subscriptions webhook).

Docs: https://dev.max.ru/docs-api/methods/POST/subscriptions/
Base: https://platform-api2.max.ru

No Telegram UI — call from ops / future Max receiver bootstrap.
Never log the access token or webhook secret.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://platform-api2.max.ru"
_SECRET_RE = re.compile(r"^[a-zA-Z0-9_-]{5,256}$")


class MaxApiError(Exception):
    """MAX API call failed."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _access_token() -> str:
    token = (os.getenv("MAX_BOT_TOKEN") or "").strip()
    if not token:
        raise MaxApiError("MAX_BOT_TOKEN is not set")
    return token


def _headers(token: str | None = None) -> dict[str, str]:
    return {
        "Authorization": token or _access_token(),
        "Content-Type": "application/json",
    }


def _parse_body(resp: requests.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except Exception as exc:
        raise MaxApiError(
            f"invalid json status={resp.status_code}",
            status_code=resp.status_code,
        ) from exc
    return data if isinstance(data, dict) else {}


def _raise_if_failed(resp: requests.Response, data: dict[str, Any]) -> None:
    if resp.status_code == 401:
        raise MaxApiError("unauthorized", status_code=401, code="unauthorized")
    # POST/DELETE /subscriptions: HTTP 200 with success:false in body.
    if resp.status_code == 200 and data.get("success") is False:
        raise MaxApiError(
            str(data.get("message") or "request failed"),
            status_code=200,
            code=str(data.get("code") or "") or None,
        )
    if resp.status_code >= 400:
        raise MaxApiError(
            str(data.get("message") or f"http {resp.status_code}"),
            status_code=resp.status_code,
            code=str(data.get("code") or "") or None,
        )


def subscribe(
    url: str,
    *,
    update_types: list[str] | None = None,
    secret: str | None = None,
    token: str | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """POST /subscriptions — deliver bot updates to ``url`` via webhook.

    ``secret`` (optional) must match ``^[a-zA-Z0-9_-]{5,256}$``; MAX sends it
    as ``X-Max-Bot-Api-Secret`` on each webhook request.
    """
    endpoint = (url or "").strip()
    if not endpoint.startswith("https://"):
        raise MaxApiError("url must be https:// on port 443")
    body: dict[str, Any] = {"url": endpoint}
    if update_types:
        body["update_types"] = list(update_types)
    if secret is not None:
        sec = str(secret).strip()
        if not _SECRET_RE.fullmatch(sec):
            raise MaxApiError(
                "secret must match ^[a-zA-Z0-9_-]{5,256}$",
                code="proto.payload",
            )
        body["secret"] = sec

    http = session or requests
    try:
        resp = http.post(
            f"{API_BASE}/subscriptions",
            headers=_headers(token),
            json=body,
            timeout=30,
        )
    except requests.RequestException as exc:
        raise MaxApiError(str(exc)) from exc
    data = _parse_body(resp)
    _raise_if_failed(resp, data)
    return data


def list_subscriptions(
    *,
    token: str | None = None,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    """GET /subscriptions — current webhook subscriptions."""
    http = session or requests
    try:
        resp = http.get(
            f"{API_BASE}/subscriptions",
            headers=_headers(token),
            timeout=30,
        )
    except requests.RequestException as exc:
        raise MaxApiError(str(exc)) from exc
    data = _parse_body(resp)
    if resp.status_code == 401:
        raise MaxApiError("unauthorized", status_code=401, code="unauthorized")
    if resp.status_code >= 400:
        raise MaxApiError(
            str(data.get("message") or f"http {resp.status_code}"),
            status_code=resp.status_code,
            code=str(data.get("code") or "") or None,
        )
    subs = data.get("subscriptions")
    if isinstance(subs, list):
        return [s for s in subs if isinstance(s, dict)]
    return []


def unsubscribe(
    url: str,
    *,
    token: str | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """DELETE /subscriptions?url=… — drop webhook subscription."""
    endpoint = (url or "").strip()
    if not endpoint.startswith("https://"):
        raise MaxApiError("url must be https://")
    http = session or requests
    try:
        resp = http.delete(
            f"{API_BASE}/subscriptions",
            headers=_headers(token),
            params={"url": endpoint},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise MaxApiError(str(exc)) from exc
    data = _parse_body(resp)
    _raise_if_failed(resp, data)
    return data
