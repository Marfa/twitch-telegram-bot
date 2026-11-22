"""Wait out a Telegram flood-control 429 and repeat the rejected call once.

Telegram rejects a call with HTTP 429 and asks the bot to wait
`parameters.retry_after` seconds before it repeats the call
(https://core.telegram.org/bots/api#making-requests). python-telegram-bot
raises `RetryAfter` from that response, and the rejected call delivers
nothing, so one repeat after the requested wait is safe.

`Bot._do_post` is the single funnel for every Bot API call, so this wrapper
covers the call sites that have no retry of their own. A wait above
`MAX_RETRY_AFTER_SECONDS` means the bot is flooding a chat for real: the
error goes back to `bot.error_handler`, which pages a person.
"""

from __future__ import annotations

import asyncio
import datetime as dtm
import functools
import logging
from typing import Any, Awaitable, Callable

from telegram.error import RetryAfter, TelegramError

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


def make_retry_after_do_post(original: Callable[..., Awaitable[Any]], sleep: Sleeper | None = None):
    """Wrap a Bot API call so a 429 waits the requested time and retries once."""
    sleeper: Sleeper = sleep or asyncio.sleep

    @functools.wraps(original)
    async def _do_post(self, endpoint, data=None, *args, **kwargs):
        try:
            return await original(self, endpoint, data, *args, **kwargs)
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
        try:
            return await original(self, endpoint, data, *args, **kwargs)
        except TelegramError as exc:
            _tag_endpoint(exc, endpoint)
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
