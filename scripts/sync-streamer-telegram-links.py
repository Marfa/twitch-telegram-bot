"""Backfill streamer_telegram_links: curated seeds + public non-DM subscription dests.

Inside the bot container:

  python scripts/sync-streamer-telegram-links.py

Or from the VPS host (script on host, modules in image):

  docker compose -f compose.vps.yml cp scripts/sync-streamer-telegram-links.py bot:/tmp/sync_tg.py
  docker compose -f compose.vps.yml exec -T bot python /tmp/sync_tg.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests

from config import DATABASE_PATH, DATABASE_URL, TELEGRAM_BOT_TOKEN, validate
from db import open_database
from streamer_telegram import (
    record_subscription_dest_link,
    seed_curated_links,
    telegram_url_from_chat_username,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("sync_streamer_tg")


def _open_db():
    validate()
    return open_database(DATABASE_PATH, database_url=DATABASE_URL)


def _non_dm_destinations(db) -> list[tuple[str, str, int]]:
    rows: list[tuple[str, str, int]] = []
    with db._conn() as conn:
        cur = db._cursor(conn)
        cur.execute(
            """
            SELECT DISTINCT lower(twitch_username) AS login,
                   dest_type,
                   chat_id
            FROM subscriptions
            WHERE dest_type IS NOT NULL
              AND lower(dest_type) <> 'dm'
              AND twitch_username IS NOT NULL
              AND btrim(twitch_username) <> ''
            """
        )
        for r in cur.fetchall():
            login = str(r["login"] or "").strip().lower()
            dest = str(r["dest_type"] or "").strip()
            try:
                chat_id = int(r["chat_id"])
            except (TypeError, ValueError):
                continue
            if login and dest and chat_id:
                rows.append((login, dest, chat_id))
    return rows


def _backfill_subscriptions(db) -> tuple[int, int, int]:
    token = (TELEGRAM_BOT_TOKEN or "").strip()
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN missing")
        return 0, 0, 0

    pairs = _non_dm_destinations(db)
    logger.info("non-DM destinations to check: %s", len(pairs))
    cache: dict[int, str | None] = {}
    recorded = skipped = errors = 0

    for login, dest, chat_id in pairs:
        if chat_id not in cache:
            try:
                resp = requests.get(
                    f"https://api.telegram.org/bot{token}/getChat",
                    params={"chat_id": chat_id},
                    timeout=30,
                )
                payload = resp.json() if resp.ok else {}
                if not payload.get("ok"):
                    cache[chat_id] = None
                    logger.warning(
                        "getChat fail chat_id=%s: %s",
                        chat_id,
                        payload.get("description"),
                    )
                else:
                    cache[chat_id] = (payload.get("result") or {}).get("username")
            except Exception:
                cache[chat_id] = None
                errors += 1
                logger.exception("getChat error chat_id=%s", chat_id)

        username = cache[chat_id]
        url = telegram_url_from_chat_username(username)
        if not url:
            skipped += 1
            continue
        before = set(db.list_streamer_telegram_urls(login))
        record_subscription_dest_link(
            db,
            twitch_login=login,
            dest_type=dest,
            chat_username=username,
            chat_id=chat_id,
        )
        after = set(db.list_streamer_telegram_urls(login))
        if url not in before and url in after:
            recorded += 1
            logger.info("subscription + %s -> %s", login, url)
        else:
            logger.info("subscription ok %s -> %s", login, url)

    return recorded, skipped, errors


def main() -> int:
    db = _open_db()
    curated_added = seed_curated_links(db)
    rec, skip, err = _backfill_subscriptions(db)
    with db._conn() as conn:
        cur = db._cursor(conn)
        cur.execute(
            """
            SELECT count(*) AS n FROM (
              SELECT DISTINCT twitch_login, url FROM streamer_telegram_links
            ) t
            """
        )
        total = int((cur.fetchone() or {}).get("n") or 0)
        cur.execute(
            """
            SELECT url FROM streamer_telegram_links
            WHERE twitch_login = 'marfapr'
            ORDER BY url
            """
        )
        marfa = [str(r["url"]) for r in cur.fetchall()]
    logger.info(
        "done curated_added=%s sub_new=%s skipped_private=%s errors=%s "
        "distinct_links=%s marfapr=%s",
        curated_added,
        rec,
        skip,
        err,
        total,
        marfa,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
