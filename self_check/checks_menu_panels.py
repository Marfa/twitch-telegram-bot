"""Inline Other panel callback wiring.

Contract: `menu_other:create_schedule|watch|game_info` are main-conversation
entry points and no handler in an earlier group answers them; the other
`menu_other:*` actions stay in the earlier group and are not entry points. A
conversation entry stores the wizard state that its next keyboard runs in.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import Message, Update, User
from telegram.ext import ConversationHandler, ExtBot

_SCHED_UID = 900001
_GAME_INFO_UID = 900002
_CONV_ACTIONS = ("create_schedule", "watch", "game_info")
_PANEL_ACTIONS = ("follow_monitor", "ai_clips", "whisper_alerts", "chat", "channel_stats")


def _callback_update(data: str, bot, user_id: int) -> Update:
    return Update.de_json(
        {
            "update_id": 1,
            "callback_query": {
                "id": f"cb:{data}",
                "chat_instance": "1",
                "data": data,
                "from": {"id": user_id, "is_bot": False, "first_name": "U"},
                "message": {
                    "message_id": 1,
                    "date": 1732000000,
                    "text": "other",
                    "chat": {"id": user_id, "type": "private"},
                    "from": {"id": 42, "is_bot": False, "first_name": "bot"},
                },
            },
        },
        bot,
    )


def _conv_group(app, conv) -> int:
    for group, handlers in app.handlers.items():
        if conv in handlers:
            return group
    raise AssertionError("main conversation is not registered")


def _matches(handlers, update) -> list:
    return [handler for handler in handlers if handler.check_update(update)]


def _patch_bot(sent: list):
    async def _reply(text, **kwargs):
        sent.append(text)
        return SimpleNamespace(message_id=len(sent) + 1)

    async def _send(chat_id, text, **kwargs):
        return await _reply(text)

    patchers = [
        patch.object(ExtBot, "send_message", AsyncMock(side_effect=_send)),
        patch.object(ExtBot, "send_chat_action", AsyncMock(return_value=True)),
        patch.object(ExtBot, "edit_message_text", AsyncMock(return_value=True)),
        patch.object(ExtBot, "edit_message_reply_markup", AsyncMock(return_value=True)),
        patch.object(ExtBot, "answer_callback_query", AsyncMock(return_value=True)),
        patch.object(ExtBot, "delete_message", AsyncMock(return_value=True)),
        patch.object(Message, "reply_text", AsyncMock(side_effect=_reply)),
    ]
    return patchers


async def _panel_entry(
    app, conv, data: str, user_id: int, expected_state: int, follow: str | None = None
) -> None:
    from i18n import t

    app._initialized = True
    app.bot._initialized = True
    app.bot._bot_user = User(id=42, first_name="bot", is_bot=True)
    sent: list = []
    patchers = _patch_bot(sent)
    for patcher in patchers:
        patcher.start()
    try:
        first = _callback_update(data, app.bot, user_id)
        await app.process_update(first)
        key = conv._get_key(first)
        state = conv._conversations.get(key)
        assert state == expected_state, (
            f"{data} stored state {state!r}, expected {expected_state}"
        )
        if follow is not None:
            await app.process_update(_callback_update(follow, app.bot, user_id))
            assert conv._conversations.get(key), f"{follow} ended the wizard"
            assert t("cancelled", "en") not in sent, f"{follow} cancelled the wizard"
    finally:
        for patcher in patchers:
            patcher.stop()


def run() -> None:
    import bot as bot_main

    from db import open_database
    from handlers.game_info import _wz
    from handlers.stream_schedule import _sched_states

    with tempfile.TemporaryDirectory() as tmp:
        db = open_database(Path(tmp) / "menu_panels.db")
        db.upsert_user(_SCHED_UID)
        db.upsert_user(_GAME_INFO_UID)
        twitch = MagicMock()
        twitch.bind_igdb_db = MagicMock()
        app = bot_main.build_application("123:abc", db, twitch)
        conv = app.bot_data["main_conv"]
        assert isinstance(conv, ConversationHandler), "main_conv is not a conversation"
        conv_group = _conv_group(app, conv)
        # Groups 0..conv_group-1 answer an update before the conversation does.
        earlier = [h for g, hs in app.handlers.items() if 0 <= g < conv_group for h in hs]

        for action in _PANEL_ACTIONS:
            update = _callback_update(f"menu_other:{action}", app.bot, _SCHED_UID)
            assert _matches(earlier, update), f"menu_other:{action} panel callback is not wired"
            assert not _matches(conv.entry_points, update), (
                f"menu_other:{action} must stay a panel callback"
            )

        for action in _CONV_ACTIONS:
            update = _callback_update(f"menu_other:{action}", app.bot, _SCHED_UID)
            assert _matches(conv.entry_points, update), (
                f"menu_other:{action} is not a conversation entry point"
            )
            assert not _matches(earlier, update), (
                f"menu_other:{action} is answered before the conversation group"
            )

        asyncio.run(
            _panel_entry(
                app,
                conv,
                "menu_other:create_schedule",
                _SCHED_UID,
                _sched_states()["STREAM_SCHEDULE_MODE"],
                follow="stream_sched:mode:week",
            )
        )
        asyncio.run(
            _panel_entry(
                app,
                conv,
                "menu_other:game_info",
                _GAME_INFO_UID,
                _wz()["GAME_INFO_SEARCH"],
            )
        )
