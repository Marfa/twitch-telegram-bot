"""Post-VOD peaks → Helix Create Clip From VOD.

Sources (priority): chat phrase clip/клип → Groq emotion (ASR+score>3) →
game (lexicon/audio heuristics, then Groq chat context) → remaining audio RMS →
remaining chat spikes.
Chat spikes: unique chatters vs rolling baseline, chat-lag shift, reaction
boost (hype/funny/…). Audio: RMS + silence→explosion + burst density.
Top-10 audio peaks get Whisper + emotion LLM; chat spikes get lexicon boost
and/or context LLM. Nearby game events collapse into one clip window.
Merged finalists get one Groq head-to-head rerank when available.
If Groq is down/rate-limited, lexicon heuristics still promote game spikes;
HTTP 429 sets a sticky 24h cooldown before the next Groq attempt.
Downloads no durable media: streamlink audio_only is piped through ffmpeg to
PCM, RMS is scored in memory, then temp dirs (if any) are removed in finally.
ponytail: no OCR/HUD — chat keywords + loudness only; upgrade = per-game
vision if we ever need kill-feed precision.

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
# Chat reacts after the beat (stream delay + typing).
CHAT_LAG_SEC = 6
MSG_SPIKE_BASELINE_SEC = 45
MSG_SPIKE_RATIO = 3.0
MSG_SPIKE_MIN_UNIQUE = 4
MSG_SPIKE_PLAIN_RATIO = 4.0
MSG_SPIKE_PLAIN_MIN_UNIQUE = 6
MSG_SPIKE_REACTION_WEIGHT = 0.5
MAX_VOD_ANALYZE_SEC = 4 * 3600  # hard cap for VPS RAM/time
# Groq free tier: top audio/spike seeds only (not full VOD).
AUDIO_SEED_N = 10
SPIKE_SEED_N = 10
ASR_WINDOW_SEC = 20
EMOTION_MIN_SCORE = 3  # candidate when score > this (4–5)
CHAT_CONTEXT_WINDOW_SEC = 45
# Nearby game beats → one Helix window (end after last event).
SERIES_GAP_SEC = 20
SERIES_POST_SEC = 4
AUDIO_NEAR_SEC = 45  # chat spike ↔ loud peak alignment
_GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
_GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
_GROQ_WHISPER_MODEL = "whisper-large-v3-turbo"
# qwen returns plain content; gpt-oss often parks the answer in reasoning.
_GROQ_CHAT_MODEL = "qwen/qwen3.8-27b"
_SAMPLE_RATE = 8000
_BYTES_PER_SEC = _SAMPLE_RATE * 2  # s16le mono
_DURATION_RE = re.compile(
    r"^(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?(?:(?P<s>\d+)s)?$"
)
CLIP_PHRASE_RE = re.compile(r"(?i)clip|клип")
_EMOTION_SCORE_RE = re.compile(r"[1-5]")
_RERANK_ORDER_RE = re.compile(r"\d+")
# Compact EN/RU reaction tokens (own list — not a copy of third-party configs).
_REACTION_WORD_CLASSES: dict[str, frozenset[str]] = {
    "hype": frozenset(
        {
            "pog",
            "poggers",
            "pogchamp",
            "pogu",
            "letsgo",
            "letsgooo",
            "insane",
            "clutch",
            "holy",
            "omg",
            "wow",
            "ez",
            "gg",
            "gigachad",
            "fire",
            "goat",
            "nice",
            "sheesh",
            "вау",
            "ого",
            "жестко",
            "имба",
            "красава",
            "топ",
        }
    ),
    "funny": frozenset(
        {
            "lul",
            "lulw",
            "kekw",
            "omegalul",
            "lol",
            "lmao",
            "xd",
            "icant",
            "hahaha",
            "ахах",
            "ахаха",
            "лол",
            "кек",
        }
    ),
    "surprise": frozenset(
        {
            "huh",
            "what",
            "wtf",
            "wait",
            "wat",
            "чего",
            "whaat",
        }
    ),
    "scare": frozenset(
        {
            "monkas",
            "monkaw",
            "wutface",
            "jumpscare",
            "aaaa",
            "scary",
        }
    ),
    "fail": frozenset(
        {
            "sadge",
            "rip",
            "nooo",
            "threw",
            "copium",
            "feelsbadman",
            "notlikethis",
        }
    ),
    "clip_ask": frozenset(
        {
            "clipit",
            "clipped",
            "clipping",
            "клипни",
            "клипайте",
            "запишите",
        }
    ),
}
_REACTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("funny", re.compile(r"(?i)^[wｗ]{2,}$|хаха|ахах|^лол+$")),
    ("hype", re.compile(r"(?i)^6{3,}$|пог+|имба")),
    ("surprise", re.compile(r"^[?？!]{2,}$")),
    ("fail", re.compile(r"(?i)^no{3,}$|^ф+$")),
    ("clip_ask", re.compile(r"(?i)clip\s*(it|that|this)|клип\s*(это|пожалуйста)?")),
)
_REACTION_TOKEN_RE = re.compile(r"[\w'+]+", re.UNICODE)
# Chat lexicon for gameplay beats (EN/RU); not emote-only spam.
GAME_KEYWORD_RE = re.compile(
    r"(?i)(?<!\w)(?:"
    r"kill(?:ed|s|ing)?|clutch(?:ed|es|ing)?|ace|headshot|hs|"
    r"knock(?:ed|s)?|nuke|1v[1-5]|booyah|winner\s*winner|"
    r"килл(?:ы|ов|а)?|убил(?:а|и)?|хедшот|клатч|эйс|"
    r"нок(?:аут)?|сбил(?:а|и)?|нокаут"
    r")(?!\w)"
)
# Substrings of Helix game_name / category (lowercase).
_ACTION_CATEGORY_SUBSTR = (
    "pubg",
    "battlegrounds",
    "counter-strike",
    "cs2",
    "cs:go",
    "valorant",
    "apex legends",
    "call of duty",
    "warzone",
    "fortnite",
    "escape from tarkov",
    "tarkov",
    "dota 2",
    "league of legends",
    "overwatch",
    "rainbow six",
    "r6",
    "teamfight tactics",
    "dead by daylight",
    "rust",
    "dayz",
    "hunt: showdown",
    "marvel rivals",
)

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

# Emotion: ASR text → 1–5; clip candidate only when score > EMOTION_MIN_SCORE.
GROQ_EMOTION_PROMPT = """You score emotional intensity of Twitch stream speech for highlight clips.
Integer 1-5 only:
1 = flat, calm, boring, silence filler
2 = mild interest, soft reaction
3 = moderate energy, ordinary stream talk
4 = strong excitement, laughter, shock, hype, tilt, celebration
5 = peak highlight emotion worth clipping

Reply with ONLY one digit 1-5.

Text:
{text}"""

# Chat spike: gameplay beat vs social chatter.
# Category/title help disambiguate (Helix VOD has no game_id — caller passes
# channel last/current category + VOD title).
GROQ_CONTEXT_PROMPT = """Classify a Twitch chat burst. Reply with ONLY one word:
game — reactions to gameplay (fight, clutch, kill, knock, ace, boss, win/lose, skill play, in-game event)
chat — social talk, greetings, memes, emotes-only, off-topic, no clear game beat

Prefer game when chat mentions kills/clutches/knocks/aces or clear play-by-play.
Prefer chat when messages are greetings, donation thanks, or emotes with no play context.

Category: {category}
Stream title: {title}

Chat:
{text}"""

# Relative order of finalists — small models rank peers better than absolute scores.
GROQ_RERANK_PROMPT = """Rank these Twitch VOD clip candidates best-first for a highlight reel.
Reply with ONLY a comma-separated list of their numbers (each once), e.g. 3,1,2,4.

Category: {category}
Stream title: {title}

Candidates:
{candidates}"""

ClipSource = Literal["phrase", "emotion", "game", "audio", "spike"]


class GroqUnavailable(RuntimeError):
    """Rate limit / outage — caller should skip remaining Groq work."""


# After Groq HTTP 429, skip Groq for a day (then probe on next job).
_GROQ_RATE_LIMIT_STICKY_SEC = 24 * 3600
_groq_rate_limited_at: float | None = None
_groq_rate_limit_sticky_sec: float = float(_GROQ_RATE_LIMIT_STICKY_SEC)


def groq_rate_limited() -> bool:
    """True while sticky cooldown after Groq 429 is still active."""
    global _groq_rate_limited_at, _groq_rate_limit_sticky_sec
    if _groq_rate_limited_at is None:
        return False
    if time.monotonic() - _groq_rate_limited_at >= _groq_rate_limit_sticky_sec:
        _groq_rate_limited_at = None
        logger.info("Groq rate-limit sticky expired; retrying Groq")
        return False
    return True


def _mark_groq_rate_limited(*, sticky_sec: float | None = None) -> None:
    global _groq_rate_limited_at, _groq_rate_limit_sticky_sec
    _groq_rate_limited_at = time.monotonic()
    sec = float(
        sticky_sec if sticky_sec is not None else _GROQ_RATE_LIMIT_STICKY_SEC
    )
    _groq_rate_limit_sticky_sec = max(60.0, min(24 * 3600, sec))


def _groq_apply_rate_limit_sticky(resp: Any) -> None:
    """Set sticky from HTTP 429."""
    if getattr(resp, "status_code", None) != 429:
        return
    _mark_groq_rate_limited()


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
    chatter_id: str = ""


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


def _rms_percentile(rms_per_sec: list[float], pct: float) -> float:
    if not rms_per_sec:
        return 0.0
    ordered = sorted(rms_per_sec)
    idx = int(max(0, min(len(ordered) - 1, round((len(ordered) - 1) * pct))))
    return float(ordered[idx])


def _window_audio_score(
    window: list[float], *, p75: float, duration: int
) -> float:
    """RMS mean/peak + silence→explosion payoff + high-energy burst density."""
    mean = sum(window) / duration
    peak = max(window)
    third = max(1, duration // 3)
    first_m = sum(window[:third]) / third
    last_m = sum(window[-third:]) / third
    payoff = max(0.0, last_m - first_m)
    thresh = p75 if p75 > 0 else mean
    burst = sum(1 for x in window if x >= thresh) / duration
    return mean + 0.5 * peak + 0.35 * payoff + 0.25 * burst * max(peak, 1.0)


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
    p75 = _rms_percentile(rms_per_sec, 0.75)
    scored: list[tuple[float, int]] = []
    for end in range(duration, len(rms_per_sec) + 1):
        window = rms_per_sec[end - duration : end]
        scored.append((_window_audio_score(window, p75=p75, duration=duration), end))
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


def classify_chat_reaction(text: str) -> str | None:
    """Reaction class for one chat line, or None if plain."""
    raw = (text or "").strip()
    if not raw:
        return None
    low = raw.lower()
    tokens = set(_REACTION_TOKEN_RE.findall(low))
    for key, words in _REACTION_WORD_CLASSES.items():
        if tokens & words or low in words:
            return key
    for key, pattern in _REACTION_PATTERNS:
        if pattern.search(raw):
            return key
    if CLIP_PHRASE_RE.search(raw):
        return "clip_ask"
    return None


def chat_reaction_hits(texts: list[str]) -> tuple[int, float]:
    """(hit_count, hit_fraction) for reaction-class messages in a burst."""
    if not texts:
        return 0, 0.0
    hits = sum(1 for t in texts if classify_chat_reaction(t))
    return hits, hits / len(texts)


def _chatter_key(msg: ChatMessage) -> str:
    cid = (msg.chatter_id or "").strip()
    if cid:
        return cid
    # No author (tests / partial GQL) — fall back so unique≈message count.
    return f"m:{msg.comment_id or f'{msg.offset_sec}:{msg.text}'}"


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
    lag_sec: int = CHAT_LAG_SEC,
) -> list[ClipCandidate]:
    """Unique-chatter spikes vs rolling baseline; lag-shifted; reaction boost."""
    if n <= 0 or duration <= 0 or bucket_sec <= 0 or not messages:
        return []
    buckets: dict[int, tuple[set[str], list[str]]] = {}
    for msg in messages:
        b = int(msg.offset_sec) // bucket_sec
        entry = buckets.get(b)
        if entry is None:
            entry = (set(), [])
            buckets[b] = entry
        ids, texts = entry
        ids.add(_chatter_key(msg))
        texts.append(msg.text or "")
    unique_counts = [len(ids) for ids, _ in buckets.values() if ids]
    if not unique_counts:
        return []
    sorted_u = sorted(unique_counts)
    median_u = float(sorted_u[len(sorted_u) // 2])
    lookback = max(1, MSG_SPIKE_BASELINE_SEC // bucket_sec)
    scored: list[tuple[float, int]] = []
    for bucket, (ids, texts) in buckets.items():
        unique = len(ids)
        prev = [
            len(buckets[b][0])
            for b in range(bucket - lookback, bucket)
            if b in buckets
        ]
        baseline = (sum(prev) / len(prev)) if prev else max(median_u, 1.0)
        hits, frac = chat_reaction_hits(texts)
        plain = hits == 0
        ratio = MSG_SPIKE_PLAIN_RATIO if plain else MSG_SPIKE_RATIO
        min_u = MSG_SPIKE_PLAIN_MIN_UNIQUE if plain else MSG_SPIKE_MIN_UNIQUE
        if unique < min_u:
            continue
        if unique < ratio * max(baseline, 0.5):
            continue
        end = (bucket + 1) * bucket_sec - max(0, int(lag_sec))
        if end < duration:
            continue
        score = float(unique) * (1.0 + MSG_SPIKE_REACTION_WEIGHT * frac) + float(
            hits
        )
        scored.append((score, end))
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


def chat_game_keyword_hits(text: str) -> int:
    """Count gameplay lexicon hits in chat/ASR text."""
    if not text:
        return 0
    return len(GAME_KEYWORD_RE.findall(text))


def category_is_action_game(category: str) -> bool:
    """True when Helix category looks like an action/competitive title."""
    name = (category or "").strip().lower()
    if not name:
        return False
    return any(s in name for s in _ACTION_CATEGORY_SUBSTR)


def collapse_candidate_series(
    cands: list[ClipCandidate],
    *,
    gap_sec: int = SERIES_GAP_SEC,
    post_sec: int = SERIES_POST_SEC,
) -> list[ClipCandidate]:
    """Merge candidates within ``gap_sec`` into one clip ending after the last.

    Score = best in the series + (n-1); source keeps the earliest priority label
    among phrase → emotion → game → audio → spike present in the group.
    """
    if not cands:
        return []
    gap = max(0, int(gap_sec))
    post = max(0, int(post_sec))
    ordered = sorted(cands, key=lambda c: c.vod_offset)
    groups: list[list[ClipCandidate]] = [[ordered[0]]]
    for c in ordered[1:]:
        if int(c.vod_offset) - int(groups[-1][-1].vod_offset) <= gap:
            groups[-1].append(c)
        else:
            groups.append([c])
    priority = ("phrase", "emotion", "game", "audio", "spike")
    out: list[ClipCandidate] = []
    for group in groups:
        best = max(group, key=lambda c: c.score)
        source: ClipSource = best.source
        for pref in priority:
            if any(c.source == pref for c in group):
                source = pref  # type: ignore[assignment]
                break
        # Extra post-roll only when several beats share a window.
        end = int(group[-1].vod_offset) + (post if len(group) > 1 else 0)
        out.append(
            ClipCandidate(
                vod_offset=end,
                score=float(best.score) + float(len(group) - 1),
                source=source,
            )
        )
    return out


def clip_start_offset(
    vod_offset_end: int, *, duration: int = CLIP_DURATION_SEC
) -> int:
    """Helix Create Clip uses end offset; display/overlap use start."""
    return max(0, int(vod_offset_end) - max(0, int(duration)))


def start_is_occupied(
    start: int,
    occupied_starts: list[int] | tuple[int, ...] | set[int],
    *,
    min_gap: int = MIN_GAP_SEC,
) -> bool:
    """True when ``start`` is within ``min_gap`` of an existing clip start."""
    if min_gap <= 0:
        return int(start) in {int(o) for o in occupied_starts}
    s = int(start)
    return any(abs(s - int(o)) < min_gap for o in occupied_starts)


def helix_vod_clip_starts(rows: list[dict[str, Any]]) -> list[int]:
    """Helix Get Clips ``vod_offset`` values (clip start on the VOD)."""
    out: list[int] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = row.get("vod_offset")
        if raw is None or raw == "":
            continue
        try:
            out.append(max(0, int(raw)))
        except (TypeError, ValueError):
            continue
    return out


def filter_unoccupied(
    items: list[ClipCandidate] | list[LoudPeak],
    occupied_starts: list[int] | tuple[int, ...] | set[int],
    *,
    duration: int = CLIP_DURATION_SEC,
    min_gap: int = MIN_GAP_SEC,
) -> list:
    """Drop peaks whose clip window overlaps an existing VOD clip."""
    if not occupied_starts:
        return list(items)
    kept: list = []
    for item in items:
        start = clip_start_offset(int(item.vod_offset), duration=duration)
        if start_is_occupied(start, occupied_starts, min_gap=min_gap):
            continue
        kept.append(item)
    return kept


def merge_clip_candidates(
    phrases: list[ClipCandidate],
    emotion: list[ClipCandidate],
    game: list[ClipCandidate],
    audio: list[LoudPeak] | list[ClipCandidate],
    spikes: list[ClipCandidate],
    *,
    n: int = MAX_CLIPS,
    duration: int = CLIP_DURATION_SEC,
    min_gap: int = MIN_GAP_SEC,
    occupied_starts: list[int] | tuple[int, ...] | set[int] | None = None,
) -> list[ClipCandidate]:
    """Priority: phrase → emotion → game → audio → spike; gap on clip start.

    Skips candidates that overlap ``occupied_starts`` (existing Helix clips on
    the VOD) and keeps walking tiers for the next free slot.
    """
    if n <= 0:
        return []
    occupied = [int(o) for o in (occupied_starts or ())]
    audio_cands: list[ClipCandidate] = []
    for a in audio:
        if isinstance(a, ClipCandidate):
            audio_cands.append(
                a
                if a.source == "audio"
                else ClipCandidate(a.vod_offset, a.score, "audio")
            )
        else:
            audio_cands.append(
                ClipCandidate(vod_offset=a.vod_offset, score=a.score, source="audio")
            )
    tiers = (
        sorted(phrases, key=lambda c: c.vod_offset),
        sorted(emotion, key=lambda c: (-c.score, c.vod_offset)),
        sorted(game, key=lambda c: c.vod_offset),
        sorted(audio_cands, key=lambda c: (-c.score, c.vod_offset)),
        sorted(spikes, key=lambda c: (-c.score, c.vod_offset)),
    )
    picked: list[ClipCandidate] = []
    for tier in tiers:
        for cand in tier:
            if len(picked) >= n:
                break
            start = clip_start_offset(cand.vod_offset, duration=duration)
            if start_is_occupied(start, occupied, min_gap=min_gap):
                continue
            if any(
                abs(start - clip_start_offset(p.vod_offset, duration=duration))
                < min_gap
                for p in picked
            ):
                continue
            picked.append(cand)
        if len(picked) >= n:
            break
    return sorted(picked, key=lambda c: c.vod_offset)


def parse_emotion_score(raw: str) -> int | None:
    """First digit 1–5 in model reply; None if missing."""
    m = _EMOTION_SCORE_RE.search((raw or "").strip())
    return int(m.group(0)) if m else None


def parse_context_label(raw: str) -> Literal["game", "chat"] | None:
    """Normalize Groq context reply to game|chat."""
    token = (raw or "").strip().lower().split()
    if not token:
        return None
    word = token[0].strip(".,:;!?\"'")
    if word in ("game", "gaming", "gameplay"):
        return "game"
    if word in ("chat", "talk", "social", "chatter"):
        return "chat"
    return None


def parse_rerank_order(raw: str, n: int) -> list[int] | None:
    """Parse 1-based candidate order; None if incomplete/invalid."""
    if n <= 0:
        return None
    seen: set[int] = set()
    order: list[int] = []
    for m in _RERANK_ORDER_RE.finditer(raw or ""):
        idx = int(m.group(0))
        if idx < 1 or idx > n or idx in seen:
            continue
        seen.add(idx)
        order.append(idx)
        if len(order) >= n:
            break
    if len(order) != n:
        return None
    return order


def normalize_clip_source(raw: str | None) -> ClipSource:
    """Map stored/legacy source labels onto ClipSource."""
    src = str(raw or "audio").strip().lower()
    if src == "asr":
        return "emotion"
    if src in ("phrase", "emotion", "game", "audio", "spike"):
        return src  # type: ignore[return-value]
    return "audio"


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
        commenter = node.get("commenter")
        chatter = ""
        if isinstance(commenter, dict):
            chatter = str(
                commenter.get("id") or commenter.get("login") or ""
            ).strip()
        out.append(
            ChatMessage(
                offset_sec=sec,
                text=text,
                comment_id=cid,
                chatter_id=chatter,
            )
        )
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


def groq_api_ready() -> bool:
    """Groq key present and not in post-429 sticky cooldown."""
    return bool(groq_api_key()) and not groq_rate_limited()


def groq_asr_ready() -> bool:
    """Groq API + streamlink/ffmpeg; respects rate-limit sticky."""
    return groq_api_ready() and ai_clips_ready()


def score_audio_peaks_with_groq(
    vod_id: str,
    peaks: list[LoudPeak],
    *,
    window_sec: int = ASR_WINDOW_SEC,
    clip_duration: int = CLIP_DURATION_SEC,
) -> tuple[list[ClipCandidate], list[LoudPeak]]:
    """ASR + emotion score top audio peaks. Returns (emotion, remaining audio).

    Stops Groq work on rate-limit/outage; unscored peaks stay in remaining.
    """
    if not peaks:
        return [], []
    if not groq_asr_ready():
        return [], list(peaks)
    vid = str(vod_id or "").strip()
    if not vid.isdigit():
        return [], list(peaks)

    emotion: list[ClipCandidate] = []
    remaining: list[LoudPeak] = []
    ordered = sorted(peaks, key=lambda p: -p.score)
    for i, peak in enumerate(ordered):
        center = max(0, int(peak.vod_offset) - clip_duration // 2)
        start = max(0, center - window_sec // 2)
        try:
            text = _transcribe_vod_window(vid, start, window_sec)
            if not text.strip():
                remaining.append(peak)
                continue
            score = _groq_emotion_score(text)
            if score is not None and score > EMOTION_MIN_SCORE:
                emotion.append(
                    ClipCandidate(
                        vod_offset=peak.vod_offset,
                        score=float(score),
                        source="emotion",
                    )
                )
            else:
                remaining.append(peak)
        except GroqUnavailable:
            logger.warning(
                "ai_clips groq unavailable during emotion vod=%s; keep %s audio",
                vid,
                len(ordered) - i,
            )
            remaining.extend(ordered[i:])
            break
        except Exception:
            logger.warning(
                "ai_clips groq emotion failed vod=%s offset=%s",
                vid,
                peak.vod_offset,
                exc_info=True,
            )
            remaining.append(peak)
    logger.info(
        "ai_clips groq emotion vod=%s scored=%s hits=%s remain=%s",
        vid,
        len(ordered),
        len(emotion),
        len(remaining),
    )
    return emotion, remaining


def classify_spikes_with_groq(
    messages: list[ChatMessage],
    spikes: list[ClipCandidate],
    *,
    category: str = "",
    title: str = "",
    window_sec: int = CHAT_CONTEXT_WINDOW_SEC,
) -> tuple[list[ClipCandidate], list[ClipCandidate]]:
    """Label chat spikes game|chat. Returns (game candidates, remaining spikes)."""
    if not spikes:
        return [], []
    if not groq_api_ready():
        return [], list(spikes)

    game: list[ClipCandidate] = []
    remaining: list[ClipCandidate] = []
    ordered = sorted(spikes, key=lambda c: -c.score)
    for i, spike in enumerate(ordered):
        chat_text = _chat_text_near(
            messages, spike.vod_offset, window_sec=window_sec
        )
        if not chat_text.strip():
            remaining.append(spike)
            continue
        try:
            label = _groq_context_label(
                chat_text, category=category, title=title
            )
            if label == "game":
                game.append(
                    ClipCandidate(
                        vod_offset=spike.vod_offset,
                        score=spike.score,
                        source="game",
                    )
                )
            else:
                remaining.append(spike)
        except GroqUnavailable:
            logger.warning(
                "ai_clips groq unavailable during context; keep %s spikes",
                len(ordered) - i,
            )
            remaining.extend(ordered[i:])
            break
        except Exception:
            logger.warning(
                "ai_clips groq context failed offset=%s",
                spike.vod_offset,
                exc_info=True,
            )
            remaining.append(spike)
    logger.info(
        "ai_clips groq context spikes=%s game=%s remain=%s",
        len(ordered),
        len(game),
        len(remaining),
    )
    return game, remaining


def rerank_clip_candidates(
    candidates: list[ClipCandidate],
    messages: list[ChatMessage] | None = None,
    *,
    category: str = "",
    title: str = "",
) -> list[ClipCandidate]:
    """Reorder finalists via one Groq head-to-head call; unchanged on failure."""
    if len(candidates) <= 1 or not groq_api_ready():
        return list(candidates)
    lines: list[str] = []
    for i, cand in enumerate(candidates, start=1):
        chat_bit = ""
        if messages:
            chat_bit = _chat_text_near(
                messages, cand.vod_offset, window_sec=20, max_lines=8, max_chars=400
            ).replace("\n", " / ")
        lines.append(
            f"{i}. source={cand.source} score={cand.score:.2f} "
            f"end={cand.vod_offset}s chat={chat_bit or '-'}"
        )
    prompt = GROQ_RERANK_PROMPT.format(
        category=(category or "").strip()[:200] or "unknown",
        title=(title or "").strip()[:300] or "unknown",
        candidates="\n".join(lines),
    )
    try:
        raw = _groq_chat(prompt, max_tokens=64)
        order = parse_rerank_order(raw, len(candidates))
        if not order:
            logger.info("ai_clips groq rerank parse miss; keep merge order")
            return list(candidates)
        reordered = [candidates[i - 1] for i in order]
        logger.info(
            "ai_clips groq rerank n=%s order=%s",
            len(candidates),
            ",".join(str(i) for i in order),
        )
        return reordered
    except GroqUnavailable:
        logger.warning("ai_clips groq unavailable during rerank; keep merge order")
        return list(candidates)
    except Exception:
        logger.warning("ai_clips groq rerank failed", exc_info=True)
        return list(candidates)


def _chat_text_near(
    messages: list[ChatMessage],
    end_offset: int,
    *,
    window_sec: int = CHAT_CONTEXT_WINDOW_SEC,
    max_lines: int = 40,
    max_chars: int = 2000,
) -> str:
    start = max(0, int(end_offset) - max(1, window_sec))
    end = int(end_offset)
    lines: list[str] = []
    for msg in messages:
        if msg.offset_sec < start or msg.offset_sec > end:
            continue
        text = (msg.text or "").strip()
        if text:
            lines.append(text)
        if len(lines) >= max_lines:
            break
    return "\n".join(lines)[:max_chars]


def _audio_near_peak(
    offset: int,
    audio_peaks: list[LoudPeak],
    *,
    within_sec: int = AUDIO_NEAR_SEC,
) -> LoudPeak | None:
    """Closest loud peak within ``within_sec`` of clip end offset, else None."""
    if not audio_peaks or within_sec <= 0:
        return None
    best: LoudPeak | None = None
    best_dist = within_sec + 1
    for peak in audio_peaks:
        dist = abs(int(peak.vod_offset) - int(offset))
        if dist <= within_sec and dist < best_dist:
            best = peak
            best_dist = dist
    return best


def promote_spikes_to_game(
    messages: list[ChatMessage],
    spikes: list[ClipCandidate],
    audio_peaks: list[LoudPeak] | None = None,
    *,
    category: str = "",
    window_sec: int = CHAT_CONTEXT_WINDOW_SEC,
) -> tuple[list[ClipCandidate], list[ClipCandidate]]:
    """Promote chat spikes to game via lexicon + nearby loudness (no OCR/LLM).

    Rules (any): ≥2 keyword hits; ≥1 hit + loud peak nearby; action category +
    ≥1 hit; action category + loud nearby + strong spike (score ≥ 8).
    Remaining spikes keep a small score bump when signals are weak but present.
    """
    if not spikes:
        return [], []
    action = category_is_action_game(category)
    audio = list(audio_peaks or ())
    game: list[ClipCandidate] = []
    remain: list[ClipCandidate] = []
    for spike in spikes:
        chat_text = _chat_text_near(
            messages, spike.vod_offset, window_sec=window_sec
        )
        hits = chat_game_keyword_hits(chat_text)
        near = _audio_near_peak(spike.vod_offset, audio)
        loud_near = near is not None
        promote = (
            hits >= 2
            or (hits >= 1 and loud_near)
            or (action and hits >= 1)
            or (action and loud_near and float(spike.score) >= 8.0)
        )
        if promote:
            boost = float(spike.score) + 2.0 * hits + (1.5 if loud_near else 0.0)
            game.append(
                ClipCandidate(
                    vod_offset=spike.vod_offset, score=boost, source="game"
                )
            )
        elif hits or loud_near:
            remain.append(
                ClipCandidate(
                    vod_offset=spike.vod_offset,
                    score=float(spike.score)
                    + float(hits)
                    + (0.5 if loud_near else 0.0),
                    source="spike",
                )
            )
        else:
            remain.append(spike)
    logger.info(
        "ai_clips heuristic game spikes=%s game=%s remain=%s action=%s",
        len(spikes),
        len(game),
        len(remain),
        action,
    )
    return game, remain


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


def _groq_emotion_score(text: str) -> int | None:
    prompt = GROQ_EMOTION_PROMPT.format(text=(text or "").strip()[:2000])
    raw = _groq_chat(prompt, max_tokens=8)
    return parse_emotion_score(raw)


def _groq_context_label(
    text: str,
    *,
    category: str = "",
    title: str = "",
) -> Literal["game", "chat"] | None:
    prompt = GROQ_CONTEXT_PROMPT.format(
        category=(category or "").strip()[:200] or "unknown",
        title=(title or "").strip()[:300] or "unknown",
        text=(text or "").strip()[:2000],
    )
    raw = _groq_chat(prompt, max_tokens=8)
    return parse_context_label(raw)


def _groq_chat(prompt: str, *, max_tokens: int = 16) -> str:
    key = groq_api_key()
    if not key:
        raise GroqUnavailable("no_key")
    import requests

    resp = requests.post(
        _GROQ_CHAT_URL,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        json={
            "model": _GROQ_CHAT_MODEL,
            "temperature": 0,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    if resp.status_code == 429:
        _groq_apply_rate_limit_sticky(resp)
        logger.warning(
            "ai_clips groq chat HTTP 429 body=%s sticky_sec=%s",
            (resp.text or "")[:200],
            _groq_rate_limit_sticky_sec,
        )
        raise GroqUnavailable("http_429")
    if resp.status_code >= 500:
        logger.warning(
            "ai_clips groq chat HTTP %s body=%s",
            resp.status_code,
            (resp.text or "")[:200],
        )
        raise GroqUnavailable(f"http_{resp.status_code}")
    if resp.status_code >= 400:
        logger.warning(
            "ai_clips groq chat HTTP %s body=%s",
            resp.status_code,
            (resp.text or "")[:200],
        )
        resp.raise_for_status()
    payload = resp.json() if resp.content else {}
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices:
        return ""
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(msg, dict):
        return ""
    content = str(msg.get("content") or "").strip()
    if content:
        return content
    # Reasoning models may leave content empty.
    return str(msg.get("reasoning") or "").strip()


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
        raise GroqUnavailable("no_key")
    import requests

    with audio_path.open("rb") as fh:
        resp = requests.post(
            _GROQ_STT_URL,
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (audio_path.name, fh, "audio/mpeg")},
            data={
                "model": _GROQ_WHISPER_MODEL,
                "response_format": "json",
                "temperature": "0",
            },
            timeout=120,
        )
    if resp.status_code == 429:
        _groq_apply_rate_limit_sticky(resp)
        logger.warning(
            "ai_clips groq STT HTTP 429 body=%s sticky_sec=%s",
            (resp.text or "")[:200],
            _groq_rate_limit_sticky_sec,
        )
        raise GroqUnavailable("http_429")
    if resp.status_code >= 500:
        logger.warning(
            "ai_clips groq STT HTTP %s body=%s",
            resp.status_code,
            (resp.text or "")[:200],
        )
        raise GroqUnavailable(f"http_{resp.status_code}")
    if resp.status_code >= 400:
        logger.warning(
            "ai_clips groq STT HTTP %s body=%s",
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
