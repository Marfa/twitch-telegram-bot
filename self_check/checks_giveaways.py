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
        assert db.has_any_giveaways_work() is False


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


def _check_beta_manifest() -> None:
    import json
    from pathlib import Path

    data = json.loads(Path("beta/manifest.json").read_text(encoding="utf-8"))
    ids = {f["id"] for f in data["features"]}
    assert "giveaways-alerts" in ids
    feat = next(f for f in data["features"] if f["id"] == "giveaways-alerts")
    assert feat["stage"] == "beta"
    assert "premium_feature_id" not in feat


def run() -> None:
    _check_sources_maps()
    _check_keyboard_order()
    _check_prefs_and_seen()
    _check_filter_requires_both()
    _check_dedupe_prefers_itad()
    _check_first_digest_unlocks_fresh_flag()
    _check_beta_manifest()
    # unused mock keeps import for future handler tests
    _ = MagicMock
    print("checks_giveaways: ok")


if __name__ == "__main__":
    run()
