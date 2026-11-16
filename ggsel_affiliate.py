"""GGLead → ggsel partner buy links for RU game cards."""
from __future__ import annotations

from urllib.parse import quote, quote_plus

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import GGLEAD_FLOW_ID, GGLEAD_TRACK_BASE
from i18n import t


def partner_search_url(
    game_name: str, *, flow_id: str | None = None
) -> str | None:
    """Build gglead.org tracking URL with ulp=ggsel search by game name."""
    name = (game_name or "").strip()
    fid = (GGLEAD_FLOW_ID if flow_id is None else flow_id).strip()
    if not name or not fid:
        return None
    search = f"https://ggsel.net/?query={quote_plus(name)}"
    base = GGLEAD_TRACK_BASE.rstrip("/") + "/"
    return f"{base}?flow={quote(fid, safe='')}&ulp={quote(search, safe='')}"


def with_buy_button(
    markup: InlineKeyboardMarkup | None,
    *,
    lang: str,
    game_name: str,
) -> InlineKeyboardMarkup | None:
    """Prepend «Купить» URL row for ru when GGLEAD_FLOW_ID is set."""
    if not str(lang or "").startswith("ru"):
        return markup
    url = partner_search_url(game_name)
    if not url:
        return markup
    buy = InlineKeyboardButton(t("game_buy_button", lang), url=url)
    rows: list[list[InlineKeyboardButton]] = [[buy]]
    if markup and markup.inline_keyboard:
        rows.extend(list(markup.inline_keyboard))
    return InlineKeyboardMarkup(rows)
