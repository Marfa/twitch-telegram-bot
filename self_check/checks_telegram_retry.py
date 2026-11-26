"""A Telegram 429 waits the requested time and repeats the call exactly once.

Connect-class failures also retry once; read TimedOut does not.
"""
import asyncio
import datetime as dtm

from telegram.error import NetworkError, RetryAfter, TimedOut

import telegram_circuit
import telegram_retry


def run() -> None:
    telegram_circuit.reset_for_tests()
    assert telegram_retry.retry_after_seconds(RetryAfter(5)) == 5
    assert telegram_retry.retry_after_seconds(RetryAfter(dtm.timedelta(seconds=7))) == 7

    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    class FakeBot:
        def __init__(self, errors: list[BaseException]) -> None:
            self.errors = list(errors)
            self.calls = 0

    async def original(self, endpoint, data=None, *args, **kwargs):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return {"endpoint": endpoint, "data": data}

    wrapped = telegram_retry.make_retry_after_do_post(original, sleep=fake_sleep)

    # One 429 inside the bound: wait the requested time, then repeat once.
    telegram_circuit.reset_for_tests()
    bot = FakeBot([RetryAfter(5)])
    assert asyncio.run(wrapped(bot, "sendMessage", {"chat_id": 1})) == {
        "endpoint": "sendMessage",
        "data": {"chat_id": 1},
    }
    assert bot.calls == 2, f"expected one retry, got {bot.calls} calls"
    assert waits == [5.5], f"expected the requested 5 s wait, got {waits}"
    assert not telegram_circuit.is_open()

    # A wait above the bound is a real flood: no retry, the error reaches error_handler.
    waits.clear()
    telegram_circuit.reset_for_tests()
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
    telegram_circuit.reset_for_tests()
    bot = FakeBot([RetryAfter(5), RetryAfter(5)])
    try:
        asyncio.run(wrapped(bot, "sendMessage", {}))
        raise AssertionError("expected the second RetryAfter to propagate")
    except RetryAfter as exc:
        assert exc.telegram_endpoint == "sendMessage", "the escaping error must name the endpoint"
    assert bot.calls == 2, f"expected exactly one retry, got {bot.calls} calls"

    # Read TimedOut: no immediate retry (ambiguous delivery).
    waits.clear()
    telegram_circuit.reset_for_tests()
    bot = FakeBot([TimedOut()])
    try:
        asyncio.run(wrapped(bot, "sendMessage", {}))
        raise AssertionError("expected TimedOut to propagate")
    except TimedOut:
        pass
    assert bot.calls == 1 and waits == [], "read TimedOut must not wait or retry"

    # ConnectError: one immediate retry, then success.
    waits.clear()
    telegram_circuit.reset_for_tests()
    bot = FakeBot([NetworkError("httpx.ConnectError")])
    assert asyncio.run(wrapped(bot, "sendMessage", {"chat_id": 2})) == {
        "endpoint": "sendMessage",
        "data": {"chat_id": 2},
    }
    assert bot.calls == 2, f"expected one connect retry, got {bot.calls} calls"
    assert waits == [], "connect retry must not sleep like RetryAfter"
    assert not telegram_circuit.is_open()

    # TimedOut whose cause is ConnectTimeout: one retry.
    waits.clear()
    telegram_circuit.reset_for_tests()
    connect_timeout = TimedOut("Timed out")
    connect_timeout.__cause__ = type("ConnectTimeout", (Exception,), {})()
    bot = FakeBot([connect_timeout])
    assert asyncio.run(wrapped(bot, "getMe", None)) == {
        "endpoint": "getMe",
        "data": None,
    }
    assert bot.calls == 2, f"expected ConnectTimeout retry, got {bot.calls} calls"

    # Two consecutive transport failures open the circuit.
    telegram_circuit.reset_for_tests()
    bot = FakeBot([TimedOut(), TimedOut()])
    try:
        asyncio.run(wrapped(bot, "sendMessage", {}))
    except TimedOut:
        pass
    assert not telegram_circuit.is_open(), "one failure must not open yet"
    bot = FakeBot([TimedOut()])
    try:
        asyncio.run(wrapped(bot, "sendMessage", {}))
    except TimedOut:
        pass
    assert telegram_circuit.is_open(), "two consecutive transport failures open the circuit"
    telegram_circuit.reset_for_tests()
