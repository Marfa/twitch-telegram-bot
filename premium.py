"""Premium entitlement: permanent, Stars, Twitch sub, or free-chat membership."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from config import (
    FREE_CHAT_ID,
    PREMIUM_FREE_ACTIVE_LIMIT,
    PREMIUM_STARS_AMOUNT,
    PREMIUM_SUBSCRIPTION_PERIOD,
    PREMIUM_TWITCH_LOGIN,
)

if TYPE_CHECKING:
    from telegram import Bot

    from db import Database
    from twitch import TwitchClient

logger = logging.getLogger(__name__)

PREMIUM_INVOICE_PREFIX = "premium:"


@dataclass(frozen=True)
class PremiumStatus:
    permanent: bool
    stars_until: int  # unix; 0 if none
    stars_charge_id: str
    stars_canceled: bool
    twitch_active: bool
    twitch_user_id: str

    @property
    def stars_active(self) -> bool:
        return self.stars_until > int(time.time())

    @property
    def is_premium(self) -> bool:
        return self.permanent or self.stars_active or self.twitch_active


def invoice_payload(user_id: int) -> str:
    return f"{PREMIUM_INVOICE_PREFIX}{user_id}"


def parse_invoice_payload(payload: str) -> int | None:
    if not payload.startswith(PREMIUM_INVOICE_PREFIX):
        return None
    raw = payload[len(PREMIUM_INVOICE_PREFIX) :]
    if not raw.isdigit():
        return None
    return int(raw)


def get_status(db: Database, user_id: int) -> PremiumStatus:
    return db.get_premium_status(user_id)


def is_premium(db: Database, user_id: int) -> bool:
    """DB-backed premium only (permanent / Stars / Twitch). Prefer has_premium in handlers."""
    return get_status(db, user_id).is_premium


async def is_free_chat_member(bot: Bot, user_id: int) -> bool:
    from telegram.constants import ChatMemberStatus

    if FREE_CHAT_ID is None:
        return False
    try:
        member = await bot.get_chat_member(FREE_CHAT_ID, user_id)
    except Exception:
        logger.exception("getChatMember failed for %s in %s", user_id, FREE_CHAT_ID)
        return False
    status = member.status
    if status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(member, "is_member", True))
    return status in {
        ChatMemberStatus.CREATOR,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.RESTRICTED,
    }


async def has_premium(bot: Bot, db: Database, user_id: int) -> bool:
    from demo_mode import is_active

    if is_active(user_id):
        return False
    if is_premium(db, user_id):
        return True
    return await is_free_chat_member(bot, user_id)


def free_active_limit() -> int:
    return PREMIUM_FREE_ACTIVE_LIMIT


def stars_price() -> int:
    return PREMIUM_STARS_AMOUNT


def stars_period() -> int:
    return PREMIUM_SUBSCRIPTION_PERIOD


def twitch_channel_login() -> str:
    return PREMIUM_TWITCH_LOGIN


def can_enable_more(db: Database, user_id: int) -> bool:
    from demo_mode import is_active

    demo = is_active(user_id)
    if not demo and is_premium(db, user_id):
        return True
    return db.count_enabled_subscriptions(user_id, demo=demo) < PREMIUM_FREE_ACTIVE_LIMIT


async def can_enable_more_async(bot: Bot, db: Database, user_id: int) -> bool:
    from demo_mode import is_active

    if await has_premium(bot, db, user_id):
        return True
    demo = is_active(user_id)
    return db.count_enabled_subscriptions(user_id, demo=demo) < PREMIUM_FREE_ACTIVE_LIMIT


async def resolve_marfapr_user(twitch: TwitchClient) -> dict[str, Any] | None:
    return await asyncio.to_thread(twitch.get_user, PREMIUM_TWITCH_LOGIN)


def apply_stars_payment(
    db: Database,
    user_id: int,
    *,
    charge_id: str,
    until_unix: int,
    stars_paid: int | None = None,
) -> None:
    db.set_premium_stars(
        user_id,
        charge_id=charge_id,
        until_unix=until_unix,
        canceled=False,
    )
    credit_referral_commission(
        db,
        invitee_id=user_id,
        charge_id=charge_id,
        stars_paid=stars_paid if stars_paid is not None else stars_price(),
    )


def credit_referral_commission(
    db: Database,
    *,
    invitee_id: int,
    charge_id: str,
    stars_paid: int,
) -> bool:
    from config import REFERRAL_COMMISSION_PERCENT

    referrer_id = db.get_referred_by(invitee_id)
    if not referrer_id:
        return False
    paid = int(stars_paid)
    if paid <= 0 or not charge_id:
        return False
    commission = paid * int(REFERRAL_COMMISSION_PERCENT) // 100
    if commission <= 0:
        return False
    return db.add_referral_credit(
        referrer_id=referrer_id,
        invitee_id=invitee_id,
        charge_id=charge_id,
        stars_paid=paid,
        commission_stars=commission,
    )


def refresh_twitch_premium(
    db: Database,
    twitch: TwitchClient,
    user_id: int,
    *,
    broadcaster_id: str | None = None,
) -> bool:
    """Re-check Twitch channel sub; update cache. Returns whether still active."""
    status = db.get_premium_status(user_id)
    refresh = db.get_premium_twitch_refresh(user_id)
    if not refresh or not status.twitch_user_id:
        if status.twitch_active:
            db.set_premium_twitch(user_id, active=False)
        return False
    try:
        token_data = twitch.refresh_user_token(refresh)
        access = token_data.get("access_token") or ""
        new_refresh = token_data.get("refresh_token") or refresh
        if new_refresh != refresh:
            db.set_premium_twitch_refresh(user_id, new_refresh)
        b_id = broadcaster_id
        if not b_id:
            broadcaster = twitch.get_user(PREMIUM_TWITCH_LOGIN)
            if not broadcaster:
                db.set_premium_twitch(user_id, active=False)
                return False
            b_id = str(broadcaster["id"])
        active = twitch.check_user_subscription(
            access, broadcaster_id=b_id, user_id=status.twitch_user_id
        )
        db.set_premium_twitch(
            user_id,
            active=active,
            twitch_user_id=status.twitch_user_id,
            refresh_token=new_refresh,
        )
        return active
    except Exception:
        logger.exception("Twitch premium refresh failed for %s", user_id)
        return status.twitch_active
