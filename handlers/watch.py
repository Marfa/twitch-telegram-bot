from __future__ import annotations

import asyncio
import html
import logging
import re
import secrets
from typing import Any

from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler

import analytics
import demo_mode
import premium as prem
from bot_helpers import _menu, _user_lang
from handlers.alert_history import _twitch_vod_url
from db import (
    Database,
    Subscription,
    WatchPrefs,
    dump_category_watch_prefs,
    is_category_watch_sub,
    parse_category_watch_prefs,
    watch_filter_auto_name,
)
from i18n import (
    DEFAULT_LOCALE,
    t,
    watch_cats_nav_keyboard,
    watch_cats_pick_keyboard,
    watch_filters_keyboard,
    watch_lang_keyboard,
    watch_mode_keyboard,
    watch_suggest_keyboard,
    watch_tags_keyboard,
    watch_viewers_keyboard,
)
from twitch import (
    TwitchClient,
    filter_streams_for_watch,
    normalize_watch_tags,
    pick_random_streams,
)

logger = logging.getLogger(__name__)


def _ws() -> dict[str, int]:
    from bot import (
        WATCH_CATEGORIES,
        WATCH_DUP,
        WATCH_FILTERS,
        WATCH_LANGUAGE,
        WATCH_PICK,
        WATCH_TAGS,
        WATCH_VIEWERS,
    )

    return {
        "WATCH_CATEGORIES": WATCH_CATEGORIES,
        "WATCH_DUP": WATCH_DUP,
        "WATCH_FILTERS": WATCH_FILTERS,
        "WATCH_LANGUAGE": WATCH_LANGUAGE,
        "WATCH_PICK": WATCH_PICK,
        "WATCH_TAGS": WATCH_TAGS,
        "WATCH_VIEWERS": WATCH_VIEWERS,
    }


def _subs_for_owner(db: Database, owner_id: int):
    from bot import _subs_for_owner as _impl

    return _impl(db, owner_id)


def _category_ids(prefs: WatchPrefs) -> tuple[str, ...]:
    return tuple(
        sorted(
            str(c.get("id") or "")
            for c in (prefs.categories or [])
            if str(c.get("id") or "").strip()
        )
    )


def _category_ids_from_cats(cats: list[dict[str, str]]) -> tuple[str, ...]:
    return tuple(
        sorted(
            str(c.get("id") or "")
            for c in (cats or [])
            if str(c.get("id") or "").strip()
        )
    )


def _find_category_watch_by_ids(
    db: Database, user_id: int, want_ids: tuple[str, ...]
) -> Subscription | None:
    if not want_ids:
        return None
    for sub in _subs_for_owner(db, user_id):
        if not is_category_watch_sub(sub):
            continue
        existing = parse_category_watch_prefs(sub.category_watch_prefs)
        if existing and _category_ids(existing) == want_ids:
            return sub
    return None


def _set_wizard_back(context: ContextTypes.DEFAULT_TYPE, state: int) -> None:
    from bot import _set_wizard_back as _impl

    _impl(context, state)


_WATCH_MAX_CATS = 1
_WATCH_SUGGEST_N = 5
# Kept for compatibility with existing imports in other modules/self-checks.
_WATCH_MAX_TAGS = 10

_WATCH_VIEWERS_RE = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+))?\s*$")
_WATCH_LANG_RE = re.compile(r"^[a-zA-Z]{2}$")

def _parse_watch_viewers(text: str) -> tuple[int, int | None] | None:
    m = _WATCH_VIEWERS_RE.match(text.strip())
    if not m:
        return None
    lo = int(m.group(1))
    hi = int(m.group(2)) if m.group(2) is not None else None
    if hi is not None and hi < lo:
        lo, hi = hi, lo
    return lo, hi


def _watch_viewers_label(prefs: WatchPrefs, lang: str) -> str:
    if prefs.min_viewers <= 0 and prefs.max_viewers is None:
        return t("watch_viewers_label_any", lang)
    if prefs.max_viewers is None:
        return t("watch_viewers_label_min", lang, min=prefs.min_viewers)
    return t(
        "watch_viewers_label_range",
        lang,
        min=prefs.min_viewers,
        max=prefs.max_viewers,
    )


def _watch_prefs_summary(prefs: WatchPrefs, lang: str) -> str:
    cats = ", ".join(c["name"] for c in prefs.categories) or "—"
    tags = ", ".join(prefs.tags) if prefs.tags else t("watch_tags_label_any", lang)
    return t(
        "watch_prefs_summary",
        lang,
        cats=cats,
        viewers=_watch_viewers_label(prefs, lang),
        language=prefs.language or t("watch_lang_label_any", lang),
        tags=tags,
        mature=(
            t("watch_mature_label_exclude", lang)
            if prefs.exclude_mature
            else t("watch_mature_label_allow", lang)
        ),
    )


def _premium_channel_badge_html(lang: str, *, login: str, db: Database) -> str:
    if not prem.is_promo_channel(login, db):
        return ""
    from config import PUBLIC_BASE_URL

    tip = html.escape(t("premium_channel_badge_title", lang))
    star = html.escape(t("premium_channel_badge", lang))
    href = html.escape(
        f"{PUBLIC_BASE_URL}/app/premium-channel"
        if PUBLIC_BASE_URL
        else "https://twitch.tv/" + login
    )
    # title= works in some Telegram clients / webviews on hover & long-press.
    return f' <a href="{href}" title="{tip}">{star}</a>'


def _format_watch_suggestions(
    streams: list[dict],
    prefs: WatchPrefs,
    lang: str,
    *,
    db: Database,
    header_key: str = "watch_suggest_header",
    include_prefs: bool = True,
) -> str:
    lines = [t(header_key, lang), ""]
    if include_prefs:
        lines.append(_watch_prefs_summary(prefs, lang))
        lines.append("")
    for i, s in enumerate(streams, start=1):
        login_raw = str(s.get("user_login") or "").lower()
        login = html.escape(login_raw)
        display = html.escape(str(s.get("user_name") or login_raw))
        title = html.escape(str(s.get("title") or "—"))
        game = html.escape(str(s.get("game_name") or "—"))
        viewers = int(s.get("viewer_count") or 0)
        badge = _premium_channel_badge_html(lang, login=login_raw, db=db)
        lines.append(
            t(
                "watch_suggest_item",
                lang,
                n=i,
                display=display,
                login=login,
                title=title,
                game=game,
                viewers=viewers,
                premium_badge=badge,
            )
        )
        lines.append("")
    return "\n".join(lines).rstrip()


def _format_watch_vod_suggestions(
    videos: list[dict], prefs: WatchPrefs, lang: str
) -> str:
    lines = [
        t("watch_suggest_vod_header", lang),
        "",
        _watch_prefs_summary(prefs, lang),
        "",
    ]
    n = 0
    for v in videos:
        vid = str(v.get("id") or "").strip().lstrip("v")
        if not vid:
            continue
        n += 1
        login = html.escape(str(v.get("user_login") or ""))
        display = html.escape(str(v.get("user_name") or login))
        title = html.escape(str(v.get("title") or "—"))
        game = html.escape(str(v.get("game_name") or "—"))
        duration = html.escape(str(v.get("duration") or "—"))
        # Always /videos/{id} — never the channel page.
        url = html.escape(_twitch_vod_url(vid))
        lines.append(
            t(
                "watch_suggest_vod_item",
                lang,
                n=n,
                display=display,
                login=login,
                title=title,
                game=game,
                duration=duration,
                url=url,
            )
        )
        lines.append("")
    return "\n".join(lines).rstrip()


def _promo_channel_user_ids(db: Database, twitch: TwitchClient) -> list[str]:
    """Twitch user ids for config + paid promo channels."""
    ids: list[str] = []
    seen: set[str] = set()
    login = prem.twitch_channel_login()
    try:
        user = twitch.get_user(login)
    except Exception:
        user = None
    if user:
        uid = str(user["id"])
        ids.append(uid)
        seen.add(uid)
    for ch in db.list_premium_channels():
        uid = str(ch.twitch_user_id)
        if uid and uid not in seen:
            ids.append(uid)
            seen.add(uid)
    return ids


def _promo_streams_matching(
    db: Database, twitch: TwitchClient, prefs: WatchPrefs
) -> list[dict]:
    """Live promo channels that match watch filters (extra slots)."""
    uids = _promo_channel_user_ids(db, twitch)
    if not uids:
        return []
    try:
        live = twitch.get_live_streams(uids)
    except Exception:
        logger.exception("promo live streams fetch failed")
        return []
    streams = list(live.values())
    cat_ids = {str(c.get("id") or "") for c in prefs.categories if c.get("id")}
    if cat_ids:
        streams = [s for s in streams if str(s.get("game_id") or "") in cat_ids]
    if prefs.language:
        streams = [
            s for s in streams if (s.get("language") or "") == prefs.language
        ]
    return filter_streams_for_watch(
        streams,
        min_viewers=prefs.min_viewers,
        max_viewers=prefs.max_viewers,
        exclude_mature=prefs.exclude_mature,
        tags=prefs.tags,
    )


def _live_promo_streams(db: Database, twitch: TwitchClient) -> list[dict]:
    """All currently live Premium / promo channels (no watch filters)."""
    uids = _promo_channel_user_ids(db, twitch)
    if not uids:
        return []
    try:
        live = twitch.get_live_streams(uids)
    except Exception:
        logger.exception("live promo streams fetch failed")
        return []
    return list(live.values())


def _watch_cats_keyboard(
    context: ContextTypes.DEFAULT_TYPE, lang: str, *, has_cats: bool
) -> InlineKeyboardMarkup:
    return watch_cats_nav_keyboard(
        lang,
        has_cats=has_cats,
        show_recommended=bool(context.user_data.get("watch_has_recommended")),
    )


async def _refresh_watch_recommended_flag(
    context: ContextTypes.DEFAULT_TYPE,
    db: Database,
    twitch: TwitchClient,
) -> bool:
    streams = await asyncio.to_thread(_live_promo_streams, db, twitch)
    flag = bool(streams)
    context.user_data["watch_has_recommended"] = flag
    return flag


async def _fetch_recommended_promo_streams(
    db: Database, twitch: TwitchClient, *, n: int = _WATCH_SUGGEST_N
) -> list[dict]:
    streams = await asyncio.to_thread(_live_promo_streams, db, twitch)
    return pick_random_streams(streams, n)


async def _fetch_watch_suggestions(
    twitch: TwitchClient, prefs: WatchPrefs, *, db: Database | None = None
) -> list[dict]:
    pooled: list[dict] = []
    for cat in prefs.categories:
        try:
            batch = await asyncio.to_thread(
                twitch.get_streams_by_game,
                cat["id"],
                language=prefs.language,
                first=100,
            )
        except Exception:
            logger.exception("watch streams fetch failed for game_id=%s", cat.get("id"))
            continue
        pooled.extend(batch)
    filtered = filter_streams_for_watch(
        pooled,
        min_viewers=prefs.min_viewers,
        max_viewers=prefs.max_viewers,
        exclude_mature=prefs.exclude_mature,
        tags=prefs.tags,
    )
    picked = pick_random_streams(filtered, _WATCH_SUGGEST_N)
    if db is None:
        return picked
    promo = await asyncio.to_thread(_promo_streams_matching, db, twitch, prefs)
    if not promo:
        return picked
    seen = {str(s.get("user_id") or s.get("user_login") or "").lower() for s in picked}
    extra: list[dict] = []
    for s in promo:
        key = str(s.get("user_id") or s.get("user_login") or "").lower()
        if key and key not in seen:
            extra.append(s)
            seen.add(key)
    return extra + picked


def _bot_lang_to_twitch(lang: str) -> str:
    loc = (lang or DEFAULT_LOCALE).lower()
    if loc.startswith("ru"):
        return "ru"
    return "en"


def _lucky_streams_from_igdb(
    twitch: TwitchClient, *, prefer_language: str
) -> tuple[list[dict[str, str]], list[dict], list[dict]]:
    """Exact order:
    1) IGDB random ×5 → live bot language, else any
    2) IGDB recently released ×5 → live bot language, else any
    3) If still empty → VOD for categories from both batches (bot language, else any)
    18+ allowed for live.
    Returns (categories, live_streams, vods).
    """

    def _streams_for_cats(
        cats: list[dict[str, str]], *, language: str | None
    ) -> list[dict]:
        pooled: list[dict] = []
        for cat in cats:
            try:
                pooled.extend(
                    twitch.get_streams_by_game(
                        cat["id"], language=language, first=100
                    )
                )
            except Exception:
                logger.exception(
                    "lucky streams fetch failed for game_id=%s", cat.get("id")
                )
        filtered = filter_streams_for_watch(pooled, exclude_mature=False)
        return pick_random_streams(filtered, _WATCH_SUGGEST_N)

    def _vods_for_cats(
        cats: list[dict[str, str]], *, language: str | None
    ) -> list[dict]:
        pooled: list[dict] = []
        for cat in cats:
            try:
                batch = twitch.get_videos_by_game(
                    cat["id"], language=language, first=100
                )
            except Exception:
                logger.exception(
                    "lucky VOD fetch failed for game_id=%s", cat.get("id")
                )
                continue
            game_name = str(cat.get("name") or "—")
            for item in batch:
                vid = str(item.get("id") or "").strip().lstrip("v")
                if not vid:
                    continue
                row = dict(item)
                row["id"] = vid
                row["game_name"] = game_name
                pooled.append(row)
        return pick_random_streams(pooled, _WATCH_SUGGEST_N)

    def _pick_lang_then_any(
        game_rows: list,
    ) -> tuple[list[dict[str, str]], list[dict]]:
        cats = twitch.resolve_igdb_games_to_twitch_categories(game_rows)
        if not cats:
            logger.info("lucky: no Twitch categories for %s IGDB games", len(game_rows))
            return [], []
        streams = _streams_for_cats(cats, language=prefer_language)
        if streams:
            return cats, streams
        streams = _streams_for_cats(cats, language=None)
        return cats, streams

    random_rows = twitch.igdb_random_games(5)
    cats, streams = _pick_lang_then_any(random_rows)
    if streams:
        return cats, streams, []
    recent_rows = twitch.igdb_recently_released_games(5)
    cats2, streams = _pick_lang_then_any(recent_rows)
    if streams:
        return cats2, streams, []
    top_rows = twitch.igdb_top100_games(5)
    cats3, streams = _pick_lang_then_any(top_rows)
    if streams:
        return cats3, streams, []
    # VOD for all categories from all batches (random first), lang then any.
    use_cats: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for group in (cats, cats2, cats3):
        for cat in group:
            cid = str(cat.get("id") or "")
            if not cid or cid in seen_ids:
                continue
            seen_ids.add(cid)
            use_cats.append(cat)
    if not use_cats:
        return [], [], []
    vods = _vods_for_cats(use_cats, language=prefer_language)
    if not vods:
        vods = _vods_for_cats(use_cats, language=None)
    return use_cats, [], vods


async def _fetch_lucky_watch_suggestions(
    twitch: TwitchClient, *, prefer_language: str
) -> tuple[list[dict[str, str]], list[dict], list[dict]]:
    return await asyncio.to_thread(
        _lucky_streams_from_igdb, twitch, prefer_language=prefer_language
    )


def _set_watch_lucky_mode(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, *, enabled: bool
) -> None:
    modes = context.application.bot_data.setdefault("watch_lucky_mode", {})
    if enabled:
        modes[user_id] = True
        _set_watch_recommended_mode(context, user_id, enabled=False)
    else:
        modes.pop(user_id, None)


def _watch_lucky_mode(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    return bool((context.application.bot_data.get("watch_lucky_mode") or {}).get(user_id))


def _set_watch_recommended_mode(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, *, enabled: bool
) -> None:
    modes = context.application.bot_data.setdefault("watch_recommended_mode", {})
    if enabled:
        modes[user_id] = True
        # Mutual exclusion with lucky "again" path.
        context.application.bot_data.setdefault("watch_lucky_mode", {}).pop(user_id, None)
    else:
        modes.pop(user_id, None)


def _watch_recommended_mode(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    return bool(
        (context.application.bot_data.get("watch_recommended_mode") or {}).get(user_id)
    )


async def _send_recommended_promo_suggestions(
    *,
    bot,
    chat_id: int,
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    edit_message=None,
) -> None:
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    _set_watch_recommended_mode(context, user_id, enabled=True)
    try:
        streams = await _fetch_recommended_promo_streams(db, twitch)
    except Exception:
        logger.exception("watch recommended fetch failed")
        text = t("watch_suggest_error", lang)
        markup = watch_suggest_keyboard(lang, offer_create_alerts=False)
        if edit_message is not None:
            try:
                await edit_message.edit_text(text, reply_markup=markup)
                return
            except BadRequest:
                pass
        await bot.send_message(chat_id, text, reply_markup=markup)
        return
    if not streams:
        text = t("watch_recommended_empty", lang)
        markup = watch_suggest_keyboard(lang, offer_create_alerts=False)
    else:
        prefs = WatchPrefs(
            categories=[],
            min_viewers=0,
            max_viewers=None,
            language=None,
            tags=[],
            exclude_mature=False,
        )
        context.application.bot_data.setdefault("watch_last_prefs", {})[user_id] = prefs
        text = _format_watch_suggestions(
            streams,
            prefs,
            lang,
            db=db,
            header_key="watch_recommended_header",
            include_prefs=False,
        )
        markup = watch_suggest_keyboard(lang, offer_create_alerts=False)
    if edit_message is not None:
        try:
            await edit_message.edit_text(
                text,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return
        except BadRequest:
            pass
    await bot.send_message(
        chat_id, t("menu_main", lang), reply_markup=_menu(lang, user_id)
    )
    await bot.send_message(
        chat_id,
        text,
        reply_markup=markup,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def _fetch_watch_vod_suggestions(
    twitch: TwitchClient, prefs: WatchPrefs
) -> list[dict]:
    """Fallback when no live streams: recent archives by category (+ language)."""
    pooled: list[dict] = []
    for cat in prefs.categories:
        try:
            batch = await asyncio.to_thread(
                twitch.get_videos_by_game,
                cat["id"],
                language=prefs.language,
                first=100,
            )
        except Exception:
            logger.exception("watch VOD fetch failed for game_id=%s", cat.get("id"))
            continue
        game_name = str(cat.get("name") or "—")
        for item in batch:
            vid = str(item.get("id") or "").strip().lstrip("v")
            if not vid:
                continue
            row = dict(item)
            row["id"] = vid
            row["game_name"] = game_name
            pooled.append(row)
    # Viewers/tags/mature do not map cleanly to Helix videos — category + language only.
    return pick_random_streams(pooled, _WATCH_SUGGEST_N)


def _watch_channel_refs(items: list[dict]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        uid = str(item.get("user_id") or "").strip()
        login = str(item.get("user_login") or "").strip().lower()
        if not uid or not login or uid in seen:
            continue
        seen.add(uid)
        out.append({"user_id": uid, "user_login": login})
    return out


async def _send_watch_suggestions(
    *,
    bot,
    chat_id: int,
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    prefs: WatchPrefs,
    edit_message=None,
    streams: list[dict] | None = None,
    vods: list[dict] | None = None,
    allow_vod: bool = True,
    offer_create_alerts: bool = False,
) -> None:
    lang = _user_lang(context, user_id)
    context.application.bot_data.setdefault("watch_last_prefs", {})[user_id] = prefs
    twitch: TwitchClient = context.application.bot_data["twitch"]
    try:
        if streams is None and vods is None:
            streams = await _fetch_watch_suggestions(
                twitch, prefs, db=context.application.bot_data["db"]
            )
    except Exception:
        logger.exception("watch suggestions failed")
        text = t("watch_suggest_error", lang)
        markup = watch_suggest_keyboard(lang, offer_create_alerts=offer_create_alerts)
        if edit_message is not None:
            try:
                await edit_message.edit_text(text, reply_markup=markup)
                return
            except BadRequest:
                pass
        await bot.send_message(
            chat_id, t("menu_main", lang), reply_markup=_menu(lang, user_id)
        )
        await bot.send_message(chat_id, text, reply_markup=markup)
        return

    if streams:
        text = _format_watch_suggestions(
            streams, prefs, lang, db=context.application.bot_data["db"]
        )
    elif vods:
        text = _format_watch_vod_suggestions(vods, prefs, lang)
    elif allow_vod:
        try:
            fetched_vods = await _fetch_watch_vod_suggestions(twitch, prefs)
        except Exception:
            logger.exception("watch VOD suggestions failed")
            fetched_vods = []
        if fetched_vods:
            text = _format_watch_vod_suggestions(fetched_vods, prefs, lang)
        else:
            text = (
                t("watch_suggest_empty", lang)
                + "\n\n"
                + _watch_prefs_summary(prefs, lang)
            )
    else:
        text = t("watch_lucky_empty", lang)
    markup = watch_suggest_keyboard(lang, offer_create_alerts=offer_create_alerts)
    if edit_message is not None:
        try:
            await edit_message.edit_text(
                text,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return
        except BadRequest:
            pass
    await bot.send_message(
        chat_id,
        t("menu_main", lang),
        reply_markup=_menu(lang, user_id),
    )
    await bot.send_message(
        chat_id,
        text,
        reply_markup=markup,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


def _watch_prefs_from_user_data(context: ContextTypes.DEFAULT_TYPE) -> WatchPrefs:
    max_v = context.user_data.get("watch_max_viewers")
    return WatchPrefs(
        categories=list(context.user_data.get("watch_categories") or []),
        min_viewers=int(context.user_data.get("watch_min_viewers") or 0),
        max_viewers=int(max_v) if max_v is not None else None,
        language=context.user_data.get("watch_language"),
        tags=list(context.user_data.get("watch_tags") or []),
        exclude_mature=bool(context.user_data.get("watch_exclude_mature", True)),
    )


def _resolve_watch_prefs(
    context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> WatchPrefs | None:
    last = context.application.bot_data.get("watch_last_prefs") or {}
    cached = last.get(user_id)
    if isinstance(cached, WatchPrefs):
        return cached
    db: Database = context.application.bot_data["db"]
    filters = db.get_watch_filters(user_id)
    return filters[0].prefs if filters else None


async def _start_watch_wizard(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    create_alert = bool(context.user_data.get("watch_create_alert"))
    context.user_data.clear()
    context.user_data["watch_create_alert"] = create_alert
    context.user_data["watch_categories"] = []
    context.user_data["watch_tags"] = []
    context.user_data["watch_min_viewers"] = 0
    context.user_data["watch_max_viewers"] = None
    context.user_data["watch_language"] = None
    context.user_data["watch_exclude_mature"] = True
    context.user_data["watch_want_tags"] = False
    context.user_data["watch_want_viewers"] = False
    context.user_data["watch_want_language"] = False
    context.user_data["watch_want_mature"] = False
    return await _go_watch_categories_prompt(update, context, lang)


async def _go_watch_mode_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    msg = update.effective_message
    if update.callback_query and update.callback_query.message:
        try:
            await update.callback_query.edit_message_text(
                t("watch_mode_prompt", lang),
                reply_markup=watch_mode_keyboard(lang),
            )
            _set_wizard_back(context, _ws()["WATCH_PICK"])
            return _ws()["WATCH_PICK"]
        except BadRequest:
            pass
    await msg.reply_text(
        t("watch_mode_prompt", lang),
        reply_markup=watch_mode_keyboard(lang),
    )
    _set_wizard_back(context, _ws()["WATCH_PICK"])
    return _ws()["WATCH_PICK"]


async def _go_watch_categories_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    await _refresh_watch_recommended_flag(context, db, twitch)
    cats = context.user_data.setdefault("watch_categories", [])
    if cats:
        text = t(
            "watch_cats_added",
            lang,
            name=cats[-1]["name"],
            count=len(cats),
            max=_WATCH_MAX_CATS,
            list=", ".join(c["name"] for c in cats),
        )
    else:
        text = t("watch_cats_prompt", lang, max=_WATCH_MAX_CATS)
    await update.effective_message.reply_text(
        text,
        reply_markup=_watch_cats_keyboard(context, lang, has_cats=bool(cats)),
        parse_mode=ParseMode.HTML,
    )
    _set_wizard_back(context, _ws()["WATCH_CATEGORIES"])
    return _ws()["WATCH_CATEGORIES"]


async def _go_watch_filters_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    context.user_data.setdefault("watch_want_tags", False)
    context.user_data.setdefault("watch_want_viewers", False)
    context.user_data.setdefault("watch_want_language", False)
    context.user_data.setdefault("watch_want_mature", False)
    text = t("watch_filt_prompt", lang)
    markup = watch_filters_keyboard(
        lang,
        want_tags=bool(context.user_data.get("watch_want_tags")),
        want_viewers=bool(context.user_data.get("watch_want_viewers")),
        want_language=bool(context.user_data.get("watch_want_language")),
        want_mature=bool(context.user_data.get("watch_want_mature")),
    )
    query = update.callback_query
    if query:
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except BadRequest:
            await context.bot.send_message(
                query.message.chat_id, text, reply_markup=markup
            )
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)
    _set_wizard_back(context, _ws()["WATCH_FILTERS"])
    return _ws()["WATCH_FILTERS"]


def _watch_detail_queue(context: ContextTypes.DEFAULT_TYPE) -> list[str]:
    q: list[str] = []
    if context.user_data.get("watch_want_tags"):
        q.append("tags")
    if context.user_data.get("watch_want_viewers"):
        q.append("viewers")
    if context.user_data.get("watch_want_language"):
        q.append("language")
    return q


async def _go_watch_next_detail(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    queue = list(context.user_data.get("watch_detail_queue") or [])
    if not queue:
        return await _finalize_watch_wizard(update, context, lang)
    step = queue.pop(0)
    context.user_data["watch_detail_queue"] = queue
    if step == "tags":
        return await _go_watch_tags_prompt(update, context, lang)
    if step == "viewers":
        return await _go_watch_viewers_prompt(update, context, lang)
    if step == "language":
        return await _go_watch_language_prompt(update, context, lang)
    return await _finalize_watch_wizard(update, context, lang)


async def _go_watch_tags_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    await update.effective_message.reply_text(
        t("watch_tags_prompt", lang),
        reply_markup=watch_tags_keyboard(lang),
        parse_mode=ParseMode.HTML,
    )
    _set_wizard_back(context, _ws()["WATCH_TAGS"])
    return _ws()["WATCH_TAGS"]


async def _go_watch_viewers_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    await update.effective_message.reply_text(
        t("watch_viewers_prompt", lang),
        reply_markup=watch_viewers_keyboard(lang),
        parse_mode=ParseMode.HTML,
    )
    _set_wizard_back(context, _ws()["WATCH_VIEWERS"])
    return _ws()["WATCH_VIEWERS"]


async def _go_watch_language_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    context.user_data.pop("watch_lang_await_other", None)
    await update.effective_message.reply_text(
        t("watch_lang_prompt", lang),
        reply_markup=watch_lang_keyboard(lang),
    )
    _set_wizard_back(context, _ws()["WATCH_LANGUAGE"])
    return _ws()["WATCH_LANGUAGE"]


async def _finalize_watch_wizard(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    db: Database = context.application.bot_data["db"]
    prefs = _watch_prefs_from_user_data(context)
    create_alert = bool(context.user_data.get("watch_create_alert"))
    if update.callback_query:
        try:
            await update.callback_query.edit_message_reply_markup(None)
        except BadRequest:
            pass
    if create_alert:
        text, sub, status = await create_category_watch_subscription(
            context.bot,
            db,
            user_id,
            lang,
            prefs,
            allow_duplicate=bool(context.user_data.get("watch_allow_duplicate")),
        )
        if status == "watch_create_alerts_dup" and sub is not None:
            from i18n import alert_dup_keyboard

            context.user_data["alert_dup_force"] = {
                "kind": "game",
                "prefs": dump_category_watch_prefs(prefs),
            }
            await context.bot.send_message(
                chat_id,
                text,
                reply_markup=alert_dup_keyboard(lang, sub.id),
            )
            context.user_data.clear()
            _set_watch_lucky_mode(context, user_id, enabled=False)
            return ConversationHandler.END
        await context.bot.send_message(
            chat_id,
            text,
            parse_mode=ParseMode.HTML,
        )
    context.user_data.clear()
    _set_watch_lucky_mode(context, user_id, enabled=False)
    await _send_watch_suggestions(
        bot=context.bot,
        chat_id=chat_id,
        user_id=user_id,
        context=context,
        prefs=prefs,
        offer_create_alerts=False,
    )
    return ConversationHandler.END


async def start_what_to_watch(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    user_id = update.effective_user.id
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    lang = _user_lang(context, user_id)
    context.user_data.clear()
    _set_watch_lucky_mode(context, user_id, enabled=False)
    _set_watch_recommended_mode(context, user_id, enabled=False)
    analytics.capture(
        user_id,
        "watch_opened",
        {"has_saved_filters": False},
    )
    return await _go_watch_mode_prompt(update, context, lang)


async def start_watch_lucky(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """📦 Other → What to watch?: I'm-feeling-lucky only (no filter wizard)."""
    user_id = update.effective_user.id
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    lang = _user_lang(context, user_id)
    context.user_data.clear()
    _set_watch_recommended_mode(context, user_id, enabled=False)
    analytics.capture(user_id, "watch_lucky_opened", {})
    status = await update.effective_message.reply_text(
        t("watch_lucky_searching", lang)
    )
    return await _run_watch_lucky(
        context,
        user_id=user_id,
        lang=lang,
        chat_id=update.effective_chat.id,
        status_message=status,
        stay_in_categories_on_empty=False,
    )


async def receive_watch_mode_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data not in ("watch_mode:search", "watch_mode:alert"):
        return _ws()["WATCH_PICK"]
    context.user_data["watch_create_alert"] = data == "watch_mode:alert"
    lang = _user_lang(context, query.from_user.id)
    try:
        await query.edit_message_reply_markup(None)
    except BadRequest:
        pass
    return await _start_watch_wizard(update, context, lang)


async def _run_watch_lucky(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_id: int,
    lang: str,
    chat_id: int,
    status_message=None,
    edit_message=None,
    stay_in_categories_on_empty: bool = False,
) -> int:
    async def _show(text: str, reply_markup=None) -> None:
        target = status_message or edit_message
        edit = getattr(target, "edit_text", None) if target is not None else None
        if callable(edit):
            try:
                await edit(text, reply_markup=reply_markup)
                return
            except BadRequest:
                pass
        await context.bot.send_message(chat_id, text, reply_markup=reply_markup)

    twitch: TwitchClient = context.application.bot_data["twitch"]
    prefer = _bot_lang_to_twitch(lang)
    try:
        cats, streams, vods = await _fetch_lucky_watch_suggestions(
            twitch, prefer_language=prefer
        )
    except Exception:
        logger.exception("watch lucky failed")
        await _show(t("watch_suggest_error", lang))
        return (
            _ws()["WATCH_CATEGORIES"]
            if stay_in_categories_on_empty
            else ConversationHandler.END
        )
    if not streams and not vods:
        markup = (
            _watch_cats_keyboard(context, lang, has_cats=False)
            if stay_in_categories_on_empty
            else None
        )
        await _show(t("watch_lucky_empty", lang), reply_markup=markup)
        if not stay_in_categories_on_empty:
            await context.bot.send_message(
                chat_id,
                t("menu_main", lang),
                reply_markup=_menu(lang, user_id),
            )
        return (
            _ws()["WATCH_CATEGORIES"]
            if stay_in_categories_on_empty
            else ConversationHandler.END
        )
    prefs = WatchPrefs(
        categories=cats,
        min_viewers=0,
        max_viewers=None,
        language=None,
        tags=[],
        exclude_mature=False,
    )
    context.user_data["watch_categories"] = list(cats)
    context.user_data["watch_tags"] = []
    context.user_data["watch_min_viewers"] = 0
    context.user_data["watch_max_viewers"] = None
    context.user_data["watch_language"] = None
    context.user_data["watch_exclude_mature"] = False
    analytics.capture(
        user_id,
        "watch_lucky",
        {
            "categories": len(cats),
            "streams": len(streams),
            "vods": len(vods),
        },
    )
    _set_watch_lucky_mode(context, user_id, enabled=True)
    edit_target = edit_message if hasattr(edit_message, "edit_text") else None
    if edit_target is None and hasattr(status_message, "edit_text"):
        edit_target = status_message
    await _send_watch_suggestions(
        bot=context.bot,
        chat_id=chat_id,
        user_id=user_id,
        context=context,
        prefs=prefs,
        edit_message=edit_target,
        streams=streams or None,
        vods=vods or None,
        allow_vod=False,
    )
    return ConversationHandler.END


async def on_watch_again(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    if _watch_recommended_mode(context, user_id):
        await _send_recommended_promo_suggestions(
            bot=context.bot,
            chat_id=query.message.chat_id,
            user_id=user_id,
            context=context,
            edit_message=query.message,
        )
        return
    if _watch_lucky_mode(context, user_id):
        try:
            await query.edit_message_text(t("watch_lucky_searching", lang))
        except BadRequest:
            pass
        twitch: TwitchClient = context.application.bot_data["twitch"]
        prefer = _bot_lang_to_twitch(lang)
        try:
            cats, streams, vods = await _fetch_lucky_watch_suggestions(
                twitch, prefer_language=prefer
            )
        except Exception:
            logger.exception("watch lucky again failed")
            await query.edit_message_text(t("watch_suggest_error", lang))
            return
        prefs = WatchPrefs(
            categories=cats,
            min_viewers=0,
            max_viewers=None,
            language=None,
            tags=[],
            exclude_mature=False,
        )
        await _send_watch_suggestions(
            bot=context.bot,
            chat_id=query.message.chat_id,
            user_id=user_id,
            context=context,
            prefs=prefs,
            edit_message=query.message,
            streams=streams or None,
            vods=vods or None,
            allow_vod=False,
        )
        return
    prefs = _resolve_watch_prefs(context, user_id)
    if not prefs:
        await query.edit_message_text(t("watch_cats_need_one", lang))
        return
    await _send_watch_suggestions(
        bot=context.bot,
        chat_id=query.message.chat_id,
        user_id=user_id,
        context=context,
        prefs=prefs,
        edit_message=query.message,
    )


async def create_category_watch_subscription(
    bot: Any,
    db: Database,
    user_id: int,
    lang: str,
    prefs: WatchPrefs,
    *,
    allow_duplicate: bool = False,
) -> tuple[str, Subscription | None, str]:
    """Create a category-watch alert. Returns (html_text, sub_or_none, status)."""
    from config import MAX_SUBSCRIPTIONS_PER_OWNER
    from handlers.notifications import CATEGORY_WATCH_COOLDOWN_MINUTES

    prefs_json = dump_category_watch_prefs(prefs)
    want_ids = _category_ids(prefs)
    existing_subs = _subs_for_owner(db, user_id)
    if not allow_duplicate and want_ids:
        existing = _find_category_watch_by_ids(db, user_id, want_ids)
        if existing is not None:
            return t("watch_create_alerts_dup", lang), existing, "watch_create_alerts_dup"
    if len(existing_subs) >= MAX_SUBSCRIPTIONS_PER_OWNER:
        return (
            t("sub_limit", lang, limit=MAX_SUBSCRIPTIONS_PER_OWNER),
            None,
            "sub_limit",
        )

    enabled = await prem.can_enable_more_async(bot, db, user_id)
    label = watch_filter_auto_name(prefs)
    sub_id = db.add_subscription(
        owner_id=user_id,
        twitch_username=label,
        twitch_user_id=f"cw:{user_id}:{secrets.token_hex(4)}",
        message_template=t("import_default_template", lang),
        dest_type="dm",
        chat_id=user_id,
        thread_id=None,
        disable_link_preview=True,
        enabled=enabled,
        notify_on_live=True,
        notify_on_end=False,
        notify_on_category_change=False,
        suppress_repeat_minutes=CATEGORY_WATCH_COOLDOWN_MINUTES,
        from_watch_suggest=True,
        category_watch_prefs=prefs_json,
        is_demo=demo_mode.is_active(user_id),
    )
    db.upsert_user(user_id)
    paused_note = ""
    if not enabled:
        paused_note = "\n\n" + t(
            "created_paused_note",
            lang,
            kind=t("paused_kind_alert", lang),
            limit=prem.free_active_limit(),
        )
    text = t(
        "watch_create_alerts_ok",
        lang,
        name=html.escape(label),
        summary=_watch_prefs_summary(prefs, lang),
        paused_note=paused_note,
    )
    analytics.capture(
        user_id,
        "watch_create_category_alert",
        {"categories": len(prefs.categories), "enabled": enabled},
    )
    created = db.get_subscription(sub_id, user_id)
    return text, created, "watch_create_alerts_ok"


async def on_watch_create_alerts(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    prefs = _resolve_watch_prefs(context, user_id)
    if not prefs or not prefs.categories:
        await query.edit_message_text(t("watch_create_alerts_none", lang))
        return

    db: Database = context.application.bot_data["db"]
    text, sub, status = await create_category_watch_subscription(
        context.bot, db, user_id, lang, prefs
    )
    if status == "watch_create_alerts_dup" and sub is not None:
        from i18n import alert_dup_keyboard

        context.user_data["alert_dup_force"] = {
            "kind": "game",
            "prefs": dump_category_watch_prefs(prefs),
        }
        await query.edit_message_text(
            text, reply_markup=alert_dup_keyboard(lang, sub.id)
        )
        return
    await query.edit_message_text(text, parse_mode=ParseMode.HTML)


async def receive_watch_category_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    cats: list[dict[str, str]] = context.user_data.setdefault("watch_categories", [])
    if len(cats) >= _WATCH_MAX_CATS:
        await update.effective_message.reply_text(
            t("watch_cats_full", lang, max=_WATCH_MAX_CATS),
            reply_markup=_watch_cats_keyboard(context, lang, has_cats=True),
        )
        return _ws()["WATCH_CATEGORIES"]
    query = (update.effective_message.text or "").strip()
    if not query:
        return _ws()["WATCH_CATEGORIES"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    try:
        found = await asyncio.to_thread(twitch.search_categories, query, first=5)
    except Exception:
        logger.exception("watch category search failed")
        await update.effective_message.reply_text(
            t("watch_cats_not_found", lang, query=query),
        )
        return _ws()["WATCH_CATEGORIES"]
    if not found:
        await update.effective_message.reply_text(
            t("watch_cats_not_found", lang, query=query),
        )
        return _ws()["WATCH_CATEGORIES"]
    if len(found) == 1:
        return await _add_watch_category(update, context, lang, found[0])
    context.user_data["watch_cat_candidates"] = [
        {"id": str(c["id"]), "name": str(c.get("name") or "")} for c in found
    ]
    await update.effective_message.reply_text(
        t("watch_cats_pick", lang),
        reply_markup=watch_cats_pick_keyboard(
            lang, context.user_data["watch_cat_candidates"]
        ),
    )
    return _ws()["WATCH_CATEGORIES"]


async def _add_watch_category(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    lang: str,
    cat: dict,
) -> int:
    cats: list[dict[str, str]] = context.user_data.setdefault("watch_categories", [])
    entry = {"id": str(cat["id"]), "name": str(cat.get("name") or "")}
    if _WATCH_MAX_CATS <= 1:
        cats[:] = [entry]
    elif not any(c["id"] == entry["id"] for c in cats):
        cats.append(entry)
    context.user_data.pop("watch_cat_candidates", None)
    if len(cats) < _WATCH_MAX_CATS:
        await update.effective_message.reply_text(
            t(
                "watch_cats_added",
                lang,
                name=entry["name"],
                count=len(cats),
                max=_WATCH_MAX_CATS,
                list=", ".join(c["name"] for c in cats),
            ),
            reply_markup=_watch_cats_keyboard(context, lang, has_cats=True),
        )
        return _ws()["WATCH_CATEGORIES"]
    if (
        context.user_data.get("watch_create_alert")
        and not context.user_data.get("watch_allow_duplicate")
    ):
        db: Database = context.application.bot_data["db"]
        user_id = update.effective_user.id
        existing = _find_category_watch_by_ids(
            db, user_id, _category_ids_from_cats(cats)
        )
        if existing is not None:
            from i18n import alert_dup_keyboard

            context.user_data["alert_dup_force"] = {
                "kind": "game_wizard",
                "sub_id": existing.id,
            }
            text = t("watch_create_alerts_dup", lang)
            markup = alert_dup_keyboard(lang, existing.id)
            msg = update.effective_message
            if update.callback_query:
                try:
                    await update.callback_query.edit_message_text(
                        text, reply_markup=markup
                    )
                except BadRequest:
                    await context.bot.send_message(
                        msg.chat_id, text, reply_markup=markup
                    )
            else:
                await msg.reply_text(text, reply_markup=markup)
            return _ws()["WATCH_DUP"]
    return await _go_watch_filters_prompt(update, context, lang)


async def receive_watch_dup_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Edit existing game alert or continue wizard to create another."""
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    data = query.data or ""
    if data.startswith("alert_dup:edit:"):
        context.user_data.pop("alert_dup_force", None)
        from handlers.subscriptions import on_share_dup_edit

        await on_share_dup_edit(update, context)
        context.user_data.clear()
        return ConversationHandler.END
    if data == "alert_dup:continue":
        context.user_data.pop("alert_dup_force", None)
        context.user_data["watch_allow_duplicate"] = True
        try:
            await query.edit_message_reply_markup(None)
        except BadRequest:
            pass
        return await _go_watch_filters_prompt(update, context, lang)
    return _ws()["WATCH_DUP"]


async def receive_watch_viewers_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    lang = _user_lang(context, update.effective_user.id)
    parsed = _parse_watch_viewers(update.effective_message.text or "")
    if parsed is None:
        await update.effective_message.reply_text(
            t("watch_viewers_bad", lang),
            reply_markup=watch_viewers_keyboard(lang),
            parse_mode=ParseMode.HTML,
        )
        return _ws()["WATCH_VIEWERS"]
    lo, hi = parsed
    context.user_data["watch_min_viewers"] = lo
    context.user_data["watch_max_viewers"] = hi
    return await _go_watch_next_detail(update, context, lang)


async def receive_watch_viewers_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    if query.data == "watch_viewers:any":
        context.user_data["watch_min_viewers"] = 0
        context.user_data["watch_max_viewers"] = None
        try:
            await query.edit_message_reply_markup(None)
        except BadRequest:
            pass
        return await _go_watch_next_detail(update, context, lang)
    return _ws()["WATCH_VIEWERS"]


async def receive_watch_language_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    data = query.data or ""
    if data == "watch_lang:any":
        context.user_data["watch_language"] = None
        context.user_data.pop("watch_lang_await_other", None)
        try:
            await query.edit_message_reply_markup(None)
        except BadRequest:
            pass
        return await _go_watch_next_detail(update, context, lang)
    if data in ("watch_lang:ru", "watch_lang:en"):
        context.user_data["watch_language"] = data.rsplit(":", 1)[1]
        context.user_data.pop("watch_lang_await_other", None)
        try:
            await query.edit_message_reply_markup(None)
        except BadRequest:
            pass
        return await _go_watch_next_detail(update, context, lang)
    if data == "watch_lang:other":
        context.user_data["watch_lang_await_other"] = True
        await query.edit_message_text(t("watch_lang_other_prompt", lang))
        return _ws()["WATCH_LANGUAGE"]
    return _ws()["WATCH_LANGUAGE"]


async def receive_watch_language_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    lang = _user_lang(context, update.effective_user.id)
    if not context.user_data.get("watch_lang_await_other"):
        await update.effective_message.reply_text(
            t("watch_lang_prompt", lang),
            reply_markup=watch_lang_keyboard(lang),
        )
        return _ws()["WATCH_LANGUAGE"]
    code = (update.effective_message.text or "").strip().lower()
    if not _WATCH_LANG_RE.match(code):
        await update.effective_message.reply_text(t("watch_lang_bad", lang))
        return _ws()["WATCH_LANGUAGE"]
    context.user_data["watch_language"] = code
    context.user_data.pop("watch_lang_await_other", None)
    return await _go_watch_next_detail(update, context, lang)


async def receive_watch_nav_back(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    from handlers.wizard import wizard_back

    query = update.callback_query
    await query.answer()
    return await wizard_back(update, context)


async def receive_watch_filters_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    data = query.data or ""
    if data.startswith("watch_filt:toggle:"):
        key = data.rsplit(":", 1)[-1]
        flag_key = {
            "tags": "watch_want_tags",
            "viewers": "watch_want_viewers",
            "language": "watch_want_language",
            "mature": "watch_want_mature",
        }.get(key)
        if not flag_key:
            return _ws()["WATCH_FILTERS"]
        context.user_data[flag_key] = not bool(context.user_data.get(flag_key))
        await query.edit_message_reply_markup(
            watch_filters_keyboard(
                lang,
                want_tags=bool(context.user_data.get("watch_want_tags")),
                want_viewers=bool(context.user_data.get("watch_want_viewers")),
                want_language=bool(context.user_data.get("watch_want_language")),
                want_mature=bool(context.user_data.get("watch_want_mature")),
            )
        )
        return _ws()["WATCH_FILTERS"]
    if data == "watch_filt:next":
        if not context.user_data.get("watch_want_tags"):
            context.user_data["watch_tags"] = []
        if not context.user_data.get("watch_want_viewers"):
            context.user_data["watch_min_viewers"] = 0
            context.user_data["watch_max_viewers"] = None
        if not context.user_data.get("watch_want_language"):
            context.user_data["watch_language"] = None
        context.user_data["watch_exclude_mature"] = bool(
            context.user_data.get("watch_want_mature")
        )
        context.user_data["watch_detail_queue"] = _watch_detail_queue(context)
        try:
            await query.edit_message_reply_markup(None)
        except BadRequest:
            pass
        return await _go_watch_next_detail(update, context, lang)
    return _ws()["WATCH_FILTERS"]


async def receive_watch_tags_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    lang = _user_lang(context, update.effective_user.id)
    tags = normalize_watch_tags(
        update.effective_message.text or "", limit=_WATCH_MAX_TAGS
    )
    if not tags:
        await update.effective_message.reply_text(
            t("watch_tags_bad", lang),
            reply_markup=watch_tags_keyboard(lang),
            parse_mode=ParseMode.HTML,
        )
        return _ws()["WATCH_TAGS"]
    context.user_data["watch_tags"] = tags
    return await _go_watch_next_detail(update, context, lang)


async def receive_watch_tags_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "watch_tags:skip":
        context.user_data["watch_tags"] = []
        try:
            await query.edit_message_reply_markup(None)
        except BadRequest:
            pass
        return await _go_watch_next_detail(
            update, context, _user_lang(context, query.from_user.id)
        )
    return _ws()["WATCH_TAGS"]


async def receive_watch_category_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    data = query.data or ""
    if data == "watch_cat:lucky":
        try:
            await query.edit_message_text(t("watch_lucky_searching", lang))
        except BadRequest:
            try:
                await query.edit_message_reply_markup(None)
            except BadRequest:
                pass
            await context.bot.send_message(
                query.message.chat_id, t("watch_lucky_searching", lang)
            )
        return await _run_watch_lucky(
            context,
            user_id=user_id,
            lang=lang,
            chat_id=query.message.chat_id,
            status_message=query.message,
            edit_message=query.message,
            stay_in_categories_on_empty=True,
        )
    if data == "watch_cat:recommended":
        db: Database = context.application.bot_data["db"]
        twitch: TwitchClient = context.application.bot_data["twitch"]
        streams = await _fetch_recommended_promo_streams(db, twitch)
        context.user_data["watch_has_recommended"] = bool(streams)
        if not streams:
            try:
                await query.edit_message_text(
                    t("watch_recommended_empty", lang),
                    reply_markup=_watch_cats_keyboard(context, lang, has_cats=False),
                )
            except BadRequest:
                await context.bot.send_message(
                    query.message.chat_id,
                    t("watch_recommended_empty", lang),
                    reply_markup=_watch_cats_keyboard(context, lang, has_cats=False),
                )
            return _ws()["WATCH_CATEGORIES"]
        analytics.capture(
            user_id,
            "watch_recommended",
            {"streams": len(streams)},
        )
        await _send_recommended_promo_suggestions(
            bot=context.bot,
            chat_id=query.message.chat_id,
            user_id=user_id,
            context=context,
            edit_message=query.message,
        )
        return ConversationHandler.END
    if data == "watch_cat:clear":
        context.user_data["watch_categories"] = []
        await query.edit_message_text(
            t("watch_cats_prompt", lang, max=_WATCH_MAX_CATS),
            reply_markup=_watch_cats_keyboard(context, lang, has_cats=False),
            parse_mode=ParseMode.HTML,
        )
        return _ws()["WATCH_CATEGORIES"]
    if data.startswith("watch_cat:pick:"):
        try:
            idx = int(data.rsplit(":", 1)[1])
        except ValueError:
            return _ws()["WATCH_CATEGORIES"]
        candidates = context.user_data.get("watch_cat_candidates") or []
        if idx < 0 or idx >= len(candidates):
            return _ws()["WATCH_CATEGORIES"]
        try:
            await query.edit_message_reply_markup(None)
        except BadRequest:
            pass
        return await _add_watch_category(update, context, lang, candidates[idx])
    return _ws()["WATCH_CATEGORIES"]




