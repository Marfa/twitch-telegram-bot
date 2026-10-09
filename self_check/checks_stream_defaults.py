"""Default stream alert settings — DB, keyboard order, apply/lock."""

from __future__ import annotations

from pathlib import Path
import tempfile

from db import open_database
from i18n import btn, edit_options_keyboard, settings_menu, t
from stream_alert_defaults import (
    apply_defaults_to_user_data,
    dump_stream_alert_defaults,
    parse_stream_alert_defaults,
    subscription_update_from_defaults,
)


def run() -> None:
    for loc in ("ru", "en", "uk", "it"):
        assert btn("default_alert_settings", loc)
        assert t("edit_apply_defaults", loc)
        assert t("stream_defaults_locked", loc)
        assert t("stream_defaults_save", loc)
        kb = settings_menu(loc).keyboard
        texts = [b.text for row in kb for b in row]
        sync = btn("sync_subs", loc)
        defaults = btn("default_alert_settings", loc)
        auth = btn("auth_tokens", loc)
        assert sync in texts and defaults in texts
        from config import show_premium_ui

        if show_premium_ui():
            default_row = next(row for row in kb if defaults in [b.text for b in row])
            assert len(default_row) == 2
            assert auth in [b.text for b in default_row]
        else:
            assert texts.index(defaults) == texts.index(sync) + 1

    kb = edit_options_keyboard(
        7,
        "ru",
        show_advanced=True,
        use_stream_defaults=False,
        dest_type="channel",
    )
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "edit_f:7:apply_defaults" in cbs
    assert cbs.index("edit_f:7:apply_defaults") < cbs.index("edit_f:7:dest")

    locked = edit_options_keyboard(
        7,
        "ru",
        show_advanced=True,
        use_stream_defaults=True,
        dest_type="channel",
        strip_name_mentions=True,
    )
    locked_cbs = [b.callback_data for row in locked.inline_keyboard for b in row]
    assert "edit_f:7:apply_defaults" in locked_cbs
    assert "edit_f:7:dest" in locked_cbs
    assert any(c == "edit_f:7:defaults_locked" for c in locked_cbs)
    assert not any(
        c and c.startswith("edit_f:7:") and c.endswith(":strip") for c in locked_cbs
    )

    defaults_kb = edit_options_keyboard(
        0,
        "ru",
        for_defaults_editor=True,
        show_advanced=True,
        show_custom_buttons=True,
        show_multistream=True,
        show_schedule_cancel=True,
        show_top_donations=True,
        show_live_remind=True,
        schedule_reminder_configured=True,
        dest_type="channel",
    )
    d_cbs = [b.callback_data for row in defaults_kb.inline_keyboard for b in row]
    assert "defedit:0:save" in d_cbs
    assert "defedit:0:cancel" in d_cbs
    assert not any(c and ":dest" in c for c in d_cbs)

    payload = parse_stream_alert_defaults("")
    payload["message_template"] = "Hello {username}"
    payload["strip_name_mentions"] = True
    raw = dump_stream_alert_defaults(payload)
    again = parse_stream_alert_defaults(raw)
    assert again["message_template"] == "Hello {username}"
    assert again["strip_name_mentions"] is True

    ud: dict = {}
    apply_defaults_to_user_data(ud, again, alert_type="live")
    assert ud["use_stream_defaults"] is True
    assert ud["message_template"] == "Hello {username}"
    assert ud["adv_want_strip"] is True

    fields = subscription_update_from_defaults(again, alert_type="upcoming")
    assert fields["use_stream_defaults"] is True
    assert fields["multistream_channels"] == "[]"
    assert fields["delay_minutes"] == 0

    with tempfile.TemporaryDirectory() as tmp:
        db = open_database(Path(tmp) / "stream_defaults.db")
        db.upsert_user(42)
        db.set_stream_alert_defaults(
            42,
            {
                "message_template": "Bound {username}",
                "pin_message": True,
            },
        )
        got = db.get_stream_alert_defaults(42)
        assert got["message_template"] == "Bound {username}"
        sid = db.add_subscription(
            42,
            "streamer",
            "99",
            "old",
            "dm",
            42,
            None,
            use_stream_defaults=True,
        )
        n = db.resync_bound_stream_alert_defaults(42)
        assert n == 1
        sub = db.get_subscription(sid, 42)
        assert sub is not None
        assert sub.message_template == "Bound {username}"
        assert sub.use_stream_defaults is True
        db.update_subscription(sid, 42, use_stream_defaults=False)
        db.set_stream_alert_defaults(42, {"message_template": "New {username}"})
        assert db.resync_bound_stream_alert_defaults(42) == 0
        sub2 = db.get_subscription(sid, 42)
        assert sub2 is not None
        assert sub2.message_template == "Bound {username}"
