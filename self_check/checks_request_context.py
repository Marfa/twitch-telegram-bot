"""One record per handler run: fields, outcome, exception path, request id."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ApplicationHandlerStop, ConversationHandler, MessageHandler, TypeHandler
from telegram.ext import filters

import analytics
import request_context


class _FakeApp:
    def __init__(self, handlers: dict[int, list]) -> None:
        self.handlers = handlers


def _update() -> SimpleNamespace:
    return SimpleNamespace(
        update_id=4242,
        effective_user=SimpleNamespace(id=7),
        effective_chat=SimpleNamespace(id=-100, type=ChatType.PRIVATE),
        message=SimpleNamespace(),
        callback_query=None,
    )


def _run(callbacks: list, update) -> None:
    for callback in callbacks:
        asyncio.run(callback(update, None))


def check_request_context_record() -> None:
    seen: dict[str, dict] = {}

    async def ok_handler(update, context) -> None:
        request_context.annotate(subscription_id=99)
        analytics.capture(7, "inside_handler", {})

    handler = MessageHandler(filters.ALL, ok_handler)
    app = _FakeApp({0: [handler]})
    request_context.instrument(app)

    client = MagicMock()
    saved_enabled, saved_client = analytics._enabled, analytics._client
    analytics._enabled, analytics._client = True, client
    try:
        _run([handler.callback], _update())
    finally:
        analytics._enabled, analytics._client = saved_enabled, saved_client

    for call in client.capture.call_args_list:
        seen[call.args[0]] = call.kwargs["properties"]

    record = seen["telegram_handler"]
    assert record["request_id"] == "u4242", record
    assert record["outcome"] == "ok", record
    assert record["update_kind"] == "message", record
    assert record["handler_group"] == 0, record
    assert record["chat_id"] == -100 and record["chat_type"] == "private", record
    assert record["subscription_id"] == 99, record
    assert record["duration_ms"] >= 0, record
    assert seen["inside_handler"]["request_id"] == "u4242", seen
    assert request_context.current_request_id() is None


def check_request_context_outcomes() -> None:
    async def failing_handler(update, context) -> None:
        raise ValueError("boom")

    async def stopping_handler(update, context) -> None:
        raise ApplicationHandlerStop

    handler = MessageHandler(filters.ALL, failing_handler)
    stopper = MessageHandler(filters.ALL, stopping_handler)
    app = _FakeApp({0: [handler], 3: [stopper]})
    request_context.instrument(app)

    client = MagicMock()
    saved_enabled, saved_client = analytics._enabled, analytics._client
    analytics._enabled, analytics._client = True, client
    try:
        try:
            _run([handler.callback], _update())
        except ValueError:
            pass
        else:
            raise AssertionError("handler exception must reach the error handler")
        try:
            _run([stopper.callback], _update())
        except ApplicationHandlerStop:
            pass
        else:
            raise AssertionError("ApplicationHandlerStop must keep stopping the update")
    finally:
        analytics._enabled, analytics._client = saved_enabled, saved_client

    outcomes = [c.kwargs["properties"]["outcome"] for c in client.capture.call_args_list]
    assert outcomes == ["error:ValueError", "stopped"], outcomes


def check_request_context_conversation() -> None:
    async def step(update, context) -> None:
        return None

    entry = MessageHandler(filters.TEXT, step)
    state = MessageHandler(filters.TEXT, step)
    fallback = MessageHandler(filters.ALL, step)
    conv = ConversationHandler(
        entry_points=[entry], states={1: [state]}, fallbacks=[fallback]
    )
    type_handler = TypeHandler(Update, step)
    app = _FakeApp({1: [conv], -3: [type_handler]})
    request_context.instrument(app)
    request_context.instrument(app)  # idempotent: a second pass must not nest wrappers

    assert getattr(conv.entry_points[0].callback, "_request_context", False)
    assert getattr(conv.fallbacks[0].callback, "_request_context", False)
    assert getattr(conv.states[1][0].callback, "_request_context", False)
    assert getattr(type_handler.callback, "_request_context", False)


def run() -> None:
    check_request_context_record()
    check_request_context_outcomes()
    check_request_context_conversation()
