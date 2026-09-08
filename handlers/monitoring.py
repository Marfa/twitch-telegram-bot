from __future__ import annotations

import asyncio
import html
import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import Application, ContextTypes

import analytics
import beta as beta_features
from bot_helpers import _BROADCAST_SEND_PAUSE, _send_dm_html
from db import Database
from i18n import DEFAULT_LOCALE, SCHEDULE_TZ, SUPPORTED_LOCALES, t
from twitch import fetch_twitch_status_summary, twitch_status_fingerprint

logger = logging.getLogger(__name__)

TWITCH_STATUS_HOST = "status.twitch.com"
TWITCH_STATUS_PAGE_URL = f"https://{TWITCH_STATUS_HOST}/"
CURSOR_STATUS_API_URL = "https://status.cursor.com/api/v2/status.json"
CURSOR_STATUS_PAGE_URL = "https://status.cursor.com/"

_TWITCH_INDICATOR_KEYS = {
    "none": "twitch_indicator_none",
    "minor": "twitch_indicator_minor",
    "major": "twitch_indicator_major",
    "critical": "twitch_indicator_critical",
    "maintenance": "twitch_indicator_maintenance",
}
_TWITCH_COMPONENT_KEYS = {
    "operational": "twitch_comp_operational",
    "degraded_performance": "twitch_comp_degraded",
    "partial_outage": "twitch_comp_partial",
    "major_outage": "twitch_comp_major",
    "under_maintenance": "twitch_comp_maintenance",
}
_POSTHOG_OVERALL_KEYS = {
    "operational": "twitch_indicator_none",
    "degraded_performance": "twitch_indicator_minor",
    "partial_outage": "twitch_indicator_major",
    "major_outage": "twitch_indicator_critical",
    "under_maintenance": "twitch_indicator_maintenance",
}

def _twitch_status_label(lang: str, status: str) -> str:
    key = _TWITCH_COMPONENT_KEYS.get(status)
    if key:
        return t(key, lang)
    return status.replace("_", " ")


def _twitch_indicator_label(lang: str, indicator: str) -> str:
    key = _TWITCH_INDICATOR_KEYS.get(indicator)
    if key:
        return t(key, lang)
    return indicator


def _format_twitch_status_message(lang: str, summary: dict) -> str:
    status = summary.get("status") or {}
    indicator = str(status.get("indicator") or "none")
    headline = _twitch_indicator_label(lang, indicator)
    lines = [
        t("twitch_status_title", lang),
        "",
        headline,
    ]
    affected = [
        comp
        for comp in summary.get("components") or []
        if isinstance(comp, dict)
        and not comp.get("group")
        and str(comp.get("status") or "operational") != "operational"
    ]
    if affected:
        lines.append("")
        lines.append(t("twitch_status_affected", lang))
        for comp in affected:
            name = html.escape(str(comp.get("name") or "?"))
            label = html.escape(_twitch_status_label(lang, str(comp.get("status") or "")))
            lines.append(f"• <b>{name}</b> — {label}")
    incidents = [
        inc for inc in summary.get("incidents") or [] if isinstance(inc, dict)
    ]
    if incidents:
        lines.append("")
        lines.append(t("twitch_status_incidents", lang))
        for inc in incidents:
            name = html.escape(str(inc.get("name") or "?").strip() or "?")
            lines.append(f"• {name}")
    lines.append("")
    lines.append(f'<a href="{TWITCH_STATUS_PAGE_URL}">{TWITCH_STATUS_PAGE_URL}</a>')
    return "\n".join(lines)


async def check_twitch_status(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Poll status.twitch.com; notify availability opt-in users on changes."""
    try:
        summary = await asyncio.to_thread(fetch_twitch_status_summary)
        fingerprint = twitch_status_fingerprint(summary)
    except Exception as exc:
        logger.warning("Twitch status poll failed: %s", exc)
        return

    bot_data = context.application.bot_data
    previous = bot_data.get("twitch_status_fingerprint")
    bot_data["twitch_status_fingerprint"] = fingerprint
    if previous is None:
        # First poll after start — baseline only, no spam.
        return
    if fingerprint == previous:
        return

    db: Database = bot_data["db"]
    user_ids = db.get_availability_recipients()
    if not user_ids:
        return

    messages = {
        locale: _format_twitch_status_message(locale, summary)
        for locale in SUPPORTED_LOCALES
    }
    locale_rows = db.get_user_locales(user_ids)
    for uid in user_ids:
        locale = locale_rows.get(uid) or DEFAULT_LOCALE
        message = messages.get(locale) or messages[DEFAULT_LOCALE]
        await _send_dm_html(context.bot, db, uid, message)
        await asyncio.sleep(_BROADCAST_SEND_PAUSE)


def _format_posthog_status_message(lang: str, snapshot: dict) -> str:
    overall = str(snapshot.get("overall") or "operational")
    overall_key = _POSTHOG_OVERALL_KEYS.get(overall)
    headline = (
        t(overall_key, lang) if overall_key else _twitch_status_label(lang, overall)
    )
    lines = [
        t("posthog_status_title", lang),
        "",
        headline,
    ]
    affected = [
        comp
        for comp in snapshot.get("components") or []
        if isinstance(comp, dict)
        and str(comp.get("status") or "operational") != "operational"
    ]
    if affected:
        lines.append("")
        lines.append(t("twitch_status_affected", lang))
        for comp in affected:
            name = html.escape(str(comp.get("name") or "?"))
            label = html.escape(_twitch_status_label(lang, str(comp.get("status") or "")))
            lines.append(f"• <b>{name}</b> — {label}")
    incidents = [
        inc for inc in snapshot.get("incidents") or [] if isinstance(inc, dict)
    ]
    if incidents:
        lines.append("")
        lines.append(t("twitch_status_incidents", lang))
        for inc in incidents:
            name = html.escape(str(inc.get("name") or "?").strip() or "?")
            lines.append(f"• {name}")
    lines.append("")
    lines.append(
        f'<a href="{analytics.POSTHOG_US_STATUS_PAGE_URL}">'
        f"{analytics.POSTHOG_US_STATUS_PAGE_URL}</a>"
    )
    return "\n".join(lines)


async def check_posthog_status(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Poll posthogstatus.com/us (App/Logs/Error Tracking/Destination Delivery)."""
    from config import ADMIN_USER_IDS

    try:
        summary = await asyncio.to_thread(analytics.fetch_posthog_status)
        fingerprint = analytics.posthog_us_fingerprint(summary)
    except Exception as exc:
        logger.warning("PostHog status poll failed: %s", exc)
        return

    bot_data = context.application.bot_data
    previous = bot_data.get("posthog_us_status_fingerprint")
    bot_data["posthog_us_status_fingerprint"] = fingerprint
    if previous is None:
        return
    if fingerprint == previous:
        return

    db: Database = bot_data["db"]
    user_ids = set(ADMIN_USER_IDS)
    user_ids.update(beta_features.user_ids_with_active_enrollment(db))
    if not user_ids:
        return

    snapshot = analytics.posthog_us_snapshot(summary)
    messages = {
        locale: _format_posthog_status_message(locale, snapshot)
        for locale in SUPPORTED_LOCALES
    }
    locale_rows = db.get_user_locales(list(user_ids))
    for uid in user_ids:
        locale = locale_rows.get(uid) or DEFAULT_LOCALE
        message = messages.get(locale) or messages[DEFAULT_LOCALE]
        await _send_dm_html(
            context.bot, db, uid, message, disable_web_page_preview=True
        )
        await asyncio.sleep(_BROADCAST_SEND_PAUSE)


def _cursor_status_fingerprint(snapshot: dict) -> tuple[str, str]:
    status = snapshot.get("status") or {}
    indicator = str(status.get("indicator") or "none")
    description = str(status.get("description") or "").strip()
    return indicator, description


def _cursor_indicator_label(lang: str, indicator: str) -> str:
    key = _TWITCH_INDICATOR_KEYS.get(indicator)
    if key:
        return t(key, lang)
    return indicator


def _format_cursor_status_message(lang: str, snapshot: dict) -> str:
    status = snapshot.get("status") or {}
    indicator = str(status.get("indicator") or "none")
    description = str(status.get("description") or "").strip()
    headline = _cursor_indicator_label(lang, indicator)

    lines = [
        t("cursor_status_title", lang),
        "",
        headline,
    ]
    if description:
        lines.extend(["", html.escape(description)])
    lines.append("")
    lines.append(
        f'<a href="{CURSOR_STATUS_PAGE_URL}">{CURSOR_STATUS_PAGE_URL}</a>'
    )
    return "\n".join(lines)


async def check_cursor_status(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Poll status.cursor.com; notify admins on rollup changes."""
    from config import ADMIN_USER_IDS

    if not ADMIN_USER_IDS:
        return

    import urllib.request

    async def _fetch() -> dict[str, object]:
        req = urllib.request.Request(
            CURSOR_STATUS_API_URL,
            headers={"User-Agent": "twitch-telegram-bot/status-poll"},
        )

        def _read() -> dict[str, object]:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("Cursor status payload is not an object")
            return data

        return await asyncio.to_thread(_read)

    try:
        snapshot = await _fetch()
        fingerprint = _cursor_status_fingerprint(snapshot)
    except Exception as exc:
        logger.warning("Cursor status poll failed: %s", exc)
        return

    bot_data = context.application.bot_data
    previous = bot_data.get("cursor_status_fingerprint")
    bot_data["cursor_status_fingerprint"] = fingerprint
    if previous is None or fingerprint == previous:
        # First poll after start — baseline only, no spam.
        return

    db: Database = bot_data["db"]
    user_ids = set(ADMIN_USER_IDS)
    if not user_ids:
        return

    messages = {
        locale: _format_cursor_status_message(locale, snapshot)
        for locale in SUPPORTED_LOCALES
    }
    locale_rows = db.get_user_locales(list(user_ids))
    for uid in user_ids:
        locale = locale_rows.get(uid) or DEFAULT_LOCALE
        message = messages.get(locale) or messages[DEFAULT_LOCALE]
        await _send_dm_html(
            context.bot, db, uid, message, disable_web_page_preview=True
        )
        await asyncio.sleep(_BROADCAST_SEND_PAUSE)


def _seconds_until_next_weekly_report() -> float:
    now = datetime.now(SCHEDULE_TZ)
    # Next Monday 10:00 MSK
    days_ahead = (7 - now.weekday()) % 7
    target = now.replace(hour=10, minute=0, second=0, microsecond=0) + timedelta(
        days=days_ahead
    )
    if target <= now:
        target += timedelta(days=7)
    return (target - now).total_seconds()


def _seconds_until_next_daily_stats() -> float:
    now = datetime.now(timezone.utc)
    target = now.replace(hour=3, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _seconds_until_next_premium_digest() -> float:
    """03:15 UTC — after daily_bot_stats (03:00)."""
    now = datetime.now(timezone.utc)
    target = now.replace(hour=3, minute=15, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


_ADMIN_GROWTH_SNAPSHOT_KEYS = ("weekly", "monthly")
_MONTH_NAMES_EN = (
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_MONTH_NAMES_RU = (
    "",
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
)


def _delta_suffix(current: int, previous: int | None) -> str:
    """Parenthetical change vs previous mailing; empty when no prior snapshot."""
    if previous is None:
        return ""
    delta = current - previous
    if delta > 0:
        return f" (+{delta})"
    return f" ({delta})"


def _admin_growth_snapshot_path() -> Path:
    from config import DATABASE_PATH

    return Path(DATABASE_PATH).expanduser().resolve().parent / "admin_growth_snapshots.json"


def _load_admin_growth_snapshots(path: Path | None = None) -> dict[str, dict[str, int]]:
    path = path or _admin_growth_snapshot_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, int]] = {}
    for key in _ADMIN_GROWTH_SNAPSHOT_KEYS:
        row = raw.get(key)
        if not isinstance(row, dict):
            continue
        try:
            out[key] = {
                "count": int(row["count"]),
                "paid": int(row["paid"]),
                "trials": int(row["trials"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _save_admin_growth_snapshot(
    kind: str, *, count: int, paid: int, trials: int, path: Path | None = None
) -> None:
    if kind not in _ADMIN_GROWTH_SNAPSHOT_KEYS:
        raise ValueError(f"unknown growth snapshot kind: {kind}")
    path = path or _admin_growth_snapshot_path()
    data = _load_admin_growth_snapshots(path)
    data[kind] = {"count": count, "paid": paid, "trials": trials}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _previous_calendar_month_bounds(
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """[start, end) of previous calendar month in SCHEDULE_TZ."""
    now = now or datetime.now(SCHEDULE_TZ)
    first_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if first_this.month == 1:
        first_prev = first_this.replace(year=first_this.year - 1, month=12)
    else:
        first_prev = first_this.replace(month=first_this.month - 1)
    return first_prev, first_this


def _month_period_label(lang: str, year: int, month: int) -> str:
    names = _MONTH_NAMES_RU if lang == "ru" else _MONTH_NAMES_EN
    return f"{names[month]} {year}"


def _format_growth_report(
    lang: str,
    *,
    template_key: str,
    count: int,
    paid: int,
    trials: list[tuple[int, int]],
    previous: dict[str, int] | None,
    period: str | None = None,
) -> str:
    trial_n = len(trials)
    trial_list = "".join(
        t(
            "weekly_trial_line",
            lang,
            user_id=user_id,
            until=datetime.fromtimestamp(until, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            ),
        )
        for user_id, until in trials
    )
    prev_count = previous.get("count") if previous else None
    prev_paid = previous.get("paid") if previous else None
    prev_trials = previous.get("trials") if previous else None
    kwargs: dict[str, object] = {
        "count": count,
        "paid": paid,
        "trials": trial_n,
        "trial_list": trial_list,
        "count_delta": _delta_suffix(count, prev_count),
        "paid_delta": _delta_suffix(paid, prev_paid),
        "trials_delta": _delta_suffix(trial_n, prev_trials),
    }
    if period is not None:
        kwargs["period"] = period
    return t(template_key, lang, **kwargs)


async def _send_growth_report_to_admins(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    template_key: str,
    count: int,
    paid: int,
    trials: list[tuple[int, int]],
    snapshot_kind: str,
    period_for_lang: Callable[[str], str] | None = None,
) -> None:
    from config import ADMIN_USER_IDS

    if not ADMIN_USER_IDS:
        return
    if count <= 0 and paid <= 0:
        return
    db: Database = context.application.bot_data["db"]
    previous = _load_admin_growth_snapshots().get(snapshot_kind)
    for admin_id in ADMIN_USER_IDS:
        lang = db.get_user_locale(admin_id) or DEFAULT_LOCALE
        period = period_for_lang(lang) if period_for_lang else None
        text = _format_growth_report(
            lang,
            template_key=template_key,
            count=count,
            paid=paid,
            trials=trials,
            previous=previous,
            period=period,
        )
        try:
            await context.bot.send_message(admin_id, text)
        except (BadRequest, Forbidden) as exc:
            logger.warning(
                "Cannot send %s report to admin %s: %s", snapshot_kind, admin_id, exc
            )
    _save_admin_growth_snapshot(
        snapshot_kind, count=count, paid=paid, trials=len(trials)
    )


async def daily_bot_stats_snapshot(context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    analytics.capture_bot_stats(db.get_bot_stats())


def _format_premium_purchase_line(lang: str, row) -> str:
    kind_key = f"premium_kind_{row.kind}"
    kind_label = t(kind_key, lang)
    if kind_label == kind_key:
        kind_label = row.kind
    if row.kind == "feat" and row.features:
        kind_label = f"{kind_label}: {row.features}"
    elif row.kind == "channel" and row.features:
        kind_label = f"{kind_label}: {row.features}"
    if row.until_unix > 0:
        until = datetime.fromtimestamp(row.until_unix, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    else:
        until = t("premium_until_lifetime", lang)
    source = row.source or "—"
    if row.source_feature:
        source = f"{source}/{row.source_feature}"
    return t(
        "daily_premium_purchase_line",
        lang,
        user_id=row.user_id,
        kind=kind_label,
        stars=row.stars,
        until=until,
        source=source,
    )


async def daily_premium_purchases_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    """DM admins a list of new Stars Premium purchases since the last digest."""
    from config import ADMIN_USER_IDS

    if not ADMIN_USER_IDS:
        return
    db: Database = context.application.bot_data["db"]
    rows = db.list_undigested_premium_purchases()
    if not rows:
        return
    for admin_id in ADMIN_USER_IDS:
        lang = db.get_user_locale(admin_id) or DEFAULT_LOCALE
        lines = "".join(_format_premium_purchase_line(lang, row) for row in rows)
        try:
            await context.bot.send_message(
                admin_id,
                t(
                    "daily_premium_purchases",
                    lang,
                    count=len(rows),
                    lines=lines,
                ),
            )
        except (BadRequest, Forbidden) as exc:
            logger.warning(
                "Cannot send premium digest to admin %s: %s", admin_id, exc
            )
    db.mark_premium_purchases_digested([row.id for row in rows])


async def weekly_new_users_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    since = datetime.now(timezone.utc) - timedelta(days=7)
    count = db.count_new_users_since(since)
    paid = db.count_stars_payers_since(since)
    trials = db.list_active_trial_users()
    await _send_growth_report_to_admins(
        context,
        template_key="weekly_new_users",
        count=count,
        paid=paid,
        trials=trials,
        snapshot_kind="weekly",
    )


async def monthly_new_users_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    """1st of month 10:00 MSK — growth stats for the previous calendar month."""
    db: Database = context.application.bot_data["db"]
    start, end = _previous_calendar_month_bounds()
    count = db.count_new_users_between(start, end)
    paid = db.count_stars_payers_between(start, end)
    trials = db.list_active_trial_users()
    await _send_growth_report_to_admins(
        context,
        template_key="monthly_new_users",
        count=count,
        paid=paid,
        trials=trials,
        snapshot_kind="monthly",
        period_for_lang=lambda lang: _month_period_label(lang, start.year, start.month),
    )


async def notify_admins_lucky_premium(
    bot,
    db: Database,
    *,
    user_id: int,
    reason_key: str,
    reason_kwargs: dict,
    until_unix: int,
) -> None:
    from config import ADMIN_USER_IDS
    import premium_lucky as lucky

    if not ADMIN_USER_IDS:
        return
    until = lucky.fmt_until(until_unix)
    for admin_id in ADMIN_USER_IDS:
        lang = db.get_user_locale(admin_id) or DEFAULT_LOCALE
        reason = t(reason_key, lang, **reason_kwargs)
        try:
            await bot.send_message(
                admin_id,
                t(
                    "lucky_premium_admin",
                    lang,
                    user_id=user_id,
                    reason=reason,
                    until=until,
                ),
                parse_mode=ParseMode.HTML,
            )
        except (BadRequest, Forbidden) as exc:
            logger.warning(
                "Cannot send lucky Premium notice to admin %s: %s", admin_id, exc
            )


async def grant_and_announce_lucky_nth(
    bot,
    db: Database,
    *,
    user_id: int,
    lang: str,
    user_count: int,
) -> bool:
    """Grant every-Nth free Premium and notify the user + admins. Returns True if granted."""
    import premium_lucky as lucky
    from premium import is_free_chat_member

    # Skip grandfathered (permanent) and FREE_CHAT_ID / «404» members.
    if lucky.is_grandfathered(db, user_id) or await is_free_chat_member(bot, user_id):
        return False
    until = lucky.grant_lucky_month(
        db, user_id, charge_id=lucky.nth_charge_id(user_count)
    )
    if until is None:
        return False
    try:
        await bot.send_message(
            user_id,
            t("lucky_premium_nth", lang, until=lucky.fmt_until(until)),
        )
    except (BadRequest, Forbidden) as exc:
        logger.warning("Cannot send lucky nth Premium to %s: %s", user_id, exc)
    await notify_admins_lucky_premium(
        bot,
        db,
        user_id=user_id,
        reason_key="lucky_premium_reason_nth",
        reason_kwargs={"n": int(user_count)},
        until_unix=until,
    )
    return True


async def monthly_lucky_premium(context: ContextTypes.DEFAULT_TYPE) -> None:
    """1st of month — free Premium for one random user without active paid Premium."""
    from config import ENABLE_PREMIUM
    import premium_lucky as lucky

    if not ENABLE_PREMIUM:
        return
    db: Database = context.application.bot_data["db"]
    user_id = await lucky.pick_monthly_lucky_user(context.bot, db)
    if user_id is None:
        logger.info("Monthly lucky Premium: no eligible user")
        return
    until = lucky.grant_lucky_month(
        db, user_id, charge_id=lucky.monthly_charge_id()
    )
    if until is None:
        return
    lang = db.get_user_locale(user_id) or DEFAULT_LOCALE
    try:
        await context.bot.send_message(
            user_id,
            t("lucky_premium_monthly", lang, until=lucky.fmt_until(until)),
        )
    except (BadRequest, Forbidden) as exc:
        logger.warning("Cannot send monthly lucky Premium to %s: %s", user_id, exc)
    month = datetime.now(SCHEDULE_TZ).strftime("%Y-%m")
    await notify_admins_lucky_premium(
        context.bot,
        db,
        user_id=user_id,
        reason_key="lucky_premium_reason_monthly",
        reason_kwargs={"month": month},
        until_unix=until,
    )


async def notify_admins_posthog_issue(
    application: Application, payload: dict[str, str]
) -> None:
    """Telegram DM to ADMIN_USER_IDS for PostHog Issue created/reopened."""
    from config import ADMIN_USER_IDS
    from translate import markdown_to_telegram_html, translate_text

    if not ADMIN_USER_IDS:
        return
    db: Database = application.bot_data["db"]
    kind = payload.get("kind") or "created"
    name = (payload.get("name") or "Issue").strip()
    description = (payload.get("description") or "").strip()
    url = (payload.get("url") or "").strip()
    # Brief Russian summary for admins (DeepL when available).
    try:
        name_ru = translate_text(name, target_lang="ru")
    except Exception:
        logger.exception("DeepL name translate failed for PostHog issue")
        name_ru = name
    desc_ru = ""
    if description:
        if len(description) > 1200:
            description = description[:1197] + "…"
        desc_html = markdown_to_telegram_html(description)
        try:
            desc_ru = translate_text(desc_html, target_lang="ru")
        except Exception:
            logger.exception("DeepL description translate failed for PostHog issue")
            desc_ru = desc_html
        if len(desc_ru) > 1200:
            desc_ru = desc_ru[:1197] + "…"
    link_block = f"\n\n{html.escape(url)}" if url else ""
    desc_block = desc_ru + "\n" if desc_ru else ""
    for admin_id in ADMIN_USER_IDS:
        lang = db.get_user_locale(admin_id) or DEFAULT_LOCALE
        if kind == "report":
            title_key = "posthog_report_created"
        elif kind == "reopened":
            title_key = "posthog_issue_reopened"
        else:
            title_key = "posthog_issue_created"
        text = t(
            "posthog_issue_body",
            lang,
            title=t(title_key, lang),
            name=html.escape(name_ru),
            description=desc_block,
            link=link_block,
        )
        try:
            await application.bot.send_message(
                admin_id,
                text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except (BadRequest, Forbidden) as exc:
            logger.warning("Cannot send PostHog issue to admin %s: %s", admin_id, exc)


_POSTHOG_SEEN_REPORTS_MAX = 200


def _is_unchanged_message_edit(exc: BaseException) -> bool:
    return isinstance(exc, BadRequest) and "not modified" in str(exc).lower()


def _is_stale_callback_query(exc: BaseException) -> bool:
    """Telegram: answerCallbackQuery after ~30s or invalid/expired query id."""
    if not isinstance(exc, BadRequest):
        return False
    msg = str(exc).lower()
    return "query is too old" in msg or "query id is invalid" in msg


def _posthog_seen_reports_path() -> Path:
    from config import DATABASE_PATH

    return Path(DATABASE_PATH).expanduser().resolve().parent / "posthog_seen_reports.json"


def _load_posthog_seen_report_ids(path: Path) -> set[str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return set()
    if isinstance(raw, list):
        return {str(x) for x in raw if x}
    if isinstance(raw, dict) and isinstance(raw.get("ids"), list):
        return {str(x) for x in raw["ids"] if x}
    return set()


def _save_posthog_seen_report_ids(path: Path, ids: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    kept = sorted(ids)[-_POSTHOG_SEEN_REPORTS_MAX:]
    path.write_text(json.dumps(kept), encoding="utf-8")


def _is_http_timeout(exc: BaseException) -> bool:
    """True for socket/urlopen timeouts (incl. URLError wrapping TimeoutError)."""
    if isinstance(exc, TimeoutError):
        return True
    return isinstance(getattr(exc, "reason", None), TimeoutError)


async def poll_posthog_inbox_reports(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Poll PostHog Inbox reports API and notify admins about new ones."""
    from config import POSTHOG_PERSONAL_API_KEY, POSTHOG_PROJECT_ID

    if not POSTHOG_PERSONAL_API_KEY:
        if not context.application.bot_data.get("_posthog_poll_key_warned"):
            context.application.bot_data["_posthog_poll_key_warned"] = True
            logger.error(
                "PostHog Inbox reports poll skipped: POSTHOG_API_KEY_PERSONAL unset"
            )
        return

    host = "https://us.posthog.com"
    url = f"{host}/api/projects/{POSTHOG_PROJECT_ID}/signals/reports/?limit=10"

    import urllib.request

    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {POSTHOG_PERSONAL_API_KEY}"}
    )
    try:
        raw = await asyncio.to_thread(
            lambda: urllib.request.urlopen(req, timeout=30).read()
        )
        data = json.loads(raw)
    except Exception as exc:
        # Transient API slowness: next 5m poll recovers; avoid ERROR→Scout noise.
        if _is_http_timeout(exc):
            logger.warning("PostHog Inbox reports poll timed out: %s", exc)
            return
        logger.exception("PostHog Inbox reports poll failed")
        return

    reports = data.get("results") or []
    if not reports:
        return

    path = _posthog_seen_reports_path()
    seen: set[str] = context.application.bot_data.setdefault(
        "_posthog_seen_reports", set()
    )
    seen.update(_load_posthog_seen_report_ids(path))
    seeded = path.is_file()

    for report in reports:
        rid = str(report.get("id") or "")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        if not seeded:
            continue
        title = (report.get("title") or "").strip()
        if not title:
            continue
        summary = (report.get("summary") or "").strip()
        status = report.get("status") or ""
        pr_url = report.get("implementation_pr_url") or ""
        report_url = (
            f"{host}/project/{POSTHOG_PROJECT_ID}/inbox/reports/{rid}"
        )
        payload = {
            "kind": "report",
            "name": title,
            "description": summary,
            "url": pr_url or report_url,
            "fingerprint": rid,
            "status": status,
        }
        await notify_admins_posthog_issue(context.application, payload)

    try:
        _save_posthog_seen_report_ids(path, seen)
    except OSError:
        logger.warning("Could not persist PostHog seen reports", exc_info=True)

