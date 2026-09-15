"""IGDB CSV data dumps → local DB (partner feature).

Syncs only endpoints required by active product features, about once a day.
Live Apicalypse calls for those datasets are replaced by local queries.
"""
from __future__ import annotations

import csv
import logging
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_IGDB_DUMPS_URL = "https://api.igdb.com/v4/dumps"
_IGDB_EXTERNAL_TWITCH = 14
_SYNC_STALE_SEC = 20 * 3600
_BATCH = 2000
_sync_lock = threading.Lock()
_syncing = False

# endpoint → local table (+ optional import filter)
_ENDPOINT_TABLE = {
    "games": "igdb_games",
    "companies": "igdb_companies",
    "genres": "igdb_genres",
    "game_modes": "igdb_game_modes",
    "external_games": "igdb_external_twitch",
    "involved_companies": "igdb_involved",
    "covers": "igdb_covers",
    "artworks": "igdb_artworks",
}

# Feature packs: which dump endpoints each product surface needs.
_PACK_LUCKY = frozenset({"games", "external_games", "covers", "artworks"})
_PACK_COVERS = frozenset({"games", "external_games", "covers", "artworks"})
_PACK_IGNORE = frozenset(
    {
        "games",
        "external_games",
        "companies",
        "genres",
        "game_modes",
        "involved_companies",
    }
)

_ARRAY_RE = re.compile(r"[{}\s]")
_JOB_NAME = "igdb_dumps_sync"


def igdb_image_url(image_id: str, *, size: str = "cover_big_2x") -> str:
    mid = str(image_id or "").strip()
    if not mid:
        return ""
    return f"https://images.igdb.com/igdb/image/upload/t_{size}/{mid}.jpg"


def needed_endpoints(db: Any) -> set[str]:
    """Dump endpoints to keep fresh for currently active features."""
    out: set[str] = set()
    try:
        if int(db.count_users() or 0) > 0:
            # «Мне повезёт» is available to everyone with an account.
            out |= set(_PACK_LUCKY)
    except Exception:
        logger.exception("igdb needed: count_users failed")
    try:
        if db.has_any_game_cover_subs():
            out |= set(_PACK_COVERS)
    except Exception:
        logger.exception("igdb needed: game_cover check failed")
    try:
        if db.has_any_igdb_ignore_users():
            out |= set(_PACK_IGNORE)
    except Exception:
        logger.exception("igdb needed: ignore check failed")
    return out


def _parse_long_array(raw: str) -> str:
    """Postgres-array CSV cell `{1,2}` → comma list `1,2`."""
    s = (raw or "").strip()
    if not s or s == "{}":
        return ""
    s = _ARRAY_RE.sub("", s)
    parts = [p for p in s.split(",") if p.isdigit()]
    return ",".join(parts)


def _parse_int(raw: str) -> int | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _parse_bool(raw: str) -> bool:
    return (raw or "").strip().lower() in {"t", "true", "1", "yes"}


def _parse_ts(raw: str) -> int | None:
    s = (raw or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue
    return None


def _auth_headers(twitch: Any) -> dict[str, str]:
    return {
        "Client-ID": twitch._igdb_headers()["Client-ID"],
        "Authorization": twitch._igdb_headers()["Authorization"],
        "Accept": "application/json",
    }


def _http_json(url: str, headers: dict[str, str]) -> Any:
    req = Request(url, headers=headers, method="GET")
    with urlopen(req, timeout=120) as resp:
        import json

        return json.load(resp)


def _download(url: str, dest: str) -> None:
    req = Request(url, method="GET")
    with urlopen(req, timeout=600) as resp, open(dest, "wb") as out:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)


def _row_games(row: dict[str, str]) -> tuple | None:
    gid = _parse_int(row.get("id") or "")
    name = (row.get("name") or "").strip()
    if gid is None or gid <= 0 or not name:
        return None
    summary = (row.get("summary") or "").strip()
    return (
        gid,
        name,
        _parse_int(row.get("version_parent") or ""),
        _parse_ts(row.get("first_release_date") or ""),
        _parse_int(row.get("total_rating_count") or "") or 0,
        _parse_long_array(row.get("genres") or ""),
        _parse_long_array(row.get("game_modes") or ""),
        _parse_int(row.get("cover") or ""),
        summary,
    )


def _row_named(row: dict[str, str]) -> tuple | None:
    gid = _parse_int(row.get("id") or "")
    name = (row.get("name") or "").strip()
    if gid is None or gid <= 0 or not name:
        return None
    return (gid, name)


def _row_external_twitch(row: dict[str, str]) -> tuple | None:
    src = _parse_int(row.get("external_game_source") or "")
    cat = _parse_int(row.get("category") or "")
    if src != _IGDB_EXTERNAL_TWITCH and cat != _IGDB_EXTERNAL_TWITCH:
        url = (row.get("url") or "").lower()
        if "twitch.tv" not in url:
            return None
    uid = (row.get("uid") or "").strip()
    game_id = _parse_int(row.get("game") or "")
    if not uid or game_id is None or game_id <= 0:
        return None
    return (uid, game_id)


def _row_involved(row: dict[str, str]) -> tuple | None:
    game_id = _parse_int(row.get("game") or "")
    company_id = _parse_int(row.get("company") or "")
    if game_id is None or company_id is None or game_id <= 0 or company_id <= 0:
        return None
    return (
        game_id,
        company_id,
        1 if _parse_bool(row.get("developer") or "") else 0,
        1 if _parse_bool(row.get("publisher") or "") else 0,
    )


def _row_image(row: dict[str, str]) -> tuple | None:
    iid = _parse_int(row.get("id") or "")
    game_id = _parse_int(row.get("game") or "")
    image_id = (row.get("image_id") or "").strip()
    if iid is None or iid <= 0 or not image_id:
        return None
    return (iid, game_id, image_id)


_ROW_PARSERS: dict[str, Callable[[dict[str, str]], tuple | None]] = {
    "games": _row_games,
    "companies": _row_named,
    "genres": _row_named,
    "game_modes": _row_named,
    "external_games": _row_external_twitch,
    "involved_companies": _row_involved,
    "covers": _row_image,
    "artworks": _row_image,
}

_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "igdb_games": (
        "id",
        "name",
        "version_parent",
        "first_release_date",
        "total_rating_count",
        "genres",
        "game_modes",
        "cover_id",
        "summary",
    ),
    "igdb_companies": ("id", "name"),
    "igdb_genres": ("id", "name"),
    "igdb_game_modes": ("id", "name"),
    "igdb_external_twitch": ("twitch_uid", "game_id"),
    "igdb_involved": ("game_id", "company_id", "is_developer", "is_publisher"),
    "igdb_covers": ("id", "game_id", "image_id"),
    "igdb_artworks": ("id", "game_id", "image_id"),
}


def _iter_csv_rows(path: str) -> Iterable[dict[str, str]]:
    # utf-8-sig: dumps may include BOM
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            if isinstance(row, dict):
                yield {str(k): ("" if v is None else str(v)) for k, v in row.items()}


def sync_endpoint(db: Any, twitch: Any, endpoint: str) -> int:
    """Download one dump and replace the local table. Returns inserted rows."""
    if endpoint not in _ENDPOINT_TABLE:
        raise ValueError(f"unsupported igdb dump endpoint: {endpoint}")
    headers = _auth_headers(twitch)
    meta = _http_json(f"{_IGDB_DUMPS_URL}/{endpoint}", headers)
    if not isinstance(meta, dict) or not meta.get("s3_url"):
        raise RuntimeError(f"igdb dump meta missing s3_url for {endpoint}")
    s3_url = str(meta["s3_url"])
    dump_updated = int(meta.get("updated_at") or 0)
    parser = _ROW_PARSERS[endpoint]
    table = _ENDPOINT_TABLE[endpoint]
    columns = _TABLE_COLUMNS[table]

    fd, path = tempfile.mkstemp(prefix=f"igdb_{endpoint}_", suffix=".csv")
    os.close(fd)
    try:
        _download(s3_url, path)

        def batches() -> Iterable[list[tuple]]:
            batch: list[tuple] = []
            for raw in _iter_csv_rows(path):
                parsed = parser(raw)
                if parsed is None:
                    continue
                batch.append(parsed)
                if len(batch) >= _BATCH:
                    yield batch
                    batch = []
            if batch:
                yield batch

        total = db.igdb_replace_rows(table, columns, batches())
        db.igdb_set_dump_state(endpoint, dump_updated, total)
        logger.info("IGDB dump synced endpoint=%s rows=%s", endpoint, total)
        return total
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def endpoints_due(db: Any, endpoints: set[str]) -> list[str]:
    """Endpoints that are missing or older than the stale window."""
    now = int(time.time())
    due: list[str] = []
    for ep in sorted(endpoints):
        state = db.igdb_get_dump_state(ep)
        if not state:
            due.append(ep)
            continue
        synced_at = int(state.get("synced_at") or 0)
        if now - synced_at >= _SYNC_STALE_SEC:
            due.append(ep)
            continue
        table = _ENDPOINT_TABLE[ep]
        if db.igdb_table_count(table) <= 0:
            due.append(ep)
    return due


def sync_needed(db: Any, twitch: Any, *, force: bool = False) -> dict[str, int]:
    """Sync all feature-needed dumps that are due. Returns {endpoint: rows}."""
    global _syncing
    needed = needed_endpoints(db)
    if not needed:
        return {}
    due = sorted(needed) if force else endpoints_due(db, needed)
    if not due:
        return {}
    if not _sync_lock.acquire(blocking=False):
        logger.info("IGDB dump sync already running; skip")
        return {}
    _syncing = True
    out: dict[str, int] = {}
    try:
        for ep in due:
            try:
                out[ep] = sync_endpoint(db, twitch, ep)
            except Exception:
                logger.exception("IGDB dump sync failed endpoint=%s", ep)
        return out
    finally:
        _syncing = False
        _sync_lock.release()


async def sync_igdb_dumps_job(context: Any) -> None:
    db = context.application.bot_data["db"]
    twitch = context.application.bot_data["twitch"]
    if not needed_endpoints(db):
        return

    def _run() -> dict[str, int]:
        return sync_needed(db, twitch)

    import asyncio

    result = await asyncio.to_thread(_run)
    if result:
        logger.info("IGDB dump job finished: %s", result)


def ensure_igdb_dump_job(job_queue: Any, db: Any) -> None:
    from handlers.background_jobs import ensure_repeating_job

    enabled = bool(needed_endpoints(db))
    # First run soon so empty DBs fill after deploy / first user.
    first = 90.0
    if enabled:
        due = endpoints_due(db, needed_endpoints(db))
        if due:
            first = 45.0
    ensure_repeating_job(
        job_queue,
        name=_JOB_NAME,
        callback=sync_igdb_dumps_job,
        interval=24 * 3600,
        first=first,
        enabled=enabled,
    )
