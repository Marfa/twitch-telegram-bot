"""User-flow navigation: every wizard/submenu screen exposes Cancel, Back, or menu."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from db import open_database
from i18n import (
    SUPPORTED_LOCALES,
    admin_menu,
    admin_other_audience_keyboard,
    admin_type_keyboard,
    alert_type_keyboard,
    broadcast_menu,
    btn,
    delete_all_confirm_keyboard,
    ignored_words_keyboard,
    main_menu,
    other_menu,
    partner_menu,
    premium_gate_keyboard,
    premium_gift_keyboard,
    settings_menu,
    language_keyboard,
    stream_schedule_confirm_keyboard,
    stream_schedule_duration_keyboard,
    stream_schedule_mode_keyboard,
    stream_schedule_vacation_active_keyboard,
    stream_schedule_vacation_auto_keyboard,
    stored_typo_fix_keyboard,
    template_typo_keyboard,
    subscriptions_menu,
    watch_cats_nav_keyboard,
    watch_filters_keyboard,
    watch_lang_keyboard,
    watch_mature_keyboard,
    watch_save_keyboard,
    watch_tags_keyboard,
    watch_viewers_keyboard,
    wizard_menu,
)
from self_check.flow_nav import (
    markup_has_escape_hatch,
    markups_from_call,
    turn_has_escape_hatch,
)

_FREE_UID = 900001
_ADMIN_UID = 1


class _BotCapture:
    def __init__(self) -> None:
        self.markups: list = []
        self.pulsed_wizard = False

    def wrap(self, bot: AsyncMock) -> AsyncMock:
        async def _send_message(*args, **kwargs):
            self.markups.extend(markups_from_call(kwargs))
            return SimpleNamespace(message_id=len(self.markups) + 1, chat_id=0)

        async def _delete_message(*args, **kwargs):
            return None

        bot.send_message = AsyncMock(side_effect=_send_message)
        bot.delete_message = AsyncMock(side_effect=_delete_message)
        return bot

    def wrap_message(self, msg: MagicMock) -> MagicMock:
        async def _reply_text(*args, **kwargs):
            self.markups.extend(markups_from_call(kwargs))
            return SimpleNamespace(message_id=len(self.markups) + 1, chat_id=0)

        msg.reply_text = AsyncMock(side_effect=_reply_text)
        return msg

    def wrap_query(self, query: AsyncMock) -> AsyncMock:
        async def _edit_message_text(*args, **kwargs):
            self.markups.extend(markups_from_call(kwargs))
            return None

        async def _edit_message_reply_markup(*args, **kwargs):
            self.markups.extend(markups_from_call(kwargs))
            return None

        query.edit_message_text = AsyncMock(side_effect=_edit_message_text)
        query.edit_message_reply_markup = AsyncMock(
            side_effect=_edit_message_reply_markup
        )
        return query

    def note_pulse(self) -> None:
        self.pulsed_wizard = True

    def assert_turn(self, step: str) -> None:
        assert turn_has_escape_hatch(
            self.markups, pulsed_wizard_keyboard=self.pulsed_wizard
        ), (
            f"{step}: expected Cancel, Back, or menu keyboard; "
            f"got markups={self.markups!r}, pulsed_wizard={self.pulsed_wizard}"
        )
        self.markups.clear()
        self.pulsed_wizard = False


def _app(db, twitch=None):
    bot = AsyncMock()
    bot.id = 42
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


def _msg_update(user_id: int, text: str = "hi", capture: _BotCapture | None = None):
    update = MagicMock()
    update.callback_query = None
    update.effective_user = SimpleNamespace(id=user_id)
    update.effective_chat = SimpleNamespace(id=user_id)
    msg = MagicMock()
    msg.text = text
    msg.chat_id = user_id
    if capture is not None:
        capture.wrap_message(msg)
    else:
        msg.reply_text = AsyncMock()
    update.effective_message = msg
    return update


def _cb_update(user_id: int, data: str, capture: _BotCapture | None = None):
    query = AsyncMock()
    query.data = data
    query.answer = AsyncMock()
    query.from_user = SimpleNamespace(id=user_id)
    msg = MagicMock()
    msg.chat_id = user_id
    msg.message_id = 1
    if capture is not None:
        capture.wrap_query(query)
        capture.wrap_message(msg)
    else:
        query.edit_message_text = AsyncMock()
        query.edit_message_reply_markup = AsyncMock()
        msg.reply_text = AsyncMock()
    query.message = msg
    update = MagicMock()
    update.callback_query = query
    update.effective_user = SimpleNamespace(id=user_id)
    update.effective_message = MagicMock()
    update.effective_message.reply_text = AsyncMock()
    update.effective_message.text = ""
    update.effective_chat = SimpleNamespace(id=user_id)
    return update, query


def _check_submenu_reply_keyboards() -> None:
    for loc in SUPPORTED_LOCALES:
        cases = [
            ("subscriptions_menu", subscriptions_menu(loc)),
            ("other_menu", other_menu(loc)),
            ("settings_menu", settings_menu(loc, beta_enrolled=0, beta_total=0)),
            ("partner_menu", partner_menu(loc)),
            ("broadcast_menu", broadcast_menu(loc)),
            ("admin_menu", admin_menu(loc)),
            ("wizard_menu", wizard_menu(loc)),
            ("wizard_menu_no_back", wizard_menu(loc, back=False)),
        ]
        for name, markup in cases:
            assert markup_has_escape_hatch(markup), (
                f"{name} ({loc}) missing Cancel/Back/menu"
            )
        assert markup_has_escape_hatch(main_menu(loc)), (
            f"main_menu ({loc}) missing menu buttons"
        )


def _check_inline_wizard_keyboards() -> None:
    from handlers.wizard import _twitch_link_offer_keyboard
    from i18n import drops_catalog_keyboard

    for loc in SUPPORTED_LOCALES:
        cases = [
            ("alert_type_keyboard", alert_type_keyboard(loc, show_drops=True)),
            ("premium_gate_first", premium_gate_keyboard(loc, first_step=True)),
            ("premium_gate_later", premium_gate_keyboard(loc, first_step=False)),
            ("stream_schedule_confirm", stream_schedule_confirm_keyboard(loc)),
            ("stream_schedule_mode", stream_schedule_mode_keyboard(loc)),
            ("stream_schedule_vacation_auto", stream_schedule_vacation_auto_keyboard(loc)),
            ("stream_schedule_vacation_active", stream_schedule_vacation_active_keyboard(loc)),
            ("stream_schedule_duration", stream_schedule_duration_keyboard(loc)),
            ("admin_type", admin_type_keyboard(loc)),
            ("admin_audience", admin_other_audience_keyboard(loc)),
            ("ignored_words", ignored_words_keyboard(loc, has_words=False)),
            ("ignored_words_clear", ignored_words_keyboard(loc, has_words=True)),
            ("language_settings", language_keyboard(loc)),
            ("delete_all_confirm", delete_all_confirm_keyboard(loc)),
            ("template_typo", template_typo_keyboard(loc)),
            ("stored_typo_fix", stored_typo_fix_keyboard(loc)),
            ("watch_cats_nav", watch_cats_nav_keyboard(loc, has_cats=False)),
            ("watch_filters", watch_filters_keyboard(
                loc,
                want_tags=False,
                want_viewers=False,
                want_language=False,
                want_mature=False,
            )),
            ("watch_viewers", watch_viewers_keyboard(loc)),
            ("watch_lang", watch_lang_keyboard(loc)),
            ("watch_mature", watch_mature_keyboard(loc)),
            ("watch_tags", watch_tags_keyboard(loc)),
            ("watch_save", watch_save_keyboard(loc)),
            ("twitch_link_offer", _twitch_link_offer_keyboard(loc, "shroud")),
            ("premium_gift_plans", premium_gift_keyboard(loc, user_id=_FREE_UID)),
            (
                "drops_catalog",
                drops_catalog_keyboard(
                    loc,
                    [{"name": "Camp", "game_name": "Game", "id": "1"}],
                ),
            ),
        ]
        for name, markup in cases:
            assert markup_has_escape_hatch(markup), (
                f"{name} ({loc}) missing inline Cancel/Back/decline"
            )


async def _scenario_menus_and_wizards(db) -> None:
    from handlers.settings import open_other_menu, open_settings_menu
    from handlers.stream_schedule import start_stream_schedule
    from handlers.watch import start_watch_lucky

    cap = _BotCapture()

    application, bot = _app(db)
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("other", "ru"), cap)
    ctx = _ctx(application)
    await open_other_menu(update, ctx)
    cap.assert_turn("open_other_menu")

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("watch", "ru"), cap)
    ctx = _ctx(application)
    db.upsert_user(_FREE_UID)
    with (
        patch("handlers.watch.analytics.capture"),
        patch(
            "handlers.watch._fetch_lucky_watch_suggestions",
            new=AsyncMock(return_value=([], [], [])),
        ),
    ):
        await start_watch_lucky(update, ctx)
    cap.assert_turn("watch_lucky_other")

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("settings", "ru"), cap)
    ctx = _ctx(application)
    await open_settings_menu(update, ctx)
    cap.assert_turn("open_settings_menu")

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("new", "ru"), cap)
    ctx = _ctx(application)
    from handlers.wizard import _go_alert_type_prompt

    await _go_alert_type_prompt(update, ctx, "ru")
    cap.assert_turn("wizard_alert_type")

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("create_schedule", "ru"), cap)
    ctx = _ctx(application)
    with patch("handlers.stream_schedule.beta_features.is_enabled", return_value=False):
        await start_stream_schedule(update, ctx)
    cap.assert_turn("stream_schedule_confirm")

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("create_schedule", "ru"), cap)
    ctx = _ctx(application)
    with patch("handlers.stream_schedule.beta_features.is_enabled", return_value=True):
        await start_stream_schedule(update, ctx)
    cap.assert_turn("stream_schedule_mode")

    async def _pulse(bot, chat_id, lang, *, back=True):
        cap.note_pulse()

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("create_schedule", "ru"), cap)
    ctx = _ctx(application)
    with patch(
        "handlers.stream_schedule.beta_features.is_enabled", return_value=True
    ), patch("handlers.stream_schedule._pulse_wizard_keyboard", new=_pulse):
        from handlers.stream_schedule import stream_schedule_mode_callback

        await start_stream_schedule(update, ctx)
        cap.assert_turn("stream_schedule_mode")
        update, _query = _cb_update(_FREE_UID, "stream_sched:mode:week", cap)
        cap.wrap(bot)
        await stream_schedule_mode_callback(update, ctx)
        cap.assert_turn("stream_schedule_after_mode")


async def _scenario_wizard_channel_step(db) -> None:
    from handlers.wizard import _go_channel_prompt

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, "x", cap)
    ctx = _ctx(application, {"alert_type": "live"})
    await _go_channel_prompt(update, ctx, "ru")
    cap.assert_turn("wizard_channel")


def _seed_live_sub(db, owner_id: int) -> int:
    return db.add_subscription(
        owner_id=owner_id,
        twitch_username="streamer",
        twitch_user_id="100",
        message_template="live {streamer}",
        dest_type="dm",
        chat_id=owner_id,
        thread_id=None,
    )


async def _scenario_subscriptions(db) -> None:
    """§4 Мои подписки — list, no-share without beta, delete confirm No, cart, pause."""
    from i18n import wizard_menu as _wizard_menu
    from handlers.subscriptions import (
        edit_menu,
        list_subscriptions,
        on_list_delete,
        on_list_delete_confirm,
        open_cart_menu,
        open_subscriptions_menu,
        start_pause_notifications,
    )

    sub_id = _seed_live_sub(db, _FREE_UID)

    application, bot = _app(db)
    bot.get_me = AsyncMock(return_value=SimpleNamespace(username="testbot"))
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("manage", "ru"), cap)
    ctx = _ctx(application)
    with patch(
        "handlers.subscriptions.beta_features.is_enabled", return_value=False
    ):
        await open_subscriptions_menu(update, ctx)
    cap.assert_turn("subscriptions_list")

    application, bot = _app(db)
    bot.get_me = AsyncMock(return_value=SimpleNamespace(username="testbot"))
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("list", "ru"), cap)
    ctx = _ctx(application)
    with patch(
        "handlers.subscriptions.beta_features.is_enabled", return_value=False
    ):
        await list_subscriptions(update, ctx)
    inline_cbs = [
        (b.callback_data or "")
        for m in cap.markups
        if getattr(m, "inline_keyboard", None)
        for row in m.inline_keyboard
        for b in row
    ]
    assert any(cb.startswith("toggle:") for cb in inline_cbs)
    assert any(cb.startswith("edit:") for cb in inline_cbs)
    assert any(cb.startswith("list_del:") for cb in inline_cbs)
    assert not any(cb.startswith("share_show:") for cb in inline_cbs)
    cap.assert_turn("subscriptions_list_no_share")

    update, _query = _cb_update(_FREE_UID, f"list_del:{sub_id}", cap)
    ctx = _ctx(application, dict(ctx.user_data))
    await on_list_delete(update, ctx)
    cap.assert_turn("subscriptions_list_delete_confirm")

    update, query = _cb_update(_FREE_UID, f"list_del_no:{sub_id}", cap)
    bot.get_me = AsyncMock(return_value=SimpleNamespace(username="testbot"))
    with patch(
        "handlers.subscriptions.beta_features.is_enabled", return_value=False
    ):
        await on_list_delete_confirm(update, ctx)
    query.edit_message_text.assert_awaited()
    restored_cbs = [
        (b.callback_data or "")
        for row in (query.edit_message_text.call_args.kwargs.get("reply_markup")
                    or SimpleNamespace(inline_keyboard=[])).inline_keyboard
        for b in row
    ]
    assert any(cb.startswith("list_del:") for cb in restored_cbs)

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("cart", "ru"), cap)
    ctx = _ctx(application)
    with patch(
        "handlers.subscriptions.beta_features.is_enabled",
        side_effect=lambda _db, _uid, feat: feat == "deleted-subscriptions-cart",
    ), patch(
        "handlers.subscriptions.prem.deleted_subscriptions_cart_days",
        return_value=10,
    ):
        await open_cart_menu(update, ctx)
    cap.assert_turn("subscriptions_cart")

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("pause_notifications", "ru"), cap)
    ctx = _ctx(application)
    with patch(
        "handlers.subscriptions._pause_notifications_enabled", return_value=True
    ), patch(
        "handlers.subscriptions._wizard",
        side_effect=lambda lang, back=True: (
            cap.note_pulse(),
            _wizard_menu(lang, back=back),
        )[1],
    ):
        state = await start_pause_notifications(update, ctx)
    assert state is not None
    cap.assert_turn("subscriptions_pause")

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("edit", "ru"), cap)
    ctx = _ctx(application)
    await edit_menu(update, ctx)
    cap.assert_turn("subscriptions_edit")
    assert sub_id > 0


async def _scenario_settings_and_partner(db) -> None:
    from handlers.partner import open_partner_menu
    from handlers.settings import start_ignored_words

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("partner", "ru"), cap)
    ctx = _ctx(application)
    await open_partner_menu(update, ctx)
    cap.assert_turn("partner_menu")

    async def _pulse(bot, chat_id, lang, *, back=True):
        cap.note_pulse()

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("ignored_words", "ru"), cap)
    ctx = _ctx(application)
    with patch(
        "handlers.settings.prem.has_feature", new=AsyncMock(return_value=True)
    ), patch("handlers.settings._pulse_wizard_keyboard", new=_pulse):
        await start_ignored_words(update, ctx)
    cap.assert_turn("settings_ignored_words")


async def _scenario_admin_broadcast(db) -> None:
    from bot import open_admin_menu
    from handlers.broadcast import admin_broadcast_start, admin_select_type

    db.upsert_user(_ADMIN_UID)
    db.set_user_locale(_ADMIN_UID, "ru")

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_ADMIN_UID, btn("admin", "ru"), cap)
    ctx = _ctx(application)
    with patch("bot._is_admin", return_value=True), patch(
        "bot.demo_mode.is_active", return_value=False
    ):
        await open_admin_menu(update, ctx)
    cap.assert_turn("admin_menu")

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_ADMIN_UID, btn("broadcast_new", "ru"), cap)
    ctx = _ctx(application)
    with patch("handlers.broadcast._can_use_admin_tools", return_value=True):
        await admin_broadcast_start(update, ctx)
    cap.assert_turn("admin_broadcast_type")

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update, _query = _cb_update(_ADMIN_UID, "admin_type:bot_update", cap)
    ctx = _ctx(application)
    with patch("handlers.broadcast._is_admin", return_value=True):
        await admin_select_type(update, ctx)
    cap.assert_turn("admin_broadcast_text")

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update, _query = _cb_update(_ADMIN_UID, "admin_type:other", cap)
    ctx = _ctx(application, {"admin_msg_type": "other"})
    with patch("handlers.broadcast._is_admin", return_value=True):
        await admin_select_type(update, ctx)
    cap.assert_turn("admin_broadcast_audience")


async def _scenario_feedback(db) -> None:
    from bot import report_problem

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("feedback", "ru"), cap)
    ctx = _ctx(application)
    await report_problem(update, ctx)
    cap.assert_turn("feedback")


async def _scenario_wizard_deep(db) -> None:
    from handlers.wizard import (
        _go_alert_type_prompt,
        _go_link_preview_prompt,
        _go_template_prompt,
        _prompt_dest_step,
        receive_alert_type,
        receive_channel,
    )

    twitch = MagicMock()
    twitch.parse_username.return_value = "streamer"
    twitch.get_user.return_value = {"id": "100", "login": "streamer", "display_name": "S"}
    twitch.is_twitch_url.return_value = False

    application, bot = _app(db, twitch=twitch)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("new", "ru"), cap)
    ctx = _ctx(application)
    from handlers.wizard import _go_alert_type_prompt

    await _go_alert_type_prompt(update, ctx, "ru")
    update, _query = _cb_update(_FREE_UID, "alert_type:live", cap)
    cap.wrap(bot)
    with patch("handlers.wizard.prem.has_feature", new=AsyncMock(return_value=True)):
        await receive_alert_type(update, ctx)
    update = _msg_update(_FREE_UID, "streamer", cap)
    cap.wrap(bot)
    with patch("handlers.wizard.prem.has_feature", new=AsyncMock(return_value=True)):
        await receive_channel(update, ctx)
    update = _msg_update(_FREE_UID, "tpl", cap)
    cap.wrap(bot)
    await _go_template_prompt(update, ctx, "ru")
    update = _msg_update(_FREE_UID, "x", cap)
    cap.wrap(bot)
    ctx.user_data.setdefault("alert_type", "live")
    await _prompt_dest_step(update, ctx, "ru", edit=False)
    update = _msg_update(_FREE_UID, "x", cap)
    cap.wrap(bot)
    await _go_link_preview_prompt(update, ctx, "ru")
    cap.assert_turn("wizard_deep_dest_link_preview")


async def _scenario_import(db) -> None:
    from handlers.subscriptions import on_import_oauth_manual, start_twitch_import

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("import_twitch", "ru"), cap)
    ctx = _ctx(application)
    with patch("config.twitch_oauth_redirect_uri", return_value=""):
        await start_twitch_import(update, ctx)
    cap.assert_turn("import_oauth_unavailable")

    twitch = MagicMock()
    twitch.build_authorize_url.return_value = "https://id.twitch.tv/oauth2/authorize?x=1"
    application, bot = _app(db, twitch=twitch)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("import_twitch", "ru"), cap)
    ctx = _ctx(application)
    with patch(
        "config.twitch_oauth_redirect_uri",
        return_value="https://example.com/oauth/callback",
    ), patch("health.create_oauth_state", return_value="oauth-state"):
        await start_twitch_import(update, ctx)
    cap.assert_turn("import_oauth_success")

    # Saved OAuth → manual import + Cancel (no authorize URL).
    db.upsert_user(_FREE_UID)
    db.upsert_twitch_sync(
        owner_id=_FREE_UID,
        twitch_user_id="tw1",
        refresh_token="rtok",
        period_days=7,
        next_sync_at="2099-01-01T00:00:00+00:00",
    )
    application, bot = _app(db, twitch=twitch)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("import_twitch", "ru"), cap)
    ctx = _ctx(application)
    with patch(
        "config.twitch_oauth_redirect_uri",
        return_value="https://example.com/oauth/callback",
    ):
        await start_twitch_import(update, ctx)
    found_manual = any(
        getattr(key, "callback_data", None) == "import_oauth:manual"
        for markup in cap.markups
        for row in (getattr(markup, "inline_keyboard", None) or [])
        for key in row
    )
    assert found_manual, "expected import_oauth:manual when refresh token is saved"
    cap.assert_turn("import_oauth_manual")

    twitch.refresh_user_token.return_value = {
        "access_token": "at",
        "refresh_token": "rtok2",
    }
    twitch.get_followed_channels.return_value = [
        {"broadcaster_id": "99", "broadcaster_login": "impchan"}
    ]
    application, bot = _app(db, twitch=twitch)
    query = AsyncMock()
    query.data = "import_oauth:manual"
    query.from_user = SimpleNamespace(id=_FREE_UID)
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=_FREE_UID),
        effective_message=None,
    )
    ctx = _ctx(application)
    with patch(
        "handlers.subscriptions._deliver_import_result", new=AsyncMock()
    ) as deliver:
        await on_import_oauth_manual(update, ctx)
    query.answer.assert_awaited()
    deliver.assert_awaited()
    subs = [
        s
        for s in db.get_subscriptions_by_owner(_FREE_UID)
        if (s.twitch_username or "").lower() == "impchan"
    ]
    assert len(subs) == 1
    assert subs[0].enabled is False


async def _scenario_alert_history(db) -> None:
    from handlers.alert_history import on_alert_history_page, show_alert_history

    for i in range(55):
        db.add_alert_history(
            _FREE_UID,
            subscription_id=None,
            twitch_username=f"ch{i}",
            alert_type="live",
            message_text="history line " + ("x" * 72),
        )

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("alert_history", "ru"), cap)
    ctx = _ctx(application)
    with patch("handlers.alert_history.prem.has_feature", new=AsyncMock(return_value=False)):
        await show_alert_history(update, ctx)
    cap.assert_turn("alert_history_page0")
    pages = ctx.user_data.get("alert_history_pages") or []
    if len(pages) > 1:
        update, _query = _cb_update(_FREE_UID, "alert_history:page:1", cap)
        cap.wrap(bot)
        await on_alert_history_page(update, ctx)
        cap.assert_turn("alert_history_page1")


async def _scenario_schedule_publish_chain(db) -> None:
    from datetime import date

    from handlers.stream_schedule import (
        _finish_stream_schedule,
        stream_schedule_duration_callback,
        stream_schedule_publish_callback,
        stream_schedule_tz,
    )

    async def _pulse(bot, chat_id, lang, *, back=True):
        cap.note_pulse()

    schedule_ctx = {
        "stream_schedule_entries": [
            {"date": date(2026, 8, 27), "time": "18:00", "game": "Just Chatting"}
        ],
    }

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, "x", cap)
    ctx = _ctx(application, dict(schedule_ctx))
    with patch("handlers.stream_schedule._pulse_wizard_keyboard", new=_pulse):
        await _finish_stream_schedule(update, ctx, "ru")
    cap.assert_turn("stream_schedule_publish")

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    ctx = _ctx(application, dict(schedule_ctx))
    update, _query = _cb_update(_FREE_UID, "stream_sched:publish:1", cap)
    cap.wrap(bot)
    with patch(
        "handlers.stream_schedule.prem.has_feature", new=AsyncMock(return_value=True)
    ), patch("handlers.stream_schedule._pulse_wizard_keyboard", new=_pulse):
        await stream_schedule_publish_callback(update, ctx)
    cap.assert_turn("stream_schedule_tz")

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    ctx = _ctx(application, {**schedule_ctx, "stream_schedule_tz_resume": "duration"})
    update = _msg_update(_FREE_UID, "+3", cap)
    cap.wrap(bot)
    with patch("handlers.stream_schedule._pulse_wizard_keyboard", new=_pulse):
        await stream_schedule_tz(update, ctx)
    cap.assert_turn("stream_schedule_duration")

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    ctx = _ctx(application, dict(schedule_ctx))
    db.set_schedule_utc_offset_minutes(_FREE_UID, 180)
    update, _query = _cb_update(_FREE_UID, "stream_sched:duration:2", cap)
    cap.wrap(bot)
    with patch("config.twitch_oauth_redirect_uri", return_value=""):
        await stream_schedule_duration_callback(update, ctx)
    cap.assert_turn("stream_schedule_publish_auth_unavailable")


async def _wizard_to_template_step(cap: _BotCapture, application, ctx):
    from handlers.wizard import receive_alert_type, receive_channel

    twitch = application.bot_data["twitch"]
    update, _query = _cb_update(_FREE_UID, "alert_type:live", cap)
    cap.wrap(application.bot)
    with patch("handlers.wizard.prem.has_feature", new=AsyncMock(return_value=True)):
        await receive_alert_type(update, ctx)
    update = _msg_update(_FREE_UID, "streamer", cap)
    cap.wrap(application.bot)
    with patch("handlers.wizard.prem.has_feature", new=AsyncMock(return_value=True)):
        await receive_channel(update, ctx)


async def _scenario_wizard_template_typo(db) -> None:
    """§2 wizard — template typo prompt; Yes auto-fixes, No keeps original."""
    from handlers.wizard import (
        receive_image_ask,
        receive_template,
        receive_template_typo_confirm,
    )

    twitch = MagicMock()
    twitch.parse_username.return_value = "streamer"
    twitch.get_user.return_value = {"id": "100", "login": "streamer", "display_name": "S"}
    twitch.is_twitch_url.return_value = False

    application, bot = _app(db, twitch=twitch)
    cap = _BotCapture()
    cap.wrap(bot)
    ctx = _ctx(application)
    await _wizard_to_template_step(cap, application, ctx)

    typo_template = "{username} в эфире!\n{name}\nКатегория: {game)"
    update = _msg_update(_FREE_UID, typo_template, cap)
    cap.wrap(bot)
    with patch(
        "handlers.wizard.prem.advanced_mode_on", new=AsyncMock(return_value=False)
    ), patch("handlers.wizard.prem.has_feature", new=AsyncMock(return_value=True)):
        await receive_template(update, ctx)
    assert ctx.user_data.get("pending_template") == typo_template
    assert "message_template" not in ctx.user_data
    cap.assert_turn("wizard_template_typo_prompt")

    fixed_template = "{username} в эфире!\n{name}\nКатегория: {game}"
    update, _query = _cb_update(_FREE_UID, "template_typo:1", cap)
    cap.wrap(bot)
    with patch(
        "handlers.wizard.prem.advanced_mode_on", new=AsyncMock(return_value=False)
    ):
        await receive_template_typo_confirm(update, ctx)
    assert ctx.user_data.get("message_template") == fixed_template
    assert ctx.user_data.get("pending_template") is None
    bot.send_message.assert_awaited()
    fixed_calls = [
        c
        for c in bot.send_message.call_args_list
        if c.args and c.args[0] == _FREE_UID and "исправлен" in str(c.args[1]).lower()
    ]
    assert fixed_calls, "expected template_typo_fixed message after Yes"

    application, bot = _app(db, twitch=twitch)
    cap = _BotCapture()
    cap.wrap(bot)
    ctx = _ctx(application)
    await _wizard_to_template_step(cap, application, ctx)
    update = _msg_update(_FREE_UID, typo_template, cap)
    cap.wrap(bot)
    with patch(
        "handlers.wizard.prem.advanced_mode_on", new=AsyncMock(return_value=False)
    ), patch("handlers.wizard.prem.has_feature", new=AsyncMock(return_value=True)):
        await receive_template(update, ctx)
    cap.assert_turn("wizard_template_typo_prompt_no")
    update, _query = _cb_update(_FREE_UID, "template_typo:0", cap)
    cap.wrap(bot)
    with patch(
        "handlers.wizard.prem.advanced_mode_on", new=AsyncMock(return_value=False)
    ):
        await receive_template_typo_confirm(update, ctx)
    assert ctx.user_data.get("message_template") == typo_template
    from handlers.wizard import receive_advanced_options_next

    update, _query = _cb_update(_FREE_UID, "advopt:next", cap)
    cap.wrap(bot)
    with patch(
        "handlers.wizard.prem.has_feature", new=AsyncMock(return_value=False)
    ):
        await receive_advanced_options_next(update, ctx)
    update, _query = _cb_update(_FREE_UID, "dest:dm", cap)
    cap.wrap(bot)
    with patch(
        "handlers.wizard.prem.can_enable_more_async", new=AsyncMock(return_value=True)
    ):
        from handlers.wizard import receive_dest_type

        await receive_dest_type(update, ctx)
    cap.assert_turn("wizard_template_typo_no_dest")


async def _scenario_wizard_finish(db) -> None:
    from handlers.wizard import (
        receive_alert_type,
        receive_channel,
        receive_dest_type,
        receive_template,
        receive_advanced_options_next,
    )

    twitch = MagicMock()
    twitch.parse_username.return_value = "streamer"
    twitch.get_user.return_value = {"id": "100", "login": "streamer", "display_name": "S"}
    twitch.is_twitch_url.return_value = False

    application, bot = _app(db, twitch=twitch)
    cap = _BotCapture()
    cap.wrap(bot)
    ctx = _ctx(application)
    update, _query = _cb_update(_FREE_UID, "alert_type:live", cap)
    cap.wrap(bot)
    with patch("handlers.wizard.prem.has_feature", new=AsyncMock(return_value=True)):
        await receive_alert_type(update, ctx)
    update = _msg_update(_FREE_UID, "streamer", cap)
    cap.wrap(bot)
    with patch("handlers.wizard.prem.has_feature", new=AsyncMock(return_value=True)):
        await receive_channel(update, ctx)
    update = _msg_update(_FREE_UID, "Live {streamer}", cap)
    cap.wrap(bot)
    with patch("handlers.wizard.prem.has_feature", new=AsyncMock(return_value=True)):
        await receive_template(update, ctx)
    update, _query = _cb_update(_FREE_UID, "advopt:next", cap)
    cap.wrap(bot)
    with patch("handlers.wizard.prem.has_feature", new=AsyncMock(return_value=False)):
        await receive_advanced_options_next(update, ctx)
    update, _query = _cb_update(_FREE_UID, "dest:dm", cap)
    cap.wrap(bot)
    with patch(
        "handlers.wizard.prem.can_enable_more_async", new=AsyncMock(return_value=True)
    ):
        await receive_dest_type(update, ctx)
    cap.assert_turn("wizard_finish")


async def _scenario_wizard_custom_buttons(db) -> None:
    """§2 custom URL buttons manager — Cancel/Back on the list screen."""
    from handlers.wizard import _go_custom_buttons_step

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, "x", cap)
    ctx = _ctx(application)
    ctx.user_data.update(
        {
            "advanced_options_done": True,
            "adv_want_buttons": True,
            "twitch_username": "streamer",
            "custom_buttons_list": [
                {"text": "A", "url": "https://example.com/a"},
                {"text": "B", "url": "https://example.com/b"},
            ],
            "custom_buttons": '[{"text":"A","url":"https://example.com/a"},'
            '{"text":"B","url":"https://example.com/b"}]',
        }
    )
    with patch(
        "handlers.custom_buttons_ui.maybe_entitled_custom_buttons",
        new=AsyncMock(return_value=True),
    ):
        await _go_custom_buttons_step(update, ctx, "ru")
    cap.assert_turn("wizard_custom_buttons")


async def _scenario_wizard_drops_game(db) -> None:
    """§2.1 Drops catalog — Cancel/Back on wizard Reply keyboard."""
    from handlers.wizard import _go_drops_catalog_step

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, "x", cap)
    ctx = _ctx(application)
    ctx.user_data["alert_type"] = "drops"
    with (
        patch("handlers.drops.drops_feature_available", return_value=True),
        patch(
            "handlers.wizard.prem.has_feature",
            new=AsyncMock(return_value=True),
        ),
        patch("handlers.drops._access_token_for_owner", return_value="tok"),
        patch("handlers.drops.list_active_drop_campaigns", return_value=[]),
    ):
        await _go_drops_catalog_step(update, ctx, "ru")
    cap.assert_turn("wizard_drops_catalog")


async def _scenario_wizard_game_alert(db) -> None:
    """§2/§6 Game alert — alert_type:game → watch categories with Cancel."""
    from handlers.wizard import receive_alert_type

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update, query = _cb_update(_FREE_UID, "alert_type:game", cap)
    update.effective_message = query.message
    ctx = _ctx(application)
    db.upsert_user(_FREE_UID)
    with (
        patch("handlers.watch.analytics.capture"),
        patch("handlers.watch._live_promo_streams", return_value=[]),
    ):
        await receive_alert_type(update, ctx)
    cap.assert_turn("wizard_game_alert_categories")


async def _scenario_wizard_extras_checkboxes(db) -> None:
    """§2 Extras checkbox values apply without a second Yes/No (delete/delay/repeat)."""
    from handlers.wizard import (
        _go_delay_prompt,
        _prompt_delete_old,
        _prompt_repeat_step,
        _wz,
    )
    from telegram.ext import ConversationHandler

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    # Prior wizard Reply stays visible in real UX (dest step).
    await bot.send_message(_FREE_UID, "·", reply_markup=wizard_menu("ru"))
    update = _msg_update(_FREE_UID, "x", cap)
    ctx = _ctx(application)
    ctx.user_data.update(
        {
            "advanced_options_done": True,
            "adv_want_delete": True,
            "twitch_username": "streamer",
            "twitch_user_id": "100",
            "alert_type": "live",
            "dest_type": "channel",
            "pending_chat_id": -1001,
            "pending_thread_id": None,
            "message_template": "hi",
        }
    )
    with patch(
        "handlers.wizard.prem.advanced_mode_on", new=AsyncMock(return_value=True)
    ), patch(
        "handlers.wizard.prem.has_feature", new=AsyncMock(return_value=True)
    ):
        state = await _prompt_delete_old(update, ctx, "ru")
    assert state == _wz()["DELETE_FAIL_NOTIFY"]
    assert ctx.user_data.get("delete_previous") is True
    inline_cbs = [
        b.callback_data or ""
        for m in cap.markups
        if getattr(m, "inline_keyboard", None)
        for row in m.inline_keyboard
        for b in row
    ]
    assert not any(cb.startswith("delete_old:") for cb in inline_cbs)
    assert any(cb.startswith("delete_fail:") for cb in inline_cbs)
    cap.assert_turn("wizard_delete_from_extras")

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    await bot.send_message(_FREE_UID, "·", reply_markup=wizard_menu("ru"))
    update = _msg_update(_FREE_UID, "x", cap)
    ctx = _ctx(application)
    ctx.user_data.update(
        {
            "advanced_options_done": True,
            "adv_want_delay": True,
            "twitch_username": "streamer",
            "alert_type": "live",
        }
    )
    with patch(
        "handlers.wizard.prem.advanced_mode_on", new=AsyncMock(return_value=True)
    ), patch(
        "handlers.wizard.prem.has_feature", new=AsyncMock(return_value=True)
    ):
        state = await _go_delay_prompt(update, ctx, "ru")
    assert state == _wz()["DELAY_MINUTES"]
    assert not any(
        (b.callback_data or "").startswith("delay_send:")
        for m in cap.markups
        if getattr(m, "inline_keyboard", None)
        for row in m.inline_keyboard
        for b in row
    )
    cap.assert_turn("wizard_delay_from_extras")

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    await bot.send_message(_FREE_UID, "·", reply_markup=wizard_menu("ru"))
    update = _msg_update(_FREE_UID, "x", cap)
    ctx = _ctx(application)
    ctx.user_data.update(
        {
            "advanced_options_done": True,
            "adv_want_repeat": True,
            "twitch_username": "streamer",
            "alert_type": "live",
        }
    )
    with patch(
        "handlers.wizard.prem.advanced_mode_on", new=AsyncMock(return_value=True)
    ), patch(
        "handlers.wizard.prem.has_feature", new=AsyncMock(return_value=True)
    ):
        state = await _prompt_repeat_step(update, ctx, "ru")
    assert state == _wz()["REPEAT_MUTE_MINUTES"]
    assert not any(
        (b.callback_data or "").startswith("repeat:")
        for m in cap.markups
        if getattr(m, "inline_keyboard", None)
        for row in m.inline_keyboard
        for b in row
    )
    cap.assert_turn("wizard_repeat_from_extras")
    assert ConversationHandler.END != state


async def _scenario_wizard_image_ask(db) -> None:
    """§2.6 Image step — game cover checkbox always shown; Skip / wizard nav escape."""
    from handlers.wizard import _go_image_ask_prompt, receive_image_ask, _wz
    from telegram.ext import ConversationHandler
    from twitch import GAME_COVER_IMAGE_ID, is_game_cover_image

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    await bot.send_message(_FREE_UID, "·", reply_markup=wizard_menu("ru"))
    update = _msg_update(_FREE_UID, "x", cap)
    ctx = _ctx(application)
    # No {game} in template — cover option must still appear.
    ctx.user_data["message_template"] = "hi {username}"
    state = await _go_image_ask_prompt(update, ctx, "ru")
    assert state == _wz()["IMAGE_ASK"]
    labels = [
        b.text
        for m in cap.markups
        if getattr(m, "inline_keyboard", None)
        for row in m.inline_keyboard
        for b in row
    ]
    assert any(lab.startswith("⬜️ ") and "обложк" in lab.lower() for lab in labels)
    assert any("своё" in lab.lower() for lab in labels)
    cap.assert_turn("wizard_image_ask")

    update, _query = _cb_update(_FREE_UID, "image_ask:game_cover", cap)
    cap.wrap(bot)
    state = await receive_image_ask(update, ctx)
    assert state == _wz()["IMAGE_ASK"]
    assert is_game_cover_image(ctx.user_data.get("image_file_id"))
    assert ctx.user_data.get("image_file_id") == GAME_COVER_IMAGE_ID
    toggled = [
        b.text
        for m in cap.markups
        if getattr(m, "inline_keyboard", None)
        for row in m.inline_keyboard
        for b in row
    ]
    assert any(lab.startswith("✅ ") and "обложк" in lab.lower() for lab in toggled)

    update, _query = _cb_update(_FREE_UID, "image_ask:skip", cap)
    cap.wrap(bot)
    with patch(
        "handlers.wizard._go_after_image_step",
        new=AsyncMock(return_value=ConversationHandler.END),
    ) as after:
        state = await receive_image_ask(update, ctx)
    after.assert_awaited()
    assert is_game_cover_image(ctx.user_data.get("image_file_id"))


async def _scenario_subscriptions_edit_checkboxes(db) -> None:
    """§4 edit menu — boolean settings toggle via checkbox (no Yes/No)."""
    from handlers.subscriptions import edit_menu, on_edit_bool_menu, on_edit_pick

    sub_id = db.add_subscription(
        owner_id=_FREE_UID,
        twitch_username="streamer",
        twitch_user_id="100",
        message_template="hi https://twitch.tv/streamer",
        dest_type="channel",
        chat_id=-1001,
        thread_id=None,
        disable_link_preview=True,
        attach_chat_button=False,
        delete_previous=False,
        strip_name_mentions=False,
    )
    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("edit", "ru"), cap)
    ctx = _ctx(application)
    await edit_menu(update, ctx)
    update, _query = _cb_update(_FREE_UID, f"edit:{sub_id}", cap)
    with patch(
        "handlers.subscriptions.prem.advanced_mode_on",
        new=AsyncMock(return_value=True),
    ):
        await on_edit_pick(update, ctx)

    for field in ("strip", "chat_button", "preview", "delete_old"):
        update, _query = _cb_update(_FREE_UID, f"edit_f:{sub_id}:{field}", cap)
        with patch(
            "handlers.subscriptions.prem.advanced_mode_on",
            new=AsyncMock(return_value=True),
        ), patch(
            "handlers.subscriptions.prem.has_feature",
            new=AsyncMock(return_value=True),
        ):
            await on_edit_bool_menu(update, ctx)
        edit_cbs = [
            b.callback_data or ""
            for m in cap.markups
            if getattr(m, "inline_keyboard", None)
            for row in m.inline_keyboard
            for b in row
        ]
        assert any(cb.startswith(f"edit_f:{sub_id}:") for cb in edit_cbs)
        assert not any(cb.startswith(f"edit_set:{sub_id}:") for cb in edit_cbs)

    update, _query = _cb_update(_FREE_UID, f"edit_f:{sub_id}:delete_fail", cap)
    with patch(
        "handlers.subscriptions.prem.advanced_mode_on",
        new=AsyncMock(return_value=True),
    ), patch(
        "handlers.subscriptions.prem.has_feature",
        new=AsyncMock(return_value=True),
    ):
        await on_edit_bool_menu(update, ctx)
    assert not any(
        (b.callback_data or "").startswith(f"edit_set:{sub_id}:")
        for m in cap.markups
        if getattr(m, "inline_keyboard", None)
        for row in m.inline_keyboard
        for b in row
    )

    sub = db.get_subscription(sub_id, _FREE_UID)
    assert sub is not None
    assert sub.strip_name_mentions is True
    assert sub.attach_chat_button is True
    assert sub.disable_link_preview is True
    assert sub.delete_previous is True
    assert sub.notify_delete_fail is True
    cap.assert_turn("subscriptions_edit_checkboxes")


async def _scenario_subscriptions_delete(db) -> None:
    """§4 delete flow — pick, delete-all confirm, No returns to pick."""
    from handlers.subscriptions import (
        delete_menu,
        on_delete_all,
        on_delete_all_confirm,
    )

    _seed_live_sub(db, _FREE_UID)

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("delete", "ru"), cap)
    ctx = _ctx(application)
    await delete_menu(update, ctx)
    cap.assert_turn("subscriptions_delete_pick")

    update, _query = _cb_update(_FREE_UID, "delete_all", cap)
    ctx = _ctx(application, dict(ctx.user_data))
    await on_delete_all(update, ctx)
    cap.assert_turn("subscriptions_delete_all_confirm")

    update, query = _cb_update(_FREE_UID, "delete_all:no", cap)
    await on_delete_all_confirm(update, ctx)
    query.edit_message_text.assert_awaited()
    markup = query.edit_message_text.call_args.kwargs.get("reply_markup")
    assert markup is not None
    labels = [btn.text for row in markup.inline_keyboard for btn in row]
    assert any("Удалить все" in text for text in labels)


async def _scenario_subscriptions_edit_pick(db) -> None:
    from handlers.subscriptions import edit_menu, on_edit_pick

    sub_id = _seed_live_sub(db, _FREE_UID)
    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("edit", "ru"), cap)
    ctx = _ctx(application)
    await edit_menu(update, ctx)
    update, _query = _cb_update(_FREE_UID, f"edit:{sub_id}", cap)
    cap.wrap(bot)
    with patch("handlers.subscriptions.prem.advanced_mode_on", new=AsyncMock(return_value=False)):
        await on_edit_pick(update, ctx)
    cap.assert_turn("subscriptions_edit_pick")


async def _scenario_subscriptions_edit_type_copy(db) -> None:
    """§4 edit menu — change type / copy+change with Cancel on type pick."""
    from handlers.subscriptions import (
        edit_menu,
        on_edit_change_type_click,
        on_edit_copy_change_click,
        on_edit_pick,
        on_edit_type_pick_cancel,
    )

    sub_id = _seed_live_sub(db, _FREE_UID)
    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("edit", "ru"), cap)
    ctx = _ctx(application)
    await edit_menu(update, ctx)
    update, _query = _cb_update(_FREE_UID, f"edit:{sub_id}", cap)
    cap.wrap(bot)
    with patch("handlers.subscriptions.prem.advanced_mode_on", new=AsyncMock(return_value=True)):
        await on_edit_pick(update, ctx)
    cap.assert_turn("subscriptions_edit_menu")

    update, _query = _cb_update(_FREE_UID, f"edit_f:{sub_id}:change_type", cap)
    await on_edit_change_type_click(update, ctx)
    cap.assert_turn("subscriptions_edit_change_type_pick")

    update, _query = _cb_update(_FREE_UID, f"edit_type_pick_cancel:change:{sub_id}", cap)
    await on_edit_type_pick_cancel(update, ctx)
    _query.edit_message_text.assert_awaited()

    update, _query = _cb_update(_FREE_UID, f"edit_f:{sub_id}:copy_change", cap)
    await on_edit_copy_change_click(update, ctx)
    cap.assert_turn("subscriptions_edit_copy_change_pick")

    update, _query = _cb_update(_FREE_UID, f"edit_type_pick_cancel:copy:{sub_id}", cap)
    await on_edit_type_pick_cancel(update, ctx)
    _query.edit_message_text.assert_awaited()


async def _scenario_share_alert_offer(db) -> None:
    """§1 deep link share — first start skips demo; decline; accept; dup prompt."""
    from bot import LANG_SELECT, receive_language, start as bot_start
    from db.models import _subscription_cart_snapshot
    from handlers.subscriptions import (
        offer_shared_alert,
        on_share_accept,
        on_share_decline,
        on_share_dup_continue,
    )
    from i18n import t as tr

    sub_id = _seed_live_sub(db, _FREE_UID)
    sub = db.get_subscription(sub_id, _FREE_UID)
    assert sub is not None
    token = db.ensure_alert_share_token(
        _FREE_UID, sub_id, _subscription_cart_snapshot(sub)
    )

    # First /start via share_* → language pick; after lang — no demo seed + share offer.
    newbie = _FREE_UID + 11
    assert not db.user_exists(newbie)
    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(newbie, "/start", cap)
    ctx = _ctx(application)
    ctx.args = [f"share_{token}"]
    with patch("bot.sync_stream_chat_menu_button", new=AsyncMock()):
        state = await bot_start(update, ctx)
    assert state == LANG_SELECT
    assert ctx.user_data.get("pending_share_token") == token
    assert ctx.user_data.get("first_welcome") is True
    assert db.user_exists(newbie)
    assert db.get_subscriptions_by_owner(newbie) == []

    update, _query = _cb_update(newbie, "lang:ru", cap)
    with patch(
        "bot._ensure_welcome_premium_channel_subscription",
        new=AsyncMock(
            side_effect=AssertionError("demo seed must not run on share first start")
        ),
    ), patch("bot.sync_stream_chat_menu_button", new=AsyncMock()), patch(
        "bot.offer_shared_alert", new=AsyncMock()
    ) as offer_mock:
        await receive_language(update, ctx)
    offer_mock.assert_awaited_once()
    assert offer_mock.await_args.args[-1] == token
    assert db.get_subscriptions_by_owner(newbie) == []
    assert "pending_share_token" not in ctx.user_data

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    await offer_shared_alert(bot, db, _FREE_UID, "ru", token)
    cap.assert_turn("share_offer")

    update, _query = _cb_update(_FREE_UID, "share_decline", cap)
    ctx = _ctx(application)
    await on_share_decline(update, ctx)
    _query.edit_message_text.assert_awaited()

    # Fresh recipient: first accept creates exactly one sub.
    stranger = _FREE_UID + 9
    db.upsert_user(stranger)
    db.set_user_locale(stranger, "ru")
    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update, query = _cb_update(stranger, f"share_accept:{token}", cap)
    ctx = _ctx(application)
    with patch(
        "handlers.subscriptions.prem.may_enable_subscription_async",
        new=AsyncMock(return_value=True),
    ):
        await on_share_accept(update, ctx)
    assert len(db.get_subscriptions_by_owner(stranger)) == 1
    sent = [
        (c.args[1] if len(c.args) > 1 else c.kwargs.get("text") or "")
        for c in bot.send_message.await_args_list
    ]
    assert any("создано" in str(t) for t in sent)
    assert query.edit_message_text.await_args.args[0] == "✓"

    # Same link again → channel_dup_prompt, no second row.
    update, query = _cb_update(stranger, f"share_accept:{token}", cap)
    with patch(
        "handlers.subscriptions.prem.may_enable_subscription_async",
        new=AsyncMock(return_value=True),
    ):
        await on_share_accept(update, ctx)
    assert len(db.get_subscriptions_by_owner(stranger)) == 1
    assert query.edit_message_text.await_args.args[0] == tr(
        "channel_dup_prompt", "ru"
    )
    markup = query.edit_message_text.await_args.kwargs.get("reply_markup")
    assert markup is not None
    cbs = {
        cell.callback_data
        for row in markup.inline_keyboard
        for cell in row
        if cell.callback_data
    }
    assert f"share_dup:edit:{db.get_subscriptions_by_owner(stranger)[0].id}" in cbs
    assert f"share_dup:continue:{token}" in cbs

    # Continue still allowed (same as wizard) — creates one more.
    update, query = _cb_update(stranger, f"share_dup:continue:{token}", cap)
    with patch(
        "handlers.subscriptions.prem.may_enable_subscription_async",
        new=AsyncMock(return_value=True),
    ):
        await on_share_dup_continue(update, ctx)
    assert len(db.get_subscriptions_by_owner(stranger)) == 2


async def _scenario_premium_gift(db) -> None:
    """§ Premium gift (beta): plan screen Back; customize Skip; redeem decline."""
    from handlers.premium_gift import (
        maybe_offer_pending_gift,
        on_gift_decline,
        on_gift_skip_img,
        on_gift_skip_msg,
        start_gift_customize,
    )
    from i18n import premium_gift_keyboard
    from premium_handlers import on_premium_callback

    buyer = _FREE_UID + 21
    db.upsert_user(buyer)
    db.set_user_locale(buyer, "ru")

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update, query = _cb_update(buyer, "premium:gift", cap)
    ctx = _ctx(application)
    with patch("config.show_premium_ui", return_value=True), patch(
        "handlers.premium_gift.gift_enabled", return_value=True
    ):
        await on_premium_callback(update, ctx)
    query.edit_message_text.assert_awaited()
    markup = query.edit_message_text.await_args.kwargs.get("reply_markup")
    assert markup is not None
    assert markup_has_escape_hatch(markup), "premium gift plans missing Back"
    cbs = {
        cell.callback_data
        for row in markup.inline_keyboard
        for cell in row
        if cell.callback_data
    }
    assert "premium:feat_back" in cbs
    assert "premium:gift_month" in cbs
    assert markup_has_escape_hatch(premium_gift_keyboard("ru", user_id=buyer))

    gift = db.create_premium_gift(
        buyer_id=buyer, kind="month", charge_id="stx_flow_gift", stars=100
    )
    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    bot.get_me = AsyncMock(return_value=SimpleNamespace(username="testbot"))
    update = _msg_update(buyer, "pay_done", cap)
    ctx = _ctx(application)
    await start_gift_customize(update, ctx, token=gift.token)
    cap.assert_turn("premium_gift_ask_message")

    update, query = _cb_update(buyer, "premium:gift_skip_msg", cap)
    await on_gift_skip_msg(update, ctx)
    cap.assert_turn("premium_gift_ask_image")

    update, query = _cb_update(buyer, "premium:gift_skip_img", cap)
    await on_gift_skip_img(update, ctx)
    cap.assert_turn("premium_gift_link_ready")
    ready = db.get_premium_gift(gift.token)
    assert ready is not None and ready.status == "ready"

    recipient = _FREE_UID + 22
    db.upsert_user(recipient)
    db.set_user_locale(recipient, "ru")
    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    ctx = _ctx(application)
    ctx.user_data["pending_gift_token"] = gift.token
    await maybe_offer_pending_gift(ctx, recipient, "ru")
    cap.assert_turn("premium_gift_offer")

    update, query = _cb_update(recipient, "gift_decline", cap)
    await on_gift_decline(update, ctx)
    query.edit_message_text.assert_awaited()


async def _scenario_twitch_link_wizard_offer(db) -> None:
    """§12 Twitch URL in DM outside wizard — offer create; decline; accept starts wizard."""
    from telegram.constants import ChatType
    from telegram.ext import ConversationHandler

    from handlers.wizard import (
        offer_twitch_link_wizard,
        on_twitch_link_decline,
        on_twitch_link_start,
    )

    twitch = MagicMock()
    twitch.parse_username = MagicMock(return_value="shroud")

    application, bot = _app(db, twitch=twitch)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, "https://www.twitch.tv/shroud", cap)
    update.effective_chat = SimpleNamespace(id=_FREE_UID, type=ChatType.PRIVATE)
    ctx = _ctx(application)
    await offer_twitch_link_wizard(update, ctx)
    cap.assert_turn("twitch_link_offer")

    update, query = _cb_update(_FREE_UID, "twitch_link:decline", cap)
    await on_twitch_link_decline(update, ctx)
    query.edit_message_text.assert_awaited()

    # Mid-wizard: same URL must not offer again.
    application, bot = _app(db, twitch=twitch)
    conv = MagicMock()
    key = (_FREE_UID, _FREE_UID)
    conv._get_key = MagicMock(return_value=key)
    conv._conversations = {key: 0}
    application.bot_data["main_conv"] = conv
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, "https://twitch.tv/shroud", cap)
    update.effective_chat = SimpleNamespace(id=_FREE_UID, type=ChatType.PRIVATE)
    ctx = _ctx(application)
    await offer_twitch_link_wizard(update, ctx)
    update.effective_message.reply_text.assert_not_awaited()

    # Accept → alert-type step + pending channel for prefill.
    application, bot = _app(db, twitch=twitch)
    cap = _BotCapture()
    cap.wrap(bot)
    update, query = _cb_update(_FREE_UID, "twitch_link:start:shroud", cap)
    update.effective_chat = SimpleNamespace(id=_FREE_UID, type=ChatType.PRIVATE)
    ctx = _ctx(application)
    state = await on_twitch_link_start(update, ctx)
    assert state != ConversationHandler.END
    assert ctx.user_data.get("pending_twitch_channel") == "shroud"
    cap.assert_turn("twitch_link_wizard_started")

    # Prefill channel leaves a lasting Reply Cancel (Extras is inline-only).
    from handlers.wizard import _go_channel_prompt

    application, bot = _app(db, twitch=twitch)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, "x", cap)
    update.effective_chat = SimpleNamespace(id=_FREE_UID, type=ChatType.PRIVATE)
    ctx = _ctx(application)
    ctx.user_data["alert_type"] = "live"
    ctx.user_data["pending_twitch_channel"] = "shroud"
    with patch(
        "handlers.wizard.receive_channel",
        new=AsyncMock(return_value=42),
    ):
        await _go_channel_prompt(update, ctx, "ru")
    assert update.effective_message.reply_text.await_count >= 1
    first = update.effective_message.reply_text.await_args_list[0]
    markup = first.kwargs.get("reply_markup")
    assert markup is not None
    labels = {
        (b.text if hasattr(b, "text") else b)
        for row in markup.keyboard
        for b in row
    }
    assert btn("wizard_cancel", "ru") in labels
    assert "shroud" in str(first.args[0] if first.args else first.kwargs.get("text"))


async def _scenario_subscriptions_list_pages(db) -> None:
    """§4 list pagination — long list yields pages; flip keeps working."""
    from handlers.subscriptions import list_subscriptions, on_list_page

    for i in range(12):
        db.add_subscription(
            owner_id=_FREE_UID,
            twitch_username=f"pagechan{i}",
            twitch_user_id=f"page{i}",
            message_template=("Live {username} " + ("настройки " * 40)),
            dest_type="dm",
            chat_id=_FREE_UID,
            thread_id=None,
            delay_minutes=i,
            ignore_keywords="kw " * 20,
            enabled=False,
        )

    application, bot = _app(db)
    bot.get_me = AsyncMock(return_value=SimpleNamespace(username="testbot"))
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("list", "ru"), cap)
    ctx = _ctx(application)
    with patch(
        "handlers.subscriptions.beta_features.is_enabled", return_value=False
    ):
        await list_subscriptions(update, ctx)
    cap.assert_turn("subscriptions_list_paged")
    pages = ctx.user_data.get("list_pages") or []
    assert len(pages) >= 1
    if len(pages) > 1:
        update, query = _cb_update(_FREE_UID, "list_page:1", cap)
        await on_list_page(update, ctx)
        query.edit_message_text.assert_awaited()


async def _scenario_schedule_deep(db) -> None:
    from handlers.stream_schedule import stream_schedule_confirm_callback

    async def _pulse(bot, chat_id, lang, *, back=True):
        cap.note_pulse()

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    ctx = _ctx(application)
    update, _query = _cb_update(_FREE_UID, "stream_sched:confirm:1", cap)
    cap.wrap(bot)
    with patch("handlers.stream_schedule._pulse_wizard_keyboard", new=_pulse):
        await stream_schedule_confirm_callback(update, ctx)
    cap.assert_turn("stream_schedule_game")


async def _scenario_schedule_vacation(db) -> None:
    """§5 Режим отпуска — календарь начала и автовыход с Cancel."""
    from handlers.stream_schedule import (
        start_stream_schedule,
        stream_schedule_mode_callback,
        stream_schedule_vacation_auto_callback,
        stream_schedule_vacation_callback,
        stream_schedule_vacation_manage_callback,
    )
    from i18n import (
        stream_schedule_vacation_active_keyboard,
        stream_schedule_vacation_auto_keyboard,
    )
    from telegram.ext import ConversationHandler

    async def _pulse(bot, chat_id, lang, *, back=True):
        cap.note_pulse()

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("create_schedule", "ru"), cap)
    ctx = _ctx(application)
    db.set_schedule_utc_offset_minutes(_FREE_UID, 180)
    with patch(
        "handlers.stream_schedule.beta_features.is_enabled", return_value=True
    ), patch("handlers.stream_schedule._pulse_wizard_keyboard", new=_pulse), patch(
        "handlers.stream_schedule._owner_vacation_active",
        new=AsyncMock(return_value=False),
    ), patch(
        "handlers.stream_schedule.prem.has_feature",
        new=AsyncMock(return_value=True),
    ):
        await start_stream_schedule(update, ctx)
        cap.assert_turn("stream_schedule_mode")
        update, _query = _cb_update(_FREE_UID, "stream_sched:mode:vacation", cap)
        cap.wrap(bot)
        await stream_schedule_mode_callback(update, ctx)
        cap.assert_turn("stream_schedule_vacation_start")
        update, _query = _cb_update(_FREE_UID, "vac:month:2026-09", cap)
        await stream_schedule_vacation_callback(update, ctx)
        cap.assert_turn("stream_schedule_vacation_days")
        update, _query = _cb_update(_FREE_UID, "vac:date:0", cap)
        await stream_schedule_vacation_callback(update, ctx)
        cap.assert_turn("stream_schedule_vacation_end")
        update, _query = _cb_update(_FREE_UID, "vac:date:1", cap)
        await stream_schedule_vacation_callback(update, ctx)
        cap.assert_turn("stream_schedule_vacation_auto")

    assert markup_has_escape_hatch(stream_schedule_vacation_auto_keyboard("ru"))
    assert markup_has_escape_hatch(stream_schedule_vacation_active_keyboard("ru"))
    update, _query = _cb_update(_FREE_UID, "stream_sched:vac_auto:0", cap)
    with patch(
        "handlers.stream_schedule.prem.has_feature",
        new=AsyncMock(return_value=False),
    ), patch("premium_handlers.send_premium_screen", new=AsyncMock()):
        state = await stream_schedule_vacation_auto_callback(update, ctx)
    assert state == ConversationHandler.END

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("create_schedule", "ru"), cap)
    ctx = _ctx(application)
    db.set_schedule_utc_offset_minutes(_FREE_UID, 180)
    with patch(
        "handlers.stream_schedule.beta_features.is_enabled", return_value=True
    ), patch("handlers.stream_schedule._pulse_wizard_keyboard", new=_pulse), patch(
        "handlers.stream_schedule._owner_vacation_active",
        new=AsyncMock(return_value=True),
    ), patch(
        "handlers.stream_schedule.prem.has_feature",
        new=AsyncMock(return_value=True),
    ):
        await start_stream_schedule(update, ctx)
        update, _query = _cb_update(_FREE_UID, "stream_sched:mode:vacation", cap)
        cap.wrap(bot)
        state = await stream_schedule_mode_callback(update, ctx)
        cap.assert_turn("stream_schedule_vacation_already")
    from handlers.stream_schedule import _owner_vacation_active, _sched_states

    assert state == _sched_states()["STREAM_SCHEDULE_MODE"]

    # Local vacation marker is enough when Helix broadcaster id is unknown.
    db.set_vacation_auto_exit_at(_FREE_UID, "2099-01-01T00:00:00Z")
    application, bot = _app(db)
    assert await _owner_vacation_active(db, application.bot_data["twitch"], _FREE_UID) is True
    db.set_vacation_auto_exit_at(_FREE_UID, None)
    db.set_vacation_ends_at(_FREE_UID, "2099-01-01T00:00:00Z")
    assert await _owner_vacation_active(db, application.bot_data["twitch"], _FREE_UID) is True
    db.set_vacation_ends_at(_FREE_UID, None)

    update, _query = _cb_update(_FREE_UID, "stream_sched:vac_manage:fix", cap)
    with patch(
        "handlers.stream_schedule.prem.has_feature",
        new=AsyncMock(return_value=True),
    ), patch(
        "handlers.stream_schedule._prompt_vacation_start",
        new=AsyncMock(return_value=_sched_states()["STREAM_SCHEDULE_VACATION"]),
    ) as start_vac:
        state = await stream_schedule_vacation_manage_callback(update, ctx)
    start_vac.assert_awaited()
    assert state == _sched_states()["STREAM_SCHEDULE_VACATION"]

    update, _query = _cb_update(_FREE_UID, "stream_sched:vac_manage:exit", cap)
    with patch(
        "handlers.stream_schedule.prem.has_feature",
        new=AsyncMock(return_value=True),
    ), patch(
        "handlers.stream_schedule._start_schedule_publish_auth",
        new=AsyncMock(return_value=ConversationHandler.END),
    ) as auth:
        state = await stream_schedule_vacation_manage_callback(update, ctx)
    auth.assert_awaited()
    assert state == ConversationHandler.END
    assert ctx.application.bot_data["pending_schedule_vacations"][_FREE_UID] == {
        "action": "disable"
    }

    # Free user: day / vacation modes are Premium-gated at entry.
    application, bot = _app(db)
    update, _query = _cb_update(_FREE_UID, "stream_sched:mode:day")
    ctx = _ctx(application)
    with patch(
        "handlers.stream_schedule.prem.has_feature",
        new=AsyncMock(return_value=False),
    ), patch("premium_handlers.send_premium_screen", new=AsyncMock()) as premium:
        state = await stream_schedule_mode_callback(update, ctx)
    assert state == ConversationHandler.END
    premium.assert_awaited()
    update, _query = _cb_update(_FREE_UID, "stream_sched:mode:vacation")
    with patch(
        "handlers.stream_schedule.prem.has_feature",
        new=AsyncMock(return_value=False),
    ), patch("premium_handlers.send_premium_screen", new=AsyncMock()) as premium2:
        state = await stream_schedule_mode_callback(update, ctx)
    assert state == ConversationHandler.END
    premium2.assert_awaited()


async def _scenario_settings_extended(db) -> None:
    from bot import open_premium_from_settings
    from handlers.settings import (
        open_settings_menu,
        open_sys_notifications_menu,
        open_whisper_alerts_menu,
        start_language_change,
    )
    from handlers.subscriptions import open_sync_settings

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("settings", "ru"), cap)
    ctx = _ctx(application)
    await open_settings_menu(update, ctx)
    await open_sys_notifications_menu(update, ctx)
    cap.assert_turn("settings_sys_notifications")

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("language", "ru"), cap)
    ctx = _ctx(application)
    await start_language_change(update, ctx)
    cap.assert_turn("settings_language")

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("settings", "ru"), cap)
    ctx = _ctx(application)
    await open_settings_menu(update, ctx)
    with patch("handlers.subscriptions.prem.has_feature", new=AsyncMock(return_value=False)):
        await open_sync_settings(update, ctx)
    cap.assert_turn("settings_sync_gate")

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("settings", "ru"), cap)
    ctx = _ctx(application)
    await open_settings_menu(update, ctx)
    await open_whisper_alerts_menu(update, ctx)
    cap.assert_turn("settings_whispers")

    application, bot = _app(db)
    cap = _BotCapture()
    cap.wrap(bot)
    update = _msg_update(_FREE_UID, btn("settings", "ru"), cap)
    ctx = _ctx(application)
    await open_settings_menu(update, ctx)
    with patch("premium_handlers.open_premium_menu", new=AsyncMock()) as prem_open:
        await open_premium_from_settings(update, ctx)
        prem_open.assert_awaited()
    cap.assert_turn("settings_premium_open")


async def _run_flow_nav_checks() -> None:
    with tempfile.TemporaryDirectory() as td:
        db = open_database(Path(td) / "flow_nav.db")
        db.upsert_user(_FREE_UID)
        db.set_user_locale(_FREE_UID, "ru")
        await _scenario_menus_and_wizards(db)
        await _scenario_wizard_channel_step(db)
        await _scenario_subscriptions(db)
        await _scenario_settings_and_partner(db)
        await _scenario_admin_broadcast(db)
        await _scenario_feedback(db)
        await _scenario_wizard_deep(db)
        await _scenario_wizard_template_typo(db)
        await _scenario_wizard_finish(db)
        await _scenario_wizard_custom_buttons(db)
        await _scenario_wizard_drops_game(db)
        await _scenario_wizard_game_alert(db)
        await _scenario_wizard_extras_checkboxes(db)
        await _scenario_wizard_image_ask(db)
        await _scenario_import(db)
        await _scenario_alert_history(db)
        await _scenario_subscriptions_edit_pick(db)
        await _scenario_subscriptions_edit_type_copy(db)
        await _scenario_subscriptions_edit_checkboxes(db)
        await _scenario_subscriptions_delete(db)
        await _scenario_share_alert_offer(db)
        await _scenario_premium_gift(db)
        await _scenario_twitch_link_wizard_offer(db)
        await _scenario_subscriptions_list_pages(db)
        await _scenario_schedule_deep(db)
        await _scenario_schedule_vacation(db)
        await _scenario_schedule_publish_chain(db)
        await _scenario_settings_extended(db)


def check_flow_nav() -> None:
    _check_submenu_reply_keyboards()
    _check_inline_wizard_keyboards()
    asyncio.run(_run_flow_nav_checks())
