"""Group vs private chat discipline: setup gate, reply targets, PostHog context."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from telegram.constants import ChatType
from telegram.ext import ConversationHandler

_ROOT = Path(__file__).resolve().parents[1]


def _check_premium_and_helpers() -> None:
    from bot_helpers import (
        GROUP_SETUP_CALLBACK_PATTERN,
        chat_context_properties,
        group_setup_menu_filter,
        is_private_chat,
        reply_chat_id,
    )
    from chat_webapp import stream_chat_open_markup

    premium_src = (_ROOT / "premium_handlers.py").read_text(encoding="utf-8")
    assert "reply_chat_id(update)" in premium_src

    uid = 42
    group_id = -100999
    msg_update = SimpleNamespace(
        effective_user=SimpleNamespace(id=uid),
        effective_chat=SimpleNamespace(id=group_id, type=ChatType.SUPERGROUP),
        effective_message=MagicMock(),
        callback_query=None,
    )
    assert reply_chat_id(msg_update) == group_id
    assert not is_private_chat(msg_update)
    props = chat_context_properties(msg_update)
    assert props["chat_id"] == group_id
    assert props["chat_type"] == str(ChatType.SUPERGROUP)

    priv_btn = stream_chat_open_markup("ru", "https://example.com/app/chat/", private=True)
    group_btn = stream_chat_open_markup("ru", "https://example.com/app/chat/", private=False)
    assert priv_btn.inline_keyboard[0][0].web_app is not None
    assert group_btn.inline_keyboard[0][0].url
    assert group_setup_menu_filter() is not None
    assert GROUP_SETUP_CALLBACK_PATTERN.startswith("^")
    error_src = (_ROOT / "bot.py").read_text(encoding="utf-8")
    assert "chat_context_properties(update)" in error_src


async def _check_group_setup_gate() -> None:
    from bot_helpers import (
        dm_only_conv_entry,
        handle_group_setup_rejection,
        reply_setup_private_only,
        reset_group_setup_notified,
        should_send_group_setup_hint,
    )
    from unittest.mock import patch

    reset_group_setup_notified()
    group_id = -1001
    assert should_send_group_setup_hint(group_id, 900002)
    assert not should_send_group_setup_hint(group_id, 900002)
    assert should_send_group_setup_hint(group_id, 900003)
    with patch("bot_helpers._is_admin", return_value=True):
        assert should_send_group_setup_hint(group_id, 900001)
        assert should_send_group_setup_hint(group_id, 900001)
    reset_group_setup_notified()

    async def _ok_handler(_update, _context):
        return ConversationHandler.END

    wrapped = dm_only_conv_entry(_ok_handler)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=900001),
        effective_chat=SimpleNamespace(id=-1001, type=ChatType.SUPERGROUP),
        effective_message=AsyncMock(),
        callback_query=None,
    )
    ctx = MagicMock()
    ctx.application.bot_data = {"db": MagicMock(get_user_locale=MagicMock(return_value="ru"))}
    state = await wrapped(update, ctx)
    assert state == ConversationHandler.END
    update.effective_message.reply_text.assert_awaited()

    update.effective_message.reply_text.reset_mock()
    update.effective_chat = SimpleNamespace(id=900001, type=ChatType.PRIVATE)
    state = await wrapped(update, ctx)
    assert state == ConversationHandler.END
    update.effective_message.reply_text.assert_not_awaited()

    cb_update = SimpleNamespace(
        effective_user=SimpleNamespace(id=900001),
        effective_chat=SimpleNamespace(id=-1001, type=ChatType.SUPERGROUP),
        effective_message=None,
        callback_query=SimpleNamespace(
            answer=AsyncMock(),
            message=SimpleNamespace(
                chat_id=-1001, chat=SimpleNamespace(type=ChatType.SUPERGROUP)
            ),
        ),
    )
    await reply_setup_private_only(cb_update, "ru")
    cb_update.callback_query.answer.assert_awaited()

    ctx = MagicMock()
    ctx.application.bot_data = {"db": MagicMock(get_user_locale=MagicMock(return_value="ru"))}
    group_update = SimpleNamespace(
        effective_user=SimpleNamespace(id=900002),
        effective_chat=SimpleNamespace(id=group_id, type=ChatType.SUPERGROUP),
        effective_message=AsyncMock(),
        callback_query=None,
    )
    reset_group_setup_notified()
    assert await handle_group_setup_rejection(group_update, ctx) is True
    group_update.effective_message.reply_text.assert_awaited_once()
    assert await handle_group_setup_rejection(group_update, ctx) is True
    group_update.effective_message.reply_text.assert_awaited_once()
    reset_group_setup_notified()


def _check_wizard_schedule_reply_targets() -> None:
    import re

    dm_target = re.compile(
        r"\.send_message\s*\(\s*"
        r"(?:user_id|owner_id|query\.from_user\.id|update\.effective_user\.id)\b"
    )
    pulse_dm = re.compile(
        r"_pulse_wizard_keyboard\s*\([^)]*,\s*"
        r"(?:user_id|update\.effective_user\.id)\b"
    )
    badrequest_dm = re.compile(
        r"except BadRequest:\s*\n\s*await context\.bot\.send_message\s*\(\s*"
        r"(?:user_id|owner_id|query\.from_user\.id|update\.effective_user\.id)\b"
    )

    wizard_src = (_ROOT / "handlers/wizard.py").read_text(encoding="utf-8")
    assert "reply_chat_id" in wizard_src
    assert not dm_target.search(wizard_src), "wizard.py: use reply_chat_id(update) as send_message chat"
    assert not pulse_dm.search(wizard_src)
    assert not badrequest_dm.search(wizard_src)

    sched_src = (_ROOT / "handlers/stream_schedule.py").read_text(encoding="utf-8")
    assert "reply_chat_id" in sched_src
    sched_handlers = sched_src.split("async def _complete_schedule_publish", 1)[0]
    assert not dm_target.search(sched_handlers), (
        "stream_schedule.py handlers: use reply_chat_id(update) as send_message chat"
    )
    assert not pulse_dm.search(sched_handlers)
    assert not badrequest_dm.search(sched_handlers)


def check_group_chat() -> None:
    _check_premium_and_helpers()
    _check_wizard_schedule_reply_targets()
    import asyncio

    asyncio.run(_check_group_setup_gate())
