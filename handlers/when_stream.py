"""Group/chat commands: next slot (/when) and day/week schedule listing."""
from __future__ import annotations

import asyncio
import html
import logging
import re
import uuid
from datetime import datetime, time, timedelta, timezone
from typing import Literal

from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import ContextTypes

from bot_helpers import (
    DeleteKind,
    clear_group_reply_keyboard,
    delete_chat_message,
    reply_lang,
)
from db import Database
from ephemeral import (
    EphemeralHandle,
    delete_ephemeral,
    edit_ephemeral_text,
    message_ephemeral_id,
    send_ephemeral_text,
)
from handlers.notifications import _parse_segment_start
from i18n import SCHEDULE_TZ, SUPPORTED_LOCALES, format_stream_schedule_date, t
from twitch import TwitchClient, twitch_login_link_html

logger = logging.getLogger(__name__)

ScheduleMode = Literal["when", "day", "week"]

# Ephemeral command + bot reply stay until we delete them (no short bot-side TTL).
SCHEDULE_REPLY_DELETE_AFTER = timedelta(minutes=5)
# Default APScheduler grace is 1s — a slow interval job (e.g. schedule_reminders)
# can delay the executor past that and drop the cleanup entirely.
SCHEDULE_REPLY_DELETE_MISFIRE_GRACE = int(SCHEDULE_REPLY_DELETE_AFTER.total_seconds())

# Free-text triggers. Leading @bot is common under Telegram privacy mode.
WHEN_STREAM_TEXT_RE = re.compile(
    r"(?i)^\s*(?:@\w+\s+)*(?:когда\s+стрим|when(?:'s|\s+is)?(?:\s+the)?\s+stream)\s*\??\s*$"
)
DAY_SCHEDULE_TEXT_RE = re.compile(
    r"(?i)^\s*(?:@\w+\s+)*("
    r"расписание\s+на\s+день"
    r"|розклад\s+на\s+день"
    r"|schedule\s+for\s+(?:the\s+)?day"
    r"|day(?:'s)?\s+schedule"
    r"|today'?s?\s+schedule"
    r")\s*\??\s*$"
)
WEEK_SCHEDULE_TEXT_RE = re.compile(
    r"(?i)^\s*(?:@\w+\s+)*("
    r"расписание\s+на\s+неделю"
    r"|розклад\s+на\s+тиждень"
    r"|schedule\s+for\s+(?:the\s+)?week"
    r"|week(?:ly)?\s+schedule"
    r")\s*\??\s*$"
)

def unique_streamers_for_chat(
    subs: list,
) -> list[tuple[str, str]]:
    """Return (twitch_user_id, twitch_username) in first-seen order."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for sub in subs:
        uid = str(getattr(sub, "twitch_user_id", "") or "").strip()
        if not uid or uid in seen:
            continue
        seen.add(uid)
        name = str(getattr(sub, "twitch_username", "") or "").strip() or uid
        out.append((uid, name))
    return out


def next_upcoming_segment(
    schedule: dict,
    *,
    now: datetime | None = None,
) -> dict | None:
    """Nearest future non-canceled segment, or None (empty/vacation/none)."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    if TwitchClient.vacation_active(schedule.get("vacation"), now=current):
        return None
    best: tuple[datetime, dict] | None = None
    for segment in schedule.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        if segment.get("canceled_until"):
            continue
        start = _parse_segment_start(segment)
        if start is None or start <= current:
            continue
        if best is None or start < best[0]:
            best = (start, segment)
    return best[1] if best else None


def schedule_window_end(
    mode: Literal["day", "week"],
    *,
    now: datetime | None = None,
) -> datetime:
    """Exclusive UTC end of the day/week window (MSK calendar)."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local = current.astimezone(SCHEDULE_TZ)
    days = 1 if mode == "day" else 7
    end_local = datetime.combine(
        local.date() + timedelta(days=days),
        time.min,
        tzinfo=SCHEDULE_TZ,
    )
    return end_local.astimezone(timezone.utc)


def segments_in_window(
    schedule: dict,
    *,
    window_start: datetime,
    window_end: datetime,
) -> list[tuple[datetime, str]]:
    """Non-canceled segments with start in [window_start, window_end), sorted."""
    if window_start.tzinfo is None:
        window_start = window_start.replace(tzinfo=timezone.utc)
    if window_end.tzinfo is None:
        window_end = window_end.replace(tzinfo=timezone.utc)
    window_start = window_start.astimezone(timezone.utc)
    window_end = window_end.astimezone(timezone.utc)
    if TwitchClient.vacation_active(schedule.get("vacation"), now=window_start):
        return []
    out: list[tuple[datetime, str]] = []
    for segment in schedule.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        if segment.get("canceled_until"):
            continue
        start = _parse_segment_start(segment)
        if start is None or start < window_start or start >= window_end:
            continue
        cat = segment.get("category") or {}
        category = (
            str(cat.get("name") or "")
            if isinstance(cat, dict)
            else ""
        )
        out.append((start, category))
    out.sort(key=lambda item: item[0])
    return out


def format_when_stream_line(
    *,
    lang: str,
    start: datetime,
    category: str,
) -> str:
    local = start.astimezone(SCHEDULE_TZ)
    when = local.strftime("%d.%m.%Y %H:%M MSK")
    category_label = (category or "").strip() or "—"
    return t("when_stream", lang, when=when, category=category_label)


def format_range_schedule_line(
    *,
    lang: str,
    start: datetime,
    category: str,
    escape_game: bool = False,
) -> str:
    local = start.astimezone(SCHEDULE_TZ)
    game = (category or "").strip() or "—"
    if escape_game:
        game = html.escape(game)
    return t(
        "stream_schedule_line",
        lang,
        date=format_stream_schedule_date(local.date(), lang),
        time=local.strftime("%H:%M"),
        game=game,
    )


def format_range_streamer_block(
    *,
    lang: str,
    slots: list[tuple[datetime, str]],
    escape_game: bool = False,
) -> str:
    return "\n".join(
        format_range_schedule_line(
            lang=lang,
            start=start,
            category=category,
            escape_game=escape_game,
        )
        for start, category in slots
    )


def format_schedule_with_streamer_link(
    *,
    username: str,
    body: str,
) -> str:
    return f"{twitch_login_link_html(username)}:\n{body}"


def schedule_streamer_keyboard(
    mode: ScheduleMode,
    streamers: list[tuple[str, str]],
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for uid, username in streamers:
        label = (username or uid)[:64]
        row.append(
            InlineKeyboardButton(label, callback_data=f"schq:{mode}:{uid}")
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def schedule_detail_keyboard(mode: ScheduleMode, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("btn_wizard_back", lang),
                    callback_data=f"schq:list:{mode}",
                )
            ]
        ]
    )


def _empty_text(mode: ScheduleMode, lang: str) -> str:
    if mode == "day":
        return t("schedule_range_empty_day", lang)
    if mode == "week":
        return t("schedule_range_empty_week", lang)
    return t("when_stream_no_schedule", lang)


def _segment_category(segment: dict) -> str:
    cat = segment.get("category") or {}
    return str(cat.get("name") or "") if isinstance(cat, dict) else ""


async def _fetch_schedule(
    twitch: TwitchClient,
    uid: str,
    *,
    mode: ScheduleMode,
    now: datetime,
) -> dict:
    window_end = (
        schedule_window_end(mode, now=now) if mode in ("day", "week") else None
    )
    stop_before = (
        window_end.strftime("%Y-%m-%dT%H:%M:%SZ") if window_end is not None else None
    )
    return await asyncio.to_thread(
        twitch.get_channel_schedule,
        uid,
        stop_before=stop_before,
    )


def _format_streamer_schedule(
    *,
    mode: ScheduleMode,
    lang: str,
    schedule: dict,
    now: datetime,
    username: str | None = None,
) -> str | None:
    linked = bool((username or "").strip())
    if mode == "when":
        segment = next_upcoming_segment(schedule, now=now)
        if segment is None:
            return None
        start = _parse_segment_start(segment)
        if start is None:
            return None
        raw_cat = (_segment_category(segment) or "").strip() or "—"
        body = format_when_stream_line(
            lang=lang,
            start=start,
            category=html.escape(raw_cat) if linked else raw_cat,
        )
    else:
        window_end = schedule_window_end(mode, now=now)
        slots = segments_in_window(schedule, window_start=now, window_end=window_end)
        if not slots:
            return None
        body = format_range_streamer_block(
            lang=lang, slots=slots, escape_game=linked
        )
    if linked:
        return format_schedule_with_streamer_link(
            username=username or "", body=body
        )
    return body


async def _discover_hits(
    *,
    twitch: TwitchClient,
    streamers: list[tuple[str, str]],
    mode: ScheduleMode,
    lang: str,
    now: datetime,
) -> tuple[list[tuple[str, str, str]], bool]:
    """Return ([(uid, username, text), …], any successful fetch)."""
    hits: list[tuple[str, str, str]] = []
    fetched_ok = False
    for uid, username in streamers:
        try:
            schedule = await _fetch_schedule(twitch, uid, mode=mode, now=now)
        except Exception:
            logger.exception("%s: schedule fetch failed for %s", mode, uid)
            continue
        fetched_ok = True
        text = _format_streamer_schedule(
            mode=mode, lang=lang, schedule=schedule, now=now
        )
        if text is None:
            continue
        hits.append((uid, username, text))
    return hits, fetched_ok


def _group_chat_type(chat) -> bool:
    return getattr(chat, "type", None) in (ChatType.GROUP, ChatType.SUPERGROUP)


async def _bot_is_chat_admin(bot, chat_id: int) -> bool:
    """True if the bot can start ephemeral replies without an ephemeral trigger."""
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id, me.id)
    except (BadRequest, Forbidden):
        return False
    except Exception:
        logger.exception("getChatMember(bot) failed in chat %s", chat_id)
        return False
    return member.status in (
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    )


async def _delete_trigger_message(bot, chat_id: int, message) -> bool:
    """Delete the user command that triggered a schedule query (slash / free-text)."""
    mid = getattr(message, "message_id", None)
    if mid is None:
        return False
    try:
        await delete_chat_message(
            bot, chat_id, int(mid), kind="user_command"
        )
        return True
    except (BadRequest, Forbidden):
        return False
    except Exception:
        logger.exception("delete trigger message failed chat=%s mid=%s", chat_id, mid)
        return False


async def _delete_schedule_messages_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    if job is None:
        return
    data = job.data or {}
    bot = context.bot
    for item in data.get("ephemerals") or []:
        try:
            handle = EphemeralHandle(
                chat_id=int(item["chat_id"]),
                receiver_user_id=int(item["receiver_user_id"]),
                ephemeral_message_id=int(item["ephemeral_message_id"]),
            )
            await delete_ephemeral(bot, handle)
        except Exception:
            logger.exception("schedule ephemeral auto-delete failed %s", item)
    chat_id = data.get("chat_id")
    for item in data.get("deletes") or []:
        if chat_id is None:
            continue
        mid = item.get("message_id")
        kind: DeleteKind | None = item.get("kind")
        if mid is None or kind not in ("bot_own", "user_command"):
            continue
        try:
            await delete_chat_message(
                bot, int(chat_id), int(mid), kind=kind
            )
        except (BadRequest, Forbidden):
            pass
        except Exception:
            logger.exception(
                "schedule message auto-delete failed chat=%s mid=%s", chat_id, mid
            )


def _schedule_cleanup(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    ephemerals: list[EphemeralHandle] | None = None,
    deletes: list[tuple[int, DeleteKind]] | None = None,
) -> None:
    eph_payload = [
        {
            "chat_id": h.chat_id,
            "receiver_user_id": h.receiver_user_id,
            "ephemeral_message_id": h.ephemeral_message_id,
        }
        for h in (ephemerals or [])
    ]
    delete_payload = [
        {"message_id": int(mid), "kind": kind}
        for mid, kind in (deletes or [])
    ]
    if not eph_payload and not delete_payload:
        return
    jq = context.job_queue
    if jq is None:
        return
    anchor = (
        eph_payload[0]["ephemeral_message_id"]
        if eph_payload
        else delete_payload[0]["message_id"]
    )
    job_name = f"sched_msg_del_{chat_id}_{anchor}_{uuid.uuid4().hex[:8]}"
    jq.run_once(
        _delete_schedule_messages_job,
        when=SCHEDULE_REPLY_DELETE_AFTER.total_seconds(),
        data={
            "chat_id": int(chat_id),
            "ephemerals": eph_payload,
            "deletes": delete_payload,
        },
        name=job_name,
        job_kwargs={
            "id": job_name,
            "replace_existing": False,
            "misfire_grace_time": SCHEDULE_REPLY_DELETE_MISFIRE_GRACE,
            "coalesce": True,
        },
    )


def _trigger_ephemeral_handle(
    message, *, chat_id: int, user_id: int
) -> EphemeralHandle | None:
    eph_id = message_ephemeral_id(message)
    if eph_id is None:
        return None
    return EphemeralHandle(
        chat_id=int(chat_id),
        receiver_user_id=int(user_id),
        ephemeral_message_id=eph_id,
    )


async def _purge_orphan_ephemerals(
    bot,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    user_id: int,
    message,
    status_eph: EphemeralHandle | None = None,
) -> None:
    """Drop status + user trigger when we abort without a schedule result.

    Immediate delete; if API fails, fall back to the usual 5‑min cleanup job.
    Public / unparsed slash also goes through deleteMessage (bot needs delete rights).
    """
    leftovers: list[EphemeralHandle] = []
    trigger = _trigger_ephemeral_handle(
        message, chat_id=chat_id, user_id=user_id
    )
    for handle in (status_eph, trigger):
        if handle is None:
            continue
        if not await delete_ephemeral(bot, handle):
            leftovers.append(handle)
    if leftovers:
        _schedule_cleanup(context, chat_id=chat_id, ephemerals=leftovers)
    if trigger is None:
        # No ephemeral id (public slash / free-text, or PTB dropped the field).
        if not await _delete_trigger_message(bot, chat_id, message):
            mid = getattr(message, "message_id", None)
            if mid is not None:
                _schedule_cleanup(
                    context,
                    chat_id=chat_id,
                    deletes=[(int(mid), "user_command")],
                )


async def _begin_schedule_ephemeral(
    bot,
    *,
    chat,
    chat_id: int,
    user_id: int,
    message,
    lang: str,
) -> tuple[EphemeralHandle | None, bool]:
    """Status ephemeral for the requester; admin path when the slash was public.

    Returns (status_handle, trigger_deleted).
    """
    if not _group_chat_type(chat):
        return None, False
    reply_eph_id = message_ephemeral_id(message)
    if reply_eph_id is not None:
        handle = await send_ephemeral_text(
            bot,
            chat_id,
            t("schedule_query_working", lang),
            user_id,
            reply_to_ephemeral_id=reply_eph_id,
            reply_markup=ReplyKeyboardRemove(),
        )
        return handle, False
    # Clients may still send a public /week even when is_ephemeral is set.
    # Chat-admin bots may start an ephemeral thread without an ephemeral trigger.
    if not await _bot_is_chat_admin(bot, chat_id):
        return None, False
    eph = await send_ephemeral_text(
        bot,
        chat_id,
        t("schedule_query_working", lang),
        user_id,
        reply_markup=ReplyKeyboardRemove(),
    )
    if eph is None:
        return None, False
    deleted = await _delete_trigger_message(bot, chat_id, message)
    return eph, deleted


async def _reply_schedule_message(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    *,
    reply_markup=None,
    as_html: bool = False,
) -> int | None:
    send_kw: dict = {}
    chat = getattr(message, "chat", None)
    in_group = chat is not None and getattr(chat, "type", None) != ChatType.PRIVATE
    if reply_markup is not None:
        send_kw["reply_markup"] = reply_markup
        # Inline and ReplyKeyboardRemove cannot share one message — pulse-clear first.
        if in_group:
            await clear_group_reply_keyboard(context.bot, chat_id)
    elif in_group:
        send_kw["reply_markup"] = ReplyKeyboardRemove()
    if as_html:
        send_kw["parse_mode"] = ParseMode.HTML
        send_kw["disable_web_page_preview"] = True
    try:
        sent = await message.reply_text(text, **send_kw)
    except BadRequest as exc:
        # Trigger deleted while we fetched schedule — still answer in the chat.
        if "message to be replied not found" not in str(exc).lower():
            raise
        sent = await context.bot.send_message(chat_id, text, **send_kw)
    return getattr(sent, "message_id", None)


async def _deliver_schedule_result(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    *,
    eph: EphemeralHandle | None,
    reply_markup=None,
    as_html: bool = False,
) -> EphemeralHandle | int | None:
    """Edit ephemeral status → result, or public reply fallback.

    Returns the final ephemeral handle, public reply message_id, or None.
    If an ephemeral status was already shown, never fall back to a public
    reply (that duplicates the answer for everyone else).
    """
    if eph is not None:
        if reply_markup is not None and _group_chat_type(getattr(message, "chat", None)):
            await clear_group_reply_keyboard(context.bot, chat_id)
        parse_mode = ParseMode.HTML if as_html else None
        ok = await edit_ephemeral_text(
            context.bot,
            eph,
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=as_html,
        )
        if ok:
            return eph
        # Edit failed — send a fresh ephemeral result; drop the stuck status.
        await delete_ephemeral(context.bot, eph)
        replacement = await send_ephemeral_text(
            context.bot,
            chat_id,
            text,
            eph.receiver_user_id,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=as_html,
        )
        if replacement is not None:
            return replacement
        logger.warning(
            "ephemeral schedule result failed chat=%s user=%s; skipping public fallback",
            chat_id,
            eph.receiver_user_id,
        )
        return None
    return await _reply_schedule_message(
        message,
        context,
        chat_id,
        text,
        reply_markup=reply_markup,
        as_html=as_html,
    )


async def _edit_schedule_message(
    query,
    text: str,
    *,
    bot=None,
    chat_id: int | None = None,
    receiver_user_id: int | None = None,
    reply_markup=None,
    as_html: bool = False,
) -> None:
    eph_id = message_ephemeral_id(getattr(query, "message", None))
    if (
        eph_id is not None
        and bot is not None
        and chat_id is not None
        and receiver_user_id is not None
    ):
        ok = await edit_ephemeral_text(
            bot,
            EphemeralHandle(
                chat_id=int(chat_id),
                receiver_user_id=int(receiver_user_id),
                ephemeral_message_id=eph_id,
            ),
            text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML if as_html else None,
            disable_web_page_preview=as_html,
        )
        if ok:
            return
    edit_kw: dict = {}
    if reply_markup is not None:
        edit_kw["reply_markup"] = reply_markup
    if as_html:
        edit_kw["parse_mode"] = ParseMode.HTML
        edit_kw["disable_web_page_preview"] = True
    try:
        await query.edit_message_text(text, **edit_kw)
    except BadRequest:
        pass


async def _finalize_schedule_result(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    user_id: int,
    message,
    result: EphemeralHandle | int | None,
    trigger_deleted: bool,
) -> None:
    """Drop the user trigger ASAP; keep the bot reply until the 5‑min TTL.

    Ephemeral slash labels («Видит только @bot») otherwise linger for the full
    TTL even though the answer is already on screen.
    """
    trigger_eph = _trigger_ephemeral_handle(
        message, chat_id=chat_id, user_id=user_id
    )
    if isinstance(result, EphemeralHandle):
        if (
            trigger_eph is not None
            and not trigger_deleted
            and trigger_eph.ephemeral_message_id != result.ephemeral_message_id
        ):
            if not await delete_ephemeral(context.bot, trigger_eph):
                _schedule_cleanup(
                    context,
                    chat_id=chat_id,
                    ephemerals=[result, trigger_eph],
                )
                return
        _schedule_cleanup(context, chat_id=chat_id, ephemerals=[result])
        return
    if isinstance(result, int):
        deletes: list[tuple[int, DeleteKind]] = [(result, "bot_own")]
        if not trigger_deleted:
            if trigger_eph is not None:
                if not await delete_ephemeral(context.bot, trigger_eph):
                    _schedule_cleanup(
                        context,
                        chat_id=chat_id,
                        ephemerals=[trigger_eph],
                        deletes=deletes,
                    )
                    return
            else:
                if not await _delete_trigger_message(context.bot, chat_id, message):
                    mid = getattr(message, "message_id", None)
                    if mid is not None:
                        deletes.append((int(mid), "user_command"))
        _schedule_cleanup(context, chat_id=chat_id, deletes=deletes)
        return
    # Deliver failed — purge leftover trigger/status.
    await _purge_orphan_ephemerals(
        context.bot,
        context,
        chat_id=chat_id,
        user_id=user_id,
        message=message,
    )


async def _schedule_query_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    mode: ScheduleMode,
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or not user:
        return

    chat_id = int(chat.id)
    user_id = int(user.id)

    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    lang = reply_lang(context, update, user.id)

    streamers = unique_streamers_for_chat(
        db.get_enabled_subscriptions_by_chat_id(chat_id)
    )
    if not streamers:
        # Ephemeral /when|/day|/week otherwise sticks forever for the caller.
        await _purge_orphan_ephemerals(
            context.bot,
            context,
            chat_id=chat_id,
            user_id=user_id,
            message=message,
        )
        return

    # Ephemeral: reply to ephemeral slash, or admin-start when the slash was public.
    eph, trigger_deleted = await _begin_schedule_ephemeral(
        context.bot,
        chat=chat,
        chat_id=chat_id,
        user_id=user_id,
        message=message,
        lang=lang,
    )

    now = datetime.now(timezone.utc)
    hits, fetched_ok = await _discover_hits(
        twitch=twitch, streamers=streamers, mode=mode, lang=lang, now=now
    )
    if not hits:
        if not fetched_ok:
            await _purge_orphan_ephemerals(
                context.bot,
                context,
                chat_id=chat_id,
                user_id=user_id,
                message=message,
                status_eph=eph,
            )
            return
        result = await _deliver_schedule_result(
            message, context, chat_id, _empty_text(mode, lang), eph=eph
        )
        await _finalize_schedule_result(
            context,
            chat_id=chat_id,
            user_id=user_id,
            message=message,
            result=result,
            trigger_deleted=trigger_deleted,
        )
        return

    if len(hits) == 1:
        result = await _deliver_schedule_result(
            message, context, chat_id, hits[0][2], eph=eph
        )
        await _finalize_schedule_result(
            context,
            chat_id=chat_id,
            user_id=user_id,
            message=message,
            result=result,
            trigger_deleted=trigger_deleted,
        )
        return

    result = await _deliver_schedule_result(
        message,
        context,
        chat_id,
        t("schedule_found_streamers", lang),
        eph=eph,
        reply_markup=schedule_streamer_keyboard(
            mode, [(uid, name) for uid, name, _text in hits]
        ),
    )
    await _finalize_schedule_result(
        context,
        chat_id=chat_id,
        user_id=user_id,
        message=message,
        result=result,
        trigger_deleted=trigger_deleted,
    )


async def when_stream_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _schedule_query_command(update, context, mode="when")


async def day_schedule_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _schedule_query_command(update, context, mode="day")


async def week_schedule_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _schedule_query_command(update, context, mode="week")


async def on_schedule_query_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    raw = query.data
    user = update.effective_user
    if user is None:
        await query.answer()
        return

    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    lang = reply_lang(context, update, user.id)
    now = datetime.now(timezone.utc)
    receiver_user_id = int(user.id)

    list_m = re.fullmatch(r"schq:list:(when|day|week)", raw)
    pick_m = re.fullmatch(r"schq:(when|day|week):(\d+)", raw)
    if list_m:
        mode: ScheduleMode = list_m.group(1)  # type: ignore[assignment]
        chat = update.effective_chat
        if chat is not None:
            chat_id = int(chat.id)
        elif query.message is not None:
            chat_id = int(query.message.chat_id)
        else:
            await query.answer()
            return
        streamers = unique_streamers_for_chat(
            db.get_enabled_subscriptions_by_chat_id(chat_id)
        )
        hits, fetched_ok = await _discover_hits(
            twitch=twitch,
            streamers=streamers,
            mode=mode,
            lang=lang,
            now=now,
        )
        await query.answer()
        edit_kw = {
            "bot": context.bot,
            "chat_id": chat_id,
            "receiver_user_id": receiver_user_id,
        }
        if not hits:
            text = (
                _empty_text(mode, lang)
                if fetched_ok
                else t("schedule_found_streamers", lang)
            )
            await _edit_schedule_message(query, text, **edit_kw)
            return
        if len(hits) == 1:
            uid, username, _plain = hits[0]
            try:
                schedule = await _fetch_schedule(twitch, uid, mode=mode, now=now)
                text = (
                    _format_streamer_schedule(
                        mode=mode,
                        lang=lang,
                        schedule=schedule,
                        now=now,
                        username=username,
                    )
                    or _empty_text(mode, lang)
                )
            except Exception:
                logger.exception("%s: schedule re-fetch failed for %s", mode, uid)
                return
            await _edit_schedule_message(query, text, as_html=True, **edit_kw)
            return
        await _edit_schedule_message(
            query,
            t("schedule_found_streamers", lang),
            reply_markup=schedule_streamer_keyboard(
                mode, [(uid, name) for uid, name, _text in hits]
            ),
            **edit_kw,
        )
        return

    if not pick_m:
        await query.answer()
        return

    mode = pick_m.group(1)  # type: ignore[assignment]
    uid = pick_m.group(2)
    chat = update.effective_chat
    if chat is not None:
        chat_id = int(chat.id)
    elif query.message is not None:
        chat_id = int(query.message.chat_id)
    else:
        await query.answer()
        return
    name_by_id = dict(
        unique_streamers_for_chat(db.get_enabled_subscriptions_by_chat_id(chat_id))
    )
    username = name_by_id.get(uid, uid)
    try:
        schedule = await _fetch_schedule(twitch, uid, mode=mode, now=now)
        text = _format_streamer_schedule(
            mode=mode,
            lang=lang,
            schedule=schedule,
            now=now,
            username=username,
        )
    except Exception:
        logger.exception("%s: schedule pick failed for %s", mode, uid)
        await query.answer()
        return
    await query.answer()
    edit_kw = {
        "bot": context.bot,
        "chat_id": chat_id,
        "receiver_user_id": receiver_user_id,
    }
    if not text:
        text = _empty_text(mode, lang)
        await _edit_schedule_message(
            query,
            text,
            reply_markup=schedule_detail_keyboard(mode, lang),
            **edit_kw,
        )
        return
    await _edit_schedule_message(
        query,
        text,
        reply_markup=schedule_detail_keyboard(mode, lang),
        as_html=True,
        **edit_kw,
    )


async def ensure_group_schedule_ephemeral_commands(bot) -> None:
    """Register /when /day /week as ephemeral for all group chats."""
    scope = BotCommandScopeAllGroupChats()
    for lang in SUPPORTED_LOCALES:
        commands = [
            BotCommand(
                "when",
                t("cmd_when_desc", lang)[:256],
                api_kwargs={"is_ephemeral": True},
            ),
            BotCommand(
                "day",
                t("cmd_day_desc", lang)[:256],
                api_kwargs={"is_ephemeral": True},
            ),
            BotCommand(
                "week",
                t("cmd_week_desc", lang)[:256],
                api_kwargs={"is_ephemeral": True},
            ),
        ]
        language_code = None if lang == "en" else lang
        try:
            await bot.set_my_commands(
                commands, scope=scope, language_code=language_code
            )
        except Exception:
            logger.exception(
                "setMyCommands ephemeral schedule failed lang=%s", lang
            )
