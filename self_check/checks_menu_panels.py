"""Inline Other panel callback wiring.

Contract: `menu_other:create_schedule|watch|game_info` are main-conversation
entry points and no handler in an earlier group answers them; the other
`menu_other:*` actions stay in the earlier group and are not entry points. A
conversation entry stores the wizard state that its next keyboard runs in, or
ends without state when its panel is one-shot (watch).
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
_WATCH_UID = 900003
_CONV_ACTIONS = ("create_schedule", "watch", "game_info")
_PANEL_ACTIONS = ("follow_monitor", "ai_clips", "whisper_alerts", "chat", "channel_stats")
_BOT_SETTINGS_CONV = ("language", "ignored_words")
_BOT_SETTINGS_PANEL = ("back", "auth_tokens", "topics_support", "default_alert_settings")
_BROADCAST_CONV = ("broadcast_new",)
_BROADCAST_PANEL = ("back", "scheduled_broadcasts", "sent_broadcasts")


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


def _patch_bot(sent: list, markups: list):
    async def _reply(text, **kwargs):
        sent.append(text)
        markups.append(kwargs.get("reply_markup"))
        return SimpleNamespace(message_id=len(sent) + 1)

    async def _send(chat_id, text, **kwargs):
        return await _reply(text, **kwargs)

    async def _edit_message_text(*args, **kwargs):
        # watch:again edits the panel message; count that as an answer.
        text = kwargs.get("text")
        if text is not None:
            sent.append(text)
            markups.append(kwargs.get("reply_markup"))
        return True

    async def _get_me(bot_self, *args, **kwargs):
        bot_self._bot_user = User(id=42, first_name="bot", is_bot=True)
        return bot_self._bot_user

    from handlers import watch as watch_handlers

    patchers = [
        patch.object(ExtBot, "send_message", AsyncMock(side_effect=_send)),
        patch.object(
            watch_handlers,
            "_fetch_lucky_watch_suggestions",
            AsyncMock(
                return_value=(
                    [{"id": "1", "name": "Game"}],
                    [
                        {
                            "id": "1",
                            "user_login": "streamer",
                            "user_name": "Streamer",
                            "title": "Live",
                            "game_name": "Game",
                            "viewer_count": 10,
                        }
                    ],
                    [],
                )
            ),
        ),
        patch.object(ExtBot, "send_chat_action", AsyncMock(return_value=True)),
        patch.object(
            ExtBot, "edit_message_text", AsyncMock(side_effect=_edit_message_text)
        ),
        patch.object(ExtBot, "edit_message_reply_markup", AsyncMock(return_value=True)),
        patch.object(ExtBot, "answer_callback_query", AsyncMock(return_value=True)),
        patch.object(ExtBot, "delete_message", AsyncMock(return_value=True)),
        patch.object(Message, "reply_text", AsyncMock(side_effect=_reply)),
        patch.object(ExtBot, "get_me", _get_me),
    ]
    return patchers


async def _panel_entry(
    app,
    conv,
    data: str,
    user_id: int,
    expected_state: int | None,
    follow: str | None = None,
) -> list:
    from i18n import t

    sent: list = []
    markups: list = []
    patchers = _patch_bot(sent, markups)
    for patcher in patchers:
        patcher.start()
    try:
        await app.initialize()
        first = _callback_update(data, app.bot, user_id)
        await app.process_update(first)
        key = conv._get_key(first)
        state = conv._conversations.get(key)
        if expected_state is None:
            assert state is None, f"{data} left state {state!r}, expected none"
            assert sent, f"{data} answered nothing"
        else:
            assert state == expected_state, (
                f"{data} stored state {state!r}, expected {expected_state}"
            )
        assert t("cancelled", "en") not in sent, f"{data} cancelled the wizard"
        if follow is not None:
            await app.process_update(_callback_update(follow, app.bot, user_id))
            assert conv._conversations.get(key), f"{follow} ended the wizard"
            assert t("cancelled", "en") not in sent, f"{follow} cancelled the wizard"
        return markups
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
        db.upsert_user(_WATCH_UID)
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

        for action in _BOT_SETTINGS_PANEL:
            update = _callback_update(f"menu_bot_settings:{action}", app.bot, _SCHED_UID)
            assert _matches(earlier, update), (
                f"menu_bot_settings:{action} panel callback is not wired"
            )
            assert not _matches(conv.entry_points, update), (
                f"menu_bot_settings:{action} must stay a panel callback"
            )
        for action in _BOT_SETTINGS_CONV:
            update = _callback_update(f"menu_bot_settings:{action}", app.bot, _SCHED_UID)
            assert _matches(conv.entry_points, update), (
                f"menu_bot_settings:{action} is not a conversation entry point"
            )
            assert not _matches(earlier, update), (
                f"menu_bot_settings:{action} is answered before the conversation group"
            )
        for action in _BROADCAST_PANEL:
            update = _callback_update(f"menu_broadcast:{action}", app.bot, _SCHED_UID)
            assert _matches(earlier, update), (
                f"menu_broadcast:{action} panel callback is not wired"
            )
            assert not _matches(conv.entry_points, update), (
                f"menu_broadcast:{action} must stay a panel callback"
            )
        for action in _BROADCAST_CONV:
            update = _callback_update(f"menu_broadcast:{action}", app.bot, _SCHED_UID)
            assert _matches(conv.entry_points, update), (
                f"menu_broadcast:{action} is not a conversation entry point"
            )
            assert not _matches(earlier, update), (
                f"menu_broadcast:{action} is answered before the conversation group"
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
        markups = asyncio.run(
            _panel_entry(app, conv, "menu_other:watch", _WATCH_UID, None)
        )
        keyboard = next(
            (m for m in reversed(markups) if hasattr(m, "inline_keyboard")), None
        )
        assert keyboard is not None, "watch panel sent no inline keyboard"
        buttons = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        assert buttons == ["watch:again"], f"unexpected watch panel buttons {buttons}"
        asyncio.run(_panel_entry(app, conv, "watch:again", _WATCH_UID, None))
        asyncio.run(
            _panel_entry(
                app,
                conv,
                "menu_other:game_info",
                _GAME_INFO_UID,
                _wz()["GAME_INFO_SEARCH"],
            )
        )
