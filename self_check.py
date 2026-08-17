"""ponytail: minimal self-check for twitch parsing and templates."""
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
    _message_link,
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
from premium import FEATURE_IDS
from hf_text import _normalize_template
from telegram import LinkPreviewOptions, Message


def main() -> None:
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_TOKEN_FOR_SELF_CHECK")
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

    from twitch import strip_name_mentions_and_commands

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
    assert parse_posthog_issue_payload({"event": {"event": "$pageview"}}) is None
    assert TwitchClient.is_one_off_schedule_forbidden(
        Exception("403 Client Error: single segment creation not authorized for url: x")
    )
    assert not TwitchClient.is_one_off_schedule_forbidden(Exception("rate limit exceeded"))
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
    assert tr("stream_schedule_duration_prompt", "ru")
    assert tr("stream_schedule_duration_unsure", "en")
    from i18n import (
        main_menu,
        admin_menu,
        other_menu,
        settings_menu,
        stream_schedule_duration_keyboard,
        watch_suggest_keyboard,
    )

    for loc in ("en", "ru"):
        main_kb = main_menu(loc).keyboard
        main_btns = [b.text for row in main_kb for b in row]
        assert btn("other", loc) in main_btns
        assert btn("alert_history", loc) in main_btns
        assert btn("create_schedule", loc) not in main_btns
        assert btn("watch", loc) not in main_btns
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
            [btn("watch", loc), btn("back", loc)],
        ]
        settings_kb = settings_menu(loc).keyboard
        ignored_row = next(
            row
            for row in settings_kb
            if btn("ignored_words", loc) in [b.text for b in row]
        )
        assert [b.text for b in ignored_row] == [
            btn("ignored_words", loc),
            btn("advanced_mode", loc),
        ]
        partner_row = next(
            row
            for row in settings_kb
            if btn("partner", loc) in [b.text for b in row]
        )
        assert [b.text for b in partner_row] == [
            btn("language", loc),
            btn("partner", loc),
        ]
        from i18n import beta_mode_btn

        beta_row = next(
            row
            for row in settings_kb
            if any(
                (b.text or "").startswith(btn("beta_mode", loc)) for b in row
            )
        )
        assert [b.text for b in beta_row][0] == beta_mode_btn(loc, 0, 0)
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
        admin_btns = [b.text for row in admin_menu(loc).keyboard for b in row]
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
    streams = {"1": {"game_id": "111"}}
    assert category_change_events(games, ["1"], streams, primed=False) == []
    assert games == {"1": "111"}
    assert category_change_events(games, ["1"], streams, primed=True) == []
    streams = {"1": {"game_id": "222"}}
    assert category_change_events(games, ["1"], streams, primed=True) == ["1"]
    assert games["1"] == "222"
    assert category_change_events(games, ["1"], {}, primed=True) == []
    assert "1" not in games
    streams = {"1": {"game_id": "222"}}
    assert category_change_events(games, ["1"], streams, primed=True) == []
    assert games["1"] == "222"

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
        assert tr("watch_cats_prompt", loc, max=5)
        assert tr("watch_cats_lucky", loc)
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
        assert tr("lucky_btn", loc)
        assert tr("lucky_hint", loc)
        assert tr("image_ask", loc)
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
        assert tr("delete_type_pick", loc)
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
        assert "Изображение можно добавить" in found or loc != "ru"
        assert "You can add an image" in found or loc != "en"
        assert "Очистка названия" in found or "Clean title" in found
        assert "x в эфире с игрой Just Chatting. Тестовый стрим" in found or loc != "ru"
        assert "x is live with Just Chatting. Test stream" in found or loc != "en"
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
        assert "Очистка названия" in edit_tpl or "Clean title" in edit_tpl
        feedback = tr("feedback", loc, github="https://example.com", user_id=42)
        assert "42" in feedback
        assert "<code>42</code>" in feedback
        assert "bot_version" not in feedback
        assert "Версия бота" not in feedback
        assert "Bot version" not in feedback

    normalized = _normalize_template("Streamer online!")
    assert "{username}" in normalized
    assert "{game}" in normalized
    assert "{name}" in normalized
    assert _normalize_template("{username}\n{game}\n{name}") == "{username}\n{game}\n{name}"
    assert "{name}" in _normalize_template("live!\nТестовый стрим")
    assert "Тестовый" not in _normalize_template("live!\nТестовый стрим")
    assert "Test stream" not in _normalize_template("hi\nTest stream")

    from hf_text import _local_template, generate_alert_template
    from config import GROQ_TEXT_MODEL
    assert GROQ_TEXT_MODEL == "openai/gpt-oss-20b" or os.getenv("GROQ_TEXT_MODEL", "").strip()
    local_ru = _local_template("ru")
    assert "{username}" in local_ru and "{game}" in local_ru and "{name}" in local_ru
    # With no cloud tokens, generation must still return a template via fallback.
    import os as _os
    _prev = {
        k: _os.environ.get(k)
        for k in ("HF_TOKEN", "HUGGING_FACE_API", "GROQ_API_KEY", "GROQ_API", "GROK_API")
    }
    for k in _prev:
        _os.environ[k] = ""
    try:
        fallback = generate_alert_template(locale="ru", channel="marfapr")
        assert "{username}" in fallback and "{name}" in fallback
    finally:
        for k, v in _prev.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v

    with tempfile.TemporaryDirectory() as tmp:
        db = SqliteDatabase(Path(tmp) / "test.db")
        db.upsert_user(1)
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
        assert imported == 1 and skipped == 1 and limited == 0 and removed == 0
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
        assert imported2 == 0 and skipped2 == 1 and removed2 == 2  # newbie + synced
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
        assert removed_edit == 0
        assert any(s["user_id"] == "666" for s in ask_edit)
        assert db.get_subscription(edited_id, 1) is not None
        db.delete_subscriptions_for_twitch_users(1, {"666"})
        assert db.get_subscription(edited_id, 1) is None
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
        assert db.get_global_ignore_keywords(1) == ""
        db.set_global_ignore_keywords(1, "irl, chatting")
        assert db.get_global_ignore_keywords(1) == "irl, chatting"
        db.set_global_ignore_keywords(1, "")
        assert db.get_global_ignore_keywords(1) == ""
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
        db.upsert_user(1)
        assert db.is_bot_blocked(1) is False
        restored = db.get_bot_stats()
        assert restored.users == 1
        assert restored.subscriptions_total == 6
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
        with db._conn() as conn:
            left = conn.execute(
                "SELECT COUNT(*) AS c FROM scheduled_broadcasts WHERE id = ?",
                (bid2,),
            ).fetchone()["c"]
        assert left == 0
        assert not db.update_subscription(999, 1, message_template="x")

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
    from config import FREE_CHAT_ID

    assert FREE_CHAT_ID == -1002155969539
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

    from bot import _extract_forward_chat, _parse_dest_input

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
    # Chat fixes: callback wiring, PTB Stars typo, cancel copy, deploy polling, Other audience.
    import inspect
    from pathlib import Path as _Path

    from telegram import Bot

    import main as main_mod
    from bot import _dump_broadcast_recipient_ids
    from i18n import admin_other_audience_keyboard, premium_owned_keyboard
    from premium_handlers import _premium_markup

    bot_src = _Path(__file__).resolve().parent.joinpath("bot.py").read_text(
        encoding="utf-8"
    )
    assert "cancel_feat:.+" in bot_src
    assert "owned|" in bot_src
    # Bot API has no getForumTopic; 404 is mapped to PTB InvalidToken.
    assert "getForumTopic" not in bot_src
    # Edit ignore-keywords: single inline Cancel (no reply pulse / no junk carrier).
    edit_ignore_chunk = bot_src.split("async def start_edit_ignore_keywords", 1)[1].split(
        "async def receive_edit_ignore_keywords", 1
    )[0]
    assert "as_cancel=True" in edit_ignore_chunk
    assert "_pulse_wizard_keyboard" not in edit_ignore_chunk
    assert "edit_message_reply_markup" not in edit_ignore_chunk
    assert "_wizard(" not in edit_ignore_chunk
    # Create ignore-keywords: inline Back/Cancel (no reply pulse).
    create_ignore_chunk = bot_src.split("async def _go_ignore_keywords_prompt", 1)[1].split(
        "async def _go_link_preview_prompt", 1
    )[0]
    assert "show_back=True" in create_ignore_chunk
    assert "show_cancel=True" in create_ignore_chunk
    assert "_pulse_wizard_keyboard" not in create_ignore_chunk
    assert 'ignore_keywords:back"' in bot_src or "ignore_keywords:back$" in bot_src
    assert "receive_ignore_keywords_back" in bot_src
    assert "drop_pending_updates=False" in inspect.getsource(main_mod.main)
    assert "mark_ready()" in bot_src
    ptb_edit = inspect.getsource(Bot.edit_user_star_subscription)
    assert "editUserStarSubscription" in ptb_edit
    assert "editUserStartSubscription" not in ptb_edit
    ph_src = _Path(__file__).resolve().parent.joinpath(
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
    assert aud_cb == {"admin_audience:ids", "admin_audience:all"}
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
        db.upsert_user(2)
        assert _premium_markup(db, 2, "ru", free_chat=True, force_free=False) is None
    assert tr("premium_title", "ru", free_limit=5, stars=100, channel="marfapr", status="s")
    assert tr("btn_premium", "en")
    assert tr("btn_premium_oferta", "ru") == "Оферта"
    assert "Докучаев" in tr(
        "oferta_page_body",
        "ru",
        free_limit=5,
        trial_days=7,
        channel="marfapr",
        month_stars=100,
        month_rub="200",
        year_stars=1000,
        year_rub="2 000",
        life_stars=2000,
        life_rub="4 000",
        feat_stars=20,
        feat_rub="40",
        rub_per_star=2,
    )
    from unittest.mock import patch

    from i18n import with_premium_oferta

    assert with_premium_oferta("en", None) is None
    with patch("config.PUBLIC_BASE_URL", "https://example.com"):
        oferta_kb = with_premium_oferta("ru", None)
        assert oferta_kb is not None
        assert oferta_kb.inline_keyboard[-1][0].url == "https://example.com/oferta"
        assert oferta_kb.inline_keyboard[-1][0].text == "Оферта"
    from health import _oferta_page

    assert b"760403963548" in _oferta_page()
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
        # Оферта остаётся и у премиум (free-chat / permanent → markup None).
        with patch("config.PUBLIC_BASE_URL", "https://example.com"):
            prem_only = with_premium_oferta(
                "ru",
                _premium_markup(db, 2, "ru", free_chat=True, force_free=False),
            )
            assert prem_only is not None
            assert len(prem_only.inline_keyboard) == 1
            assert prem_only.inline_keyboard[0][0].url == "https://example.com/oferta"
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
        ok2, reason2 = start_trial(db, 50)
        assert not ok2 and reason2 == "active"
        # Force expire
        db.set_premium_trial(50, until_unix=1, used=True)
        assert prem.ensure_trial_expired(db, 50) is True
        sub = db.get_subscription(sid, 50)
        assert sub is not None
        assert sub.enabled is False
        assert sub.trial_paused is True
        assert prem.is_live_only_alert(sub)
        ok3, reason3 = start_trial(db, 50)
        assert not ok3 and reason3 == "used"
        db.upsert_user(51)
        apply_lifetime_payment(db, 51, charge_id="life1", stars_paid=2000)
        assert prem.get_status(db, 51).permanent
        db.upsert_user(52)
        apply_features_payment(
            db,
            52,
            feature_ids=["delay", "repeat"],
            charge_id="feat1",
            until_unix=10**12,
            stars_paid=40,
        )
        st52 = prem.get_status(db, 52)
        assert st52.feature_active("delay")
        assert st52.feature_active("repeat")
        assert not st52.feature_active("twitch_sync")
        assert st52.is_premium
        assert not st52.has_full_plan
        assert st52.feature_charge_id("delay") == "feat1"
        assert prem.has_feature_sync(db, 52, "delay")
        assert not prem.has_feature_sync(db, 52, "twitch_sync")
        db.clear_premium_feature(52, "delay")
        assert not prem.get_status(db, 52).feature_active("delay")
        assert prem.get_status(db, 52).feature_active("repeat")

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
        assert tr("btn_alert_history_more", "ru") == "Показать больше"
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
        assert _alert_history_nav_keyboard("ru", 0, 1, show_more=False) is None
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
        assert tr("weekly_new_users", "ru", count=1, paid=2)
        assert "настройках" in tr("broadcast_footer", "ru", type="x")
        assert "Settings" in tr("broadcast_footer", "en", type="x")
        assert tr("broadcast_type_other", "ru") == "📢 Прочие"
        assert tr("broadcast_type_other", "en") == "📢 Other"
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
    assert "status.twitch.com" in msg_ru
    msg_ok = _format_twitch_status_message("en", status_ok)
    assert "All Systems Operational" in msg_ok
    assert tr("broadcast_started", "ru")
    assert "status.twitch.com" in tr("sys_notifications_menu", "ru")

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

    assert btn("beta_mode", "ru") == "🧪 Бета-режим"
    assert btn("beta_mode", "en") == "🧪 Beta mode"
    from i18n import beta_mode_btn, is_menu_button

    assert beta_mode_btn("ru", 0, 0) == "🧪 Бета-режим (0/0)"
    assert beta_mode_btn("en", 1, 3) == "🧪 Beta mode (1/3)"
    assert is_menu_button(beta_mode_btn("ru", 2, 5))
    assert not is_menu_button("not a menu button")

    print("ok")


if __name__ == "__main__":
    main()
