"""Characterization checks for game giveaways digest."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from db.sqlite import SqliteDatabase
from giveaway_sources import (
    GiveawayOffer,
    _dedupe_key,
    _merge_dedupe,
    canonicalize_platforms,
    canonicalize_store,
    demo_self_check,
    filter_giveaways,
)
from i18n import alert_type_keyboard, t


def _check_sources_maps() -> None:
    demo_self_check()
    assert canonicalize_store("Epic Games Store") == "epic"
    assert "pc" in canonicalize_platforms(["Windows", "Steam"])


def _check_keyboard_order() -> None:
    kb = alert_type_keyboard(
        "en", show_drops=True, show_release=True, show_giveaways=True
    )
    labels = [row[0].text for row in kb.inline_keyboard]
    assert t("alert_type_release", "en") in labels
    assert t("alert_type_giveaways", "en") in labels
    ri = labels.index(t("alert_type_release", "en"))
    gi = labels.index(t("alert_type_giveaways", "en"))
    assert gi == ri + 1
    assert t("alert_type_giveaways", "ru")


def _check_prefs_and_seen() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = SqliteDatabase(Path(tmp) / "t.db")
        assert db.get_giveaways_prefs(1) is None
        assert db.has_any_giveaways_work() is False
        db.upsert_giveaways_prefs(
            1,
            stores=["steam", "epic"],
            platforms=["pc"],
            digest_enabled=True,
        )
        prefs = db.get_giveaways_prefs(1)
        assert prefs is not None
        assert prefs.stores == ["steam", "epic"]
        assert prefs.platforms == ["pc"]
        assert prefs.digest_enabled is True
        assert db.has_any_giveaways_work() is True
        assert db.list_giveaways_digest_owner_ids() == [1]
        assert db.has_seen_giveaway(1, "gamerpower", "9") is False
        db.mark_giveaway_seen(1, "gamerpower", "9", seen_at=1)
        assert db.has_seen_giveaway(1, "gamerpower", "9") is True
        db.set_giveaways_digest_enabled(1, False)
        # Configured stores/platforms still need the daily catalog job.
        assert db.has_any_giveaways_work() is True


def _check_catalog_snapshot() -> None:
    from db.models import GiveawayCatalogEntry
    from handlers.giveaways import (
        _enriched_from_catalog,
        filter_catalog_entries,
    )

    with tempfile.TemporaryDirectory() as tmp:
        db = SqliteDatabase(Path(tmp) / "t.db")
        assert db.list_giveaways_catalog() == []
        assert db.giveaways_catalog_refreshed_at() == 0
        entry = GiveawayCatalogEntry(
            source="gamerpower",
            external_id="1",
            title="Demo Game Free",
            store_id="steam",
            platform_ids=("pc",),
            claim_url="https://example.com",
            start_at="2026-01-01",
            end_at="N/A",
            description="desc",
            image_url="",
            dedupe_key="steam|demo",
            igdb_id=42,
            name="Demo Game",
            year="2020",
            publisher="Pub",
            developer="Dev",
            summary="A demo summary.",
            cover_url="https://example.com/cover.jpg",
            refreshed_at=1000,
        )
        db.replace_giveaways_catalog([entry])
        rows = db.list_giveaways_catalog()
        assert len(rows) == 1
        assert rows[0].name == "Demo Game"
        assert rows[0].igdb_id == 42
        assert db.giveaways_catalog_refreshed_at() == 1000
        matched = filter_catalog_entries(
            rows, stores={"steam"}, platforms={"pc"}
        )
        assert len(matched) == 1
        assert filter_catalog_entries(rows, stores={"epic"}, platforms={"pc"}) == []
        enriched = _enriched_from_catalog(db, rows[0], "en")
        assert enriched.name == "Demo Game"
        assert enriched.igdb_id == 42
        # Serve path must not re-run IGDB fuzzy search.
        import inspect
        from handlers.giveaways import _send_cards_batch

        src = inspect.getsource(_send_cards_batch)
        assert "igdb_search_games_by_name" not in src
        assert "_enriched_from_catalog" in src
        db.replace_giveaways_catalog([])
        assert db.list_giveaways_catalog() == []


def _check_filter_requires_both() -> None:
    offer = GiveawayOffer(
        source="gamerpower",
        external_id="1",
        title="Test",
        store_id="steam",
        platform_ids=("pc",),
        claim_url="https://example.com",
        start_at="",
        end_at="",
        description="",
        image_url="",
        dedupe_key=_dedupe_key("steam", "Test"),
    )
    assert filter_giveaways([offer], stores=set(), platforms={"pc"}) == []
    assert filter_giveaways([offer], stores={"steam"}, platforms=set()) == []
    assert len(filter_giveaways([offer], stores={"steam"}, platforms={"pc"})) == 1


def _check_dedupe_prefers_itad() -> None:
    a = GiveawayOffer(
        source="gamerpower",
        external_id="1",
        title="Foo (Steam)",
        store_id="steam",
        platform_ids=("pc",),
        claim_url="a",
        start_at="",
        end_at="",
        description="",
        image_url="",
        dedupe_key=_dedupe_key("steam", "Foo (Steam)"),
    )
    b = GiveawayOffer(
        source="itad",
        external_id="2",
        title="Foo",
        store_id="steam",
        platform_ids=("pc",),
        claim_url="b",
        start_at="",
        end_at="",
        description="",
        image_url="",
        dedupe_key=_dedupe_key("steam", "Foo"),
    )
    merged = _merge_dedupe([a], [b])
    assert len(merged) == 1 and merged[0].source == "itad"


def _check_first_digest_unlocks_fresh_flag() -> None:
    """Fresh button needs stores+platforms+first_digest_sent."""
    from handlers.giveaways import giveaways_hub_keyboard

    kb_locked = giveaways_hub_keyboard(
        "en", digest_enabled=True, show_fresh=False
    )
    texts = [b.text for row in kb_locked.inline_keyboard for b in row]
    assert t("giveaways_btn_fresh", "en") not in texts
    kb = giveaways_hub_keyboard("en", digest_enabled=True, show_fresh=True)
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert t("giveaways_btn_fresh", "en") in texts
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "gv:fresh" in cbs


def _check_card_keyboard() -> None:
    from handlers.giveaways import _card_keyboard, _details_keyboard

    kb = _card_keyboard(
        "ru",
        claim_url="https://store.example/game",
        igdb_game_id=42,
        show_more_offset=5,
    )
    flat = [(b.text, b.url, b.callback_data) for row in kb.inline_keyboard for b in row]
    assert any(u == "https://store.example/game" for _, u, _ in flat)
    assert any(cb == "gv:streams:42" for _, _, cb in flat)
    assert any(cb == "gv:more:5" for _, _, cb in flat)
    assert t("giveaways_go_store", "ru") in [x[0] for x in flat]
    assert t("giveaways_show_more", "ru") in [x[0] for x in flat]
    no_more = _card_keyboard(
        "en",
        claim_url="https://store.example/g",
        igdb_game_id=None,
        show_more_offset=None,
    )
    cbs = [b.callback_data for row in no_more.inline_keyboard for b in row]
    assert all(c is None or not str(c).startswith("gv:more:") for c in cbs)
    det = _details_keyboard("en")
    assert det.inline_keyboard[0][0].callback_data == "gv:details"


def _check_beta_manifest() -> None:
    import json
    from pathlib import Path

    data = json.loads(Path("beta/manifest.json").read_text(encoding="utf-8"))
    ids = {f["id"] for f in data["features"]}
    assert "giveaways-alerts" in ids
    feat = next(f for f in data["features"] if f["id"] == "giveaways-alerts")
    assert feat["stage"] == "beta"
    assert "premium_feature_id" not in feat


def _check_card_html_caption_budget() -> None:
    """Long summaries must not break Telegram HTML via naive caption slicing."""
    from handlers.giveaways import _Enriched, _build_card_html

    offer = GiveawayOffer(
        source="gamerpower",
        external_id="1",
        title="Test",
        store_id="steam",
        platform_ids=("pc", "mac"),
        claim_url="https://example.com",
        start_at="2024-01-01",
        end_at="2024-12-31",
        description="",
        image_url="",
        dedupe_key=_dedupe_key("steam", "Test"),
    )
    item = _Enriched(
        offer=offer,
        igdb_id=1,
        name="Game <Name> & Co",
        year="2020",
        publisher="Pub & Co",
        developer="Dev <Ltd>",
        summary="A & B <tag> " + ("word " * 400),
        cover_url="",
    )
    footer = (
        '<a href="https://www.gamerpower.com">GamerPower</a> · '
        '<a href="https://www.igdb.com">IGDB.com</a>'
    )
    body = _build_card_html(item, "en", footer=footer)
    assert len(body) <= 1024
    assert body.count("<b>") == body.count("</b>") == 1
    assert body.count("<a ") == body.count("</a>")
    assert "<tag>" not in body
    assert "&amp;" in body or "Game" in body


def run() -> None:
    _check_sources_maps()
    _check_keyboard_order()
    _check_prefs_and_seen()
    _check_catalog_snapshot()
    _check_filter_requires_both()
    _check_dedupe_prefers_itad()
    _check_first_digest_unlocks_fresh_flag()
    _check_card_keyboard()
    _check_card_html_caption_budget()
    _check_beta_manifest()
    # unused mock keeps import for future handler tests
    _ = MagicMock
    print("checks_giveaways: ok")


if __name__ == "__main__":
    run()
