from __future__ import annotations

import asyncio
import html
import json
import logging
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


async def daily_bot_stats_snapshot(context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    analytics.capture_bot_stats(db.get_bot_stats())


async def weekly_new_users_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    from config import ADMIN_USER_IDS

    if not ADMIN_USER_IDS:
        return
    db: Database = context.application.bot_data["db"]
    since = datetime.now(timezone.utc) - timedelta(days=7)
    count = db.count_new_users_since(since)
    paid = db.count_stars_payers_since(since)
    trials = db.list_active_trial_users()
    if count <= 0 and paid <= 0:
        return
    for admin_id in ADMIN_USER_IDS:
        lang = db.get_user_locale(admin_id) or DEFAULT_LOCALE
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
        try:
            await context.bot.send_message(
                admin_id,
                t(
                    "weekly_new_users",
                    lang,
                    count=count,
                    paid=paid,
                    trials=len(trials),
                    trial_list=trial_list,
                ),
            )
        except (BadRequest, Forbidden) as exc:
            logger.warning("Cannot send weekly report to admin %s: %s", admin_id, exc)


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

