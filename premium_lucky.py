"""Free Premium lottery: every Nth new user and a monthly random grant.

Excluded from grants: grandfathered (premium_permanent), FREE_CHAT_ID / «404»
channel members, active Stars / à la carte payments, blocked users.
Not documented in README/guide — silent promo.
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone

from telegram import Bot

from config import ENABLE_PREMIUM
from db import Database
from i18n import SCHEDULE_TZ
from premium import get_status, is_free_chat_member, stars_period

logger = logging.getLogger(__name__)

LUCKY_NTH_EVERY = 100


def fmt_until(unix: int) -> str:
    return datetime.fromtimestamp(unix, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def nth_charge_id(user_count: int) -> str:
    return f"lucky:nth:{int(user_count)}"


def monthly_charge_id(*, when: datetime | None = None) -> str:
    dt = when or datetime.now(SCHEDULE_TZ)
    return f"lucky:monthly:{dt.strftime('%Y-%m')}"


def is_nth_milestone(user_count: int) -> bool:
    n = int(user_count)
    return n > 0 and n % LUCKY_NTH_EVERY == 0


def has_active_premium_payment(db: Database, user_id: int) -> bool:
    """True for grandfathered lifetime, active Stars, or paid à la carte features."""
    st = get_status(db, user_id)
    return bool(st.permanent or st.stars_active or st.has_active_features)


def is_grandfathered(db: Database, user_id: int) -> bool:
    return bool(get_status(db, user_id).permanent)


def grant_lucky_month(
    db: Database,
    user_id: int,
    *,
    charge_id: str,
) -> int | None:
    """Grant one month of full Premium (Stars-shaped, no auto-renew).

    Returns until_unix, or None if skipped (disabled, duplicate charge, grandfathered).
    """
    if not ENABLE_PREMIUM:
        return None
    cid = str(charge_id or "").strip()
    if not cid:
        return None
    existing = db.find_user_id_by_premium_charge(cid)
    if existing is not None:
        return None
    if is_grandfathered(db, user_id):
        return None
    st = get_status(db, user_id)
    now = int(time.time())
    base = max(now, int(st.stars_until or 0))
    until = base + stars_period()
    db.set_premium_stars(
        user_id,
        charge_id=cid,
        until_unix=until,
        canceled=True,
        touch_paid_at=False,
    )
    logger.info(
        "Lucky Premium granted user_id=%s charge_id=%s until=%s",
        user_id,
        cid,
        until,
    )
    return until


async def pick_monthly_lucky_user(
    bot: Bot,
    db: Database,
    *,
    rng: random.Random | None = None,
) -> int | None:
    """Random eligible user: not blocked, not grandfathered, no paid Premium, not in 404 chat."""
    if not ENABLE_PREMIUM:
        return None
    cid = monthly_charge_id()
    if db.find_user_id_by_premium_charge(cid) is not None:
        return None
    candidates = [
        uid
        for uid in db.list_lucky_monthly_candidate_ids()
        if not has_active_premium_payment(db, uid)
    ]
    if not candidates:
        return None
    picker = rng or random.SystemRandom()
    picker.shuffle(candidates)
    for uid in candidates:
        if await is_free_chat_member(bot, uid):
            continue
        return int(uid)
    return None
