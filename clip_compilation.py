"""Build a top-clips compilation MP4 for a broadcaster (own channel only).

Helix list → download clip MP4s → ffmpeg concat → durable file under
``/data/clip_compilations/{token}.mp4``. Not a twitchtube fork (no moviepy /
YouTube). Caps: ~10 min of source clips, ~200 MB output.
"""
from __future__ import annotations

import logging
import re
import secrets
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal

logger = logging.getLogger(__name__)

PeriodKey = Literal["24h", "7d", "30d"]

PERIOD_HOURS: dict[PeriodKey, int] = {
    "24h": 24,
    "7d": 24 * 7,
    "30d": 24 * 30,
}

TARGET_DURATION_SEC = 10 * 60
MAX_OUTPUT_BYTES = 200 * 1024 * 1024
RETENTION_DAYS = 3
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
_PREVIEW_MARK = "-preview-"


@dataclass(frozen=True)
class SelectedClip:
    clip_id: str
    url: str
    title: str
    duration: float
    view_count: int
    thumbnail_url: str


@dataclass(frozen=True)
class CompilationResult:
    token: str
    path: Path
    clip_count: int
    duration_sec: float
    expires_at: datetime


def clip_compilation_ready() -> bool:
    return bool(shutil.which("ffmpeg"))


def compilations_root() -> Path:
    from config import DATABASE_PATH

    return Path(DATABASE_PATH).resolve().parent / "clip_compilations"


def period_window(period: PeriodKey, *, now: datetime | None = None) -> tuple[datetime, datetime]:
    end = now or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    hours = PERIOD_HOURS.get(period)
    if hours is None:
        raise ValueError(f"unknown period: {period}")
    return end - timedelta(hours=hours), end


def parse_period(raw: str) -> PeriodKey | None:
    key = str(raw or "").strip().lower()
    if key in PERIOD_HOURS:
        return key  # type: ignore[return-value]
    return None


def clip_mp4_url(thumbnail_url: str) -> str | None:
    thumb = str(thumbnail_url or "").strip()
    if _PREVIEW_MARK not in thumb:
        return None
    return thumb[: thumb.index(_PREVIEW_MARK)] + ".mp4"


def select_clips_by_views(
    rows: list[dict[str, Any]],
    *,
    max_duration_sec: float = TARGET_DURATION_SEC,
) -> list[SelectedClip]:
    """Sort by view_count desc and take until duration budget is filled."""
    ranked: list[SelectedClip] = []
    for row in rows:
        cid = str(row.get("id") or "").strip()
        thumb = str(row.get("thumbnail_url") or "").strip()
        if not cid or not clip_mp4_url(thumb):
            continue
        try:
            duration = float(row.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        if duration <= 0:
            continue
        try:
            views = int(row.get("view_count") or 0)
        except (TypeError, ValueError):
            views = 0
        ranked.append(
            SelectedClip(
                clip_id=cid,
                url=str(row.get("url") or "").strip(),
                title=str(row.get("title") or cid).strip() or cid,
                duration=duration,
                view_count=max(0, views),
                thumbnail_url=thumb,
            )
        )
    ranked.sort(key=lambda c: (-c.view_count, c.clip_id))
    out: list[SelectedClip] = []
    total = 0.0
    for clip in ranked:
        if total >= max_duration_sec:
            break
        out.append(clip)
        total += clip.duration
    return out


def new_compilation_token() -> str:
    # urlsafe ~32 chars; TOKEN_RE requires >= 20
    return secrets.token_urlsafe(24)


def public_compilation_url(token: str) -> str:
    from config import PUBLIC_BASE_URL

    base = (PUBLIC_BASE_URL or "").rstrip("/")
    tok = str(token or "").strip()
    if not base or not TOKEN_RE.match(tok):
        return ""
    return f"{base}/media/compilations/{tok}.mp4"


def compilation_file_path(token: str) -> Path:
    tok = str(token or "").strip()
    if not TOKEN_RE.match(tok):
        raise ValueError("invalid compilation token")
    return compilations_root() / f"{tok}.mp4"


def download_clip_mp4(thumbnail_url: str, dest: Path) -> int:
    mp4 = clip_mp4_url(thumbnail_url)
    if not mp4:
        raise ValueError("no mp4 url from thumbnail")
    dest.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(mp4, str(dest))
    return int(dest.stat().st_size) if dest.is_file() else 0


def _ffmpeg_concat(paths: list[Path], out: Path, *, reencode: bool) -> None:
    ff = shutil.which("ffmpeg")
    if not ff:
        raise RuntimeError("ffmpeg missing")
    list_path = out.parent / "concat.txt"
    lines = []
    for p in paths:
        # ffmpeg concat demuxer: escape single quotes
        safe = str(p.resolve()).replace("'", "'\\''")
        lines.append(f"file '{safe}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cmd = [
        ff,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_path),
    ]
    if reencode:
        cmd.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "28",
                "-vf",
                "scale=-2:720",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
            ]
        )
    else:
        cmd.extend(["-c", "copy"])
    cmd.append(str(out))
    proc = subprocess.run(
        cmd,
        capture_output=True,
        timeout=30 * 60,
        check=False,
    )
    if proc.returncode != 0 or not out.is_file() or out.stat().st_size <= 0:
        err = (proc.stderr or b"").decode("utf-8", errors="replace")[-800:]
        raise RuntimeError(f"ffmpeg concat failed: {err}")


def build_compilation(
    clips: list[SelectedClip],
    *,
    token: str,
    work_dir: Path,
    progress: Callable[[int], None] | None = None,
) -> CompilationResult:
    """Download + concat; returns durable path under compilations_root."""
    if not clips:
        raise ValueError("no clips")
    if not TOKEN_RE.match(token):
        raise ValueError("invalid token")
    if not clip_compilation_ready():
        raise RuntimeError("ffmpeg missing")

    work_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[Path] = []
    bytes_total = 0
    duration_total = 0.0
    n = len(clips)
    for i, clip in enumerate(clips):
        if progress:
            # 5..70 while downloading
            progress(max(5, min(70, 5 + int(i * 65 / max(1, n)))))
        dest = work_dir / f"{i:03d}_{clip.clip_id}.mp4"
        try:
            size = download_clip_mp4(clip.thumbnail_url, dest)
        except Exception:
            logger.warning(
                "clip_compilation download failed id=%s", clip.clip_id, exc_info=True
            )
            continue
        if size <= 0:
            continue
        if bytes_total + size > MAX_OUTPUT_BYTES and downloaded:
            dest.unlink(missing_ok=True)
            break
        downloaded.append(dest)
        bytes_total += size
        duration_total += clip.duration
        if bytes_total >= MAX_OUTPUT_BYTES:
            break

    if not downloaded:
        raise RuntimeError("download failed")

    if progress:
        progress(75)
    root = compilations_root()
    root.mkdir(parents=True, exist_ok=True)
    out = compilation_file_path(token)
    tmp_out = work_dir / "out.mp4"
    try:
        _ffmpeg_concat(downloaded, tmp_out, reencode=False)
    except Exception:
        logger.info("clip_compilation copy concat failed; re-encoding")
        _ffmpeg_concat(downloaded, tmp_out, reencode=True)

    if tmp_out.stat().st_size > MAX_OUTPUT_BYTES:
        # Second pass: force lighter re-encode if copy produced a huge file.
        light = work_dir / "out_light.mp4"
        _ffmpeg_concat(downloaded, light, reencode=True)
        if light.stat().st_size <= MAX_OUTPUT_BYTES or light.stat().st_size < tmp_out.stat().st_size:
            tmp_out = light
        if tmp_out.stat().st_size > MAX_OUTPUT_BYTES:
            raise RuntimeError("output too large")

    if progress:
        progress(95)
    shutil.move(str(tmp_out), str(out))
    expires = datetime.now(timezone.utc) + timedelta(days=RETENTION_DAYS)
    if progress:
        progress(100)
    return CompilationResult(
        token=token,
        path=out,
        clip_count=len(downloaded),
        duration_sec=duration_total,
        expires_at=expires,
    )


def safe_unlink_compilation(token: str) -> None:
    try:
        path = compilation_file_path(token)
    except ValueError:
        return
    path.unlink(missing_ok=True)
