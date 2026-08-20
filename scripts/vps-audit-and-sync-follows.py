#!/usr/bin/env python3
"""Audit subscription limits and force Twitch follow sync for active sync users."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import demo_mode
import premium as prem
from bot import (
    _deleted_subscriptions_cart_enabled,
    _next_sync_iso,
    import_followed_as_subscriptions,
)
from config import ADMIN_USER_IDS, DATABASE_PATH, DATABASE_URL, MAX_SUBSCRIPTIONS_PER_OWNER
from db import Database, TwitchSync, open_database
from i18n import DEFAULT_LOCALE, t
from twitch import TwitchClient

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


def _subs_for_owner(db: Database, owner_id: int) -> list:
    demo = demo_mode.is_active(owner_id)
    return [
        s
        for s in db.get_subscriptions_by_owner(owner_id)
        if bool(s.is_demo) == demo
    ]


def audit(db: Database) -> list[tuple[int, int, int, int, bool, bool]]:
    """Return rows: owner_id, total, enabled, disabled, has_sync, is_admin."""
    rows: list[tuple[int, int, int, int, bool, bool]] = []
    for owner_id in db.get_all_owner_ids():
        subs = _subs_for_owner(db, owner_id)
        if not subs:
            continue
        total = len(subs)
        enabled = sum(1 for s in subs if s.enabled)
        disabled = total - enabled
        sync = db.get_twitch_sync(owner_id)
        has_sync = bool(sync and sync.period_days > 0)
        is_admin = owner_id in ADMIN_USER_IDS
        if total >= MAX_SUBSCRIPTIONS_PER_OWNER or has_sync:
            rows.append((owner_id, total, enabled, disabled, has_sync, is_admin))
    rows.sort(key=lambda r: (-r[1], r[0]))
    return rows


def list_active_syncs(db: Database) -> list[TwitchSync]:
    out: list[TwitchSync] = []
    for owner_id in db.get_all_owner_ids():
        sync = db.get_twitch_sync(owner_id)
        if sync and sync.period_days > 0:
            out.append(sync)
    out.sort(key=lambda s: s.owner_id)
    return out


async def sync_one(db: Database, twitch: TwitchClient, row: TwitchSync) -> dict:
    lang = db.get_user_locale(row.owner_id) or DEFAULT_LOCALE
    now = datetime.now(timezone.utc)
    try:
        token_data = await asyncio.to_thread(twitch.refresh_user_token, row.refresh_token)
        access = token_data.get("access_token") or ""
        refresh = token_data.get("refresh_token") or row.refresh_token
        followed = await asyncio.to_thread(
            twitch.get_followed_channels, access, row.twitch_user_id
        )
    except Exception:
        log.exception("sync failed owner=%s — twitch_sync row removed", row.owner_id)
        db.delete_twitch_sync(row.owner_id)
        return {"owner_id": row.owner_id, "ok": False, "error": "auth_failed"}

    imported, skipped, limited, removed_names, _new, _ask = import_followed_as_subscriptions(
        db,
        row.owner_id,
        followed,
        template=t("import_default_template", lang),
        limit=MAX_SUBSCRIPTIONS_PER_OWNER,
        prune_missing=True,
        enabled=True,
        is_demo=demo_mode.is_active(row.owner_id),
        delete_to_cart=_deleted_subscriptions_cart_enabled(db, row.owner_id),
    )
    next_at = _next_sync_iso(row.period_days, from_dt=now)
    db.update_twitch_sync_tokens(
        row.owner_id,
        refresh,
        last_sync_at=now.isoformat(),
        next_sync_at=next_at,
    )
    subs_after = _subs_for_owner(db, row.owner_id)
    enabled_after = sum(1 for s in subs_after if s.enabled)
    return {
        "owner_id": row.owner_id,
        "ok": True,
        "follows": len(followed),
        "imported": imported,
        "skipped": skipped,
        "limited": limited,
        "removed": len(removed_names),
        "subs_total": len(subs_after),
        "subs_enabled": enabled_after,
    }


async def run_sync_all(db: Database, twitch: TwitchClient) -> list[dict]:
    rows = list_active_syncs(db)
    log.info("active twitch sync users: %s", len(rows))
    results: list[dict] = []
    for row in rows:
        log.info("sync owner=%s period_days=%s", row.owner_id, row.period_days)
        results.append(await sync_one(db, twitch, row))
    return results


def print_audit(db: Database, rows: list[tuple[int, int, int, int, bool, bool]]) -> None:
    log.info(
        "MAX_SUBSCRIPTIONS_PER_OWNER=%s admins=%s",
        MAX_SUBSCRIPTIONS_PER_OWNER,
        sorted(ADMIN_USER_IDS),
    )
    at_limit = [r for r in rows if r[1] >= MAX_SUBSCRIPTIONS_PER_OWNER]
    log.info("owners at or over limit: %s", len(at_limit))
    for owner_id, total, enabled, disabled, has_sync, is_admin in rows:
        if total < MAX_SUBSCRIPTIONS_PER_OWNER and not has_sync:
            continue
        log.info(
            "owner=%s total=%s enabled=%s disabled=%s sync=%s admin=%s can_enable_more=%s",
            owner_id,
            total,
            enabled,
            disabled,
            has_sync,
            is_admin,
            prem.can_enable_more(db, owner_id),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--sync-all", action="store_true")
    args = parser.parse_args()
    if not args.audit_only and not args.sync_all:
        parser.error("pass --audit-only and/or --sync-all")

    db = open_database(DATABASE_PATH, DATABASE_URL)
    audit_rows = audit(db)
    print_audit(db, audit_rows)

    if args.sync_all:
        twitch = TwitchClient()
        results = asyncio.run(run_sync_all(db, twitch))
        ok = sum(1 for r in results if r.get("ok"))
        failed = len(results) - ok
        limited_total = sum(int(r.get("limited") or 0) for r in results if r.get("ok"))
        imported_total = sum(int(r.get("imported") or 0) for r in results if r.get("ok"))
        log.info(
            "sync done: users=%s ok=%s failed=%s imported=%s still_limited=%s",
            len(results),
            ok,
            failed,
            imported_total,
            limited_total,
        )
        for r in results:
            log.info("result %s", r)


if __name__ == "__main__":
    main()
