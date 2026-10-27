"""Group vs private chat discipline: setup gate, reply targets, PostHog context."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from telegram.constants import ChatType
from telegram.ext import ConversationHandler

_ROOT = Path(__file__).resolve().parents[1]


def _check_premium_and_helpers() -> None:
    from bot_helpers import (
        GROUP_DM_ONLY_COMMANDS,
        GROUP_SETUP_CALLBACK_PATTERN,
        bot_admin_lang,
        chat_context_properties,
        group_setup_menu_filter,
        is_private_chat,
        reply_chat_id,
        reply_lang,
    )
    from i18n import DEFAULT_LOCALE
    from unittest.mock import patch
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
    assert "when" not in GROUP_DM_ONLY_COMMANDS
    for cmd in ("start", "help", "cancel", "schedule", "feedback", "settings", "stats"):
        assert cmd in GROUP_DM_ONLY_COMMANDS
    helpers_src = (_ROOT / "bot_helpers.py").read_text(encoding="utf-8")
    for btn_key in ("chat", "other", "feedback", "back", "cart", "admin", "stats"):
        assert f'_btn_filter("{btn_key}")' in helpers_src
    error_src = (_ROOT / "bot.py").read_text(encoding="utf-8")
    assert "chat_context_properties(update)" in error_src
    assert "when_stream_command" in error_src
    assert "day_schedule_command" in error_src
    assert "week_schedule_command" in error_src
    assert "on_schedule_query_callback" in error_src
    assert "schq:" in error_src
    assert 'CommandHandler("when"' in error_src
    assert 'CommandHandler("day"' in error_src
    assert 'CommandHandler("week"' in error_src
    assert "GROUP_DM_ONLY_COMMANDS" in error_src
    assert "raise ApplicationHandlerStop" in error_src
    from i18n import t as _t

    assert "личн" in _t("setup_private_only", "ru").lower()
    assert "private chat" in _t("setup_private_only", "en").lower()

    db_lang = MagicMock()
    db_lang.get_user_locale = MagicMock(side_effect=lambda uid: "ru" if uid == 7 else "en")
    with patch("config.ADMIN_USER_IDS", frozenset({7, 99})):
        assert bot_admin_lang(db_lang) == "ru"
    db_empty = MagicMock(get_user_locale=MagicMock(return_value=None))
    with patch("config.ADMIN_USER_IDS", frozenset({7})):
        assert bot_admin_lang(db_empty) == DEFAULT_LOCALE
    ctx_lang = MagicMock()
    ctx_lang.application.bot_data = {"db": db_lang}
    with patch("config.ADMIN_USER_IDS", frozenset({7})):
        assert reply_lang(ctx_lang, msg_update, uid) == "ru"
    priv_update = SimpleNamespace(
        effective_user=SimpleNamespace(id=uid),
        effective_chat=SimpleNamespace(id=uid, type=ChatType.PRIVATE),
        effective_message=MagicMock(),
        callback_query=None,
    )
    assert reply_lang(ctx_lang, priv_update, uid) == "en"
    when_src = (_ROOT / "handlers/when_stream.py").read_text(encoding="utf-8")
    assert "reply_lang(" in when_src
    assert "_user_lang(" not in when_src
    assert "send_ephemeral_text" in when_src
    assert "edit_ephemeral_text" in when_src
    assert "ensure_group_schedule_ephemeral_commands" in when_src
    assert "is_ephemeral" in when_src
    bot_src_eph = (_ROOT / "bot.py").read_text(encoding="utf-8")
    assert "ensure_group_schedule_ephemeral_commands" in bot_src_eph
    eph_src = (_ROOT / "ephemeral.py").read_text(encoding="utf-8")
    assert "editEphemeralMessageText" in eph_src
    assert "ephemeral_message_parameters" in eph_src


def _check_when_stream_helpers() -> None:
    from handlers.when_stream import (
        DAY_SCHEDULE_TEXT_RE,
        SCHEDULE_REPLY_DELETE_AFTER,
        SCHEDULE_REPLY_DELETE_MISFIRE_GRACE,
        WEEK_SCHEDULE_TEXT_RE,
        WHEN_STREAM_TEXT_RE,
        format_range_schedule_line,
        format_range_streamer_block,
        format_when_stream_line,
        next_upcoming_segment,
        schedule_detail_keyboard,
        schedule_streamer_keyboard,
        schedule_window_end,
        segments_in_window,
        unique_streamers_for_chat,
    )
    from i18n import t

    assert SCHEDULE_REPLY_DELETE_AFTER.total_seconds() == 300
    assert SCHEDULE_REPLY_DELETE_MISFIRE_GRACE == 300

    assert WHEN_STREAM_TEXT_RE.match("Когда стрим?")
    assert WHEN_STREAM_TEXT_RE.match("когда стрим")
    assert WHEN_STREAM_TEXT_RE.match("@twitch2telegram_bot Когда стрим?")
    assert WHEN_STREAM_TEXT_RE.match("@twitch2telegram_bot  Когда стрим?")
    assert WHEN_STREAM_TEXT_RE.match("When stream?")
    assert WHEN_STREAM_TEXT_RE.match("When is the stream?")
    assert WHEN_STREAM_TEXT_RE.match("@bot When is the stream?")
    assert not WHEN_STREAM_TEXT_RE.match("когда стрим завтра")
    assert not WHEN_STREAM_TEXT_RE.match("@bot когда стрим завтра")
    assert DAY_SCHEDULE_TEXT_RE.match("расписание на день")
    assert DAY_SCHEDULE_TEXT_RE.match("@bot расписание на день")
    assert DAY_SCHEDULE_TEXT_RE.match("schedule for the day")
    assert DAY_SCHEDULE_TEXT_RE.match("розклад на день")
    assert WEEK_SCHEDULE_TEXT_RE.match("расписание на неделю")
    assert WEEK_SCHEDULE_TEXT_RE.match("schedule for the week")
    assert WEEK_SCHEDULE_TEXT_RE.match("розклад на тиждень")
    assert not DAY_SCHEDULE_TEXT_RE.match("расписание на неделю")
    assert not WEEK_SCHEDULE_TEXT_RE.match("расписание на день")

    subs = [
        SimpleNamespace(twitch_user_id="1", twitch_username="alpha"),
        SimpleNamespace(twitch_user_id="1", twitch_username="alpha"),
        SimpleNamespace(twitch_user_id="2", twitch_username="beta"),
    ]
    assert unique_streamers_for_chat(subs) == [("1", "alpha"), ("2", "beta")]

    now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert next_upcoming_segment({"segments": [], "vacation": None}, now=now) is None
    assert (
        next_upcoming_segment(
            {
                "segments": [],
                "vacation": {
                    "start_time": "2029-01-01T00:00:00Z",
                    "end_time": "2031-01-01T00:00:00Z",
                },
            },
            now=now,
        )
        is None
    )
    past = {
        "id": "past",
        "start_time": "2029-12-01T12:00:00Z",
        "category": {"name": "Old"},
    }
    future = {
        "id": "future",
        "start_time": "2030-01-02T15:00:00Z",
        "category": {"name": "Elden Ring"},
    }
    canceled = {
        "id": "x",
        "start_time": "2030-01-01T18:00:00Z",
        "canceled_until": "2030-01-03T00:00:00Z",
        "category": {"name": "Skip"},
    }
    seg = next_upcoming_segment(
        {"segments": [past, canceled, future], "vacation": None}, now=now
    )
    assert seg is not None and seg["id"] == "future"

    window_end = schedule_window_end("week", now=now)
    assert window_end > now
    week_slots = segments_in_window(
        {"segments": [past, canceled, future], "vacation": None},
        window_start=now,
        window_end=window_end,
    )
    assert len(week_slots) == 1 and week_slots[0][1] == "Elden Ring"
    day_end = schedule_window_end("day", now=now)
    assert (
        segments_in_window(
            {"segments": [future], "vacation": None},
            window_start=now,
            window_end=day_end,
        )
        == []
    )
    range_line = format_range_schedule_line(
        lang="ru",
        start=datetime(2030, 1, 2, 15, 0, tzinfo=timezone.utc),
        category="Fallout: A Post Nuclear Role Playing Game",
    )
    assert range_line.startswith("- 2 января 18:00 ")
    assert "Fallout: A Post Nuclear Role Playing Game" in range_line
    multi_block = format_range_streamer_block(
        lang="ru",
        slots=[
            (datetime(2030, 1, 2, 15, 0, tzinfo=timezone.utc), "Elden Ring"),
        ],
    )
    assert multi_block == "- 2 января 18:00 Elden Ring"
    from handlers.when_stream import format_schedule_with_streamer_link

    linked = format_schedule_with_streamer_link(
        username="alpha", body=multi_block
    )
    assert linked.startswith('<a href="https://www.twitch.tv/alpha">alpha</a>:')
    assert "- 2 января 18:00 Elden Ring" in linked
    kb = schedule_streamer_keyboard(
        "week", [("1", "alpha"), ("2", "beta")]
    )
    assert kb.inline_keyboard[0][0].callback_data == "schq:week:1"
    assert kb.inline_keyboard[0][1].callback_data == "schq:week:2"
    assert schedule_detail_keyboard("day", "ru").inline_keyboard[0][0].callback_data == (
        "schq:list:day"
    )

    line = format_when_stream_line(
        lang="ru",
        start=datetime(2030, 1, 2, 15, 0, tzinfo=timezone.utc),
        category="Elden Ring",
    )
    assert line.startswith("Следующий эфир будет ")
    assert "Категория Elden Ring" in line
    assert "MSK" in line
    assert t("when_stream_no_schedule", "ru").startswith("Откуда мне-то знать?")
    assert t("schedule_found_streamers", "ru") == "Найдено расписание для стримеров"


async def _check_ephemeral_helpers() -> None:
    from ephemeral import (
        EphemeralHandle,
        edit_ephemeral_text,
        message_ephemeral_id,
        send_ephemeral_text,
    )
    from types import MappingProxyType, SimpleNamespace
    from unittest.mock import AsyncMock

    assert message_ephemeral_id(None) is None
    assert message_ephemeral_id(SimpleNamespace(api_kwargs={})) is None
    assert (
        message_ephemeral_id(
            SimpleNamespace(api_kwargs={"ephemeral_message_id": "7"})
        )
        == 7
    )
    assert (
        message_ephemeral_id(SimpleNamespace(ephemeral_message_id=9, api_kwargs={}))
        == 9
    )
    # PTB Message.api_kwargs is a mappingproxy, not a plain dict.
    assert (
        message_ephemeral_id(
            SimpleNamespace(
                api_kwargs=MappingProxyType({"ephemeral_message_id": 42})
            )
        )
        == 42
    )

    bot = AsyncMock()
    bot.send_message = AsyncMock(
        return_value=SimpleNamespace(
            api_kwargs={"ephemeral_message_id": 55},
            message_id=1,
        )
    )
    handle = await send_ephemeral_text(
        bot, -1001, "status", 42, reply_to_ephemeral_id=3
    )
    assert handle == EphemeralHandle(
        chat_id=-1001, receiver_user_id=42, ephemeral_message_id=55
    )
    kw = bot.send_message.await_args.kwargs
    assert kw["api_kwargs"]["ephemeral_message_parameters"]["receiver_user_id"] == 42
    assert kw["api_kwargs"]["reply_parameters"]["ephemeral_message_id"] == 3

    bot.do_api_request = AsyncMock(return_value=True)
    assert await edit_ephemeral_text(bot, handle, "done")
    endpoint = bot.do_api_request.await_args.args[0]
    assert endpoint == "editEphemeralMessageText"
    edit_kw = bot.do_api_request.await_args.kwargs["api_kwargs"]
    assert edit_kw["ephemeral_message_id"] == 55
    assert edit_kw["text"] == "done"


async def _check_when_stream_ephemeral_path() -> None:
    from handlers.when_stream import when_stream_command
    from i18n import t
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch

    msg = AsyncMock()
    msg.chat = SimpleNamespace(type=ChatType.SUPERGROUP)
    msg.api_kwargs = {"ephemeral_message_id": 11}
    msg.ephemeral_message_id = 11
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=900001),
        effective_chat=SimpleNamespace(id=-1009, type=ChatType.SUPERGROUP),
        effective_message=msg,
        callback_query=None,
    )
    sub = SimpleNamespace(twitch_user_id="42", twitch_username="marfapr")
    db = MagicMock()
    db.get_user_locale = MagicMock(return_value="ru")
    db.get_enabled_subscriptions_by_chat_id = MagicMock(return_value=[sub])
    twitch = MagicMock()
    twitch.get_channel_schedule = MagicMock(
        return_value={"segments": [], "vacation": None}
    )
    ctx = MagicMock()
    ctx.bot = AsyncMock()
    ctx.bot.send_message = AsyncMock(
        return_value=SimpleNamespace(api_kwargs={"ephemeral_message_id": 99})
    )
    ctx.bot.do_api_request = AsyncMock(return_value=True)
    ctx.job_queue = MagicMock()
    ctx.application.bot_data = {"db": db, "twitch": twitch}

    with patch("handlers.when_stream.reply_lang", return_value="ru"):
        await when_stream_command(update, ctx)

    ctx.bot.send_message.assert_awaited()
    status_text = ctx.bot.send_message.await_args.args[1]
    assert status_text == t("schedule_query_working", "ru")
    # edit result + immediate delete of ephemeral slash (not deferred 5 min).
    endpoints = [c.args[0] for c in ctx.bot.do_api_request.await_args_list if c.args]
    assert "editEphemeralMessageText" in endpoints
    assert "deleteEphemeralMessage" in endpoints
    edit_call = next(
        c
        for c in ctx.bot.do_api_request.await_args_list
        if c.args and c.args[0] == "editEphemeralMessageText"
    )
    assert edit_call.kwargs["api_kwargs"]["text"] == t("when_stream_no_schedule", "ru")
    deleted = {
        c.kwargs["api_kwargs"]["ephemeral_message_id"]
        for c in ctx.bot.do_api_request.await_args_list
        if c.args and c.args[0] == "deleteEphemeralMessage"
    }
    assert 11 in deleted
    msg.reply_text.assert_not_awaited()
    ctx.job_queue.run_once.assert_called_once()
    run_kw = ctx.job_queue.run_once.call_args.kwargs
    assert run_kw["when"] == 300
    assert run_kw["job_kwargs"]["misfire_grace_time"] == 300
    job_data = run_kw["data"]
    # Only the bot reply waits for TTL — slash already deleted.
    assert [item["ephemeral_message_id"] for item in job_data["ephemerals"]] == [99]


async def _check_when_stream_ephemeral_admin_public_slash() -> None:
    """Public /week in a group where the bot is admin → ephemeral + delete trigger."""
    from handlers.when_stream import when_stream_command
    from i18n import t
    from telegram.constants import ChatMemberStatus
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch

    msg = AsyncMock()
    msg.chat = SimpleNamespace(type=ChatType.SUPERGROUP)
    msg.message_id = 77
    msg.api_kwargs = {}
    # No real ephemeral id on the public slash.
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=900001),
        effective_chat=SimpleNamespace(id=-1010, type=ChatType.SUPERGROUP),
        effective_message=msg,
        callback_query=None,
    )
    sub = SimpleNamespace(twitch_user_id="42", twitch_username="marfapr")
    db = MagicMock()
    db.get_enabled_subscriptions_by_chat_id = MagicMock(return_value=[sub])
    twitch = MagicMock()
    twitch.get_channel_schedule = MagicMock(
        return_value={"segments": [], "vacation": None}
    )
    ctx = MagicMock()
    ctx.bot = AsyncMock()
    ctx.bot.get_me = AsyncMock(return_value=SimpleNamespace(id=1))
    ctx.bot.get_chat_member = AsyncMock(
        return_value=SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR)
    )
    ctx.bot.send_message = AsyncMock(
        return_value=SimpleNamespace(api_kwargs={"ephemeral_message_id": 88})
    )
    ctx.bot.delete_message = AsyncMock()
    ctx.bot.do_api_request = AsyncMock(return_value=True)
    ctx.job_queue = MagicMock()
    ctx.application.bot_data = {"db": db, "twitch": twitch}

    with patch("handlers.when_stream.reply_lang", return_value="ru"):
        await when_stream_command(update, ctx)

    ctx.bot.send_message.assert_awaited()
    assert "reply_parameters" not in (
        ctx.bot.send_message.await_args.kwargs.get("api_kwargs") or {}
    )
    ctx.bot.delete_message.assert_awaited_once_with(
        chat_id=-1010, message_id=77
    )
    assert ctx.bot.do_api_request.await_args.args[0] == "editEphemeralMessageText"
    assert ctx.bot.do_api_request.await_args.kwargs["api_kwargs"]["text"] == t(
        "when_stream_no_schedule", "ru"
    )
    msg.reply_text.assert_not_awaited()
    ctx.job_queue.run_once.assert_called_once()
    run_kw = ctx.job_queue.run_once.call_args.kwargs
    assert run_kw["job_kwargs"]["misfire_grace_time"] == 300
    job_data = run_kw["data"]
    assert [item["ephemeral_message_id"] for item in job_data["ephemerals"]] == [88]
    assert job_data["deletes"] == []


async def _check_when_stream_silent() -> None:
    from handlers.when_stream import when_stream_command

    msg = AsyncMock()
    msg.message_id = 55
    msg.api_kwargs = {}
    msg.ephemeral_message_id = None
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=900001),
        effective_chat=SimpleNamespace(id=-1001, type=ChatType.SUPERGROUP),
        effective_message=msg,
        callback_query=None,
    )
    db = MagicMock()
    db.get_user_locale = MagicMock(return_value="ru")
    db.get_enabled_subscriptions_by_chat_id = MagicMock(return_value=[])
    ctx = MagicMock()
    ctx.bot = AsyncMock()
    ctx.bot.delete_message = AsyncMock()
    ctx.job_queue = MagicMock()
    ctx.application.bot_data = {"db": db, "twitch": MagicMock()}
    await when_stream_command(update, ctx)
    msg.reply_text.assert_not_awaited()
    # No streamers + public slash → still delete the user command.
    ctx.bot.delete_message.assert_awaited_once_with(chat_id=-1001, message_id=55)


async def _check_when_stream_silent_purges_ephemeral_slash() -> None:
    """No streamers in chat → still delete the ephemeral /when (no sticky orphan)."""
    from handlers.when_stream import when_stream_command
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    msg = AsyncMock()
    msg.chat = SimpleNamespace(type=ChatType.SUPERGROUP)
    msg.api_kwargs = {"ephemeral_message_id": 71}
    msg.ephemeral_message_id = 71
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=900001),
        effective_chat=SimpleNamespace(id=-1001, type=ChatType.SUPERGROUP),
        effective_message=msg,
        callback_query=None,
    )
    db = MagicMock()
    db.get_enabled_subscriptions_by_chat_id = MagicMock(return_value=[])
    ctx = MagicMock()
    ctx.bot = AsyncMock()
    ctx.bot.do_api_request = AsyncMock(return_value=True)
    ctx.job_queue = MagicMock()
    ctx.application.bot_data = {"db": db, "twitch": MagicMock()}
    await when_stream_command(update, ctx)
    msg.reply_text.assert_not_awaited()
    ctx.bot.do_api_request.assert_awaited()
    assert ctx.bot.do_api_request.await_args.args[0] == "deleteEphemeralMessage"
    assert (
        ctx.bot.do_api_request.await_args.kwargs["api_kwargs"]["ephemeral_message_id"]
        == 71
    )
    ctx.job_queue.run_once.assert_not_called()


async def _check_when_stream_fetch_fail_purges_trigger() -> None:
    """Twitch schedule fetch fail → purge status + ephemeral slash, not leave orphans."""
    from handlers.when_stream import when_stream_command
    from i18n import t
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch

    msg = AsyncMock()
    msg.chat = SimpleNamespace(type=ChatType.SUPERGROUP)
    msg.api_kwargs = {"ephemeral_message_id": 12}
    msg.ephemeral_message_id = 12
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=900001),
        effective_chat=SimpleNamespace(id=-1009, type=ChatType.SUPERGROUP),
        effective_message=msg,
        callback_query=None,
    )
    sub = SimpleNamespace(twitch_user_id="42", twitch_username="marfapr")
    db = MagicMock()
    db.get_enabled_subscriptions_by_chat_id = MagicMock(return_value=[sub])
    twitch = MagicMock()
    twitch.get_channel_schedule = MagicMock(side_effect=RuntimeError("helix down"))
    ctx = MagicMock()
    ctx.bot = AsyncMock()
    ctx.bot.send_message = AsyncMock(
        return_value=SimpleNamespace(api_kwargs={"ephemeral_message_id": 99})
    )
    ctx.bot.do_api_request = AsyncMock(return_value=True)
    ctx.job_queue = MagicMock()
    ctx.application.bot_data = {"db": db, "twitch": twitch}

    with patch("handlers.when_stream.reply_lang", return_value="ru"):
        await when_stream_command(update, ctx)

    ctx.bot.send_message.assert_awaited()
    assert ctx.bot.send_message.await_args.args[1] == t("schedule_query_working", "ru")
    deleted = [
        c.kwargs["api_kwargs"]["ephemeral_message_id"]
        for c in ctx.bot.do_api_request.await_args_list
        if c.args and c.args[0] == "deleteEphemeralMessage"
    ]
    assert set(deleted) == {99, 12}
    msg.reply_text.assert_not_awaited()
    ctx.job_queue.run_once.assert_not_called()


async def _check_when_stream_no_schedule() -> None:
    from handlers.when_stream import when_stream_command
    from i18n import t
    from telegram.error import BadRequest
    from unittest.mock import patch

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=900001),
        effective_chat=SimpleNamespace(id=-1002, type=ChatType.SUPERGROUP),
        effective_message=AsyncMock(),
        callback_query=None,
    )
    sub = SimpleNamespace(twitch_user_id="42", twitch_username="marfapr")
    db = MagicMock()
    db.get_user_locale = MagicMock(return_value="ru")
    db.get_enabled_subscriptions_by_chat_id = MagicMock(return_value=[sub])
    twitch = MagicMock()
    twitch.get_channel_schedule = MagicMock(
        return_value={"segments": [], "vacation": None}
    )
    ctx = MagicMock()
    ctx.bot = AsyncMock()
    ctx.application.bot_data = {"db": db, "twitch": twitch}
    update.effective_message.chat = SimpleNamespace(type=ChatType.SUPERGROUP)
    with patch("handlers.when_stream.reply_lang", return_value="ru"):
        await when_stream_command(update, ctx)
        update.effective_message.reply_text.assert_awaited_once()
        assert update.effective_message.reply_text.await_args.args[0] == t(
            "when_stream_no_schedule", "ru"
        )
        assert "reply_markup" in update.effective_message.reply_text.await_args.kwargs

        # Trigger message deleted while schedule was fetching → plain send, no raise.
        update.effective_message.reply_text = AsyncMock(
            side_effect=BadRequest("Message to be replied not found")
        )
        update.effective_message.chat = SimpleNamespace(type=ChatType.SUPERGROUP)
        ctx.bot.send_message = AsyncMock()
        await when_stream_command(update, ctx)
        ctx.bot.send_message.assert_awaited_once()
        assert ctx.bot.send_message.await_args.args[:2] == (
            -1002,
            t("when_stream_no_schedule", "ru"),
        )
        assert "reply_markup" in ctx.bot.send_message.await_args.kwargs

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
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=1)
    )
    ctx.bot.delete_message = AsyncMock()
    ctx.application.bot_data = {"db": MagicMock(get_user_locale=MagicMock(return_value="ru"))}

    # Chat admin → DM-only hint once (and ReplyKeyboardRemove).
    with patch(
        "bot_helpers.user_is_telegram_chat_admin",
        new=AsyncMock(return_value=True),
    ):
        reset_group_setup_notified()
        state = await wrapped(update, ctx)
        assert state == ConversationHandler.END
        update.effective_message.reply_text.assert_awaited()
        _rm_kw = update.effective_message.reply_text.await_args.kwargs
        assert "reply_markup" in _rm_kw

    # Non-admin member → silent text, but clear sticky reply keyboard.
    update.effective_message.reply_text.reset_mock()
    ctx.bot.send_message.reset_mock()
    with patch(
        "bot_helpers.user_is_telegram_chat_admin",
        new=AsyncMock(return_value=False),
    ):
        reset_group_setup_notified()
        state = await wrapped(update, ctx)
        assert state == ConversationHandler.END
        update.effective_message.reply_text.assert_not_awaited()
        ctx.bot.send_message.assert_awaited()

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
    clear_bot = MagicMock()
    clear_bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=9))
    clear_bot.delete_message = AsyncMock()
    await reply_setup_private_only(cb_update, "ru", bot=clear_bot)
    cb_update.callback_query.answer.assert_awaited()
    clear_bot.send_message.assert_awaited()

    helpers_remove = (_ROOT / "bot_helpers.py").read_text(encoding="utf-8")
    assert "ReplyKeyboardRemove" in helpers_remove
    assert "clear_group_reply_keyboard" in helpers_remove
    when_src = (_ROOT / "handlers/when_stream.py").read_text(encoding="utf-8")
    assert "ReplyKeyboardRemove" in when_src
    assert "clear_group_reply_keyboard" in when_src

    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=2))
    ctx.bot.delete_message = AsyncMock()
    ctx.application.bot_data = {"db": MagicMock(get_user_locale=MagicMock(return_value="ru"))}
    group_update = SimpleNamespace(
        effective_user=SimpleNamespace(id=900002),
        effective_chat=SimpleNamespace(id=group_id, type=ChatType.SUPERGROUP),
        effective_message=AsyncMock(),
        callback_query=None,
    )
    with patch(
        "bot_helpers.user_is_telegram_chat_admin",
        new=AsyncMock(return_value=True),
    ):
        reset_group_setup_notified()
        assert await handle_group_setup_rejection(group_update, ctx) is True
        group_update.effective_message.reply_text.assert_awaited_once()
        assert await handle_group_setup_rejection(group_update, ctx) is True
        group_update.effective_message.reply_text.assert_awaited_once()
        # Second attempt is silent but still clears sticky keyboard.
        ctx.bot.send_message.assert_awaited()
    reset_group_setup_notified()

    bot_src = (_ROOT / "bot.py").read_text(encoding="utf-8")
    assert 'CommandHandler("help", dm_only_conv_entry(help_command))' in bot_src


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


async def _check_day_week_schedule() -> None:
    from handlers.when_stream import (
        day_schedule_command,
        on_schedule_query_callback,
        week_schedule_command,
    )
    from i18n import t
    from unittest.mock import patch

    now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
    today_slot = {
        "id": "today",
        "start_time": "2030-01-01T15:00:00Z",
        "category": {"name": "Fallout"},
    }
    later_slot = {
        "id": "later",
        "start_time": "2030-01-03T15:00:00Z",
        "category": {"name": "Elden Ring"},
    }
    sub_a = SimpleNamespace(twitch_user_id="1", twitch_username="alpha")
    sub_b = SimpleNamespace(twitch_user_id="2", twitch_username="beta")
    db = MagicMock()
    db.get_user_locale = MagicMock(return_value="ru")
    db.get_enabled_subscriptions_by_chat_id = MagicMock(return_value=[sub_a, sub_b])

    def _sched(uid, **_kwargs):
        if str(uid) == "1":
            return {"segments": [today_slot, later_slot], "vacation": None}
        return {"segments": [later_slot], "vacation": None}

    twitch = MagicMock()
    twitch.get_channel_schedule = MagicMock(side_effect=_sched)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=900001),
        effective_chat=SimpleNamespace(id=-1003, type=ChatType.SUPERGROUP),
        effective_message=AsyncMock(),
        callback_query=None,
    )
    ctx = MagicMock()
    ctx.bot = AsyncMock()
    ctx.application.bot_data = {"db": db, "twitch": twitch}

    def _freeze_now(mock_dt):
        mock_dt.now = MagicMock(return_value=now)
        mock_dt.combine = datetime.combine

    from telegram import InlineKeyboardMarkup, ReplyKeyboardRemove

    update.effective_message.chat = SimpleNamespace(type=ChatType.SUPERGROUP)
    ctx.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=3))
    ctx.bot.delete_message = AsyncMock()

    with patch("handlers.when_stream.reply_lang", return_value="ru"):
        # Day: only alpha has a slot today → single streamer text, no picker.
        with patch("handlers.when_stream.datetime") as mock_dt:
            _freeze_now(mock_dt)
            await day_schedule_command(update, ctx)
        text = update.effective_message.reply_text.await_args.args[0]
        assert "Fallout" in text
        assert "Elden Ring" not in text
        assert "alpha" not in text
        day_markup = update.effective_message.reply_text.await_args.kwargs.get(
            "reply_markup"
        )
        assert isinstance(day_markup, ReplyKeyboardRemove)

        # Week: both have slots → picker with streamer buttons.
        update.effective_message.reply_text.reset_mock()
        with patch("handlers.when_stream.datetime") as mock_dt:
            _freeze_now(mock_dt)
            await week_schedule_command(update, ctx)
        week_call = update.effective_message.reply_text.await_args
        assert week_call.args[0] == t("schedule_found_streamers", "ru")
        markup = week_call.kwargs.get("reply_markup")
        assert isinstance(markup, InlineKeyboardMarkup)
        labels = [btn.text for row in markup.inline_keyboard for btn in row]
        assert labels == ["alpha", "beta"]
        # Sticky reply keyboard cleared before inline picker.
        ctx.bot.send_message.assert_awaited()

        # Pick alpha → schedule detail + back.
        query = SimpleNamespace(
            data="schq:week:1",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            message=SimpleNamespace(chat_id=-1003),
        )
        cb_update = SimpleNamespace(
            effective_user=SimpleNamespace(id=900001),
            effective_chat=SimpleNamespace(id=-1003, type=ChatType.SUPERGROUP),
            effective_message=None,
            callback_query=query,
        )
        with patch("handlers.when_stream.datetime") as mock_dt:
            _freeze_now(mock_dt)
            await on_schedule_query_callback(cb_update, ctx)
        detail = query.edit_message_text.await_args.args[0]
        assert 'href="https://www.twitch.tv/alpha"' in detail
        assert "Fallout" in detail and "Elden Ring" in detail
        assert query.edit_message_text.await_args.kwargs.get("parse_mode")
        back_kb = query.edit_message_text.await_args.kwargs.get("reply_markup")
        assert back_kb.inline_keyboard[0][0].callback_data == "schq:list:week"

        update.effective_message.reply_text.reset_mock()
        twitch.get_channel_schedule = MagicMock(
            return_value={"segments": [], "vacation": None}
        )
        db.get_enabled_subscriptions_by_chat_id = MagicMock(return_value=[sub_a])
        update.effective_message.chat = SimpleNamespace(type=ChatType.SUPERGROUP)
        await day_schedule_command(update, ctx)
        update.effective_message.reply_text.assert_awaited_once()
        assert update.effective_message.reply_text.await_args.args[0] == t(
            "schedule_range_empty_day", "ru"
        )
        assert "reply_markup" in update.effective_message.reply_text.await_args.kwargs


def _check_message_delete_policy() -> None:
    """Bot may delete only its own messages and user commands — via delete_chat_message."""
    import re

    from bot_helpers import delete_chat_message

    assert callable(delete_chat_message)
    helpers = (_ROOT / "bot_helpers.py").read_text(encoding="utf-8")
    assert 'kind: DeleteKind' in helpers or 'kind: Literal["bot_own"' in helpers
    assert "bot_own" in helpers and "user_command" in helpers
    # Single raw Telegram delete call site — inside delete_chat_message.
    raw_calls = [
        m.start()
        for m in re.finditer(r"await bot\.delete_message\s*\(", helpers)
    ]
    assert len(raw_calls) == 1, (
        "bot_helpers.py: only delete_chat_message may call bot.delete_message"
    )

    forbidden = re.compile(
        r"(?:bot\.delete_message\s*\(|effective_message\.delete\s*\(|\bmsg\.delete\s*\()"
    )
    scan_paths = [
        _ROOT / "request_progress.py",
        *(_ROOT / "handlers").glob("*.py"),
    ]
    for path in scan_paths:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if forbidden.search(line):
                raise AssertionError(
                    f"{path.relative_to(_ROOT)}:{i}: use delete_chat_message"
                    f"(..., kind='bot_own'|'user_command'), not raw delete"
                )


def check_group_chat() -> None:
    _check_premium_and_helpers()
    _check_wizard_schedule_reply_targets()
    _check_when_stream_helpers()
    _check_message_delete_policy()
    import asyncio

    asyncio.run(_check_group_setup_gate())
    asyncio.run(_check_ephemeral_helpers())
    asyncio.run(_check_when_stream_ephemeral_path())
    asyncio.run(_check_when_stream_ephemeral_admin_public_slash())
    asyncio.run(_check_when_stream_silent())
    asyncio.run(_check_when_stream_silent_purges_ephemeral_slash())
    asyncio.run(_check_when_stream_fetch_fail_purges_trigger())
    asyncio.run(_check_when_stream_no_schedule())
    asyncio.run(_check_day_week_schedule())
