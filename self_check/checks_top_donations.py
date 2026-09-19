"""Top donations (DonationAlerts) beta — format + wiring smoke."""

from __future__ import annotations

from datetime import datetime, timezone

from alert_settings import ALERT_SETTING_ORDER
from donationalerts import (
    BETA_FEATURE_ID,
    DEFAULT_TOP_DONATIONS_TEMPLATE,
    Donation,
    format_top_donations_block,
    top_donations,
)
from i18n import SUPPORTED_LOCALES, t


def check_top_donations() -> None:
    assert BETA_FEATURE_ID == "top-donations"
    assert "top_donations" in ALERT_SETTING_ORDER
    assert ALERT_SETTING_ORDER.index("top_donations") == ALERT_SETTING_ORDER.index(
        "live_remind"
    ) + 1

    now = datetime.now(timezone.utc)
    donations = [
        Donation(1, "a", 10.0, "RUB", now),
        Donation(2, "b", 50.5, "USD", now),
        Donation(3, "c", 20.0, "RUB", now),
        Donation(4, "d", 50.5, "EUR", now),
        Donation(5, "e", 5.0, "RUB", now),
        Donation(6, "f", 100.0, "RUB", now),
    ]
    top = top_donations(donations, limit=5)
    assert [d.username for d in top] == ["f", "b", "d", "c", "a"]

    block = format_top_donations_block(
        DEFAULT_TOP_DONATIONS_TEMPLATE, donations, limit=3
    )
    assert "f" in block and "100 RUB" in block
    assert block.count("\n") == 2
    assert format_top_donations_block("", donations) == ""
    assert format_top_donations_block(DEFAULT_TOP_DONATIONS_TEMPLATE, []) == ""

    for loc in SUPPORTED_LOCALES:
        assert t("advanced_options_top_donations", loc)
        assert t("advanced_options_hint_top_donations", loc)
        assert t("sub_list_top_donations_yes", loc)
        assert t("top_donations_template_prompt", loc)
        assert t("top_donations_oauth_prompt", loc)
        assert t("top_donations_oauth_button", loc)
        assert t("top_donations_oauth_done", loc)
        assert t("top_donations_oauth_failed", loc)
        assert t("top_donations_oauth_unavailable", loc)
        assert t("top_donations_need_end", loc)
        assert t("beta_feat_top_donations", loc)
        assert t("beta_feat_top_donations_desc", loc)
        assert t("edit_top_donations_template", loc)
