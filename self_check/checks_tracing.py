"""Traces: spans redact URLs, skip the exporter host, mark 5xx as errors."""

from __future__ import annotations

import requests
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode
from telegram.ext import Application

import tracing

_BOT_TOKEN = "123456789:FAKEtokenVALUE00000000000000000000"  # not a credential
_TELEGRAM_URL = f"https://api.telegram.org/bot{_BOT_TOKEN}/getMe"


class _FakeChat:
    type = "private"


class _FakeUpdate:
    message = object()
    callback_query = None
    effective_chat = _FakeChat()


def _response(status_code: int) -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response._content = b"{}"
    return response


def _with_probe(status_code: int, skip_hosts: frozenset[str]):
    """Run one requests call through the patched session and collect spans."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "probe"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    saved = (tracing._tracer, tracing._skip_hosts)
    original_send = requests.Session.send
    tracing._tracer = provider.get_tracer("self_check")
    tracing._skip_hosts = skip_hosts
    tracing.instrument_requests()
    requests.Session.send = lambda self, request, **kwargs: _response(status_code)
    try:
        session = requests.Session()
        session.request("GET", _TELEGRAM_URL)
    finally:
        requests.Session.send = original_send
        tracing._tracer, tracing._skip_hosts = saved
    return exporter.get_finished_spans()


def check_tracing_spans() -> None:
    spans = _with_probe(200, frozenset())
    assert len(spans) == 1, f"expected one span, got {len(spans)}"
    span = spans[0]
    assert span.attributes["http.response.status_code"] == 200
    assert span.attributes["server.address"] == "api.telegram.org"
    url_attr = span.attributes["url.full"]
    assert "[redacted]" in url_attr, "credential was not redacted from the span url"
    assert _BOT_TOKEN.split(":", 1)[1] not in url_attr, "bot token survived redaction"
    assert span.status.status_code is not StatusCode.ERROR


def check_tracing_errors_and_skip() -> None:
    failing = _with_probe(500, frozenset())
    assert len(failing) == 1
    assert failing[0].status.status_code is StatusCode.ERROR, "5xx not marked as error"
    assert failing[0].attributes["exception.type"] == "RuntimeError"

    skipped = _with_probe(200, frozenset({"api.telegram.org"}))
    assert skipped == (), "exporter host was traced and would recurse"


def check_tracing_application_class() -> None:
    traced = tracing.traced_application_class()
    assert issubclass(traced, Application)
    assert traced is not Application
    attributes = tracing._update_attributes(_FakeUpdate())
    assert attributes == {"telegram.update_type": "message", "telegram.chat_type": "private"}


def run() -> None:
    check_tracing_spans()
    check_tracing_errors_and_skip()
    check_tracing_application_class()
