"""PostHog product analytics + error tracking + WARNING/ERROR logs (no-op when unset)."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from typing import Any

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


class _WarningPlusRedactFilter(logging.Filter):
    """Only WARNING+; scrub token-like substrings from the rendered message."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.WARNING:
            return False
        try:
            text = record.getMessage()
        except Exception:
            return True
        redacted = _SECRET_RE.sub(r"\1\2[redacted]", text)
        if redacted != text:
            record.msg = redacted
            record.args = ()
        return True


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
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    set_logger_provider(provider)

    handler = LoggingHandler(level=logging.WARNING, logger_provider=provider)
    handler.addFilter(_WarningPlusRedactFilter())
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
            "locale_unset": int(stats.locale_unset),
        },
        timestamp=timestamp,
    )


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
        locale_unset = 0

    capture_bot_stats(_Stats())

    f = _WarningPlusRedactFilter()
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "ok", (), None)
    assert f.filter(rec) is False
    rec2 = logging.LogRecord(
        "t",
        logging.WARNING,
        __file__,
        1,
        "token=supersecrettokenvalue",
        (),
        None,
    )
    assert f.filter(rec2) is True
    assert "[redacted]" in rec2.getMessage()
    assert "supersecret" not in rec2.getMessage()
    print("analytics ok")


if __name__ == "__main__":
    _self_check()
