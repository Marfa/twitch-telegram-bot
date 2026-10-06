"""Post-VOD peaks → Helix Create Clip From VOD.

Sources (priority): chat phrase clip/клип → spoken phrase (Groq Whisper on
short windows) → message-volume spikes → audio RMS.
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

logger = logging.getLogger(__name__)

MAX_CLIPS = 5
CLIP_DURATION_SEC = 30
MIN_GAP_SEC = 60
CHAT_PHRASE_DEDUP_SEC = 300
MSG_SPIKE_BUCKET_SEC = 30
MAX_VOD_ANALYZE_SEC = 4 * 3600  # hard cap for VPS RAM/time
# Groq free tier: short windows around existing peaks only (not full VOD).
ASR_WINDOW_SEC = 20
ASR_MAX_WINDOWS = 6
ASR_MIN_GAP_SEC = 45
_GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
_GROQ_MODEL = "whisper-large-v3-turbo"
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
_GQL_REQUEST_DELAY = 0.05
_GQL_WORKERS = 4
CHAT_SCAN_MAX_SEC = 8 * 60  # wall-clock cap so long VODs do not look hung

ClipSource = Literal["phrase", "asr", "spike", "audio"]


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
    comment_id: str = ""


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
    asr: list[ClipCandidate] | None = None,
    n: int = MAX_CLIPS,
    duration: int = CLIP_DURATION_SEC,
    min_gap: int = MIN_GAP_SEC,
) -> list[ClipCandidate]:
    """Priority: phrase → asr → spike → audio; within tier by time; gap on start."""
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
        sorted(asr or [], key=lambda c: c.vod_offset),
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
    vod_id: str,
    *,
    max_seconds: int = MAX_VOD_ANALYZE_SEC,
    progress: Callable[[int, int], None] | None = None,
) -> list[ChatMessage]:
    """Scan VOD chat via GQL time offsets. Empty list on failure (caller falls back).

    No durable files — HTTP only. Wall-clock capped by ``CHAT_SCAN_MAX_SEC``.
    """
    vid = str(vod_id or "").strip()
    if not vid.isdigit():
        return []
    max_sec = max(0, min(int(max_seconds), MAX_VOD_ANALYZE_SEC))
    if max_sec <= 0:
        return []
    try:
        return _fetch_vod_chat_gql(vid, max_sec, progress=progress)
    except Exception:
        logger.warning("ai_clips chat GQL failed vod=%s", vid, exc_info=True)
        return []


def _gql_headers() -> dict[str, str]:
    return {
        "Client-ID": _GQL_WEB_CLIENT_ID,
        "Content-Type": "application/json",
        "User-Agent": "twitch-telegram-bot/ai-clips",
        "X-Device-Id": str(uuid.uuid4()),
    }


def _fetch_comment_page(vod_id: str, offset: float, headers: dict[str, str]) -> list[ChatMessage]:
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
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read()
    data = json.loads(body)
    row = data[0] if isinstance(data, list) else data
    if not isinstance(row, dict):
        return []
    if row.get("errors"):
        logger.warning(
            "ai_clips GQL errors vod=%s offset=%s err=%s",
            vod_id,
            offset,
            row.get("errors"),
        )
        return []
    video = ((row.get("data") or {}).get("video")) or {}
    edges = ((video.get("comments") or {}).get("edges")) or []
    out: list[ChatMessage] = []
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        node = edge.get("node") or {}
        if not isinstance(node, dict):
            continue
        t_off = node.get("contentOffsetSeconds")
        try:
            sec = float(t_off)
        except (TypeError, ValueError):
            continue
        msg = node.get("message") or {}
        fragments = msg.get("fragments") if isinstance(msg, dict) else None
        text = _fragments_text(fragments)
        if not text:
            continue
        cid = str(node.get("id") or "").strip()
        out.append(ChatMessage(offset_sec=sec, text=text, comment_id=cid))
    time.sleep(_GQL_REQUEST_DELAY)
    return out


def _fetch_vod_chat_gql(
    vod_id: str,
    max_seconds: int,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> list[ChatMessage]:
    headers = _gql_headers()
    offsets = list(range(0, max_seconds + 1, _GQL_STEP_SEC))
    total = len(offsets)
    deadline = time.monotonic() + CHAT_SCAN_MAX_SEC
    seen: set[str] = set()
    out: list[ChatMessage] = []
    done = 0

    def _one(offset: int) -> list[ChatMessage]:
        if time.monotonic() > deadline:
            return []
        try:
            return _fetch_comment_page(vod_id, float(offset), headers)
        except Exception:
            logger.debug(
                "ai_clips GQL page failed vod=%s offset=%s", vod_id, offset, exc_info=True
            )
            return []

    workers = min(_GQL_WORKERS, max(1, total))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_one, off): off for off in offsets}
        for fut in as_completed(futures):
            done += 1
            if progress and done % 20 == 0:
                try:
                    progress(done, total)
                except Exception:
                    pass
            if time.monotonic() > deadline:
                logger.warning(
                    "ai_clips chat scan deadline vod=%s done=%s/%s",
                    vod_id,
                    done,
                    total,
                )
                for pending in futures:
                    pending.cancel()
                break
            for msg in fut.result():
                key = msg.comment_id or f"{msg.offset_sec}:{msg.text}"
                if key in seen:
                    continue
                seen.add(key)
                if msg.offset_sec <= max_seconds:
                    out.append(msg)
    if progress:
        try:
            progress(min(done, total), total)
        except Exception:
            pass
    out.sort(key=lambda m: m.offset_sec)
    logger.info(
        "ai_clips chat scan vod=%s messages=%s offsets=%s/%s",
        vod_id,
        len(out),
        done,
        total,
    )
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


def groq_api_key() -> str:
    return (os.getenv("GROQ_API_KEY") or "").strip()


def groq_asr_ready() -> bool:
    """Groq Whisper + local streamlink/ffmpeg for short window extract."""
    return bool(groq_api_key() and ai_clips_ready())


def find_asr_phrase_peaks(
    vod_id: str,
    seeds: list[ClipCandidate] | list[LoudPeak],
    *,
    max_windows: int = ASR_MAX_WINDOWS,
    window_sec: int = ASR_WINDOW_SEC,
    clip_duration: int = CLIP_DURATION_SEC,
    min_gap: int = ASR_MIN_GAP_SEC,
) -> list[ClipCandidate]:
    """Transcribe short windows around ``seeds``; keep those with spoken clip/клип.

    No-op when Groq is not configured. Temp audio is deleted after each window.
    """
    if not groq_asr_ready() or max_windows <= 0 or window_sec <= 0:
        return []
    vid = str(vod_id or "").strip()
    if not vid.isdigit():
        return []
    normalized: list[ClipCandidate] = []
    for s in seeds:
        if isinstance(s, ClipCandidate):
            normalized.append(s)
        else:
            normalized.append(
                ClipCandidate(vod_offset=s.vod_offset, score=s.score, source="audio")
            )
    # Prefer spike/audio seeds (chat phrase already covered); fill with phrases.
    preferred = [c for c in normalized if c.source in ("spike", "audio")]
    fillers = [c for c in normalized if c.source == "phrase"]
    ordered = sorted(preferred, key=lambda c: c.vod_offset) + sorted(
        fillers, key=lambda c: c.vod_offset
    )
    windows: list[int] = []
    for cand in ordered:
        if len(windows) >= max_windows:
            break
        # Helix offset is clip end; center the ASR window on the clip body.
        center = max(0, int(cand.vod_offset) - clip_duration // 2)
        start = max(0, center - window_sec // 2)
        if any(abs(start - w) < min_gap for w in windows):
            continue
        windows.append(start)

    out: list[ClipCandidate] = []
    for start in windows:
        try:
            text = _transcribe_vod_window(vid, start, window_sec)
        except Exception:
            logger.warning(
                "ai_clips groq window failed vod=%s start=%s",
                vid,
                start,
                exc_info=True,
            )
            continue
        if not text or not CLIP_PHRASE_RE.search(text):
            continue
        end = max(clip_duration, start + window_sec)
        out.append(
            ClipCandidate(vod_offset=end, score=float(end), source="asr")
        )
    logger.info(
        "ai_clips groq asr vod=%s windows=%s hits=%s",
        vid,
        len(windows),
        len(out),
    )
    return out


def _transcribe_vod_window(vod_id: str, start_sec: int, window_sec: int) -> str:
    """Extract ``window_sec`` of audio and send to Groq Whisper. Deletes temps."""
    work = Path(tempfile.mkdtemp(prefix=f"ai_clips_asr_{vod_id}_"))
    try:
        audio_path = _extract_vod_audio_window(vod_id, work, start_sec, window_sec)
        if audio_path is None or not audio_path.is_file():
            return ""
        return _groq_transcribe(audio_path)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _extract_vod_audio_window(
    vod_id: str,
    work: Path,
    start_sec: int,
    window_sec: int,
) -> Path | None:
    sl = shutil.which("streamlink")
    ff = shutil.which("ffmpeg")
    if not sl or not ff:
        return None
    out = work / "window.mp3"
    url = f"https://www.twitch.tv/videos/{vod_id}"
    env = os.environ.copy()
    env["TMPDIR"] = str(work)
    env["TMP"] = str(work)
    env["TEMP"] = str(work)
    env["STREAMLINK_CONFIG_DIR"] = str(work / "sl_config")
    (work / "sl_config").mkdir(parents=True, exist_ok=True)
    start = max(0, int(start_sec))
    dur = max(5, min(60, int(window_sec)))
    sl_cmd = [
        sl,
        "--stdout",
        "--twitch-disable-ads",
        "--hls-start-offset",
        str(start),
        "--hls-duration",
        str(dur),
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
        "-t",
        str(dur),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        "64k",
        str(out),
    ]
    sl_proc: subprocess.Popen[bytes] | None = None
    ff_proc: subprocess.Popen[bytes] | None = None
    try:
        sl_proc = subprocess.Popen(
            sl_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
            cwd=str(work),
        )
        ff_proc = subprocess.Popen(
            ff_cmd,
            stdin=sl_proc.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=env,
            cwd=str(work),
        )
        if sl_proc.stdout:
            sl_proc.stdout.close()
        ff_proc.communicate(timeout=max(60, dur + 45))
        if out.is_file() and out.stat().st_size > 256:
            return out
        return None
    except Exception:
        logger.warning(
            "ai_clips extract window failed vod=%s start=%s",
            vod_id,
            start,
            exc_info=True,
        )
        return None
    finally:
        _kill_proc(ff_proc)
        _kill_proc(sl_proc)


def _groq_transcribe(audio_path: Path) -> str:
    key = groq_api_key()
    if not key:
        return ""
    import requests

    with audio_path.open("rb") as fh:
        resp = requests.post(
            _GROQ_STT_URL,
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (audio_path.name, fh, "audio/mpeg")},
            data={
                "model": _GROQ_MODEL,
                "response_format": "json",
                "temperature": "0",
            },
            timeout=120,
        )
    if resp.status_code >= 400:
        logger.warning(
            "ai_clips groq HTTP %s body=%s",
            resp.status_code,
            (resp.text or "")[:200],
        )
        resp.raise_for_status()
    payload = resp.json() if resp.content else {}
    if isinstance(payload, dict):
        return str(payload.get("text") or "").strip()
    return str(payload or "").strip()


def analyze_vod_rms(
    vod_id: str,
    *,
    max_seconds: int = MAX_VOD_ANALYZE_SEC,
    progress: Callable[[int, int], None] | None = None,
) -> list[float]:
    """Stream audio_only → mono PCM → per-second RMS.

    All child temp files go under a TemporaryDirectory (TMPDIR) and are removed
    in ``finally`` on success or error. No durable media left behind.
    """
    vid = str(vod_id or "").strip()
    if not vid.isdigit():
        return []
    if not ai_clips_ready():
        raise RuntimeError("ai_clips_tools_missing")

    work = Path(tempfile.mkdtemp(prefix=f"ai_clips_{vid}_"))
    try:
        return _analyze_vod_rms_in_dir(
            vid, work, max_seconds=max_seconds, progress=progress
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _analyze_vod_rms_in_dir(
    vod_id: str,
    work: Path,
    *,
    max_seconds: int,
    progress: Callable[[int, int], None] | None = None,
) -> list[float]:
    sl = shutil.which("streamlink")
    ff = shutil.which("ffmpeg")
    assert sl and ff
    url = f"https://www.twitch.tv/videos/{vod_id}"
    # Isolate streamlink/ffmpeg scratch under ``work`` so finally wipes everything.
    env = os.environ.copy()
    env["TMPDIR"] = str(work)
    env["TMP"] = str(work)
    env["TEMP"] = str(work)
    env["STREAMLINK_CONFIG_DIR"] = str(work / "sl_config")
    (work / "sl_config").mkdir(parents=True, exist_ok=True)

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
            env=env,
            cwd=str(work),
        )
        ff_proc = subprocess.Popen(
            ff_cmd,
            stdin=sl_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=env,
            cwd=str(work),
        )
        if sl_proc.stdout:
            sl_proc.stdout.close()
        assert ff_proc.stdout is not None
        rms: list[float] = []
        buf = b""
        last_prog = 0
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
            if progress and len(rms) - last_prog >= 120:
                last_prog = len(rms)
                try:
                    progress(len(rms), max_seconds)
                except Exception:
                    pass
        if progress:
            try:
                progress(len(rms), max_seconds)
            except Exception:
                pass
        return rms
    finally:
        _kill_proc(ff_proc)
        _kill_proc(sl_proc)
        # Second pass after kill — streamlink may drop late scratch files.
        for leftover in work.iterdir():
            try:
                if leftover.is_dir():
                    shutil.rmtree(leftover, ignore_errors=True)
                else:
                    leftover.unlink(missing_ok=True)
            except Exception:
                pass


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
