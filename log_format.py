"""Structured logging: one JSON line per record on stdout, secrets redacted."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import request_context
from analytics import _redact_secrets

# Fields every LogRecord already carries; anything else is an extra= field.
_RESERVED = frozenset(logging.makeLogRecord({}).__dict__)


class RequestIdFilter(logging.Filter):
    """Copy the id of the update being handled onto its log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            request_id = request_context.current_request_id()
            if request_id is not None:
                record.request_id = request_id
        return True


class JsonFormatter(logging.Formatter):
    """Render a record as one JSON line; extra= keys become top-level fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": _redact_secrets(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = _redact_secrets(value) if isinstance(value, str) else value
        if record.exc_info:
            payload["exc"] = _redact_secrets(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack"] = _redact_secrets(self.formatStack(record.stack_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def install_logging(level: int = logging.INFO) -> None:
    """Send root logs to stdout as JSON; keeps any handler already configured."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestIdFilter())
    logging.basicConfig(handlers=[handler], level=level)
