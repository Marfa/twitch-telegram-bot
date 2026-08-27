"""Handler entrypoint smoke: post-split NameError / Forbidden soft-fail paths.

Exercises free-user and gated branches with mocked Telegram/Twitch — not a live
E2E against Telegram or Twitch APIs.
"""
from __future__ import annotations

import asyncio
import inspect
import tempfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.error import Forbidden
from telegram.ext import ConversationHandler

from db import open_database
from links import TelegramTopicLink
import premium as prem

_FREE_UID = 900001
_ADMIN_UID = 1


def _app(db, twitch=None):
    bot = AsyncMock()
    bot.id = 42
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
    bot.get_chat = AsyncMock()
    bot.set_chat_menu_button = AsyncMock()
    application = MagicMock()
    application.bot = bot
    application.bot_data = {
        "db": db,
        "twitch": twitch or MagicMock(),
        "pending_schedule_publishes": {},
    }
    return application, bot


def _ctx(application, user_data=None):
    ctx = MagicMock()
    ctx.application = application
    ctx.bot = application.bot
    ctx.user_data = user_data if user_data is not None else {}
    ctx.chat_data = {}
    return ctx


def _cb_update(user_id: int, data: str):
    query = AsyncMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.from_user = SimpleNamespace(id=user_id)
    query.message = SimpleNamespace(chat_id=user_id, message_id=1)
    update = MagicMock()
    update.callback_query = query
    update.effective_user = SimpleNamespace(id=user_id)
    update.effective_message = MagicMock()
    update.effective_message.reply_text = AsyncMock()
    update.effective_message.text = ""
    update.effective_chat = SimpleNamespace(id=user_id)
    return update, query


def _msg_update(user_id: int, text: str = "hi"):
    update = MagicMock()
    update.callback_query = None
    update.effective_user = SimpleNamespace(id=user_id)
    update.effective_chat = SimpleNamespace(id=user_id)
    msg = MagicMock()
    msg.reply_text = AsyncMock()
    msg.text = text
    msg.chat_id = user_id
    update.effective_message = msg
    return update


async def _smoke_schedule(db) -> None:
    import handlers.stream_schedule as schedule_mod
    from handlers.stream_schedule import (
        _complete_schedule_publish,
        _prompt_stream_schedule_fix_game,
        _prompt_stream_schedule_fix_time,
        _sched_states,
        format_utc_offset,
        parse_utc_offset_text,
        start_stream_schedule,
        stream_schedule_duration_callback,
        stream_schedule_fix_edit_callback,
        stream_schedule_fix_game,
        stream_schedule_fix_time,
        stream_schedule_mode_callback,
        stream_schedule_publish_callback,
        stream_schedule_tz,
        stream_schedule_tz_callback,
    )

    # Regression: NameError prem / _wizard (PostHog 01a03a79 / 01a03a80)
    assert schedule_mod.prem is not None
    assert callable(schedule_mod._wizard)

    st = _sched_states()
    db.upsert_user(_FREE_UID)
    db.set_schedule_utc_offset_minutes(_FREE_UID, 180)
    assert db.get_schedule_utc_offset_minutes(_FREE_UID) == 180
    assert parse_utc_offset_text("UTC+3") == 180
    assert parse_utc_offset_text("UTC-5") == -300
    assert format_utc_offset(180) == "UTC+3"

    application, bot = _app(db)
    update = _msg_update(_FREE_UID)
    ctx = _ctx(application)
    with patch("handlers.stream_schedule.beta_features.is_enabled", return_value=False):
        state = await start_stream_schedule(update, ctx)
    assert state == st["STREAM_SCHEDULE_CONFIRM"]

    application, bot = _app(db)
    update = _msg_update(_FREE_UID)
    ctx = _ctx(application)
    with patch("handlers.stream_schedule.beta_features.is_enabled", return_value=True):
        state = await start_stream_schedule(update, ctx)
    assert state == st["STREAM_SCHEDULE_MODE"]

    application, bot = _app(db)
    update, query = _cb_update(_FREE_UID, "stream_sched:tz:mode")
    ctx = _ctx(application)
    state = await stream_schedule_tz_callback(update, ctx)
    assert state == st["STREAM_SCHEDULE_TZ"]
    query.edit_message_text.assert_awaited()
    assert "UTC+3" in query.edit_message_text.await_args.args[0]

    application, bot = _app(db)
    update = _msg_update(_FREE_UID, text="UTC-5")
    ctx = _ctx(application, {"stream_schedule_tz_resume": "mode"})
    state = await stream_schedule_tz(update, ctx)
    assert state == st["STREAM_SCHEDULE_MODE"]
    assert db.get_schedule_utc_offset_minutes(_FREE_UID) == -300
    db.set_schedule_utc_offset_minutes(_FREE_UID, 180)

    application, bot = _app(db)
    update, _query = _cb_update(_FREE_UID, "stream_sched:mode:week")
    ctx = _ctx(application)
    state = await stream_schedule_mode_callback(update, ctx)
    assert state == st["STREAM_SCHEDULE_CONFIRM"]

    application, bot = _app(db)
    update, _query = _cb_update(_FREE_UID, "ss:pub:1")
    ctx = _ctx(
        application,
        {
            "stream_schedule_entries": [{"title": "t"}],
            "stream_schedule_updates": [],
        },
    )
    with patch(
        "handlers.stream_schedule.prem.has_feature",
        new=AsyncMock(return_value=False),
    ), patch("premium_handlers.send_premium_screen", new=AsyncMock()):
        state = await stream_schedule_publish_callback(update, ctx)
    assert state == ConversationHandler.END

    application, bot = _app(db)
    # Unknown TZ → ask before duration
    db_no_tz = db
    other = _FREE_UID + 1
    db_no_tz.upsert_user(other)
    update, _query = _cb_update(other, "ss:pub:1")
    ctx = _ctx(
        application,
        {
            "stream_schedule_entries": [{"title": "t"}],
            "stream_schedule_updates": [],
            "stream_schedule_clear_mode": "overlap",
        },
    )
    with patch(
        "handlers.stream_schedule.prem.has_feature",
        new=AsyncMock(return_value=True),
    ):
        state = await stream_schedule_publish_callback(update, ctx)
    assert state == st["STREAM_SCHEDULE_TZ"]

    application, bot = _app(db)
    update, _query = _cb_update(_FREE_UID, "ss:pub:1")
    ctx = _ctx(
        application,
        {
            "stream_schedule_entries": [{"title": "t"}],
            "stream_schedule_updates": [],
            "stream_schedule_clear_mode": "overlap",
        },
    )
    with patch(
        "handlers.stream_schedule.prem.has_feature",
        new=AsyncMock(return_value=True),
    ):
        state = await stream_schedule_publish_callback(update, ctx)
    assert state == st["STREAM_SCHEDULE_DURATION"]

    application, bot = _app(db)
    update, _query = _cb_update(_FREE_UID, "ss:fixedit:0")
    ctx = _ctx(
        application,
        {
            "stream_schedule_fix_date": date.today(),
            "stream_schedule_existing": [{"id": "seg1", "time": "12:00"}],
            "stream_schedule_entries": [],
            "stream_schedule_updates": [],
        },
    )
    with patch(
        "handlers.stream_schedule._prompt_stream_schedule_fix_game",
        new=AsyncMock(return_value=st["STREAM_SCHEDULE_FIX_GAME"]),
    ) as prompt:
        await stream_schedule_fix_edit_callback(update, ctx)
    assert ctx.user_data.get("stream_schedule_edit_id") == "seg1"
    prompt.assert_awaited()

    application, bot = _app(db)
    update, _query = _cb_update(_FREE_UID, "ss:x")
    ctx = _ctx(application, {"stream_schedule_fix_date": date.today()})
    state = await _prompt_stream_schedule_fix_game(update, ctx, "ru")
    assert state == st["STREAM_SCHEDULE_FIX_GAME"]
    bot.send_message.assert_awaited()

    application, bot = _app(db)
    update = _msg_update(_FREE_UID, "Just Chatting")
    ctx = _ctx(application, {"stream_schedule_fix_date": date.today()})
    state = await stream_schedule_fix_game(update, ctx)
    assert state == st["STREAM_SCHEDULE_FIX_TIME"]
    assert ctx.user_data["stream_schedule_fix_game"] == "Just Chatting"

    application, bot = _app(db)
    update = _msg_update(_FREE_UID)
    ctx = _ctx(application)
    state = await _prompt_stream_schedule_fix_time(update, ctx, "ru")
    assert state == st["STREAM_SCHEDULE_FIX_TIME"]

    application, bot = _app(db)
    update = _msg_update(_FREE_UID, "19:30")
    day = date.today()
    ctx = _ctx(
        application,
        {
            "stream_schedule_fix_date": day,
            "stream_schedule_fix_game": "Game",
            "stream_schedule_edit_id": "seg1",
            "stream_schedule_updates": [],
            "stream_schedule_entries": [],
            "stream_schedule_existing": [{"id": "seg1", "time": "12:00"}],
        },
    )
    with patch(
        "handlers.stream_schedule._show_day_slots",
        new=AsyncMock(return_value=st["STREAM_SCHEDULE_FIX_SLOTS"]),
    ):
        state = await stream_schedule_fix_time(update, ctx)
    assert state == st["STREAM_SCHEDULE_FIX_SLOTS"]

    application, bot = _app(db)
    update, _query = _cb_update(_FREE_UID, "ss:dur:2")
    ctx = _ctx(
        application,
        {
            "stream_schedule_entries": [
                {"date": day, "time": "19:30", "game": "Game"}
            ],
            "stream_schedule_updates": [],
            "stream_schedule_clear_mode": "overlap",
        },
    )
    with patch(
        "handlers.stream_schedule.twitch_oauth_redirect_uri",
        return_value="",
        create=True,
    ), patch(
        "config.twitch_oauth_redirect_uri", return_value=""
    ):
        state = await stream_schedule_duration_callback(update, ctx)
    assert state == ConversationHandler.END

    application, bot = _app(db)
    await _complete_schedule_publish(application, _FREE_UID, "oauth_error", None)
    await _complete_schedule_publish(application, _FREE_UID, None, None)


async def _smoke_settings_oauth(db) -> None:
    from handlers.settings import (
        complete_chat_oauth,
        complete_whisper_oauth,
        notify_whisper_received,
        on_whisper_eventsub_revoked,
        open_settings_menu,
        open_stream_chat,
        open_sys_notifications_menu,
        open_whisper_alerts_menu,
    )

    application, bot = _app(db)
    alert = SimpleNamespace(owner_id=_FREE_UID, twitch_login="x")
    event = SimpleNamespace(
        to_user_id="tw1",
        from_user_id="tw2",
        to_user_login="a",
        from_user_login="b",
        from_user_name="Bob",
        text="hello",
    )
    with patch.object(
        db, "get_whisper_alerts_by_twitch_user_id", return_value=[alert]
    ), patch.object(db, "is_bot_blocked", return_value=False):
        await notify_whisper_received(application, event)
    bot.send_message.assert_awaited()

    bot_forbidden = AsyncMock()
    bot_forbidden.send_message = AsyncMock(side_effect=Forbidden("blocked"))
    application.bot = bot_forbidden
    with patch.object(
        db, "get_whisper_alerts_by_twitch_user_id", return_value=[alert]
    ), patch.object(db, "is_bot_blocked", return_value=False), patch.object(
        db, "set_bot_blocked"
    ) as set_blocked:
        await notify_whisper_received(application, event)
        set_blocked.assert_called_with(_FREE_UID, True)

    application.bot = bot
    with patch.object(
        db, "disable_whisper_alerts_for_twitch_user", return_value=[_FREE_UID]
    ), patch.object(db, "is_bot_blocked", return_value=False):
        await on_whisper_eventsub_revoked(application, "tw1")

    await complete_whisper_oauth(application, _FREE_UID, "access_denied", None)
    await complete_whisper_oauth(
        application, _FREE_UID, None, {"access_token": "", "twitch_user_id": ""}
    )
    await complete_chat_oauth(application, _FREE_UID, "err", None)
    await complete_chat_oauth(
        application,
        _FREE_UID,
        None,
        {
            "refresh_token": "rt",
            "twitch_user_id": "99",
            "twitch_login": "u",
        },
    )
    assert db.get_chat_auth(_FREE_UID).twitch_login == "u"

    update = _msg_update(_FREE_UID)
    ctx = _ctx(application)
    with patch("beta.is_enabled", return_value=False):
        await open_stream_chat(update, ctx)
    update.effective_message.reply_text.assert_awaited()

    for opener in (
        open_settings_menu,
        open_whisper_alerts_menu,
        open_sys_notifications_menu,
    ):
        update = _msg_update(_FREE_UID)
        ctx = _ctx(application)
        await opener(update, ctx)
        update.effective_message.reply_text.assert_awaited()


async def _smoke_watch(db) -> None:
    from handlers.watch import (
        _ws,
        receive_watch_category_callback,
        receive_watch_mature_callback,
        receive_watch_nav_back,
        receive_watch_save_callback,
        receive_watch_tags_callback,
        start_what_to_watch,
    )

    application, bot = _app(db)
    update = _msg_update(_FREE_UID)
    ctx = _ctx(application)
    with patch(
        "handlers.watch._start_watch_wizard",
        new=AsyncMock(return_value=_ws()["WATCH_CATEGORIES"]),
    ) as start_wiz:
        state = await start_what_to_watch(update, ctx)
    start_wiz.assert_awaited()
    assert state == _ws()["WATCH_CATEGORIES"]

    update, _query = _cb_update(_FREE_UID, "wizard:back")
    ctx = _ctx(application)
    with patch(
        "handlers.wizard.wizard_back", new=AsyncMock(return_value=42)
    ) as wizard_back:
        state = await receive_watch_nav_back(update, ctx)
    assert state == 42
    wizard_back.assert_awaited()

    update, _query = _cb_update(_FREE_UID, "watch_cat:lucky")
    ctx = _ctx(application, {"watch_categories": []})
    with patch(
        "handlers.watch._fetch_lucky_watch_suggestions",
        new=AsyncMock(return_value=([], [], [])),
    ):
        state = await receive_watch_category_callback(update, ctx)
    assert state == _ws()["WATCH_CATEGORIES"]

    update, _query = _cb_update(_FREE_UID, "watch_tags:skip")
    ctx = _ctx(application)
    with patch(
        "handlers.watch._go_watch_viewers_prompt",
        new=AsyncMock(return_value=_ws()["WATCH_VIEWERS"]),
    ):
        state = await receive_watch_tags_callback(update, ctx)
    assert state == _ws()["WATCH_VIEWERS"]
    assert ctx.user_data["watch_tags"] == []

    update, _query = _cb_update(_FREE_UID, "watch_mature:1")
    ctx = _ctx(application)
    with patch(
        "handlers.watch._go_watch_save_prompt",
        new=AsyncMock(return_value=_ws()["WATCH_SAVE"]),
    ):
        state = await receive_watch_mature_callback(update, ctx)
    assert state == _ws()["WATCH_SAVE"]
    assert ctx.user_data["watch_exclude_mature"] is True

    update, _query = _cb_update(_FREE_UID, "watch_save:0")
    ctx = _ctx(
        application,
        {
            "watch_categories": [{"id": "1", "name": "JC"}],
            "watch_tags": [],
            "watch_min_viewers": 0,
            "watch_max_viewers": None,
            "watch_language": None,
            "watch_exclude_mature": True,
        },
    )
    with patch(
        "handlers.watch._complete_watch_wizard",
        new=AsyncMock(return_value=ConversationHandler.END),
    ) as complete:
        state = await receive_watch_save_callback(update, ctx)
    complete.assert_awaited()
    assert state == ConversationHandler.END


async def _smoke_wizard(db) -> None:
    import handlers.wizard as wz
    from bot import _resolve_from_topic_link
    from handlers.wizard import (
        _wz,
        cancel,
        receive_alert_type,
        receive_channel,
        receive_dest_chat,
        receive_dest_type,
        start_new_subscription,
    )

    application, bot = _app(db)
    update = _msg_update(_FREE_UID)
    ctx = _ctx(application)
    with patch(
        "handlers.wizard._go_alert_type_prompt",
        new=AsyncMock(return_value=_wz()["ALERT_TYPE"]),
    ):
        state = await start_new_subscription(update, ctx)
    assert state == _wz()["ALERT_TYPE"]

    update, _query = _cb_update(_FREE_UID, "alert:live")
    ctx = _ctx(application)
    with patch(
        "handlers.wizard._go_channel_prompt",
        new=AsyncMock(return_value=_wz()["CHANNEL"]),
    ):
        state = await receive_alert_type(update, ctx)
    assert state == _wz()["CHANNEL"]
    assert ctx.user_data["alert_type"] == "live"

    twitch = MagicMock()
    twitch.parse_username.return_value = "streamer"
    twitch.get_user.return_value = {"id": "1", "login": "streamer", "display_name": "S"}
    application, bot = _app(db, twitch=twitch)
    update = _msg_update(_FREE_UID, "https://twitch.tv/streamer")
    ctx = _ctx(application, {"alert_type": "live", "notify_on_live": True})
    with patch(
        "handlers.wizard._go_template_prompt",
        new=AsyncMock(return_value=_wz()["TEMPLATE"]),
    ):
        state = await receive_channel(update, ctx)
    assert state == _wz()["TEMPLATE"]

    update, _query = _cb_update(_FREE_UID, "dest:channel")
    ctx = _ctx(application, {"alert_type": "live", "twitch_username": "streamer"})
    state = await receive_dest_type(update, ctx)
    assert state == _wz()["DEST_CHAT"]

    application, bot = _app(db)
    bot.get_chat = AsyncMock(side_effect=Forbidden("forbidden"))
    link = TelegramTopicLink(chat_ref="SomeGroup", thread_id=1)
    raised = False
    try:
        await _resolve_from_topic_link(bot, link)
    except Forbidden:
        raised = True
    assert raised
    assert "Forbidden" in inspect.getsource(wz.receive_dest_chat)
    assert "Forbidden" in inspect.getsource(wz._parse_dest_input)

    application, bot = _app(db)
    bot.get_chat = AsyncMock(side_effect=Forbidden("not a member"))
    update = _msg_update(_FREE_UID, "@somechannel")
    ctx = _ctx(application, {"dest_type": "channel", "twitch_username": "x"})
    state = await receive_dest_chat(update, ctx)
    update.effective_message.reply_text.assert_awaited()
    assert state == _wz()["DEST_CHAT"]

    application, bot = _app(db)
    update = _msg_update(_FREE_UID)
    ctx = _ctx(application, {"twitch_username": "x"})
    state = await cancel(update, ctx)
    assert state == ConversationHandler.END


async def _smoke_subscriptions(db) -> None:
    from handlers.subscriptions import (
        _deliver_subs_list,
        list_subscriptions,
        open_subscriptions_menu,
        start_twitch_import,
    )

    application, bot = _app(db)
    update = _msg_update(_FREE_UID)
    ctx = _ctx(application)
    await open_subscriptions_menu(update, ctx)
    update.effective_message.reply_text.assert_awaited()

    update = _msg_update(_FREE_UID)
    ctx = _ctx(application)
    await list_subscriptions(update, ctx)
    update.effective_message.reply_text.assert_awaited()

    reply = MagicMock()
    reply.reply_text = AsyncMock()
    fake_sub = MagicMock()
    fake_sub.id = 1
    fake_sub.enabled = True
    fake_sub.twitch_username = "x"
    with patch(
        "handlers.subscriptions._format_subs_overview_lines",
        new=AsyncMock(return_value=(["1. x"], [fake_sub])),
    ), patch(
        "handlers.subscriptions._bot_username",
        new=AsyncMock(return_value="testbot"),
    ), patch(
        "handlers.subscriptions._subs_toggle_keyboard",
        return_value=[],
    ):
        await _deliver_subs_list(
            bot=bot,
            db=db,
            owner_id=_FREE_UID,
            lang="ru",
            subs=[],
            reply_message=reply,
        )
    reply.reply_text = AsyncMock(side_effect=Forbidden("blocked"))
    with patch(
        "handlers.subscriptions._format_subs_overview_lines",
        new=AsyncMock(return_value=(["1. x"], [fake_sub])),
    ), patch(
        "handlers.subscriptions._bot_username",
        new=AsyncMock(return_value="testbot"),
    ), patch(
        "handlers.subscriptions._subs_toggle_keyboard",
        return_value=[],
    ):
        await _deliver_subs_list(
            bot=bot,
            db=db,
            owner_id=_FREE_UID,
            lang="ru",
            subs=[fake_sub],
            reply_message=reply,
        )

    update = _msg_update(_FREE_UID)
    ctx = _ctx(application)
    with patch("config.twitch_oauth_redirect_uri", return_value=""):
        await start_twitch_import(update, ctx)
    update.effective_message.reply_text.assert_awaited()

    # Chat button on + preview left on: still save template, force preview off.
    from bot import _save_edit_template

    sub_id = db.add_subscription(
        owner_id=_FREE_UID,
        twitch_username="chatbtn",
        twitch_user_id="9001",
        message_template="old {username}",
        dest_type="dm",
        chat_id=_FREE_UID,
        thread_id=None,
        disable_link_preview=False,
        attach_chat_button=True,
        enabled=True,
    )
    update = _msg_update(_FREE_UID, text="new {username}\nhttps://twitch.tv/{username}")
    update.effective_message.link_preview_options = None
    ctx = _ctx(application, user_data={"edit_sub_id": sub_id})
    state = await _save_edit_template(
        update, ctx, "ru", "new {username}\nhttps://twitch.tv/{username}"
    )
    assert state == ConversationHandler.END
    saved = db.get_subscription(sub_id, _FREE_UID)
    assert saved is not None
    assert saved.message_template.startswith("new ")
    assert saved.disable_link_preview is True
    assert all(
        "превью" not in str(c).lower() and "preview" not in str(c).lower()
        for c in bot.send_message.await_args_list
    )


async def _smoke_premium_and_menus(db) -> None:
    from bot import (
        on_premium_callback_router,
        open_premium_from_settings,
        precheckout_premium_router,
        successful_premium_payment_router,
    )
    from handlers.admin_stats import admin_show_stats
    from handlers.alert_history import show_alert_history
    from handlers.broadcast import admin_broadcast_start, admin_select_type
    from handlers.partner import open_partner_menu
    from premium_handlers import (
        on_premium_callback,
        precheckout_premium,
        successful_premium_payment,
    )

    application, bot = _app(db)
    update = _msg_update(_FREE_UID)
    ctx = _ctx(application)
    with patch("premium_handlers.open_premium_menu", new=AsyncMock()) as m:
        await open_premium_from_settings(update, ctx)
        m.assert_awaited()
    update2, _query = _cb_update(_FREE_UID, "prem:x")
    with patch("premium_handlers.on_premium_callback", new=AsyncMock()) as m:
        await on_premium_callback_router(update2, ctx)
        m.assert_awaited()
    with patch("premium_handlers.precheckout_premium", new=AsyncMock()) as m:
        await precheckout_premium_router(update2, ctx)
        m.assert_awaited()
    with patch(
        "premium_handlers.successful_premium_payment", new=AsyncMock()
    ) as m:
        await successful_premium_payment_router(update2, ctx)
        m.assert_awaited()

    application, bot = _app(db)
    payload = prem.invoice_payload(_FREE_UID, "month")
    pq = MagicMock()
    pq.invoice_payload = payload
    pq.answer = AsyncMock()
    update = MagicMock()
    update.pre_checkout_query = pq
    update.effective_user = SimpleNamespace(id=_FREE_UID)
    ctx = _ctx(application)
    await precheckout_premium(update, ctx)
    pq.answer.assert_awaited()

    for kind in ("month", "year", "life"):
        uid = _FREE_UID + hash(kind) % 1000
        db.upsert_user(uid)
        db.set_user_locale(uid, "ru")
        payload = prem.invoice_payload(uid, kind)
        payment = SimpleNamespace(
            invoice_payload=payload,
            telegram_payment_charge_id=f"chg_{kind}",
            total_amount=100,
            subscription_expiration_date=None,
        )
        msg = MagicMock()
        msg.successful_payment = payment
        msg.reply_text = AsyncMock()
        update = MagicMock()
        update.message = msg
        update.effective_user = SimpleNamespace(id=uid)
        ctx = _ctx(application)
        await successful_premium_payment(update, ctx)

    # Trial confirm callback
    update, query = _cb_update(_FREE_UID + 7, "premium:trial_confirm")
    db.upsert_user(_FREE_UID + 7)
    db.set_user_locale(_FREE_UID + 7, "ru")
    ctx = _ctx(application)
    await on_premium_callback(update, ctx)
    query.answer.assert_awaited()

    # Invoice link failure soft path
    update, query = _cb_update(_FREE_UID, "premium:month")
    ctx = _ctx(application)
    bot = application.bot
    bot.create_invoice_link = AsyncMock(side_effect=RuntimeError("no stars"))
    query.get_bot = MagicMock(return_value=bot)
    await on_premium_callback(update, ctx)
    query.edit_message_text.assert_awaited()

    update = _msg_update(_FREE_UID)
    ctx = _ctx(application)
    await open_partner_menu(update, ctx)
    update.effective_message.reply_text.assert_awaited()

    update = _msg_update(_FREE_UID)
    ctx = _ctx(application)
    with patch(
        "handlers.alert_history.prem.has_feature",
        new=AsyncMock(return_value=False),
    ):
        await show_alert_history(update, ctx)
    update.effective_message.reply_text.assert_awaited()

    application, bot = _app(db)
    update = _msg_update(_FREE_UID)
    ctx = _ctx(application)
    with patch("handlers.broadcast._can_use_admin_tools", return_value=False):
        state = await admin_broadcast_start(update, ctx)
    assert state == ConversationHandler.END
    update = _msg_update(_ADMIN_UID)
    ctx = _ctx(application)
    with patch("handlers.broadcast._can_use_admin_tools", return_value=True):
        state = await admin_broadcast_start(update, ctx)
    assert state != ConversationHandler.END

    update, _query = _cb_update(_ADMIN_UID, "admin:bot_update")
    ctx = _ctx(application)
    with patch("handlers.broadcast._is_admin", return_value=True):
        state = await admin_select_type(update, ctx)
    assert state != ConversationHandler.END
    assert ctx.user_data.get("admin_msg_type") == "bot_update"

    update = _msg_update(_ADMIN_UID)
    ctx = _ctx(application)
    with patch("handlers.admin_stats._can_use_admin_tools", return_value=True):
        await admin_show_stats(update, ctx)
    update.effective_message.reply_text.assert_awaited()


async def _smoke_delivery_and_helpers(db) -> None:
    from bot_helpers import _pulse_wizard_keyboard
    from handlers.delivery import _resolve_chat_display_name
    from handlers.notifications import check_schedule_reminders

    bot = AsyncMock()
    bot.send_message = AsyncMock(side_effect=Forbidden("blocked"))
    await _pulse_wizard_keyboard(bot, _FREE_UID, "ru")

    bot = AsyncMock()
    bot.get_chat = AsyncMock(side_effect=Forbidden("nope"))
    sub = SimpleNamespace(chat_id=-1001, dest_type="channel")
    name = await _resolve_chat_display_name(bot, sub)
    assert name == "-1001"

    application, bot = _app(db)
    ctx = _ctx(application)
    ctx.job = None
    # empty due set — should return without crash
    await check_schedule_reminders(ctx)


async def _run_smoke() -> None:
    with tempfile.TemporaryDirectory() as td:
        db = open_database(Path(td) / "smoke.db")
        db.upsert_user(_FREE_UID)
        db.upsert_user(_ADMIN_UID)
        db.set_user_locale(_FREE_UID, "ru")
        db.set_user_locale(_ADMIN_UID, "ru")

        await _smoke_schedule(db)
        await _smoke_settings_oauth(db)
        await _smoke_watch(db)
        await _smoke_wizard(db)
        await _smoke_subscriptions(db)
        await _smoke_premium_and_menus(db)
        await _smoke_delivery_and_helpers(db)


def check_handler_smoke() -> None:
    asyncio.run(_run_smoke())
