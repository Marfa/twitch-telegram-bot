"""Release alerts: prefs, free entitlement, active cap, keyboard."""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

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


def run() -> None:
    _check_release_prefs_roundtrip()
    _check_release_keyboard()
    _check_release_date_backfill()
    _check_release_active_cap()
