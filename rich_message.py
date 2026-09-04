"""Bot API rich messages via PTB do_api_request (until PTB types land)."""
from __future__ import annotations

import logging
from typing import Any

from telegram import InlineKeyboardMarkup, Message
from telegram.error import BadRequest, TelegramError

logger = logging.getLogger(__name__)

# None = not probed; True/False after first success/unsupported failure.
_rich_ok: bool | None = None


def rich_messages_known_ok() -> bool | None:
    return _rich_ok


def _is_rich_unsupported_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    needles = (
        "unknown method",
        "method not found",
        "there is no method",
        "unsupported",
        "can't parse inputrichmessage",
        "can't parse rich",
    )
    return any(n in text for n in needles)


def _markup_dict(reply_markup: InlineKeyboardMarkup | None) -> dict | None:
    if reply_markup is None:
        return None
    return reply_markup.to_dict()


async def send_rich_message(
    bot,
    chat_id: int,
    rich_message: dict[str, Any],
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message | dict | None:
    """Send via sendRichMessage. Returns None if rich API is unavailable."""
    global _rich_ok
    if _rich_ok is False:
        return None
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "rich_message": rich_message,
    }
    markup = _markup_dict(reply_markup)
    if markup is not None:
        payload["reply_markup"] = markup
    try:
        result = await bot.do_api_request(
            "sendRichMessage",
            api_kwargs=payload,
            return_type=Message,
        )
        # AsyncMock doubles look truthy and even int()-able — require a real shape.
        if isinstance(result, Message):
            mid = result.message_id
        elif isinstance(result, dict):
            mid = result.get("message_id")
        else:
            return None
        if mid is None or type(mid) is not int:
            return None
        _rich_ok = True
        return result
    except (BadRequest, TelegramError) as exc:
        if _is_rich_unsupported_error(exc):
            logger.info("sendRichMessage unavailable: %s", exc)
            _rich_ok = False
            return None
        raise


async def edit_rich_message(
    bot,
    *,
    chat_id: int,
    message_id: int,
    rich_message: dict[str, Any],
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Edit message as rich_message. False if rich edit unavailable."""
    global _rich_ok
    if _rich_ok is False:
        return False
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "rich_message": rich_message,
    }
    markup = _markup_dict(reply_markup)
    if markup is not None:
        payload["reply_markup"] = markup
    try:
        result = await bot.do_api_request("editMessageText", api_kwargs=payload)
        # Real API returns True or Message; reject AsyncMock doubles.
        if result is True or isinstance(result, Message):
            _rich_ok = True
            return True
        if isinstance(result, dict) and result.get("message_id") is not None:
            _rich_ok = True
            return True
        return False
    except BadRequest as exc:
        if "not modified" in str(exc).lower():
            return True
        if _is_rich_unsupported_error(exc):
            logger.info("editMessageText(rich_message) unavailable: %s", exc)
            _rich_ok = False
            return False
        raise
    except TelegramError as exc:
        if _is_rich_unsupported_error(exc):
            _rich_ok = False
            return False
        raise


def reset_rich_support_for_tests() -> None:
    global _rich_ok
    _rich_ok = None
