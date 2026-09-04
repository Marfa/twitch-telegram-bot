"""Core self-checks: analytics, twitch parse, templates, oauth, eventsub, posthog."""
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
    fix_placeholder_typos,
    normalize_ignore_keywords,
    merge_ignore_keywords,
    normalize_watch_tags,
    pick_random_streams,
    preview_stream_title,
    render_template,
    should_ignore_stream,
    stream_duration_minutes,
    stream_end_snapshot,
    template_uses_html,
    twitch_status_fingerprint,
)
from translate import build_translations, markdown_to_telegram_html, translate_text
from bot import (
    _alert_history_item_url,
    _alert_history_nav_keyboard,
    _build_alert_history_chunks,
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
    end_cover_stream,
    migrate_import_sync_subscriptions,
    needs_live_game_recheck,
    _format_pause_until,
    _user_notifications_paused,
)
from handlers.notifications import _end_alert_template_args, _offline_end_stream
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
from telegram import LinkPreviewOptions, Message



def check_core() -> None:
    import analytics as analytics_mod

    assert analytics_mod.distinct_id(1) == analytics_mod.distinct_id(1)
    assert analytics_mod.distinct_id(1) != analytics_mod.distinct_id(2)
    analytics_mod.capture(1, "self_check_noop")
    analytics_mod.capture_exception(RuntimeError("self_check"), user_id=1)

    class _Stats:
        users = 1
        notify_users = 1
        unique_owners = 1
        subscriptions_total = 2
        subscriptions_enabled = 1
        subscriptions_disabled = 1
        unique_twitch_channels = 1
        premium_paid = 0
        blocked_users = 0
        sys_updates = 1
        sys_availability = 1
        sys_other = 1
        locale_en = 0
        locale_ru = 1
        locale_unset = 0

    analytics_mod.capture_bot_stats(_Stats())

    filt = analytics_mod._WarningPlusRedactFilter()
    import logging as _logging

    info_rec = _logging.LogRecord("t", _logging.INFO, __file__, 1, "ok", (), None)
    assert filt.filter(info_rec) is False
    warn_rec = _logging.LogRecord(
        "t",
        _logging.WARNING,
        __file__,
        1,
        "token=supersecrettokenvalue",
        (),
        None,
    )
    assert filt.filter(warn_rec) is True
    assert "[redacted]" in warn_rec.getMessage()

    from bot import _seconds_until_next_daily_stats

    delay = _seconds_until_next_daily_stats()
    assert 0 < delay <= 24 * 3600

    CHANNEL = "marfapr"
    t = TwitchClient()
    assert t.parse_username(CHANNEL) == CHANNEL
    assert t.parse_username("https://www.twitch.tv/marfapr") == CHANNEL
    assert t.parse_username("https://twitch.tv/Marfapr") == CHANNEL
    assert t.parse_username("https://m.twitch.tv/marfapr") == CHANNEL
    assert t.parse_username("@marfapr") == CHANNEL
    assert t.parse_username("not valid!!!") is None
    assert t.is_twitch_url("https://www.twitch.tv/marfapr")
    assert t.is_twitch_url("https://m.twitch.tv/marfapr")
    assert t.is_twitch_url("twitch.tv/marfapr")
    assert not t.is_twitch_url("marfapr")
    assert not t.is_twitch_url("@marfapr")

    out = render_template("{username}: {game} / {name}", CHANNEL, "Just Chatting", "Test")
    assert out == "marfapr: Just Chatting / Test"

    class _FakeTwitch:
        def get_user(self, login: str):
            return {"login": login} if login.lower() == "shroud" else None

    from twitch import (
        box_art_cdn_url,
        format_box_art_url,
        is_game_cover_image,
        resolve_sub_image_photo,
        strip_name_mentions_and_commands,
        template_has_game_placeholder,
        GAME_COVER_IMAGE_ID,
    )

    assert is_game_cover_image(GAME_COVER_IMAGE_ID)
    assert not is_game_cover_image("AgAC_test")
    assert template_has_game_placeholder("{username} {game}")
    assert not template_has_game_placeholder("{username} only")
    assert format_box_art_url(
        "https://cdn.example/{width}x{height}.jpg", width=1920, height=2560
    ) == "https://cdn.example/1920x2560.jpg"
    assert (
        box_art_cdn_url("509658", width=1920, height=2560)
        == "https://static-cdn.jtvnw.net/ttv-boxart/509658-1920x2560.jpg"
    )

    class _CoverTwitch:
        def resolve_box_art_url(self, *, game_id: str = "", game_name: str = "", **_kw):
            if game_id:
                return f"https://cdn.example/{game_id}.jpg"
            if game_name:
                return f"https://cdn.example/name/{game_name}.jpg"
            return None

    cover_sub = type("S", (), {"image_file_id": GAME_COVER_IMAGE_ID})()
    assert (
        resolve_sub_image_photo(
            cover_sub, {"game_id": "509658", "game_name": "Just Chatting"}, _CoverTwitch()
        )
        == "https://cdn.example/509658.jpg"
    )
    # Schedule segments expose category{id,name}, not game_id/game_name.
    assert (
        resolve_sub_image_photo(
            cover_sub,
            {"category": {"id": "516575", "name": "VALORANT"}},
            _CoverTwitch(),
        )
        == "https://cdn.example/516575.jpg"
    )
    assert resolve_sub_image_photo(cover_sub, {}, _CoverTwitch()) is None
    assert (
        resolve_sub_image_photo(
            type("S", (), {"image_file_id": "AgAC_custom"})(),
            None,
            None,
        )
        == "AgAC_custom"
    )

    cleaned = strip_name_mentions_and_commands(
        "hi @shroud !drop @notarealuser123xx", _FakeTwitch()
    )
    assert "@shroud" not in cleaned
    assert "!drop" not in cleaned
    assert "@notarealuser123xx" in cleaned
    assert (
        render_template(
            "{name}",
            CHANNEL,
            name="play with @shroud !points",
            strip_name_mentions=True,
            twitch=_FakeTwitch(),
        )
        == "play with"
    )
    assert (
        render_template(
            "{name}",
            CHANNEL,
            name="play with @shroud !points",
            strip_name_mentions=False,
            twitch=_FakeTwitch(),
        )
        == "play with @shroud !points"
    )
    rich = render_template(
        "{username} {viewer_count} {tags} {is_mature} {game_id}",
        "x",
        "Game",
        "Title",
        stream={
            "viewer_count": 42,
            "tags": ["a", "b"],
            "is_mature": True,
            "game_id": "509658",
            "id": "99",
            "type": "live",
            "started_at": "2026-01-01T00:00:00Z",
            "thumbnail_url": "https://example.com/{width}x{height}.jpg",
            "language": "en",
        },
    )
    assert "42" in rich and "a, b" in rich and "18+" in rich and "509658" in rich
    thumb = render_template(
        "{thumbnail_url}",
        "x",
        stream={"thumbnail_url": "https://cdn/{width}x{height}.jpg"},
    )
    assert "480x270" in thumb
    assert "{game}" not in render_template("{game_id}", "u", "G", "N", stream={"game_id": "1"})
    assert render_template("{game_id}", "u", "G", "N", stream={"game_id": "1"}) == "1"
    assert (
        render_template(
            "in {minutes} min: {name}",
            "u",
            name="Soon",
            extra={"minutes": "15"},
        )
        == "in 15 min: Soon"
    )
    assert template_uses_html("<b>{username}</b> live")
    assert template_uses_html('<a href="https://x">{name}</a>')
    assert not template_uses_html("{username} is live")
    assert (
        render_template(
            "<b>{username}</b>",
            "a<b>c",
            escape_html=True,
        )
        == "<b>a&lt;b&gt;c</b>"
    )
    from twitch import template_has_link

    assert template_has_link("https://twitch.tv/{username}")
    assert template_has_link("see twitch.tv/foo")
    assert not template_has_link("Скоро стрим. Не забудь сделать анон")
    auth_url = t.build_authorize_url(
        redirect_uri="https://example.com/oauth/twitch/callback",
        state="abc",
    )
    assert "response_type=code" in auth_url
    assert "user%3Aread%3Afollows" in auth_url or "user:read:follows" in auth_url
    assert "offline_access" not in auth_url
    assert "state=abc" in auth_url
    state = create_oauth_state(42, "ru")
    assert pop_oauth_state(state) == (42, "ru", "import")
    assert pop_oauth_state(state) is None
    state2 = create_oauth_state(42, "en", purpose="schedule")
    assert pop_oauth_state(state2) == (42, "en", "schedule")
    state3 = create_oauth_state(7, "ru", purpose="whispers")
    assert pop_oauth_state(state3) == (7, "ru", "whispers")
    assert WHISPERS_SCOPE == "user:read:whispers"
    import inspect as _inspect
    from eventsub import timestamp_fresh as _ts_fresh

    create_params = list(_inspect.signature(TwitchClient.create_whisper_eventsub).parameters)
    assert "user_access_token" not in create_params
    assert _ts_fresh("") is True

    import hashlib
    import hmac as hmac_mod
    from datetime import datetime as dt
    from i18n import t as i18n_t
    from eventsub import (
        format_whisper_alert,
        handle_eventsub_post,
        parse_whisper_event,
        remember_message_id,
        verify_signature,
        whisper_conversation_url,
    )

    secret = "s3cRe7s3cRe7"
    body = b'{"challenge":"abc-challenge"}'
    mid = "msg-fresh-1"
    ts = dt.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sig = "sha256=" + hmac_mod.new(
        secret.encode(), (mid + ts).encode() + body, hashlib.sha256
    ).hexdigest()
    assert verify_signature(
        secret=secret, message_id=mid, timestamp=ts, body=body, signature=sig
    )
    assert not verify_signature(
        secret=secret, message_id=mid, timestamp=ts, body=body, signature="sha256=dead"
    )
    challenge = handle_eventsub_post(
        headers={
            "Twitch-Eventsub-Message-Id": mid,
            "Twitch-Eventsub-Message-Timestamp": ts,
            "Twitch-Eventsub-Message-Signature": sig,
            "Twitch-Eventsub-Message-Type": "webhook_callback_verification",
        },
        body=body,
        secret=secret,
    )
    assert challenge.status == 200
    assert challenge.body == b"abc-challenge"
    payload = {
        "subscription": {
            "id": "sub-1",
            "type": "user.whisper.message",
            "condition": {"user_id": "42"},
        },
        "event": {
            "from_user_id": "1",
            "from_user_login": "alice",
            "from_user_name": "Alice",
            "to_user_id": "42",
            "to_user_login": "bobby",
            "whisper_id": "w1",
            "whisper": {"text": "hi <b>there</b>"},
        },
    }
    parsed = parse_whisper_event(payload)
    assert parsed is not None
    assert parsed.from_user_login == "alice"
    assert parsed.text == "hi <b>there</b>"
    url = whisper_conversation_url(to_login="bobby", from_login="alice")
    assert url == "https://www.twitch.tv/popout/bobby/whisper"
    assert whisper_conversation_url() == "https://www.twitch.tv/inbox"
    formatted = format_whisper_alert("ru", parsed, url=url)
    assert "Alice" in formatted
    assert "@alice" in formatted
    assert "hi &lt;b&gt;there&lt;/b&gt;" in formatted
    assert "Открыть переписку" in formatted
    nbody = json.dumps(payload).encode()
    nid = "msg-notify-1"
    nts = dt.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    nsig = "sha256=" + hmac_mod.new(
        secret.encode(), (nid + nts).encode() + nbody, hashlib.sha256
    ).hexdigest()
    headers_n = {
        "Twitch-Eventsub-Message-Id": nid,
        "Twitch-Eventsub-Message-Timestamp": nts,
        "Twitch-Eventsub-Message-Signature": nsig,
        "Twitch-Eventsub-Message-Type": "notification",
    }
    first = handle_eventsub_post(headers=headers_n, body=nbody, secret=secret)
    assert first.status == 204 and first.whisper is not None
    dup = handle_eventsub_post(headers=headers_n, body=nbody, secret=secret)
    assert dup.status == 204 and dup.whisper is None
    assert remember_message_id(nid) is False
    assert i18n_t("whisper_alerts_enable", "ru") == "Включить"
    assert "личных сообщениях" in i18n_t("whisper_alerts_screen", "ru")
    assert i18n_t("btn_whisper_alerts", "ru")

    created = parse_posthog_issue_payload(
        {
            "kind": "$error_tracking_issue_created",
            "name": "ValueError: boom",
            "description": "Traceback…",
            "fingerprint": "abc",
            "url": "https://us.posthog.com/project/1/error_tracking/x",
        }
    )
    assert created is not None
    assert created["kind"] == "created"
    assert created["name"].startswith("ValueError")
    reopened = parse_posthog_issue_payload(
        {
            "event": {
                "event": "$error_tracking_issue_reopened",
                "properties": {
                    "name": "KeyError",
                    "description": "missing",
                    "fingerprint": "fp1",
                },
            }
        }
    )
    assert reopened is not None and reopened["kind"] == "reopened"
    report = parse_posthog_issue_payload(
        {
            "kind": "$scout_report_emitted",
            "title": "fix(bot): example",
            "summary": "Something broke",
            "report_url": "https://us.posthog.com/project/1/inbox/reports/x",
            "outcome": "surfaced",
        }
    )
    assert report is not None and report["kind"] == "report"
    assert parse_posthog_issue_payload(
        {
            "kind": "$scout_report_emitted",
            "title": "held back",
            "outcome": "held_back",
        }
    ) is None
    assert _is_unchanged_message_edit(
        BadRequest(
            "Message is not modified: specified new message content and reply markup "
            "are exactly the same as a current content and reply markup of the message"
        )
    )
    assert not _is_unchanged_message_edit(BadRequest("Chat not found"))
    assert not _is_unchanged_message_edit(RuntimeError("not modified"))
    with tempfile.TemporaryDirectory() as tmp:
        seen_path = Path(tmp) / "posthog_seen_reports.json"
        assert _load_posthog_seen_report_ids(seen_path) == set()
        _save_posthog_seen_report_ids(seen_path, {"b", "a"})
        assert _load_posthog_seen_report_ids(seen_path) == {"a", "b"}
    assert _is_http_timeout(TimeoutError("timed out"))
    import urllib.error

    assert _is_http_timeout(urllib.error.URLError(TimeoutError("timed out")))
    assert not _is_http_timeout(urllib.error.URLError("connection refused"))
    assert not _is_http_timeout(RuntimeError("boom"))
    assert parse_posthog_issue_payload({"event": {"event": "$pageview"}}) is None
    assert TwitchClient.is_one_off_schedule_forbidden(
        Exception("403 Client Error: single segment creation not authorized for url: x")
    )
    assert not TwitchClient.is_one_off_schedule_forbidden(Exception("rate limit exceeded"))
    assert TwitchClient.is_recurring_start_forbidden(
        Exception(
            "400 Client Error: FirstOccurrenceDate can't set FirstOccurrenceDate "
            "on recurring segments for url: https://api.twitch.tv/helix/schedule/segment"
        )
    )
    assert TwitchClient.is_overlapping_schedule(
        Exception("400 Client Error: Segment cannot create overlapping segment for url: x")
    )
    overlap_ids = TwitchClient.overlapping_schedule_segment_ids(
        [
            {
                "id": "seg-a",
                "start_time": "2026-08-10T12:30:00Z",
                "end_time": "2026-08-10T14:30:00Z",
            },
            {
                "id": "seg-b",
                "start_time": "2026-08-11T12:30:00Z",
                "end_time": "2026-08-11T14:30:00Z",
            },
        ],
        start_time="2026-08-10T12:30:00Z",
        duration=120,
    )
    assert overlap_ids == ["seg-a"]
    from twitch import SCHEDULE_OAUTH_SCOPES, SCHEDULE_SCOPE

    assert SCHEDULE_SCOPE in SCHEDULE_OAUTH_SCOPES
    from health import _html_page
    from i18n import t as tr

    assert "Partner/Affiliate" in tr("stream_schedule_publish_ok_recurring", "en")
    assert "Partner/Affiliate" in tr("stream_schedule_publish_ok_recurring", "ru")
    assert tr("stream_schedule_publishing", "ru")
    assert tr("stream_schedule_publishing", "en")
    assert "UTC+3" in tr("stream_schedule_tz_prompt", "ru")
    assert "UTC-5" in tr("stream_schedule_tz_prompt", "en")
    assert "New York" in tr("stream_schedule_tz_prompt", "en")
    assert tr("stream_schedule_mode_tz_btn", "ru")
    assert tr("stream_schedule_mode_tz_btn", "en")
    assert tr("stream_schedule_duration_prompt", "ru")
    assert tr("stream_schedule_duration_prompt_keep", "ru")
    assert tr("stream_schedule_more_prompt", "ru")
    assert tr("stream_schedule_add_slot", "en")
    assert tr("stream_schedule_delete_slot", "en")
    assert tr("stream_schedule_deleted_slots", "en", count=2)
    assert "Пример" not in tr("stream_schedule_mode_intro", "ru")
    assert "Example" not in tr("stream_schedule_mode_intro", "en")
    assert "Пример" not in tr("stream_schedule_intro", "ru")
    assert "Example" not in tr("stream_schedule_intro", "en")
    assert "Sovereign" not in tr("stream_schedule_intro", "ru")
    assert "Sovereign" not in tr("stream_schedule_mode_intro", "ru")
    assert "{date}" in tr("stream_schedule_err_generic", "ru")
    assert tr("stream_schedule_duration_unsure", "en")
    from i18n import (
        main_menu,
        admin_menu,
        other_menu,
        settings_menu,
        subscriptions_menu,
        stream_schedule_duration_keyboard,
        watch_suggest_keyboard,
    )

    for loc in ("en", "ru"):
        main_kb = main_menu(loc).keyboard
        main_btns = [b.text for row in main_kb for b in row]
        assert btn("other", loc) in main_btns
        assert btn("alert_history", loc) in main_btns
        assert btn("list", loc) in main_btns
        assert btn("manage", loc) not in main_btns
        assert btn("create_schedule", loc) not in main_btns
        assert btn("watch", loc) not in main_btns
        assert main_btns.index(btn("list", loc)) < main_btns.index(
            btn("alert_history", loc)
        )
        assert main_btns.index(btn("alert_history", loc)) < main_btns.index(
            btn("other", loc)
        )
        assert main_btns.index(btn("other", loc)) < main_btns.index(
            btn("settings", loc)
        )
        other_settings_row = next(
            row
            for row in main_kb
            if btn("other", loc) in [b.text for b in row]
        )
        assert [b.text for b in other_settings_row] == [
            btn("other", loc),
            btn("settings", loc),
        ]
        feedback_row = next(
            row
            for row in main_kb
            if btn("feedback", loc) in [b.text for b in row]
        )
        assert [b.text for b in feedback_row] == [btn("feedback", loc)]
        other_kb = other_menu(loc).keyboard
        assert [[b.text for b in row] for row in other_kb] == [
            [btn("whisper_alerts", loc), btn("create_schedule", loc)],
            [btn("watch", loc), btn("chat", loc)],
            [btn("back", loc)],
        ]
        for i, row in enumerate(other_kb):
            if i == len(other_kb) - 1 and len(row) == 1:
                continue
            assert len(row) == 2, (
                f"other_menu row {i} must be paired: {[b.text for b in row]}"
            )
        subs_kb = subscriptions_menu(loc).keyboard
        assert [[b.text for b in row] for row in subs_kb] == [
            [btn("back", loc)],
        ]
        subs_kb_pause = subscriptions_menu(loc, pause_notifications=True).keyboard
        assert [[b.text for b in row] for row in subs_kb_pause] == [
            [btn("pause_notifications", loc), btn("back", loc)],
        ]
        subs_kb_full = subscriptions_menu(
            loc, cart=True, pause_notifications=True
        ).keyboard
        assert [[b.text for b in row] for row in subs_kb_full] == [
            [btn("cart", loc), btn("pause_notifications", loc)],
            [btn("back", loc)],
        ]
        for subs_rows in (subs_kb, subs_kb_pause, subs_kb_full):
            for i, row in enumerate(subs_rows):
                if i == len(subs_rows) - 1 and len(row) == 1:
                    continue
                assert len(row) == 2, (
                    f"subscriptions_menu row {i} must be paired: "
                    f"{[b.text for b in row]}"
                )
        settings_kb = settings_menu(loc).keyboard
        ignored_row = next(
            row
            for row in settings_kb
            if btn("ignored_words", loc) in [b.text for b in row]
        )
        assert btn("ignored_words", loc) in [b.text for b in ignored_row]
        assert btn("advanced_mode", loc) not in [
            b.text for row in settings_kb for b in row
        ]
        assert btn("message_draft", loc) not in [
            b.text for row in settings_kb for b in row
        ]
        from i18n import beta_mode_btn

        partner_row = next(
            row
            for row in settings_kb
            if btn("partner", loc) in [b.text for b in row]
        )
        assert btn("partner", loc) in [b.text for b in partner_row]
        assert len(partner_row) == 1
        lang_row = next(
            row
            for row in settings_kb
            if btn("language", loc) in [b.text for b in row]
        )
        assert {b.text for b in lang_row} == {
            btn("sys_notifications", loc),
            btn("language", loc),
        }
        suggest_kb = watch_suggest_keyboard(loc, offer_create_alerts=True)
        cbs = [
            (btn.callback_data or "")
            for row in suggest_kb.inline_keyboard
            for btn in row
        ]
        assert "watch:create_alerts" in cbs
        assert "watch:again" in cbs
        assert watch_suggest_keyboard(loc).inline_keyboard[0][0].callback_data == (
            "watch:again"
        )
        admin_kb = admin_menu(loc).keyboard
        assert [[b.text for b in row] for row in admin_kb] == [
            [btn("broadcast", loc), btn("stats", loc)],
            [btn("admin_withdrawals", loc), btn("demo", loc)],
            [btn("admin_refund", loc)],
            [btn("back", loc)],
        ]
        admin_btns = [b.text for row in admin_kb for b in row]
        assert btn("create_schedule", loc) not in admin_btns
        dur_kb = stream_schedule_duration_keyboard(loc)
        assert any(
            (btn.callback_data or "").startswith("stream_sched:duration:")
            for row in dur_kb.inline_keyboard
            for btn in row
        )
    html_ru = _html_page(
        tr("oauth_web_done_title", "ru"),
        tr("oauth_web_done_body", "ru"),
    ).decode()
    assert "Готово" in html_ru
    assert "Telegram" in html_ru
    html_en = _html_page(
        tr("oauth_web_done_title", "en"),
        tr("oauth_web_done_body", "en"),
    ).decode()
    assert "Done" in html_en
    # Threading health server: a hung client must not block /health.
    import socket
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from health import _HealthHandler, mark_ready

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    mark_ready()
    hung = socket.create_connection(("127.0.0.1", port), timeout=2)
    try:
        assert urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=2
        ).read() == b"ok"
    finally:
        hung.close()
        httpd.shutdown()
    title_ru = preview_stream_title("ru", "Elden Ring")
    assert "Elden Ring" in title_ru
    assert "Тестовый" not in title_ru
    title_en = preview_stream_title("en", "Elden Ring")
    assert "Elden Ring" in title_en
    assert "Test stream" not in title_en

    state: dict[str, bool] = {}
    assert live_transitions(state, ["1", "2"], {"1": {}}, primed=False) == ([], [])
    assert state == {"1": True, "2": False}
    assert live_transitions(state, ["1", "2"], {"1": {}}, primed=True) == ([], [])
    assert live_transitions(state, ["1", "2"], {"1": {}, "2": {}}, primed=True) == (
        ["2"],
        [],
    )
    assert state["2"] is True
    assert live_transitions(state, ["1", "2"], {}, primed=True) == ([], ["1", "2"])
    assert state == {"1": False, "2": False}
    assert live_transitions(state, ["1"], {"1": {}}, primed=True) == (["1"], [])
    assert live_transitions(state, ["1"], {}, primed=True) == ([], ["1"])
    assert state["1"] is False

    assert needs_live_game_recheck("", 0) is True
    assert needs_live_game_recheck("   ", 0) is True
    assert needs_live_game_recheck("Just Chatting", 0) is False
    assert needs_live_game_recheck("", 5) is False
    assert SCHEDULE_CHECK_INTERVAL >= 60

    games: dict[str, str] = {}
    names: dict[str, str] = {}
    streams = {"1": {"game_id": "111", "game_name": "Just Chatting"}}
    assert category_change_events(
        games, ["1"], streams, primed=False, last_game_names=names
    ) == []
    assert games == {"1": "111"}
    assert names == {"1": "Just Chatting"}
    assert category_change_events(
        games, ["1"], streams, primed=True, last_game_names=names
    ) == []
    streams = {"1": {"game_id": "222", "game_name": "VALORANT"}}
    assert category_change_events(
        games, ["1"], streams, primed=True, last_game_names=names
    ) == ["1"]
    assert games["1"] == "222"
    assert names["1"] == "VALORANT"
    assert category_change_events(
        games, ["1"], {}, primed=True, last_game_names=names
    ) == []
    assert "1" not in games
    assert "1" not in names
    streams = {"1": {"game_id": "222", "game_name": "VALORANT"}}
    assert category_change_events(
        games, ["1"], streams, primed=True, last_game_names=names
    ) == []
    assert games["1"] == "222"
    assert end_cover_stream(game_id="509658", game_name="Just Chatting") == {
        "game_id": "509658",
        "game_name": "Just Chatting",
    }
    assert end_cover_stream(game_id="", game_name="") is None
    assert end_cover_stream(game_id="", game_name="—") is None
    assert end_cover_stream(game_id="1", game_name="") == {
        "game_id": "1",
        "game_name": "",
    }

    assert find_placeholder_typos("{username} {game} {name}") == []
    assert find_placeholder_typos("{game)") == [("{game)", "{game}")]
    assert find_placeholder_typos("(game}") == [("(game}", "{game}")]
    assert find_placeholder_typos("{Game}") == [("{Game}", "{game}")]
    assert find_placeholder_typos("{gama}") == [("{gama}", "{game}")]
    assert find_placeholder_typos("{user_name}") == [("{user_name}", "{username}")]
    assert ("{nam}", "{name}") in find_placeholder_typos("hi {nam}!")
    assert find_placeholder_typos("Play (game) tonight") == []
    assert find_placeholder_typos("{username} is live, {game)") == [("{game)", "{game}")]
    assert find_placeholder_typos("{ game }") == [("{ game }", "{game}")]
    assert find_placeholder_typos("{title}") == [("{title}", "{name}")]
    assert find_placeholder_typos("{game_name)") == [("{game_name)", "{game}")]
    assert fix_placeholder_typos("{game)") == "{game}"
    assert (
        fix_placeholder_typos("gold_apple в эфире! Категория: {game)")
        == "gold_apple в эфире! Категория: {game}"
    )
    assert fix_placeholder_typos("{username} {Game} {title}") == "{username} {game} {name}"

    snap = stream_end_snapshot(
        {
            "user_login": "foo",
            "game_id": "509658",
            "game_name": "Just Chatting",
            "title": "Test stream",
            "viewer_count": 42,
            "started_at": "2026-01-01T12:00:00Z",
            "tags": ["en", "fps"],
        }
    )
    assert snap and snap["title"] == "Test stream" and snap["viewer_count"] == 42
    assert stream_end_snapshot({}) is None
    last_streams = {
        "1": snap,
    }
    offline = _offline_end_stream(
        "1",
        last_streams=last_streams,
        last_games={},
        last_game_names={},
    )
    assert offline and offline["title"] == "Test stream"
    fallback = _offline_end_stream(
        "2",
        last_streams={},
        last_games={"2": "111"},
        last_game_names={"2": "VALORANT"},
    )
    assert fallback == {"game_id": "111", "game_name": "VALORANT"}
    class _Sub:
        twitch_username = "fallback_user"
    user, game, title, extra = _end_alert_template_args(
        _Sub(),
        {
            "user_login": "foo",
            "game_name": "Just Chatting",
            "title": "My title",
            "started_at": "2026-01-01T00:00:00Z",
        },
    )
    assert user == "foo" and game == "Just Chatting" and title == "My title"
    assert extra and "minutes" in extra
    assert stream_duration_minutes(None) == "—"
    assert normalize_ignore_keywords("foo, bar , baz") == "foo, bar, baz"
    assert normalize_ignore_keywords("") == ""
    assert merge_ignore_keywords("foo", "bar, baz") == "foo, bar, baz"
    assert merge_ignore_keywords("foo", "") == "foo"
    assert merge_ignore_keywords("", "") == ""
    assert should_ignore_stream("chatting", "Just Chatting", "Playing games")
    assert should_ignore_stream("foo", "Foo Bar", "Playing games")
    assert should_ignore_stream("stream", "Just Chatting", "My Foo stream")
    assert not should_ignore_stream("foo, bar", "Just Chatting", "Playing games")
    assert not should_ignore_stream("", "Just Chatting", "Foo")
    assert should_ignore_stream(r"just.?chatting", "Just Chatting", "Playing games")
    assert should_ignore_stream(r"foo|bar", "Just Chatting", "My bar stream")
    assert should_ignore_stream(r"^Just", "Just Chatting", "Playing games")
    assert not should_ignore_stream(r"^Chatting", "Just Chatting", "Playing games")
    assert should_ignore_stream("(unclosed", "foo (unclosed bar", "title")  # invalid re → literal

    assert _parse_watch_viewers("100") == (100, None)
    assert _parse_watch_viewers("100-500") == (100, 500)
    assert _parse_watch_viewers("500-100") == (100, 500)
    assert _parse_watch_viewers("nope") is None
    streams = [
        {"user_id": "1", "viewer_count": 10, "is_mature": False, "tags": ["English"]},
        {"user_id": "2", "viewer_count": 200, "is_mature": True, "tags": ["English", "fps"]},
        {"user_id": "3", "viewer_count": 50, "is_mature": False, "tags": ["English", "fps"]},
        {"user_id": "1", "viewer_count": 99, "is_mature": False, "tags": ["Русский"]},
    ]
    filtered = filter_streams_for_watch(
        streams, min_viewers=20, max_viewers=100, exclude_mature=True
    )
    assert [s["user_id"] for s in filtered] == ["3", "1"]
    assert all(20 <= int(s["viewer_count"]) <= 100 for s in filtered)
    assert all(not s.get("is_mature") for s in filtered)
    tagged = filter_streams_for_watch(
        streams, min_viewers=0, exclude_mature=False, tags=["english", "FPS"]
    )
    assert [s["user_id"] for s in tagged] == ["2", "3"]
    assert normalize_watch_tags(" English , fps; English ") == ["English", "fps"]
    picked = pick_random_streams(streams, 2)
    assert len(picked) == 2
    assert len({s["user_id"] for s in picked}) == 2
    lang_streams = [
        {"user_id": "a", "language": "ru"},
        {"user_id": "b", "language": "en"},
        {"user_id": "c", "language": "ru"},
        {"user_id": "d", "language": "de"},
    ]
    preferred = pick_random_streams(lang_streams, 3, prefer_language="ru")
    assert len(preferred) == 3
    assert preferred[0]["language"] == "ru"
    assert preferred[1]["language"] == "ru"
    assert _watch_channel_refs(
        [
            {"user_id": "1", "user_login": "Alice"},
            {"user_id": "1", "user_login": "alice"},
            {"user_id": "2", "user_login": "bob"},
            {"user_id": "", "user_login": "x"},
        ]
    ) == [
        {"user_id": "1", "user_login": "alice"},
        {"user_id": "2", "user_login": "bob"},
    ]
    prefs = WatchPrefs(
        categories=[{"id": "1", "name": "Just Chatting"}],
        min_viewers=10,
        max_viewers=None,
        language="ru",
        tags=["English"],
        exclude_mature=True,
    )
    assert parse_watch_prefs(dump_watch_prefs(prefs)) == prefs
    legacy_list = parse_watch_filters(
        '{"categories":[{"id":"1","name":"Just Chatting"}],"min_viewers":5,'
        '"exclude_mature":true}'
    )
    assert len(legacy_list) == 1
    assert legacy_list[0].prefs.min_viewers == 5
    multi = parse_watch_filters(dump_watch_filters(legacy_list + legacy_list))
    assert len(multi) == 2
    assert parse_watch_prefs("") is None
    assert parse_watch_prefs("{}") is None
    assert WATCH_MAX_FILTERS >= 1

    link = parse_telegram_topic_link("https://t.me/c/themarfa_gaming/30")
    assert link is not None
    assert link.chat_ref == "themarfa_gaming"
    assert link.thread_id == 30
    from links import parse_telegram_public_chat_link

    assert parse_telegram_public_chat_link("https://t.me/aipchat") == "aipchat"
    assert parse_telegram_public_chat_link("https://t.me/aipchat/") == "aipchat"
    assert parse_telegram_public_chat_link("t.me/aipchat") == "aipchat"
    assert parse_telegram_public_chat_link("https://t.me/aipchat/30") is None
    assert parse_telegram_public_chat_link("@aipchat") is None
    assert chat_ref_to_id("1234567890") == -1001234567890
    assert _message_link(-1001234567890, 42) == "https://t.me/c/1234567890/42"
    assert _message_link(-1001234567890, 42, 7) == "https://t.me/c/1234567890/7/42"
    start = _parse_segment_start({"start_time": "2030-01-01T12:00:00Z"})
    assert start is not None
    assert start.year == 2030
    assert _parse_segment_start({"start_time": "bad"}) is None

    pg_url = _normalize_pg_url("postgres://user:pass@host:1234/db")
    assert pg_url.startswith("postgresql://")
    assert "sslmode=require" in pg_url

    plain = Message(message_id=1, date=None, chat=None)
    assert not _is_link_preview_disabled(plain)
    no_preview = Message(
        message_id=2,
        date=None,
        chat=None,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    assert _is_link_preview_disabled(no_preview)

    for loc in SUPPORTED_LOCALES:
        help_txt = _help_text(loc)
        assert "/schedule" in help_txt
        assert "/settings" in help_txt
        assert "/stats" not in help_txt
        assert btn("create_schedule", loc) in help_txt
        assert btn("alert_history", loc) in help_txt
        assert btn("other", loc) in help_txt
        assert btn("whisper_alerts", loc) in help_txt
        assert btn("new", loc)
        assert btn("watch", loc)
        assert btn("settings", loc)
        assert btn("sync_subs", loc)
        assert btn("language", loc)
        assert tr("start_welcome", loc)
        assert tr("start_welcome_demo", loc, channel="marfapr")
        assert btn("welcome_demo_edit", loc)
        assert btn("welcome_demo_delete", loc)
        assert tr("watch_cats_prompt", loc, max=5)
        assert tr("watch_cats_lucky", loc)
        assert tr("watch_cats_recommended", loc)
        assert tr("watch_recommended_header", loc)
        assert tr("watch_recommended_empty", loc)
        assert tr("watch_lucky_searching", loc)
        assert tr("watch_lucky_empty", loc)
        assert tr("watch_tags_prompt", loc)
        assert tr("watch_pick_prompt", loc)
        assert tr("watch_pick_delete_btn", loc)
        assert tr("watch_delete_pick", loc)
        assert tr("watch_save_prompt", loc, summary="x", max=5)
        assert tr("watch_suggest_header", loc)
        assert tr("watch_suggest_vod_header", loc)
        assert tr(
            "watch_suggest_vod_item",
            loc,
            n=1,
            display="A",
            login="a",
            title="t",
            game="g",
            duration="1h",
            url="https://twitch.tv/videos/1",
        )
        assert tr("watch_create_alerts", loc)
        assert tr("watch_create_alerts_dup", loc)
        assert tr("edit_watch_locked", loc)
        assert tr("import_mode_prompt", loc)
        assert tr("sync_menu_off", loc)
        assert tr("sync_unfollow_ask", loc, list="@x")
        assert tr("sync_unfollow_yes", loc)
        assert tr("sync_unfollow_no", loc)
        assert tr("image_ask", loc)
        assert tr("image_game_cover", loc)
        assert tr("image_game_cover_note", loc)
        assert tr("sub_list_image_game_cover", loc)
        assert tr("edit_image", loc)
        assert tr("edit_image_prompt", loc)
        assert tr("edit_image_delete", loc)
        assert tr("edit_image_keep", loc)
        assert tr("schedule_reminder_prompt", loc)
        assert tr("schedule_reminder_minutes_prompt", loc)
        assert tr("schedule_live_add_prompt", loc)
        assert tr("setup_schedule_only_done", loc, sub_id=1, twitch_username="x", schedule_reminder_note="r", dest="d", thread_note="")
        assert tr("alert_type_prompt", loc)
        assert tr("alert_type_live", loc)
        assert tr("alert_type_category", loc)
        assert tr("alert_type_upcoming", loc)
        assert tr("alert_type_end", loc)
        assert tr("edit_type_pick", loc)
        assert tr("list_type_pick", loc)
        assert tr("sub_list_share", loc)
        assert tr("share_friend_prompt", loc, link="https://t.me/bot?start=share_x")
        assert tr("share_offer", loc, username="x", alert_type="live")
        assert tr("share_accept", loc)
        assert tr("share_decline", loc)
        assert tr("share_created", loc, sub_id=1, username="x")
        assert tr("share_created_paused", loc, limit=5)
        assert tr(
            "premium_enable_need_feature",
            loc,
            feature=tr("premium_feat_alert_types", loc),
        )
        assert tr("share_declined", loc)
        assert tr("share_invalid", loc)
        assert tr("beta_feat_share_alerts", loc)
        assert tr("beta_feat_share_alerts_desc", loc)
        assert tr("delete_type_pick", loc)
        assert tr("delete_all", loc)
        assert tr("delete_all_confirm", loc)
        assert tr("delete_all_yes", loc)
        assert tr("delete_all_no", loc)
        assert tr("alert_type_no_schedule", loc)
        assert tr("alert_note_live", loc, twitch_username="x")
        assert tr("alert_note_category", loc, twitch_username="x")
        assert tr("alert_note_end", loc, twitch_username="x")
        assert tr("delete_old_text_category", loc)
        assert tr("delete_sibling_text", loc)
        assert tr("sub_list_alert_category", loc)
        assert tr("edit_delete_other", loc)
        assert tr("edit_delete_other_menu", loc)
        assert tr("edit_delete_old_menu_category", loc)
        assert tr("edit_change_type", loc)
        assert tr("edit_copy", loc)
        assert tr("edit_copy_change", loc)
        assert tr("edit_type_changed", loc, alert_type="x")
        assert tr("edit_copied", loc, sub_id=1, username="x")
        assert tr("edit_copy_cancelled", loc)
        assert tr("sub_list_delete_other_yes", loc)
        assert tr("sub_list_delete_other_no", loc)
        assert tr("btn_demo", loc)
        assert tr("demo_on", loc)
        assert tr("demo_off", loc)
        done = tr(
            "setup_done",
            loc,
            sub_id=1,
            twitch_username="x",
            image_note="",
            ignore_keywords_note="",
            preview_note="",
            delay_note="",
            repeat_note="",
            schedule_reminder_note="",
            dest="d",
            thread_note="",
            delete_note="",
            delete_fail_note="",
            alert_note="ALERT_NOTE_OK",
        )
        assert "ALERT_NOTE_OK" in done
        assert tr("edit_schedule_reminder", loc)
        assert tr("edit_schedule_reminder_no_schedule", loc)
        assert tr("channel_dup_prompt", loc)
        found = tr(
            "channel_found",
            loc,
            display_name="x",
            placeholders_link="LINK",
        )
        assert "LINK" in found
        assert "{username}" in found or "{{username}}" not in found
        assert "Дополнительно" in found or loc != "ru"
        assert "Extras" in found or loc != "en"
        assert "очистка названия" in found.lower() or "clean title" in found.lower()
        assert "x в эфире с игрой Just Chatting. Тестовый стрим" in found or loc != "ru"
        assert "x is live with Just Chatting. Test stream" in found or loc != "en"
        assert tr("advanced_options_strip", loc)
        assert "Очистка названия" in tr("advanced_options_hint_strip", "ru")
        assert "Clean title" in tr("advanced_options_hint_strip", "en")
        edit_tpl = tr(
            "edit_template_prompt",
            loc,
            sub_id=1,
            placeholders_link="LINK",
            current="{username} live",
            preview="marfapr live",
        )
        assert "LINK" in edit_tpl
        assert "{username} live" in edit_tpl
        assert "marfapr live" in edit_tpl
        assert "Current format" in edit_tpl or "Текущий формат" in edit_tpl
        assert "How it will look" in edit_tpl or "Как будет выглядеть" in edit_tpl
        assert "Очистка названия" not in edit_tpl and "Clean title" not in edit_tpl
        feedback = tr("feedback", loc, github="https://example.com", user_id=42)
        assert "42" in feedback
        assert "<code>42</code>" in feedback
        assert "bot_version" not in feedback
        assert "Версия бота" not in feedback
        assert "Bot version" not in feedback

    from i18n import welcome_demo_keyboard

    kb = welcome_demo_keyboard("ru", 42)
    assert kb.inline_keyboard[0][0].callback_data == "edit:42"
    assert kb.inline_keyboard[1][0].callback_data == "welcome_del:42"

    from db.models import migrate_sub_fields_for_alert_type

    base = {
        "dest_type": "channel",
        "delete_previous": True,
        "notify_delete_fail": True,
        "delete_other_alerts": True,
        "suppress_repeat_minutes": 15,
        "schedule_reminder_minutes": 30,
        "schedule_reminder_configured": True,
        "delay_minutes": 5,
        "notify_on_live": True,
        "notify_on_end": False,
        "notify_on_category_change": False,
    }
    cat = migrate_sub_fields_for_alert_type(base, "category")
    assert cat["notify_on_category_change"] is True
    assert cat["suppress_repeat_minutes"] == 0
    assert cat["schedule_reminder_minutes"] == 0
    upcoming = migrate_sub_fields_for_alert_type(base, "upcoming")
    assert upcoming["delay_minutes"] == 0
    assert upcoming["suppress_repeat_minutes"] == 0

    # message_fx: typing + draft stream with classic fallback
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from message_fx import (
        growing_prefixes,
        install_message_fx,
        message_fx_disabled,
        stream_draft_then,
        _plain_for_draft,
    )

    assert growing_prefixes("short") == []
    prefs = growing_prefixes("alpha bravo charlie delta echo foxtrot golf hotel")
    assert prefs and all(prefs[i] in prefs[i + 1] for i in range(len(prefs) - 1))
    assert _plain_for_draft("<b>Hi</b> &amp; you", parse_mode="HTML") == "Hi & you"

    class _FrozenBot:
        """Mimic PTB ExtBot: instance attrs cannot be assigned normally."""

        def __setattr__(self, key, value):
            raise AttributeError(
                f"Attribute `{key}` of class `{type(self).__name__}` can't be set!"
            )

    frozen = _FrozenBot()
    object.__setattr__(frozen, "send_message", AsyncMock(return_value="ok"))
    object.__setattr__(frozen, "send_chat_action", AsyncMock(return_value=True))
    object.__setattr__(
        frozen, "send_message_draft", AsyncMock(side_effect=RuntimeError("no draft"))
    )
    # ExtBot is not weak-referenceable; prefs must use id(bot), not WeakKeyDictionary.
    class _NoWeak:
        __slots__ = ()

    no_weak = _NoWeak()
    from message_fx import _draft_on_for, _remember_draft_pref

    _remember_draft_pref(no_weak, lambda _uid: False)
    assert _draft_on_for(no_weak, 42) is False
    _remember_draft_pref(no_weak, lambda _uid: True)
    assert _draft_on_for(no_weak, 42) is True

    bot = MagicMock()
    original_send = AsyncMock(return_value="ok")
    bot.send_message = original_send
    bot.send_chat_action = AsyncMock(return_value=True)
    bot.send_message_draft = AsyncMock(side_effect=RuntimeError("no draft"))
    bot._message_fx_installed = False
    install_message_fx(bot)

    async def _fx_roundtrip() -> None:
        # Draft fails → still classic send after typing.
        assert await bot.send_message(42, "x" * 80) == "ok"
        bot.send_chat_action.assert_awaited()
        bot.send_message_draft.assert_awaited()
        assert original_send.await_count == 1

        bot.send_chat_action.reset_mock()
        bot.send_message_draft.reset_mock()
        original_send.reset_mock()
        with message_fx_disabled():
            await bot.send_message(42, "x" * 80)
        bot.send_chat_action.assert_not_awaited()
        bot.send_message_draft.assert_not_awaited()
        assert original_send.await_count == 1

        # Preference off: classic only (no typing/draft).
        bot.send_chat_action.reset_mock()
        bot.send_message_draft.reset_mock()
        original_send.reset_mock()
        bot2 = MagicMock()
        orig2 = AsyncMock(return_value="ok")
        bot2.send_message = orig2
        bot2.send_chat_action = AsyncMock()
        bot2.send_message_draft = AsyncMock()
        bot2._message_fx_installed = False
        install_message_fx(bot2, draft_enabled=lambda _uid: False)
        await bot2.send_message(42, "x" * 80)
        bot2.send_chat_action.assert_not_awaited()
        bot2.send_message_draft.assert_not_awaited()
        assert orig2.await_count == 1

        # Group chats: classic only.
        bot.send_chat_action.reset_mock()
        await bot.send_message(-1001, "x" * 80)
        bot.send_chat_action.assert_not_awaited()

        ok = await stream_draft_then(bot, 42, "plain " * 20)
        assert ok is False  # mock still raises

        bot2 = MagicMock()
        bot2.send_message_draft = AsyncMock(return_value=True)
        assert await stream_draft_then(bot2, 42, "plain " * 20) is True
        assert bot2.send_message_draft.await_count >= 1

    asyncio.run(_fx_roundtrip())

