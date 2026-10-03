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
        assert prefs.deal_cut_min is None
        db.upsert_giveaways_prefs(
            1,
            stores=prefs.stores,
            platforms=prefs.platforms,
            deal_cut_min=50,
        )
        prefs = db.get_giveaways_prefs(1)
        assert prefs is not None and prefs.deal_cut_min == 50
        # Omitting deal_cut_min keeps existing value.
        db.upsert_giveaways_prefs(
            1,
            stores=["steam"],
            platforms=["pc"],
            digest_enabled=True,
        )
        prefs = db.get_giveaways_prefs(1)
        assert prefs is not None and prefs.deal_cut_min == 50
        # 100 / clear → NULL (free-only default).
        db.upsert_giveaways_prefs(
            1,
            stores=prefs.stores,
            platforms=prefs.platforms,
            deal_cut_min=None,
        )
        prefs = db.get_giveaways_prefs(1)
        assert prefs is not None and prefs.deal_cut_min is None
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


def _check_rebuild_is_lazy() -> None:
    """Catalog rebuild stores raw rows; IGDB enrich is deferred to browse pages."""
    from unittest.mock import patch

    from handlers.giveaways import (
        _needs_igdb_enrich,
        rebuild_giveaways_catalog_sync,
    )

    offer = GiveawayOffer(
        source="gamerpower",
        external_id="99",
        title="Lazy Rebuild Game",
        store_id="steam",
        platform_ids=("pc",),
        claim_url="https://example.com",
        start_at="",
        end_at="",
        description="English blurb.",
        image_url="",
        dedupe_key=_dedupe_key("steam", "Lazy Rebuild Game"),
    )
    with tempfile.TemporaryDirectory() as td:
        db = SqliteDatabase(Path(td) / "gv_lazy.db")
        with patch(
            "handlers.giveaways.fetch_active_giveaways",
            return_value=[offer],
        ):
            entries = rebuild_giveaways_catalog_sync(db)
        assert len(entries) == 1
        assert entries[0].refreshed_at < 0
        assert _needs_igdb_enrich(entries[0])
        assert db.giveaways_catalog_refreshed_at() == abs(entries[0].refreshed_at)
        assert entries[0].igdb_id is None


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
    assert t("giveaways_btn_discount", "en") in texts
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "gv:fresh" in cbs
    assert "gv:discount" in cbs
    # Discount under platforms
    assert cbs.index("gv:platforms") < cbs.index("gv:discount")


def _check_deal_page_enrich_lazy() -> None:
    """ITAD deals stay raw until the browse page is sent."""
    from db.models import GiveawayCatalogEntry
    from handlers.giveaways import (
        _enrich_browse_page,
        _needs_igdb_enrich,
        _raw_catalog_entry_from_offer,
        _store_browse,
    )

    deal = GiveawayOffer(
        source="itad_deal",
        external_id="d1",
        title="Lazy Deal",
        store_id="steam",
        platform_ids=("pc",),
        claim_url="https://example.com/d",
        start_at="",
        end_at="",
        description="−15%",
        image_url="https://example.com/i.jpg",
        dedupe_key=_dedupe_key("steam", "Lazy Deal"),
        cut=15,
    )
    raw = _raw_catalog_entry_from_offer(deal)
    assert _needs_igdb_enrich(raw)
    assert raw.refreshed_at == 0
    free = GiveawayCatalogEntry(
        source="gamerpower",
        external_id="1",
        title="Free",
        store_id="steam",
        platform_ids=("pc",),
        claim_url="a",
        start_at="",
        end_at="",
        description="",
        image_url="",
        dedupe_key=_dedupe_key("steam", "Free"),
        igdb_id=1,
        name="Free",
        year="2020",
        publisher="P",
        developer="D",
        summary="s",
        cover_url="",
        refreshed_at=100,
    )
    assert not _needs_igdb_enrich(free)
    entries = [raw, free]
    app = MagicMock()
    app.bot_data = {}
    stored = _store_browse(app, 1, entries, "en")
    assert stored is app.bot_data["giveaways_browse"][1]["entries"]

    class _Db:
        def igdb_search_games_by_name(self, q, limit=5):
            return [{"id": 99, "name": q, "first_release_date": 0}]

        def igdb_publisher_developer_names(self, _gid):
            return ("Pub", "Dev")

        def igdb_game_by_id(self, _gid):
            return {"summary": "Hello"}

        def igdb_cover_image_id_for_game(self, _gid):
            return None

    _enrich_browse_page(_Db(), stored, 0, 1)
    assert stored[0].refreshed_at > 0
    assert stored[0].name == "Lazy Deal"
    assert not _needs_igdb_enrich(stored[0])
    # Second row untouched by page size 1
    assert stored[1].refreshed_at == 100


def _check_deal_merge_and_card_cut() -> None:
    from db.models import GiveawayCatalogEntry
    from giveaway_sources import merge_prefer_giveaway
    from handlers.giveaways import (
        _Enriched,
        _build_card_html,
        _merge_catalog_prefer_giveaway,
    )

    free = GiveawayOffer(
        source="gamerpower",
        external_id="1",
        title="Foo",
        store_id="steam",
        platform_ids=("pc",),
        claim_url="a",
        start_at="",
        end_at="",
        description="",
        image_url="",
        dedupe_key=_dedupe_key("steam", "Foo"),
    )
    deal = GiveawayOffer(
        source="itad_deal",
        external_id="d",
        title="Foo",
        store_id="steam",
        platform_ids=("pc",),
        claim_url="d",
        start_at="",
        end_at="",
        description="−50%",
        image_url="",
        dedupe_key=_dedupe_key("steam", "Foo"),
        cut=50,
    )
    merged = merge_prefer_giveaway([free], [deal])
    assert len(merged) == 1 and merged[0].source == "gamerpower"
    entry_free = GiveawayCatalogEntry(
        source="gamerpower",
        external_id="1",
        title="Foo",
        store_id="steam",
        platform_ids=("pc",),
        claim_url="a",
        start_at="",
        end_at="",
        description="",
        image_url="",
        dedupe_key=_dedupe_key("steam", "Foo"),
        igdb_id=None,
        name="Foo",
        year="",
        publisher="",
        developer="",
        summary="",
        cover_url="",
    )
    entry_deal = GiveawayCatalogEntry(
        source="itad_deal",
        external_id="d",
        title="Bar",
        store_id="steam",
        platform_ids=("pc",),
        claim_url="d",
        start_at="",
        end_at="",
        description="−70%",
        image_url="",
        dedupe_key=_dedupe_key("steam", "Bar"),
        igdb_id=None,
        name="Bar",
        year="",
        publisher="",
        developer="",
        summary="",
        cover_url="",
        cut=70,
    )
    cat = _merge_catalog_prefer_giveaway([entry_free], [entry_deal])
    assert len(cat) == 2
    item = _Enriched(
        offer=GiveawayOffer(
            source="itad_deal",
            external_id="d",
            title="Bar",
            store_id="steam",
            platform_ids=("pc",),
            claim_url="d",
            start_at="",
            end_at="",
            description="",
            image_url="",
            dedupe_key=_dedupe_key("steam", "Bar"),
            cut=70,
        ),
        igdb_id=None,
        name="Bar",
        year="",
        publisher="",
        developer="",
        genre="",
        summary="",
        cover_url="",
    )
    html_body = _build_card_html(item, "en")
    assert "Discount:" in html_body
    assert "70%" in html_body
    assert "−70% ·" not in html_body
    free_item = _Enriched(
        offer=GiveawayOffer(
            source="gamerpower",
            external_id="1",
            title="Free",
            store_id="steam",
            platform_ids=("pc",),
            claim_url="a",
            start_at="",
            end_at="",
            description="",
            image_url="",
            dedupe_key=_dedupe_key("steam", "Free"),
            cut=None,
        ),
        igdb_id=None,
        name="Free",
        year="",
        publisher="",
        developer="",
        genre="",
        summary="",
        cover_url="",
    )
    free_html = _build_card_html(free_item, "ru")
    assert "Скидка:" in free_html
    assert "100%" in free_html


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
    assert feat["stage"] == "ga"
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
        genre="Action",
        summary="A & B <tag> " + ("word " * 400),
        cover_url="",
    )
    from i18n import igdb_attribution

    footer = (
        '<a href="https://www.gamerpower.com">GamerPower</a> · '
        + igdb_attribution("en")
    )
    body = _build_card_html(item, "en", footer=footer)
    assert len(body) <= 1024
    assert body.count("<b>") == body.count("</b>")
    assert body.count("<b>") >= 7  # title + genre + pub + dev + platforms + store + dates
    assert "Genre:" in body or "<b>Genre:</b>" in body
    assert "Giveaway dates:" in body
    assert body.index("Platforms:") < body.index("Store:")
    assert body.index("Store:") < body.index("Giveaway dates:")
    assert "\n\n<b>Giveaway dates:" in body or "\n\n<b>Giveaway dates:</b>" in body
    assert 'href="https://example.com"' in body
    assert ">Steam</a>" in body
    assert body.count("<a ") == body.count("</a>")
    assert "<tag>" not in body
    assert "&amp;" in body or "Game" in body


def _check_unified_card_dates_after_platforms() -> None:
    from game_card import GameCardFields, build_game_card_html, release_dates_line

    fields = GameCardFields(
        name="Test",
        genre="RPG",
        publisher="P",
        developer="D",
        platforms="PC",
        dates="2024-01-01 (PC)",
        dates_key="game_card_release_dates",
        summary="Hi",
    )
    body = build_game_card_html(fields, "en")
    assert "<b>Genre:</b>" in body
    assert body.index("Platforms:") < body.index("Release dates:")
    assert "\n\n<b>Release dates:</b>" in body
    assert "2024-01-01 (PC)" in body

    class _Db:
        def igdb_release_dates_for_game(self, _gid: int):
            return [
                {"date": 1704067200, "platform_name": "PC"},
                {"date": 1706745600, "platform_name": "PlayStation 5"},
            ]

        def igdb_game_by_id(self, _gid: int):
            return {}

    line = release_dates_line(_Db(), 1, "en")
    assert line == "2024-01-01 (PC), 2024-02-01 (PlayStation 5)"


def run() -> None:
    _check_sources_maps()
    _check_keyboard_order()
    _check_prefs_and_seen()
    _check_catalog_snapshot()
    _check_rebuild_is_lazy()
    _check_filter_requires_both()
    _check_dedupe_prefers_itad()
    _check_first_digest_unlocks_fresh_flag()
    _check_deal_page_enrich_lazy()
    _check_deal_merge_and_card_cut()
    _check_card_keyboard()
    _check_card_html_caption_budget()
    _check_unified_card_dates_after_platforms()
    _check_beta_manifest()
    # unused mock keeps import for future handler tests
    _ = MagicMock
    print("checks_giveaways: ok")


if __name__ == "__main__":
    run()
