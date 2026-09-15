"""Start/stop optional JobQueue timers when nobody needs them."""
from __future__ import annotations

from telegram.ext import JobQueue

from db import Database

JOB_TWITCH_SYNC = "twitch_follows_sync"
JOB_PREMIUM_TWITCH = "premium_twitch_refresh"
JOB_DROPS = "drops_check"
JOB_SCHEDULE_REMINDERS = "schedule_reminders"
JOB_FOLLOW_MONITOR = "follow_monitor_sync"


def ensure_repeating_job(
    job_queue: JobQueue | None,
    *,
    name: str,
    callback,
    interval: float,
    first: float,
    enabled: bool,
) -> None:
    if job_queue is None:
        return
    existing = list(job_queue.get_jobs_by_name(name))
    if enabled and not existing:
        job_queue.run_repeating(
            callback, interval=interval, first=first, name=name
        )
    elif not enabled:
        for job in existing:
            job.schedule_removal()


def sync_optional_jobs(job_queue: JobQueue | None, db: Database) -> None:
    """Enable per-user jobs only while someone needs them."""
    from config import CHECK_INTERVAL, SCHEDULE_CHECK_INTERVAL
    from handlers.drops import check_drops
    from handlers.follow_monitor import sync_follow_monitors
    from handlers.notifications import check_schedule_reminders
    from handlers.stream_schedule import (
        VACATION_AUTO_JOB_NAME,
        _VACATION_AUTO_INTERVAL_SEC,
        process_vacation_auto_exits,
    )
    from handlers.subscriptions import sync_twitch_follows
    from igdb_dumps import ensure_igdb_dump_job
    from premium_handlers import refresh_premium_twitch_job

    ensure_igdb_dump_job(job_queue, db)
    ensure_repeating_job(
        job_queue,
        name=JOB_TWITCH_SYNC,
        callback=sync_twitch_follows,
        interval=3600,
        first=90,
        enabled=db.has_any_periodic_twitch_sync(),
    )
    ensure_repeating_job(
        job_queue,
        name=JOB_FOLLOW_MONITOR,
        callback=sync_follow_monitors,
        interval=3600,
        first=150,
        enabled=db.has_any_enabled_follow_monitor(),
    )
    ensure_repeating_job(
        job_queue,
        name=JOB_PREMIUM_TWITCH,
        callback=refresh_premium_twitch_job,
        interval=3600,
        first=120,
        enabled=bool(db.list_premium_twitch_user_ids()),
    )
    ensure_repeating_job(
        job_queue,
        name=JOB_DROPS,
        callback=check_drops,
        interval=max(900, CHECK_INTERVAL * 6),
        first=120,
        enabled=db.has_any_drops_work(),
    )
    ensure_repeating_job(
        job_queue,
        name=JOB_SCHEDULE_REMINDERS,
        callback=check_schedule_reminders,
        interval=SCHEDULE_CHECK_INTERVAL,
        first=25,
        enabled=bool(db.get_unique_schedule_reminder_twitch_ids()),
    )
    ensure_repeating_job(
        job_queue,
        name=VACATION_AUTO_JOB_NAME,
        callback=process_vacation_auto_exits,
        interval=_VACATION_AUTO_INTERVAL_SEC,
        first=10,
        enabled=db.has_any_vacation_auto_exit(),
    )
