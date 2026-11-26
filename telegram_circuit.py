"""In-process circuit breaker for Telegram Bot API transport failures.

Opens after consecutive TimedOut / connect-class NetworkError responses so alert
delivery can enqueue instead of hammering a down API. Closed again on any
successful Bot API call (including a getMe probe).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

logger = logging.getLogger(__name__)

FAILURES_TO_OPEN = 2
PROBE_MIN_INTERVAL_SEC = 30.0

_open = False
_failures = 0
_last_probe_monotonic = 0.0


def reset_for_tests() -> None:
    global _open, _failures, _last_probe_monotonic
    _open = False
    _failures = 0
    _last_probe_monotonic = 0.0


def is_open() -> bool:
    return _open


def allow_send() -> bool:
    return not _open


def is_transport_failure(exc: BaseException) -> bool:
    """True for ambiguous/connect transport errors — not app-level Telegram rejects."""
    if isinstance(exc, (BadRequest, Forbidden, RetryAfter)):
        return False
    if isinstance(exc, TimedOut):
        return True
    return isinstance(exc, NetworkError)


def note_success() -> None:
    global _open, _failures
    was_open = _open
    _failures = 0
    _open = False
    if was_open:
        logger.info("Telegram circuit closed after successful Bot API call")


def note_failure(exc: BaseException | None = None) -> None:
    global _open, _failures
    if exc is not None and not is_transport_failure(exc):
        return
    _failures += 1
    if _failures >= FAILURES_TO_OPEN and not _open:
        _open = True
        logger.warning(
            "Telegram circuit open after %s consecutive transport failure(s)",
            _failures,
        )
    elif _failures >= FAILURES_TO_OPEN:
        _open = True


async def probe_get_me(bot: Any, *, force: bool = False) -> bool:
    """If the circuit is open, try getMe. Returns True when Telegram looks up."""
    global _last_probe_monotonic
    if not _open:
        return True
    now = time.monotonic()
    if not force and (now - _last_probe_monotonic) < PROBE_MIN_INTERVAL_SEC:
        return False
    _last_probe_monotonic = now
    try:
        await bot.get_me()
    except Exception as exc:
        if is_transport_failure(exc):
            # getMe already counted via _do_post note_failure when patched;
            # keep circuit open either way.
            logger.info("Telegram circuit probe still down: %s", exc)
        else:
            logger.info("Telegram circuit probe failed: %s", exc)
        return False
    # Successful getMe goes through _do_post → note_success when patched.
    # If called without the patch (tests), close here.
    if _open:
        note_success()
    return True
