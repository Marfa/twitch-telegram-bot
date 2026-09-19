"""ALERT_SETTING_ORDER must match Extras, edit menu, and subscription list."""

from __future__ import annotations

from db.models import Subscription
from alert_settings import ALERT_SETTING_ORDER, EDIT_FIELD
from handlers.subscriptions import _format_sub_line
from i18n import (
    SUPPORTED_LOCALES,
    advanced_options_keyboard,
    chat_button_keyboard,
    edit_bool_keyboard,
    edit_options_keyboard,
    t,
    t_bullet,
)


def _advopt_ids(markup) -> list[str]:
    out: list[str] = []
    for row in markup.inline_keyboard:
        for btn in row:
            data = btn.callback_data or ""
            if data.startswith("advopt:toggle:"):
                out.append(data.rsplit(":", 1)[-1])
    return out


def _edit_setting_ids(markup, sub_id: int = 1) -> list[str]:
    prefix = f"edit_f:{sub_id}:"
    field_to_sid = {v: k for k, v in EDIT_FIELD.items()}
    out: list[str] = []
    for row in markup.inline_keyboard:
        for btn in row:
            data = btn.callback_data or ""
            if not data.startswith(prefix):
                continue
            field = data[len(prefix) :]
            sid = field_to_sid.get(field)
            if sid:
                out.append(sid)
    return out


def _assert_subsequence(found: list[str], canonical: tuple[str, ...]) -> None:
    expected = [s for s in canonical if s in found]
    assert found == expected, f"order {found!r} != {expected!r} (from {canonical})"


def _list_markers(lang: str) -> dict[str, str]:
    return {
        "image": t_bullet("image_before_note", lang),
        "strip": t("sub_list_strip_yes", lang),
        "ignore": t_bullet("ignore_keywords_yes_note", lang, keywords="spoiler"),
        "delay": t_bullet("delay_yes_note", lang, minutes=5),
        "repeat": t("sub_list_repeat_mute", lang, minutes=10),
        "delete": t("sub_list_delete_yes", lang),
        "pin": t("sub_list_pin_yes", lang),
        "buttons": t("sub_list_custom_buttons", lang, count=1),
        "chat": t("sub_list_chat_button_yes", lang),
        "live_remind": t("sub_list_live_remind_yes", lang),
        "top_donations": t("sub_list_top_donations_yes", lang),
        "preview": t_bullet("preview_off", lang),
        "schedule_cancel": t("sub_list_schedule_cancel_yes", lang),
        "multistream": t("sub_list_multistream", lang, count=1),
    }


def _found_setting_ids(line: str, markers: dict[str, str]) -> list[str]:
    hits = [(line.find(text), sid) for sid, text in markers.items() if text in line]
    hits.sort()
    return [sid for _, sid in hits]


def _sample_sub(**overrides: object) -> Subscription:
    base: dict[str, object] = {
        "id": 1,
        "owner_id": 1,
        "twitch_username": "orderchan",
        "twitch_user_id": "uid_order",
        "message_template": "hi https://twitch.tv/orderchan",
        "dest_type": "channel",
        "chat_id": -1001,
        "thread_id": None,
        "enabled": True,
        "delete_previous": True,
        "notify_delete_fail": False,
        "disable_link_preview": False,
        "strip_name_mentions": True,
        "attach_chat_button": True,
        "attach_live_remind_button": False,
        "custom_buttons": '[{"text":"Go","url":"https://example.com"}]',
        "multistream_channels": "[]",
        "delay_minutes": 5,
        "suppress_repeat_minutes": 10,
        "schedule_reminder_minutes": 0,
        "schedule_reminder_configured": False,
        "notify_on_live": True,
        "notify_on_end": False,
        "notify_on_category_change": False,
        "ignore_keywords": "spoiler",
        "use_global_ignore": False,
        "image_file_id": "photo_file_id",
        "image_position": "before",
        "notify_cooldown_until": None,
        "last_message_id": None,
        "last_schedule_reminder_segment_id": None,
        "from_twitch_sync": False,
        "from_watch_suggest": False,
        "sync_user_edited": False,
        "category_watch_prefs": "{}",
        "category_watch_live_ids": "[]",
        "category_watch_primed": False,
        "delete_other_alerts": False,
        "pin_message": True,
        "is_demo": False,
        "trial_paused": False,
        "delivery_paused": False,
    }
    base.update(overrides)
    return Subscription(**base)  # type: ignore[arg-type]


def check_button_style_options() -> None:
    """Color picker appears in Extras/edit when any button option is on."""
    from custom_buttons import (
        BUTTON_STYLE_CHOICES,
        normalize_button_style,
        styled_inline_button,
    )

    assert normalize_button_style("primary") == "primary"
    assert normalize_button_style("BLUE") == ""
    assert normalize_button_style("default") == ""
    assert normalize_button_style(None) == ""

    btn = styled_inline_button("Go", url="https://example.com", style="danger")
    assert (btn.api_kwargs or {}).get("style") == "danger"
    plain = styled_inline_button("Go", url="https://example.com")
    assert not plain.api_kwargs

    adv_off = advanced_options_keyboard(
        "en",
        want_image=False,
        want_strip=False,
        want_ignore=False,
        want_delay=False,
        want_repeat=False,
        want_delete=False,
        want_pin=False,
        want_chat=False,
        want_buttons=False,
        want_live_remind=False,
        show_buttons=True,
        show_live_remind=True,
    )
    style_cbs = [
        (b.callback_data or "")
        for row in adv_off.inline_keyboard
        for b in row
        if (b.callback_data or "").startswith("advopt:style:")
    ]
    assert style_cbs == []

    adv_on = advanced_options_keyboard(
        "en",
        want_image=False,
        want_strip=False,
        want_ignore=False,
        want_delay=False,
        want_repeat=False,
        want_delete=False,
        want_pin=False,
        want_chat=True,
        want_buttons=False,
        button_style="success",
        show_buttons=True,
    )
    style_cbs = [
        (b.callback_data or "")
        for row in adv_on.inline_keyboard
        for b in row
        if (b.callback_data or "").startswith("advopt:style:")
    ]
    assert style_cbs == [f"advopt:style:{c}" for c in BUTTON_STYLE_CHOICES]
    marked = [
        b.text
        for row in adv_on.inline_keyboard
        for b in row
        if (b.callback_data or "") == "advopt:style:success"
    ]
    assert marked and marked[0].startswith("✅")

    edit = edit_options_keyboard(
        1,
        "en",
        dest_type="dm",
        attach_chat_button=True,
        show_advanced=True,
        button_style="primary",
    )
    assert any(
        (b.callback_data or "") == "edit_f:1:button_style"
        for row in edit.inline_keyboard
        for b in row
    )
    edit_off = edit_options_keyboard(
        1,
        "en",
        dest_type="dm",
        attach_chat_button=False,
        show_advanced=True,
    )
    assert not any(
        (b.callback_data or "") == "edit_f:1:button_style"
        for row in edit_off.inline_keyboard
        for b in row
    )

    for loc in SUPPORTED_LOCALES:
        assert t("button_style_default", loc)
        assert t("button_style_primary", loc)
        assert t("button_style_success", loc)
        assert t("button_style_danger", loc)
        assert t("advanced_options_hint_button_style", loc)
        assert t("edit_button_style", loc, style="x")
        assert t("sub_list_button_style", loc, style="x")


def check_alert_setting_order() -> None:
    assert ALERT_SETTING_ORDER == (
        "image",
        "strip",
        "ignore",
        "delay",
        "repeat",
        "delete",
        "pin",
        "buttons",
        "chat",
        "live_remind",
        "top_donations",
        "preview",
        "schedule_remind",
        "schedule_cancel",
        "multistream",
    )
    assert set(EDIT_FIELD) == set(ALERT_SETTING_ORDER)

    adv = advanced_options_keyboard(
        "en",
        want_image=False,
        want_strip=False,
        want_ignore=False,
        want_delay=False,
        want_repeat=False,
        want_delete=False,
        want_pin=False,
        want_chat=False,
        want_buttons=False,
        want_live_remind=False,
        want_top_donations=False,
        want_preview=False,
        want_schedule_cancel=False,
        want_multistream=False,
        show_delay=True,
        show_repeat=True,
        show_buttons=True,
        show_live_remind=True,
        show_top_donations=True,
        show_preview=True,
        show_schedule_remind=False,
        show_schedule_cancel=True,
        show_multistream=True,
    )
    assert _advopt_ids(adv) == [
        sid for sid in ALERT_SETTING_ORDER if sid != "schedule_remind"
    ]

    edit = edit_options_keyboard(
        1,
        "en",
        dest_type="channel",
        delete_previous=True,
        pin_message=True,
        has_image=True,
        strip_name_mentions=True,
        attach_chat_button=True,
        disable_link_preview=True,
        show_link_preview=True,
        show_advanced=True,
        show_custom_buttons=True,
        show_multistream=True,
        is_upcoming=False,
    )
    assert _edit_setting_ids(edit) == [
        sid
        for sid in ALERT_SETTING_ORDER
        if sid not in ("live_remind", "schedule_remind", "schedule_cancel", "top_donations")
    ]
    labels = {
        (btn.callback_data or ""): btn.text
        for row in edit.inline_keyboard
        for btn in row
    }
    assert labels["edit_f:1:strip"].startswith("✅ ")
    assert labels["edit_f:1:chat_button"].startswith("✅ ")
    assert labels["edit_f:1:preview"].startswith("⬜️ ")
    assert labels["edit_f:1:delete_old"].startswith("✅ ")
    assert labels["edit_f:1:pin_message"].startswith("✅ ")
    assert labels["edit_f:1:delete_fail"].startswith("⬜️ ")
    assert not labels["edit_f:1:repeat"].startswith(("✅ ", "⬜️ "))

    upcoming_edit = edit_options_keyboard(
        1,
        "en",
        dest_type="dm",
        show_advanced=True,
        is_upcoming=True,
        show_live_remind=True,
        show_schedule_cancel=True,
        attach_live_remind_button=True,
        notify_on_schedule_cancel=True,
        schedule_reminder_configured=True,
        schedule_reminder_minutes=15,
    )
    assert _edit_setting_ids(upcoming_edit) == [
        sid
        for sid in ALERT_SETTING_ORDER
        if sid not in ("delay", "repeat", "delete", "pin", "buttons", "multistream", "top_donations")
    ]
    upcoming_labels = {
        (btn.callback_data or ""): btn.text
        for row in upcoming_edit.inline_keyboard
        for btn in row
    }
    assert upcoming_labels["edit_f:1:live_remind"].startswith("✅ ")
    assert upcoming_labels["edit_f:1:sched_remind"].startswith("✅ ")
    assert upcoming_labels["edit_f:1:schedule_cancel"].startswith("✅ ")
    # Cancel stays last among shared setting rows.
    setting_ids = _edit_setting_ids(upcoming_edit)
    assert setting_ids[-1] == "schedule_cancel"
    assert setting_ids[-2] == "schedule_remind"
    assert "edit_f:1:delay" not in upcoming_labels
    assert "edit_f:1:repeat" not in upcoming_labels
    assert "edit_f:1:top_donations" not in upcoming_labels

    end_edit = edit_options_keyboard(
        1,
        "en",
        dest_type="dm",
        show_advanced=True,
        notify_on_end=True,
        show_top_donations=True,
        top_donations=True,
    )
    end_labels = {
        (btn.callback_data or ""): btn.text
        for row in end_edit.inline_keyboard
        for btn in row
    }
    assert end_labels["edit_f:1:top_donations"].startswith("✅ ")
    assert "edit_f:1:live_remind" not in end_labels

    off = edit_options_keyboard(
        1,
        "en",
        dest_type="dm",
        attach_chat_button=False,
        disable_link_preview=False,
        show_link_preview=True,
        show_advanced=True,
    )
    off_labels = {
        (btn.callback_data or ""): btn.text
        for row in off.inline_keyboard
        for btn in row
    }
    assert off_labels["edit_f:1:chat_button"].startswith("⬜️ ")
    assert off_labels["edit_f:1:preview"].startswith("✅ ")

    delete_off = edit_options_keyboard(
        1,
        "en",
        dest_type="channel",
        delete_previous=False,
        show_advanced=True,
    )
    delete_labels = {
        (btn.callback_data or ""): btn.text
        for row in delete_off.inline_keyboard
        for btn in row
    }
    assert delete_labels["edit_f:1:delete_old"].startswith("⬜️ ")

    fail_on = edit_options_keyboard(
        1,
        "en",
        dest_type="channel",
        delete_previous=True,
        notify_delete_fail=True,
        delete_other_alerts=True,
        notify_on_category_change=True,
        schedule_reminder_configured=True,
        schedule_reminder_minutes=15,
        show_advanced=True,
    )
    fail_labels = {
        (btn.callback_data or ""): btn.text
        for row in fail_on.inline_keyboard
        for btn in row
    }
    assert fail_labels["edit_f:1:delete_fail"].startswith("✅ ")
    assert fail_labels["edit_f:1:delete_other"].startswith("✅ ")
    assert fail_labels["edit_f:1:sched_remind"].startswith("✅ ")
    assert "edit_f:1:repeat" not in fail_labels  # hidden for category alerts

    live_repeat = edit_options_keyboard(
        1,
        "en",
        dest_type="channel",
        show_advanced=True,
    )
    live_labels = {
        (btn.callback_data or ""): btn.text
        for row in live_repeat.inline_keyboard
        for btn in row
    }
    assert "edit_f:1:repeat" in live_labels
    assert not live_labels["edit_f:1:repeat"].startswith(("✅ ", "⬜️ "))

    from i18n import edit_game_options_keyboard

    # Exclude 18+ is a checkbox in the game editor — no follow-up step.
    game_edit = edit_game_options_keyboard(
        1,
        "en",
        tags_label="any",
        viewers_label="any",
        language_label="any",
        exclude_mature=True,
    )
    game_cbs = {
        (btn.callback_data or ""): btn.text
        for row in game_edit.inline_keyboard
        for btn in row
    }
    assert set(game_cbs) == {
        "edit_g:1:tags",
        "edit_g:1:viewers",
        "edit_g:1:language",
        "edit_g:1:mature",
        "edit_g:1:cooldown",
    }
    assert game_cbs["edit_g:1:mature"].startswith("✅ ")
    off = edit_game_options_keyboard(
        1,
        "en",
        tags_label="any",
        viewers_label="any",
        language_label="any",
        exclude_mature=False,
    )
    off_cbs = {
        (btn.callback_data or ""): btn.text
        for row in off.inline_keyboard
        for btn in row
    }
    assert off_cbs["edit_g:1:mature"].startswith("⬜️ ")

    for loc in SUPPORTED_LOCALES:
        chat_kb = chat_button_keyboard(loc)
        assert [b.text for b in chat_kb.inline_keyboard[0]] == [
            t("chat_button_yes", loc)
        ]
        assert [b.text for b in chat_kb.inline_keyboard[1]] == [
            t("chat_button_no", loc)
        ]
        bool_chat = edit_bool_keyboard(1, "chat_button", loc)
        assert [b.text for row in bool_chat.inline_keyboard for b in row] == [
            t("chat_button_yes", loc),
            t("chat_button_no", loc),
        ]

    markers = _list_markers("en")
    with_image = _format_sub_line(_sample_sub(), "en", 1)
    found_img = _found_setting_ids(with_image, markers)
    assert "image" in found_img
    assert "preview" not in found_img  # image forces preview off in list
    _assert_subsequence(found_img, ALERT_SETTING_ORDER)

    with_preview = _format_sub_line(
        _sample_sub(
            image_file_id=None,
            disable_link_preview=True,
        ),
        "en",
        1,
    )
    found_prev = _found_setting_ids(with_preview, markers)
    assert "preview" in found_prev
    assert "image" not in found_prev
    _assert_subsequence(found_prev, ALERT_SETTING_ORDER)
