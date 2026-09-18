"""Capture ~30s of a live Twitch stream to a local MP4 for video preview.

Uses streamlink + ffmpeg (record-forward from live HLS). Not Helix Create Clip.
ponytail: Twitch ToS / unofficial HLS; upgrade path = Helix Create Clip if Delete Clip
appears, or an official short preview API.

Connection lifecycle (must not "sit" on the stream):
- Prefer streamlink --stdout (ad-filtered) piped into ffmpeg for ~duration, then exit.
- Fallback: streamlink --stream-url + ffmpeg -c:v copy (low RAM on 512MiB VPS).
- Kill process groups on timeout/failure so no orphan keeps the CDN session open.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from config import DATABASE_PATH

logger = logging.getLogger(__name__)

DEFAULT_DURATION_SEC = 30.0
# Reuse one capture across many alerts for the same streamer (go-live fan-out).
SHARED_TTL_SEC = 180.0
_QUALITY = "360p,480p,worst"
_LOGIN_RE = re.compile(r"^[a-zA-Z0-9_]{4,25}$")
_LOCK = threading.Lock()
# path_str -> twitch_user_id
_PENDING: dict[str, str] = {}
# twitch_user_id -> shared capture (reused until TTL / invalidate / purge)
_SHARED: dict[str, "_SharedEntry"] = {}
# Reject near-static / truncated captures (Telegram shows them as a frozen GIF).
_MIN_DURATION_SEC = 4.0
_MIN_PACKETS = 40
# Telegram animations stay snappy under ~6–8MB; larger files often fail editMessageMedia.
_MAX_BYTES = 6_000_000


@dataclass(frozen=True)
class _SharedEntry:
    path: Path
    data: bytes
    mono: float


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
    force: bool = False,
) -> CapturedPreview | None:
    """Record ~duration seconds of live stream; one shared file per streamer (TTL).

    force=True bypasses the shared cache (periodic refresh wants a fresh clip).
    """
    if not video_preview_ready():
        return None
    user = str(login or "").strip().lstrip("@").lower()
    if not _LOGIN_RE.match(user):
        logger.warning("Invalid Twitch login for stream capture: %r", login)
        return None
    uid = str(twitch_user_id or "").strip()
    if not uid:
        return None
    if not force:
        hit = _shared_get(uid)
        if hit is not None:
            return CapturedPreview(
                path=hit.path, data=hit.data, twitch_user_id=uid
            )
    dur = max(5.0, min(60.0, float(duration)))
    out_dir = preview_dir()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.exception("Cannot create stream preview dir %s", out_dir)
        return None
    out_path = out_dir / f"preview_{user}_{int(time.time())}_{os.getpid()}.mp4"
    ok = _record_streamlink_pipe(user, out_path, duration=dur)
    if not ok:
        _safe_unlink(out_path)
        ok = _record_hls_copy(user, out_path, duration=dur)
    if not ok:
        _safe_unlink(out_path)
        return None
    if not _mp4_looks_animated(out_path):
        logger.warning(
            "Rejecting static/short stream preview login=%s path=%s",
            user,
            out_path.name,
        )
        _safe_unlink(out_path)
        return None
    try:
        size = out_path.stat().st_size
    except OSError:
        size = 0
    if size > _MAX_BYTES:
        logger.info(
            "Preview too large (%s bytes), re-encoding lighter login=%s",
            size,
            user,
        )
        light = out_dir / f"preview_{user}_{int(time.time())}_{os.getpid()}_lite.mp4"
        if not _ffmpeg_reencode_light(out_path, light):
            _safe_unlink(out_path)
            _safe_unlink(light)
            return None
        _safe_unlink(out_path)
        out_path = light
        if not _mp4_looks_animated(out_path):
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
    _shared_put(uid, out_path, data)
    return CapturedPreview(path=out_path, data=data, twitch_user_id=uid)


def register_pending(path: Path | str, twitch_user_id: str) -> None:
    key = str(Path(path).resolve())
    uid = str(twitch_user_id or "").strip()
    with _LOCK:
        _PENDING[key] = uid


def forget_and_unlink(path: Path | str | None) -> None:
    """Unlink unless the path is still the active shared capture for a streamer."""
    if not path:
        return
    key = str(Path(path).resolve())
    with _LOCK:
        for entry in _SHARED.values():
            if str(entry.path.resolve()) == key:
                return
        _PENDING.pop(key, None)
    _safe_unlink(Path(key))


def invalidate_shared(twitch_user_id: str) -> None:
    """Drop shared cache for a streamer (next capture is forced fresh)."""
    uid = str(twitch_user_id or "").strip()
    if not uid:
        return
    with _LOCK:
        old = _SHARED.pop(uid, None)
        if old is not None:
            _PENDING.pop(str(old.path.resolve()), None)
    if old is not None:
        _safe_unlink(old.path)


def purge_for_streamer(twitch_user_id: str) -> int:
    """Force-delete pending + shared preview files for a streamer (stream ended)."""
    uid = str(twitch_user_id or "").strip()
    if not uid:
        return 0
    with _LOCK:
        shared = _SHARED.pop(uid, None)
        victims = [p for p, owner in _PENDING.items() if owner == uid]
        for p in victims:
            _PENDING.pop(p, None)
        if shared is not None:
            sp = str(shared.path.resolve())
            if sp not in victims:
                victims.append(sp)
    n = 0
    for p in victims:
        if _safe_unlink(Path(p)):
            n += 1
    return n


def purge_stale_on_startup() -> int:
    """Clear registry/cache and delete leftover MP4s in the preview dir."""
    with _LOCK:
        _PENDING.clear()
        _SHARED.clear()
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


def _shared_get(uid: str) -> _SharedEntry | None:
    now = time.monotonic()
    with _LOCK:
        hit = _SHARED.get(uid)
        if hit is None:
            return None
        if (now - hit.mono) > SHARED_TTL_SEC or not hit.path.is_file():
            _SHARED.pop(uid, None)
            _PENDING.pop(str(hit.path.resolve()), None)
            stale = hit
        else:
            return hit
    _safe_unlink(stale.path)
    return None


def _shared_put(uid: str, path: Path, data: bytes) -> None:
    with _LOCK:
        old = _SHARED.pop(uid, None)
        _SHARED[uid] = _SharedEntry(
            path=path, data=data, mono=time.monotonic()
        )
        if old is not None and str(old.path.resolve()) != str(path.resolve()):
            _PENDING.pop(str(old.path.resolve()), None)
            prev = old
        else:
            prev = None
    if prev is not None:
        _safe_unlink(prev.path)


def _record_streamlink_pipe(login: str, out_path: Path, *, duration: float) -> bool:
    """Ad-filtered live pipe → MP4 (stream copy, low RAM)."""
    sl = shutil.which("streamlink")
    ff = shutil.which("ffmpeg")
    if not sl or not ff:
        return False
    sl_cmd = [
        sl,
        "--stdout",
        "--twitch-disable-ads",
        f"https://www.twitch.tv/{login}",
        _QUALITY,
    ]
    ff_cmd = _ffmpeg_copy_cmd(out_path, duration=duration, input_arg="pipe:0")
    timeout = float(duration) + 90.0
    sl_proc: subprocess.Popen[bytes] | None = None
    ff_proc: subprocess.Popen[bytes] | None = None
    try:
        sl_proc = subprocess.Popen(
            sl_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        ff_proc = subprocess.Popen(
            ff_cmd,
            stdin=sl_proc.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        if sl_proc.stdout is not None:
            sl_proc.stdout.close()
        try:
            _ff_err = ff_proc.communicate(timeout=timeout)[1]
        except subprocess.TimeoutExpired:
            _kill_process_group(ff_proc)
            _kill_process_group(sl_proc)
            try:
                ff_proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            logger.warning("streamlink|ffmpeg pipe timed out login=%s", login)
            return False
        _kill_process_group(sl_proc)
        try:
            sl_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _kill_process_group(sl_proc)
        if ff_proc.returncode != 0 or not out_path.is_file() or out_path.stat().st_size <= 0:
            err = (_ff_err or b"")[:400].decode("utf-8", errors="replace")
            logger.warning(
                "streamlink|ffmpeg pipe failed login=%s code=%s: %s",
                login,
                ff_proc.returncode,
                err,
            )
            return False
        return True
    except OSError as exc:
        logger.warning("streamlink|ffmpeg pipe error login=%s: %s", login, exc)
        return False
    finally:
        if ff_proc is not None and ff_proc.poll() is None:
            _kill_process_group(ff_proc)
        if sl_proc is not None and sl_proc.poll() is None:
            _kill_process_group(sl_proc)


def _record_hls_copy(login: str, out_path: Path, *, duration: float) -> bool:
    """Fallback: resolve HLS URL, stream-copy without re-encode."""
    hls_url = _streamlink_url(login)
    if not hls_url:
        return False
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-rw_timeout",
        "20000000",
        "-http_persistent",
        "0",
        "-i",
        hls_url,
        "-t",
        f"{duration:.1f}",
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-ac",
        "2",
        "-b:a",
        "96k",
        "-movflags",
        "+faststart",
        str(out_path),
    ]
    timeout = float(duration) + 90.0
    try:
        proc = _run_killable(cmd, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("ffmpeg HLS copy failed login=%s: %s", login, exc)
        return False
    if proc.returncode != 0 or not out_path.is_file() or out_path.stat().st_size <= 0:
        err = (proc.stderr or "")[:400]
        logger.warning(
            "ffmpeg HLS copy failed login=%s code=%s: %s",
            login,
            proc.returncode,
            err,
        )
        return False
    return True


def _ffmpeg_copy_cmd(
    out_path: Path, *, duration: float, input_arg: str
) -> list[str]:
    """H.264(+AAC) MP4 via stream copy — low RAM on 512–768MiB containers."""
    return [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        input_arg,
        "-t",
        f"{duration:.1f}",
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-ac",
        "2",
        "-b:a",
        "96k",
        "-movflags",
        "+faststart",
        str(out_path),
    ]


def _ffmpeg_reencode_light(src: Path, dest: Path) -> bool:
    """Low-RAM H.264+AAC re-encode when the stream-copy file is too large."""
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-profile:v",
        "baseline",
        "-level",
        "3.0",
        "-preset",
        "ultrafast",
        "-threads",
        "1",
        "-crf",
        "32",
        "-maxrate",
        "500k",
        "-bufsize",
        "1000k",
        "-vf",
        "fps=15,scale=480:-2",
        "-c:a",
        "aac",
        "-ac",
        "2",
        "-b:a",
        "96k",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    try:
        proc = _run_killable(cmd, timeout=120.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("ffmpeg light re-encode failed: %s", exc)
        return False
    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size <= 0:
        err = (proc.stderr or "")[:400]
        logger.warning("ffmpeg light re-encode failed code=%s: %s", proc.returncode, err)
        return False
    return True


def _streamlink_url(login: str) -> str | None:
    """Resolve HLS URL only — streamlink must exit immediately (no player / no pipe)."""
    cmd = [
        "streamlink",
        "--stream-url",
        "--twitch-disable-ads",
        f"https://www.twitch.tv/{login}",
        _QUALITY,
    ]
    try:
        proc = _run_killable(cmd, timeout=45.0)
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


def _mp4_looks_animated(path: Path) -> bool:
    """True when the file has enough duration/packets to play as a Telegram GIF."""
    if not shutil.which("ffprobe"):
        # No probe — accept non-empty file (Docker image includes ffprobe via ffmpeg).
        try:
            return path.is_file() and path.stat().st_size > 50_000
        except OSError:
            return False
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-count_packets",
        "-show_entries",
        "stream=nb_read_packets,duration",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0 or not proc.stdout:
        return False
    import json

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return False
    duration = 0.0
    packets = 0
    for stream in data.get("streams") or []:
        try:
            packets = max(packets, int(stream.get("nb_read_packets") or 0))
        except (TypeError, ValueError):
            pass
        try:
            duration = max(duration, float(stream.get("duration") or 0))
        except (TypeError, ValueError):
            pass
    fmt = data.get("format") or {}
    try:
        duration = max(duration, float(fmt.get("duration") or 0))
    except (TypeError, ValueError):
        pass
    ok = duration >= _MIN_DURATION_SEC and packets >= _MIN_PACKETS
    if not ok:
        logger.info(
            "Preview probe weak duration=%.2f packets=%s file=%s",
            duration,
            packets,
            path.name,
        )
    return ok


def _run_killable(
    cmd: list[str], *, timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess; on timeout/error kill the whole process group (no orphans)."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            stdout, stderr = proc.communicate(timeout=5)
        raise
    except Exception:
        _kill_process_group(proc)
        raise
    return subprocess.CompletedProcess(
        cmd, int(proc.returncode or 0), stdout or "", stderr or ""
    )


def _kill_process_group(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _safe_unlink(path: Path) -> bool:
    try:
        if path.is_file():
            path.unlink()
            return True
    except OSError:
        logger.warning("Failed to delete stream preview file %s", path, exc_info=True)
    return False
