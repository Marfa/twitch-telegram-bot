"""MAX Messenger Bot API client (subscriptions webhook).

Docs: https://dev.max.ru/docs-api/methods/POST/subscriptions/
Base: https://platform-api2.max.ru

TLS: platform-api2.max.ru uses Russian Trusted CA (Mintsifry). Standard
certifi alone fails; we append ``certs/russian_trusted_root_ca.pem``.

Never log the access token or webhook secret.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunsplit

import certifi
import requests

logger = logging.getLogger(__name__)

API_BASE = "https://platform-api2.max.ru"
_SECRET_RE = re.compile(r"^[a-zA-Z0-9_-]{5,256}$")
_RU_ROOT = Path(__file__).resolve().parent / "certs" / "russian_trusted_root_ca.pem"
_verify_path: str | None = None
_default_session: requests.Session | None = None


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


def _tls_verify() -> str | bool:
    """certifi + Russian Trusted Root (Max / Mintsifry)."""
    global _verify_path
    if _verify_path and Path(_verify_path).is_file():
        return _verify_path
    if not _RU_ROOT.is_file():
        logger.warning("Missing %s — Max TLS may fail", _RU_ROOT.name)
        return True
    combined = Path(certifi.where()).read_text(encoding="utf-8")
    combined += "\n" + _RU_ROOT.read_text(encoding="utf-8")
    fd, path = tempfile.mkstemp(prefix="max_ca_", suffix=".pem")
    os.close(fd)
    Path(path).write_text(combined, encoding="utf-8")
    _verify_path = path
    return path


def _http(session: requests.Session | None = None) -> requests.Session:
    global _default_session
    if session is not None:
        return session
    if _default_session is None:
        _default_session = requests.Session()
    return _default_session


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


def get_me(
    *,
    token: str | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """GET /me — bot identity."""
    http = _http(session)
    try:
        resp = http.get(
            f"{API_BASE}/me",
            headers=_headers(token),
            timeout=30,
            verify=_tls_verify(),
        )
    except requests.RequestException as exc:
        raise MaxApiError(str(exc)) from exc
    data = _parse_body(resp)
    _raise_if_failed(resp, data)
    return data


def set_my_commands(
    commands: list[dict[str, str]],
    *,
    token: str | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """PATCH /me/commands — replace slash-command hints (max 32).

    Empty ``commands`` clears the list. Each item needs ``name`` (1–64) and
    optional ``description`` (1–128). Shown when the user types ``/`` in a
    dialog (iOS/Android/desktop) or group (iOS/Android); not in channels.
    """
    body: dict[str, Any] = {
        "commands": [
            {
                "name": str(c.get("name") or "").strip()[:64],
                **(
                    {"description": str(c["description"]).strip()[:128]}
                    if (c.get("description") or "").strip()
                    else {}
                ),
            }
            for c in commands
            if str(c.get("name") or "").strip()
        ]
    }
    http = _http(session)
    try:
        resp = http.patch(
            f"{API_BASE}/me/commands",
            headers=_headers(token),
            json=body,
            timeout=30,
            verify=_tls_verify(),
        )
    except requests.RequestException as exc:
        raise MaxApiError(str(exc)) from exc
    data = _parse_body(resp)
    _raise_if_failed(resp, data)
    return data


def is_image_upload_error(exc: BaseException) -> bool:
    """True when Max rejected an image attachment (do not pause destination)."""
    if not isinstance(exc, MaxApiError):
        return False
    msg = str(exc).lower()
    code = str(exc.code or "").lower()
    return any(
        token in msg or token in code
        for token in (
            "failed to upload image",
            "upload image",
            "attachment.not.ready",
            "process.attachment",
            "errors.process.attachment",
        )
    )


def _extract_upload_token(data: dict[str, Any]) -> str | None:
    """Pull media token from POST /uploads or multipart upload responses."""
    direct = str(data.get("token") or "").strip()
    if direct:
        return direct
    photos = data.get("photos")
    if isinstance(photos, dict):
        for entry in photos.values():
            if isinstance(entry, dict):
                tok = str(entry.get("token") or "").strip()
                if tok:
                    return tok
            elif isinstance(entry, str) and entry.strip():
                return entry.strip()
    return None


def _unlink_quiet(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Failed to delete temp Max image %s: %s", path, exc)


# Only image CDNs the bot itself produces may be fetched. A user-editable URL
# must never reach internal services (CodeQL py/full-ssrf).
_IMAGE_HOSTS = (
    "static-cdn.jtvnw.net",
    "clips-media-assets2.twitch.tv",
    "images.igdb.com",
)


def _trusted_image_url(raw: str) -> str | None:
    """Canonical https image URL on a known CDN, or None when not allowed."""
    text = (raw or "").strip()
    if not text or len(text) > 2048:
        return None
    try:
        parts = urlparse(text)
    except ValueError:
        return None
    if parts.scheme.lower() != "https" or parts.username or parts.password:
        return None
    if parts.port not in (None, 443):
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    if not any(
        host == allowed or host.endswith("." + allowed) for allowed in _IMAGE_HOSTS
    ):
        return None
    return urlunsplit(("https", host, parts.path or "/", parts.query, ""))


def _download_image_to_temp(url: str, *, session: requests.Session | None = None) -> Path:
    """Download image to a temp file. Caller must delete the path (use finally)."""
    href = _trusted_image_url(url)
    if href is None:
        raise MaxApiError("image url not allowed", code="proto.payload")
    http = _http(session)
    try:
        resp = http.get(
            href, timeout=30, verify=True, stream=True, allow_redirects=False
        )
    except requests.RequestException as exc:
        raise MaxApiError(str(exc)) from exc
    path: Path | None = None
    try:
        if resp.is_redirect:
            raise MaxApiError(
                f"image download redirect http {resp.status_code}",
                status_code=resp.status_code,
            )
        if resp.status_code >= 400:
            raise MaxApiError(
                f"image download http {resp.status_code}",
                status_code=resp.status_code,
            )
        ctype = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
        if not ctype.startswith("image/"):
            ctype = "image/jpeg"
        ext = {
            "image/png": "png",
            "image/gif": "gif",
            "image/webp": "webp",
            "image/jpeg": "jpg",
            "image/jpg": "jpg",
        }.get(ctype.lower(), "jpg")
        fd, name = tempfile.mkstemp(prefix="max_alert_img_", suffix=f".{ext}")
        path = Path(name)
        with os.fdopen(fd, "wb") as out:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if chunk:
                    out.write(chunk)
        if path.stat().st_size <= 0:
            raise MaxApiError("empty image download")
        return path
    except Exception:
        _unlink_quiet(path)
        raise
    finally:
        resp.close()


def upload_image_file(
    path: Path | str,
    *,
    content_type: str = "image/jpeg",
    token: str | None = None,
    session: requests.Session | None = None,
) -> str:
    """POST /uploads + multipart from a local file; return Max attachment token."""
    file_path = Path(path)
    if not file_path.is_file() or file_path.stat().st_size <= 0:
        raise MaxApiError("empty image", code="proto.payload")
    http = _http(session)
    auth = token or _access_token()
    try:
        resp = http.post(
            f"{API_BASE}/uploads",
            headers={"Authorization": auth},
            params={"type": "image"},
            timeout=30,
            verify=_tls_verify(),
        )
    except requests.RequestException as exc:
        raise MaxApiError(str(exc)) from exc
    meta = _parse_body(resp)
    _raise_if_failed(resp, meta)
    early = _extract_upload_token(meta)
    upload_url = str(meta.get("url") or "").strip()
    if early and not upload_url:
        return early
    if not upload_url:
        raise MaxApiError(
            "uploads: missing url",
            status_code=resp.status_code,
            code=str(meta.get("code") or "") or None,
        )
    name = file_path.name[:200] or "image.jpg"
    ctype = content_type or "image/jpeg"
    try:
        with file_path.open("rb") as fh:
            up = http.post(
                upload_url,
                headers={"Authorization": auth},
                files={"data": (name, fh, ctype)},
                timeout=60,
                verify=_tls_verify(),
            )
    except requests.RequestException as exc:
        raise MaxApiError(str(exc)) from exc
    body = _parse_body(up)
    _raise_if_failed(up, body)
    out = _extract_upload_token(body) or early
    if not out:
        raise MaxApiError(
            "uploads: missing token",
            status_code=up.status_code,
            code=str(body.get("code") or "") or None,
        )
    return out


def upload_image_from_url(
    url: str,
    *,
    token: str | None = None,
    session: requests.Session | None = None,
) -> str:
    """Download ``url`` to a temp file, upload to Max, always delete the temp file.

    Temp file is removed on success and on any error after download started.
    """
    href = (url or "").strip()
    path: Path | None = None
    try:
        path = _download_image_to_temp(href, session=session)
        suffix = path.suffix.lower().lstrip(".")
        ctype = {
            "png": "image/png",
            "gif": "image/gif",
            "webp": "image/webp",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
        }.get(suffix, "image/jpeg")
        return upload_image_file(
            path, content_type=ctype, token=token, session=session
        )
    finally:
        _unlink_quiet(path)


def send_message(
    *,
    text: str,
    user_id: int | None = None,
    chat_id: int | None = None,
    attachments: list[dict[str, Any]] | None = None,
    disable_link_preview: bool | None = None,
    token: str | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """POST /messages — send to a user (DM) or chat/channel."""
    if user_id is None and chat_id is None:
        raise MaxApiError("user_id or chat_id required", code="proto.payload")
    if user_id is not None and chat_id is not None:
        raise MaxApiError(
            "pass only one of user_id or chat_id",
            code="proto.payload",
        )
    params: dict[str, Any] = {}
    if user_id is not None:
        params["user_id"] = int(user_id)
    if chat_id is not None:
        params["chat_id"] = int(chat_id)
    if disable_link_preview is not None:
        params["disable_link_preview"] = bool(disable_link_preview)

    body: dict[str, Any] = {"text": text if text is not None else ""}
    if attachments is not None:
        body["attachments"] = list(attachments)

    http = _http(session)
    try:
        resp = http.post(
            f"{API_BASE}/messages",
            headers=_headers(token),
            params=params,
            json=body,
            timeout=30,
            verify=_tls_verify(),
        )
    except requests.RequestException as exc:
        raise MaxApiError(str(exc)) from exc
    data = _parse_body(resp)
    _raise_if_failed(resp, data)
    return data


def answer_callback(
    callback_id: str,
    *,
    message: dict[str, Any] | None = None,
    notification: str | None = None,
    token: str | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """POST /answers — react to ``message_callback`` (edit message and/or toast)."""
    cid = (callback_id or "").strip()
    if not cid:
        raise MaxApiError("callback_id required", code="proto.payload")
    body: dict[str, Any] = {}
    if message is not None:
        body["message"] = message
    if notification is not None:
        body["notification"] = notification
    if not body:
        raise MaxApiError(
            "message or notification required",
            code="proto.payload",
        )
    http = _http(session)
    try:
        resp = http.post(
            f"{API_BASE}/answers",
            headers=_headers(token),
            params={"callback_id": cid},
            json=body,
            timeout=30,
            verify=_tls_verify(),
        )
    except requests.RequestException as exc:
        raise MaxApiError(str(exc)) from exc
    data = _parse_body(resp)
    _raise_if_failed(resp, data)
    return data


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

    http = _http(session)
    try:
        resp = http.post(
            f"{API_BASE}/subscriptions",
            headers=_headers(token),
            json=body,
            timeout=30,
            verify=_tls_verify(),
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
    http = _http(session)
    try:
        resp = http.get(
            f"{API_BASE}/subscriptions",
            headers=_headers(token),
            timeout=30,
            verify=_tls_verify(),
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
    http = _http(session)
    try:
        resp = http.delete(
            f"{API_BASE}/subscriptions",
            headers=_headers(token),
            params={"url": endpoint},
            timeout=30,
            verify=_tls_verify(),
        )
    except requests.RequestException as exc:
        raise MaxApiError(str(exc)) from exc
    data = _parse_body(resp)
    _raise_if_failed(resp, data)
    return data


def get_chat_membership(
    chat_id: int,
    *,
    token: str | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """GET /chats/{chatId}/members/me — bot membership + admin permissions."""
    cid = int(chat_id)
    http = _http(session)
    try:
        resp = http.get(
            f"{API_BASE}/chats/{cid}/members/me",
            headers=_headers(token),
            timeout=30,
            verify=_tls_verify(),
        )
    except requests.RequestException as exc:
        raise MaxApiError(str(exc)) from exc
    data = _parse_body(resp)
    _raise_if_failed(resp, data)
    return data


def bot_can_post(membership: dict[str, Any] | None, *, is_channel: bool) -> bool:
    """Whether the bot may send messages into this chat/channel."""
    if not isinstance(membership, dict):
        return False
    if not is_channel:
        # Group: membership is enough to POST /messages for a normal participant.
        return True
    if membership.get("is_owner") or membership.get("is_admin"):
        perms = membership.get("permissions")
        if not isinstance(perms, list) or not perms:
            # Owner/admin with no permissions list — allow write.
            return True
        names = {str(p).strip() for p in perms if p is not None}
        return bool(
            names
            & {
                "write",
                "post_edit_delete_message",
                "edit",
                "edit_message",
            }
        )
    return False
