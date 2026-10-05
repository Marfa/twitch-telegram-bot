"""Unified long-running request indicator (icon + elapsed timer, no Stop).

Prefer sendRichMessageDraft with a <thinking> block (Telegram AIActions emoji).
Fall back to a classic editable status message when rich drafts are unavailable.
"""
from __future__ import annotations

import asyncio
import html
import logging
import random
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from telegram import Message
from telegram.error import BadRequest, TelegramError

from i18n import t
from message_fx import message_fx_disabled
from rich_message import send_rich_message_draft

logger = logging.getLogger(__name__)

_REFRESH_S = 2.0
_FALLBACK_EMOJI = "⏳"
# AIActions pack — first search-like animation (pack emoji field is mostly 🙂).
_DEFAULT_CUSTOM_EMOJI_ID = "5535034915403333642"
_AIACTIONS_SET = "AIActions"

# None = not loaded; "" = unavailable; otherwise custom_emoji_id string.
_progress_emoji_id: str | None = None


def format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def progress_plain_text(lang: str, elapsed_s: float) -> str:
    label = t("request_in_progress", lang)
    return f"{_FALLBACK_EMOJI} {label} {format_elapsed(elapsed_s)}"


def progress_thinking_html(
    lang: str, elapsed_s: float, *, custom_emoji_id: str | None
) -> str:
    label = html.escape(t("request_in_progress", lang))
    timer = format_elapsed(elapsed_s)
    if custom_emoji_id:
        icon = (
            f'<tg-emoji emoji-id="{html.escape(custom_emoji_id, quote=True)}">'
            f"{_FALLBACK_EMOJI}</tg-emoji>"
        )
    else:
        icon = _FALLBACK_EMOJI
    return f"<thinking>{icon} {label} {timer}</thinking>"


async def _resolve_progress_emoji_id(bot: Any) -> str | None:
    global _progress_emoji_id
    if _progress_emoji_id is not None:
        return _progress_emoji_id or None
    try:
        sticker_set = await bot.get_sticker_set(_AIACTIONS_SET)
        stickers = getattr(sticker_set, "stickers", None) or []
        preferred = ("🔎", "🔍", "⏳", "⌛", "🤔", "💭")
        by_emoji = {
            str(getattr(s, "emoji", "") or ""): str(
                getattr(s, "custom_emoji_id", "") or ""
            )
            for s in stickers
        }
        for em in preferred:
            eid = by_emoji.get(em)
            if eid:
                _progress_emoji_id = eid
                return eid
        for s in stickers:
            eid = str(getattr(s, "custom_emoji_id", "") or "")
            if eid:
                _progress_emoji_id = eid
                return eid
    except Exception:
        logger.debug("AIActions sticker set unavailable", exc_info=True)
    _progress_emoji_id = _DEFAULT_CUSTOM_EMOJI_ID
    return _progress_emoji_id


def reset_progress_emoji_for_tests() -> None:
    global _progress_emoji_id
    _progress_emoji_id = None


@asynccontextmanager
async def request_progress(
    bot: Any,
    chat_id: int,
    lang: str,
    *,
    message_thread_id: int | None = None,
) -> AsyncIterator[None]:
    """Show a single in-progress indicator until the block exits."""
    started = time.monotonic()
    draft_id = random.randint(1, 2_147_483_647)
    stop = asyncio.Event()
    fallback_msg: Message | None = None
    use_rich = True
    emoji_id = await _resolve_progress_emoji_id(bot)

    async def _push_rich(elapsed: float) -> bool:
        html_body = progress_thinking_html(
            lang, elapsed, custom_emoji_id=emoji_id
        )
        return await send_rich_message_draft(
            bot,
            chat_id=chat_id,
            draft_id=draft_id,
            rich_message={"html": html_body},
            can_stop=False,
            message_thread_id=message_thread_id,
        )

    async def _push_fallback(elapsed: float) -> None:
        nonlocal fallback_msg
        text = progress_plain_text(lang, elapsed)
        if fallback_msg is None:
            with message_fx_disabled():
                kwargs: dict[str, Any] = {"chat_id": chat_id, "text": text}
                if message_thread_id is not None:
                    kwargs["message_thread_id"] = message_thread_id
                fallback_msg = await bot.send_message(**kwargs)
            return
        try:
            await fallback_msg.edit_text(text)
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                logger.debug("progress fallback edit failed", exc_info=True)
        except TelegramError:
            logger.debug("progress fallback edit failed", exc_info=True)

    # Initial paint.
    try:
        if not await _push_rich(0.0):
            use_rich = False
            await _push_fallback(0.0)
    except Exception:
        logger.debug("rich progress draft failed; using classic", exc_info=True)
        use_rich = False
        await _push_fallback(0.0)

    async def _loop() -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=_REFRESH_S)
                return
            except asyncio.TimeoutError:
                pass
            elapsed = time.monotonic() - started
            try:
                if use_rich:
                    ok = await _push_rich(elapsed)
                    if not ok:
                        break
                else:
                    await _push_fallback(elapsed)
            except Exception:
                logger.debug("progress refresh failed", exc_info=True)
                break

    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        if fallback_msg is not None:
            try:
                await bot.delete_message(
                    chat_id=chat_id, message_id=fallback_msg.message_id
                )
            except Exception:
                logger.debug("progress fallback delete failed", exc_info=True)
