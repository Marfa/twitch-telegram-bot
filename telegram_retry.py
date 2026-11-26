"""Wait out a Telegram flood-control 429 and repeat the rejected call once.

Telegram rejects a call with HTTP 429 and asks the bot to wait
`parameters.retry_after` seconds before it repeats the call
(https://core.telegram.org/bots/api#making-requests). python-telegram-bot
raises `RetryAfter` from that response, and the rejected call delivers
nothing, so one repeat after the requested wait is safe.

Also retries once on pre-request connect failures (httpx ConnectError /
ConnectTimeout). Read TimedOut is not retried here — the request may already
have been accepted (duplicate risk).

`Bot._do_post` is the single funnel for every Bot API call, so this wrapper
covers the call sites that have no retry of their own. A wait above
`MAX_RETRY_AFTER_SECONDS` means the bot is flooding a chat for real: the
error goes back to `bot.error_handler`, which pages a person.

Successful / failed transport outcomes also feed ``telegram_circuit``.
"""

from __future__ import annotations

import asyncio
import datetime as dtm
import functools
import logging
from typing import Any, Awaitable, Callable

from telegram.error import BadRequest, RetryAfter, TelegramError

import telegram_circuit

logger = logging.getLogger(__name__)

# A longer wait means a real flood; fail instead of holding the caller for minutes.
MAX_RETRY_AFTER_SECONDS = 30.0
_patched = False

Sleeper = Callable[[float], Awaitable[Any]]


def retry_after_seconds(exc: BaseException) -> float:
    """Seconds Telegram asked the caller to wait, in either form of the value."""
    value = getattr(exc, "retry_after", 0)
    if isinstance(value, dtm.timedelta):
        return value.total_seconds()
    return float(value or 0)


def _tag_endpoint(exc: BaseException, endpoint: Any) -> None:
    """Name the rejected endpoint on the error that escapes to error_handler."""
    try:
        exc.telegram_endpoint = str(endpoint)  # type: ignore[attr-defined]
    except AttributeError:
        pass


def is_pre_request_transport_error(exc: BaseException) -> bool:
    """True when the request likely never reached Telegram processing."""
    if isinstance(exc, BadRequest):
        return False
    cause = getattr(exc, "__cause__", None)
    cause_name = type(cause).__name__ if cause is not None else ""
    if cause_name in ("ConnectError", "ConnectTimeout"):
        return True
    # Tests and some PTB paths raise NetworkError("httpx.ConnectError") without cause.
    msg = str(exc)
    if "ConnectTimeout" in msg or "ConnectError" in msg:
        if cause_name == "ReadTimeout":
            return False
        return True
    return False


def _note_final_failure(exc: BaseException) -> None:
    if telegram_circuit.is_transport_failure(exc):
        telegram_circuit.note_failure(exc)


def make_retry_after_do_post(original: Callable[..., Awaitable[Any]], sleep: Sleeper | None = None):
    """Wrap Bot API calls: one 429 wait-retry, one connect-failure retry."""
    sleeper: Sleeper = sleep or asyncio.sleep

    @functools.wraps(original)
    async def _do_post(self, endpoint, data=None, *args, **kwargs):
        retry_reason: str | None = None
        try:
            result = await original(self, endpoint, data, *args, **kwargs)
            telegram_circuit.note_success()
            return result
        except RetryAfter as exc:
            wait = retry_after_seconds(exc)
            if wait > MAX_RETRY_AFTER_SECONDS:
                _tag_endpoint(exc, endpoint)
                raise
            logger.warning(
                "Telegram flood control on %s: waiting %.0fs and retrying once",
                endpoint,
                wait,
            )
            await sleeper(wait + 0.5)
            retry_reason = "retry_after"
        except TelegramError as exc:
            if is_pre_request_transport_error(exc):
                logger.warning(
                    "Telegram connect failure on %s: retrying once (%s)",
                    endpoint,
                    exc,
                )
                retry_reason = "connect"
            else:
                _note_final_failure(exc)
                _tag_endpoint(exc, endpoint)
                raise
        try:
            result = await original(self, endpoint, data, *args, **kwargs)
            telegram_circuit.note_success()
            return result
        except TelegramError as exc:
            _note_final_failure(exc)
            _tag_endpoint(exc, endpoint)
            if retry_reason:
                logger.warning(
                    "Telegram %s retry on %s failed: %s",
                    retry_reason,
                    endpoint,
                    exc,
                )
            raise

    return _do_post


def install_telegram_retry_after() -> None:
    """Install the retry wrapper on Bot._do_post once per process."""
    global _patched
    if _patched:
        return
    from telegram import Bot

    Bot._do_post = make_retry_after_do_post(Bot._do_post)  # type: ignore[method-assign]
    _patched = True
