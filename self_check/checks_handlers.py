"""Handler self-checks: bot wiring, alert history, premium UI, whispers-related strings."""
import json
import re
from pathlib import Path
import os
from urllib.parse import urlparse
import tempfile
from datetime import datetime, timedelta, timezone

from config import SCHEDULE_CHECK_INTERVAL, parse_admin_user_ids
from links import parse_telegram_topic_link, chat_ref_to_id
from twitch import (
    FOLLOWS_SCOPE,
    SCHEDULE_SCOPE,
    WHISPERS_SCOPE,
    TwitchClient,
    filter_streams_for_watch,
    find_placeholder_typos,
    normalize_ignore_keywords,
    merge_ignore_keywords,
    normalize_watch_tags,
    pick_random_streams,
    preview_stream_title,
    render_template,
    should_ignore_stream,
    twitch_status_fingerprint,
)
from translate import build_translations, markdown_to_telegram_html, translate_text
from bot import (
    _alert_history_item_url,
    _alert_history_nav_keyboard,
    _build_alert_history_chunks,
    _edit_present_types,
    _format_alert_history_block,
    _format_twitch_status_message,
    _format_posthog_status_message,
    _format_vod_timestamp,
    _format_watch_vod_suggestions,
    _help_text,
    _is_link_preview_disabled,
    _is_http_timeout,
    _is_unchanged_message_edit,
    _load_posthog_seen_report_ids,
    _message_link,
    _save_posthog_seen_report_ids,
    _parse_broadcast_recipient_ids,
    _parse_sb_edit_f_id,
    _parse_segment_start,
    _parse_watch_viewers,
    _premium_gate_text,
    _twitch_vod_url,
    _vod_id_from_videos,
    _watch_channel_refs,
    import_followed_as_subscriptions,
    live_transitions,
    category_change_events,
    migrate_import_sync_subscriptions,
    needs_live_game_recheck,
    _format_pause_until,
    _user_notifications_paused,
    TWITCH_STATUS_HOST,
    TWITCH_STATUS_PAGE_URL,
)
from db import (
    AlertHistoryEntry,
    SqliteDatabase,
    WATCH_MAX_FILTERS,
    WatchPrefs,
    dump_category_watch_prefs,
    dump_watch_filters,
    dump_watch_prefs,
    is_category_watch_sub,
    parse_category_watch_prefs,
    parse_watch_filters,
    parse_watch_prefs,
    watch_filter_auto_name,
    _normalize_pg_url,
    open_database,
)
from i18n import SUPPORTED_LOCALES, btn, t as tr
from health import create_oauth_state, parse_posthog_issue_payload, pop_oauth_state
from telegram.error import BadRequest
from premium import FEATURE_IDS
from hf_text import _normalize_template
from telegram import LinkPreviewOptions, Message



def check_handlers() -> None:
    import premium as prem
    from i18n import premium_features_keyboard
    from premium import (
        apply_features_payment,
        apply_lifetime_payment,
        apply_stars_payment,
        start_trial,
    )

    # Chat fixes: callback wiring, PTB Stars typo, cancel copy, deploy polling, Other audience.
    import inspect
    from pathlib import Path as _Path

    from telegram import Bot

    import main as main_mod
    from bot import _dump_broadcast_recipient_ids
    from i18n import admin_other_audience_keyboard, premium_owned_keyboard
    from premium_handlers import _premium_markup

    bot_src = _Path(__file__).resolve().parents[1].joinpath("bot.py").read_text(
        encoding="utf-8"
    )
    wizard_src = (
        _Path(__file__).resolve().parents[1].joinpath("handlers/wizard.py").read_text(
            encoding="utf-8"
        )
    )
    subscriptions_src = (
        _Path(__file__)
        .resolve()
        .parents[1]
        .joinpath("handlers/subscriptions.py")
        .read_text(encoding="utf-8")
    )
    monitoring_src = (
        _Path(__file__).resolve().parents[1].joinpath("handlers/monitoring.py").read_text(
            encoding="utf-8"
        )
    )
    assert "cancel_feat:.+" in bot_src
    assert "owned|" in bot_src
    # Bot API has no getForumTopic; 404 is mapped to PTB InvalidToken.
    assert "getForumTopic" not in bot_src
    # Edit ignore-keywords: single inline Cancel (no reply pulse / no junk carrier).
    edit_ignore_chunk = subscriptions_src.split(
        "async def start_edit_ignore_keywords", 1
    )[1].split("async def receive_edit_ignore_keywords", 1)[0]
    assert "as_cancel=True" in edit_ignore_chunk
    assert "_pulse_wizard_keyboard" not in edit_ignore_chunk
    assert "edit_message_reply_markup" not in edit_ignore_chunk
    assert "_wizard(" not in edit_ignore_chunk
    # Create ignore-keywords: inline Back/Cancel (no reply pulse).
    create_ignore_chunk = wizard_src.split("async def _go_ignore_keywords_prompt", 1)[1].split(
        "async def _go_link_preview_prompt", 1
    )[0]
    assert "show_back=True" in create_ignore_chunk
    assert "show_cancel=True" in create_ignore_chunk
    assert "_pulse_wizard_keyboard" not in create_ignore_chunk
    assert 'ignore_keywords:back"' in bot_src or "ignore_keywords:back$" in bot_src
    assert "receive_ignore_keywords_back" in bot_src
    assert "drop_pending_updates=False" in inspect.getsource(main_mod.main)
    assert "mark_ready()" in bot_src
    assert "_is_unchanged_message_edit(err)" in bot_src or (
        "_is_unchanged_message_edit(err)" in monitoring_src
    )
    assert "posthog_seen_reports.json" in monitoring_src
    ptb_edit = inspect.getsource(Bot.edit_user_star_subscription)
    # PTB internals naming may differ slightly across versions:
    # - older: editUserStarSubscription
    # - newer: editUserStartSubscription (typo preserved in method plumbing)
    assert (
        "editUserStarSubscription" in ptb_edit
        or "editUserStartSubscription" in ptb_edit
    )
    ph_src = _Path(__file__).resolve().parents[1].joinpath(
        "premium_handlers.py"
    ).read_text(encoding="utf-8")
    assert "edit_user_star_subscription(" in ph_src
    assert "editUserStartSubscription" not in ph_src
    cancel_block = ph_src.split('if action == "cancel":', 1)[1].split(
        'if action == "marfapr":', 1
    )[0]
    assert 't("import_failed"' not in cancel_block
    assert "premium_cancel_feat_done" in ph_src
    assert "set_premium_feature_canceled" in ph_src
    assert "clear_premium_feature" not in ph_src.split(
        'if data.startswith("premium:cancel_feat:")', 1
    )[1].split("if data == \"premium:feat_pay\":", 1)[0]
    assert "{until}" in tr("premium_cancel_feat_done", "ru")
    assert "{until}" in tr("premium_cancel_feat_done", "en")
    assert "{until}" in tr("premium_cancel_done", "ru")
    assert "{until}" in tr("premium_cancel_done", "en")
    assert tr("premium_cancel_failed", "ru")
    assert tr("premium_pay_failed", "ru")
    assert "Twitch" not in tr("premium_cancel_feat_done", "ru")
    assert "Twitch" not in tr("premium_pay_failed", "ru")
    assert "снята" not in tr("premium_cancel_feat_done", "ru").lower()
    assert "Автопродление подписки отключено" in tr("premium_cancel_done", "ru")
    assert "Автопродление подписки отключено" in tr("premium_cancel_feat_done", "ru")
    assert tr("premium_owned_feat_canceled", "ru")
    assert "автопродление выкл" in tr("premium_feat_line_canceled", "ru")
    assert "Stars" not in tr("premium_status_stars", "ru")
    assert "Stars" not in tr("premium_status_stars_canceled", "ru")
    assert "Stars" not in tr("premium_owned_stars", "ru")
    assert "Stars" not in tr("premium_owned_stars_canceled", "ru")
    assert "Stars" not in btn("premium_cancel_stars", "ru")
    aud_cb = {
        b.callback_data
        for row in admin_other_audience_keyboard("ru").inline_keyboard
        for b in row
        if b.callback_data
    }
    assert aud_cb == {"admin_audience:ids", "admin_audience:all", "admin_audience:cancel"}
    assert tr("broadcast_audience_ids", "ru") == "Указать ID"
    assert tr("broadcast_audience_all", "ru") == "Разослать всем"
    owned_kb = premium_owned_keyboard(
        "ru", stars_cancelable=True, feature_ids=["alert_types"]
    )
    owned_cb = {
        b.callback_data
        for row in owned_kb.inline_keyboard
        for b in row
        if b.callback_data
    }
    assert "premium:cancel" in owned_cb
    assert "premium:cancel_feat:alert_types" in owned_cb
    assert _dump_broadcast_recipient_ids([9, 9, 8, "x"]) == "9,8"

    with tempfile.TemporaryDirectory() as d:
        db = SqliteDatabase(Path(d) / "pay_btns.db")
        db.upsert_user(249097744)
        kb = _premium_markup(
            db, 249097744, "ru", free_chat=True, force_free=False
        )
        assert kb is not None
        callbacks = {
            b.callback_data
            for row in kb.inline_keyboard
            for b in row
            if b.callback_data
        }
        assert "premium:month" in callbacks
        apply_features_payment(
            db,
            249097744,
            feature_ids=["alert_types"],
            charge_id="stx_test",
            until_unix=10**12,
            stars_paid=1,
        )
        assert prem.is_premium(db, 249097744)
        assert not prem.get_status(db, 249097744).has_full_plan
        assert db.get_bot_stats().premium_paid == 1
        assert not prem.has_feature_sync(db, 249097744, "extra_alerts")
        assert prem.can_enable_more(db, 249097744) is True
        # Cancel renew: keep access until expiry; block repurchase.
        db.set_premium_feature_canceled(249097744, "alert_types")
        st_c = prem.get_status(db, 249097744)
        assert st_c.feature_active("alert_types")
        assert st_c.is_feature_canceled("alert_types")
        assert not st_c.feature_cancelable("alert_types")
        assert db.get_bot_stats().premium_paid == 1
        kb_c = _premium_markup(
            db, 249097744, "ru", free_chat=False, force_free=False
        )
        assert kb_c is not None
        cb_c = {
            b.callback_data
            for row in kb_c.inline_keyboard
            for b in row
            if b.callback_data
        }
        assert "premium:month" not in cb_c
        assert "premium:features" in cb_c
        assert "premium:owned" in cb_c
        db.clear_premium_feature(249097744, "alert_types")
        assert not prem.get_status(db, 249097744).feature_active("alert_types")
        assert db.get_bot_stats().premium_paid == 0
        apply_features_payment(
            db,
            249097744,
            feature_ids=["alert_types"],
            charge_id="stx_test2",
            until_unix=10**12,
            stars_paid=1,
        )
        assert prem.can_enable_more(db, 249097744) is True
        db2 = SqliteDatabase(Path(d) / "perm_not_paid.db")
        db2.upsert_user(1)
        db2.set_premium_permanent(1, True)
        assert db2.get_bot_stats().premium_paid == 0
        for i in range(5):
            db.add_subscription(
                owner_id=249097744,
                twitch_username=f"lim{i}",
                twitch_user_id=str(9000 + i),
                message_template="x",
                dest_type="dm",
                chat_id=249097744,
                thread_id=None,
            )
        assert prem.can_enable_more(db, 249097744) is False
        assert prem.is_promo_channel("marfapr") is True
        assert prem.is_promo_channel("https://twitch.tv/Other") is False
        assert prem.is_promo_channel("paidstreamer", db) is False
        db.upsert_premium_channel(
            twitch_user_id="pc1",
            twitch_login="PaidStreamer",
            display_name="Paid Streamer",
            owner_telegram_id=1,
            charge_id="ch_test",
        )
        assert prem.is_promo_channel("paidstreamer", db) is True
        assert "paidstreamer" in prem.list_promo_channel_logins(db)
        assert prem.can_enable_more(db, 249097744, twitch_username="paidstreamer") is True
        assert prem.has_feature_sync(db, 249097744, "delay", channel="paidstreamer")
        assert prem.can_enable_more(db, 249097744, twitch_username="marfapr") is True
        assert prem.has_feature_sync(db, 249097744, "alert_types", channel="marfapr")
        assert prem.has_feature_sync(db, 249097744, "delay", channel="MarfaPR")
        assert not prem.has_feature_sync(db, 249097744, "delay", channel="other")
        assert prem.chat_send_unlimited(db, 249097744, broadcaster_login="marfapr")
        assert not prem.chat_send_unlimited(db, 249097744, broadcaster_login="other")
        # Promo channel does not consume free active slots.
        db.add_subscription(
            owner_id=249097744,
            twitch_username="marfapr",
            twitch_user_id="promo1",
            message_template="x",
            dest_type="dm",
            chat_id=249097744,
            thread_id=None,
            enabled=True,
        )
        assert prem.active_subscription_slots(db, 249097744).remaining == 0
        assert prem.can_enable_more(db, 249097744) is False
        assert prem.can_enable_more(db, 249097744, twitch_username="marfapr") is True
        kb2 = _premium_markup(
            db, 249097744, "ru", free_chat=True, force_free=False
        )
        assert kb2 is not None
        cb2 = {
            b.callback_data
            for row in kb2.inline_keyboard
            for b in row
            if b.callback_data
        }
        assert "premium:month" not in cb2
        assert "premium:features" in cb2
        assert "premium:owned" in cb2
        assert "alert_types" not in {
            (b.callback_data or "").split(":")[-1]
            for row in premium_features_keyboard(
                "ru",
                set(),
                user_id=249097744,
                owned={"alert_types"},
            ).inline_keyboard
            for b in row
            if (b.callback_data or "").startswith("premium:feat_toggle:")
        }
        purchasable = set(prem.purchasable_feature_ids())
        assert "stream_chat" in purchasable
        assert "deleted_subscriptions_cart" in purchasable
        assert "alert_history" in purchasable
        toggle_ids = {
            (b.callback_data or "").split(":")[-1]
            for row in premium_features_keyboard(
                "ru", set(), user_id=249097744, owned=set()
            ).inline_keyboard
            for b in row
            if (b.callback_data or "").startswith("premium:feat_toggle:")
        }
        assert "stream_chat" in toggle_ids
        assert "deleted_subscriptions_cart" in toggle_ids
        db.upsert_user(2)
        assert _premium_markup(db, 2, "ru", free_chat=True, force_free=False) is None
    _pt = tr("premium_title", "ru", free_limit=5, stars=100, channel="marfapr", status="s")
    assert "Продвинутый режим" in _pt
    assert "мини-приложении" in _pt
    assert "Премиум-канал" in _pt
    assert "рекомендация" in _pt
    assert tr("btn_premium", "en")
    assert "Пробный" in tr("btn_premium_trial", "ru") or "триал" in tr(
        "btn_premium_trial", "ru"
    ).lower() or "Пробный" in tr("btn_premium_trial", "ru")
    with tempfile.TemporaryDirectory() as d:
        db = SqliteDatabase(Path(d) / "premium.db")
        db.upsert_user(1)
        assert not prem.is_premium(db, 1)
        apply_stars_payment(
            db,
            1,
            charge_id="chg",
            until_unix=int(
                (datetime.now(timezone.utc) + timedelta(days=40)).timestamp()
            ),
        )
        assert prem.is_premium(db, 1)
        assert db.count_enabled_subscriptions(1) == 0
        assert db.count_stars_payers_since(datetime.now(timezone.utc) - timedelta(days=1)) == 1
        # Status helper: free-chat maps to permanent wording without naming the chat
        from premium_handlers import _status_text

        assert "пожизненный премиум" in _status_text(db, 2, "ru", free_chat=True)
        assert "Покупка новых тарифов" in _status_text(db, 2, "ru", free_chat=True)
        assert "бесплатный план" in _status_text(db, 2, "ru", free_chat=False)
        assert "Покупка новых тарифов" not in _status_text(db, 2, "ru", free_chat=False)
        assert "бесплатный план" in _status_text(
            db, 1, "ru", force_free=True
        )
        # Active Stars with auto-renew → no buy-after note.
        assert "Покупка новых тарифов" not in _status_text(db, 1, "ru")
        db.set_premium_stars_canceled(1, True)
        canceled_status = _status_text(db, 1, "ru")
        assert "автопродление выкл" in canceled_status
        assert "Покупка новых тарифов" in canceled_status
        assert "<b>" in tr("premium_buy_after_current", "ru")
        assert "<b>" in tr("premium_buy_after_current", "en")
        db.set_premium_stars_canceled(1, False)
        from premium_handlers import _premium_markup

        assert _premium_markup(db, 2, "ru", free_chat=False, force_free=False) is not None
        assert _premium_markup(db, 2, "ru", free_chat=True, force_free=False) is None
        assert _premium_markup(db, 1, "ru", free_chat=False, force_free=True) is not None
        import demo_mode as dm
        from premium_handlers import (
            _blocks_feature_purchase,
            _blocks_plan_purchase,
            _demo_force_free,
        )

        # Stars/full plan blocks real purchases; demo force-free must not.
        assert _demo_force_free(1) is False
        assert _blocks_plan_purchase(db, 1) is True
        assert _blocks_feature_purchase(db, 1) is True
        dm.activate(1)
        assert _demo_force_free(1) is True
        assert _blocks_plan_purchase(db, 1) is False
        assert _blocks_feature_purchase(db, 1) is False
        assert "бесплатный план" in _status_text(db, 1, "ru", force_free=True)
        dm.deactivate(1)
        assert _blocks_plan_purchase(db, 1) is True
        # Free user (no plan) can purchase.
        db.upsert_user(55)
        assert _blocks_plan_purchase(db, 55) is False
        assert _blocks_feature_purchase(db, 55) is False
        dm.deactivate(1)

    with tempfile.TemporaryDirectory() as d:
        db = SqliteDatabase(Path(d) / "trial.db")
        db.upsert_user(50)
        sid = db.add_subscription(
            50,
            "x",
            "1",
            "hi",
            "dm",
            50,
            None,
            enabled=True,
            notify_on_live=True,
        )
        ok, reason = start_trial(db, 50)
        assert ok and reason == "started"
        assert prem.is_premium(db, 50)
        st_trial = prem.get_status(db, 50)
        assert st_trial.trial_active and st_trial.has_full_plan
        # Full plan unlocks every FEATURE_IDS entry (not only is_premium).
        for fid in prem.FEATURE_IDS:
            assert prem.has_feature_sync(db, 50, fid), fid
        for fid in ("ignore_keywords", "delay", "repeat", "delete_prev"):
            assert prem.has_feature_sync(db, 50, fid), fid
        assert prem.active_subscription_slots(db, 50).unlimited is True
        ok2, reason2 = start_trial(db, 50)
        assert not ok2 and reason2 == "active"
        # Force expire: features close by clock; ensure_trial_expired pauses subs.
        db.set_premium_trial(50, until_unix=1, used=True)
        assert not prem.get_status(db, 50).trial_active
        assert not prem.get_status(db, 50).has_full_plan
        for fid in prem.FEATURE_IDS:
            assert not prem.has_feature_sync(db, 50, fid), fid
        assert db.get_subscription(sid, 50).enabled is True  # pause is lazy
        assert prem.ensure_trial_expired(db, 50) is True
        sub = db.get_subscription(sid, 50)
        assert sub is not None
        assert sub.enabled is False
        assert sub.trial_paused is True
        assert prem.get_status(db, 50).trial_until == 0
        assert prem.active_subscription_slots(db, 50).unlimited is False
        assert prem.is_live_only_alert(sub)
        ok3, reason3 = start_trial(db, 50)
        assert not ok3 and reason3 == "used"

        # Background sweep: expire all due trials in one pass.
        db.upsert_user(60)
        db.upsert_user(61)
        db.upsert_user(62)
        s60 = db.add_subscription(
            60, "a", "1", "hi", "dm", 60, None, enabled=True, notify_on_live=True
        )
        s61 = db.add_subscription(
            61, "b", "2", "hi", "dm", 61, None, enabled=True, notify_on_live=True
        )
        future = int(__import__("time").time()) + 86400
        recent = int(__import__("time").time()) - 30
        db.set_premium_trial(60, until_unix=1, used=True)
        db.set_premium_trial(61, until_unix=recent, used=True)
        db.set_premium_trial(62, until_unix=future, used=True)
        expired = prem.expire_due_trials(db)
        assert len(expired) == 2
        by_uid = {uid: until for uid, until in expired}
        assert 60 in by_uid and 61 in by_uid
        assert not prem.trial_expiry_should_notify(by_uid[60], max_age_sec=120)
        assert prem.trial_expiry_should_notify(by_uid[61], max_age_sec=120)
        assert db.get_premium_status(60).trial_until == 0
        assert db.get_premium_status(61).trial_until == 0
        assert db.get_premium_status(62).trial_until == future
        assert db.get_subscription(s60, 60).enabled is False
        assert db.get_subscription(s60, 60).trial_paused is True
        assert db.get_subscription(s61, 61).trial_paused is True
        assert prem.expire_due_trials(db) == []

        db.upsert_user(51)
        apply_lifetime_payment(db, 51, charge_id="life1", stars_paid=2000)
        assert prem.get_status(db, 51).permanent
        db.upsert_user(52)
        apply_features_payment(
            db,
            52,
            feature_ids=["alert_history", "extra_alerts"],
            charge_id="feat1",
            until_unix=10**12,
            stars_paid=40,
        )
        st52 = prem.get_status(db, 52)
        assert st52.feature_active("alert_history")
        assert st52.feature_active("extra_alerts")
        assert not st52.feature_active("twitch_sync")
        assert st52.is_premium
        assert not st52.has_full_plan
        assert st52.feature_charge_id("alert_history") == "feat1"
        assert prem.has_feature_sync(db, 52, "alert_history")
        assert not prem.has_feature_sync(db, 52, "twitch_sync")
        db.clear_premium_feature(52, "alert_history")
        assert not prem.get_status(db, 52).feature_active("alert_history")
        assert prem.get_status(db, 52).feature_active("extra_alerts")

    with tempfile.TemporaryDirectory() as d:
        db = SqliteDatabase(Path(d) / "ref.db")
        db.upsert_user(10)
        db.upsert_user(20)
        assert db.set_referred_by(20, 10) is True
        assert db.set_referred_by(20, 11) is False  # already set
        assert db.get_referred_by(20) == 10
        assert db.set_referred_by(10, 10) is False  # self
        apply_stars_payment(
            db, 20, charge_id="pay1", until_unix=10**12, stars_paid=100
        )
        stats = db.get_referral_stats(10)
        assert stats.invited == 1
        assert stats.payments == 1
        assert stats.available_stars == 10
        # Same charge must not double-credit.
        apply_stars_payment(
            db, 20, charge_id="pay1", until_unix=10**12, stars_paid=100
        )
        assert db.get_referral_stats(10).available_stars == 10
        apply_stars_payment(
            db, 20, charge_id="pay2", until_unix=10**12, stars_paid=5000
        )
        assert db.get_referral_stats(10).available_stars == 510
        assert db.request_referral_withdrawal(10, 9999) is None  # over available
        wid = db.request_referral_withdrawal(10, 510)
        assert wid is not None
        assert db.get_referral_stats(10).available_stars == 0
        listed = db.list_referral_withdrawals(10)
        assert len(listed) == 1 and listed[0].id == wid
        pending = db.list_pending_referral_withdrawals()
        assert any(p.id == wid for p in pending)
        paid = db.resolve_referral_withdrawal(wid, "paid")
        assert paid is not None and paid.status == "paid"
        assert db.resolve_referral_withdrawal(wid, "rejected") is None
        assert db.list_pending_referral_withdrawals() == []
        # Alert history: newest first, 60-day storage window.
        assert db.list_alert_history(10) == []
        for i in range(3):
            db.add_alert_history(
                10,
                subscription_id=i + 1,
                twitch_username=f"user{i}",
                alert_type="live" if i % 2 == 0 else "end",
                message_text=f"Hello {i}",
            )
        hist = db.list_alert_history(10)
        assert len(hist) == 3
        assert hist[0].twitch_username == "user2"
        assert hist[0].alert_type == "live"
        assert hist[0].message_text == "Hello 2"
        assert hist[-1].twitch_username == "user0"
        recent = db.list_alert_history(
            10, since=datetime.now(timezone.utc) - timedelta(days=7)
        )
        assert len(recent) == 3
        assert "alert_history" in FEATURE_IDS
        assert "advanced_mode" in FEATURE_IDS
        assert "ignore_keywords" not in FEATURE_IDS
        assert "delay" not in FEATURE_IDS
        assert "repeat" not in FEATURE_IDS
        assert "delete_prev" not in FEATURE_IDS
        assert tr("premium_feat_advanced_mode", "ru") == "Продвинутый режим"
        assert tr("premium_feat_alert_history", "ru")
        assert "упрощённом режиме" in tr("wizard_simple_mode_note", "ru")
        assert tr("btn_advanced_mode", "ru")
        assert "стоп-слова" in tr("advanced_mode_screen", "ru")
        assert "Premium-каналов" in tr("advanced_mode_premium_only", "ru")
        db.upsert_user(77)
        assert db.get_advanced_mode_setting(77) is None
        assert not prem.is_advanced_mode_enabled(db, 77)
        # Explicit on without Premium entitlement → still off.
        db.set_advanced_mode_setting(77, True)
        assert prem.is_advanced_mode_enabled(db, 77) is False
        assert prem.is_advanced_mode_enabled(db, 77, entitled=True) is True
        db.set_advanced_mode_setting(77, False)
        assert prem.is_advanced_mode_enabled(db, 77, entitled=True) is False
        # Legacy à la carte unlocks still count as advanced_mode entitlement.
        import time as _time

        until = int(_time.time()) + 3600
        db.extend_premium_features(88, ["delay"], until_unix=until, charge_id="c1")
        assert prem.has_feature_sync(db, 88, "advanced_mode")
        assert prem.has_feature_sync(db, 88, "ignore_keywords")
        apply_features_payment(
            db,
            89,
            feature_ids=["advanced_mode"],
            charge_id="c2",
            until_unix=until,
            stars_paid=20,
        )
        assert prem.has_feature_sync(db, 89, "delay")
        assert db.get_advanced_mode_setting(89) is True
        assert prem.is_advanced_mode_enabled(db, 89) is True
        # Auto-on only when entitled + alert already uses advanced options.
        db.upsert_user(90)
        sid90 = db.add_subscription(
            owner_id=90,
            twitch_username="x",
            twitch_user_id="90",
            message_template="hi",
            dest_type="dm",
            chat_id=90,
            thread_id=None,
            delay_minutes=5,
        )
        assert sid90
        assert prem.is_advanced_mode_enabled(db, 90) is False  # not premium
        db.set_premium_permanent(90, True)
        assert prem.is_advanced_mode_enabled(db, 90) is True  # auto-on
        db.set_advanced_mode_setting(90, False)
        assert prem.is_advanced_mode_enabled(db, 90) is False
        # Demo mode forces off even if setting/premium would enable it.
        import demo_mode as _dm

        _dm.activate(89)
        assert prem.is_advanced_mode_enabled(db, 89) is False
        _dm.deactivate(89)
        assert prem.is_advanced_mode_enabled(db, 89) is True
        # migrate_advanced_mode_defaults: ON only if entitled + alert options.
        db.upsert_user(91)
        db.set_premium_permanent(91, True)
        sid91 = db.add_subscription(
            owner_id=91,
            twitch_username="y",
            twitch_user_id="91",
            message_template="hi",
            dest_type="dm",
            chat_id=91,
            thread_id=None,
            delay_minutes=3,
        )
        assert sid91
        examined, on_n, off_n = prem.migrate_advanced_mode_defaults(db)
        assert examined >= 1
        assert db.get_advanced_mode_setting(91) is True
        assert db.get_advanced_mode_setting(77) is False  # not entitled
        assert on_n >= 1 and off_n >= 1
        assert tr("btn_alert_history_more", "ru") == "Ещё"
        assert tr("alert_history_go_stream", "ru") == "Перейти к стриму"
        assert "<b>📅 " in tr("alert_history_day", "ru", date="пятница, 14 августа")
        assert _format_vod_timestamp(45) == "45s"
        assert _format_vod_timestamp(125) == "2m5s"
        assert _format_vod_timestamp(3723) == "1h2m3s"
        assert _twitch_vod_url("99") == "https://www.twitch.tv/videos/99"
        assert _twitch_vod_url("99", 125) == "https://www.twitch.tv/videos/99?t=2m5s"
        assert _vod_id_from_videos([{"id": "v1", "stream_id": "s1"}], "s1") == "v1"
        assert _vod_id_from_videos([{"id": "v1", "stream_id": "s1"}], "no") == ""
        vod_text = _format_watch_vod_suggestions(
            [
                {
                    "id": "99",
                    "user_login": "alice",
                    "user_name": "Alice",
                    "title": "Hi",
                    "game_name": "Just Chatting",
                    "duration": "1h2m3s",
                    "url": "https://www.twitch.tv/videos/99",
                }
            ],
            WatchPrefs(
                categories=[{"id": "1", "name": "Just Chatting"}],
                min_viewers=0,
                max_viewers=None,
                language=None,
                tags=[],
                exclude_mature=False,
            ),
            "en",
        )
        assert "No one is live" in vod_text
        assert "https://www.twitch.tv/videos/99" in vod_text
        assert "twitch.tv/alice" not in vod_text
        assert "1h2m3s" in vod_text
        # Without video id — skip item (never fall back to channel URL).
        no_id = _format_watch_vod_suggestions(
            [
                {
                    "id": "",
                    "user_login": "alice",
                    "user_name": "Alice",
                    "title": "Hi",
                    "game_name": "Just Chatting",
                    "duration": "1h",
                    "url": "https://www.twitch.tv/alice",
                }
            ],
            WatchPrefs(
                categories=[{"id": "1", "name": "Just Chatting"}],
                min_viewers=0,
                max_viewers=None,
                language=None,
                tags=[],
                exclude_mature=False,
            ),
            "en",
        )
        assert "alice" not in no_id
        assert "twitch.tv/alice" not in no_id
        cat_item = AlertHistoryEntry(
            id=1,
            owner_id=1,
            subscription_id=1,
            twitch_username="frank_sg",
            alert_type="category",
            message_text="",
            sent_at="",
            vod_id="99",
            vod_offset_seconds=125,
        )
        assert _alert_history_item_url(cat_item) == "https://www.twitch.tv/videos/99?t=2m5s"
        live_item = AlertHistoryEntry(
            id=1,
            owner_id=1,
            subscription_id=1,
            twitch_username="frank_sg",
            alert_type="live",
            message_text="",
            sent_at="",
            vod_id="99",
            vod_offset_seconds=125,
        )
        assert _alert_history_item_url(live_item) == "https://www.twitch.tv/videos/99"
        hist_block = _format_alert_history_block(
            time_str="17:00",
            username="frank_sg",
            body="Hello <b>x</b>",
            lang="ru",
        )
        assert "• 17:00 — <b>frank_sg</b>" in hist_block
        assert "Hello &lt;b&gt;x&lt;/b&gt;" in hist_block
        assert '<a href="https://twitch.tv/frank_sg">Перейти к стриму</a>' in hist_block
        assert "videos/99?t=2m5s" in _format_alert_history_block(
            time_str="17:00",
            username="frank_sg",
            body="Hi",
            lang="ru",
            stream_url="https://www.twitch.tv/videos/99?t=2m5s",
        )
        nav_mid = _alert_history_nav_keyboard("ru", 0, 3, show_more=True)
        assert nav_mid is not None
        mid_data = [b.callback_data for row in nav_mid.inline_keyboard for b in row]
        assert "alert_history:page:1" in mid_data
        assert "alert_history:more" not in mid_data
        nav_last = _alert_history_nav_keyboard("ru", 2, 3, show_more=True)
        last_data = [b.callback_data for row in nav_last.inline_keyboard for b in row]
        assert "alert_history:more" in last_data
        assert "alert_history:page:1" in last_data
        nav_free_one = _alert_history_nav_keyboard("ru", 0, 1, show_more=True)
        assert nav_free_one is not None
        assert any(
            b.callback_data == "alert_history:more"
            for row in nav_free_one.inline_keyboard
            for b in row
        )
        nav_premium_one = _alert_history_nav_keyboard("ru", 0, 1, show_more=False)
        assert nav_premium_one is not None
        assert any(
            b.callback_data == "alert_history:menu"
            for row in nav_premium_one.inline_keyboard
            for b in row
        )
        fat_items = [
            AlertHistoryEntry(
                id=i,
                owner_id=10,
                subscription_id=1,
                twitch_username=f"u{i}",
                alert_type="live",
                message_text=("x" * 800),
                sent_at=datetime.now(timezone.utc).isoformat(),
            )
            for i in range(12)
        ]
        pages = _build_alert_history_chunks(fat_items, "ru", 7)
        assert len(pages) > 1
        assert all(len(p) <= 4100 for p in pages)
        db.add_alert_history(
            10,
            subscription_id=9,
            twitch_username="frank_sg",
            alert_type="category",
            message_text="cat",
            twitch_user_id="uid1",
            stream_id="sid1",
            vod_offset_seconds=125,
        )
        vod_row = db.list_alert_history(10)[0]
        assert vod_row.stream_id == "sid1"
        assert vod_row.twitch_user_id == "uid1"
        assert vod_row.vod_offset_seconds == 125
        db.set_alert_history_vod_id(vod_row.id, "99")
        assert db.list_alert_history(10)[0].vod_id == "99"
        # Reject restores available balance.
        apply_stars_payment(
            db, 20, charge_id="pay3", until_unix=10**12, stars_paid=1000
        )
        wid2 = db.request_referral_withdrawal(10, 100)
        assert wid2 is not None
        assert db.get_referral_stats(10).available_stars == 0
        rejected = db.resolve_referral_withdrawal(wid2, "rejected")
        assert rejected is not None and rejected.status == "rejected"
        assert db.get_referral_stats(10).available_stars == 100
        trials = db.list_active_trial_users()
        trial_list = "".join(
            tr(
                "weekly_trial_line",
                "ru",
                user_id=user_id,
                until=datetime.fromtimestamp(until, tz=timezone.utc).strftime(
                    "%Y-%m-%d %H:%M UTC"
                ),
            )
            for user_id, until in trials
        )
        assert tr(
            "weekly_new_users",
            "ru",
            count=1,
            paid=2,
            trials=len(trials),
            trial_list=trial_list,
        )
        assert "настройках" in tr("broadcast_footer", "ru", type="x")
        assert "Settings" in tr("broadcast_footer", "en", type="x")
        assert tr("broadcast_type_other", "ru") == "📢 Прочие"
        assert tr("broadcast_type_other", "en") == "📢 Other"
        assert tr("schedule_pick_month", "ru")
        assert tr("schedule_pick_day", "en", month="Sep")
        from i18n import schedule_keyboard, schedule_month_keyboard

        sched_cbs = {
            (btn.callback_data or "")
            for row in schedule_keyboard("ru", {"date_page": 0, "date_offset": 0}).inline_keyboard
            for btn in row
        }
        assert "sched:calendar" in sched_cbs
        month_cbs = {
            (btn.callback_data or "")
            for row in schedule_month_keyboard("ru").inline_keyboard
            for btn in row
        }
        assert any(c.startswith("sched:month:") for c in month_cbs)
        assert "sched:time" in month_cbs
        assert "Partner" in tr("btn_partner", "en") or "🤝" in tr("btn_partner", "en")

    status_ok = {
        "status": {"indicator": "none", "description": "All Systems Operational"},
        "components": [
            {"name": "Chat", "status": "operational", "group": False},
            {"name": "Login", "status": "operational", "group": False},
            {"name": "Group", "status": "major_outage", "group": True},
        ],
        "incidents": [],
    }
    status_bad = {
        "status": {"indicator": "major", "description": "Partial System Outage"},
        "components": [
            {"name": "Chat", "status": "partial_outage", "group": False},
            {"name": "Login", "status": "operational", "group": False},
        ],
        "incidents": [
            {"id": "abc", "name": "Chat issues", "status": "investigating"},
        ],
    }
    fp_ok = twitch_status_fingerprint(status_ok)
    fp_bad = twitch_status_fingerprint(status_bad)
    assert fp_ok[0] == "none"
    assert ("Chat", "operational") in fp_ok[1]
    assert all(name != "Group" for name, _ in fp_ok[1])
    assert fp_ok != fp_bad
    assert fp_bad[0] == "major"
    assert ("abc", "investigating") in fp_bad[2]
    msg_ru = _format_twitch_status_message("ru", status_bad)
    assert "Twitch Status" in msg_ru
    assert "Chat" in msg_ru
    status_host = urlparse(TWITCH_STATUS_PAGE_URL).hostname
    assert status_host is not None
    assert any(
        urlparse(href).hostname == status_host
        for href in re.findall(r'href="([^"]+)"', msg_ru)
    )
    msg_ok = _format_twitch_status_message("en", status_ok)
    assert "All Systems Operational" in msg_ok
    assert tr("broadcast_started", "ru")
    assert f"({TWITCH_STATUS_HOST})" in tr("sys_notifications_menu", "ru")

    ph_ok = {
        "overall": "operational",
        "components": [{"id": "us-app", "name": "App", "status": "operational"}],
        "incidents": [],
    }
    ph_bad = {
        "overall": "partial_outage",
        "components": [{"id": "us-app", "name": "App", "status": "partial_outage"}],
        "incidents": [{"id": "us-inc", "name": "App partial outage", "status": "investigating"}],
    }
    ph_msg_ru = _format_posthog_status_message("ru", ph_bad)
    assert "PostHog Status" in ph_msg_ru
    assert "App" in ph_msg_ru
    assert "posthogstatus.com/us" in ph_msg_ru
    ph_msg_ok = _format_posthog_status_message("en", ph_ok)
    assert "All Systems Operational" in ph_msg_ok
    assert "EU Cloud" not in ph_msg_ok

    def _fake_sub(**kwargs):
        base = dict(
            notify_on_category_change=False,
            notify_on_end=False,
            schedule_reminder_minutes=0,
            notify_on_live=True,
        )
        base.update(kwargs)
        return type("Sub", (), base)()

    assert _edit_present_types([_fake_sub()]) == ["live"]
    assert _edit_present_types(
        [
            _fake_sub(notify_on_end=True, notify_on_live=False),
            _fake_sub(notify_on_category_change=True, notify_on_live=False),
            _fake_sub(),
        ]
    ) == ["live", "category", "end"]
    from handlers.subscriptions import _present_types_for_picker

    assert _present_types_for_picker(
        [
            _fake_sub(notify_on_end=True, notify_on_live=False),
            _fake_sub(notify_on_category_change=True, notify_on_live=False),
            _fake_sub(),
        ]
    ) == ["live", "end"]

    import beta as beta_mod
    from premium import ensure_trial_expired, has_feature_sync

    beta_mod._self_check()
    with tempfile.TemporaryDirectory() as beta_tmp:
        manifest = Path(beta_tmp) / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "features": [
                        {
                            "id": "sc_premium_beta",
                            "branch": "feat/sc-premium-beta",
                            "title_key": "beta_feat_sc",
                            "description_key": "beta_feat_sc_desc",
                            "issue_label": "beta/sc-premium-beta",
                            "stage": "beta",
                            "premium_feature_id": "schedule_publish",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        beta_mod.load_manifest(manifest)
        bdb = SqliteDatabase(Path(beta_tmp) / "beta.db")
        bdb.upsert_user(99)
        ensure_trial_expired(bdb, 99)
        assert not has_feature_sync(bdb, 99, "schedule_publish")
        bdb.set_beta_enrollment(99, "sc_premium_beta", True)
        assert has_feature_sync(bdb, 99, "schedule_publish")
        assert beta_mod.enrollment_counts(bdb, 99) == (1, 1)
        beta_mod.load_manifest(beta_mod.manifest_path())

    from i18n import beta_mode_btn, is_menu_button

    pause_ids = {f.id for f in beta_mod.list_features()}
    assert "pause-notifications" not in pause_ids
    pause_feat = beta_mod.get_feature("pause-notifications")
    assert pause_feat is not None and pause_feat.stage == "ga"
    assert btn("pause_notifications", "ru") == "⏸ Приостановить оповещения"
    assert btn("pause_notifications", "en") == "⏸ Pause notifications"
    assert "0 дней" in tr("pause_notifications_prompt", "ru")
    assert "0 days" in tr("pause_notifications_prompt", "en")
    assert is_menu_button(btn("pause_notifications", "ru"))
    with tempfile.TemporaryDirectory() as pause_tmp:
        pdb = SqliteDatabase(Path(pause_tmp) / "pause.db")
        pdb.upsert_user(501)
        assert not _user_notifications_paused(pdb, 501)
        until = int((datetime.now(timezone.utc) + timedelta(days=2)).timestamp())
        pdb.set_notifications_paused_until(501, until)
        assert _user_notifications_paused(pdb, 501)
        pdb.set_notifications_paused_until(501, 0)
        assert not _user_notifications_paused(pdb, 501)
        expired = int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp())
        pdb.set_notifications_paused_until(501, expired)
        assert not _user_notifications_paused(pdb, 501)
    until_label = _format_pause_until(
        int(datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc).timestamp()), "ru"
    )
    assert "2026" in until_label

    assert btn("beta_mode", "ru") == "🧪 Бета-режим"
    assert btn("beta_mode", "en") == "🧪 Beta mode"

    assert beta_mode_btn("ru", 0, 0) == "🧪 Бета-режим (0/0)"
    assert beta_mode_btn("en", 1, 3) == "🧪 Beta mode (1/3)"
    assert is_menu_button(beta_mode_btn("ru", 2, 5))
    assert not is_menu_button("not a menu button")

    assert "stream_chat" in FEATURE_IDS
    assert "stream-chat" not in {f.id for f in beta_mod.list_features()}
    assert "deleted-subscriptions-cart" not in {f.id for f in beta_mod.list_features()}
    sc_feat = beta_mod.get_feature("stream-chat")
    assert sc_feat is not None and sc_feat.premium_feature_id == "stream_chat"
    assert sc_feat.stage == "ga"
    assert "stream_chat" in prem.purchasable_feature_ids()
    assert "deleted_subscriptions_cart" in prem.purchasable_feature_ids()
    assert tr("menu_btn_chat", "ru") == "Чат"
    assert tr("premium_feat_stream_chat", "ru")
    assert tr("beta_feat_stream_chat", "en")
    from chat_webapp import (
        WEBAPP_DIR,
        alert_chat_button_url,
        chat_webapp_url,
        make_webapp_token,
        static_file,
        validate_webapp_init_data,
        validate_webapp_token,
    )
    from premium import CHAT_FREE_DAILY_SEND_LIMIT, chat_daily_send_limit

    assert (WEBAPP_DIR / "index.html").is_file()
    assert static_file("index.html") is not None
    assert static_file("app.js") is not None
    assert '/app/chat/app.js?v=10' in (WEBAPP_DIR / "index.html").read_text(encoding="utf-8")
    assert validate_webapp_init_data("") is None
    assert validate_webapp_init_data("hash=deadbeef") is None
    import asyncio
    from unittest.mock import AsyncMock, patch

    with patch("chat_webapp.PUBLIC_BASE_URL", "https://example.com"), patch(
        "chat_webapp.TELEGRAM_BOT_TOKEN", "123456:TEST_TOKEN_FOR_SELF_CHECK"
    ):
        from handlers.settings import (
            set_default_stream_chat_menu_button,
            sync_all_stream_chat_menu_buttons,
        )

        mock_bot = AsyncMock()
        asyncio.run(set_default_stream_chat_menu_button(mock_bot))
        assert mock_bot.set_chat_menu_button.await_count == 1
        default_kwargs = mock_bot.set_chat_menu_button.await_args.kwargs
        assert "chat_id" not in default_kwargs
        assert default_kwargs["menu_button"].web_app is not None

        class _FakeDb:
            def get_notify_user_ids(self):
                return [11, 12]

            def is_bot_blocked(self, uid):
                return uid == 12

            def get_user_locale(self, uid):
                return "ru"

        with patch(
            "handlers.settings.sync_stream_chat_menu_button",
            new_callable=AsyncMock,
        ) as sync_one:
            asyncio.run(sync_all_stream_chat_menu_buttons(AsyncMock(), _FakeDb()))
            assert sync_one.await_count == 1
            assert sync_one.await_args.args[2] == 11

        assert chat_webapp_url(lang="ru") == "https://example.com/app/chat/?lang=ru"
        assert chat_webapp_url() == "https://example.com/app/chat/"
        tok = make_webapp_token(42)
        assert validate_webapp_token(tok) == 42
        assert validate_webapp_token("1:1:dead") is None
        tok_ru = make_webapp_token(42, lang="ru")
        from chat_webapp import parse_webapp_token

        uid, loc = parse_webapp_token(tok_ru)
        assert uid == 42 and loc == "ru"
        url = chat_webapp_url(lang="ru", user_id=42)
        assert "lang=ru" in url and "t=" in url
        alert_url = alert_chat_button_url(login="SomeStreamer", lang="ru", user_id=42)
        assert "login=somestreamer" in alert_url
        assert "open=1" in alert_url and "t=" in alert_url
        from types import SimpleNamespace

        from bot import _alert_chat_button_markup

        dm_markup = _alert_chat_button_markup(
            SimpleNamespace(
                attach_chat_button=True,
                dest_type="dm",
                twitch_username="SomeStreamer",
                owner_id=42,
            ),
            "ru",
        )
        assert dm_markup is not None
        dm_btn = dm_markup.inline_keyboard[0][0]
        assert dm_btn.web_app is not None and dm_btn.url is None
        group_markup = _alert_chat_button_markup(
            SimpleNamespace(
                attach_chat_button=True,
                dest_type="group",
                twitch_username="SomeStreamer",
                owner_id=42,
            ),
            "ru",
        )
        assert group_markup is not None
        group_btn = group_markup.inline_keyboard[0][0]
        assert group_btn.url and group_btn.web_app is None
        from chat_webapp import stream_chat_open_markup

        priv_chat_kb = stream_chat_open_markup(
            "ru", "https://example.com/app/chat/", private=True
        )
        group_chat_kb = stream_chat_open_markup(
            "ru", "https://example.com/app/chat/", private=False
        )
        priv_chat_btn = priv_chat_kb.inline_keyboard[0][0]
        group_chat_btn = group_chat_kb.inline_keyboard[0][0]
        assert priv_chat_btn.web_app is not None and priv_chat_btn.url is None
        assert group_chat_btn.url and group_chat_btn.web_app is None
        assert TwitchClient._about_link_key("https://VK.com/stopgameru/") == "https://vk.com/stopgameru"
    with tempfile.TemporaryDirectory() as chat_tmp:
        cdb = SqliteDatabase(Path(chat_tmp) / "chat.db")
        cdb.upsert_user(777)
        ensure_trial_expired(cdb, 777)
        assert chat_daily_send_limit(cdb, 777) == CHAT_FREE_DAILY_SEND_LIMIT
        assert cdb.get_chat_send_count(777, "2026-08-21") == 0
        assert cdb.increment_chat_send_count(777, "2026-08-21") == 1
        assert cdb.increment_chat_send_count(777, "2026-08-21") == 2
        cdb.upsert_chat_auth(
            777,
            twitch_user_id="1",
            twitch_login="viewer",
            refresh_token="refresh-token-value",
        )
        auth = cdb.get_chat_auth(777)
        assert auth is not None
        assert auth.twitch_login == "viewer"
        assert auth.refresh_token == "refresh-token-value"
        cdb.set_beta_enrollment(777, "stream-chat", True)
        # GA: beta enrollment no longer grants unlimited chat.
        assert chat_daily_send_limit(cdb, 777) == CHAT_FREE_DAILY_SEND_LIMIT
        import time as _t

        cdb.upsert_user(778)
        ensure_trial_expired(cdb, 778)
        assert chat_daily_send_limit(cdb, 778) == CHAT_FREE_DAILY_SEND_LIMIT
        cdb.extend_premium_features(
            778, ["stream_chat"], until_unix=int(_t.time()) + 3600, charge_id="chat1"
        )
        assert chat_daily_send_limit(cdb, 778) is None

        import chat_webapp as cw

        cdb.upsert_user(779)
        ensure_trial_expired(cdb, 779)
        assert chat_daily_send_limit(cdb, 779) == CHAT_FREE_DAILY_SEND_LIMIT
        cdb.upsert_premium_channel(
            twitch_user_id="200",
            twitch_login="paidstreamer",
            display_name="Paid Streamer",
            owner_telegram_id=1,
            charge_id="pc_test",
        )
        cdb.add_subscription(
            owner_id=779,
            twitch_username="marfapr",
            twitch_user_id="100",
            message_template="x",
            dest_type="dm",
            chat_id=779,
            thread_id=None,
            enabled=True,
        )

        class _FakeTwitchOther:
            def get_users_by_login(self, logins):
                ids = {"marfapr": "100", "paidstreamer": "200"}
                return {
                    login: {"id": ids[login], "login": login, "profile_image_url": ""}
                    for login in logins
                    if login in ids
                }

            def get_live_streams(self, uids):
                names = {"100": "marfapr", "200": "paidstreamer"}
                out = {}
                for uid in uids:
                    login = names.get(str(uid), f"u{uid}")
                    out[str(uid)] = {
                        "user_id": str(uid),
                        "user_login": login,
                        "user_name": login,
                        "title": "live",
                        "game_name": "Game",
                        "viewer_count": 42,
                    }
                return out

        fake = _FakeTwitchOther()
        with patch.object(cw, "_db", cdb), patch.object(cw, "_twitch", fake):
            other = cw._other_promo_streams(779, sub_logins={"marfapr"})
        assert len(other) == 1
        assert other[0]["login"] == "paidstreamer"
        cdb.extend_premium_features(
            779, ["stream_chat"], until_unix=int(_t.time()) + 3600, charge_id="chat2"
        )
        with patch.object(cw, "_db", cdb), patch.object(cw, "_twitch", fake):
            assert cw._other_promo_streams(779, sub_logins=set()) == []

