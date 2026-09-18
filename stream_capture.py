"""Capture ~30s of a live Twitch stream to a local MP4 for video preview.

Uses streamlink + ffmpeg (record-forward from live HLS). Not Helix Create Clip.
ponytail: Twitch ToS / unofficial HLS; upgrade path = Helix Create Clip if Delete Clip
appears, or an official short preview API.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from config import DATABASE_PATH

logger = logging.getLogger(__name__)

DEFAULT_DURATION_SEC = 30.0
_QUALITY = "360p,480p,worst,best"
_LOGIN_RE = re.compile(r"^[a-zA-Z0-9_]{4,25}$")
_PENDING_LOCK = threading.Lock()
# path_str -> twitch_user_id
_PENDING: dict[str, str] = {}


def preview_dir() -> Path:
    """Temp MP4s live next to the DB under /data/stream_preview on VPS."""
    return Path(DATABASE_PATH).resolve().parent / "stream_preview"


def video_preview_ready() -> bool:
    """True when streamlink and ffmpeg are on PATH."""
    return bool(shutil.which("streamlink") and shutil.which("ffmpeg"))


@dataclass(frozen=True)
class CapturedPreview:
    path: Path
    data: bytes
    twitch_user_id: str


def capture_live_preview_mp4(
    login: str,
    *,
    twitch_user_id: str,
    duration: float = DEFAULT_DURATION_SEC,
) -> CapturedPreview | None:
    """Record ~duration seconds of live stream; register file until unlink/purge."""
    if not video_preview_ready():
        return None
    user = str(login or "").strip().lstrip("@").lower()
    if not _LOGIN_RE.match(user):
        logger.warning("Invalid Twitch login for stream capture: %r", login)
        return None
    uid = str(twitch_user_id or "").strip()
    if not uid:
        return None
    dur = max(5.0, min(60.0, float(duration)))
    out_dir = preview_dir()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.exception("Cannot create stream preview dir %s", out_dir)
        return None
    out_path = out_dir / f"preview_{user}_{int(time.time())}_{os.getpid()}.mp4"
    hls_url = _streamlink_url(user)
    if not hls_url:
        return None
    if not _ffmpeg_record(hls_url, out_path, duration=dur):
        _safe_unlink(out_path)
        return None
    try:
        data = out_path.read_bytes()
    except OSError:
        logger.exception("Failed to read captured preview %s", out_path)
        _safe_unlink(out_path)
        return None
    if not data:
        _safe_unlink(out_path)
        return None
    register_pending(out_path, uid)
    return CapturedPreview(path=out_path, data=data, twitch_user_id=uid)


def register_pending(path: Path | str, twitch_user_id: str) -> None:
    key = str(Path(path).resolve())
    uid = str(twitch_user_id or "").strip()
    with _PENDING_LOCK:
        _PENDING[key] = uid


def forget_and_unlink(path: Path | str | None) -> None:
    if not path:
        return
    key = str(Path(path).resolve())
    with _PENDING_LOCK:
        _PENDING.pop(key, None)
    _safe_unlink(Path(key))


def purge_for_streamer(twitch_user_id: str) -> int:
    """Force-delete pending preview files for a streamer (e.g. stream ended)."""
    uid = str(twitch_user_id or "").strip()
    if not uid:
        return 0
    with _PENDING_LOCK:
        victims = [p for p, owner in _PENDING.items() if owner == uid]
        for p in victims:
            _PENDING.pop(p, None)
    n = 0
    for p in victims:
        if _safe_unlink(Path(p)):
            n += 1
    return n


def purge_stale_on_startup() -> int:
    """Clear registry and delete leftover MP4s in the preview dir (crash / restart)."""
    with _PENDING_LOCK:
        _PENDING.clear()
    out_dir = preview_dir()
    if not out_dir.is_dir():
        return 0
    n = 0
    try:
        for path in out_dir.glob("preview_*.mp4"):
            if _safe_unlink(path):
                n += 1
    except OSError:
        logger.exception("Failed scanning stream preview dir %s", out_dir)
    if n:
        logger.info("Purged %s stale stream preview file(s) on startup", n)
    return n


def _streamlink_url(login: str) -> str | None:
    cmd = [
        "streamlink",
        "--stream-url",
        f"https://www.twitch.tv/{login}",
        _QUALITY,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("streamlink failed for %s: %s", login, exc)
        return None
    url = (proc.stdout or "").strip().splitlines()
    url = url[-1].strip() if url else ""
    if proc.returncode != 0 or not url.startswith("http"):
        err = ((proc.stderr or "") + (proc.stdout or ""))[:400]
        logger.warning(
            "streamlink --stream-url failed login=%s code=%s: %s",
            login,
            proc.returncode,
            err,
        )
        return None
    return url


def _ffmpeg_record(hls_url: str, out_path: Path, *, duration: float) -> bool:
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        hls_url,
        "-t",
        f"{duration:.1f}",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(out_path),
    ]
    timeout = float(duration) + 45.0
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("ffmpeg record failed: %s", exc)
        return False
    if proc.returncode != 0 or not out_path.is_file() or out_path.stat().st_size <= 0:
        err = (proc.stderr or "")[:400]
        logger.warning("ffmpeg record failed code=%s: %s", proc.returncode, err)
        return False
    return True


def _safe_unlink(path: Path) -> bool:
    try:
        if path.is_file():
            path.unlink()
            return True
    except OSError:
        logger.warning("Failed to delete stream preview file %s", path, exc_info=True)
    return False
