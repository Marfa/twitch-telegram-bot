"""Premium entitlement: full plan, trial, per-feature Stars, Twitch, free-chat."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from config import (
    FREE_CHAT_ID,
    PREMIUM_FREE_ACTIVE_LIMIT,
    PREMIUM_STARS_AMOUNT,
    PREMIUM_STARS_FEATURE,
    PREMIUM_STARS_LIFETIME,
    PREMIUM_STARS_YEAR,
    PREMIUM_SUBSCRIPTION_PERIOD,
    PREMIUM_TRIAL_DAYS,
    PREMIUM_TWITCH_LOGIN,
    PREMIUM_YEAR_SECONDS,
)

if TYPE_CHECKING:
    from telegram import Bot

    from db import Database
    from twitch import TwitchClient

logger = logging.getLogger(__name__)

PREMIUM_INVOICE_PREFIX = "premium:"

FEATURE_IDS: tuple[str, ...] = (
    "extra_alerts",
    "alert_types",
    "twitch_sync",
    "advanced_mode",
    "schedule_publish",
    "alert_history",
    "deleted_subscriptions_cart",
    "stream_chat",
)

# Wizard steps bundled into advanced_mode (legacy à la carte ids still honored).
ADVANCED_MODE_FEATURE_IDS: frozenset[str] = frozenset(
    {
        "advanced_mode",
        "ignore_keywords",
        "delay",
        "repeat",
        "delete_prev",
    }
)
_LEGACY_ADVANCED_FEATURE_IDS: tuple[str, ...] = (
    "ignore_keywords",
    "delay",
    "repeat",
    "delete_prev",
)

# DM alert history retention shown to the user (storage keeps the premium window).
ALERT_HISTORY_FREE_DAYS = 7
ALERT_HISTORY_PREMIUM_DAYS = 60

# Deleted subscriptions "cart" retention.
DELETED_SUBSCRIPTIONS_CART_FREE_DAYS = 10
DELETED_SUBSCRIPTIONS_CART_PREMIUM_DAYS = 30
DELETED_SUBSCRIPTIONS_CART_MAX_DAYS = DELETED_SUBSCRIPTIONS_CART_PREMIUM_DAYS

_FEATURE_LABEL_KEYS = {
    "extra_alerts": "premium_feat_extra_alerts",
    "alert_types": "premium_feat_alert_types",
    "twitch_sync": "premium_feat_twitch_sync",
    "advanced_mode": "premium_feat_advanced_mode",
    "ignore_keywords": "premium_feat_ignore_keywords",
    "delay": "premium_feat_delay",
    "repeat": "premium_feat_repeat",
    "delete_prev": "premium_feat_delete_prev",
    "schedule_publish": "premium_feat_schedule_publish",
    "alert_history": "premium_feat_alert_history",
    "deleted_subscriptions_cart": "premium_feat_deleted_subscriptions_cart",
    "stream_chat": "premium_feat_stream_chat",
}

# Free Mini App chat: read unlimited; send capped unless stream_chat / full plan.
CHAT_FREE_DAILY_SEND_LIMIT = 20


def deleted_subscriptions_cart_days(db: "Database", user_id: int) -> int:
    """10 days for free, 30 days if feature (or full plan) is active."""
    ensure_trial_expired(db, user_id)
    # Full plan also satisfies has_feature_sync(), so users get 30 days.
    return (
        DELETED_SUBSCRIPTIONS_CART_PREMIUM_DAYS
        if has_feature_sync(db, user_id, "deleted_subscriptions_cart")
        else DELETED_SUBSCRIPTIONS_CART_FREE_DAYS
    )


def chat_daily_send_limit(db: "Database", user_id: int) -> int | None:
    """None = unlimited (Premium stream_chat / full plan); else free daily cap."""
    ensure_trial_expired(db, user_id)
    if has_feature_sync(db, user_id, "stream_chat"):
        return None
    return CHAT_FREE_DAILY_SEND_LIMIT


@dataclass(frozen=True)
class PremiumStatus:
    permanent: bool
    stars_until: int  # unix; 0 if none
    stars_charge_id: str
    stars_canceled: bool
    twitch_active: bool
    twitch_user_id: str
    trial_until: int = 0
    trial_used: bool = False
    features: dict[str, int] = field(default_factory=dict)
    feature_charges: dict[str, str] = field(default_factory=dict)
    feature_canceled: dict[str, bool] = field(default_factory=dict)

    @property
    def stars_active(self) -> bool:
        return self.stars_until > int(time.time())

    @property
    def trial_active(self) -> bool:
        return self.trial_until > int(time.time())

    @property
    def has_full_plan(self) -> bool:
        """Month/year/life/trial/Twitch — unlocks every feature."""
        return (
            self.permanent
            or self.stars_active
            or self.twitch_active
            or self.trial_active
        )

    @property
    def has_active_features(self) -> bool:
        now = int(time.time())
        return any(int(u) > now for u in self.features.values())

    @property
    def is_premium(self) -> bool:
        """Full plan or any paid à la carte feature."""
        return self.has_full_plan or self.has_active_features

    def feature_until(self, feature_id: str) -> int:
        return int(self.features.get(feature_id) or 0)

    def feature_active(self, feature_id: str) -> bool:
        return self.feature_until(feature_id) > int(time.time())

    def feature_charge_id(self, feature_id: str) -> str:
        return self.feature_charges.get(feature_id) or ""

    def is_feature_canceled(self, feature_id: str) -> bool:
        return bool(self.feature_canceled.get(feature_id))

    def feature_cancelable(self, feature_id: str) -> bool:
        return (
            self.feature_active(feature_id)
            and not self.is_feature_canceled(feature_id)
            and bool(self.feature_charge_id(feature_id))
        )


def parse_premium_features_blob(
    raw: str | dict | None,
) -> tuple[dict[str, int], dict[str, str], dict[str, bool]]:
    """Support `{fid: until}` and `{fid: {until, charge_id, canceled}}`."""
    features: dict[str, int] = {}
    charges: dict[str, str] = {}
    canceled: dict[str, bool] = {}
    if not raw:
        return features, charges, canceled
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return features, charges, canceled
    if not isinstance(data, dict):
        return features, charges, canceled
    for key, val in data.items():
        fid = str(key)
        if isinstance(val, dict):
            try:
                features[fid] = int(val.get("until") or 0)
            except (TypeError, ValueError):
                features[fid] = 0
            cid = str(val.get("charge_id") or "")
            if cid:
                charges[fid] = cid
            if val.get("canceled"):
                canceled[fid] = True
        else:
            try:
                features[fid] = int(val)
            except (TypeError, ValueError):
                features[fid] = 0
    return features, charges, canceled


def dump_premium_features_blob(
    features: dict[str, int],
    charges: dict[str, str] | None = None,
    canceled: dict[str, bool] | None = None,
) -> str:
    charges = charges or {}
    canceled = canceled or {}
    out: dict[str, Any] = {}
    for fid, until in features.items():
        cid = charges.get(fid) or ""
        is_canceled = bool(canceled.get(fid))
        if cid or is_canceled:
            entry: dict[str, Any] = {"until": int(until)}
            if cid:
                entry["charge_id"] = cid
            if is_canceled:
                entry["canceled"] = True
            out[fid] = entry
        else:
            out[fid] = int(until)
    return json.dumps(out, ensure_ascii=False)


def feature_label_key(feature_id: str) -> str:
    return _FEATURE_LABEL_KEYS.get(feature_id, feature_id)


def premium_feature_in_unreleased_beta(premium_feature_id: str) -> bool:
    """True while a beta manifest entry still gates this Premium id (alpha/beta)."""
    import beta as beta_features

    if not premium_feature_id:
        return False
    for feat in beta_features.list_features(stages=frozenset({"alpha", "beta"})):
        if feat.premium_feature_id == premium_feature_id:
            return True
    return False


def purchasable_feature_ids() -> tuple[str, ...]:
    """FEATURE_IDS visible in à la carte Stars purchase (hides unreleased betas)."""
    return tuple(
        fid for fid in FEATURE_IDS if not premium_feature_in_unreleased_beta(fid)
    )


def invoice_payload(user_id: int, kind: str = "month", features: list[str] | None = None) -> str:
    if kind == "feat":
        allowed = set(purchasable_feature_ids())
        ids = ",".join(f for f in (features or []) if f in allowed)
        return f"{PREMIUM_INVOICE_PREFIX}feat:{user_id}:{ids}"
    if kind in ("month", "year", "life"):
        return f"{PREMIUM_INVOICE_PREFIX}{kind}:{user_id}"
    # legacy: premium:{uid}
    return f"{PREMIUM_INVOICE_PREFIX}{user_id}"


@dataclass(frozen=True)
class ParsedInvoice:
    user_id: int
    kind: str  # month | year | life | feat | legacy
    features: tuple[str, ...] = ()


def parse_invoice_payload(payload: str) -> ParsedInvoice | None:
    if not payload.startswith(PREMIUM_INVOICE_PREFIX):
        return None
    raw = payload[len(PREMIUM_INVOICE_PREFIX) :]
    if raw.isdigit():
        return ParsedInvoice(user_id=int(raw), kind="legacy")
    parts = raw.split(":", 2)
    if len(parts) < 2:
        return None
    kind, uid_s = parts[0], parts[1]
    if not uid_s.isdigit():
        return None
    uid = int(uid_s)
    if kind in ("month", "year", "life"):
        return ParsedInvoice(user_id=uid, kind=kind)
    if kind == "feat":
        feat_raw = parts[2] if len(parts) > 2 else ""
        feats = tuple(f for f in feat_raw.split(",") if f in FEATURE_IDS)
        if not feats:
            return None
        return ParsedInvoice(user_id=uid, kind="feat", features=feats)
    return None


def get_status(db: Database, user_id: int) -> PremiumStatus:
    return db.get_premium_status(user_id)


def is_premium(db: Database, user_id: int) -> bool:
    """DB-backed full Premium (permanent / Stars / Twitch / trial). Prefer has_premium."""
    ensure_trial_expired(db, user_id)
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
        ChatMemberStatus.OWNER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.RESTRICTED,
    }


def has_feature_sync(db: Database, user_id: int, feature_id: str) -> bool:
    """DB-only feature check (no free-chat / demo). Call ensure_trial_expired first."""
    st = get_status(db, user_id)
    if st.has_full_plan:
        return True
    if feature_id in ADVANCED_MODE_FEATURE_IDS:
        if st.feature_active("advanced_mode"):
            return True
        if any(st.feature_active(fid) for fid in _LEGACY_ADVANCED_FEATURE_IDS):
            return True
    elif st.feature_active(feature_id):
        return True
    import beta as beta_features

    return beta_features.grants_premium_feature(db, user_id, feature_id)


def is_advanced_mode_enabled(
    db: Database, user_id: int, *, entitled: bool | None = None
) -> bool:
    """Wizard shows ignore/delay/repeat/delete steps when True.

    Default off. Demo always off. Non‑Premium always off.
    Explicit setting wins when entitled; if unset, auto-on only for entitled
    users who already configured advanced options on an alert.
    """
    from demo_mode import is_active

    if is_active(user_id):
        return False
    if entitled is None:
        entitled = has_feature_sync(db, user_id, "advanced_mode")
    if not entitled:
        return False
    setting = db.get_advanced_mode_setting(user_id)
    if setting is not None:
        return setting
    return db.owner_has_advanced_subscription_options(user_id)


async def advanced_mode_on(bot: Bot, db: Database, user_id: int) -> bool:
    """Like is_advanced_mode_enabled, but includes free-chat Premium via has_feature."""
    from demo_mode import is_active

    if is_active(user_id):
        return False
    entitled = await has_feature(bot, db, user_id, "advanced_mode")
    return is_advanced_mode_enabled(db, user_id, entitled=entitled)


def migrate_advanced_mode_defaults(
    db: Database, *, dry_run: bool = False
) -> tuple[int, int, int]:
    """Materialize users.advanced_mode from product defaults.

    ON only when DB-entitled for advanced_mode (full plan / feature / legacy
    à la carte) and at least one alert already uses ignore / delay / repeat /
    delete. Everyone else → OFF. Overwrites previous setting.

    Returns (examined, set_on, set_off).
    """
    examined = set_on = set_off = 0
    for user_id in sorted(set(db.get_notify_user_ids())):
        examined += 1
        ensure_trial_expired(db, user_id)
        desired = has_feature_sync(
            db, user_id, "advanced_mode"
        ) and db.owner_has_advanced_subscription_options(user_id)
        current = db.get_advanced_mode_setting(user_id)
        if current is not None and current is desired:
            continue
        if dry_run:
            if desired:
                set_on += 1
            else:
                set_off += 1
            continue
        db.set_advanced_mode_setting(user_id, desired)
        if desired:
            set_on += 1
        else:
            set_off += 1
    return examined, set_on, set_off


async def has_feature(bot: Bot, db: Database, user_id: int, feature_id: str) -> bool:
    from demo_mode import is_active

    ensure_trial_expired(db, user_id)
    if is_active(user_id):
        return False
    if has_feature_sync(db, user_id, feature_id):
        return True
    return await is_free_chat_member(bot, user_id)


async def has_premium(bot: Bot, db: Database, user_id: int) -> bool:
    """Full Premium (all features). Demo always False."""
    from demo_mode import is_active

    ensure_trial_expired(db, user_id)
    if is_active(user_id):
        return False
    if get_status(db, user_id).has_full_plan:
        return True
    return await is_free_chat_member(bot, user_id)


def free_active_limit() -> int:
    return PREMIUM_FREE_ACTIVE_LIMIT


# Per-user Stars price override (gift / test). Applies to month/year/life/feature.
_STARS_BY_USER: dict[int, int] = {
    249097744: 1,
}


def _stars_override(user_id: int | None) -> int | None:
    if user_id is None:
        return None
    return _STARS_BY_USER.get(int(user_id))


def has_custom_stars_price(user_id: int) -> bool:
    return _stars_override(user_id) is not None


def stars_price(user_id: int | None = None) -> int:
    o = _stars_override(user_id)
    return o if o is not None else PREMIUM_STARS_AMOUNT


def clear_premium(db: Database, user_id: int) -> None:
    """Drop full Premium + feature unlocks for a user (DB only; free-chat still applies)."""
    db.clear_premium(user_id)


def stars_year_price(user_id: int | None = None) -> int:
    o = _stars_override(user_id)
    return o if o is not None else PREMIUM_STARS_YEAR


def stars_lifetime_price(user_id: int | None = None) -> int:
    o = _stars_override(user_id)
    return o if o is not None else PREMIUM_STARS_LIFETIME


def stars_feature_price(user_id: int | None = None) -> int:
    o = _stars_override(user_id)
    return o if o is not None else PREMIUM_STARS_FEATURE


def stars_period() -> int:
    return PREMIUM_SUBSCRIPTION_PERIOD


def year_seconds() -> int:
    return PREMIUM_YEAR_SECONDS


def trial_days() -> int:
    return PREMIUM_TRIAL_DAYS


def twitch_channel_login() -> str:
    return PREMIUM_TWITCH_LOGIN


@dataclass(frozen=True)
class ActiveSubscriptionSlots:
    unlimited: bool
    remaining: int


def active_subscription_slots(
    db: Database, user_id: int, *, demo: bool | None = None
) -> ActiveSubscriptionSlots:
    """Single source of truth for the free active-alert cap (extra_alerts bypass).

    Use `.remaining` for bulk caps (enable-all, restore, sync import).
    Use `may_enable_subscription()` for a yes/no before enabling one row.
    """
    from demo_mode import is_active

    ensure_trial_expired(db, user_id)
    if demo is None:
        demo = is_active(user_id)
    if not demo and has_feature_sync(db, user_id, "extra_alerts"):
        return ActiveSubscriptionSlots(unlimited=True, remaining=0)
    remaining = max(
        0,
        PREMIUM_FREE_ACTIVE_LIMIT
        - db.count_enabled_subscriptions(user_id, demo=demo),
    )
    return ActiveSubscriptionSlots(unlimited=False, remaining=remaining)


def may_enable_subscription(
    db: Database, user_id: int, *, demo: bool | None = None
) -> bool:
    slots = active_subscription_slots(db, user_id, demo=demo)
    return slots.unlimited or slots.remaining > 0


async def may_enable_subscription_async(
    bot: Bot, db: Database, user_id: int, *, demo: bool | None = None
) -> bool:
    from demo_mode import is_active

    if demo is None:
        demo = is_active(user_id)
    if not demo and await has_feature(bot, db, user_id, "extra_alerts"):
        return True
    return may_enable_subscription(db, user_id, demo=demo)


def can_enable_more(db: Database, user_id: int) -> bool:
    return may_enable_subscription(db, user_id)


async def can_enable_more_async(bot: Bot, db: Database, user_id: int) -> bool:
    return await may_enable_subscription_async(bot, db, user_id)


def ensure_trial_expired(db: Database, user_id: int) -> bool:
    """If trial ended, pause subs and clear trial_until. Returns True if just expired."""
    st = get_status(db, user_id)
    if st.trial_until <= 0:
        return False
    if st.trial_until > int(time.time()):
        return False
    # Expired: pause and clear until (keep trial_used).
    db.expire_premium_trial(user_id)
    return True


def start_trial(db: Database, user_id: int) -> tuple[bool, str]:
    """Returns (ok, reason_code). reason: started | used | active | has_premium."""
    ensure_trial_expired(db, user_id)
    st = get_status(db, user_id)
    if st.trial_active:
        return False, "active"
    if st.trial_used:
        return False, "used"
    if st.permanent or st.stars_active or st.twitch_active:
        return False, "has_premium"
    until = int(time.time()) + trial_days() * 86400
    db.set_premium_trial(user_id, until_unix=until, used=True)
    return True, "started"


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
        stars_paid=stars_paid if stars_paid is not None else stars_price(user_id),
    )


def apply_lifetime_payment(
    db: Database,
    user_id: int,
    *,
    charge_id: str,
    stars_paid: int | None = None,
) -> None:
    db.set_premium_permanent(user_id, True)
    credit_referral_commission(
        db,
        invitee_id=user_id,
        charge_id=charge_id,
        stars_paid=stars_paid if stars_paid is not None else stars_lifetime_price(user_id),
    )


def apply_features_payment(
    db: Database,
    user_id: int,
    *,
    feature_ids: list[str] | tuple[str, ...],
    charge_id: str,
    until_unix: int,
    stars_paid: int | None = None,
) -> None:
    allowed = set(purchasable_feature_ids())
    ids = [fid for fid in feature_ids if fid in allowed]
    if not ids:
        return
    db.extend_premium_features(
        user_id,
        ids,
        until_unix=until_unix,
        charge_id=charge_id,
    )
    if "advanced_mode" in ids:
        db.set_advanced_mode_setting(user_id, True)
    credit_referral_commission(
        db,
        invitee_id=user_id,
        charge_id=charge_id,
        stars_paid=stars_paid
        if stars_paid is not None
        else stars_feature_price(user_id) * max(1, len(ids)),
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


def is_live_only_alert(sub: Any) -> bool:
    """True if alert is live-start only (free-tier type)."""
    return bool(
        getattr(sub, "notify_on_live", True)
        and not getattr(sub, "notify_on_end", False)
        and not getattr(sub, "notify_on_category_change", False)
        and not getattr(sub, "schedule_reminder_configured", False)
    )
