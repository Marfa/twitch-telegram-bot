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
        games, "en", companies={1: "Studio A", 2: "Studio B"}
    )
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert "The CUBE (Studio A)" in labels
    assert "The CUBE (Studio B)" in labels
    assert "Other" in labels
    # No company → plain name even when duplicated.
    kb2 = release_game_pick_keyboard(games, "en", companies={1: "Studio A"})
    labels2 = [b.text for row in kb2.inline_keyboard for b in row]
    assert "The CUBE (Studio A)" in labels2
    assert "The CUBE" in labels2


def _check_release_pick_pagination() -> None:
    from handlers.release_watch import release_game_pick_keyboard

    games = [{"id": i, "name": f"Game {i}"} for i in range(12)]
    kb0 = release_game_pick_keyboard(games, "en", page=0)
    texts0 = [b.text for row in kb0.inline_keyboard for b in row]
    cbs0 = [b.callback_data for row in kb0.inline_keyboard for b in row]
    assert "Game 0" in texts0
    assert "Game 4" in texts0
    assert "Game 5" not in texts0
    assert "1/3" in texts0
    assert "rel:page:1" in cbs0
    assert "rel:page:noop" in cbs0
    kb1 = release_game_pick_keyboard(games, "en", page=1)
    texts1 = [b.text for row in kb1.inline_keyboard for b in row]
    cbs1 = [b.callback_data for row in kb1.inline_keyboard for b in row]
    assert "Game 5" in texts1
    assert "Game 9" in texts1
    assert "2/3" in texts1
    assert "rel:page:0" in cbs1
    assert "rel:page:2" in cbs1
    # Duplicate names across pages still get company labels.
    dupes = (
        [{"id": 1, "name": "The Cube"}]
        + [{"id": i, "name": f"Other {i}"} for i in range(2, 7)]
        + [{"id": 99, "name": "The Cube"}]
    )
    kb_dup = release_game_pick_keyboard(
        dupes, "en", companies={1: "A", 99: "B"}, page=0
    )
    assert any(
        b.text == "The Cube (A)" for row in kb_dup.inline_keyboard for b in row
    )
    kb_dup2 = release_game_pick_keyboard(
        dupes, "en", companies={1: "A", 99: "B"}, page=1
    )
    assert any(
        b.text == "The Cube (B)" for row in kb_dup2.inline_keyboard for b in row
    )


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


def _check_release_search_control_prefix() -> None:
    """'Control' prefers Control* titles (newest first), not Air Control."""
    with tempfile.TemporaryDirectory() as tmp:
        db = open_database(Path(tmp) / "bot.db")
        with db._conn() as conn:
            for gid, name, released, rating in (
                (1, "Air Control", 1_700_000_000, 50),
                (2, "Control", 1_566_864_000, 966),
                (3, "Control Craft 2", 1_455_494_400, 1),
                (4, "Control Resonant", 1_790_208_000, 0),
                (5, "Remote Control", 1_800_000_000, 10),
                (6, "Control Room", 1_780_000_000, 0),
                (7, "Control Over", 1_760_000_000, 0),
            ):
                conn.execute(
                    """
                    INSERT INTO igdb_games
                        (id, name, summary, first_release_date, total_rating_count)
                    VALUES (?, ?, '', ?, ?)
                    """,
                    (gid, name, released, rating),
                )
            conn.commit()
        hits = db.igdb_search_games_by_name("Control", limit=5)
        names = [h["name"] for h in hits]
        assert names[0] == "Control"
        assert "Control Resonant" in names
        assert names.index("Control Resonant") == 1  # newest Control*
        assert "Air Control" not in names
        assert "Remote Control" not in names


def _check_release_search_gta_alias() -> None:
    """GTA / GTA 6 / GTA VI resolve to Grand Theft Auto VI via acronym + numerals."""
    with tempfile.TemporaryDirectory() as tmp:
        db = open_database(Path(tmp) / "bot.db")
        with db._conn() as conn:
            for gid, name, released, rating in (
                (10, "GTA Long Night", 1_700_000_000, 8),
                (11, "Grand Theft Auto: Vice City", 1_000_000_000, 3157),
                (12, "Grand Theft Auto VI", 1_795_046_400, 0),
                (13, "Grand Theft Auto V", 1_400_000_000, 5000),
            ):
                conn.execute(
                    """
                    INSERT INTO igdb_games
                        (id, name, summary, first_release_date, total_rating_count)
                    VALUES (?, ?, '', ?, ?)
                    """,
                    (gid, name, released, rating),
                )
            conn.commit()
        for q in ("GTA", "GTA 6", "GTA VI"):
            hits = db.igdb_search_games_by_name(q, limit=5)
            names = [h["name"] for h in hits]
            assert "Grand Theft Auto VI" in names, q
        # Numeral query should not pull Vice City (vi ⊂ vice).
        hits6 = db.igdb_search_games_by_name("GTA VI", limit=5)
        assert all("Vice" not in h["name"] for h in hits6)


def _check_release_search_exact_duplicates() -> None:
    """Exact-title duplicates are all returned (Mundfish The Cube case)."""
    with tempfile.TemporaryDirectory() as tmp:
        db = open_database(Path(tmp) / "bot.db")
        with db._conn() as conn:
            for gid, name in (
                (1, "The Cube"),
                (2, "The Cube"),
                (3, "The Cube"),
                (4, "The Cube"),
                (5, "The Cube"),
                (347640, "The Cube"),
                (99, "Into the Cube"),
            ):
                conn.execute(
                    "INSERT INTO igdb_games (id, name, summary) VALUES (?, ?, ?)",
                    (gid, name, ""),
                )
            conn.execute(
                "INSERT INTO igdb_companies (id, name) VALUES (1, ?), (2, ?)",
                ("Other Co", "Mundfish"),
            )
            conn.execute(
                """
                INSERT INTO igdb_involved
                    (game_id, company_id, is_developer, is_publisher)
                VALUES (1, 1, 1, 0), (347640, 2, 1, 1)
                """
            )
            conn.commit()
        hits = db.igdb_search_games_by_name("The Cube", limit=5)
        ids = {h["id"] for h in hits}
        assert 347640 in ids
        assert all(h["name"].casefold() == "the cube" for h in hits)
        assert 99 not in ids  # exact hits fill the list; no fuzzy padding
        labels = db.igdb_company_labels_for_games([1, 347640])
        assert labels[347640] == "Mundfish"
        assert labels[1] == "Other Co"  # developer fallback when no publisher
        names = [h["name"] for h in hits]
        assert names == sorted(names, key=str.casefold)


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
    assert "паузу" in t("release_notify_paused_note", "ru").lower()
    assert "paused" in t("release_notify_paused_note", "en").lower()


def _check_release_pause_when_all_notified() -> None:
    """After every platform is notified, pause even if the release date is still future."""
    from handlers.release_watch import (
        _release_all_platforms_notified,
        _release_delete_keyboard,
    )

    future = int(time.time()) + 7 * 86400
    prefs = ReleaseWatchPrefs(
        igdb_game_id=1,
        game_name="Soon",
        days_before=3,
        platforms=[
            ReleasePlatformPref(
                platform_id=167,
                platform_name="PS5",
                date=future,
                human="soon",
            )
        ],
        notified_keys=[],
    )
    assert not _release_all_platforms_notified(prefs)
    prefs.notified_keys = [release_platform_key(167, future)]
    assert _release_all_platforms_notified(prefs)
    kb = _release_delete_keyboard(42, "ru")
    assert kb.inline_keyboard[0][0].callback_data == "rel:del:42"
    assert "Удалить" in kb.inline_keyboard[0][0].text


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


def _check_alert_dup_edit_opens_type_menu() -> None:
    """Dup → Edit must open game/release/drops editor, not full stream options."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch

    from db.models import (
        ReleaseWatchPrefs,
        WatchPrefs,
        dump_category_watch_prefs,
        dump_release_watch_prefs,
    )
    from handlers.subscriptions import on_share_dup_edit
    from i18n import t as _t

    uid = 910_010

    def _inline_cbs(bot: AsyncMock) -> set[str]:
        out: set[str] = set()
        for call in bot.send_message.await_args_list:
            markup = call.kwargs.get("reply_markup")
            if markup is None or not hasattr(markup, "inline_keyboard"):
                continue
            for row in markup.inline_keyboard:
                for btn in row:
                    if btn.callback_data:
                        out.add(btn.callback_data)
        return out

    with tempfile.TemporaryDirectory() as tmp:
        db = open_database(Path(tmp) / "dup_edit.db")
        db.upsert_user(uid)
        db.set_user_locale(uid, "en")

        game_prefs = WatchPrefs(
            categories=[{"id": "123", "name": "DupEdit"}],
            tags=[],
            min_viewers=0,
            max_viewers=None,
            language=None,
            exclude_mature=False,
        )
        game_id = db.add_subscription(
            owner_id=uid,
            twitch_username="dupedit",
            twitch_user_id="cw_123",
            message_template="x",
            dest_type="dm",
            chat_id=uid,
            thread_id=None,
            category_watch_prefs=dump_category_watch_prefs(game_prefs),
            from_watch_suggest=True,
        )
        rel_prefs = ReleaseWatchPrefs(
            igdb_game_id=77,
            game_name="Rel Dup",
            days_before=2,
            platforms=[],
            date_unknown=True,
        )
        import beta as beta_features
        from handlers.release_watch import RELEASE_BETA_ID

        db.set_beta_enrollment(uid, RELEASE_BETA_ID, True)
        assert beta_features.is_enabled(db, uid, RELEASE_BETA_ID)
        rel_id = db.add_subscription(
            owner_id=uid,
            twitch_username="reldup",
            twitch_user_id="rw_77",
            message_template="x",
            dest_type="dm",
            chat_id=uid,
            thread_id=None,
            release_watch_prefs=dump_release_watch_prefs(rel_prefs),
        )
        drops_id = db.add_subscription(
            owner_id=uid,
            twitch_username="dropdup",
            twitch_user_id="dg_1",
            message_template="x",
            dest_type="dm",
            chat_id=uid,
            thread_id=None,
            notify_on_live=False,
            notify_on_drops=True,
            drops_game_id="1",
        )

        async def _run() -> None:
            bot = AsyncMock()
            app = MagicMock()
            app.bot_data = {"db": db, "main_conv": None}
            app.bot = bot

            def _make(sub_id: int):
                update = MagicMock()
                query = AsyncMock()
                query.data = f"alert_dup:edit:{sub_id}"
                query.from_user = SimpleNamespace(id=uid)
                query.message = MagicMock(chat_id=uid)
                update.callback_query = query
                update.effective_chat = SimpleNamespace(id=uid)
                update.effective_user = SimpleNamespace(id=uid)
                ctx = MagicMock()
                ctx.application = app
                ctx.bot = bot
                ctx.user_data = {}
                return update, ctx

            update, ctx = _make(game_id)
            await on_share_dup_edit(update, ctx)
            game_cbs = _inline_cbs(bot)
            assert any(c.startswith(f"edit_g:{game_id}:") for c in game_cbs)
            assert not any(c.startswith(f"edit_f:{game_id}:") for c in game_cbs)

            bot.send_message.reset_mock()
            update, ctx = _make(rel_id)
            await on_share_dup_edit(update, ctx)
            rel_cbs = _inline_cbs(bot)
            assert f"edit_r:{rel_id}:days" in rel_cbs
            assert not any(c.startswith(f"edit_f:{rel_id}:") for c in rel_cbs)

            bot.send_message.reset_mock()
            update, ctx = _make(drops_id)
            with patch(
                "handlers.drops.drops_configure_block_reason",
                new=AsyncMock(return_value=None),
            ):
                await on_share_dup_edit(update, ctx)
            assert ctx.user_data.get("edit_game_cooldown") is True
            assert ctx.user_data.get("edit_sub_id") == drops_id
            sent = " ".join(
                str(c.args[1] if len(c.args) > 1 else c.kwargs.get("text") or "")
                for c in bot.send_message.await_args_list
            )
            assert _t("edit_game_cooldown_prompt", "en") in sent
            assert not any(
                c.startswith(f"edit_f:{drops_id}:") for c in _inline_cbs(bot)
            )

        asyncio.run(_run())


def _check_sync_unfollow_skips_release_and_drops() -> None:
    """Follow sync unfollow-ask must not treat release/Drops rows as streamers."""
    with tempfile.TemporaryDirectory() as tmp:
        db = open_database(Path(tmp) / "unfollow.db")
        uid = 920_001
        db.upsert_user(uid)
        prefs = ReleaseWatchPrefs(
            igdb_game_id=7,
            game_name="Grand Theft Auto VI",
            days_before=3,
            platforms=[],
            date_unknown=True,
        )
        db.add_subscription(
            owner_id=uid,
            twitch_username="Grand Theft Auto VI",
            twitch_user_id=f"rel:{uid}:abcd",
            message_template="t",
            dest_type="dm",
            chat_id=uid,
            thread_id=None,
            release_watch_prefs=dump_release_watch_prefs(prefs),
        )
        db.add_subscription(
            owner_id=uid,
            twitch_username="Some Drop Game",
            twitch_user_id=f"drops:{uid}:ef01",
            message_template="t",
            dest_type="dm",
            chat_id=uid,
            thread_id=None,
            notify_on_drops=True,
        )
        db.add_subscription(
            owner_id=uid,
            twitch_username="realstreamer",
            twitch_user_id="12345",
            message_template="t",
            dest_type="dm",
            chat_id=uid,
            thread_id=None,
            notify_on_live=True,
        )
        asked = db.get_unfollowed_manual_alert_streamers(uid, keep_twitch_user_ids=set())
        assert [a["user_login"] for a in asked] == ["realstreamer"]
        asked_kept = db.get_unfollowed_manual_alert_streamers(
            uid, keep_twitch_user_ids={"12345"}
        )
        assert asked_kept == []


def run() -> None:
    _check_release_prefs_roundtrip()
    _check_release_pick_disambiguates()
    _check_release_pick_pagination()
    _check_release_search_punct()
    _check_release_search_control_prefix()
    _check_release_search_gta_alias()
    _check_release_search_exact_duplicates()
    _check_release_keyboard()
    _check_release_pause_when_all_notified()
    _check_release_date_backfill()
    _check_release_active_cap()
    _check_release_early_dup_stops_wizard()
    _check_game_alert_dedup_by_category()
    _check_game_alert_early_dup_after_category()
    _check_alert_dup_edit_opens_type_menu()
    _check_sync_unfollow_skips_release_and_drops()
