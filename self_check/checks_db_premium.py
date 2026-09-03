"""DB/premium self-checks: subscriptions, gates, cart, sync, billing helpers."""
import json
from pathlib import Path
import os
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



def check_db_premium() -> None:
    CHANNEL = "marfapr"
    import os as _os
    from hf_text import _local_template, generate_alert_template

    _prev = {
        k: _os.environ.get(k)
        for k in ("HF_TOKEN", "HUGGING_FACE_API", "GROQ_API_KEY", "GROQ_API", "GROK_API")
    }
    with tempfile.TemporaryDirectory() as tmp:
        db = SqliteDatabase(Path(tmp) / "test.db")
        assert not db.user_exists(1)
        db.upsert_user(1)
        assert db.user_exists(1)
        assert db.get_user_locale(1) is None
        db.set_user_locale(1, "en")
        assert db.get_user_locale(1) == "en"
        assert db.get_user_locales([1, 2]) == {1: "en", 2: None}
        assert db.get_user_locales([]) == {}
        prefs = WatchPrefs(
            categories=[{"id": "509658", "name": "Just Chatting"}],
            min_viewers=50,
            max_viewers=500,
            language="en",
            tags=["English"],
            exclude_mature=True,
        )
        db.add_watch_filter(1, prefs)
        db.add_watch_filter(
            1,
            WatchPrefs(
                categories=[{"id": "2", "name": "Dota 2"}],
                exclude_mature=True,
            ),
        )
        filters = db.get_watch_filters(1)
        assert len(filters) == 2
        assert db.get_watch_prefs(1) == filters[0].prefs
        assert db.delete_watch_filter(1, filters[0].id)
        assert len(db.get_watch_filters(1)) == 1
        db.clear_watch_prefs(1)
        assert db.get_watch_filters(1) == []
        sample = "{username} live!\n{name}\n{game}"
        db.add_lucky_template("ru", sample)
        with db._conn() as conn:
            texts = {
                str(r["text"])
                for r in conn.execute(
                    "SELECT text FROM lucky_templates WHERE locale = 'ru'"
                ).fetchall()
            }
            ru_n = conn.execute(
                "SELECT COUNT(*) AS c FROM lucky_templates WHERE locale = 'ru'"
            ).fetchone()["c"]
            en_n = conn.execute(
                "SELECT COUNT(*) AS c FROM lucky_templates WHERE locale = 'en'"
            ).fetchone()["c"]
        assert sample in texts
        assert db.pick_lucky_template("en") is not None  # seeded on migrate
        assert en_n == 100
        assert ru_n == 100  # seeded full; add_lucky_template trims to 100
        # Extra inserts still capped at 100.
        for i in range(105):
            db.add_lucky_template("ru", f"{{username}}\n{{name}}\n{{game}}\n#{i}")
        with db._conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM lucky_templates WHERE locale = 'ru'"
            ).fetchone()["c"]
        assert count == 100
        from_store = _local_template("ru", db)
        assert "{username}" in from_store
        # Empty cloud tokens → pick from store, not seed.
        for k in _prev:
            _os.environ[k] = ""
        try:
            via_store = generate_alert_template(locale="ru", channel="x", store=db)
            assert "{username}" in via_store
        finally:
            for k, v in _prev.items():
                if v is None:
                    _os.environ.pop(k, None)
                else:
                    _os.environ[k] = v
        sub_id = db.add_subscription(
            owner_id=1,
            twitch_username=CHANNEL,
            twitch_user_id="123",
            message_template="hi",
            dest_type="dm",
            chat_id=1,
            thread_id=None,
        )
        stats = db.get_bot_stats()
        assert stats.users == 1
        assert stats.subscriptions_total == 1
        assert stats.subscriptions_enabled == 1
        assert stats.premium_paid == 0
        assert stats.sys_updates == 1
        assert stats.sys_other == 1
        db.set_premium_stars(1, charge_id="c", until_unix=10**12, canceled=False)
        assert db.get_bot_stats().premium_paid == 1
        import premium as prem

        paused_id = db.add_subscription(
            owner_id=1,
            twitch_username="other",
            twitch_user_id="999",
            message_template=tr("import_default_template", "ru"),
            dest_type="dm",
            chat_id=1,
            thread_id=None,
            enabled=False,
        )
        paused = db.get_subscription(paused_id, 1)
        assert paused is not None and paused.enabled is False
        assert "{username}" in paused.message_template
        assert "{game}" in paused.message_template
        assert "https://twitch.tv/{username}" in paused.message_template
        imported, skipped, limited, removed, new_subs, ask0 = import_followed_as_subscriptions(
            db,
            1,
            [
                {"broadcaster_id": "123", "broadcaster_login": CHANNEL},
                {"broadcaster_id": "555", "broadcaster_login": "newbie"},
                {"broadcaster_id": "555", "broadcaster_login": "newbie"},
            ],
            template=tr("import_default_template", "en"),
            limit=25,
        )
        assert imported == 1 and skipped == 1 and limited == 0 and removed == []
        assert ask0 == []
        assert len(new_subs) == 1
        assert new_subs[0].twitch_username == "newbie"
        assert new_subs[0].from_twitch_sync is True
        assert new_subs[0].enabled is False  # import stays paused
        assert new_subs[0].disable_link_preview is True
        assert paused.from_twitch_sync is False
        # Sync path: new follows are enabled
        imported_sync, _, _, _, sync_subs, ask1 = import_followed_as_subscriptions(
            db,
            1,
            [
                {"broadcaster_id": "123", "broadcaster_login": CHANNEL},
                {"broadcaster_id": "777", "broadcaster_login": "synced"},
            ],
            template=tr("import_default_template", "en"),
            limit=25,
            enabled=True,
        )
        assert imported_sync == 1 and len(sync_subs) == 1
        assert ask1 == []
        assert sync_subs[0].twitch_username == "synced"
        assert sync_subs[0].enabled is True
        assert sync_subs[0].from_twitch_sync is True
        assert sync_subs[0].disable_link_preview is True
        # Prune: remove sync-origin "newbie" when follows only keep CHANNEL
        imported2, skipped2, limited2, removed2, _, ask2 = import_followed_as_subscriptions(
            db,
            1,
            [{"broadcaster_id": "123", "broadcaster_login": CHANNEL}],
            template=tr("import_default_template", "en"),
            limit=25,
            prune_missing=True,
        )
        assert imported2 == 0 and skipped2 == 1 and len(removed2) == 2  # newbie + synced
        assert set(removed2) == {"newbie", "synced"}
        # Manual paused "other" (999) is not in follows → ask before delete
        assert any(s["user_id"] == "999" for s in ask2)
        assert db.get_subscription(new_subs[0].id, 1) is None
        assert db.get_subscription(sync_subs[0].id, 1) is None
        assert db.get_subscription(paused_id, 1) is not None  # manual kept until user confirms
        deleted_manual = db.delete_subscriptions_for_twitch_users(1, {"999"})
        assert deleted_manual == 1
        assert db.get_subscription(paused_id, 1) is None
        # Edited sync: prune keeps it and asks
        edited_id = db.add_subscription(
            owner_id=1,
            twitch_username="edited",
            twitch_user_id="666",
            message_template=tr("import_default_template", "en"),
            dest_type="dm",
            chat_id=1,
            thread_id=None,
            enabled=True,
            from_twitch_sync=True,
        )
        assert db.update_subscription(edited_id, 1, message_template="custom {username}")
        edited = db.get_subscription(edited_id, 1)
        assert edited is not None and edited.sync_user_edited is True
        _, _, _, removed_edit, _, ask_edit = import_followed_as_subscriptions(
            db,
            1,
            [{"broadcaster_id": "123", "broadcaster_login": CHANNEL}],
            template=tr("import_default_template", "en"),
            limit=25,
            prune_missing=True,
        )
        assert removed_edit == []
        assert any(s["user_id"] == "666" for s in ask_edit)
        assert db.get_subscription(edited_id, 1) is not None
        db.delete_subscriptions_for_twitch_users(1, {"666"})
        assert db.get_subscription(edited_id, 1) is None
        db.upsert_twitch_sync(
            owner_id=1,
            twitch_user_id="sync-self",
            refresh_token="rtok-self",
            period_days=7,
            next_sync_at="2030-01-01T00:00:00+00:00",
        )
        self_alert_id = db.add_subscription(
            owner_id=1,
            twitch_username="marfapr",
            twitch_user_id="sync-self",
            message_template=tr("import_default_template", "en"),
            dest_type="dm",
            chat_id=1,
            thread_id=None,
            enabled=True,
        )
        _, _, _, _, _, ask_self = import_followed_as_subscriptions(
            db,
            1,
            [{"broadcaster_id": "123", "broadcaster_login": CHANNEL}],
            template=tr("import_default_template", "en"),
            limit=25,
            prune_missing=True,
        )
        assert not any(s["user_id"] == "sync-self" for s in ask_self)
        assert db.get_subscription(self_alert_id, 1) is not None
        db.delete_subscriptions_for_twitch_users(1, {"sync-self"})
        db.delete_twitch_sync(1)
        # Restore a manual paused row for later assertions that expect paused_id
        paused_id = db.add_subscription(
            owner_id=1,
            twitch_username="other",
            twitch_user_id="999",
            message_template=tr("import_default_template", "ru"),
            dest_type="dm",
            chat_id=1,
            thread_id=None,
            enabled=False,
        )
        db.set_user_locale(1, "ru")
        legacy_id = db.add_subscription(
            owner_id=1,
            twitch_username="legacy",
            twitch_user_id="888",
            message_template="Стример {username} вышел в эфир с игрой {game}",
            dest_type="dm",
            chat_id=1,
            thread_id=None,
            enabled=True,
            from_twitch_sync=True,
        )
        preview_only_id = db.add_subscription(
            owner_id=1,
            twitch_username="preview",
            twitch_user_id="889",
            message_template=tr("import_default_template", "en"),
            dest_type="dm",
            chat_id=1,
            thread_id=None,
            enabled=True,
            from_twitch_sync=True,
        )
        templates, previews = migrate_import_sync_subscriptions(db)
        assert templates == 1 and previews == 2
        legacy = db.get_subscription(legacy_id, 1)
        preview_only = db.get_subscription(preview_only_id, 1)
        assert legacy is not None and preview_only is not None
        assert legacy.message_template == tr("import_default_template", "ru")
        assert legacy.disable_link_preview is True
        assert preview_only.message_template == tr("import_default_template", "en")
        assert preview_only.disable_link_preview is True
        dry_templates, dry_previews = migrate_import_sync_subscriptions(db, dry_run=True)
        assert dry_templates == 0 and dry_previews == 0
        assert db.enable_all_subscriptions(1) >= 1
        assert db.get_subscription(paused_id, 1).enabled is True
        cap_owner = 88001
        for login in ("c1", "c2", "c3"):
            db.add_subscription(
                owner_id=cap_owner,
                twitch_username=login,
                twitch_user_id=login,
                message_template="t",
                dest_type="dm",
                chat_id=cap_owner,
                thread_id=None,
                enabled=False,
            )
        assert db.count_enabled_subscriptions(cap_owner) == 0
        assert db.enable_all_subscriptions(cap_owner, max_count=2) == 2
        assert db.count_enabled_subscriptions(cap_owner) == 2
        assert db.enable_all_subscriptions(cap_owner, max_count=10) == 1
        assert db.count_enabled_subscriptions(cap_owner) == 3
        db.delete_subscriptions_for_twitch_users(cap_owner, {"c1", "c2", "c3"})
        assert db.get_twitch_sync(1) is None
        db.upsert_twitch_sync(
            owner_id=1,
            twitch_user_id="tw1",
            refresh_token="rtok",
            period_days=7,
            next_sync_at="2020-01-01T00:00:00+00:00",
        )
        # Ciphertext at rest
        with db._conn() as conn:
            raw = conn.execute(
                "SELECT refresh_token FROM twitch_sync WHERE owner_id = 1"
            ).fetchone()["refresh_token"]
        assert str(raw).startswith("enc:v1:")
        assert "rtok" not in str(raw)
        sync = db.get_twitch_sync(1)
        assert sync is not None
        assert sync.period_days == 7
        assert sync.refresh_token == "rtok"
        due = db.get_due_twitch_syncs("2020-01-02T00:00:00+00:00")
        assert len(due) == 1 and due[0].owner_id == 1
        assert due[0].refresh_token == "rtok"
        assert db.set_twitch_sync_period(1, 14, "2030-01-01T00:00:00+00:00")
        assert db.get_twitch_sync(1).period_days == 14
        assert db.get_due_twitch_syncs("2020-01-02T00:00:00+00:00") == []
        db.update_twitch_sync_tokens(
            1,
            "rtok2",
            last_sync_at="2026-01-01T00:00:00+00:00",
            next_sync_at="2030-06-01T00:00:00+00:00",
        )
        assert db.get_twitch_sync(1).refresh_token == "rtok2"
        assert db.delete_twitch_sync(1) is True
        assert db.get_twitch_sync(1) is None
        db.upsert_whisper_alert(
            11,
            enabled=True,
            twitch_user_id="tw-w",
            twitch_login="bob",
            refresh_token="wtok",
            eventsub_id="es-1",
        )
        with db._conn() as conn:
            wraw = conn.execute(
                "SELECT refresh_token FROM whisper_alerts WHERE owner_id = 11"
            ).fetchone()["refresh_token"]
        assert str(wraw).startswith("enc:v1:")
        assert "wtok" not in str(wraw)
        walert = db.get_whisper_alert(11)
        assert walert is not None
        assert walert.enabled is True
        assert walert.refresh_token == "wtok"
        assert walert.twitch_login == "bob"
        found = db.get_whisper_alerts_by_twitch_user_id("tw-w")
        assert len(found) == 1 and found[0].owner_id == 11
        db.set_whisper_alert_enabled(11, False, eventsub_id="")
        assert db.get_whisper_alert(11).enabled is False
        db.upsert_whisper_alert(
            12,
            enabled=True,
            twitch_user_id="tw-w",
            twitch_login="bob",
            refresh_token="wtok2",
            eventsub_id="es-2",
        )
        disabled = db.disable_whisper_alerts_for_twitch_user("tw-w")
        assert 12 in disabled
        assert db.get_whisper_alerts_by_twitch_user_id("tw-w") == []
        from token_crypto import encrypt_secret, decrypt_secret

        assert decrypt_secret(encrypt_secret("secret-token")) == "secret-token"
        assert stats.sys_availability == 1
        assert stats.blocked_users == 0
        assert db.update_subscription(sub_id, 1, message_template="bye")
        sub = db.get_subscription(sub_id, 1)
        assert sub is not None
        assert sub.message_template == "bye"
        assert sub.delay_minutes == 0
        assert db.update_subscription(sub_id, 1, delay_minutes=10)
        sub = db.get_subscription(sub_id, 1)
        assert sub is not None
        assert sub.delay_minutes == 10
        assert db.update_subscription(sub_id, 1, suppress_repeat_minutes=30)
        sub = db.get_subscription(sub_id, 1)
        assert sub is not None
        assert sub.suppress_repeat_minutes == 30
        assert sub.schedule_reminder_minutes == 0
        assert sub.schedule_reminder_configured is False
        assert db.update_subscription(sub_id, 1, schedule_reminder_minutes=15)
        sub = db.get_subscription(sub_id, 1)
        assert sub is not None
        assert sub.schedule_reminder_minutes == 15
        assert sub.schedule_reminder_configured is False
        assert db.update_subscription(
            sub_id, 1, schedule_reminder_configured=True, schedule_reminder_minutes=15
        )
        sub = db.get_subscription(sub_id, 1)
        assert sub is not None
        assert sub.schedule_reminder_configured is True
        assert db.update_subscription(sub_id, 1, schedule_reminder_minutes=0)
        sub = db.get_subscription(sub_id, 1)
        assert sub is not None
        assert sub.schedule_reminder_minutes == 0
        assert sub.schedule_reminder_configured is True
        assert sub.last_schedule_reminder_segment_id is None
        db.set_last_schedule_reminder_segment(sub_id, "seg-abc")
        sub = db.get_subscription(sub_id, 1)
        assert sub is not None
        assert sub.last_schedule_reminder_segment_id == "seg-abc"
        assert db.get_unique_schedule_reminder_twitch_ids() == []
        assert db.update_subscription(sub_id, 1, schedule_reminder_minutes=10)
        assert db.get_unique_schedule_reminder_twitch_ids() == [sub.twitch_user_id]
        assert sub.notify_on_live is True
        assert sub.notify_on_end is False
        assert getattr(sub, "from_watch_suggest", False) is False
        cw_prefs = WatchPrefs(
            categories=[{"id": "509658", "name": "Just Chatting"}],
            min_viewers=0,
            max_viewers=None,
            language=None,
            tags=[],
            exclude_mature=True,
        )
        cw_raw = dump_category_watch_prefs(cw_prefs)
        assert parse_category_watch_prefs(cw_raw) == cw_prefs
        watch_sid = db.add_subscription(
            owner_id=1,
            twitch_username=watch_filter_auto_name(cw_prefs),
            twitch_user_id="cw:1:abcd",
            message_template="hi",
            dest_type="dm",
            chat_id=1,
            thread_id=None,
            from_watch_suggest=True,
            category_watch_prefs=cw_raw,
            notify_on_live=True,
        )
        watch_sub = db.get_subscription(watch_sid, 1)
        assert watch_sub is not None
        assert watch_sub.from_watch_suggest is True
        assert is_category_watch_sub(watch_sub)
        assert "cw:1:abcd" not in db.get_unique_twitch_user_ids()
        assert any(s.id == watch_sid for s in db.get_enabled_category_watch_subscriptions())
        db.set_category_watch_live_state(watch_sid, ["9"], primed=True)
        assert db.get_subscription(watch_sid, 1).category_watch_primed is True
        assert db.delete_subscription(watch_sid, 1)
        assert db.update_subscription(sub_id, 1, notify_on_live=False)
        sub = db.get_subscription(sub_id, 1)
        assert sub is not None
        assert sub.notify_on_live is False
        assert sub.twitch_user_id not in db.get_unique_twitch_user_ids()
        assert db.update_subscription(sub_id, 1, notify_on_end=True)
        sub = db.get_subscription(sub_id, 1)
        assert sub is not None
        assert sub.notify_on_end is True
        assert sub.twitch_user_id in db.get_unique_twitch_user_ids()
        assert db.update_subscription(sub_id, 1, notify_on_end=False, notify_on_live=True)
        assert sub.twitch_user_id in db.get_unique_twitch_user_ids()
        end_id = db.add_subscription(
            owner_id=1,
            twitch_username="ender",
            twitch_user_id="end-uid",
            message_template="ended {username}",
            dest_type="dm",
            chat_id=1,
            thread_id=None,
            notify_on_live=False,
            notify_on_end=True,
        )
        end_sub = db.get_subscription(end_id, 1)
        assert end_sub is not None
        assert end_sub.notify_on_end is True
        assert end_sub.notify_on_live is False
        assert "end-uid" in db.get_unique_twitch_user_ids()
        cat_id = db.add_subscription(
            owner_id=1,
            twitch_username="changer",
            twitch_user_id="cat-uid",
            message_template="now {game}",
            dest_type="channel",
            chat_id=-100,
            thread_id=None,
            notify_on_live=False,
            notify_on_end=False,
            notify_on_category_change=True,
            delete_other_alerts=True,
        )
        cat_sub = db.get_subscription(cat_id, 1)
        assert cat_sub is not None
        assert cat_sub.notify_on_category_change is True
        assert cat_sub.notify_on_live is False
        assert cat_sub.delete_other_alerts is True
        assert "cat-uid" in db.get_unique_twitch_user_ids()
        assert db.update_subscription(cat_id, 1, notify_on_category_change=False)
        cat_sub = db.get_subscription(cat_id, 1)
        assert cat_sub is not None
        assert cat_sub.notify_on_category_change is False
        assert "cat-uid" not in db.get_unique_twitch_user_ids()
        demo_id = db.add_subscription(
            owner_id=1,
            twitch_username="demochan",
            twitch_user_id="demo-uid",
            message_template="demo {username}",
            dest_type="dm",
            chat_id=1,
            thread_id=None,
            notify_on_live=True,
            is_demo=True,
        )
        demo_sub = db.get_subscription(demo_id, 1)
        assert demo_sub is not None
        assert demo_sub.is_demo is True
        assert db.count_enabled_subscriptions(1, demo=True) >= 1
        assert db.delete_demo_subscriptions(1) >= 1
        assert db.get_subscription(demo_id, 1) is None
        import demo_mode as dm

        dm.activate(1)
        from premium import can_enable_more

        assert can_enable_more(db, 1) is True
        dm.deactivate(1)
        assert sub.notify_delete_fail is False
        assert db.update_subscription(sub_id, 1, notify_delete_fail=True)
        sub = db.get_subscription(sub_id, 1)
        assert sub is not None
        assert sub.notify_delete_fail is True
        assert sub.ignore_keywords == ""
        assert sub.use_global_ignore is False
        assert db.update_subscription(sub_id, 1, ignore_keywords="foo, bar")
        sub = db.get_subscription(sub_id, 1)
        assert sub is not None
        assert sub.ignore_keywords == "foo, bar"
        assert db.update_subscription(sub_id, 1, use_global_ignore=True)
        sub = db.get_subscription(sub_id, 1)
        assert sub is not None
        assert sub.use_global_ignore is True
        assert db.update_subscription(sub_id, 1, use_global_ignore=False)
        sub = db.get_subscription(sub_id, 1)
        assert sub is not None
        assert sub.use_global_ignore is False
        assert db.get_global_ignore_keywords(1) == ""
        db.set_global_ignore_keywords(1, "irl, chatting")
        assert db.get_global_ignore_keywords(1) == "irl, chatting"
        db.set_global_ignore_keywords(1, "")
        assert db.get_global_ignore_keywords(1) == ""
        assert db.mark_template_typo_notice_sent(1) is True
        assert db.mark_template_typo_notice_sent(1) is False
        assert sub.image_file_id is None
        assert sub.image_position == ""
        assert db.update_subscription(
            sub_id, 1, image_file_id="AgAC_test_file", image_position="before"
        )
        sub = db.get_subscription(sub_id, 1)
        assert sub is not None
        assert sub.image_file_id == "AgAC_test_file"
        assert sub.image_position == "before"
        assert db.update_subscription(sub_id, 1, image_file_id=None, image_position="")
        sub = db.get_subscription(sub_id, 1)
        assert sub is not None
        assert sub.image_file_id is None
        assert sub.image_position == ""
        assert db.count_new_users_since(datetime.now(timezone.utc) - timedelta(days=1)) == 1
        assert db.count_new_users_since(datetime.now(timezone.utc) + timedelta(days=1)) == 0
        db.set_notify_cooldown(sub_id, 5)
        sub = db.get_subscription(sub_id, 1)
        assert sub is not None
        assert sub.notify_cooldown_until is not None
        from db import is_on_notify_cooldown

        assert is_on_notify_cooldown(sub)
        assert db.get_receive_bot_updates(1) is True
        db.set_receive_bot_updates(1, False)
        assert db.get_receive_bot_updates(1) is False
        assert 1 not in db.get_bot_update_recipients()
        assert db.get_receive_availability_updates(1) is True
        db.set_receive_availability_updates(1, False)
        assert db.get_receive_availability_updates(1) is False
        assert 1 not in db.get_availability_recipients()
        assert db.get_receive_other_updates(1) is True
        db.set_receive_other_updates(1, False)
        assert db.get_receive_other_updates(1) is False
        assert 1 not in db.get_other_recipients()
        assert db.get_receive_sync_updates(1) is True
        db.set_receive_sync_updates(1, False)
        assert db.get_receive_sync_updates(1) is False
        db.set_receive_sync_updates(1, True)
        db.set_receive_bot_updates(1, True)
        db.set_receive_availability_updates(1, True)
        db.set_receive_other_updates(1, True)
        assert db.get_notifications_paused_until(1) == 0
        pause_until = int(
            (datetime.now(timezone.utc) + timedelta(days=3)).timestamp()
        )
        db.set_notifications_paused_until(1, pause_until)
        assert db.get_notifications_paused_until(1) == pause_until
        db.set_notifications_paused_until(1, 0)
        assert db.get_notifications_paused_until(1) == 0
        db.set_bot_blocked(1, True)
        assert db.is_bot_blocked(1) is True
        assert 1 not in db.get_bot_update_recipients()
        assert 1 not in db.get_availability_recipients()
        assert 1 not in db.get_other_recipients()
        blocked_stats = db.get_bot_stats()
        assert blocked_stats.blocked_users == 1
        assert blocked_stats.users == 0
        assert blocked_stats.notify_users == 0
        assert blocked_stats.subscriptions_total == 0
        assert blocked_stats.subscriptions_enabled == 0
        assert blocked_stats.unique_owners == 0
        assert blocked_stats.unique_twitch_channels == 0
        assert blocked_stats.sys_updates == 0
        assert blocked_stats.sys_availability == 0
        assert blocked_stats.sys_other == 0
        assert db.is_chat_unreachable(-1001980871389) is False
        db.set_chat_unreachable(-1001980871389, True)
        assert db.is_chat_unreachable(-1001980871389) is True
        db.set_chat_unreachable(-1001980871389, False)
        assert db.is_chat_unreachable(-1001980871389) is False
        ch = -100555
        sid_a = db.add_subscription(
            1, "a", "a", "t", "channel", ch, None, enabled=True
        )
        sid_b = db.add_subscription(
            1, "b", "b", "t", "channel", ch, None, enabled=True
        )
        assert db.pause_delivery_for_chat(ch) == 2
        assert db.get_subscription(sid_a, 1).enabled is False
        assert db.get_subscription(sid_a, 1).delivery_paused is True
        assert db.get_subscription(sid_b, 1).delivery_paused is True
        db.clear_delivery_paused(sid_a, enabled=True)
        assert db.get_subscription(sid_a, 1).enabled is True
        assert db.get_subscription(sid_a, 1).delivery_paused is False
        db.clear_delivery_paused(sid_b, enabled=False)
        assert db.get_subscription(sid_b, 1).enabled is False
        assert db.get_subscription(sid_b, 1).delivery_paused is False
        db.upsert_user(1)
        assert db.is_bot_blocked(1) is False
        restored = db.get_bot_stats()
        assert restored.users == 1
        assert restored.subscriptions_total == 8
        assert restored.blocked_users == 0
        bid = db.add_scheduled_broadcast(
            "bot_update", "hello", "2099-01-01T00:00:00+00:00", 1
        )
        unsent = db.get_unsent_scheduled_broadcasts()
        assert any(b.id == bid for b in unsent)
        item = db.get_scheduled_broadcast(bid)
        assert item is not None
        assert item.text == "hello"
        assert item.recipient_ids == ""
        bid_ids = db.add_scheduled_broadcast(
            "other", "hi", "2099-01-01T00:00:00+00:00", 1, recipient_ids="11,22"
        )
        item_ids = db.get_scheduled_broadcast(bid_ids)
        assert item_ids is not None and item_ids.recipient_ids == "11,22"
        assert _parse_broadcast_recipient_ids("11, 22, x, 22") == [11, 22]
        assert db.delete_scheduled_broadcast(bid_ids)
        assert db.update_scheduled_broadcast(bid, text="updated")
        item = db.get_scheduled_broadcast(bid)
        assert item is not None
        assert item.text == "updated"
        assert db.delete_scheduled_broadcast(bid)
        assert db.get_scheduled_broadcast(bid) is None
        bid2 = db.add_scheduled_broadcast(
            "bot_update", "bye", "2099-01-01T00:00:00+00:00", 1
        )
        db.mark_scheduled_broadcast_sent(bid2)
        assert db.get_scheduled_broadcast(bid2) is None
        sent_items = db.get_sent_broadcasts(retention_days=30)
        assert any(b.id == bid2 for b in sent_items)
        up, down = db.get_broadcast_feedback_counts(bid2)
        assert up == 0 and down == 0
        db.set_broadcast_feedback(bid2, 1, 1)
        db.set_broadcast_feedback(bid2, 2, -1)
        assert db.get_broadcast_feedback_vote(bid2, 1) == 1
        assert db.get_broadcast_feedback_counts(bid2) == (1, 1)
        db.clear_broadcast_feedback(bid2, 1)
        assert db.get_broadcast_feedback_vote(bid2, 1) is None
        assert db.get_broadcast_feedback_counts(bid2) == (0, 1)
        db.add_broadcast_delivery(bid2, 1, 100)
        assert db.get_broadcast_deliveries(bid2) == [(1, 100)]
        with db._conn() as conn:
            left = conn.execute(
                "SELECT COUNT(*) AS c FROM scheduled_broadcasts WHERE id = ?",
                (bid2,),
            ).fetchone()["c"]
        assert left == 1

        from datetime import date
        from handlers.broadcast import (
            _broadcast_waves,
            _default_broadcast_utc_offset,
            _schedule_wall_clock,
            _utc_due_for_offset,
        )

        assert _default_broadcast_utc_offset() == 180
        day, hour, minute = _schedule_wall_clock("2026-08-26T09:00:00+00:00")
        assert day == date(2026, 8, 26) and hour == 12 and minute == 0
        due_msk = _utc_due_for_offset(day, hour, minute, 180)
        due_ny = _utc_due_for_offset(day, hour, minute, -300)
        assert due_ny > due_msk

        db.upsert_user(701)
        db.upsert_user(702)
        db.set_schedule_utc_offset_minutes(702, -300)
        bid_tz = db.add_scheduled_broadcast(
            "other", "tz", "2026-08-26T09:00:00+00:00", 1, recipient_ids="701,702"
        )
        item_tz = db.get_scheduled_broadcast(bid_tz)
        assert item_tz is not None
        waves = _broadcast_waves(db, item_tz)
        assert len(waves) == 2
        offsets = {off for off, _ in waves}
        assert offsets == {180, -300}
        db.record_broadcast_offset_sent(bid_tz, 180, 1)
        assert 180 in db.get_broadcast_sent_offsets(bid_tz)
        assert db.get_broadcast_sent_count(bid_tz) == 1
        db.record_broadcast_offset_sent(bid_tz, -300, 1)
        assert offsets.issubset(db.get_broadcast_sent_offsets(bid_tz))
        assert db.get_broadcast_sent_count(bid_tz) == 2
        db.reset_broadcast_send_progress(bid_tz)
        assert db.get_broadcast_sent_offsets(bid_tz) == set()
        assert db.get_broadcast_sent_count(bid_tz) == 0
        assert db.delete_scheduled_broadcast(bid_tz)
        assert not db.update_subscription(999, 1, message_template="x")

    import premium as prem

    with tempfile.TemporaryDirectory() as gate_tmp:
        gate_db = SqliteDatabase(Path(gate_tmp) / "gate.db")
        free_sync_owner = 88006
        gate_db.upsert_user(free_sync_owner)
        prem.ensure_trial_expired(gate_db, free_sync_owner)
        plimit = prem.free_active_limit()
        for i in range(plimit):
            gate_db.add_subscription(
                owner_id=free_sync_owner,
                twitch_username=f"on{i}",
                twitch_user_id=f"on{i}",
                message_template="t",
                dest_type="dm",
                chat_id=free_sync_owner,
                thread_id=None,
                enabled=True,
            )
        _, _, _, _, capped_subs, _ = import_followed_as_subscriptions(
            gate_db,
            free_sync_owner,
            [{"broadcaster_id": "9001", "broadcaster_login": "over"}],
            template="t",
            limit=25,
            enabled=True,
        )
        assert len(capped_subs) == 1 and capped_subs[0].enabled is False

        # Delivery pause frees active slots; resume respects free active cap.
        from handlers.delivery import apply_chat_unreachable, clear_chat_unreachable

        pause_owner = 88016
        pause_chat = -10088016
        gate_db.upsert_user(pause_owner)
        prem.ensure_trial_expired(gate_db, pause_owner)
        pause_ids: list[int] = []
        for i in range(plimit):
            pause_ids.append(
                gate_db.add_subscription(
                    owner_id=pause_owner,
                    twitch_username=f"dp{i}",
                    twitch_user_id=f"dp{i}",
                    message_template="t",
                    dest_type="channel",
                    chat_id=pause_chat,
                    thread_id=None,
                    enabled=True,
                )
            )
        assert apply_chat_unreachable(gate_db, pause_chat) == plimit
        assert gate_db.count_enabled_subscriptions(pause_owner) == 0
        # Fill free slots while delivery-paused.
        filler = gate_db.add_subscription(
            owner_id=pause_owner,
            twitch_username="filler",
            twitch_user_id="filler",
            message_template="t",
            dest_type="dm",
            chat_id=pause_owner,
            thread_id=None,
            enabled=True,
        )
        assert gate_db.get_subscription(filler, pause_owner).enabled is True
        clear_chat_unreachable(gate_db, pause_chat)
        # One slot taken by filler → only plimit-1 of delivery-paused can re-enable.
        reenabled = sum(
            1
            for sid in pause_ids
            if gate_db.get_subscription(sid, pause_owner).enabled
        )
        assert reenabled == plimit - 1
        still_paused_flag = sum(
            1
            for sid in pause_ids
            if gate_db.get_subscription(sid, pause_owner).delivery_paused
        )
        assert still_paused_flag == 0
        assert (
            sum(
                1
                for sid in pause_ids
                if not gate_db.get_subscription(sid, pause_owner).enabled
            )
            == 1
        )

        restore_owner = 88007
        gate_db.upsert_user(restore_owner)
        restore_ids: list[int] = []
        for login in ("rc1", "rc2", "rc3"):
            sid = gate_db.add_subscription(
                owner_id=restore_owner,
                twitch_username=login,
                twitch_user_id=login,
                message_template="t",
                dest_type="dm",
                chat_id=restore_owner,
                thread_id=None,
                enabled=False,
            )
            assert gate_db.delete_subscription(sid, restore_owner, to_cart=True)
            items = gate_db.list_deleted_subscriptions(
                restore_owner, days=30, is_demo=False, limit=10
            )
            restore_ids = [int(i.cart_id) for i in items]
        type_owner = 88008
        gate_db.upsert_user(type_owner)
        live_sid = gate_db.add_subscription(
            owner_id=type_owner,
            twitch_username="live1",
            twitch_user_id="live1",
            message_template="t",
            dest_type="dm",
            chat_id=type_owner,
            thread_id=None,
            enabled=False,
        )
        cat_sid = gate_db.add_subscription(
            owner_id=type_owner,
            twitch_username="cat1",
            twitch_user_id="cat1",
            message_template="t",
            dest_type="dm",
            chat_id=type_owner,
            thread_id=None,
            enabled=False,
            notify_on_live=False,
            notify_on_category_change=True,
        )
        assert gate_db.delete_subscription(live_sid, type_owner, to_cart=True)
        assert gate_db.delete_subscription(cat_sid, type_owner, to_cart=True)
        typed = gate_db.list_deleted_subscriptions(
            type_owner, days=30, is_demo=False, limit=10
        )
        by_login = {i.twitch_username: i.alert_type for i in typed}
        assert by_login["live1"] == "live"
        assert by_login["cat1"] == "category"
        restored_n, enabled_n = gate_db.restore_deleted_subscriptions(
            restore_owner, restore_ids, days=30, is_demo=False, max_enabled=1
        )
        assert restored_n == 3 and enabled_n == 1
        assert gate_db.count_enabled_subscriptions(restore_owner) == 1

    assert parse_admin_user_ids("") == frozenset()
    assert parse_admin_user_ids("123, 456") == frozenset({123, 456})
    assert _parse_sb_edit_f_id("sb_edit_f:42:text") == 42
    assert _parse_sb_edit_f_id("sb_edit_f:7:time") == 7
    assert "⏸" in tr("sync_disable", "ru")
    assert "⏸" in tr("toggle_off", "ru")

    from translate import markdown_to_telegram_html

    assert translate_text("hello", target_lang="en", source_lang="en") == "hello"
    assert (
        markdown_to_telegram_html("**Доказательства**\n\n- item")
        == "<b>Доказательства</b>\n\n- item"
    )
    assert (
        markdown_to_telegram_html("[logs](https://example.com)")
        == '<a href="https://example.com">logs</a>'
    )
    assert build_translations("hello", "en", {"en"}) == {"en": "hello"}
    assert build_translations("hello", "en", {"en", "ru"})["en"] == "hello"

    import premium as prem
    from premium import (
        apply_features_payment,
        apply_lifetime_payment,
        apply_stars_payment,
        invoice_payload,
        parse_invoice_payload,
        start_trial,
    )
    from i18n import premium_features_keyboard

    parsed = parse_invoice_payload(invoice_payload(7, "month"))
    assert parsed is not None and parsed.user_id == 7 and parsed.kind == "month"
    assert parse_invoice_payload("premium:7").kind == "legacy"
    assert parse_invoice_payload("other") is None
    feat_p = parse_invoice_payload(
        invoice_payload(3, "feat", ["advanced_mode", "alert_history"])
    )
    assert feat_p is not None and feat_p.features == ("advanced_mode", "alert_history")
    # Legacy feature ids are stripped from new invoices → invalid empty feat payload.
    assert parse_invoice_payload(invoice_payload(3, "feat", ["delay", "repeat"])) is None
    ch_p = parse_invoice_payload(
        invoice_payload(
            9, "channel", twitch_user_id="42", twitch_login="StreamerX"
        )
    )
    assert ch_p is not None and ch_p.kind == "channel"
    assert ch_p.user_id == 9 and ch_p.twitch_user_id == "42"
    assert ch_p.twitch_login == "streamerx"
    from config import FREE_CHAT_ID, PREMIUM_CHANNEL_STARS

    assert FREE_CHAT_ID == -1002155969539
    assert PREMIUM_CHANNEL_STARS == 1500
    from telegram.constants import ChatMemberStatus

    assert hasattr(ChatMemberStatus, "OWNER")
    assert not hasattr(ChatMemberStatus, "CREATOR")
    assert hasattr(ChatMemberStatus, "BANNED")
    assert not hasattr(ChatMemberStatus, "KICKED")
    # Bot API 7.0 removed Message.forward_from_chat; a personal forward must
    # return no chat instead of raising AttributeError in the wizard.
    import asyncio

    from telegram import (
        Chat,
        MessageOriginChannel,
        MessageOriginChat,
        MessageOriginHiddenUser,
        MessageOriginUser,
        User,
    )

    from bot import (
        _delivery_fail_chat_label,
        _delivery_fail_notice_due,
        _extract_forward_chat,
        _membership_check_blocked,
        _parse_dest_input,
        _user_can_manage_chat,
    )

    _now = datetime.now(timezone.utc)
    _pm = Chat(id=7, type="private")
    _fwd_user = Message(
        message_id=1,
        date=_now,
        chat=_pm,
        forward_origin=MessageOriginUser(
            date=_now, sender_user=User(id=5, first_name="A", is_bot=False)
        ),
    )
    try:
        _ = _fwd_user.forward_from_chat
        raise AssertionError("Message.forward_from_chat must be removed")
    except AttributeError:
        pass
    assert _extract_forward_chat(_fwd_user) == (None, None)
    _fwd_hidden = Message(
        message_id=2,
        date=_now,
        chat=_pm,
        forward_origin=MessageOriginHiddenUser(date=_now, sender_user_name="anon"),
    )
    assert _extract_forward_chat(_fwd_hidden) == (None, None)
    _fwd_channel = Message(
        message_id=3,
        date=_now,
        chat=_pm,
        forward_origin=MessageOriginChannel(
            date=_now, chat=Chat(id=-100123, type="channel"), message_id=9
        ),
    )
    assert _extract_forward_chat(_fwd_channel) == (-100123, None)
    _fwd_group = Message(
        message_id=4,
        date=_now,
        chat=_pm,
        message_thread_id=30,
        forward_origin=MessageOriginChat(
            date=_now, sender_chat=Chat(id=-100999, type="supergroup")
        ),
    )
    assert _extract_forward_chat(_fwd_group) == (-100999, 30)
    # Same path as the wizard DEST_CHAT step: DM forward → hint, no crash.
    _cid, _tid, _err = asyncio.run(_parse_dest_input(None, _fwd_user, "group", "ru"))
    assert _cid is None and _tid is None and _err == tr("fwd_from_dm", "ru")
    _cid, _tid, _err = asyncio.run(_parse_dest_input(None, _fwd_channel, "channel", "ru"))
    assert (_cid, _tid, _err) == (-100123, None, None)
    assert _membership_check_blocked(BadRequest("Member list is inaccessible"))
    assert not _membership_check_blocked(BadRequest("User not found"))

    class _BotMemberOk:
        async def get_chat_member(self, chat_id, user_id):
            from telegram.constants import ChatMemberStatus

            class _Member:
                status = ChatMemberStatus.ADMINISTRATOR

            return _Member()

    class _BotMemberBlocked:
        async def get_chat_member(self, chat_id, user_id):
            raise BadRequest("Member list is inaccessible")

    assert asyncio.run(_user_can_manage_chat(_BotMemberOk(), -1001, 42)) is True
    assert asyncio.run(_user_can_manage_chat(_BotMemberBlocked(), -1001, 42)) is None
    _now = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
    assert _delivery_fail_notice_due(99, now=_now)
    from bot import _delivery_fail_notified

    _delivery_fail_notified[99] = _now - timedelta(hours=1)
    assert not _delivery_fail_notice_due(99, now=_now)
    _delivery_fail_notified[99] = _now - timedelta(hours=25)
    assert _delivery_fail_notice_due(99, now=_now)
    _delivery_fail_notified.pop(99, None)
    assert _delivery_fail_chat_label("My Group", -1001980871389) == (
        "My Group (-1001980871389)"
    )
    assert _delivery_fail_chat_label("-1001980871389", -1001980871389) == (
        "-1001980871389"
    )
    from i18n import delivery_fail_notice_keyboard

    kb = delivery_fail_notice_keyboard(42, "ru")
    assert kb.inline_keyboard[0][0].callback_data == "edit:42"
    assert kb.inline_keyboard[1][0].callback_data == "delivery_fail_del:42"
    # PTB 21.8+: subscription_expiration_date is datetime (same formula as premium_handlers)
    stars_exp = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    until_sub = int(stars_exp.timestamp()) if stars_exp is not None else 0
    assert until_sub == int(stars_exp.timestamp())
    none_exp = None
    assert (int(none_exp.timestamp()) if none_exp is not None else 0) == 0
    try:
        int(stars_exp)
        raise AssertionError("int(datetime) must raise TypeError")
    except TypeError:
        pass
    assert prem.stars_price(249097744) == 1
    assert prem.stars_year_price(249097744) == 1
    assert prem.stars_lifetime_price(249097744) == 1
    assert prem.stars_feature_price(249097744) == 1
    assert prem.has_custom_stars_price(249097744)
    assert not prem.has_custom_stars_price(1)
    assert prem.stars_price() == prem.stars_price(1)
    assert prem.stars_feature_price() == prem.stars_feature_price(1)
    assert "5" in _premium_gate_text("ru", "active_limit", "отмените")
    assert "Типы кроме старта стрима" in _premium_gate_text(
        "ru", "alert_type", "отмените"
    )
    assert "функция Premium" in _premium_gate_text(
        "ru", "delay", "пропустите шаг"
    )

    # Share token snapshot + list pagination chunks + free edit resets advanced fields.
    from db.models import _subscription_cart_snapshot
    from handlers.subscriptions import (
        _PICK_PAGE_SIZE,
        _build_subs_list_pages,
        _edit_pick_keyboard,
        _format_sub_line,
        _subs_toggle_keyboard,
    )

    with tempfile.TemporaryDirectory() as share_tmp:
        share_db = SqliteDatabase(Path(share_tmp) / "share.db")
        share_db.upsert_user(9001)
        share_db.set_user_locale(9001, "ru")
        src_id = share_db.add_subscription(
            owner_id=9001,
            twitch_username="sharechan",
            twitch_user_id="uid_share",
            message_template="hi {username}",
            dest_type="channel",
            chat_id=-100111,
            thread_id=7,
            delay_minutes=15,
            suppress_repeat_minutes=30,
            ignore_keywords="spoiler",
            use_global_ignore=True,
            attach_chat_button=True,
            delete_previous=True,
            notify_delete_fail=True,
            enabled=True,
        )
        src = share_db.get_subscription(src_id, 9001)
        assert src is not None
        snap = _subscription_cart_snapshot(src)
        token = share_db.ensure_alert_share_token(9001, src_id, snap)
        assert token
        assert share_db.ensure_alert_share_token(9001, src_id, snap) == token
        loaded = share_db.get_alert_share_snapshot(token)
        assert loaded is not None
        assert loaded["twitch_username"] == "sharechan"
        assert loaded["delay_minutes"] == 15
        assert loaded["ignore_keywords"] == "spoiler"

        share_db.upsert_user(9002)
        share_db.set_user_locale(9002, "ru")
        assert prem.may_enable_subscription(share_db, 9002)
        cloned_id = share_db.add_subscription(
            owner_id=9002,
            twitch_username=str(loaded["twitch_username"]),
            twitch_user_id=str(loaded["twitch_user_id"]),
            message_template=str(loaded["message_template"]),
            dest_type="dm",
            chat_id=9002,
            thread_id=None,
            delete_previous=bool(loaded.get("delete_previous")),
            notify_delete_fail=bool(loaded.get("notify_delete_fail")),
            disable_link_preview=bool(loaded.get("disable_link_preview")),
            strip_name_mentions=bool(loaded.get("strip_name_mentions")),
            attach_chat_button=bool(loaded.get("attach_chat_button")),
            delay_minutes=int(loaded.get("delay_minutes") or 0),
            suppress_repeat_minutes=int(loaded.get("suppress_repeat_minutes") or 0),
            ignore_keywords=str(loaded.get("ignore_keywords") or ""),
            use_global_ignore=bool(loaded.get("use_global_ignore")),
            enabled=True,
            from_twitch_sync=False,
            from_watch_suggest=False,
            is_demo=False,
        )
        cloned = share_db.get_subscription(cloned_id, 9002)
        assert cloned is not None
        assert cloned.dest_type == "dm"
        assert cloned.chat_id == 9002
        assert cloned.thread_id is None
        assert cloned.delay_minutes == 15
        assert cloned.ignore_keywords == "spoiler"
        assert cloned.attach_chat_button is True

        # Free user without advanced_mode: opening edit resets premium fields.
        assert not prem.has_feature_sync(share_db, 9002, "advanced_mode")
        share_db.update_subscription(
            cloned_id,
            9002,
            ignore_keywords="",
            use_global_ignore=False,
            delay_minutes=0,
            suppress_repeat_minutes=0,
            attach_chat_button=False,
            delete_previous=False,
            notify_delete_fail=False,
            delete_other_alerts=False,
        )
        reset = share_db.get_subscription(cloned_id, 9002)
        assert reset is not None
        assert reset.delay_minutes == 0
        assert reset.ignore_keywords == ""
        assert reset.attach_chat_button is False

        line = _format_sub_line(src, "ru", 1)
        assert "Поделиться оповещением" not in line
        assert "• Оповещение: начало стрима" in line
        assert "<a href" not in line
        kb_rows = _subs_toggle_keyboard(share_db, 9001, "ru", [src])
        assert len(kb_rows) >= 2
        assert [b.callback_data for b in kb_rows[0]] == [
            f"toggle:{src.id}",
            f"edit:{src.id}",
        ]
        delete_cbs = [b.callback_data for b in kb_rows[1]]
        assert f"list_del:{src.id}" in delete_cbs
        share_cbs = [
            b.callback_data
            for row in kb_rows
            for b in row
            if (b.callback_data or "").startswith("share_show:")
        ]
        assert share_cbs == [f"share_show:{src.id}"]
        assert all(
            (src.twitch_username or "") in (b.text or "")
            for row in kb_rows
            for b in row
        )

        long_blocks = [
            (f"block-{i}\n" + ("x" * 500), src) for i in range(12)
        ]
        pages = _build_subs_list_pages("title\n", long_blocks)
        assert len(pages) > 1
        many = [
            share_db.get_subscription(
                share_db.add_subscription(
                    owner_id=9001,
                    twitch_username=f"u{i}",
                    twitch_user_id=f"id{i}",
                    message_template="t",
                    dest_type="dm",
                    chat_id=9001,
                    thread_id=None,
                    enabled=False,
                ),
                9001,
            )
            for i in range(_PICK_PAGE_SIZE + 3)
        ]
        many = [s for s in many if s is not None]
        kb0 = _edit_pick_keyboard(share_db, 9001, many, page=0)
        kb1 = _edit_pick_keyboard(share_db, 9001, many, page=1)
        assert any(
            (b.callback_data or "").startswith("edit_page:")
            for row in kb0.inline_keyboard
            for b in row
        )
        assert any(
            (b.callback_data or "") == "edit_page:0"
            for row in kb1.inline_keyboard
            for b in row
        )

    # Admin refund by charge_id: revoke features / stars immediately + find owner.
    with tempfile.TemporaryDirectory() as refund_tmp:
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from premium import (
            admin_refund_charge,
            apply_features_payment,
            apply_stars_payment,
            get_status,
            revoke_premium_for_charge,
        )

        rdb = SqliteDatabase(Path(refund_tmp) / "refund.db")
        uid = 424242
        feat_charge = "stx" + ("A" * 40)
        stars_charge = "stx" + ("B" * 40)
        until = int(datetime.now(timezone.utc).timestamp()) + 86400 * 30
        apply_features_payment(
            rdb,
            uid,
            feature_ids=["advanced_mode", "twitch_sync"],
            charge_id=feat_charge,
            until_unix=until,
            stars_paid=100,
        )
        rdb.set_advanced_mode_setting(uid, True)
        assert rdb.find_user_id_by_premium_charge(feat_charge) == uid
        revoked = revoke_premium_for_charge(rdb, uid, feat_charge)
        assert "advanced_mode" in revoked and "twitch_sync" in revoked
        st = get_status(rdb, uid)
        assert not st.feature_active("advanced_mode")
        assert not st.feature_active("twitch_sync")
        assert rdb.get_advanced_mode_setting(uid) is False
        assert rdb.find_user_id_by_premium_charge(feat_charge) is None

        apply_stars_payment(
            rdb, uid, charge_id=stars_charge, until_unix=until, stars_paid=50
        )
        assert get_status(rdb, uid).stars_active
        bot = MagicMock()
        bot.refund_star_payment = AsyncMock(return_value=True)
        bot.edit_user_star_subscription = AsyncMock(return_value=True)
        result = asyncio.run(admin_refund_charge(bot, rdb, stars_charge))
        assert result.ok and result.user_id == uid
        assert "stars" in result.revoked
        assert not get_status(rdb, uid).stars_active
        bot.refund_star_payment.assert_awaited_once()
