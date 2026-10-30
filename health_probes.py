"""Component health probes for /health/c/<name> (PingZen / ops).

Aggregate /health stays deploy-critical (db + Helix poll). These probes are
optional dependency signals and must not change deploy rollback behavior.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Paid / quota APIs: reuse last result so PingZen @60s does not burn limits.
_CACHE_TTL_SEC = 300.0
_CACHE_LOCAL_TTL_SEC = 15.0
_HTTP_TIMEOUT = 12

_IGDB_MAX_AGE_SEC = 36 * 3600

COMPONENT_NAMES = frozenset(
    {
        "db",
        "twitch",
        "telegram",
        "igdb",
        "posthog",
        "groq",
        "bothub",
        "deepl",
        "azure_translator",
        "youtube",
        "itad",
        "gamerpower",
        "twitchdrops",
        "donationalerts",
        "clips_oauth",
    }
)

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, int, bytes]] = {}

_db_ping: Callable[[], bool] | None = None
_db: Any | None = None
_streams_ok_at_fn: Callable[[], float] | None = None
_ready_at_fn: Callable[[], float] | None = None
_is_ready_fn: Callable[[], bool] | None = None
_PROBES_LOCK = threading.Lock()

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "twitch-telegram-bot/health-probes"})


def register_component_probes(
    *,
    db_ping: Callable[[], bool] | None = None,
    db: Any | None = None,
    streams_ok_at: Callable[[], float] | None = None,
    ready_at: Callable[[], float] | None = None,
    is_ready: Callable[[], bool] | None = None,
) -> None:
    """Wire DB / readiness helpers used by local probes."""
    global _db_ping, _db, _streams_ok_at_fn, _ready_at_fn, _is_ready_fn
    with _PROBES_LOCK:
        if db_ping is not None:
            _db_ping = db_ping
        if db is not None:
            _db = db
        if streams_ok_at is not None:
            _streams_ok_at_fn = streams_ok_at
        if ready_at is not None:
            _ready_at_fn = ready_at
        if is_ready is not None:
            _is_ready_fn = is_ready


def clear_probe_cache_for_tests() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def evaluate_component(name: str) -> tuple[int, bytes]:
    """Return (HTTP status, body) for /health/c/<name>."""
    key = (name or "").strip().lower()
    if key not in COMPONENT_NAMES:
        return 404, b"unknown"

    ttl = (
        _CACHE_LOCAL_TTL_SEC
        if key in {"db", "twitch", "igdb"}
        else _CACHE_TTL_SEC
    )
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None and (now - hit[0]) < ttl:
            return hit[1], hit[2]

    try:
        status, body = _run_probe(key)
    except Exception:
        logger.exception("health probe failed name=%s", key)
        status, body = 503, b"error"

    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic(), status, body)
    return status, body


def _ok() -> tuple[int, bytes]:
    return 200, b"ok"


def _na() -> tuple[int, bytes]:
    return 200, b"n/a"


def _fail(reason: bytes) -> tuple[int, bytes]:
    return 503, reason


def _run_probe(name: str) -> tuple[int, bytes]:
    if name == "db":
        return _probe_db()
    if name == "twitch":
        return _probe_twitch()
    if name == "telegram":
        return _probe_telegram()
    if name == "igdb":
        return _probe_igdb()
    if name == "posthog":
        return _probe_posthog()
    if name == "groq":
        return _probe_groq()
    if name == "bothub":
        return _probe_bothub()
    if name == "deepl":
        return _probe_deepl()
    if name == "azure_translator":
        return _probe_azure_translator()
    if name == "youtube":
        return _probe_youtube()
    if name == "itad":
        return _probe_itad()
    if name == "gamerpower":
        return _probe_gamerpower()
    if name == "twitchdrops":
        return _probe_twitchdrops()
    if name == "donationalerts":
        return _probe_donationalerts()
    if name == "clips_oauth":
        return _probe_clips_oauth()
    return 404, b"unknown"


def _probe_db() -> tuple[int, bytes]:
    with _PROBES_LOCK:
        ping = _db_ping
    if ping is None:
        return _na()
    try:
        if ping():
            return _ok()
        return _fail(b"db")
    except Exception:
        logger.exception("health probe db ping failed")
        return _fail(b"db")


def _probe_twitch() -> tuple[int, bytes]:
    """Helix poll freshness — same signal as aggregate /health streams checks."""
    from config import CHECK_INTERVAL

    with _PROBES_LOCK:
        is_ready = _is_ready_fn
        ready_at_fn = _ready_at_fn
        streams_fn = _streams_ok_at_fn
    if is_ready is None or streams_fn is None or ready_at_fn is None:
        return _na()
    if not is_ready():
        return _fail(b"starting")
    now = time.time()
    ready_at = float(ready_at_fn())
    last_ok = float(streams_fn())
    grace = max(float(CHECK_INTERVAL) * 3.0, 180.0)
    stale = max(float(CHECK_INTERVAL) * 5.0, 300.0)
    if last_ok <= 0.0:
        if ready_at > 0.0 and (now - ready_at) < grace:
            return _ok()
        return _fail(b"streams_never")
    if (now - last_ok) > stale:
        return _fail(b"streams_stale")
    return _ok()


def _probe_telegram() -> tuple[int, bytes]:
    from config import TELEGRAM_BOT_TOKEN

    token = (TELEGRAM_BOT_TOKEN or "").strip()
    if not token:
        return _na()
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        r = _SESSION.get(url, timeout=_HTTP_TIMEOUT)
    except Exception:
        logger.exception("health probe telegram failed")
        return _fail(b"net")
    if r.status_code != 200:
        return _fail(b"auth" if r.status_code in (401, 403) else b"http")
    try:
        data = r.json()
    except Exception:
        return _fail(b"body")
    if not data.get("ok"):
        return _fail(b"api")
    return _ok()


def _probe_igdb() -> tuple[int, bytes]:
    from igdb_dumps import USED_ENDPOINTS

    with _PROBES_LOCK:
        db = _db
    if db is None:
        return _na()
    now = int(time.time())
    try:
        for ep in sorted(USED_ENDPOINTS):
            state = db.igdb_get_dump_state(ep)
            if not state:
                return _fail(b"missing")
            synced_at = int(state.get("synced_at") or 0)
            if synced_at <= 0 or (now - synced_at) > _IGDB_MAX_AGE_SEC:
                return _fail(b"stale")
            if int(state.get("row_count") or 0) <= 0:
                return _fail(b"empty")
            if ep == "external_games":
                # Same CSV also fills Steam mappings (igdb_dumps sync).
                if db.igdb_table_count("igdb_external_steam") <= 0:
                    return _fail(b"empty")
    except Exception:
        logger.exception("health probe igdb failed")
        return _fail(b"error")
    return _ok()


def _probe_posthog() -> tuple[int, bytes]:
    from config import (
        POSTHOG_API_KEY,
        POSTHOG_HOST,
        POSTHOG_PERSONAL_API_KEY,
        POSTHOG_PROJECT_ID,
    )

    personal = (POSTHOG_PERSONAL_API_KEY or "").strip()
    project_key = (POSTHOG_API_KEY or "").strip()
    if not personal and not project_key:
        return _na()
    if personal:
        # App host (not ingest): personal keys hit us.posthog.com / eu.posthog.com.
        host = "https://us.posthog.com"
        ph_host = (POSTHOG_HOST or "").lower()
        if "eu." in ph_host or "eu-" in ph_host:
            host = "https://eu.posthog.com"
        pid = (POSTHOG_PROJECT_ID or "").strip() or "554824"
        url = f"{host.rstrip('/')}/api/projects/{pid}/"
        try:
            r = _SESSION.get(
                url,
                headers={"Authorization": f"Bearer {personal}"},
                timeout=_HTTP_TIMEOUT,
            )
        except Exception:
            logger.exception("health probe posthog personal failed")
            return _fail(b"net")
        if r.status_code == 200:
            return _ok()
        return _fail(b"auth" if r.status_code in (401, 403) else b"http")
    # Project API key: cheap decide on ingest host.
    host = (POSTHOG_HOST or "https://us.i.posthog.com").rstrip("/")
    try:
        r = _SESSION.get(
            f"{host}/decide/?v=3",
            params={"token": project_key},
            timeout=_HTTP_TIMEOUT,
        )
    except Exception:
        logger.exception("health probe posthog decide failed")
        return _fail(b"net")
    if r.status_code in (200, 204):
        return _ok()
    return _fail(b"auth" if r.status_code in (401, 403) else b"http")


def _probe_groq() -> tuple[int, bytes]:
    from config import GROQ_API_KEY

    key = (GROQ_API_KEY or "").strip()
    if not key:
        return _na()
    try:
        r = _SESSION.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=_HTTP_TIMEOUT,
        )
    except Exception:
        logger.exception("health probe groq failed")
        return _fail(b"net")
    if r.status_code == 200:
        return _ok()
    return _fail(b"auth" if r.status_code in (401, 403) else b"http")


def _probe_bothub() -> tuple[int, bytes]:
    from config import BOTHUB_API_KEY, BOTHUB_BASE_URL

    key = (BOTHUB_API_KEY or "").strip()
    if not key:
        return _na()
    base = (BOTHUB_BASE_URL or "").rstrip("/")
    try:
        r = _SESSION.get(
            f"{base}/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=_HTTP_TIMEOUT,
        )
    except Exception:
        logger.exception("health probe bothub failed")
        return _fail(b"net")
    if r.status_code == 200:
        return _ok()
    return _fail(b"auth" if r.status_code in (401, 403) else b"http")


def _probe_deepl() -> tuple[int, bytes]:
    from config import DEEPL_API_KEY

    key = (DEEPL_API_KEY or "").strip()
    if not key:
        return _na()
    base = (
        "https://api-free.deepl.com"
        if key.endswith(":fx")
        else "https://api.deepl.com"
    )
    try:
        r = _SESSION.get(
            f"{base}/v2/usage",
            headers={"Authorization": f"DeepL-Auth-Key {key}"},
            timeout=_HTTP_TIMEOUT,
        )
    except Exception:
        logger.exception("health probe deepl failed")
        return _fail(b"net")
    if r.status_code == 200:
        return _ok()
    return _fail(b"auth" if r.status_code in (401, 403) else b"http")


def _probe_azure_translator() -> tuple[int, bytes]:
    from config import (
        AZURE_TRANSLATOR_ENDPOINT,
        AZURE_TRANSLATOR_KEY,
        AZURE_TRANSLATOR_REGION,
    )

    key = (AZURE_TRANSLATOR_KEY or "").strip()
    region = (AZURE_TRANSLATOR_REGION or "").strip()
    if not key or not region:
        return _na()
    endpoint = (AZURE_TRANSLATOR_ENDPOINT or "").rstrip("/")
    url = f"{endpoint}/languages"
    try:
        r = _SESSION.get(
            url,
            params={"api-version": "3.0"},
            headers={
                "Ocp-Apim-Subscription-Key": key,
                "Ocp-Apim-Subscription-Region": region,
            },
            timeout=_HTTP_TIMEOUT,
        )
    except Exception:
        logger.exception("health probe azure translator failed")
        return _fail(b"net")
    # languages is often public; key+region still validated by 200 from our endpoint call
    if r.status_code == 200:
        return _ok()
    return _fail(b"auth" if r.status_code in (401, 403) else b"http")


def _probe_youtube() -> tuple[int, bytes]:
    from config import YOUTUBE_API_KEY

    key = (YOUTUBE_API_KEY or "").strip()
    if not key:
        return _na()
    try:
        r = _SESSION.get(
            "https://www.googleapis.com/youtube/v3/i18nLanguages",
            params={"part": "snippet", "key": key},
            timeout=_HTTP_TIMEOUT,
        )
    except Exception:
        logger.exception("health probe youtube failed")
        return _fail(b"net")
    if r.status_code == 200:
        return _ok()
    return _fail(b"auth" if r.status_code in (400, 401, 403) else b"http")


def _probe_itad() -> tuple[int, bytes]:
    from config import ISTHEREANYDEAL_API_KEY

    key = (ISTHEREANYDEAL_API_KEY or "").strip()
    if not key:
        return _na()
    try:
        r = _SESSION.get(
            "https://api.isthereanydeal.com/giveaways/v1",
            params={
                "offset": 0,
                "limit": 1,
                "expired": "false",
                "mature": "false",
            },
            headers={"ITAD-API-Key": key},
            timeout=_HTTP_TIMEOUT,
        )
    except Exception:
        logger.exception("health probe itad failed")
        return _fail(b"net")
    if r.status_code == 200:
        return _ok()
    return _fail(b"auth" if r.status_code in (401, 403) else b"http")


def _probe_gamerpower() -> tuple[int, bytes]:
    try:
        r = _SESSION.get(
            "https://www.gamerpower.com/api/giveaways",
            params={"type": "game"},
            timeout=_HTTP_TIMEOUT,
        )
    except Exception:
        logger.exception("health probe gamerpower failed")
        return _fail(b"net")
    if r.status_code == 200:
        return _ok()
    return _fail(b"http")


def _probe_twitchdrops() -> tuple[int, bytes]:
    try:
        r = _SESSION.get(
            "https://twitchdrops.app/api/drops",
            params={"sort": "new", "page": 1},
            timeout=_HTTP_TIMEOUT,
        )
    except Exception:
        logger.exception("health probe twitchdrops failed")
        return _fail(b"net")
    if r.status_code == 200:
        return _ok()
    return _fail(b"http")


def _probe_donationalerts() -> tuple[int, bytes]:
    import donationalerts as da

    if not da.configured():
        return _na()
    # Unauthenticated call: 401/403 means API is up and requiring auth.
    try:
        r = _SESSION.get(
            f"{da.API_BASE}/user/oauth",
            timeout=_HTTP_TIMEOUT,
        )
    except Exception:
        logger.exception("health probe donationalerts failed")
        return _fail(b"net")
    if r.status_code in (401, 403):
        return _ok()
    if r.status_code == 200:
        return _ok()
    return _fail(b"http")


def _probe_clips_oauth() -> tuple[int, bytes]:
    refresh = (os.getenv("TWITCH_CLIPS_REFRESH_TOKEN") or "").strip().split()[0:1]
    token = refresh[0] if refresh else ""
    if not token:
        return _na()
    from config import TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET

    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        return _na()
    try:
        r = _SESSION.post(
            "https://id.twitch.tv/oauth2/token",
            data={
                "client_id": TWITCH_CLIENT_ID,
                "client_secret": TWITCH_CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": token,
            },
            timeout=_HTTP_TIMEOUT,
        )
    except Exception:
        logger.exception("health probe clips_oauth failed")
        return _fail(b"net")
    if r.status_code != 200:
        return _fail(b"auth" if r.status_code in (400, 401, 403) else b"http")
    try:
        data = r.json()
    except Exception:
        return _fail(b"body")
    scope = data.get("scope") or []
    if isinstance(scope, str):
        scopes = scope.split()
    else:
        scopes = [str(s) for s in scope]
    if "clips:edit" not in scopes:
        return _fail(b"scope")
    return _ok()
