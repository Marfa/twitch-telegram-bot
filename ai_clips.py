"""Post-VOD peaks → Helix Create Clip From VOD.

Sources (priority): chat phrase clip/клип → message-volume spikes → audio RMS.
Downloads no durable media: streamlink audio_only is piped through ffmpeg to
PCM, RMS is scored in memory, then temp dirs (if any) are removed in finally.
ponytail: streamlink/Twitch HLS is the same unofficial path as stream_capture;
upgrade = official media API if Twitch ever ships one.

Gray area: VOD chat uses unofficial gql.twitch.tv VideoCommentsByOffsetOrCursor
(time-offset scan only; no cursor). Scoped exception in api-license-compliance.
Upgrade = official Helix VOD chat if Twitch ships one.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import signal
import struct
import subprocess
import tempfile
import time
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

MAX_CLIPS = 5
CLIP_DURATION_SEC = 30
MIN_GAP_SEC = 60
CHAT_PHRASE_DEDUP_SEC = 300
MSG_SPIKE_BUCKET_SEC = 30
MAX_VOD_ANALYZE_SEC = 4 * 3600  # hard cap for VPS RAM/time
_SAMPLE_RATE = 8000
_BYTES_PER_SEC = _SAMPLE_RATE * 2  # s16le mono
_DURATION_RE = re.compile(
    r"^(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?(?:(?P<s>\d+)s)?$"
)
CLIP_PHRASE_RE = re.compile(r"(?i)clip|клип")

# Twitch web Client-ID required for persisted VideoComments query (partner Helix
# Client-ID does not serve this GQL). Read-only; not used for Helix auth.
_GQL_WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
_GQL_URL = "https://gql.twitch.tv/gql"
_GQL_COMMENTS_HASH = (
    "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"
)
_GQL_STEP_SEC = 30
_GQL_REQUEST_DELAY = 0.12

ClipSource = Literal["phrase", "spike", "audio"]


@dataclass(frozen=True)
class LoudPeak:
    """vod_offset is the Helix end second of the clip."""

    vod_offset: int
    score: float


@dataclass(frozen=True)
class ClipCandidate:
    """vod_offset is the Helix end second of the clip."""

    vod_offset: int
    score: float
    source: ClipSource


@dataclass(frozen=True)
class CreatedClip:
    clip_id: str
    edit_url: str
    vod_offset: int
    url: str


@dataclass(frozen=True)
class ChatMessage:
    offset_sec: float
    text: str


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


def find_phrase_peaks(
    messages: list[ChatMessage],
    *,
    duration: int = CLIP_DURATION_SEC,
    dedup_sec: int = CHAT_PHRASE_DEDUP_SEC,
) -> list[ClipCandidate]:
    """First clip/клип substring hit per dedup window; clip ends at the phrase."""
    if duration <= 0:
        return []
    hits: list[ClipCandidate] = []
    last_keep = -dedup_sec
    for msg in sorted(messages, key=lambda m: m.offset_sec):
        if not CLIP_PHRASE_RE.search(msg.text or ""):
            continue
        offset = int(msg.offset_sec)
        if offset < duration:
            continue
        if offset - last_keep < dedup_sec:
            continue
        hits.append(
            ClipCandidate(vod_offset=offset, score=float(offset), source="phrase")
        )
        last_keep = offset
    return hits


def find_message_spikes(
    messages: list[ChatMessage],
    *,
    n: int = MAX_CLIPS,
    duration: int = CLIP_DURATION_SEC,
    min_gap: int = MIN_GAP_SEC,
    bucket_sec: int = MSG_SPIKE_BUCKET_SEC,
) -> list[ClipCandidate]:
    """Multiplicative chat-volume spikes; clip ends at bucket end."""
    if n <= 0 or duration <= 0 or bucket_sec <= 0 or not messages:
        return []
    counts: dict[int, int] = {}
    for msg in messages:
        b = int(msg.offset_sec) // bucket_sec
        counts[b] = counts.get(b, 0) + 1
    nonzero = [c for c in counts.values() if c > 0]
    if not nonzero:
        return []
    sorted_nz = sorted(nonzero)
    median = sorted_nz[len(sorted_nz) // 2]
    threshold = max(4, int(2 * median))
    scored: list[tuple[float, int]] = []
    for bucket, count in counts.items():
        if count < threshold:
            continue
        end = (bucket + 1) * bucket_sec
        if end < duration:
            continue
        scored.append((float(count), end))
    scored.sort(key=lambda x: (-x[0], x[1]))
    picked: list[ClipCandidate] = []
    for score, end in scored:
        start = end - duration
        if any(abs(start - (p.vod_offset - duration)) < min_gap for p in picked):
            continue
        picked.append(ClipCandidate(vod_offset=end, score=score, source="spike"))
        if len(picked) >= n:
            break
    return sorted(picked, key=lambda p: p.vod_offset)


def merge_clip_candidates(
    phrases: list[ClipCandidate],
    spikes: list[ClipCandidate],
    audio: list[LoudPeak] | list[ClipCandidate],
    *,
    n: int = MAX_CLIPS,
    duration: int = CLIP_DURATION_SEC,
    min_gap: int = MIN_GAP_SEC,
) -> list[ClipCandidate]:
    """Priority: phrase → spike → audio; within tier by time; gap on clip start."""
    if n <= 0:
        return []
    audio_cands: list[ClipCandidate] = []
    for a in audio:
        if isinstance(a, ClipCandidate):
            audio_cands.append(a)
        else:
            audio_cands.append(
                ClipCandidate(vod_offset=a.vod_offset, score=a.score, source="audio")
            )
    tiers = (
        sorted(phrases, key=lambda c: c.vod_offset),
        sorted(spikes, key=lambda c: c.vod_offset),
        sorted(audio_cands, key=lambda c: c.vod_offset),
    )
    picked: list[ClipCandidate] = []
    for tier in tiers:
        for cand in tier:
            if len(picked) >= n:
                break
            start = cand.vod_offset - duration
            if any(abs(start - (p.vod_offset - duration)) < min_gap for p in picked):
                continue
            picked.append(cand)
        if len(picked) >= n:
            break
    return sorted(picked, key=lambda c: c.vod_offset)


def fetch_vod_chat_messages(
    vod_id: str, *, max_seconds: int = MAX_VOD_ANALYZE_SEC
) -> list[ChatMessage]:
    """Scan VOD chat via GQL time offsets. Empty list on failure (caller falls back)."""
    vid = str(vod_id or "").strip()
    if not vid.isdigit():
        return []
    max_sec = max(0, min(int(max_seconds), MAX_VOD_ANALYZE_SEC))
    if max_sec <= 0:
        return []
    try:
        return _fetch_vod_chat_gql(vid, max_sec)
    except Exception:
        logger.warning("ai_clips chat GQL failed vod=%s", vid, exc_info=True)
        return []


def _fetch_vod_chat_gql(vod_id: str, max_seconds: int) -> list[ChatMessage]:
    device_id = str(uuid.uuid4())
    headers = {
        "Client-ID": _GQL_WEB_CLIENT_ID,
        "Content-Type": "application/json",
        "User-Agent": "twitch-telegram-bot/ai-clips",
        "X-Device-Id": device_id,
    }
    seen: set[str] = set()
    out: list[ChatMessage] = []
    for offset in range(0, max_seconds + 1, _GQL_STEP_SEC):
        payload = [
            {
                "operationName": "VideoCommentsByOffsetOrCursor",
                "variables": {
                    "videoID": vod_id,
                    "contentOffsetSeconds": float(offset),
                },
                "extensions": {
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": _GQL_COMMENTS_HASH,
                    }
                },
            }
        ]
        req = urllib.request.Request(
            _GQL_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read()
        data = json.loads(body)
        row = data[0] if isinstance(data, list) else data
        if not isinstance(row, dict):
            continue
        if row.get("errors"):
            logger.warning(
                "ai_clips GQL errors vod=%s offset=%s err=%s",
                vod_id,
                offset,
                row.get("errors"),
            )
            break
        video = ((row.get("data") or {}).get("video")) or {}
        edges = ((video.get("comments") or {}).get("edges")) or []
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            node = edge.get("node") or {}
            if not isinstance(node, dict):
                continue
            cid = str(node.get("id") or "").strip()
            if cid and cid in seen:
                continue
            if cid:
                seen.add(cid)
            t_off = node.get("contentOffsetSeconds")
            try:
                sec = float(t_off)
            except (TypeError, ValueError):
                continue
            if sec > max_seconds:
                continue
            msg = node.get("message") or {}
            fragments = msg.get("fragments") if isinstance(msg, dict) else None
            text = _fragments_text(fragments)
            if not text:
                continue
            out.append(ChatMessage(offset_sec=sec, text=text))
        time.sleep(_GQL_REQUEST_DELAY)
    out.sort(key=lambda m: m.offset_sec)
    return out


def _fragments_text(fragments: Any) -> str:
    if not isinstance(fragments, list):
        return ""
    parts: list[str] = []
    for frag in fragments:
        if isinstance(frag, dict):
            parts.append(str(frag.get("text") or ""))
    return "".join(parts)


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


def format_clip_timecode(seconds: int) -> str:
    """Human VOD position as ``H:MM:SS`` (hours + minutes + seconds)."""
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"
