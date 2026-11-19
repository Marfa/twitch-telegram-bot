"""Structured logs: one parseable JSON line per record, secrets redacted."""

from __future__ import annotations

import io
import json
import logging
import sys

from log_format import JsonFormatter, install_logging


def _payload(record: logging.LogRecord) -> dict:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.handle(record)
    line = stream.getvalue().strip()
    assert "\n" not in line, "one record must render as one line"
    return json.loads(line)


def _check_extra_fields_become_json() -> None:
    record = logging.LogRecord(
        "self_check.logs", logging.INFO, __file__, 1, "alert sent chat=%s", (42,), None
    )
    record.chat_id = 42
    record.outcome = "delivered"
    payload = _payload(record)
    assert payload["msg"] == "alert sent chat=42"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "self_check.logs"
    assert payload["chat_id"] == 42
    assert payload["outcome"] == "delivered"
    assert payload["ts"].endswith("Z")
    # Stdlib record fields stay out of the payload.
    assert "pathname" not in payload and "args" not in payload


def _check_secrets_redacted() -> None:
    record = logging.LogRecord(
        "self_check.logs",
        logging.ERROR,
        __file__,
        1,
        "call failed token=%s",
        ("123456:FAKEtokenVALUE000000",),
        None,
    )
    record.chat_id = "Bearer abcdef1234567890"
    dumped = json.dumps(_payload(record))
    assert "FAKEtokenVALUE000000" not in dumped
    assert "abcdef1234567890" not in dumped


def _check_traceback_redacted() -> None:
    try:
        raise ValueError("token=123456:FAKEtokenVALUE000000 rejected")
    except ValueError:
        record = logging.LogRecord(
            "self_check.logs", logging.ERROR, __file__, 1, "boom", (), sys.exc_info()
        )
    payload = _payload(record)
    assert "Traceback" in payload["exc"]
    assert "FAKEtokenVALUE000000" not in payload["exc"]
    assert "[redacted]" in payload["exc"]


def _check_delivery_record_fields() -> None:
    from types import SimpleNamespace

    from handlers.delivery import _delivery_log_extra

    sub = SimpleNamespace(id=17, owner_id=555, chat_id=-100123, dest_type="channel")
    record = logging.LogRecord(
        "handlers.delivery",
        logging.WARNING,
        __file__,
        1,
        "Cannot send to %s: %s",
        (sub.chat_id, "Forbidden: bot was kicked"),
        None,
    )
    record.__dict__.update(
        _delivery_log_extra(
            sub,
            alert_type="live",
            outcome="failed",
            stream_id="44112233",
            duration_ms=812,
        )
    )
    payload = _payload(record)
    expected = {
        "sub_id": 17,
        "user_id": 555,
        "chat_id": -100123,
        "dest_type": "channel",
        "alert_type": "live",
        "outcome": "failed",
        "stream_id": "44112233",
        "duration_ms": 812,
    }
    for key, value in expected.items():
        assert payload[key] == value, key


def run() -> None:
    _check_extra_fields_become_json()
    _check_secrets_redacted()
    _check_traceback_redacted()
    _check_delivery_record_fields()
    install_logging()  # repeated setup must not raise
