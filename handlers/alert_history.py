from __future__ import annotations

import asyncio
import html
import logging
from datetime import date, datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

import premium as prem
from bot_helpers import _menu, _user_lang, reply_chat_id
from db import AlertHistoryEntry, Database
from i18n import SCHEDULE_TZ, btn, format_stream_schedule_prompt_date, t
from rich_message import edit_rich_message, send_rich_message
from twitch import TwitchClient

logger = logging.getLogger(__name__)

ALERT_HISTORY_VIEWED_EMOJI = "🫣"
ALERT_HISTORY_UNVIEWED_EMOJI = "🙄"

# /start payloads for HTML-fallback action links (classic sendMessage).
_START_VIEWED = "ah_v_"
_START_UNVIEWED = "ah_u_"
_START_VIEWED_BELOW = "ah_vb_"

# Rich-message / inline callback actions (Button Revolution).
_CB_VIEWED = "ah:v:"
_CB_UNVIEWED = "ah:u:"
_CB_VIEWED_BELOW = "ah:vb:"


def _alert_history_type_label(alert_type: str, lang: str) -> str:
    key = {
        "live": "alert_history_type_live",
        "end": "alert_history_type_end",
        "category": "alert_history_type_category",
        "schedule": "alert_history_type_schedule",
    }.get(alert_type)
    return t(key, lang) if key else alert_type


def _alert_history_stream_url(username: str) -> str:
    login = (username or "").strip().lstrip("@")
    return f"https://twitch.tv/{login}"


def _format_vod_timestamp(seconds: int) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes or hours:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return "".join(parts)


def _twitch_vod_url(vod_id: str, offset_seconds: int | None = None) -> str:
    url = f"https://www.twitch.tv/videos/{vod_id}"
    if offset_seconds and int(offset_seconds) > 0:
        url += f"?t={_format_vod_timestamp(int(offset_seconds))}"
    return url


def _vod_offset_seconds(stream: dict | None) -> int:
    if not stream:
        return 0
    raw = str(stream.get("started_at") or "").strip()
    if not raw:
        return 0
    if "T" not in raw and " " in raw:
        raw = raw.replace(" ", "T", 1)
    try:
        started = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - started.astimezone(timezone.utc)).total_seconds()))


def _vod_id_from_videos(videos: list[dict], stream_id: str) -> str:
    sid = str(stream_id or "").strip()
    if not sid:
        return ""
    for video in videos:
        if str(video.get("stream_id") or "") == sid:
            return str(video.get("id") or "")
    return ""


def _alert_history_item_url(item: AlertHistoryEntry) -> str:
    vod_id = (item.vod_id or "").strip()
    if vod_id:
        offset = item.vod_offset_seconds if item.alert_type == "category" else None
        return _twitch_vod_url(vod_id, offset)
    login = (item.twitch_username or "").strip().lstrip("@")
    if login and login != "—":
        return _alert_history_stream_url(login)
    return ""


def _alert_history_start_url(bot_username: str, prefix: str, history_id: int) -> str:
    return f"https://t.me/{bot_username}?start={prefix}{int(history_id)}"


def _html_action_link(url: str, label: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'


def _resolve_stream_url(username: str, stream_url: str | None) -> str:
    if stream_url is not None:
        return stream_url
    login = (username or "").strip().lstrip("@")
    return _alert_history_stream_url(login) if login and login != "—" else ""


def _alert_history_rich_buttons(
    *,
    lang: str,
    stream_url: str,
    viewed: bool,
    history_id: int,
) -> list[dict]:
    """In-message Button Revolution row for one history entry."""
    buttons: list[dict] = []
    if stream_url:
        buttons.append(
            {
                "text": t("alert_history_go_stream", lang),
                "url": stream_url,
                "style": "link",
            }
        )
    if viewed:
        buttons.append(
            {
                "text": t("alert_history_already_viewed", lang),
                "style": "success",
                "disabled": {},
            }
        )
        buttons.append(
            {
                "text": t("alert_history_mark_unviewed", lang),
                "callback_data": f"{_CB_UNVIEWED}{int(history_id)}",
                "style": "primary",
            }
        )
    else:
        buttons.append(
            {
                "text": t("alert_history_mark_viewed", lang),
                "callback_data": f"{_CB_VIEWED}{int(history_id)}",
                "style": "success",
            }
        )
    buttons.append(
        {
            "text": t("alert_history_mark_viewed_below", lang),
            "callback_data": f"{_CB_VIEWED_BELOW}{int(history_id)}",
            "style": "primary",
        }
    )
    return buttons


def _format_alert_history_block(
    *,
    time_str: str,
    username: str,
    body: str,
    lang: str,
    stream_url: str | None = None,
    viewed: bool = False,
    history_id: int | None = None,
    bot_username: str = "",
) -> str:
    """HTML fallback block (deep links) when sendRichMessage is unavailable."""
    status = ALERT_HISTORY_VIEWED_EMOJI if viewed else ALERT_HISTORY_UNVIEWED_EMOJI
    parts = [
        t(
            "alert_history_line",
            lang,
            time=time_str,
            username=html.escape(username),
            status=status,
        )
    ]
    parts.append(t("alert_history_body", lang, text=html.escape(body)))
    url = _resolve_stream_url(username, stream_url)
    action_bits: list[str] = []
    if url:
        action_bits.append(
            _html_action_link(url, t("alert_history_go_stream", lang))
        )
    bot = (bot_username or "").strip().lstrip("@")
    if bot and history_id is not None:
        if viewed:
            action_bits.append(
                _html_action_link(
                    _alert_history_start_url(bot, _START_UNVIEWED, history_id),
                    t("alert_history_mark_unviewed", lang),
                )
            )
        else:
            action_bits.append(
                _html_action_link(
                    _alert_history_start_url(bot, _START_VIEWED, history_id),
                    t("alert_history_mark_viewed", lang),
                )
            )
        action_bits.append(
            _html_action_link(
                _alert_history_start_url(bot, _START_VIEWED_BELOW, history_id),
                t("alert_history_mark_viewed_below", lang),
            )
        )
    elif history_id is not None:
        # No bot username (tests / offline) — still show labels for layout checks.
        if viewed:
            action_bits.append(html.escape(t("alert_history_mark_unviewed", lang)))
        else:
            action_bits.append(html.escape(t("alert_history_mark_viewed", lang)))
        action_bits.append(html.escape(t("alert_history_mark_viewed_below", lang)))
    if action_bits:
        parts.append(" | ".join(action_bits))
    return "\n".join(parts)


def _alert_history_item_rich_blocks(
    *,
    time_str: str,
    username: str,
    body: str,
    lang: str,
    stream_url: str | None = None,
    viewed: bool = False,
    history_id: int | None = None,
    day_heading: str | None = None,
) -> list[dict]:
    status = ALERT_HISTORY_VIEWED_EMOJI if viewed else ALERT_HISTORY_UNVIEWED_EMOJI
    blocks: list[dict] = []
    if day_heading:
        blocks.append({"type": "heading", "size": 4, "text": day_heading})
    line = t(
        "alert_history_line",
        lang,
        time=time_str,
        username=username,
        status=status,
    )
    # Strip HTML tags from locale templates for rich plain text.
    line = (
        line.replace("<b>", "")
        .replace("</b>", "")
        .replace("<i>", "")
        .replace("</i>", "")
    )
    text = f"{line}\n{(body or '').strip()}"
    blocks.append({"type": "paragraph", "text": text})
    if history_id is not None:
        url = _resolve_stream_url(username, stream_url)
        blocks.append(
            {
                "type": "buttons",
                "align": "left",
                "buttons": _alert_history_rich_buttons(
                    lang=lang,
                    stream_url=url,
                    viewed=viewed,
                    history_id=int(history_id),
                ),
            }
        )
    return blocks


async def _fill_alert_history_vods(
    twitch: TwitchClient, db: Database, items: list[AlertHistoryEntry]
) -> None:
    missing = [
        item
        for item in items
        if not (item.vod_id or "").strip()
        and (item.stream_id or "").strip()
        and (item.twitch_user_id or "").strip()
    ]
    if not missing:
        return
    user_ids = list(dict.fromkeys(item.twitch_user_id for item in missing))

    async def _fetch(uid: str) -> tuple[str, list[dict]]:
        try:
            videos = await asyncio.to_thread(twitch.get_videos_by_user, uid)
        except Exception:
            logger.exception("VOD lookup failed for %s", uid)
            return uid, []
        return uid, videos

    fetched = await asyncio.gather(*[_fetch(uid) for uid in user_ids])
    videos_by_user = dict(fetched)
    for item in missing:
        vid = _vod_id_from_videos(
            videos_by_user.get(item.twitch_user_id) or [], item.stream_id
        )
        if not vid:
            continue
        item.vod_id = vid
        try:
            db.set_alert_history_vod_id(item.id, vid)
        except Exception:
            logger.exception("Failed to cache VOD id for history %s", item.id)


def _parse_alert_sent_at(sent_at: str) -> datetime:
    raw = (sent_at or "").strip()
    if not raw:
        return datetime.now(timezone.utc)
    # SQLite datetime('now') is UTC without timezone marker.
    if "T" not in raw and " " in raw:
        raw = raw.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _build_alert_history_chunks(
    items: list,
    lang: str,
    days: int,
    *,
    bot_username: str = "",
) -> list[str]:
    title = t("alert_history_title", lang, days=days, n=len(items))
    blocks: list[str] = []
    last_day: date | None = None
    for item in items:
        local = _parse_alert_sent_at(item.sent_at).astimezone(SCHEDULE_TZ)
        parts: list[str] = []
        if last_day != local.date():
            parts.append(
                t(
                    "alert_history_day",
                    lang,
                    date=format_stream_schedule_prompt_date(local.date(), lang),
                )
            )
            last_day = local.date()
        body = (item.message_text or "").strip()
        if not body:
            body = _alert_history_type_label(item.alert_type, lang)
        parts.append(
            _format_alert_history_block(
                time_str=local.strftime("%H:%M"),
                username=item.twitch_username,
                body=body,
                lang=lang,
                stream_url=_alert_history_item_url(item),
                viewed=bool(getattr(item, "viewed", False)),
                history_id=int(item.id),
                bot_username=bot_username,
            )
        )
        blocks.append("\n".join(parts))

    chunks: list[str] = []
    buf = title
    for block in blocks:
        candidate = f"{buf}\n\n{block}" if buf else block
        if buf and len(candidate) > 4000:
            chunks.append(buf if len(buf) <= 4000 else buf[:3990].rstrip() + "\n…")
            buf = block if len(block) <= 4000 else block[:3990].rstrip() + "\n…"
        else:
            buf = candidate if len(candidate) <= 4000 else candidate[:3990].rstrip() + "\n…"
    if buf:
        chunks.append(buf)
    return chunks


def _build_alert_history_rich_pages(
    items: list,
    lang: str,
    days: int,
) -> list[dict]:
    """InputRichMessage pages with in-body action buttons."""
    title = t("alert_history_title", lang, days=days, n=len(items))
    title_plain = (
        title.replace("<b>", "")
        .replace("</b>", "")
        .replace("<i>", "")
        .replace("</i>", "")
    )

    def _day_label(d: date) -> str:
        raw = t(
            "alert_history_day",
            lang,
            date=format_stream_schedule_prompt_date(d, lang),
        )
        return (
            raw.replace("<b>", "")
            .replace("</b>", "")
            .replace("<i>", "")
            .replace("</i>", "")
        )

    pages: list[dict] = []
    blocks: list[dict] = [{"type": "heading", "size": 3, "text": title_plain}]
    approx = len(title_plain)
    last_day: date | None = None

    for item in items:
        local = _parse_alert_sent_at(item.sent_at).astimezone(SCHEDULE_TZ)
        day = local.date()
        day_heading = _day_label(day) if last_day != day else None
        body = (item.message_text or "").strip()
        if not body:
            body = _alert_history_type_label(item.alert_type, lang)
        item_blocks = _alert_history_item_rich_blocks(
            time_str=local.strftime("%H:%M"),
            username=item.twitch_username,
            body=body,
            lang=lang,
            stream_url=_alert_history_item_url(item),
            viewed=bool(getattr(item, "viewed", False)),
            history_id=int(item.id),
            day_heading=day_heading,
        )
        item_len = sum(len(str(b.get("text") or "")) for b in item_blocks) + 80
        if approx + item_len > 12000 and len(blocks) > 1:
            pages.append({"blocks": blocks})
            blocks = [{"type": "heading", "size": 3, "text": title_plain}]
            approx = len(title_plain)
            # New page: always repeat the day heading for context.
            item_blocks = _alert_history_item_rich_blocks(
                time_str=local.strftime("%H:%M"),
                username=item.twitch_username,
                body=body,
                lang=lang,
                stream_url=_alert_history_item_url(item),
                viewed=bool(getattr(item, "viewed", False)),
                history_id=int(item.id),
                day_heading=_day_label(day),
            )
            item_len = sum(len(str(b.get("text") or "")) for b in item_blocks) + 80
        blocks.extend(item_blocks)
        approx += item_len
        last_day = day
    if blocks:
        pages.append({"blocks": blocks})
    return pages


def _alert_history_menu_row(lang: str) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(btn("back", lang), callback_data="alert_history:menu")
    ]


def _alert_history_nav_keyboard(
    lang: str, page: int, total: int, *, show_more: bool
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if total > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton(
                    "‹",
                    callback_data=f"alert_history:page:{page - 1}",
                )
            )
        nav.append(
            InlineKeyboardButton(
                f"{page + 1}/{total}",
                callback_data="alert_history:noop",
            )
        )
        if page < total - 1:
            nav.append(
                InlineKeyboardButton(
                    "›",
                    callback_data=f"alert_history:page:{page + 1}",
                )
            )
        rows.append(nav)
    if show_more and page >= total - 1:
        rows.append(
            [
                InlineKeyboardButton(
                    btn("alert_history_more", lang),
                    callback_data="alert_history:more",
                )
            ]
        )
    rows.append(_alert_history_menu_row(lang))
    return InlineKeyboardMarkup(rows)


def _remember_alert_history_ui(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    *,
    chat_id: int,
    message_id: int,
    page: int,
    is_rich: bool = False,
) -> None:
    store = context.application.bot_data.setdefault("alert_history_ui", {})
    store[int(user_id)] = {
        "chat_id": int(chat_id),
        "message_id": int(message_id),
        "page": int(page),
        "is_rich": bool(is_rich),
    }


def _resolve_message_chat_id(message: object, fallback: int) -> int:
    cid = getattr(message, "chat_id", None)
    if cid is not None:
        return int(cid)
    chat = getattr(message, "chat", None)
    if chat is not None and getattr(chat, "id", None) is not None:
        return int(chat.id)
    return int(fallback)


def _alert_history_bot_username(context: ContextTypes.DEFAULT_TYPE) -> str:
    bot = context.bot
    return str(getattr(bot, "username", None) or "").strip()


def _message_id_from_result(result: object) -> int | None:
    if result is None:
        return None
    mid = getattr(result, "message_id", None)
    if mid is not None:
        return int(mid)
    if isinstance(result, dict) and result.get("message_id") is not None:
        return int(result["message_id"])
    return None


async def _send_alert_history_page(
    bot,
    chat_id: int,
    *,
    rich_page: dict | None,
    html_page: str,
    reply_markup: InlineKeyboardMarkup,
) -> tuple[object | None, bool]:
    """Prefer rich message; fall back to classic HTML. Returns (msg, is_rich)."""
    if rich_page is not None:
        msg = await send_rich_message(
            bot, chat_id, rich_page, reply_markup=reply_markup
        )
        if msg is not None:
            return msg, True
    msg = await bot.send_message(
        chat_id=chat_id,
        text=html_page,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    return msg, False


async def _edit_alert_history_page(
    bot,
    *,
    chat_id: int,
    message_id: int,
    rich_page: dict | None,
    html_page: str,
    reply_markup: InlineKeyboardMarkup,
    prefer_rich: bool,
) -> bool:
    """Edit history page. Returns True when the resulting message is rich."""
    if prefer_rich and rich_page is not None:
        ok = await edit_rich_message(
            bot,
            chat_id=chat_id,
            message_id=message_id,
            rich_message=rich_page,
            reply_markup=reply_markup,
        )
        if ok:
            return True
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=html_page,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return False
    except BadRequest as exc:
        if "not modified" in str(exc).lower():
            return prefer_rich and rich_page is not None
        raise


async def _load_alert_history_pages(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, lang: str
) -> tuple[list[str], list[dict], bool]:
    db: Database = context.application.bot_data["db"]
    deep = await prem.has_feature(context.bot, db, user_id, "alert_history")
    days = (
        prem.ALERT_HISTORY_PREMIUM_DAYS if deep else prem.ALERT_HISTORY_FREE_DAYS
    )
    since = datetime.now(timezone.utc) - timedelta(days=days)
    items = db.list_alert_history(user_id, since=since)
    twitch = context.application.bot_data.get("twitch")
    if twitch is not None and items:
        await _fill_alert_history_vods(twitch, db, items)
    if not items:
        context.user_data["alert_history_pages"] = []
        context.user_data["alert_history_rich_pages"] = []
        context.user_data["alert_history_deep"] = deep
        return [], [], deep
    bot_username = _alert_history_bot_username(context)
    pages = _build_alert_history_chunks(
        items, lang, days, bot_username=bot_username
    )
    rich_pages = _build_alert_history_rich_pages(items, lang, days)
    context.user_data["alert_history_pages"] = pages
    context.user_data["alert_history_rich_pages"] = rich_pages
    context.user_data["alert_history_deep"] = deep
    return pages, rich_pages, deep


async def show_alert_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    pages, rich_pages, deep = await _load_alert_history_pages(context, user_id, lang)

    if not pages:
        empty_rows: list[list[InlineKeyboardButton]] = []
        if not deep:
            empty_rows.append(
                [
                    InlineKeyboardButton(
                        btn("alert_history_more", lang),
                        callback_data="alert_history:more",
                    )
                ]
            )
        empty_rows.append(_alert_history_menu_row(lang))
        await update.effective_message.reply_text(
            t("alert_history_empty", lang),
            reply_markup=InlineKeyboardMarkup(empty_rows),
            disable_web_page_preview=True,
        )
        return

    kb = _alert_history_nav_keyboard(
        lang, 0, len(pages), show_more=not deep
    )
    chat_id = reply_chat_id(update)
    msg, is_rich = await _send_alert_history_page(
        context.bot,
        chat_id,
        rich_page=rich_pages[0] if rich_pages else None,
        html_page=pages[0],
        reply_markup=kb,
    )
    mid = _message_id_from_result(msg)
    if mid is not None:
        _remember_alert_history_ui(
            context,
            user_id,
            chat_id=_resolve_message_chat_id(msg, chat_id),
            message_id=mid,
            page=0,
            is_rich=is_rich,
        )


async def on_alert_history_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    try:
        page = int(query.data.rsplit(":", 1)[1])
    except (TypeError, ValueError, IndexError):
        return
    pages = context.user_data.get("alert_history_pages")
    rich_pages = context.user_data.get("alert_history_rich_pages")
    deep = bool(context.user_data.get("alert_history_deep"))
    if not isinstance(pages, list) or not pages:
        pages, rich_pages, deep = await _load_alert_history_pages(
            context, user_id, lang
        )
        if not pages:
            await query.edit_message_text(t("alert_history_empty", lang))
            return
    if not isinstance(rich_pages, list):
        rich_pages = []
    page = max(0, min(page, len(pages) - 1))
    kb = _alert_history_nav_keyboard(
        lang, page, len(pages), show_more=not deep
    )
    store = context.application.bot_data.get("alert_history_ui") or {}
    ui = store.get(int(user_id)) if isinstance(store, dict) else None
    prefer_rich = bool(isinstance(ui, dict) and ui.get("is_rich"))
    rich_page = rich_pages[page] if page < len(rich_pages) else None
    if query.message is not None:
        mid = getattr(query.message, "message_id", None)
        chat_id = _resolve_message_chat_id(query.message, reply_chat_id(update))
        if mid is not None:
            is_rich = False
            if (prefer_rich or rich_page is not None) and rich_page is not None:
                is_rich = await edit_rich_message(
                    context.bot,
                    chat_id=chat_id,
                    message_id=int(mid),
                    rich_message=rich_page,
                    reply_markup=kb,
                )
            if not is_rich:
                try:
                    await query.edit_message_text(
                        pages[page],
                        reply_markup=kb,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                except BadRequest as exc:
                    if "not modified" not in str(exc).lower():
                        raise
            _remember_alert_history_ui(
                context,
                user_id,
                chat_id=chat_id,
                message_id=int(mid),
                page=page,
                is_rich=is_rich,
            )
            return


async def on_alert_history_noop(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await update.callback_query.answer()


async def on_alert_history_more(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    from premium_handlers import send_premium_screen

    await send_premium_screen(context.bot, user_id, lang, db, update=update)


async def on_alert_history_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    context.user_data.pop("alert_history_pages", None)
    context.user_data.pop("alert_history_deep", None)
    store = context.application.bot_data.get("alert_history_ui")
    if isinstance(store, dict):
        store.pop(int(user_id), None)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        pass
    await context.bot.send_message(
        reply_chat_id(update), t("menu_main", lang), reply_markup=_menu(lang, user_id)
    )


def _parse_alert_history_start_arg(raw: str) -> tuple[str, int] | None:
    text = (raw or "").strip()
    for prefix, action in (
        (_START_VIEWED_BELOW, "vb"),
        (_START_VIEWED, "v"),
        (_START_UNVIEWED, "u"),
    ):
        if text.startswith(prefix):
            rest = text[len(prefix) :]
            try:
                return action, int(rest)
            except ValueError:
                return None
    return None


async def _refresh_alert_history_message(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    lang: str,
    *,
    preferred_page: int | None = None,
) -> None:
    pages, rich_pages, deep = await _load_alert_history_pages(context, user_id, lang)
    store = context.application.bot_data.get("alert_history_ui") or {}
    ui = store.get(int(user_id)) if isinstance(store, dict) else None
    page = 0
    if preferred_page is not None:
        page = preferred_page
    elif isinstance(ui, dict):
        try:
            page = int(ui.get("page") or 0)
        except (TypeError, ValueError):
            page = 0
    if not pages:
        if isinstance(ui, dict) and ui.get("chat_id") and ui.get("message_id"):
            try:
                await context.bot.edit_message_text(
                    chat_id=int(ui["chat_id"]),
                    message_id=int(ui["message_id"]),
                    text=t("alert_history_empty", lang),
                    reply_markup=InlineKeyboardMarkup(
                        [_alert_history_menu_row(lang)]
                    ),
                    disable_web_page_preview=True,
                )
            except BadRequest:
                pass
        return
    page = max(0, min(page, len(pages) - 1))
    kb = _alert_history_nav_keyboard(
        lang, page, len(pages), show_more=not deep
    )
    rich_page = rich_pages[page] if page < len(rich_pages) else None
    prefer_rich = bool(isinstance(ui, dict) and ui.get("is_rich"))
    if isinstance(ui, dict) and ui.get("chat_id") and ui.get("message_id"):
        try:
            is_rich = await _edit_alert_history_page(
                context.bot,
                chat_id=int(ui["chat_id"]),
                message_id=int(ui["message_id"]),
                rich_page=rich_page,
                html_page=pages[page],
                reply_markup=kb,
                prefer_rich=prefer_rich or rich_page is not None,
            )
            _remember_alert_history_ui(
                context,
                user_id,
                chat_id=int(ui["chat_id"]),
                message_id=int(ui["message_id"]),
                page=page,
                is_rich=is_rich,
            )
            return
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
    msg, is_rich = await _send_alert_history_page(
        context.bot,
        user_id,
        rich_page=rich_page,
        html_page=pages[page],
        reply_markup=kb,
    )
    mid = _message_id_from_result(msg)
    if mid is not None:
        _remember_alert_history_ui(
            context,
            user_id,
            chat_id=_resolve_message_chat_id(msg, user_id),
            message_id=mid,
            page=page,
            is_rich=is_rich,
        )


async def on_alert_history_action(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Mark viewed / unviewed / viewed-below from rich in-message buttons."""
    query = update.callback_query
    await query.answer()
    data = (query.data or "").strip()
    action = None
    history_id = None
    for prefix, name in (
        (_CB_VIEWED_BELOW, "vb"),
        (_CB_VIEWED, "v"),
        (_CB_UNVIEWED, "u"),
    ):
        if data.startswith(prefix):
            action = name
            try:
                history_id = int(data[len(prefix) :])
            except ValueError:
                return
            break
    if action is None or history_id is None:
        return
    user = query.from_user
    if user is None:
        return
    user_id = user.id
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    if action == "v":
        db.set_alert_history_viewed(user_id, history_id, viewed=True)
    elif action == "u":
        db.set_alert_history_viewed(user_id, history_id, viewed=False)
    else:
        db.set_alert_history_viewed_below(user_id, history_id, viewed=True)
    lang = _user_lang(context, user_id)
    store = context.application.bot_data.get("alert_history_ui") or {}
    ui = store.get(int(user_id)) if isinstance(store, dict) else None
    preferred_page = None
    if isinstance(ui, dict):
        try:
            preferred_page = int(ui.get("page") or 0)
        except (TypeError, ValueError):
            preferred_page = 0
    await _refresh_alert_history_message(
        context, user_id, lang, preferred_page=preferred_page
    )


async def handle_alert_history_start_arg(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Handle /start ah_v_* / ah_u_* / ah_vb_* from HTML-fallback action links.

    Returns True when the start payload was consumed (caller should skip welcome).
    """
    args = context.args
    if not args:
        return False
    parsed = _parse_alert_history_start_arg(args[0])
    if parsed is None:
        return False
    action, history_id = parsed
    user = update.effective_user
    if user is None:
        return True
    user_id = user.id
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    if action == "v":
        db.set_alert_history_viewed(user_id, history_id, viewed=True)
    elif action == "u":
        db.set_alert_history_viewed(user_id, history_id, viewed=False)
    else:
        db.set_alert_history_viewed_below(user_id, history_id, viewed=True)
    lang = db.get_user_locale(user_id) or "ru"
    store = context.application.bot_data.get("alert_history_ui") or {}
    ui = store.get(int(user_id)) if isinstance(store, dict) else None
    preferred_page = None
    if isinstance(ui, dict):
        try:
            preferred_page = int(ui.get("page") or 0)
        except (TypeError, ValueError):
            preferred_page = 0
    # Drop the visible /start ah_* message when possible.
    if update.effective_message is not None:
        try:
            await update.effective_message.delete()
        except BadRequest:
            pass
    await _refresh_alert_history_message(
        context, user_id, lang, preferred_page=preferred_page
    )
    return True
