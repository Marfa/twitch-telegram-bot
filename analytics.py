"""PostHog product analytics + error tracking (no-op when unset)."""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

logger = logging.getLogger(__name__)

_client = None
_enabled = False


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


def shutdown_analytics() -> None:
    global _client, _enabled
    if _client is None:
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


def _self_check() -> None:
    assert distinct_id(1) == distinct_id(1)
    assert distinct_id(1) != distinct_id(2)
    assert distinct_id(1).startswith("tg_")
    assert len(distinct_id(1)) == 35
    # No client → no crash
    capture(1, "self_check_noop")
    capture_exception(RuntimeError("self_check"), user_id=1)
    print("analytics ok")


if __name__ == "__main__":
    _self_check()
