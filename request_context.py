"""One PostHog record per Telegram handler run, joinable by a request id.

An inbound update carries no request id of its own, so every handler callback is
wrapped once at startup. The wrapper times the run, records the outcome, and
exposes the id plus any annotated business fields to the analytics calls that run
inside it.
"""
from __future__ import annotations

import contextvars
import functools
import logging
import time
from typing import Any, Awaitable, Callable

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    BaseHandler,
    ConversationHandler,
)

logger = logging.getLogger(__name__)

_UPDATE_KINDS = (
    "callback_query",
    "message",
    "edited_message",
    "channel_post",
    "inline_query",
    "chat_member",
    "my_chat_member",
    "pre_checkout_query",
    "poll_answer",
)

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)
_fields: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "request_fields", default=None
)


def current_request_id() -> str | None:
    """Id of the update handled by this task, or None outside a handler."""
    return _request_id.get()


def annotate(**fields: Any) -> None:
    """Attach business fields to the record of the handler now running."""
    bucket = _fields.get()
    if bucket is not None:
        bucket.update(fields)


def instrument(app: Application) -> None:
    """Wrap every handler registered on app. Call once, after all registrations."""
    for group, handlers in app.handlers.items():
        for handler in handlers:
            _instrument_handler(handler, group)


def _instrument_handler(handler: BaseHandler, group: int) -> None:
    if isinstance(handler, ConversationHandler):
        inner = [*handler.entry_points, *handler.fallbacks]
        for steps in handler.states.values():
            inner.extend(steps)
        for item in inner:
            _instrument_handler(item, group)
        return
    callback = getattr(handler, "callback", None)
    if callback is None or getattr(callback, "_request_context", False):
        return
    handler.callback = _wrap(callback, group)


def _wrap(
    callback: Callable[..., Awaitable[Any]], group: int
) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(callback)
    async def wrapper(update: Update, context: Any) -> Any:
        record_id = f"u{update.update_id}"
        fields: dict[str, Any] = {}
        request_token = _request_id.set(record_id)
        fields_token = _fields.set(fields)
        started = time.perf_counter()
        outcome = "ok"
        try:
            return await callback(update, context)
        except ApplicationHandlerStop:
            outcome = "stopped"
            raise
        except Exception as exc:
            outcome = f"error:{type(exc).__name__}"
            raise
        finally:
            _record(update, callback, group, record_id, fields, outcome, started)
            _fields.reset(fields_token)
            _request_id.reset(request_token)

    wrapper._request_context = True
    return wrapper


def _record(
    update: Update,
    callback: Callable[..., Awaitable[Any]],
    group: int,
    record_id: str,
    fields: dict[str, Any],
    outcome: str,
    started: float,
) -> None:
    import analytics
    from bot_helpers import chat_context_properties

    user = update.effective_user
    properties: dict[str, Any] = {
        "request_id": record_id,
        "handler": f"{type(callback).__name__}:{getattr(callback, '__name__', '?')}",
        "handler_group": group,
        "update_kind": _update_kind(update),
        "outcome": outcome,
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        **chat_context_properties(update),
        **fields,
    }
    analytics.capture(
        user.id if user is not None else None, "telegram_handler", properties
    )


def _update_kind(update: Update) -> str:
    for kind in _UPDATE_KINDS:
        if getattr(update, kind, None) is not None:
            return kind
    return "other"
