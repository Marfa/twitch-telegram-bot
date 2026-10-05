"""Post-VOD loudness peaks → Helix Create Clip From VOD.

Downloads no durable media: streamlink audio_only is piped through ffmpeg to
PCM, RMS is scored in memory, then temp dirs (if any) are removed in finally.
ponytail: streamlink/Twitch HLS is the same unofficial path as stream_capture;
upgrade = official media API if Twitch ever ships one.
"""
from __future__ import annotations

import logging
import math
import os
import re
import shutil
import signal
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_CLIPS = 5
CLIP_DURATION_SEC = 30
MIN_GAP_SEC = 60
MAX_VOD_ANALYZE_SEC = 4 * 3600  # hard cap for VPS RAM/time
_SAMPLE_RATE = 8000
_BYTES_PER_SEC = _SAMPLE_RATE * 2  # s16le mono
_DURATION_RE = re.compile(
    r"^(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?(?:(?P<s>\d+)s)?$"
)


@dataclass(frozen=True)
class LoudPeak:
    """vod_offset is the Helix end second of the clip."""

    vod_offset: int
    score: float


@dataclass(frozen=True)
class CreatedClip:
    clip_id: str
    edit_url: str
    vod_offset: int
    url: str


def parse_helix_duration(raw: str | None) -> int:
    """Parse Helix video duration like ``1h2m3s`` → seconds."""
    text = (raw or "").strip().lower()
    if not text:
        return 0
    m = _DURATION_RE.fullmatch(text)
    if not m:
        return 0
    h = int(m.group("h") or 0)
    mins = int(m.group("m") or 0)
    secs = int(m.group("s") or 0)
    return h * 3600 + mins * 60 + secs


def find_loud_peaks(
    rms_per_sec: list[float],
    *,
    n: int = MAX_CLIPS,
    duration: int = CLIP_DURATION_SEC,
    min_gap: int = MIN_GAP_SEC,
) -> list[LoudPeak]:
    """Pick up to ``n`` non-overlapping loud windows; return end offsets."""
    if n <= 0 or duration <= 0 or len(rms_per_sec) < duration:
        return []
    scored: list[tuple[float, int]] = []
    for end in range(duration, len(rms_per_sec) + 1):
        window = rms_per_sec[end - duration : end]
        mean = sum(window) / duration
        peak = max(window)
        scored.append((mean + 0.5 * peak, end))
    scored.sort(key=lambda x: x[0], reverse=True)
    picked: list[LoudPeak] = []
    for score, end in scored:
        start = end - duration
        if any(abs(start - (p.vod_offset - duration)) < min_gap for p in picked):
            continue
        picked.append(LoudPeak(vod_offset=end, score=score))
        if len(picked) >= n:
            break
    return sorted(picked, key=lambda p: p.vod_offset)


def ai_clips_ready() -> bool:
    return bool(shutil.which("streamlink") and shutil.which("ffmpeg"))


def analyze_vod_rms(vod_id: str, *, max_seconds: int = MAX_VOD_ANALYZE_SEC) -> list[float]:
    """Stream audio_only → mono PCM → per-second RMS. No durable files left behind."""
    vid = str(vod_id or "").strip()
    if not vid.isdigit():
        return []
    if not ai_clips_ready():
        raise RuntimeError("ai_clips_tools_missing")

    work = Path(tempfile.mkdtemp(prefix=f"ai_clips_{vid}_"))
    try:
        return _analyze_vod_rms_in_dir(vid, work, max_seconds=max_seconds)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _analyze_vod_rms_in_dir(
    vod_id: str, work: Path, *, max_seconds: int
) -> list[float]:
    import time

    sl = shutil.which("streamlink")
    ff = shutil.which("ffmpeg")
    assert sl and ff
    url = f"https://www.twitch.tv/videos/{vod_id}"
    (work / ".keep").write_text("1", encoding="utf-8")

    sl_cmd = [
        sl,
        "--stdout",
        "--twitch-disable-ads",
        "--retry-max",
        "2",
        url,
        "audio_only,worst",
    ]
    ff_cmd = [
        ff,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(_SAMPLE_RATE),
        "-f",
        "s16le",
        "pipe:1",
    ]
    deadline = time.monotonic() + max(120, min(max_seconds, MAX_VOD_ANALYZE_SEC) + 300)
    sl_proc: subprocess.Popen[bytes] | None = None
    ff_proc: subprocess.Popen[bytes] | None = None
    try:
        sl_proc = subprocess.Popen(
            sl_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        ff_proc = subprocess.Popen(
            ff_cmd,
            stdin=sl_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        if sl_proc.stdout:
            sl_proc.stdout.close()
        assert ff_proc.stdout is not None
        rms: list[float] = []
        buf = b""
        while len(rms) < max_seconds:
            if time.monotonic() > deadline:
                logger.warning("ai_clips analyze timed out vod=%s", vod_id)
                break
            chunk = ff_proc.stdout.read(_BYTES_PER_SEC)
            if not chunk:
                break
            buf += chunk
            while len(buf) >= _BYTES_PER_SEC and len(rms) < max_seconds:
                frame = buf[:_BYTES_PER_SEC]
                buf = buf[_BYTES_PER_SEC:]
                rms.append(_rms_s16le(frame))
        return rms
    finally:
        _kill_proc(ff_proc)
        _kill_proc(sl_proc)


def _rms_s16le(frame: bytes) -> float:
    n = len(frame) // 2
    if n <= 0:
        return 0.0
    samples = struct.unpack(f"<{n}h", frame[: n * 2])
    acc = 0.0
    for s in samples:
        v = float(s)
        acc += v * v
    return math.sqrt(acc / n)


def _kill_proc(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None:
        return
    try:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=2)
    except Exception:
        pass


def clip_watch_url(clip_id: str) -> str:
    cid = (clip_id or "").strip()
    return f"https://clips.twitch.tv/{cid}" if cid else ""
