"""Shared Extras / edit / subscription-list order for alert option rows.

Edit-only rows (template, image_del, delete_fail, schedule, dest, …) stay outside
this tuple. List always leads with alert type and ends with dest/thread.
"""

from __future__ import annotations

# Canonical order for advanced options shared by create Extras, edit menu, and
# _format_sub_line (when the setting is active / shown).
ALERT_SETTING_ORDER: tuple[str, ...] = (
    "image",
    "strip",
    "ignore",
    "delay",
    "repeat",
    "delete",
    "buttons",
    "chat",
    "preview",
)

# advopt:toggle:<id> — same as ALERT_SETTING_ORDER ids.
# edit_f:<sub_id>:<field> for the primary row of each setting.
EDIT_FIELD: dict[str, str] = {
    "image": "image",
    "strip": "strip",
    "ignore": "ignore_keywords",
    "delay": "delay",
    "repeat": "repeat",
    "delete": "delete_old",
    "buttons": "custom_buttons",
    "chat": "chat_button",
    "preview": "preview",
}

ADVOPT_LABEL_KEY: dict[str, str] = {
    "image": "advanced_options_image",
    "strip": "advanced_options_strip",
    "ignore": "advanced_options_ignore",
    "delay": "advanced_options_delay",
    "repeat": "advanced_options_repeat",
    "delete": "advanced_options_delete",
    "buttons": "advanced_options_buttons",
    "chat": "advanced_options_chat",
    "preview": "advanced_options_preview",
}
