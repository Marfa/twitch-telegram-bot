"""OpenTelemetry traces to PostHog (no-op when POSTHOG_API_KEY is unset).

One span per Telegram update, one per scheduled job run, one per Telegram Bot
API call, and one per requests call. Calls to the trace exporter host are
skipped, so the exporter never traces itself.
"""

from __future__ import annotations

import functools
import logging
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_SERVICE_NAME = "twitch-telegram-bot"
_UPDATE_KINDS = (
    "message",
    "edited_message",
    "channel_post",
    "callback_query",
    "my_chat_member",
    "pre_checkout_query",
    "inline_query",
)

_provider: Any | None = None
_tracer: Any | None = None
_skip_hosts: frozenset[str] = frozenset()
_requests_patched = False
_bot_patched = False
_jobs_patched = False


def _host_of(url: str) -> str:
    try:
        return (urlsplit(str(url)).hostname or "").lower()
    except ValueError:
        return ""


def _is_skip_host(url: str) -> bool:
    host = _host_of(url)
    return bool(host) and host in _skip_hosts


def _redacted_url(url: str) -> str:
    from analytics import _redact_secrets

    return _redact_secrets(str(url))


def _mark_error(span: Any, exc: BaseException) -> None:
    """Record the failure type only: an exception message can carry a token."""
    from opentelemetry.trace import Status, StatusCode

    span.set_attribute("exception.type", type(exc).__name__)
    span.set_status(Status(StatusCode.ERROR))


def _request_attributes(method: str, url: str) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "http.request.method": str(method).upper(),
        "url.full": _redacted_url(url),
    }
    host = _host_of(url)
    if host:
        attributes["server.address"] = host
    return attributes


def _update_attributes(update: object) -> dict[str, Any]:
    """Update and chat type only, no Telegram user or chat identifiers."""
    attributes: dict[str, Any] = {}
    for name in _UPDATE_KINDS:
        if getattr(update, name, None) is not None:
            attributes["telegram.update_type"] = name
            break
    chat = getattr(update, "effective_chat", None)
    if chat is not None:
        attributes["telegram.chat_type"] = getattr(chat.type, "value", str(chat.type))
    return attributes


def _job_attributes(job: object) -> dict[str, Any]:
    """Callback name only: a job name can carry a Telegram user or subscription id."""
    callback = getattr(job, "callback", None)
    if isinstance(callback, functools.partial):
        callback = callback.func
    name = getattr(callback, "__name__", None) or type(callback).__name__
    return {"telegram.job.callback": str(name)}


def init_tracing() -> None:
    """Create the tracer provider and instrument outbound calls. Call once."""
    global _provider, _tracer, _skip_hosts
    from config import (
        BOT_VERSION,
        POSTHOG_API_KEY,
        POSTHOG_HOST,
        TRACES_OTLP_ENDPOINT,
        TRACES_SAMPLE_RATIO,
    )

    if not POSTHOG_API_KEY:
        logger.info("Tracing disabled (POSTHOG_API_KEY unset)")
        return
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBasedTraceIdRatio
        from opentelemetry.semconv.resource import ResourceAttributes
        from opentelemetry.trace import set_tracer_provider
    except ImportError:
        logger.warning("OpenTelemetry tracing missing; traces disabled")
        return

    endpoint = TRACES_OTLP_ENDPOINT
    provider = TracerProvider(
        resource=Resource.create(
            {
                ResourceAttributes.SERVICE_NAME: _SERVICE_NAME,
                ResourceAttributes.SERVICE_VERSION: BOT_VERSION,
            }
        ),
        sampler=ParentBasedTraceIdRatio(TRACES_SAMPLE_RATIO),
    )
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=endpoint,
                headers={"Authorization": f"Bearer {POSTHOG_API_KEY}"},
            )
        )
    )
    set_tracer_provider(provider)
    _provider = provider
    _tracer = provider.get_tracer(_SERVICE_NAME)
    # Telemetry export talks to the same host as the PostHog client: skip both.
    _skip_hosts = frozenset(
        host for host in (_host_of(endpoint), _host_of(POSTHOG_HOST)) if host
    )
    instrument_requests()
    instrument_telegram()
    instrument_jobs()
    logger.info(
        "Tracing enabled endpoint=%s sample_ratio=%s", endpoint, TRACES_SAMPLE_RATIO
    )


def instrument_requests() -> None:
    """Span every requests call except traffic to the telemetry host."""
    global _requests_patched
    if _requests_patched:
        return
    import requests
    from opentelemetry.trace import SpanKind

    original = requests.sessions.Session.request

    @functools.wraps(original)
    def request(self, method, url, *args, **kwargs):
        tracer = _tracer
        if tracer is None or _is_skip_host(url):
            return original(self, method, url, *args, **kwargs)
        with tracer.start_as_current_span(
            f"{str(method).upper()} {_host_of(url) or 'request'}",
            attributes=_request_attributes(method, url),
            kind=SpanKind.CLIENT,
        ) as span:
            try:
                response = original(self, method, url, *args, **kwargs)
            except Exception as exc:
                _mark_error(span, exc)
                raise
            span.set_attribute("http.response.status_code", response.status_code)
            if response.status_code >= 500:
                _mark_error(span, RuntimeError(f"HTTP {response.status_code}"))
            return response

    requests.sessions.Session.request = request  # type: ignore[method-assign]
    _requests_patched = True


def instrument_telegram() -> None:
    """Span every Telegram Bot API call at the single _do_post funnel."""
    global _bot_patched
    if _bot_patched:
        return
    from opentelemetry.trace import SpanKind
    from telegram import Bot

    original = Bot._do_post

    @functools.wraps(original)
    async def _do_post(self, endpoint, data, *args, **kwargs):
        tracer = _tracer
        if tracer is None:
            return await original(self, endpoint, data, *args, **kwargs)
        with tracer.start_as_current_span(
            f"telegram.{endpoint}",
            attributes={
                "http.request.method": "POST",
                "server.address": _host_of(getattr(self, "_base_url", "")) or "telegram",
                "telegram.endpoint": str(endpoint),
            },
            kind=SpanKind.CLIENT,
        ) as span:
            try:
                return await original(self, endpoint, data, *args, **kwargs)
            except Exception as exc:
                _mark_error(span, exc)
                raise

    Bot._do_post = _do_post  # type: ignore[method-assign]
    _bot_patched = True


def instrument_jobs() -> None:
    """Span every scheduled job run at the single Job._run funnel."""
    global _jobs_patched
    if _jobs_patched:
        return
    from opentelemetry.trace import SpanKind
    from telegram.ext import Job

    original = Job._run

    @functools.wraps(original)
    async def _run(self, application, *args, **kwargs):
        tracer = _tracer
        if tracer is None:
            return await original(self, application, *args, **kwargs)
        with tracer.start_as_current_span(
            "telegram.job",
            attributes=_job_attributes(self),
            kind=SpanKind.INTERNAL,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                return await original(self, application, *args, **kwargs)
            except Exception as exc:
                _mark_error(span, exc)
                raise

    Job._run = _run  # type: ignore[method-assign]
    _jobs_patched = True


def traced_application_class() -> type:
    """Application subclass that brackets every update in one root span."""
    from telegram.ext import Application

    try:
        from opentelemetry.trace import SpanKind
    except ImportError:
        return Application

    class TracedApplication(Application):
        async def process_update(self, update: object) -> None:
            tracer = _tracer
            if tracer is None:
                await super().process_update(update)
                return
            with tracer.start_as_current_span(
                "telegram.update",
                attributes=_update_attributes(update),
                kind=SpanKind.INTERNAL,
            ):
                await super().process_update(update)

    return TracedApplication


def shutdown_tracing() -> None:
    global _provider, _tracer
    provider = _provider
    _provider = None
    _tracer = None
    if provider is None:
        return
    try:
        provider.force_flush()
        provider.shutdown()
    except Exception:
        logger.exception("Tracing shutdown failed")
