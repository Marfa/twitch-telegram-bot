"""Giveaway-watch prefs + platform matching self-check."""

from __future__ import annotations

from types import SimpleNamespace

import premium as prem
from db.models import (
    GiveawayPlatformPref,
    GiveawayWatchPrefs,
    alert_type_from_payload,
    dump_giveaway_watch_prefs,
    is_giveaway_watch_sub,
    parse_giveaway_watch_prefs,
)
from giveaway_sources import GiveawayOffer
from handlers.giveaway_watch import (
    _game_pick_keyboard,
    canonicalize_igdb_platform_name,
    platforms_match_offer,
)


def _check_prefs_roundtrip() -> None:
    prefs = GiveawayWatchPrefs(
        igdb_game_id=42,
        game_name="Demo Game",
        platforms=[
            GiveawayPlatformPref(platform_id=6, platform_name="PC (Microsoft Windows)"),
            GiveawayPlatformPref(platform_id=48, platform_name="PlayStation 4"),
        ],
        notified_keys=["gamerpower:1"],
    )
    raw = dump_giveaway_watch_prefs(prefs)
    back = parse_giveaway_watch_prefs(raw)
    assert back is not None
    assert back.igdb_game_id == 42
    assert back.game_name == "Demo Game"
    assert len(back.platforms) == 2
    assert back.notified_keys == ["gamerpower:1"]
    assert alert_type_from_payload({"giveaway_watch_prefs": raw}) == "giveaway_watch"
    assert is_giveaway_watch_sub(SimpleNamespace(giveaway_watch_prefs=raw))
    assert prem.is_live_only_alert(SimpleNamespace(giveaway_watch_prefs=raw))


def _check_empty_platforms_any() -> None:
    prefs = GiveawayWatchPrefs(igdb_game_id=1, game_name="X", platforms=[])
    raw = dump_giveaway_watch_prefs(prefs)
    back = parse_giveaway_watch_prefs(raw)
    assert back is not None
    assert back.platforms == []
    offer = GiveawayOffer(
        source="gamerpower",
        external_id="9",
        title="X",
        store_id="steam",
        platform_ids=("pc",),
        claim_url="https://example.com",
        start_at="",
        end_at="",
        description="",
        image_url="",
        dedupe_key="steam|x",
    )
    assert platforms_match_offer(back, offer)


def _check_platform_canonicalize() -> None:
    assert "pc" in canonicalize_igdb_platform_name("PC (Microsoft Windows)")
    assert "ps5" in canonicalize_igdb_platform_name("PlayStation 5")
    prefs = GiveawayWatchPrefs(
        igdb_game_id=1,
        game_name="X",
        platforms=[
            GiveawayPlatformPref(platform_id=6, platform_name="PC (Microsoft Windows)")
        ],
    )
    pc = GiveawayOffer(
        source="itad",
        external_id="1",
        title="X",
        store_id="steam",
        platform_ids=("pc",),
        claim_url="",
        start_at="",
        end_at="",
        description="",
        image_url="",
        dedupe_key="steam|x",
    )
    switch = GiveawayOffer(
        source="itad",
        external_id="2",
        title="X",
        store_id="nintendo_eshop",
        platform_ids=("switch",),
        claim_url="",
        start_at="",
        end_at="",
        description="",
        image_url="",
        dedupe_key="nintendo|x",
    )
    assert platforms_match_offer(prefs, pc)
    assert not platforms_match_offer(prefs, switch)


def _check_game_pick_keyboard_company_label() -> None:
    # Regression: labels are dict[int, str], not (pub, dev) tuples.
    # Unpacking a multi-char string as 2-tuple raises ValueError.
    markup = _game_pick_keyboard(
        [{"id": 1, "name": "Half-Life"}],
        "en",
        companies={1: "Valve"},
    )
    label = markup.inline_keyboard[0][0].text
    assert "Half-Life" in label
    assert "Valve" in label


def _check_igdb_platforms_for_game() -> None:
    import tempfile
    from pathlib import Path

    from db import open_database

    with tempfile.TemporaryDirectory() as tmp:
        db = open_database(Path(tmp) / "bot.db")
        assert db.igdb_platforms_for_game(0) == []
        with db._conn() as conn:
            conn.execute(
                "INSERT INTO igdb_platforms (id, name) VALUES (?, ?), (?, ?)",
                (6, "PC (Microsoft Windows)", 48, "PlayStation 4"),
            )
            conn.execute(
                """
                INSERT INTO igdb_release_dates (id, game_id, platform_id, date, human)
                VALUES (1, 99, 48, 1, ''), (2, 99, 6, 1, ''), (3, 99, 6, 2, '')
                """
            )
            conn.commit()
        rows = db.igdb_platforms_for_game(99)
        assert [r["platform_id"] for r in rows] == [6, 48]
        assert rows[0]["platform_name"].startswith("PC")


def run() -> None:
    _check_prefs_roundtrip()
    _check_empty_platforms_any()
    _check_platform_canonicalize()
    _check_game_pick_keyboard_company_label()
    _check_igdb_platforms_for_game()


if __name__ == "__main__":
    run()
    print("ok")
