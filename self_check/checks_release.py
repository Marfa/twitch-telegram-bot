"""Release alerts: prefs, free entitlement, active cap, keyboard."""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import premium as prem
from db import open_database
from db.models import (
    ReleasePlatformPref,
    ReleaseWatchPrefs,
    alert_type_from_payload,
    dump_release_watch_prefs,
    is_release_watch_sub,
    parse_release_watch_prefs,
    release_platform_key,
)
from handlers.release_watch import create_release_subscription
from i18n import alert_type_keyboard, t


def _check_release_prefs_roundtrip() -> None:
    prefs = ReleaseWatchPrefs(
        igdb_game_id=42,
        game_name="Demo Game",
        days_before=7,
        platforms=[
            ReleasePlatformPref(6, "PC", 2_000_000_000, "2023"),
            ReleasePlatformPref(48, "PS4", 2_100_000_000, ""),
        ],
        notified_keys=["6:2000000000"],
    )
    raw = dump_release_watch_prefs(prefs)
    back = parse_release_watch_prefs(raw)
    assert back is not None
    assert back.igdb_game_id == 42
    assert back.days_before == 7
    assert len(back.platforms) == 2
    assert back.notified_keys == ["6:2000000000"]
    assert back.date_unknown is False
    assert release_platform_key(6, 2_000_000_000) == "6:2000000000"
    assert alert_type_from_payload({"release_watch_prefs": raw}) == "release"
    assert is_release_watch_sub(SimpleNamespace(release_watch_prefs=raw))
    assert prem.is_live_only_alert(SimpleNamespace(release_watch_prefs=raw))

    unknown = ReleaseWatchPrefs(
        igdb_game_id=7,
        game_name="TBA",
        days_before=3,
        platforms=[],
        date_unknown=True,
    )
    uraw = dump_release_watch_prefs(unknown)
    uback = parse_release_watch_prefs(uraw)
    assert uback is not None
    assert uback.date_unknown is True
    assert uback.platforms == []


def _check_release_pick_disambiguates() -> None:
    from handlers.release_watch import release_game_pick_keyboard
    from search_normalize import normalize_search_query, search_tokens

    assert search_tokens("Worms: Galactic Tactics") == [
        "worms",
        "galactic",
        "tactics",
    ]
    assert normalize_search_query("Worms: Galactic Tactics") == (
        "worms galactic tactics"
    )
    games = [
        {"id": 1, "name": "The CUBE"},
        {"id": 2, "name": "The CUBE"},
        {"id": 3, "name": "Other"},
    ]
    kb = release_game_pick_keyboard(
        games, "en", developers={1: "Studio A", 2: "Studio B"}
    )
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert "The CUBE (Studio A)" in labels
    assert "The CUBE (Studio B)" in labels
    assert "Other" in labels


def _check_release_search_punct() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = open_database(Path(tmp) / "bot.db")
        with db._conn() as conn:
            conn.execute(
                "INSERT INTO igdb_games (id, name, summary) VALUES (?, ?, ?)",
                (9, "Worms Galactic Tactics", ""),
            )
            conn.commit()
        hits = db.igdb_search_games_by_name("Worms: Galactic Tactics", limit=5)
        assert any(h["id"] == 9 for h in hits)


def _check_release_keyboard() -> None:
    hidden = alert_type_keyboard("en")
    assert not any(
        (b.callback_data or "") == "alert_type:release"
        for r in hidden.inline_keyboard
        for b in r
    )
    shown = alert_type_keyboard("en", show_release=True)
    cbs = [b.callback_data for r in shown.inline_keyboard for b in r]
    assert "alert_type:release" in cbs
    assert cbs.index("alert_type:game") < cbs.index("alert_type:release")
    assert t("alert_type_release", "ru")
    assert t("alert_type_release", "en")
    assert t("release_date_unknown", "ru")
    assert t("release_subscribed_unknown_date", "en")
    assert t("sub_list_release_date_unknown", "ru")
    assert "выход релизов" in t("alert_type_prompt", "ru").lower()
    assert "release alerts" in t("alert_type_prompt", "en").lower()
    assert t("release_game_searching", "ru")
    assert t("sub_list_release_dates", "en", dates="x")
    assert t("release_find_streams", "ru")
    assert t("release_find_streams_none", "en", game="X")


def _check_release_date_backfill() -> None:
    from handlers.release_watch import _refresh_platforms_from_db

    with tempfile.TemporaryDirectory() as tmp:
        db = open_database(Path(tmp) / "bot.db")
        prefs = ReleaseWatchPrefs(
            igdb_game_id=555,
            game_name="Soon",
            days_before=1,
            platforms=[],
            date_unknown=True,
        )
        assert _refresh_platforms_from_db(db, prefs) == []
        with db._conn() as conn:
            conn.execute(
                "INSERT INTO igdb_platforms (id, name) VALUES (?, ?)",
                (6, "PC"),
            )
            conn.execute(
                """
                INSERT INTO igdb_release_dates (id, game_id, platform_id, date, human)
                VALUES (?, ?, ?, ?, ?)
                """,
                (1, 555, 6, 2_500_000_000, "2030"),
            )
            conn.commit()
        filled = _refresh_platforms_from_db(db, prefs)
        assert len(filled) == 1
        assert filled[0].platform_id == 6
        assert filled[0].date == 2_500_000_000
        assert filled[0].platform_name == "PC"


def _check_release_active_cap() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = open_database(Path(tmp) / "bot.db")
        uid = 900_001
        db.upsert_user(uid)
        # 5 enabled live alerts → free cap full
        for i in range(5):
            db.add_subscription(
                owner_id=uid,
                twitch_username=f"ch{i}",
                twitch_user_id=f"tw{i}",
                message_template="hi",
                dest_type="dm",
                chat_id=uid,
                thread_id=None,
                enabled=True,
                notify_on_live=True,
            )
        assert prem.may_enable_subscription(db, uid) is False
        prefs = ReleaseWatchPrefs(
            igdb_game_id=99,
            game_name="Cap Game",
            days_before=0,
            platforms=[ReleasePlatformPref(6, "PC", int(time.time()) + 86400 * 30)],
        )

        async def _run() -> None:
            # Beta gate: enroll user so create is allowed past beta check
            import beta as beta_features
            from handlers.release_watch import RELEASE_BETA_ID

            db.set_beta_enrollment(uid, RELEASE_BETA_ID, True)
            assert beta_features.is_enabled(db, uid, RELEASE_BETA_ID)
            bot = AsyncMock()
            sub, status = await create_release_subscription(
                bot, db, uid, "en", prefs=prefs
            )
            assert status == "release_subscribed_paused"
            assert sub is not None
            assert sub.enabled is False
            assert is_release_watch_sub(sub)

        asyncio.run(_run())


def _check_release_early_dup_stops_wizard() -> None:
    """Existing release alert: show edit/continue, do not advance to days."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from db.models import ReleaseWatchPrefs, dump_release_watch_prefs
    from handlers.release_watch import _wz, receive_release_pick

    uid = 910_001
    game_id = 4242

    with tempfile.TemporaryDirectory() as tmp:
        db = open_database(Path(tmp) / "rel_dup.db")
        db.upsert_user(uid)
        prefs = ReleaseWatchPrefs(
            igdb_game_id=game_id,
            game_name="Dup Game",
            days_before=3,
            platforms=[],
            date_unknown=True,
        )
        db.add_subscription(
            owner_id=uid,
            twitch_username="dupgame",
            twitch_user_id=f"rel:{uid}:x",
            message_template="t",
            dest_type="dm",
            chat_id=uid,
            thread_id=None,
            release_watch_prefs=dump_release_watch_prefs(prefs),
        )
        game = {"id": game_id, "name": "Dup Game", "summary": ""}
        db.igdb_game_by_id = MagicMock(return_value=game)

        async def _run() -> None:
            bot = AsyncMock()
            application = MagicMock()
            application.bot_data = {"db": db}
            query = AsyncMock()
            query.data = f"rel:pick:{game_id}"
            query.from_user = SimpleNamespace(id=uid)
            query.message = SimpleNamespace(chat_id=uid)
            query.edit_message_text = AsyncMock()
            query.answer = AsyncMock()
            update = MagicMock()
            update.callback_query = query
            update.effective_user = SimpleNamespace(id=uid)
            update.effective_chat = SimpleNamespace(id=uid)
            ctx = MagicMock()
            ctx.application = application
            ctx.bot = bot
            ctx.user_data = {}
            state = await receive_release_pick(update, ctx)
            assert state == _wz()["RELEASE_DUP"]
            assert ctx.user_data.get("alert_dup_force", {}).get("kind") == "release_wizard"
            query.edit_message_text.assert_awaited()
            markup = query.edit_message_text.await_args.kwargs.get("reply_markup")
            assert markup is not None
            cbs = {
                (b.callback_data or "")
                for row in markup.inline_keyboard
                for b in row
            }
            assert any(c.startswith("alert_dup:edit:") for c in cbs)
            assert "alert_dup:continue" in cbs
            bot.send_message.assert_not_awaited()

        asyncio.run(_run())


def _check_game_alert_dedup_by_category() -> None:
    """Same Twitch category → dup even if filters differ."""
    import asyncio
    from unittest.mock import AsyncMock

    from db.models import WatchPrefs
    from handlers.watch import create_category_watch_subscription

    uid = 910_002
    with tempfile.TemporaryDirectory() as tmp:
        db = open_database(Path(tmp) / "game_dup.db")
        db.upsert_user(uid)

        async def _run() -> None:
            bot = AsyncMock()
            first = WatchPrefs(
                categories=[{"id": "509658", "name": "Just Chatting"}],
                min_viewers=0,
                max_viewers=None,
                language=None,
                tags=[],
                exclude_mature=True,
            )
            _text, sub, status = await create_category_watch_subscription(
                bot, db, uid, "en", first
            )
            assert status == "watch_create_alerts_ok"
            assert sub is not None
            second = WatchPrefs(
                categories=[{"id": "509658", "name": "Just Chatting"}],
                min_viewers=100,
                max_viewers=None,
                language="ru",
                tags=["fps"],
                exclude_mature=False,
            )
            _text2, existing, status2 = await create_category_watch_subscription(
                bot, db, uid, "en", second
            )
            assert status2 == "watch_create_alerts_dup"
            assert existing is not None
            assert existing.id == sub.id

        asyncio.run(_run())


def _check_game_alert_early_dup_after_category() -> None:
    """Alert mode: dup right after category pick, before filters."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from db.models import WatchPrefs, dump_category_watch_prefs
    from handlers.watch import _add_watch_category, _ws

    uid = 910_003
    with tempfile.TemporaryDirectory() as tmp:
        db = open_database(Path(tmp) / "game_early.db")
        db.upsert_user(uid)
        prefs = WatchPrefs(
            categories=[{"id": "509658", "name": "Just Chatting"}],
            min_viewers=0,
            max_viewers=None,
            language=None,
            tags=[],
            exclude_mature=True,
        )
        db.add_subscription(
            owner_id=uid,
            twitch_username="jc",
            twitch_user_id=f"cw:{uid}:x",
            message_template="t",
            dest_type="dm",
            chat_id=uid,
            thread_id=None,
            category_watch_prefs=dump_category_watch_prefs(prefs),
            from_watch_suggest=True,
        )

        async def _run() -> None:
            bot = AsyncMock()
            application = MagicMock()
            application.bot_data = {"db": db}
            update = MagicMock()
            update.callback_query = None
            update.effective_user = SimpleNamespace(id=uid)
            update.effective_message = MagicMock()
            update.effective_message.reply_text = AsyncMock()
            update.effective_message.chat_id = uid
            ctx = MagicMock()
            ctx.application = application
            ctx.bot = bot
            ctx.user_data = {
                "watch_create_alert": True,
                "watch_categories": [],
            }
            state = await _add_watch_category(
                update,
                ctx,
                "en",
                {"id": "509658", "name": "Just Chatting"},
            )
            assert state == _ws()["WATCH_DUP"]
            assert ctx.user_data.get("alert_dup_force", {}).get("kind") == "game_wizard"
            update.effective_message.reply_text.assert_awaited()
            markup = update.effective_message.reply_text.await_args.kwargs.get(
                "reply_markup"
            )
            assert markup is not None
            cbs = {
                (b.callback_data or "")
                for row in markup.inline_keyboard
                for b in row
            }
            assert any(c.startswith("alert_dup:edit:") for c in cbs)
            assert "alert_dup:continue" in cbs

        asyncio.run(_run())


def run() -> None:
    _check_release_prefs_roundtrip()
    _check_release_pick_disambiguates()
    _check_release_search_punct()
    _check_release_keyboard()
    _check_release_date_backfill()
    _check_release_active_cap()
    _check_release_early_dup_stops_wizard()
    _check_game_alert_dedup_by_category()
    _check_game_alert_early_dup_after_category()
