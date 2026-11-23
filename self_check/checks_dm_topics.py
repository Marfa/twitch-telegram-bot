"""Private DM alert topics — keys, defaults off, markup helpers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bot_helpers import send_reply_keyboard_menu
from dm_topics import (
    GENERAL_TOPIC_ID,
    TOPIC_KEYS,
    _TOPIC_ICON_COLOR,
    _TOPIC_ICON_EMOJI,
    ensure_all_user_topics,
    ensure_user_topic,
    icon_custom_emoji_id_for_topic,
    install_private_ui_to_general,
    private_ui_should_go_to_general,
    reset_topic_icon_cache_for_tests,
    strip_reply_keyboards_from_threads,
    sync_user_topic_icons,
    topic_key_for_subscription,
)
from i18n import btn, other_menu, topics_intro_keyboard, topics_support_keyboard, t


def run() -> None:
    assert "live" in TOPIC_KEYS and "giveaway_watch" in TOPIC_KEYS
    for key in TOPIC_KEYS:
        assert key in _TOPIC_ICON_COLOR
        assert key in _TOPIC_ICON_EMOJI and _TOPIC_ICON_EMOJI[key]
        # createForumTopic only accepts these six RGB values.
        assert _TOPIC_ICON_COLOR[key] in {
            0x6FB9F0,
            0xFFD67E,
            0xCB86DB,
            0x8EEE98,
            0xFF93B2,
            0xFB6F5F,
        }
    for loc in ("ru", "en", "uk", "it"):
        assert btn("bot_settings", loc)
        assert t("topics_support_toggle", loc)
        assert t("topics_intro", loc)
        for key in TOPIC_KEYS:
            assert t(f"topic_name_{key}", loc)
        on = topics_support_keyboard(loc, enabled=True)
        off = topics_support_keyboard(loc, enabled=False)
        assert on.inline_keyboard[0][0].callback_data == "topics:toggle"
        assert off.inline_keyboard[0][0].text.startswith("⬜️")
        assert on.inline_keyboard[0][0].text.startswith("✅")
        intro = topics_intro_keyboard(loc)
        assert intro.inline_keyboard[0][0].callback_data == "topics:intro_toggle"
        assert intro.inline_keyboard[0][0].text.startswith("⬜️")
        assert any(
            (b.callback_data or "") == "dm_menu"
            for row in intro.inline_keyboard
            for b in row
        )

    live = SimpleNamespace(
        notify_on_live=True,
        notify_on_end=False,
        notify_on_category_change=False,
        schedule_reminder_minutes=0,
        notify_on_drops=False,
        category_watch_prefs="",
        release_watch_prefs="",
        giveaway_watch_prefs="",
    )
    assert topic_key_for_subscription(live, "live") == "live"
    upcoming = SimpleNamespace(
        notify_on_live=False,
        notify_on_end=False,
        notify_on_category_change=False,
        schedule_reminder_minutes=30,
        notify_on_drops=False,
        category_watch_prefs="",
        release_watch_prefs="",
        giveaway_watch_prefs="",
    )
    assert topic_key_for_subscription(upcoming, "schedule") == "upcoming"
    assert topic_key_for_subscription(upcoming, "schedule_cancel") == "upcoming"

    asyncio.run(_check_topic_icons())
    asyncio.run(_check_menu_keyboard_goes_to_general())
    _check_private_ui_general_gate()
    asyncio.run(_check_reply_text_from_topic_goes_to_general())


async def _check_menu_keyboard_goes_to_general() -> None:
    """Reply-keyboard menus must not inherit Эфир / All-messages thread ids."""
    from telegram import ReplyKeyboardRemove

    bot = AsyncMock()
    bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=1, chat_id=7)
    )
    kb = other_menu("ru")
    await send_reply_keyboard_menu(bot, 7, "Прочее:", kb)
    assert bot.send_message.await_count == 1
    args, kwargs = bot.send_message.await_args
    assert args[0] == 7
    assert args[1] == "Прочее:"
    assert kwargs.get("reply_markup") is kb
    assert "message_thread_id" not in kwargs
    assert kwargs.get("message_thread_id") is None

    # Strip sticky keyboard from Эфир (live) before setting General.
    bot.send_message.reset_mock()
    with patch(
        "bot_helpers.delete_chat_message", new=AsyncMock()
    ) as del_bh, patch("asyncio.sleep", new=AsyncMock()):
        await strip_reply_keyboards_from_threads(bot, 7, [99])
        assert bot.send_message.await_count == 1
        skw = bot.send_message.await_args.kwargs
        assert skw.get("message_thread_id") == 99
        assert isinstance(skw.get("reply_markup"), ReplyKeyboardRemove)
        del_bh.assert_awaited()

    reset_topic_icon_cache_for_tests()
    db = MagicMock()
    db.is_topics_enabled = MagicMock(return_value=True)
    db.get_user_topic_thread = MagicMock(
        side_effect=lambda _u, key: 50 if key == "live" else None
    )
    bot.send_message.reset_mock()
    with patch(
        "bot_helpers.delete_chat_message", new=AsyncMock()
    ), patch("asyncio.sleep", new=AsyncMock()):
        await send_reply_keyboard_menu(
            bot, 7, "Прочее:", kb, db=db, user_id=7
        )
    # 1 strip (live) + 1 General menu
    assert bot.send_message.await_count == 2
    strip_call, menu_call = bot.send_message.await_args_list
    assert strip_call.kwargs.get("message_thread_id") == 50
    assert isinstance(strip_call.kwargs.get("reply_markup"), ReplyKeyboardRemove)
    assert "message_thread_id" not in menu_call.kwargs
    assert menu_call.kwargs.get("reply_markup") is kb

    # Second menu tap must not post into topics again.
    bot.send_message.reset_mock()
    with patch(
        "bot_helpers.delete_chat_message", new=AsyncMock()
    ), patch("asyncio.sleep", new=AsyncMock()):
        await send_reply_keyboard_menu(
            bot, 7, "Настройки:", kb, db=db, user_id=7
        )
    assert bot.send_message.await_count == 1
    assert "message_thread_id" not in bot.send_message.await_args.kwargs
    assert bot.send_message.await_args.kwargs.get("reply_markup") is kb


def _check_private_ui_general_gate() -> None:
    from telegram import Chat
    from telegram.constants import ChatType

    topic = SimpleNamespace(
        chat=Chat(id=7, type=ChatType.PRIVATE),
        is_topic_message=True,
        message_thread_id=99,
    )
    assert private_ui_should_go_to_general(topic) is True
    general = SimpleNamespace(
        chat=Chat(id=7, type=ChatType.PRIVATE),
        is_topic_message=False,
        message_thread_id=None,
    )
    assert private_ui_should_go_to_general(general) is False
    general_id = SimpleNamespace(
        chat=Chat(id=7, type=ChatType.PRIVATE),
        is_topic_message=False,
        message_thread_id=GENERAL_TOPIC_ID,
    )
    assert private_ui_should_go_to_general(general_id) is False
    group = SimpleNamespace(
        chat=Chat(id=-100, type=ChatType.SUPERGROUP),
        is_topic_message=True,
        message_thread_id=99,
    )
    assert private_ui_should_go_to_general(group) is False


async def _check_reply_text_from_topic_goes_to_general() -> None:
    """Patched Message.reply_text from Эфир must send_message without thread."""
    from datetime import datetime, timezone

    from telegram import Chat, Message, User
    from telegram.constants import ChatType

    install_private_ui_to_general()
    bot = AsyncMock()
    msg = Message(
        message_id=10,
        date=datetime.now(timezone.utc),
        chat=Chat(id=7, type=ChatType.PRIVATE),
        from_user=User(id=7, first_name="t", is_bot=False),
        text="📦 Прочее",
        is_topic_message=True,
        message_thread_id=99,
    )
    msg.set_bot(bot)
    kb = other_menu("ru")
    await msg.reply_text("Прочее:", reply_markup=kb)
    assert bot.send_message.await_count == 1
    args, kwargs = bot.send_message.await_args
    assert args[0] == 7
    assert args[1] == "Прочее:"
    assert kwargs.get("reply_markup") is kb
    assert "message_thread_id" not in kwargs
    assert "reply_parameters" not in kwargs


async def _check_topic_icons() -> None:
    reset_topic_icon_cache_for_tests()
    bot = AsyncMock()
    stickers = [
        SimpleNamespace(emoji="📣", custom_emoji_id="id-live"),
        SimpleNamespace(emoji="🏁", custom_emoji_id="id-end"),
        SimpleNamespace(emoji="🎮", custom_emoji_id="id-cat"),
        SimpleNamespace(emoji="📆", custom_emoji_id="id-up"),
        SimpleNamespace(emoji="💎", custom_emoji_id="id-drops"),  # 🎁 missing → fallback
        SimpleNamespace(emoji="🔎", custom_emoji_id="id-game"),
        SimpleNamespace(emoji="📰", custom_emoji_id="id-rel"),
        SimpleNamespace(emoji="🎉", custom_emoji_id="id-gv"),
    ]
    bot.get_forum_topic_icon_stickers = AsyncMock(return_value=stickers)
    assert await icon_custom_emoji_id_for_topic(bot, "live") == "id-live"
    assert await icon_custom_emoji_id_for_topic(bot, "drops") == "id-drops"
    assert await icon_custom_emoji_id_for_topic(bot, "missing") is None

    db = MagicMock()
    db.get_user_topic_thread = MagicMock(return_value=None)
    db.set_user_topic_thread = MagicMock()
    bot.do_api_request = AsyncMock(
        return_value={"message_thread_id": 42}
    )
    bot.edit_forum_topic = AsyncMock()
    reset_topic_icon_cache_for_tests()
    bot.get_forum_topic_icon_stickers = AsyncMock(return_value=stickers)

    tid = await ensure_user_topic(bot, db, 7, "live", lang="en")
    assert tid == 42
    create_kwargs = bot.do_api_request.await_args.kwargs["api_kwargs"]
    assert create_kwargs["icon_custom_emoji_id"] == "id-live"
    assert create_kwargs["icon_color"] == _TOPIC_ICON_COLOR["live"]
    db.set_user_topic_thread.assert_called_once_with(7, "live", 42)

    # Existing threads: ensure_all creates none and syncs icons via editForumTopic.
    db.get_user_topic_thread = MagicMock(
        side_effect=lambda _u, key: 100 + TOPIC_KEYS.index(key)
    )
    bot.do_api_request.reset_mock()
    bot.edit_forum_topic.reset_mock()
    reset_topic_icon_cache_for_tests()
    bot.get_forum_topic_icon_stickers = AsyncMock(return_value=stickers)
    n = await ensure_all_user_topics(bot, db, 9, lang="en")
    assert n == len(TOPIC_KEYS)
    assert bot.do_api_request.await_count == 0
    assert bot.edit_forum_topic.await_count == len(TOPIC_KEYS)
    edited_ids = {
        call.kwargs["icon_custom_emoji_id"]
        for call in bot.edit_forum_topic.await_args_list
    }
    assert "id-live" in edited_ids
    assert "id-drops" in edited_ids

    bot.edit_forum_topic.reset_mock()
    reset_topic_icon_cache_for_tests()
    bot.get_forum_topic_icon_stickers = AsyncMock(return_value=stickers)
    updated = await sync_user_topic_icons(bot, db, 9)
    assert updated == len(TOPIC_KEYS)
    assert bot.edit_forum_topic.await_count == len(TOPIC_KEYS)
