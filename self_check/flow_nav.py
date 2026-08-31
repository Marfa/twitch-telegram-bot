"""Escape-hatch helpers: Cancel / Back / main-menu Reply keyboard."""
from __future__ import annotations

import re
from typing import Any

from i18n import (
    SUPPORTED_LOCALES,
    all_menu_buttons,
    all_wizard_nav_buttons,
    btn,
    t,
)
from telegram import InlineKeyboardMarkup, ReplyKeyboardMarkup

# Inline callbacks that exit, decline, or step back in a wizard.
_INLINE_ESCAPE_CALLBACK_RE = re.compile(
    r"(?:"
    r":cancel$|"
    r":back$|"
    r"^watch_nav:(?:back|cancel)$|"
    r"^alert_type:cancel$|"
    r"^premium_gate:(?:cancel|skip)$|"
    r"^lang:cancel$|"
    r"^admin_type:cancel$|"
    r"^admin_audience:cancel$|"
    r"^stream_sched:confirm:0$|"
    r"^stream_sched:publish:0$|"
    r"^import_oauth:cancel$|"
    r"^alert_history:menu$|"
    r"^share_decline$|"
    r"^delete_all:no$|"
    r"^list_del_no:\d+$|"
    r"^ignored_words:cancel$|"
    r"^ignore_keywords:(?:cancel|back)$|"
    r"^strip_name:(?:cancel|back)$"
    r")"
)


def _decline_labels() -> set[str]:
    labels: set[str] = set()
    for loc in SUPPORTED_LOCALES:
        labels.add(t("stream_schedule_no", loc))
        labels.add(t("stream_schedule_publish_no", loc))
        labels.add(t("ignored_words_cancel", loc))
        labels.add(t("share_decline", loc))
        labels.add(btn("premium_feat_back", loc))
    return labels


def escape_hatch_reply_labels() -> set[str]:
    return all_wizard_nav_buttons() | all_menu_buttons() | _decline_labels()


def reply_keyboard_labels(markup: ReplyKeyboardMarkup | None) -> set[str]:
    if markup is None:
        return set()
    labels: set[str] = set()
    for row in markup.keyboard:
        for key in row:
            labels.add(key.text)
    return labels


def inline_keyboard_buttons(
    markup: InlineKeyboardMarkup | None,
) -> list[tuple[str, str | None]]:
    if markup is None:
        return []
    out: list[tuple[str, str | None]] = []
    for row in markup.inline_keyboard:
        for key in row:
            out.append((key.text, key.callback_data))
    return out


def markup_has_escape_hatch(markup: Any) -> bool:
    """True when Reply or Inline markup exposes Cancel, Back, or menu navigation."""
    if markup is None:
        return False
    escape = escape_hatch_reply_labels()
    if isinstance(markup, ReplyKeyboardMarkup):
        return bool(reply_keyboard_labels(markup) & escape)
    if isinstance(markup, InlineKeyboardMarkup):
        for text, callback in inline_keyboard_buttons(markup):
            if text in escape:
                return True
            if callback and _INLINE_ESCAPE_CALLBACK_RE.search(callback):
                return True
        return False
    return False


def markups_from_call(kwargs: dict[str, Any]) -> list[Any]:
    markup = kwargs.get("reply_markup")
    return [markup] if markup is not None else []


def turn_has_escape_hatch(
    markups: list[Any], *, pulsed_wizard_keyboard: bool = False
) -> bool:
    if pulsed_wizard_keyboard:
        return True
    return any(markup_has_escape_hatch(m) for m in markups)
