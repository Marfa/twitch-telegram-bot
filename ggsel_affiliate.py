"""GGLead partner buy link for RU game cards."""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from i18n import t

BUY_URL = "https://gglead.org/go22483"


def with_buy_button(
    markup: InlineKeyboardMarkup | None,
    *,
    lang: str,
) -> InlineKeyboardMarkup | None:
    """Prepend «Купить» URL row for ru only."""
    if not str(lang or "").startswith("ru"):
        return markup
    buy = InlineKeyboardButton(t("game_buy_button", lang), url=BUY_URL)
    rows: list[list[InlineKeyboardButton]] = [[buy]]
    if markup and markup.inline_keyboard:
        rows.extend(list(markup.inline_keyboard))
    return InlineKeyboardMarkup(rows)
