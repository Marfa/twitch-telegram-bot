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
    assert 'CommandHandler("when"' in error_src
    assert 'CommandHandler("day"' in error_src
    assert 'CommandHandler("week"' in error_src
    assert "GROUP_DM_ONLY_COMMANDS" in error_src
    assert "raise ApplicationHandlerStop" in error_src
    from i18n import t as _t

    assert "личн" in _t("setup_private_only", "ru").lower()
    assert "private chat" in _t("setup_private_only", "en").lower()


def _check_when_stream_helpers() -> None:
    from handlers.when_stream import (
        DAY_SCHEDULE_TEXT_RE,
        WEEK_SCHEDULE_TEXT_RE,
        WHEN_STREAM_TEXT_RE,
        format_range_schedule_line,
        format_range_streamer_block,
        format_when_stream_line,
        next_upcoming_segment,
        reset_when_stream_cooldown,
        schedule_window_end,
        segments_in_window,
        unique_streamers_for_chat,
        _cooldown_allows,
        _mark_cooldown,
    )
    from i18n import t

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
        username="alpha",
        slots=[
            (datetime(2030, 1, 2, 15, 0, tzinfo=timezone.utc), "Elden Ring"),
        ],
        show_username=True,
    )
    assert multi_block.startswith('<a href="https://www.twitch.tv/alpha">alpha</a>:')
    assert "- 2 января 18:00 Elden Ring" in multi_block

    line = format_when_stream_line(
        lang="ru",
        username="alpha",
        start=datetime(2030, 1, 2, 15, 0, tzinfo=timezone.utc),
        category="Elden Ring",
        show_username=False,
    )
    assert line.startswith("Следующий эфир будет ")
    assert "Категория Elden Ring" in line
    assert "MSK" in line
    named = format_when_stream_line(
        lang="en",
        username="alpha",
        start=datetime(2030, 1, 2, 15, 0, tzinfo=timezone.utc),
        category="Elden Ring",
        show_username=True,
    )
    assert 'href="https://www.twitch.tv/alpha"' in named
    assert ">alpha</a>:" in named
    assert "Category Elden Ring" in named
    named_esc = format_when_stream_line(
        lang="en",
        username="alpha",
        start=datetime(2030, 1, 2, 15, 0, tzinfo=timezone.utc),
        category="A < B",
        show_username=True,
    )
    assert "A &lt; B" in named_esc
    assert t("when_stream_no_schedule", "ru").startswith("Откуда мне-то знать?")

    reset_when_stream_cooldown()
    assert _cooldown_allows(-1001, now=now)
    _mark_cooldown(-1001, now=now)
    assert not _cooldown_allows(-1001, now=now)
    assert _cooldown_allows(-1001, now=datetime(2030, 1, 1, 12, 1, tzinfo=timezone.utc))
    reset_when_stream_cooldown()


async def _check_when_stream_silent() -> None:
    from handlers.when_stream import reset_when_stream_cooldown, when_stream_command

    reset_when_stream_cooldown()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=900001),
        effective_chat=SimpleNamespace(id=-1001, type=ChatType.SUPERGROUP),
        effective_message=AsyncMock(),
        callback_query=None,
    )
    db = MagicMock()
    db.get_user_locale = MagicMock(return_value="ru")
    db.get_enabled_subscriptions_by_chat_id = MagicMock(return_value=[])
    ctx = MagicMock()
    ctx.application.bot_data = {"db": db, "twitch": MagicMock()}
    await when_stream_command(update, ctx)
    update.effective_message.reply_text.assert_not_awaited()


async def _check_when_stream_no_schedule() -> None:
    from handlers.when_stream import reset_when_stream_cooldown, when_stream_command
    from i18n import t
    from telegram.error import BadRequest

    reset_when_stream_cooldown()
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
    await when_stream_command(update, ctx)
    update.effective_message.reply_text.assert_awaited_once_with(
        t("when_stream_no_schedule", "ru")
    )
    # Cooldown: second call is silent.
    update.effective_message.reply_text.reset_mock()
    await when_stream_command(update, ctx)
    update.effective_message.reply_text.assert_not_awaited()
    reset_when_stream_cooldown()

    # Trigger message deleted while schedule was fetching → plain send, no raise.
    update.effective_message.reply_text = AsyncMock(
        side_effect=BadRequest("Message to be replied not found")
    )
    ctx.bot.send_message = AsyncMock()
    await when_stream_command(update, ctx)
    ctx.bot.send_message.assert_awaited_once_with(
        -1002, t("when_stream_no_schedule", "ru")
    )
    reset_when_stream_cooldown()

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
    ctx.application.bot_data = {"db": MagicMock(get_user_locale=MagicMock(return_value="ru"))}

    # Chat admin → DM-only hint once.
    with patch(
        "bot_helpers.user_is_telegram_chat_admin",
        new=AsyncMock(return_value=True),
    ):
        reset_group_setup_notified()
        state = await wrapped(update, ctx)
        assert state == ConversationHandler.END
        update.effective_message.reply_text.assert_awaited()

    # Non-admin member → silent (no help / no hint).
    update.effective_message.reply_text.reset_mock()
    with patch(
        "bot_helpers.user_is_telegram_chat_admin",
        new=AsyncMock(return_value=False),
    ):
        reset_group_setup_notified()
        state = await wrapped(update, ctx)
        assert state == ConversationHandler.END
        update.effective_message.reply_text.assert_not_awaited()

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
    ctx.bot = MagicMock()
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
        reset_when_stream_cooldown,
        week_schedule_command,
    )
    from i18n import t
    from unittest.mock import patch

    reset_when_stream_cooldown()
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

    with patch("handlers.when_stream.datetime") as mock_dt:
        _freeze_now(mock_dt)
        await day_schedule_command(update, ctx)
    text = update.effective_message.reply_text.await_args.args[0]
    assert "alpha" in text and "beta" not in text
    assert "Fallout" in text
    assert "Elden Ring" not in text
    assert update.effective_message.reply_text.await_args.kwargs.get("parse_mode")

    reset_when_stream_cooldown()
    update.effective_message.reply_text.reset_mock()
    with patch("handlers.when_stream.datetime") as mock_dt:
        _freeze_now(mock_dt)
        await week_schedule_command(update, ctx)
    week_text = update.effective_message.reply_text.await_args.args[0]
    assert "alpha" in week_text and "beta" in week_text
    assert "Fallout" in week_text and "Elden Ring" in week_text

    reset_when_stream_cooldown()
    update.effective_message.reply_text.reset_mock()
    twitch.get_channel_schedule = MagicMock(
        return_value={"segments": [], "vacation": None}
    )
    db.get_enabled_subscriptions_by_chat_id = MagicMock(return_value=[sub_a])
    await day_schedule_command(update, ctx)
    update.effective_message.reply_text.assert_awaited_once_with(
        t("schedule_range_empty_day", "ru")
    )
    reset_when_stream_cooldown()


def check_group_chat() -> None:
    _check_premium_and_helpers()
    _check_wizard_schedule_reply_targets()
    _check_when_stream_helpers()
    import asyncio

    asyncio.run(_check_group_setup_gate())
    asyncio.run(_check_when_stream_silent())
    asyncio.run(_check_when_stream_no_schedule())
    asyncio.run(_check_day_week_schedule())
