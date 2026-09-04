"""Typing indicator + progressive draft text for private sends.

Uses sendMessageDraft when available; falls back to a single classic send_message.
Bulk paths (broadcast / alerts) opt out via message_fx_disabled().
"""
from __future__ import annotations

import asyncio
import contextvars
import html as html_lib
import logging
import random
import re
from collections.abc import Iterator
from contextlib import contextmanager

from telegram.constants import ChatAction

logger = logging.getLogger(__name__)

_fx_disabled: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "message_fx_disabled", default=False
)

# Skip stream for tiny prompts; keep total animation short.
_MIN_STREAM_CHARS = 40
_MAX_STEPS = 10
_STEP_DELAY_S = 0.035
_TAG_RE = re.compile(r"<[^>]+>")


@contextmanager
def message_fx_disabled() -> Iterator[None]:
    """Disable typing/draft for the current task (broadcasts, alerts)."""
    token = _fx_disabled.set(True)
    try:
        yield
    finally:
        _fx_disabled.reset(token)


def _private_chat_id(chat_id: object) -> bool:
    try:
        return int(chat_id) > 0
    except (TypeError, ValueError):
        return False


def _plain_for_draft(text: str, *, parse_mode: object | None) -> str:
    if parse_mode:
        return html_lib.unescape(_TAG_RE.sub("", text))
    return text


def growing_prefixes(text: str) -> list[str]:
    """Cumulative prefixes for draft animation (excludes the full final string)."""
    if len(text) < _MIN_STREAM_CHARS:
        return []
    step = max(12, (len(text) + _MAX_STEPS - 1) // _MAX_STEPS)
    out: list[str] = []
    pos = step
    while pos < len(text) and len(out) < _MAX_STEPS:
        cut = text.rfind(" ", 0, pos)
        if cut < pos // 2:
            cut = pos
        prefix = text[:cut].rstrip()
        if prefix and (not out or prefix != out[-1]):
            out.append(prefix)
        pos = max(cut, pos) + step
    return out


async def send_typing(
    bot, chat_id: object, message_thread_id: int | None = None
) -> None:
    kwargs: dict = {"chat_id": chat_id, "action": ChatAction.TYPING}
    if message_thread_id is not None:
        kwargs["message_thread_id"] = message_thread_id
    try:
        await bot.send_chat_action(**kwargs)
    except Exception:
        logger.debug("send_chat_action(typing) failed for %s", chat_id, exc_info=True)


async def send_message_draft(
    bot,
    *,
    chat_id: object,
    draft_id: int,
    text: str,
    message_thread_id: int | None = None,
) -> bool:
    if hasattr(bot, "send_message_draft"):
        kwargs: dict = {
            "chat_id": chat_id,
            "draft_id": draft_id,
            "text": text,
        }
        if message_thread_id is not None:
            kwargs["message_thread_id"] = message_thread_id
        return bool(await bot.send_message_draft(**kwargs))
    data: dict = {"chat_id": chat_id, "draft_id": draft_id, "text": text}
    if message_thread_id is not None:
        data["message_thread_id"] = message_thread_id
    return bool(await bot._post("sendMessageDraft", data))


async def stream_draft_then(
    bot,
    chat_id: object,
    text: str,
    *,
    parse_mode: object | None = None,
    message_thread_id: int | None = None,
) -> bool:
    """Animate draft growth. Returns False if streaming was skipped or failed."""
    plain = _plain_for_draft(text, parse_mode=parse_mode)
    prefixes = growing_prefixes(plain)
    if not prefixes:
        return False
    draft_id = random.randint(1, 2_147_483_647)
    try:
        for partial in prefixes:
            await send_message_draft(
                bot,
                chat_id=chat_id,
                draft_id=draft_id,
                text=partial,
                message_thread_id=message_thread_id,
            )
            await asyncio.sleep(_STEP_DELAY_S)
        return True
    except Exception as exc:
        logger.debug("sendMessageDraft fallback to classic: %s", exc)
        return False


def _extract_send_args(
    args: tuple, kwargs: dict
) -> tuple[object | None, str | None, int | None, object | None]:
    chat_id = args[0] if args else kwargs.get("chat_id")
    text = args[1] if len(args) > 1 else kwargs.get("text")
    thread_id = kwargs.get("message_thread_id")
    parse_mode = kwargs.get("parse_mode")
    return chat_id, text if isinstance(text, str) else None, thread_id, parse_mode


def install_message_fx(
    bot,
    *,
    draft_enabled=None,
) -> None:
    """Wrap bot.send_message: typing + draft stream in private chats, else classic.

    draft_enabled: optional ``(user_id: int) -> bool``; default treats draft as on.
    """
    if getattr(bot, "_message_fx_installed", False):
        return
    original = bot.send_message

    def _draft_on(chat_id: object) -> bool:
        if draft_enabled is None:
            return True
        try:
            return bool(draft_enabled(int(chat_id)))
        except (TypeError, ValueError):
            return True

    async def send_message(*args, **kwargs):
        if _fx_disabled.get():
            return await original(*args, **kwargs)

        chat_id, text, thread_id, parse_mode = _extract_send_args(args, kwargs)
        if text is None or not _private_chat_id(chat_id):
            return await original(*args, **kwargs)

        if not _draft_on(chat_id):
            return await original(*args, **kwargs)

        # Entities-only formatting is too brittle to preview mid-string.
        if kwargs.get("entities"):
            await send_typing(bot, chat_id, thread_id)
            return await original(*args, **kwargs)

        await send_typing(bot, chat_id, thread_id)
        await stream_draft_then(
            bot,
            chat_id,
            text,
            parse_mode=parse_mode,
            message_thread_id=thread_id,
        )
        return await original(*args, **kwargs)

    # ExtBot freezes attribute assignment (TelegramObject.__setattr__).
    object.__setattr__(bot, "send_message", send_message)
    object.__setattr__(bot, "_message_fx_installed", True)
