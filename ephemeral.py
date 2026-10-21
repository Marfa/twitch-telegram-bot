"""Telegram ephemeral messages (Bot API 10.2+) via PTB api_kwargs / do_api_request.

PTB 22.x targets Bot API 10.0 — native types land later; until then use forward-compat.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from telegram.error import BadRequest, Forbidden

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EphemeralHandle:
    chat_id: int
    receiver_user_id: int
    ephemeral_message_id: int


def _coerce_ephemeral_id(raw: Any) -> int | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    return None


def message_ephemeral_id(message: Any) -> int | None:
    """Incoming/outgoing Message ephemeral id (native attr or api_kwargs)."""
    if message is None:
        return None
    # Avoid unittest.mock Auto-attrs (AsyncMock.ephemeral_message_id → MagicMock).
    raw = _coerce_ephemeral_id(getattr(message, "ephemeral_message_id", None))
    if raw is not None:
        return raw
    # PTB stores unknown fields in api_kwargs as mappingproxy — not a plain dict.
    api_kwargs = getattr(message, "api_kwargs", None)
    if isinstance(api_kwargs, Mapping):
        return _coerce_ephemeral_id(api_kwargs.get("ephemeral_message_id"))
    return None


def _ephemeral_params(
    receiver_user_id: int,
    *,
    callback_query_id: str | None = None,
    replace_callback_query_message: bool = False,
) -> dict[str, Any]:
    params: dict[str, Any] = {"receiver_user_id": int(receiver_user_id)}
    if callback_query_id:
        params["callback_query_id"] = str(callback_query_id)
        if replace_callback_query_message:
            params["replace_callback_query_message"] = True
    return params


async def send_ephemeral_text(
    bot,
    chat_id: int,
    text: str,
    receiver_user_id: int,
    *,
    reply_to_ephemeral_id: int | None = None,
    callback_query_id: str | None = None,
    replace_callback_query_message: bool = False,
    reply_markup=None,
    parse_mode: str | None = None,
    disable_web_page_preview: bool = False,
) -> EphemeralHandle | None:
    """Send a group/supergroup message visible only to receiver_user_id."""
    api_kwargs: dict[str, Any] = {
        "ephemeral_message_parameters": _ephemeral_params(
            receiver_user_id,
            callback_query_id=callback_query_id,
            replace_callback_query_message=replace_callback_query_message,
        ),
    }
    if reply_to_ephemeral_id is not None:
        api_kwargs["reply_parameters"] = {
            "ephemeral_message_id": int(reply_to_ephemeral_id),
        }
    send_kw: dict[str, Any] = {"api_kwargs": api_kwargs}
    if reply_markup is not None:
        send_kw["reply_markup"] = reply_markup
    if parse_mode is not None:
        send_kw["parse_mode"] = parse_mode
    if disable_web_page_preview:
        send_kw["disable_web_page_preview"] = True
    try:
        msg = await bot.send_message(chat_id, text, **send_kw)
    except (BadRequest, Forbidden) as exc:
        logger.info(
            "ephemeral sendMessage failed chat=%s user=%s: %s",
            chat_id,
            receiver_user_id,
            exc,
        )
        return None
    except Exception:
        logger.exception(
            "ephemeral sendMessage error chat=%s user=%s",
            chat_id,
            receiver_user_id,
        )
        return None
    eph_id = message_ephemeral_id(msg)
    if eph_id is None:
        logger.warning(
            "ephemeral sendMessage returned no ephemeral_message_id chat=%s",
            chat_id,
        )
        return None
    return EphemeralHandle(
        chat_id=int(chat_id),
        receiver_user_id=int(receiver_user_id),
        ephemeral_message_id=eph_id,
    )


async def edit_ephemeral_text(
    bot,
    handle: EphemeralHandle,
    text: str,
    *,
    reply_markup=None,
    parse_mode: str | None = None,
    disable_web_page_preview: bool = False,
) -> bool:
    """Replace ephemeral message text. Returns False on API failure."""
    api_kwargs: dict[str, Any] = {
        "chat_id": handle.chat_id,
        "receiver_user_id": handle.receiver_user_id,
        "ephemeral_message_id": handle.ephemeral_message_id,
        "text": text,
    }
    if parse_mode is not None:
        api_kwargs["parse_mode"] = parse_mode
    if disable_web_page_preview:
        api_kwargs["link_preview_options"] = {"is_disabled": True}
    if reply_markup is not None:
        api_kwargs["reply_markup"] = reply_markup
    try:
        await bot.do_api_request("editEphemeralMessageText", api_kwargs=api_kwargs)
        return True
    except (BadRequest, Forbidden) as exc:
        logger.info(
            "editEphemeralMessageText failed chat=%s eph=%s: %s",
            handle.chat_id,
            handle.ephemeral_message_id,
            exc,
        )
        return False
    except Exception:
        logger.exception(
            "editEphemeralMessageText error chat=%s eph=%s",
            handle.chat_id,
            handle.ephemeral_message_id,
        )
        return False


async def delete_ephemeral(bot, handle: EphemeralHandle) -> bool:
    api_kwargs = {
        "chat_id": handle.chat_id,
        "receiver_user_id": handle.receiver_user_id,
        "ephemeral_message_id": handle.ephemeral_message_id,
    }
    try:
        await bot.do_api_request("deleteEphemeralMessage", api_kwargs=api_kwargs)
        return True
    except (BadRequest, Forbidden) as exc:
        logger.info(
            "deleteEphemeralMessage failed chat=%s eph=%s: %s",
            handle.chat_id,
            handle.ephemeral_message_id,
            exc,
        )
        return False
    except Exception:
        logger.exception(
            "deleteEphemeralMessage error chat=%s eph=%s",
            handle.chat_id,
            handle.ephemeral_message_id,
        )
        return False
