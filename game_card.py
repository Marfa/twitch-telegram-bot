"""Unified game card HTML for giveaways, release alerts, and game info."""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from i18n import DEFAULT_LOCALE, igdb_attribution, t


_CAP = 1024


@dataclass
class GameCardFields:
    name: str
    year: str = ""
    genre: str = ""
    publisher: str = ""
    developer: str = ""
    platforms: str = ""
    dates: str = ""
    dates_key: str = "game_card_release_dates"
    summary: str = ""
    # Prebuilt HTML value for the store line (linked name). Survives Forward.
    store_html: str = ""


def year_from_unix(ts: Any) -> str:
    try:
        n = int(ts or 0)
        if n <= 0:
            return ""
        return str(datetime.fromtimestamp(n, tz=timezone.utc).year)
    except Exception:
        return ""


def format_release_date(ts: int, lang: str) -> str:
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return str(ts)
    if str(lang or "").startswith("ru"):
        return dt.strftime("%d.%m.%Y")
    return dt.strftime("%Y-%m-%d")


def escaped_fit(text: str, room: int) -> str:
    """Escape plain text so the result length is ≤ room (no mid-tag cuts)."""
    if room <= 0 or not text:
        return ""
    raw = text
    escaped = html.escape(raw)
    if len(escaped) <= room:
        return escaped
    ellipsis_room = 1 if room > 1 else 0
    target = max(0, room - ellipsis_room)
    while raw and len(html.escape(raw)) > target:
        over = len(html.escape(raw)) - target
        step = max(1, over // 2)
        raw = raw[: max(0, len(raw) - step)]
    if not raw:
        return ""
    out = html.escape(raw)
    if ellipsis_room and len(text) > len(raw):
        out = out + "…"
    return out


def _bold_label(label: str, value: str) -> str:
    return f"<b>{html.escape(label)}</b> {value}"


def build_game_card_html(fields: GameCardFields, lang: str, *, footer: str = "") -> str:
    """Photo captions ≤1024. Never slice assembled HTML — that splits <b>/<a>."""
    year_bit = f" ({html.escape(fields.year)})" if fields.year else ""
    name_room = max(16, 180 - len(year_bit))
    title = f"<b>{escaped_fit(fields.name, name_room)}{year_bit}</b>"
    genre = html.escape(fields.genre) if fields.genre else "—"
    pub = html.escape(fields.publisher) if fields.publisher else "—"
    dev = html.escape(fields.developer) if fields.developer else "—"
    plats = html.escape(fields.platforms) if fields.platforms else "—"
    dates = html.escape(fields.dates) if fields.dates else "—"
    meta_lines = [
        title,
        _bold_label(t("game_card_genre", lang), genre),
        _bold_label(t("giveaways_publisher", lang), pub),
        _bold_label(t("giveaways_developer", lang), dev),
        _bold_label(t("giveaways_platforms", lang), plats),
    ]
    store = (fields.store_html or "").strip()
    if store:
        meta_lines.append(_bold_label(t("giveaways_store", lang), store))
    meta = "\n".join(meta_lines)
    dates_line = _bold_label(t(fields.dates_key, lang), dates)
    dates_block = f"\n\n{dates_line}"
    use_footer = footer
    footer_block = f"\n\n{use_footer}" if use_footer else ""
    if len(meta) + len(dates_block) + len(footer_block) > _CAP:
        use_footer = ""
        footer_block = ""
    if len(meta) + len(dates_block) + len(footer_block) > _CAP:
        dates_block = ""
    room = _CAP - len(meta) - len(dates_block) - len(footer_block)
    desc = ""
    if fields.summary and room > 22:
        room -= 2
        desc = escaped_fit(fields.summary.strip(), room)
    parts = [meta]
    if dates_block:
        parts.append("")
        parts.append(dates_line)
    if desc:
        parts.extend(["", desc])
    if use_footer:
        parts.extend(["", use_footer])
    return "\n".join(parts)


def release_dates_line(db: Any, game_id: int, lang: str) -> str:
    """Actual or planned release date(s) from local IGDB dumps."""
    rows = db.igdb_release_dates_for_game(int(game_id or 0)) or []
    seen: list[str] = []
    for r in sorted(rows, key=lambda x: (int(x.get("date") or 0), str(x.get("platform_name") or ""))):
        try:
            date_s = format_release_date(int(r["date"]), lang)
        except (TypeError, ValueError):
            continue
        if not date_s:
            continue
        plat = str(r.get("platform_name") or "").strip()
        s = f"{date_s} ({plat})" if plat and plat != "—" else date_s
        if s not in seen:
            seen.append(s)
    if seen:
        extra = "…" if len(seen) > 5 else ""
        return ", ".join(seen[:5]) + extra
    game = db.igdb_game_by_id(int(game_id or 0)) or {}
    frd = game.get("first_release_date")
    if frd:
        return format_release_date(int(frd), lang)
    return ""


def platforms_line(db: Any, game_id: int) -> str:
    plats = db.igdb_platforms_for_game(int(game_id or 0)) or []
    names = [
        str(p.get("platform_name") or "").strip()
        for p in plats
        if str(p.get("platform_name") or "").strip()
    ]
    return ", ".join(names)


def genre_line(db: Any, game_id: int) -> str:
    names = db.igdb_genre_names_for_game(int(game_id or 0)) or []
    return ", ".join(n for n in names if n)


def fields_from_igdb(
    db: Any,
    game_id: int,
    *,
    lang: str = DEFAULT_LOCALE,
    name: str | None = None,
    summary: str | None = None,
) -> GameCardFields:
    from twitch import localize_igdb_summary

    game = db.igdb_game_by_id(int(game_id or 0)) or {}
    gname = (name or str(game.get("name") or "")).strip() or f"#{game_id}"
    raw_summary = (
        summary
        if summary is not None
        else str(game.get("summary") or "")
    ).strip()
    localized = (
        localize_igdb_summary(raw_summary, lang, db=db) if raw_summary else ""
    )
    publisher, developer = db.igdb_publisher_developer_names(int(game_id or 0))
    return GameCardFields(
        name=gname,
        year=year_from_unix(game.get("first_release_date")),
        genre=genre_line(db, game_id),
        publisher=publisher,
        developer=developer,
        platforms=platforms_line(db, game_id),
        dates=release_dates_line(db, game_id, lang),
        dates_key="game_card_release_dates",
        summary=localized if localized != "—" else "",
    )


def attribution_footer(lang: str, *, page_url: str | None = None) -> str:
    url = page_url or "https://www.igdb.com"
    return igdb_attribution(lang, url=url)
