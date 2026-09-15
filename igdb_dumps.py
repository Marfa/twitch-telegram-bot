"""IGDB CSV data dumps → local DB (partner feature).

Syncs every used dump endpoint about once a day.
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
    "release_dates": "igdb_release_dates",
    "platforms": "igdb_platforms",
}

# All dump endpoints the bot uses — synced daily regardless of feature gates.
USED_ENDPOINTS = frozenset(_ENDPOINT_TABLE)

_ARRAY_RE = re.compile(r"[{}\s]")
_JOB_NAME = "igdb_dumps_sync"


def igdb_image_url(image_id: str, *, size: str = "cover_big_2x") -> str:
    mid = str(image_id or "").strip()
    if not mid:
        return ""
    return f"https://images.igdb.com/igdb/image/upload/t_{size}/{mid}.jpg"


def needed_endpoints(db: Any = None) -> set[str]:
    """All IGDB dump endpoints we keep locally (db ignored; kept for call sites)."""
    return set(USED_ENDPOINTS)


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


def _row_release_dates(row: dict[str, str]) -> tuple | None:
    rid = _parse_int(row.get("id") or "")
    game_id = _parse_int(row.get("game") or "")
    date = _parse_ts(row.get("date") or "")
    if rid is None or rid <= 0 or game_id is None or game_id <= 0 or date is None:
        return None
    platform_id = _parse_int(row.get("platform") or "") or 0
    human = (row.get("human") or "").strip()
    return (rid, game_id, platform_id, date, human)


_ROW_PARSERS: dict[str, Callable[[dict[str, str]], tuple | None]] = {
    "games": _row_games,
    "companies": _row_named,
    "genres": _row_named,
    "game_modes": _row_named,
    "external_games": _row_external_twitch,
    "involved_companies": _row_involved,
    "covers": _row_image,
    "artworks": _row_image,
    "release_dates": _row_release_dates,
    "platforms": _row_named,
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
    "igdb_release_dates": ("id", "game_id", "platform_id", "date", "human"),
    "igdb_platforms": ("id", "name"),
}


def _iter_csv_rows(path: str) -> Iterable[dict[str, str]]:
    # utf-8-sig: dumps may include BOM
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            if isinstance(row, dict):
                yield {str(k): ("" if v is None else str(v)) for k, v in row.items()}


def sync_endpoint(db: Any, twitch: Any, endpoint: str) -> int:
    """Download one dump and merge into the local table. Returns row count."""
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

    state = db.igdb_get_dump_state(endpoint)
    if (
        state
        and int(state.get("dump_updated_at") or 0) == dump_updated
        and dump_updated > 0
        and db.igdb_table_count(table) > 0
    ):
        rows = int(state.get("row_count") or 0) or db.igdb_table_count(table)
        db.igdb_set_dump_state(endpoint, dump_updated, rows)
        logger.info(
            "IGDB dump unchanged endpoint=%s rows=%s dump_updated_at=%s",
            endpoint,
            rows,
            dump_updated,
        )
        return rows

    fd, path = tempfile.mkstemp(prefix=f"igdb_{endpoint}_", suffix=".csv")
    os.close(fd)
    started = time.monotonic()
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
        logger.info(
            "IGDB dump synced endpoint=%s rows=%s took=%.1fs",
            endpoint,
            total,
            time.monotonic() - started,
        )
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
    """Sync all used dumps that are due (or all if force). Returns {endpoint: rows}."""
    global _syncing
    needed = needed_endpoints()
    due = sorted(needed) if force else endpoints_due(db, needed)
    if not due:
        return {}
    if not _sync_lock.acquire(blocking=False):
        logger.info("IGDB dump sync already running; skip")
        return {}
    _syncing = True
    out: dict[str, int] = {}
    started = time.monotonic()
    try:
        for ep in due:
            try:
                out[ep] = sync_endpoint(db, twitch, ep)
            except Exception:
                logger.exception("IGDB dump sync failed endpoint=%s", ep)
        logger.info(
            "IGDB dump sync done endpoints=%s took=%.1fs",
            len(out),
            time.monotonic() - started,
        )
        return out
    finally:
        _syncing = False
        _sync_lock.release()


async def sync_igdb_dumps_job(context: Any) -> None:
    db = context.application.bot_data["db"]
    twitch = context.application.bot_data["twitch"]

    def _run() -> dict[str, int]:
        return sync_needed(db, twitch)

    import asyncio

    result = await asyncio.to_thread(_run)
    if result:
        logger.info("IGDB dump job finished: %s", result)


def ensure_igdb_dump_job(job_queue: Any, db: Any) -> None:
    from handlers.background_jobs import ensure_repeating_job

    # Always on: refresh every used dump table about once a day.
    due = endpoints_due(db, needed_endpoints())
    first = 45.0 if due else 90.0
    ensure_repeating_job(
        job_queue,
        name=_JOB_NAME,
        callback=sync_igdb_dumps_job,
        interval=24 * 3600,
        first=first,
        enabled=True,
    )
