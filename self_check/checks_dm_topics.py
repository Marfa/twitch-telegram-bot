"""Private DM alert topics — keys, new-user default on, markup helpers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bot_helpers import send_reply_keyboard_menu
from dm_topics import (
    GENERAL_TOPIC_ID,
    STREAMER_TOPIC_LIMIT,
    TOPIC_KEYS,
    _TOPIC_ICON_COLOR,
    _TOPIC_ICON_EMOJI,
    ensure_all_user_topics,
    ensure_streamer_topic,
    ensure_user_topic,
    extract_topic_emoji,
    icon_custom_emoji_id_for_emoji,
    icon_custom_emoji_id_for_topic,
    install_private_ui_to_general,
    private_ui_should_go_to_general,
    reset_topic_icon_cache_for_tests,
    resolve_alert_thread_id,
    streamer_topic_can_enable,
    strip_alert_topic_reply_keyboards,
    strip_reply_keyboards_from_threads,
    sync_user_topic_icons,
    topic_key_for_subscription,
)
from i18n import (
    btn,
    dest_streamer_topic_keyboard,
    other_menu,
    topics_intro_keyboard,
    topics_support_keyboard,
    t,
)


def _check_new_user_topics_default_on() -> None:
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    from db import open_database
    import analytics

    with tempfile.TemporaryDirectory() as tmp:
        db = open_database(Path(tmp) / "topics_default.db")
        db.upsert_user(101)
        assert db.is_topics_enabled(101) is True
        # Intro already marked so legacy prompt is never offered.
        assert db.mark_topics_intro_sent(101) is False
        db.set_topics_enabled(101, False)
        db.upsert_user(101)
        assert db.is_topics_enabled(101) is False

    with tempfile.TemporaryDirectory() as tmp:
        db = open_database(Path(tmp) / "topics_exp.db")
        db.upsert_user(202)
        with patch.object(analytics, "get_feature_flag", return_value="control"):
            assert analytics.apply_topics_default_experiment(db, 202) == "control"
        assert db.is_topics_enabled(202) is False
        assert db.mark_topics_intro_sent(202) is True
        db.upsert_user(203)
        with patch.object(analytics, "get_feature_flag", return_value="test"):
            assert analytics.apply_topics_default_experiment(db, 203) == "test"
        assert db.is_topics_enabled(203) is True
        with patch.object(analytics, "get_feature_flag", return_value=False):
            db.upsert_user(204)
            assert analytics.apply_topics_default_experiment(db, 204) is None
        assert db.is_topics_enabled(204) is True


def _check_start_offloads_flag_lookup() -> None:
    """The PostHog flag request blocks, so /start must run it off the event loop."""
    import tempfile
    import time
    from pathlib import Path

    import analytics
    import bot as bot_module
    from db import open_database
    from self_check.checks_flow_nav import _app, _ctx, _msg_update

    def slow_flag(user_id: int, key: str) -> str:
        time.sleep(0.3)
        return "control"

    async def _scenario() -> int:
        with tempfile.TemporaryDirectory() as tmp:
            db = open_database(Path(tmp) / "start_loop.db")
            application, _bot = _app(db)
            user_id = 900777
            update = _msg_update(user_id, "/start")
            ctx = _ctx(application)
            ctx.args = []
            ticks = 0
            stop = False

            async def _ticker() -> None:
                nonlocal ticks
                while not stop:
                    ticks += 1
                    await asyncio.sleep(0.02)

            ticker = asyncio.create_task(_ticker())
            try:
                with patch.object(
                    analytics, "get_feature_flag", side_effect=slow_flag
                ), patch.object(
                    bot_module, "sync_stream_chat_menu_button", new=AsyncMock()
                ):
                    await bot_module.start(update, ctx)
            finally:
                stop = True
                await ticker

            assert db.user_exists(user_id)
            # "control" switches topics off, so this also proves the awaited
            # result reaches the new user instead of being dropped.
            assert db.is_topics_enabled(user_id) is False
            return ticks

    ticks = asyncio.run(_scenario())
    assert ticks > 5, f"/start blocked the event loop (ticker turns={ticks})"


def run() -> None:
    assert "live" in TOPIC_KEYS and "giveaway_watch" in TOPIC_KEYS
    _check_new_user_topics_default_on()
    _check_start_offloads_flag_lookup()
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
    assert STREAMER_TOPIC_LIMIT == 5
    assert extract_topic_emoji("🎮 play") == "🎮"
    assert extract_topic_emoji("nope") is None

    for loc in ("ru", "en", "uk", "it"):
        assert btn("bot_settings", loc)
        assert btn("topics_support", loc)
        assert t("topics_support_toggle", loc)
        assert t("topics_intro", loc)
        assert t("dest_streamer_topic_toggle", loc)
        assert t(
            "dest_streamer_topic_prompt", loc, used=1, limit=STREAMER_TOPIC_LIMIT
        )
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
        sk = dest_streamer_topic_keyboard(loc, enabled=True, icon_emoji="🎮")
        assert sk.inline_keyboard[0][0].callback_data == "dest_topic:toggle"
        cbs = [b.callback_data or "" for row in sk.inline_keyboard for b in row]
        assert "dest_topic:icon" in cbs
        assert "dest_topic:back" in cbs
        assert "dest_topic:done" in cbs
        assert cbs.index("dest_topic:back") < cbs.index("dest_topic:done")
        sk_off = dest_streamer_topic_keyboard(loc, enabled=False)
        cbs_off = [
            b.callback_data or "" for row in sk_off.inline_keyboard for b in row
        ]
        assert "dest_topic:icon" not in cbs_off
        assert t("dest_streamer_topic_back", loc)

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

    from bot_helpers import _menu

    bot = AsyncMock()
    bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=1, chat_id=7)
    )
    # other_menu is legacy sticky; helper coerces to main_menu.
    kb = other_menu("ru")
    await send_reply_keyboard_menu(bot, 7, "Прочее:", kb, user_id=7, lang="ru")
    assert bot.send_message.await_count == 1
    args, kwargs = bot.send_message.await_args
    assert args[0] == 7
    assert args[1] == "Прочее:"
    assert kwargs.get("reply_markup").keyboard == _menu("ru", 7).keyboard
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

    db = MagicMock()
    db.is_topics_enabled = MagicMock(return_value=True)
    db.get_user_topic_thread = MagicMock(
        side_effect=lambda _u, key: 50 if key == "live" else None
    )
    bot.send_message.reset_mock()
    await send_reply_keyboard_menu(
        bot, 7, "Прочее:", kb, db=db, user_id=7, lang="ru"
    )
    # Menu must not strip topics — only force=True on topics enable does.
    assert bot.send_message.await_count == 1
    assert "message_thread_id" not in bot.send_message.await_args.kwargs
    assert (
        bot.send_message.await_args.kwargs.get("reply_markup").keyboard
        == _menu("ru", 7).keyboard
    )

    bot.send_message.reset_mock()
    with patch(
        "bot_helpers.delete_chat_message", new=AsyncMock()
    ), patch("asyncio.sleep", new=AsyncMock()):
        await strip_alert_topic_reply_keyboards(bot, db, 7)
        assert bot.send_message.await_count == 0
        await strip_alert_topic_reply_keyboards(bot, db, 7, force=True)
    assert bot.send_message.await_count == 1
    assert bot.send_message.await_args.kwargs.get("message_thread_id") == 50
    assert isinstance(
        bot.send_message.await_args.kwargs.get("reply_markup"), ReplyKeyboardRemove
    )


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

    await _check_streamer_topics(bot, stickers)


async def _check_streamer_topics(bot, stickers) -> None:
    reset_topic_icon_cache_for_tests()
    bot.get_forum_topic_icon_stickers = AsyncMock(return_value=stickers)
    assert await icon_custom_emoji_id_for_emoji(bot, "📣") == "id-live"
    assert await icon_custom_emoji_id_for_emoji(bot, "🦄") is None

    db = MagicMock()
    db.count_enabled_streamer_topics = MagicMock(return_value=5)
    db.get_streamer_topic = MagicMock(return_value=None)
    assert streamer_topic_can_enable(db, 1, "tw1") is False
    db.count_enabled_streamer_topics = MagicMock(return_value=2)
    assert streamer_topic_can_enable(db, 1, "tw1") is True
    db.get_streamer_topic = MagicMock(
        return_value={
            "enabled": True,
            "message_thread_id": 99,
            "icon_emoji": "",
            "twitch_login": "foo",
        }
    )
    assert streamer_topic_can_enable(db, 1, "tw1") is True

    db.get_streamer_topic = MagicMock(return_value=None)
    db.count_enabled_streamer_topics = MagicMock(return_value=0)
    db.upsert_streamer_topic = MagicMock()
    db.set_streamer_topic_enabled = MagicMock()
    bot.do_api_request = AsyncMock(return_value={"message_thread_id": 77})
    reset_topic_icon_cache_for_tests()
    bot.get_forum_topic_icon_stickers = AsyncMock(return_value=stickers)
    tid = await ensure_streamer_topic(
        bot, db, 3, "uid42", twitch_login="marfapr", icon_emoji="📣", lang="en"
    )
    assert tid == 77
    db.upsert_streamer_topic.assert_called()
    create_kwargs = bot.do_api_request.await_args.kwargs["api_kwargs"]
    assert create_kwargs["name"] == "marfapr"
    assert create_kwargs["icon_custom_emoji_id"] == "id-live"

    sub = SimpleNamespace(
        dest_type="dm",
        owner_id=3,
        twitch_user_id="uid42",
        twitch_username="marfapr",
        thread_id=None,
        notify_on_live=True,
        notify_on_end=False,
        notify_on_category_change=False,
        schedule_reminder_minutes=0,
        notify_on_drops=False,
        category_watch_prefs="",
        release_watch_prefs="",
        giveaway_watch_prefs="",
    )
    db.is_topics_enabled = MagicMock(return_value=True)
    db.get_user_locale = MagicMock(return_value="en")
    db.get_streamer_topic = MagicMock(
        return_value={
            "enabled": True,
            "message_thread_id": 77,
            "icon_emoji": "📣",
            "twitch_login": "marfapr",
        }
    )
    db.get_user_topic_thread = MagicMock(return_value=None)
    with patch("premium.has_feature_sync", return_value=True):
        assert await resolve_alert_thread_id(bot, db, sub, "live") == 77
    with patch("premium.has_feature_sync", return_value=False):
        # Unentitled → fall back to type topic create path.
        bot.do_api_request = AsyncMock(return_value={"message_thread_id": 11})
        db.set_user_topic_thread = MagicMock()
        reset_topic_icon_cache_for_tests()
        bot.get_forum_topic_icon_stickers = AsyncMock(return_value=stickers)
        assert await resolve_alert_thread_id(bot, db, sub, "live") == 11
