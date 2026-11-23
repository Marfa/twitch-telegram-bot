"""Application metrics over OTLP/HTTP to PostHog (no-op when POSTHOG_API_KEY is unset)."""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_SERVICE_NAME = "twitch-telegram-bot"
_EXPORT_INTERVAL_MS = 60_000

_enabled = False
_provider = None
_update_counter = None
_update_duration = None
_update_error_counter = None
_alert_counter = None
_broadcast_counter = None
_job_counter = None
_job_duration = None
_job_skip_counter = None


def init_metrics() -> None:
    """Create the OTLP metric reader from env. Safe to call once at startup."""
    global _enabled, _provider
    global _update_counter, _update_duration, _update_error_counter, _alert_counter
    global _broadcast_counter
    global _job_counter, _job_duration, _job_skip_counter
    from config import BOT_VERSION, POSTHOG_API_KEY, POSTHOG_HOST

    if not POSTHOG_API_KEY:
        logger.info("PostHog metrics disabled (POSTHOG_API_KEY unset)")
        return
    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.semconv.resource import ResourceAttributes
    except ImportError:
        logger.warning("OpenTelemetry metrics missing; application metrics disabled")
        return

    endpoint = f"{POSTHOG_HOST.rstrip('/')}/i/v1/metrics"
    provider = MeterProvider(
        resource=Resource.create(
            {
                ResourceAttributes.SERVICE_NAME: _SERVICE_NAME,
                ResourceAttributes.SERVICE_VERSION: BOT_VERSION,
            }
        ),
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(
                    endpoint=endpoint,
                    headers={"Authorization": f"Bearer {POSTHOG_API_KEY}"},
                ),
                export_interval_millis=_EXPORT_INTERVAL_MS,
            )
        ],
    )
    meter = provider.get_meter(_SERVICE_NAME)
    _update_counter = meter.create_counter(
        "bot.update", description="Inbound Telegram updates"
    )
    _update_duration = meter.create_histogram(
        "bot.update.duration", unit="s", description="Update handling time"
    )
    _update_error_counter = meter.create_counter(
        "bot.update.error", description="Unhandled handler errors"
    )
    _alert_counter = meter.create_counter(
        "bot.alert.delivery", description="Alert delivery attempts"
    )
    _broadcast_counter = meter.create_counter(
        "bot.broadcast.delivery", description="Admin broadcast deliveries"
    )
    _job_counter = meter.create_counter(
        "bot.job.run", description="Scheduled job runs"
    )
    _job_duration = meter.create_histogram(
        "bot.job.duration", unit="s", description="Scheduled job run time"
    )
    _job_skip_counter = meter.create_counter(
        "bot.job.skipped", description="Scheduled jobs skipped or missed"
    )
    _provider = provider
    _enabled = True
    logger.info("PostHog metrics enabled endpoint=%s", endpoint)


def shutdown_metrics() -> None:
    global _enabled, _provider

    if _provider is None:
        return
    try:
        _provider.force_flush()
        _provider.shutdown()
    except Exception:
        logger.exception("PostHog metrics shutdown failed")
    _provider = None
    _enabled = False


def is_enabled() -> bool:
    return _enabled


def _update_kind(update: Any) -> str:
    """Low-cardinality update class — never a user, chat or login."""
    for kind in (
        "message",
        "callback_query",
        "my_chat_member",
        "pre_checkout_query",
        "edited_message",
    ):
        if getattr(update, kind, None) is not None:
            return kind
    return "other"


def record_update(update_type: str, duration_seconds: float) -> None:
    if not _enabled:
        return
    attributes = {"update_type": update_type}
    _update_counter.add(1, attributes)
    _update_duration.record(duration_seconds, attributes)


def record_update_error(handler: str, error_type: str) -> None:
    if not _enabled:
        return
    _update_error_counter.add(1, {"handler": handler, "error_type": error_type})


def record_alert_delivery(*, alert_type: str, dest_type: str, outcome: str) -> None:
    if not _enabled:
        return
    _alert_counter.add(
        1,
        {
            "alert_type": alert_type or "unknown",
            "dest_type": dest_type or "unknown",
            "outcome": outcome,
        },
    )


def record_broadcast_delivery(
    *, msg_type: str, outcome: str, count: int = 1
) -> None:
    """Count sent or failed broadcast recipients in one add."""
    if not _enabled or count <= 0:
        return
    _broadcast_counter.add(
        count,
        {"msg_type": msg_type or "unknown", "outcome": outcome},
    )


def record_job(job_id: str, outcome: str, duration_seconds: float) -> None:
    if not _enabled:
        return
    attributes = {"job_id": job_id, "outcome": outcome}
    _job_counter.add(1, attributes)
    _job_duration.record(duration_seconds, attributes)


def record_job_skip(job_id: str, reason: str) -> None:
    if not _enabled:
        return
    _job_skip_counter.add(1, {"job_id": job_id, "reason": reason})


def instrument_update_loop(app: Any) -> None:
    """Count and time every inbound update around Application.process_update."""
    if not _enabled:
        return
    original = app.process_update

    async def _process_update(update: Any) -> Any:
        started = time.perf_counter()
        try:
            return await original(update)
        finally:
            record_update(_update_kind(update), time.perf_counter() - started)

    app.process_update = _process_update


def instrument_job(job_id: str, callback: Any) -> Any:
    """Time one repeating job callback; returns it unchanged when metrics are off."""
    if not _enabled:
        return callback

    async def _run(context: Any) -> None:
        started = time.perf_counter()
        try:
            await callback(context)
        except Exception:
            record_job(job_id, "error", time.perf_counter() - started)
            raise
        else:
            record_job(job_id, "ok", time.perf_counter() - started)

    return _run
