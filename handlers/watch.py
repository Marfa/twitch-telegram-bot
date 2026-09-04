from __future__ import annotations

import asyncio
import html
import logging
import re
import secrets

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
    WATCH_MAX_FILTERS,
    Database,
    WatchPrefs,
    dump_category_watch_prefs,
    watch_filter_auto_name,
)
from i18n import (
    DEFAULT_LOCALE,
    t,
    watch_cats_nav_keyboard,
    watch_cats_pick_keyboard,
    watch_delete_pick_keyboard,
    watch_lang_keyboard,
    watch_mature_keyboard,
    watch_pick_keyboard,
    watch_save_keyboard,
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
        WATCH_DELETE,
        WATCH_LANGUAGE,
        WATCH_MATURE,
        WATCH_PICK,
        WATCH_SAVE,
        WATCH_TAGS,
        WATCH_VIEWERS,
    )

    return {
        "WATCH_CATEGORIES": WATCH_CATEGORIES,
        "WATCH_DELETE": WATCH_DELETE,
        "WATCH_LANGUAGE": WATCH_LANGUAGE,
        "WATCH_MATURE": WATCH_MATURE,
        "WATCH_PICK": WATCH_PICK,
        "WATCH_SAVE": WATCH_SAVE,
        "WATCH_TAGS": WATCH_TAGS,
        "WATCH_VIEWERS": WATCH_VIEWERS,
    }


def _subs_for_owner(db: Database, owner_id: int):
    from bot import _subs_for_owner as _impl

    return _impl(db, owner_id)


def _set_wizard_back(context: ContextTypes.DEFAULT_TYPE, state: int) -> None:
    from bot import _set_wizard_back as _impl

    _impl(context, state)


_WATCH_MAX_CATS = 5
_WATCH_SUGGEST_N = 5
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
        markup = watch_suggest_keyboard(lang, offer_create_alerts=True)
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
    markup = watch_suggest_keyboard(lang, offer_create_alerts=True)
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
    context.user_data.clear()
    context.user_data["watch_categories"] = []
    context.user_data["watch_tags"] = []
    return await _go_watch_categories_prompt(update, context, lang)


async def _go_watch_pick_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    db: Database = context.application.bot_data["db"]
    user_id = update.effective_user.id
    filters = db.get_watch_filters(user_id)
    if not filters:
        await update.effective_message.reply_text(t("watch_pick_empty", lang))
        return await _start_watch_wizard(update, context, lang)
    msg = update.effective_message
    if update.callback_query and update.callback_query.message:
        try:
            await update.callback_query.edit_message_text(
                t("watch_pick_prompt", lang),
                reply_markup=watch_pick_keyboard(lang, filters),
            )
            _set_wizard_back(context, _ws()["WATCH_PICK"])
            return _ws()["WATCH_PICK"]
        except BadRequest:
            pass
    await msg.reply_text(
        t("watch_pick_prompt", lang),
        reply_markup=watch_pick_keyboard(lang, filters),
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


async def _go_watch_mature_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    await update.effective_message.reply_text(
        t("watch_mature_prompt", lang),
        reply_markup=watch_mature_keyboard(lang),
    )
    _set_wizard_back(context, _ws()["WATCH_MATURE"])
    return _ws()["WATCH_MATURE"]


async def _go_watch_save_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str
) -> int:
    prefs = _watch_prefs_from_user_data(context)
    await update.effective_message.reply_text(
        t(
            "watch_save_prompt",
            lang,
            max=WATCH_MAX_FILTERS,
            summary=_watch_prefs_summary(prefs, lang),
        ),
        reply_markup=watch_save_keyboard(lang),
        parse_mode=ParseMode.HTML,
    )
    _set_wizard_back(context, _ws()["WATCH_SAVE"])
    return _ws()["WATCH_SAVE"]


async def _complete_watch_wizard(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, save: bool
) -> int:
    user_id = update.effective_user.id
    db: Database = context.application.bot_data["db"]
    prefs = _watch_prefs_from_user_data(context)
    if save:
        db.add_watch_filter(user_id, prefs)
    context.user_data.clear()
    _set_watch_lucky_mode(context, user_id, enabled=False)
    chat_id = update.effective_chat.id
    if update.callback_query:
        try:
            await update.callback_query.edit_message_reply_markup(None)
        except BadRequest:
            pass
    await _send_watch_suggestions(
        bot=context.bot,
        chat_id=chat_id,
        user_id=user_id,
        context=context,
        prefs=prefs,
    )
    return ConversationHandler.END


async def start_what_to_watch(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    user_id = update.effective_user.id
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    lang = _user_lang(context, user_id)
    filters = db.get_watch_filters(user_id)
    context.user_data.clear()
    _set_watch_lucky_mode(context, user_id, enabled=False)
    _set_watch_recommended_mode(context, user_id, enabled=False)
    analytics.capture(
        user_id,
        "watch_opened",
        {"has_saved_filters": bool(filters)},
    )
    if filters:
        return await _go_watch_pick_prompt(update, context, lang)
    return await _start_watch_wizard(update, context, lang)


async def start_watch_change(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    return await start_what_to_watch(update, context)


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

    from config import MAX_SUBSCRIPTIONS_PER_OWNER

    db: Database = context.application.bot_data["db"]
    prefs_json = dump_category_watch_prefs(prefs)
    existing_subs = _subs_for_owner(db, user_id)
    for sub in existing_subs:
        if (sub.category_watch_prefs or "").strip() == prefs_json:
            await query.edit_message_text(t("watch_create_alerts_dup", lang))
            return
    if len(existing_subs) >= MAX_SUBSCRIPTIONS_PER_OWNER:
        await query.edit_message_text(
            t("sub_limit", lang, limit=MAX_SUBSCRIPTIONS_PER_OWNER)
        )
        return

    enabled = await prem.can_enable_more_async(context.bot, db, user_id)
    label = watch_filter_auto_name(prefs)
    db.add_subscription(
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
    await query.edit_message_text(text, parse_mode=ParseMode.HTML)


async def receive_watch_pick_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    data = query.data or ""
    if data == "watch_pick:new":
        try:
            await query.edit_message_reply_markup(None)
        except BadRequest:
            pass
        return await _start_watch_wizard(update, context, lang)
    if data == "watch_pick:delete":
        filters = db.get_watch_filters(user_id)
        if not filters:
            return await _go_watch_pick_prompt(update, context, lang)
        context.user_data["watch_delete_selected"] = set()
        await query.edit_message_text(
            t("watch_delete_pick", lang),
            reply_markup=watch_delete_pick_keyboard(lang, filters, set()),
        )
        _set_wizard_back(context, _ws()["WATCH_DELETE"])
        return _ws()["WATCH_DELETE"]
    if data.startswith("watch_pick:"):
        fid = data.split(":", 1)[1]
        filters = db.get_watch_filters(user_id)
        match = next((f for f in filters if f.id == fid), None)
        if not match:
            return await _go_watch_pick_prompt(update, context, lang)
        context.user_data.clear()
        _set_watch_lucky_mode(context, user_id, enabled=False)
        try:
            await query.edit_message_reply_markup(None)
        except BadRequest:
            pass
        await _send_watch_suggestions(
            bot=context.bot,
            chat_id=query.message.chat_id,
            user_id=user_id,
            context=context,
            prefs=match.prefs,
        )
        return ConversationHandler.END
    return _ws()["WATCH_PICK"]


async def receive_watch_del_sel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    fid = (query.data or "").split(":", 1)[-1]
    selected: set[str] = context.user_data.setdefault("watch_delete_selected", set())
    if fid in selected:
        selected.discard(fid)
    else:
        selected.add(fid)
    db: Database = context.application.bot_data["db"]
    filters = db.get_watch_filters(user_id)
    await query.edit_message_reply_markup(
        reply_markup=watch_delete_pick_keyboard(lang, filters, selected)
    )
    return _ws()["WATCH_DELETE"]


async def receive_watch_del_clear(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    context.user_data["watch_delete_selected"] = set()
    db: Database = context.application.bot_data["db"]
    filters = db.get_watch_filters(user_id)
    await query.edit_message_reply_markup(
        reply_markup=watch_delete_pick_keyboard(lang, filters, set())
    )
    return _ws()["WATCH_DELETE"]


async def receive_watch_del_go(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    selected: set[str] = set(context.user_data.get("watch_delete_selected") or ())
    if not selected:
        await query.answer(t("watch_delete_none", lang), show_alert=True)
        return _ws()["WATCH_DELETE"]
    await query.answer()
    db: Database = context.application.bot_data["db"]
    deleted = 0
    for fid in list(selected):
        if db.delete_watch_filter(user_id, fid):
            deleted += 1
    context.user_data["watch_delete_selected"] = set()
    filters = db.get_watch_filters(user_id)
    try:
        await query.edit_message_text(t("watch_deleted", lang, count=deleted))
    except BadRequest:
        pass
    if not filters:
        await context.bot.send_message(
            query.message.chat_id,
            t("watch_pick_empty", lang),
            reply_markup=_menu(lang, user_id),
        )
        return await _start_watch_wizard(update, context, lang)
    await context.bot.send_message(
        query.message.chat_id,
        t("watch_pick_prompt", lang),
        reply_markup=watch_pick_keyboard(lang, filters),
    )
    _set_wizard_back(context, _ws()["WATCH_PICK"])
    return _ws()["WATCH_PICK"]


async def receive_watch_del_back(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    context.user_data.pop("watch_delete_selected", None)
    return await _go_watch_pick_prompt(update, context, lang)


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
    if not any(c["id"] == entry["id"] for c in cats):
        cats.append(entry)
    context.user_data.pop("watch_cat_candidates", None)
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
        twitch: TwitchClient = context.application.bot_data["twitch"]
        prefer = _bot_lang_to_twitch(lang)
        try:
            cats, streams, vods = await _fetch_lucky_watch_suggestions(
                twitch, prefer_language=prefer
            )
        except Exception:
            logger.exception("watch lucky failed")
            await context.bot.send_message(
                query.message.chat_id, t("watch_suggest_error", lang)
            )
            return _ws()["WATCH_CATEGORIES"]
        if not streams and not vods:
            try:
                await query.edit_message_text(
                    t("watch_lucky_empty", lang),
                    reply_markup=_watch_cats_keyboard(context, lang, has_cats=False),
                )
            except BadRequest:
                await context.bot.send_message(
                    query.message.chat_id,
                    t("watch_lucky_empty", lang),
                    reply_markup=_watch_cats_keyboard(context, lang, has_cats=False),
                )
            return _ws()["WATCH_CATEGORIES"]
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
        return ConversationHandler.END
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
    if data == "watch_cat:done":
        cats = context.user_data.get("watch_categories") or []
        if not cats:
            await query.edit_message_text(t("watch_cats_need_one", lang))
            return _ws()["WATCH_CATEGORIES"]
        try:
            await query.edit_message_reply_markup(None)
        except BadRequest:
            pass
        return await _go_watch_tags_prompt(update, context, lang)
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
    return await _go_watch_language_prompt(update, context, lang)


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
        return await _go_watch_language_prompt(update, context, lang)
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
        return await _go_watch_mature_prompt(update, context, lang)
    if data in ("watch_lang:ru", "watch_lang:en"):
        context.user_data["watch_language"] = data.rsplit(":", 1)[1]
        context.user_data.pop("watch_lang_await_other", None)
        try:
            await query.edit_message_reply_markup(None)
        except BadRequest:
            pass
        return await _go_watch_mature_prompt(update, context, lang)
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
    return await _go_watch_mature_prompt(update, context, lang)


async def receive_watch_nav_back(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    # Lazy: handlers.wizard imports this module at top level.
    from handlers.wizard import wizard_back

    query = update.callback_query
    await query.answer()
    return await wizard_back(update, context)


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
    return await _go_watch_viewers_prompt(update, context, lang)


async def receive_watch_tags_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    if query.data == "watch_tags:skip":
        context.user_data["watch_tags"] = []
        try:
            await query.edit_message_reply_markup(None)
        except BadRequest:
            pass
        return await _go_watch_viewers_prompt(update, context, lang)
    return _ws()["WATCH_TAGS"]


async def receive_watch_mature_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    lang = _user_lang(context, query.from_user.id)
    data = query.data or ""
    context.user_data["watch_exclude_mature"] = data == "watch_mature:1"
    try:
        await query.edit_message_reply_markup(None)
    except BadRequest:
        pass
    return await _go_watch_save_prompt(update, context, lang)


async def receive_watch_save_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    save = (query.data or "") == "watch_save:1"
    return await _complete_watch_wizard(update, context, save=save)


