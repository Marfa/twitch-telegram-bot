"""A Telegram 429 waits the requested time and repeats the call exactly once."""
import asyncio
import datetime as dtm

from telegram.error import RetryAfter, TimedOut

import telegram_retry


def run() -> None:
    assert telegram_retry.retry_after_seconds(RetryAfter(5)) == 5
    assert telegram_retry.retry_after_seconds(RetryAfter(dtm.timedelta(seconds=7))) == 7

    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    class FakeBot:
        def __init__(self, errors: list[BaseException]) -> None:
            self.errors = errors
            self.calls = 0

    async def original(self, endpoint, data=None, *args, **kwargs):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return {"endpoint": endpoint, "data": data}

    wrapped = telegram_retry.make_retry_after_do_post(original, sleep=fake_sleep)

    # One 429 inside the bound: wait the requested time, then repeat once.
    bot = FakeBot([RetryAfter(5)])
    assert asyncio.run(wrapped(bot, "sendMessage", {"chat_id": 1})) == {
        "endpoint": "sendMessage",
        "data": {"chat_id": 1},
    }
    assert bot.calls == 2, f"expected one retry, got {bot.calls} calls"
    assert waits == [5.5], f"expected the requested 5 s wait, got {waits}"

    # A wait above the bound is a real flood: no retry, the error reaches error_handler.
    waits.clear()
    bot = FakeBot([RetryAfter(300)])
    try:
        asyncio.run(wrapped(bot, "sendMessage", {}))
        raise AssertionError("expected RetryAfter above the bound to propagate")
    except RetryAfter:
        pass
    assert bot.calls == 1, f"expected no retry above the bound, got {bot.calls} calls"
    assert waits == [], f"expected no wait above the bound, got {waits}"

    # A second 429 after the retry propagates instead of looping, and names the endpoint.
    waits.clear()
    bot = FakeBot([RetryAfter(5), RetryAfter(5)])
    try:
        asyncio.run(wrapped(bot, "sendMessage", {}))
        raise AssertionError("expected the second RetryAfter to propagate")
    except RetryAfter as exc:
        assert exc.telegram_endpoint == "sendMessage", "the escaping error must name the endpoint"
    assert bot.calls == 2, f"expected exactly one retry, got {bot.calls} calls"

    # Every other Telegram error passes through untouched.
    waits.clear()
    bot = FakeBot([TimedOut()])
    try:
        asyncio.run(wrapped(bot, "sendMessage", {}))
        raise AssertionError("expected TimedOut to propagate")
    except TimedOut:
        pass
    assert bot.calls == 1 and waits == [], "non-429 errors must not wait or retry"
