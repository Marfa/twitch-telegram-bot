"""PostHog product analytics + error tracking + WARNING/ERROR logs (no-op when unset)."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from typing import Any

import requests

logger = logging.getLogger(__name__)

_SERVICE_NAME = "twitch-telegram-bot"
_SECRET_RE = re.compile(
    r"(?i)\b(authorization|bearer|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|password|token|phc_|phx_|phs_)"
    r"([\"'=\s:]*)([^\s\"'&,;]{6,})"
)

_client = None
_enabled = False
_logger_provider = None
_logs_handler: logging.Handler | None = None


def _redact_secrets(text: str) -> str:
    """Replace token-like substrings with a placeholder."""
    return _SECRET_RE.sub(r"\1\2[redacted]", text)


def _redact_event(event: Any) -> Any:
    """Scrub token-like substrings from every string in a PostHog event.

    Used as the client `before_send` hook, so an exception carrying a
    credential (message, args, or captured frame value) is scrubbed on the
    error-tracking / analytics path — the sink the log exporter never sees.
    """
    if isinstance(event, str):
        return _redact_secrets(event)
    if isinstance(event, dict):
        return {key: _redact_event(value) for key, value in event.items()}
    if isinstance(event, list):
        return [_redact_event(value) for value in event]
    return event


def _redact_log_record(record: Any) -> None:
    """Scrub the log body and every string attribute of one record in place."""
    if isinstance(record.body, str):
        record.body = _redact_secrets(record.body)
    attributes = record.attributes
    if attributes:
        record.attributes = {
            key: _redact_secrets(value) if isinstance(value, str) else value
            for key, value in attributes.items()
        }


class _RedactingLogExporter:
    """Wrap an OTLP log exporter and scrub each record before it leaves the process.

    The OTLP SDK copies an exception's message and traceback into the
    ``exception.message`` / ``exception.stacktrace`` attributes. A logging
    filter only sees the rendered message, so it never reaches those
    attributes — where a credential in an exception text ends up. Redacting at
    export is the last point before PostHog receives the record.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def export(self, batch: Any) -> Any:
        for data in batch:
            _redact_log_record(data.log_record)
        return self._inner.export(batch)

    def shutdown(self) -> Any:
        return self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> Any:
        return self._inner.force_flush(timeout_millis)


def init_analytics() -> None:
    """Create PostHog client from env. Safe to call once at startup."""
    global _client, _enabled
    from config import BOT_VERSION, POSTHOG_API_KEY, POSTHOG_HOST

    if not POSTHOG_API_KEY:
        _client = None
        _enabled = False
        logger.info("PostHog disabled (POSTHOG_API_KEY unset)")
        return
    try:
        from posthog import Posthog
    except ImportError:
        _client = None
        _enabled = False
        logger.warning("PostHog package missing; analytics disabled")
        return
    _client = Posthog(
        POSTHOG_API_KEY,
        host=POSTHOG_HOST,
        enable_exception_autocapture=False,
        before_send=_redact_event,
    )
    _enabled = True
    logger.info("PostHog enabled host=%s version=%s", POSTHOG_HOST, BOT_VERSION)
    _init_logs()


def _init_logs() -> None:
    """OTLP logs to PostHog: WARNING/ERROR only, service.name=twitch-telegram-bot."""
    global _logger_provider, _logs_handler
    if _logs_handler is not None:
        return
    from config import BOT_VERSION, POSTHOG_API_KEY, POSTHOG_HOST

    if not POSTHOG_API_KEY:
        return
    try:
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.semconv.resource import ResourceAttributes
    except ImportError:
        logger.warning("OpenTelemetry missing; PostHog logs disabled")
        return

    resource = Resource.create(
        {
            ResourceAttributes.SERVICE_NAME: _SERVICE_NAME,
            ResourceAttributes.SERVICE_VERSION: BOT_VERSION,
        }
    )
    provider = LoggerProvider(resource=resource)
    endpoint = f"{POSTHOG_HOST.rstrip('/')}/i/v1/logs"
    exporter = OTLPLogExporter(
        endpoint=endpoint,
        headers={"Authorization": f"Bearer {POSTHOG_API_KEY}"},
    )
    provider.add_log_record_processor(
        BatchLogRecordProcessor(_RedactingLogExporter(exporter))
    )
    set_logger_provider(provider)

    handler = LoggingHandler(level=logging.WARNING, logger_provider=provider)
    logging.getLogger().addHandler(handler)

    _logger_provider = provider
    _logs_handler = handler
    logger.info("PostHog logs enabled endpoint=%s level=WARNING+", endpoint)


def shutdown_analytics() -> None:
    global _client, _enabled, _logger_provider, _logs_handler
    if _logs_handler is not None:
        root = logging.getLogger()
        try:
            root.removeHandler(_logs_handler)
        except Exception:
            pass
        _logs_handler = None
    if _logger_provider is not None:
        try:
            _logger_provider.force_flush()
            _logger_provider.shutdown()
        except Exception:
            logger.exception("PostHog logs shutdown failed")
        _logger_provider = None
    if _client is None:
        _enabled = False
        return
    try:
        _client.flush()
        _client.shutdown()
    except Exception:
        logger.exception("PostHog shutdown failed")
    _client = None
    _enabled = False


def is_enabled() -> bool:
    return _enabled


def distinct_id(user_id: int) -> str:
    """Stable anonymized id — not the raw Telegram user id."""
    from config import TELEGRAM_BOT_TOKEN

    secret = (TELEGRAM_BOT_TOKEN or "local").encode()
    digest = hmac.new(secret, f"tg:{user_id}".encode(), hashlib.sha256).hexdigest()
    return f"tg_{digest[:32]}"


def capture(
    user_id: int | None,
    event: str,
    properties: dict[str, Any] | None = None,
    *,
    timestamp: Any | None = None,
) -> None:
    if not _enabled or _client is None:
        return
    from config import BOT_VERSION

    props = dict(properties or {})
    props.setdefault("bot_version", BOT_VERSION)
    props.setdefault("$process_person_profile", True)
    try:
        kwargs: dict[str, Any] = {"properties": props}
        if user_id is not None:
            kwargs["distinct_id"] = distinct_id(user_id)
        else:
            kwargs["distinct_id"] = "bot_system"
        if timestamp is not None:
            kwargs["timestamp"] = timestamp
        _client.capture(event, **kwargs)
    except Exception:
        logger.exception("PostHog capture failed event=%s", event)


def capture_exception(
    exc: BaseException | None,
    user_id: int | None = None,
    properties: dict[str, Any] | None = None,
) -> None:
    if not _enabled or _client is None or exc is None:
        return
    from config import BOT_VERSION

    props = dict(properties or {})
    props.setdefault("bot_version", BOT_VERSION)
    try:
        kwargs: dict[str, Any] = {"properties": props}
        if user_id is not None:
            kwargs["distinct_id"] = distinct_id(user_id)
        else:
            kwargs["distinct_id"] = "bot_system"
        _client.capture_exception(exc, **kwargs)
    except Exception:
        logger.exception("PostHog capture_exception failed")


def capture_bot_stats(stats: Any, *, timestamp: Any | None = None) -> None:
    """Snapshot admin BotStats fields as daily_bot_stats (distinct_id=bot_system)."""
    capture(
        None,
        "daily_bot_stats",
        {
            "users": int(stats.users),
            "notify_users": int(stats.notify_users),
            "unique_owners": int(stats.unique_owners),
            "subscriptions_total": int(stats.subscriptions_total),
            "subscriptions_enabled": int(stats.subscriptions_enabled),
            "subscriptions_disabled": int(stats.subscriptions_disabled),
            "unique_twitch_channels": int(stats.unique_twitch_channels),
            "premium_paid": int(stats.premium_paid),
            "blocked_users": int(stats.blocked_users),
            "sys_updates": int(stats.sys_updates),
            "sys_availability": int(stats.sys_availability),
            "sys_other": int(stats.sys_other),
            "locale_en": int(stats.locale_en),
            "locale_ru": int(stats.locale_ru),
            "locale_uk": int(stats.locale_uk),
            "locale_it": int(stats.locale_it),
            "locale_unset": int(stats.locale_unset),
        },
        timestamp=timestamp,
    )


POSTHOG_STATUS_URL = "https://www.posthogstatus.com/api/status"
POSTHOG_US_STATUS_PAGE_URL = "https://www.posthogstatus.com/us"
_US_CLOUD_PREFIX = "US Cloud"
# Only these US Cloud components trigger bot alerts (name match on status API).
POSTHOG_US_WATCHED_COMPONENTS = frozenset(
    {
        "App",
        "Logs",
        "Error Tracking",
        "Destination Delivery",
    }
)
_STATUS_RANK = {
    "operational": 0,
    "under_maintenance": 1,
    "degraded_performance": 2,
    "partial_outage": 3,
    "major_outage": 4,
}


def fetch_posthog_status(session: requests.Session | None = None) -> dict[str, Any]:
    """Fetch PostHog status JSON (posthogstatus.com)."""
    http = session if session is not None else requests
    response = http.get(
        POSTHOG_STATUS_URL,
        timeout=15,
        headers={"User-Agent": "twitch-telegram-bot/status-poll"},
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("PostHog status payload is not an object")
    return data


def _is_us_cloud_name(name: str) -> bool:
    return name.startswith(_US_CLOUD_PREFIX)


def _worst_status(statuses: list[str]) -> str:
    worst = "operational"
    worst_rank = 0
    for status in statuses:
        rank = _STATUS_RANK.get(status, 0)
        if rank > worst_rank:
            worst = status
            worst_rank = rank
    return worst


def posthog_us_snapshot(summary: dict[str, Any]) -> dict[str, Any]:
    """US Cloud slice of posthogstatus.com (watched components only)."""
    group = None
    for item in summary.get("component_groups") or []:
        if isinstance(item, dict) and _is_us_cloud_name(str(item.get("name") or "")):
            group = item
            break
    components: list[dict[str, Any]] = []
    if group:
        for comp in group.get("components") or []:
            if not isinstance(comp, dict):
                continue
            if str(comp.get("name") or "") not in POSTHOG_US_WATCHED_COMPONENTS:
                continue
            components.append(comp)
    us_ids = {str(comp.get("id") or "") for comp in components if comp.get("id")}
    incidents: list[dict[str, Any]] = []
    incident_statuses: list[str] = []
    for incident in summary.get("active_incidents") or []:
        if not isinstance(incident, dict):
            continue
        hit = False
        for affected in incident.get("affected_components") or []:
            if not isinstance(affected, dict):
                continue
            if str(affected.get("component_id") or "") not in us_ids:
                continue
            hit = True
            incident_statuses.append(str(affected.get("status") or "operational"))
        if hit:
            incidents.append(incident)
    overall = _worst_status(
        [str(comp.get("status") or "operational") for comp in components]
        + incident_statuses
    )
    return {"overall": overall, "components": components, "incidents": incidents}


def posthog_us_fingerprint(
    summary: dict[str, Any],
) -> tuple[str, tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]:
    """Comparable US Cloud snapshot: overall, components, active incidents."""
    snapshot = posthog_us_snapshot(summary)
    components = [
        (str(comp.get("id") or ""), str(comp.get("status") or "operational"))
        for comp in snapshot["components"]
        if comp.get("id")
    ]
    components.sort(key=lambda item: item[0])
    incidents = [
        (str(incident.get("id") or ""), str(incident.get("status") or ""))
        for incident in snapshot["incidents"]
        if incident.get("id")
    ]
    incidents.sort(key=lambda item: item[0])
    return snapshot["overall"], tuple(components), tuple(incidents)


def _redaction_self_check() -> None:
    """An exception carrying a fake credential must export with no secret value."""
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor
    from opentelemetry.sdk.resources import Resource

    secret = "123456:FAKEtokenVALUE000000"  # not a real credential
    captured: list[Any] = []

    class _Capture:
        def export(self, batch: Any) -> None:
            captured.extend(batch)

        def shutdown(self) -> None:
            pass

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return True

    provider = LoggerProvider(resource=Resource.create({"service.name": _SERVICE_NAME}))
    provider.add_log_record_processor(
        SimpleLogRecordProcessor(_RedactingLogExporter(_Capture()))
    )
    probe = logging.getLogger("analytics._redaction_probe")
    probe.propagate = False
    probe.setLevel(logging.WARNING)
    handler = LoggingHandler(level=logging.WARNING, logger_provider=provider)
    probe.addHandler(handler)
    try:
        raise ValueError(f"The token `{secret}` was rejected by the server.")
    except ValueError:
        probe.error("Network Retry Loop (Bootstrap): Invalid token.", exc_info=True)
    provider.force_flush()
    probe.removeHandler(handler)

    assert captured, "redaction probe exported nothing"
    record = captured[0].log_record
    blob = (record.body or "") + "".join(
        str(value) for value in (record.attributes or {}).values()
    )
    assert secret not in blob, "credential survived log redaction"
    assert "[redacted]" in blob, "redaction placeholder missing"

    # Error-tracking / analytics path: before_send scrubs the event too.
    event = {
        "event": "$exception",
        "distinct_id": "bot_system",
        "properties": {
            "$exception_list": [
                {
                    "type": "InvalidToken",
                    "value": f"The token `{secret}` was rejected by the server.",
                }
            ]
        },
    }
    cleaned_value = _redact_event(event)["properties"]["$exception_list"][0]["value"]
    assert secret not in cleaned_value, "credential survived event redaction"
    assert "[redacted]" in cleaned_value, "event redaction placeholder missing"


def _self_check() -> None:
    assert distinct_id(1) == distinct_id(1)
    assert distinct_id(1) != distinct_id(2)
    assert distinct_id(1).startswith("tg_")
    assert len(distinct_id(1)) == 35
    # No client → no crash
    capture(1, "self_check_noop")
    capture_exception(RuntimeError("self_check"), user_id=1)

    class _Stats:
        users = 1
        notify_users = 1
        unique_owners = 1
        subscriptions_total = 2
        subscriptions_enabled = 1
        subscriptions_disabled = 1
        unique_twitch_channels = 1
        premium_paid = 0
        blocked_users = 0
        sys_updates = 1
        sys_availability = 1
        sys_other = 1
        locale_en = 0
        locale_ru = 1
        locale_uk = 0
        locale_it = 0
        locale_unset = 0

    capture_bot_stats(_Stats())

    _redaction_self_check()

    status_ok = {
        "component_groups": [
            {
                "name": "US Cloud \U0001f1fa\U0001f1f8",
                "components": [
                    {"id": "us-app", "name": "App", "status": "operational"},
                    {"id": "us-logs", "name": "Logs", "status": "operational"},
                    {
                        "id": "us-query",
                        "name": "Querying",
                        "status": "major_outage",
                    },
                ],
            },
            {
                "name": "EU Cloud \U0001f1ea\U0001f1fa",
                "components": [
                    {"id": "eu-app", "name": "App", "status": "major_outage"},
                ],
            },
        ],
        "active_incidents": [
            {
                "id": "eu-only",
                "name": "EU App down",
                "status": "investigating",
                "affected_components": [
                    {
                        "component_id": "eu-app",
                        "group_name": "EU Cloud \U0001f1ea\U0001f1fa",
                        "status": "major_outage",
                    }
                ],
            },
            {
                "id": "us-query-only",
                "name": "Querying down",
                "status": "investigating",
                "affected_components": [
                    {
                        "component_id": "us-query",
                        "group_name": "US Cloud \U0001f1fa\U0001f1f8",
                        "status": "major_outage",
                    }
                ],
            },
        ],
    }
    status_bad = {
        "component_groups": [
            {
                "name": "US Cloud \U0001f1fa\U0001f1f8",
                "components": [
                    {"id": "us-app", "name": "App", "status": "partial_outage"},
                    {"id": "us-logs", "name": "Logs", "status": "operational"},
                    {
                        "id": "us-query",
                        "name": "Querying",
                        "status": "major_outage",
                    },
                ],
            },
            status_ok["component_groups"][1],
        ],
        "active_incidents": [
            {
                "id": "us-inc",
                "name": "App partial outage",
                "status": "investigating",
                "affected_components": [
                    {
                        "component_id": "us-app",
                        "group_name": "US Cloud \U0001f1fa\U0001f1f8",
                        "status": "partial_outage",
                    }
                ],
            }
        ],
    }
    fp_ok = posthog_us_fingerprint(status_ok)
    fp_bad = posthog_us_fingerprint(status_bad)
    assert fp_ok[0] == "operational"
    assert ("us-app", "operational") in fp_ok[1]
    assert ("us-logs", "operational") in fp_ok[1]
    assert all(item[0] != "eu-app" for item in fp_ok[1])
    assert all(item[0] != "us-query" for item in fp_ok[1])
    assert fp_ok[2] == ()
    assert fp_ok != fp_bad
    assert fp_bad[0] == "partial_outage"
    assert ("us-inc", "investigating") in fp_bad[2]
    print("analytics ok")


if __name__ == "__main__":
    _self_check()
