"""CloudConvert: remote MP4 → GIF without storing files on our disk."""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

from config import CLOUDCONVERT_API_KEY

logger = logging.getLogger(__name__)

_API = "https://api.cloudconvert.com/v2"
# Keep GIFs small enough for Telegram animations.
_GIF_WIDTH = 480
_GIF_FPS = 10
_JOB_WAIT_SEC = 120.0
_POLL_SEC = 2.0


def cloudconvert_configured() -> bool:
    return bool(CLOUDCONVERT_API_KEY)


def mp4_url_to_gif_bytes(mp4_url: str) -> bytes | None:
    """Import MP4 URL → convert to GIF → download export bytes (temp on CloudConvert only)."""
    url = str(mp4_url or "").strip()
    if not url or not CLOUDCONVERT_API_KEY:
        return None
    headers = {
        "Authorization": f"Bearer {CLOUDCONVERT_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "tasks": {
            "import-1": {"operation": "import/url", "url": url},
            "convert-1": {
                "operation": "convert",
                "input": "import-1",
                "output_format": "gif",
                "width": _GIF_WIDTH,
                "fit": "scale",
                "video_fps": _GIF_FPS,
            },
            "export-1": {
                "operation": "export/url",
                "input": "convert-1",
                "inline": False,
                "archive_multiple_files": False,
            },
        },
        "tag": "stream-video-preview",
    }
    try:
        resp = requests.post(
            f"{_API}/jobs", headers=headers, json=payload, timeout=30
        )
        resp.raise_for_status()
        job = (resp.json() or {}).get("data") or {}
        job_id = str(job.get("id") or "").strip()
        if not job_id:
            return None
        export_url = _wait_export_url(job_id, headers)
        if not export_url:
            return None
        gif = requests.get(export_url, timeout=60)
        gif.raise_for_status()
        data = gif.content
        return data or None
    except Exception:
        logger.exception("CloudConvert MP4→GIF failed")
        return None


def _wait_export_url(job_id: str, headers: dict[str, str]) -> str | None:
    deadline = time.time() + _JOB_WAIT_SEC
    while time.time() < deadline:
        resp = requests.get(f"{_API}/jobs/{job_id}", headers=headers, timeout=20)
        resp.raise_for_status()
        job: dict[str, Any] = (resp.json() or {}).get("data") or {}
        status = str(job.get("status") or "")
        if status == "error":
            logger.warning("CloudConvert job error: %s", job.get("message"))
            return None
        if status == "finished":
            for task in job.get("tasks") or []:
                if task.get("operation") != "export/url":
                    continue
                files = ((task.get("result") or {}).get("files")) or []
                if files:
                    url = str(files[0].get("url") or "").strip()
                    if url:
                        return url
            return None
        time.sleep(_POLL_SEC)
    logger.warning("CloudConvert job timed out: %s", job_id)
    return None
