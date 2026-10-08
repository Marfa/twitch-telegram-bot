"""AI clips: chat cues + loud VOD peaks → Helix Create Clip From VOD (Premium)."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from html import escape as html_escape
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, ContextTypes

import analytics
import beta as beta_features
import premium as prem
from ai_clips import (
    AUDIO_SEED_N,
    CLIP_DURATION_SEC,
    MAX_CLIPS,
    MAX_VOD_ANALYZE_SEC,
    SPIKE_SEED_N,
    ClipCandidate,
    ClipSource,
    CreatedClip,
    LoudPeak,
    ai_clips_ready,
    analyze_vod_rms,
    classify_spikes_with_groq,
    clip_start_offset,
    clip_watch_url,
    fetch_vod_chat_messages,
    filter_unoccupied,
    find_loud_peaks,
    find_message_spikes,
    find_phrase_peaks,
    format_clip_timecode,
    groq_api_ready,
    groq_asr_ready,
    helix_vod_clip_starts,
    merge_clip_candidates,
    normalize_clip_source,
    parse_helix_duration,
    score_audio_peaks_with_groq,
    start_is_occupied,
)
from bot_helpers import _user_lang, reply_chat_id, with_oauth_legal
from db import Database
from i18n import DEFAULT_LOCALE, other_menu, t
from twitch import CLIPS_MANAGE_SCOPE, TwitchClient

logger = logging.getLogger(__name__)

BETA_FEATURE_ID = "ai-clips"
FEATURE_ID = "ai_clips"
VOD_PAGE_SIZE = 5
_VOD_FETCH_FIRST = 50

# In-process guard so the same job id is not started twice after resume.
_RUNNING_JOB_IDS: set[int] = set()
_RUNNING_LOCK = asyncio.Lock()


def _oauth_ready() -> bool:
    from config import twitch_oauth_redirect_uri

    return bool(twitch_oauth_redirect_uri())


async def _entitled(bot: Any, db: Database, user_id: int) -> tuple[bool, bool]:
    beta_ok = beta_features.is_enabled(db, user_id, BETA_FEATURE_ID)
    if not beta_ok:
        return False, False
    premium_ok = await prem.has_feature(bot, db, user_id, FEATURE_ID)
    return True, premium_ok


def _vod_pick_keyboard(
    lang: str,
    videos: list[dict[str, Any]],
    *,
    page: int,
    show_status: bool = False,
    auto_on: bool = False,
) -> InlineKeyboardMarkup:
    total = len(videos)
    pages = max(1, (total + VOD_PAGE_SIZE - 1) // VOD_PAGE_SIZE)
    page = max(0, min(int(page), pages - 1))
    start = page * VOD_PAGE_SIZE
    chunk = videos[start : start + VOD_PAGE_SIZE]
    rows: list[list[InlineKeyboardButton]] = []
    if show_status:
        rows.append(
            [
                InlineKeyboardButton(
                    t("ai_clips_status_btn", lang),
                    callback_data="ai_clips:status",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                t(
                    "ai_clips_auto_on" if auto_on else "ai_clips_auto_off",
                    lang,
                ),
                callback_data="ai_clips:auto",
            )
        ]
    )
    for v in chunk:
        vid = str(v.get("id") or "").strip()
        if not vid:
            continue
        title = str(v.get("title") or vid).strip() or vid
        dur = str(v.get("duration") or "").strip()
        label = f"{title[:40]} ({dur})" if dur else title[:48]
        rows.append(
            [InlineKeyboardButton(label, callback_data=f"ai_clips:vod:{vid}")]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                t("ai_clips_page_prev", lang),
                callback_data=f"ai_clips:page:{page - 1}",
            )
        )
    if page < pages - 1:
        nav.append(
            InlineKeyboardButton(
                t("ai_clips_page_next", lang),
                callback_data=f"ai_clips:page:{page + 1}",
            )
        )
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(
                t("ai_clips_cancel", lang), callback_data="ai_clips:cancel"
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def _auto_on(db: Database, user_id: int) -> bool:
    pref = db.get_ai_clips_auto(user_id)
    return bool(pref and pref.enabled)


async def _send_oauth_prompt(
    bot: Any, twitch: TwitchClient, user_id: int, lang: str
) -> None:
    from config import twitch_oauth_redirect_uri
    from health import create_pending_login_state

    if not _oauth_ready():
        await bot.send_message(user_id, t("ai_clips_oauth_unavailable", lang))
        return
    redirect = twitch_oauth_redirect_uri()
    state = create_pending_login_state(user_id, lang, purpose="ai_clips")
    url = twitch.build_authorize_url(
        redirect_uri=redirect,
        state=state,
        force_verify=True,
    )
    await bot.send_message(
        user_id,
        with_oauth_legal(t("ai_clips_oauth_prompt", lang), lang),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        t("ai_clips_oauth_button", lang), url=url
                    )
                ]
            ]
        ),
    )


async def _ensure_clips_token(
    db: Database, twitch: TwitchClient, user_id: int
) -> dict[str, str] | None:
    sync = db.get_twitch_sync(user_id)
    if not sync or not sync.refresh_token or not sync.twitch_user_id:
        return None
    try:
        token_data = await asyncio.to_thread(
            twitch.refresh_user_token, sync.refresh_token
        )
    except Exception:
        logger.warning("ai_clips token refresh failed owner=%s", user_id)
        return None
    access = token_data.get("access_token") or ""
    refresh = token_data.get("refresh_token") or sync.refresh_token
    if not access or not await asyncio.to_thread(
        twitch.token_has_scope, access, CLIPS_MANAGE_SCOPE
    ):
        return None
    if refresh != sync.refresh_token:
        db.update_twitch_sync_tokens(
            user_id,
            refresh,
            last_sync_at=sync.last_sync_at or "",
            next_sync_at=sync.next_sync_at or "",
        )
    return {
        "access_token": access,
        "refresh_token": refresh,
        "twitch_user_id": sync.twitch_user_id,
    }


def _archive_videos(videos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for v in videos:
        vid = str(v.get("id") or "").strip()
        if not vid.isdigit():
            continue
        if parse_helix_duration(str(v.get("duration") or "")) < CLIP_DURATION_SEC:
            continue
        out.append(
            {
                "id": vid,
                "title": str(v.get("title") or vid),
                "duration": str(v.get("duration") or ""),
                "created_at": str(v.get("created_at") or ""),
                "stream_id": str(v.get("stream_id") or "").strip(),
            }
        )
    return out


def _archives_excluding_live_recording(
    archives: list[dict[str, Any]],
    live_stream: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Drop the in-progress archive while the channel is live.

    Twitch creates an archive VOD at stream start; Helix ``stream_id`` matches
    the live stream ``id``. Auto clips must wait until the stream ends.
    """
    if not archives or not live_stream:
        return archives
    live_sid = str(live_stream.get("id") or "").strip()
    if live_sid:
        filtered = [
            v for v in archives if str(v.get("stream_id") or "").strip() != live_sid
        ]
        if len(filtered) < len(archives):
            return filtered
    # Fallback when Helix omits stream_id: newest archive is the recording.
    return archives[1:]


def _parse_rfc3339(raw: str) -> datetime | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _existing_clips_keyboard(lang: str, vod_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("ai_clips_rerun", lang),
                    callback_data=f"ai_clips:rerun:{vod_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    t("ai_clips_back_vods", lang),
                    callback_data="ai_clips:back",
                )
            ],
        ]
    )


def _format_clips_message(
    lang: str,
    clips: list[dict[str, Any]],
    *,
    title_key: str = "ai_clips_existing_title",
) -> str:
    if not clips:
        return t("ai_clips_existing_empty", lang)
    lines = [t(title_key, lang, count=len(clips))]
    for i, c in enumerate(clips, start=1):
        url = html_escape(
            str(c.get("url") or clip_watch_url(str(c.get("id") or ""))).strip()
        )
        offset_raw = c.get("vod_offset")
        try:
            offset = int(offset_raw) if offset_raw is not None else 0
        except (TypeError, ValueError):
            offset = 0
        lines.append(
            t(
                "ai_clips_done_line",
                lang,
                n=i,
                offset=format_clip_timecode(max(0, offset)),
                url=url,
            )
        )
    return "\n".join(lines)


def _stored_clip_start(clip: CreatedClip) -> int:
    """Create Clip From VOD stores end offset; UI / Helix use start."""
    return clip_start_offset(int(clip.vod_offset))


def _helix_offset_or_none(row: dict[str, Any]) -> int | None:
    raw = row.get("vod_offset")
    if raw is None or raw == "":
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return None


def _merge_clip_row(
    row: dict[str, Any],
    *,
    fallback_start: int | None = None,
) -> dict[str, Any]:
    """Copy Helix row; keep ``fallback_start`` when Helix ``vod_offset`` is null.

    Create Clip From VOD often returns ``video_id=""`` and ``vod_offset=null``
    for minutes (or longer); without a fallback the UI shows 0:00:00.
    """
    merged = dict(row)
    if _helix_offset_or_none(merged) is None and fallback_start is not None:
        merged["vod_offset"] = int(fallback_start)
    return merged


async def _fetch_live_vod_clips(
    twitch: TwitchClient,
    *,
    broadcaster_id: str,
    vod_id: str,
    stored: list[CreatedClip],
    started_at: datetime | None,
) -> tuple[list[dict[str, Any]], bool]:
    """Current Helix clips for a VOD (bot-made + user-made).

    Only clips Helix still returns are listed — deleted Twitch clips drop out.
    ``stored`` is used for ``vod_offset`` fallback when Create Clip From VOD
    leaves Helix ``vod_offset`` null. Returns ``(clips, helix_ok)``; on total
    Helix failure the caller may fall back to the stored job payload.
    """
    by_id: dict[str, dict[str, Any]] = {}
    stored_starts = {
        c.clip_id: _stored_clip_start(c) for c in stored if c.clip_id
    }
    helix_ok = False
    stored_ids = list(stored_starts)
    if stored_ids:
        try:
            for row in await asyncio.to_thread(twitch.get_clips_by_ids, stored_ids):
                cid = str(row.get("id") or "").strip()
                if not cid:
                    continue
                by_id[cid] = _merge_clip_row(
                    row, fallback_start=stored_starts.get(cid)
                )
            helix_ok = True
        except Exception:
            logger.warning(
                "ai_clips get_clips_by_ids failed vod=%s", vod_id, exc_info=True
            )
    since = started_at
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(days=7)
    else:
        # Clips can be created slightly before VOD publish stamp.
        since = since - timedelta(hours=6)
    try:
        live = await asyncio.to_thread(
            twitch.get_clips_for_video,
            broadcaster_id,
            vod_id,
            started_at=since,
        )
        helix_ok = True
        for row in live:
            cid = str(row.get("id") or "").strip()
            if not cid:
                continue
            prev = by_id.get(cid) or {}
            fallback = stored_starts.get(cid)
            if fallback is None:
                fallback = _helix_offset_or_none(prev)
            by_id[cid] = _merge_clip_row(row, fallback_start=fallback)
    except Exception:
        logger.warning(
            "ai_clips get_clips_for_video failed vod=%s", vod_id, exc_info=True
        )
    clips = list(by_id.values())

    def _sort_key(row: dict[str, Any]) -> tuple[int, str]:
        off = _helix_offset_or_none(row)
        if off is None:
            off = 10**9
        return (off, str(row.get("created_at") or ""))

    clips.sort(key=_sort_key)
    return clips, helix_ok


async def _show_vod_picker(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    lang: str,
    db: Database,
    user_id: int,
) -> None:
    videos: list[dict[str, Any]] = context.user_data.get("ai_clips_videos") or []
    page = int(context.user_data.get("ai_clips_page") or 0)
    if not videos:
        try:
            await query.edit_message_text(t("ai_clips_failed", lang))
        except BadRequest:
            pass
        return
    active = db.get_active_ai_clips_job(user_id)
    try:
        await query.edit_message_text(
            t("ai_clips_pick_vod", lang, max_clips=MAX_CLIPS),
            parse_mode=ParseMode.HTML,
            reply_markup=_vod_pick_keyboard(
                lang,
                videos,
                page=page,
                show_status=active is not None,
                auto_on=_auto_on(db, user_id),
            ),
        )
    except BadRequest:
        pass


async def start_ai_clips(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    db.upsert_user(user_id)
    chat = reply_chat_id(update)
    beta_ok, premium_ok = await _entitled(context.bot, db, user_id)
    if not beta_ok:
        await context.bot.send_message(
            chat,
            t("ai_clips_beta_required", lang),
            reply_markup=other_menu(lang),
        )
        return
    if not premium_ok:
        from premium_handlers import send_premium_screen

        await context.bot.send_message(
            chat,
            t("ai_clips_premium_required", lang),
            reply_markup=other_menu(lang),
        )
        await send_premium_screen(
            context.bot,
            user_id,
            lang,
            db,
            update=update,
            context=context,
            source="ai_clips",
            feature=FEATURE_ID,
        )
        return
    if not ai_clips_ready():
        await context.bot.send_message(
            chat,
            t("ai_clips_tools_missing", lang),
            reply_markup=other_menu(lang),
        )
        return

    token = await _ensure_clips_token(db, twitch, user_id)
    if not token:
        await context.bot.send_message(
            chat,
            t("ai_clips_need_auth", lang),
            reply_markup=other_menu(lang),
        )
        await _send_oauth_prompt(context.bot, twitch, user_id, lang)
        return

    try:
        videos = await asyncio.to_thread(
            twitch.get_videos_by_user,
            token["twitch_user_id"],
            first=_VOD_FETCH_FIRST,
        )
    except Exception:
        logger.exception("ai_clips list VODs failed owner=%s", user_id)
        await context.bot.send_message(
            chat,
            t("ai_clips_list_failed", lang),
            reply_markup=other_menu(lang),
        )
        return

    archives = _archive_videos(videos)
    if not archives:
        await context.bot.send_message(
            chat,
            t("ai_clips_no_vods", lang),
            reply_markup=other_menu(lang),
        )
        return

    context.user_data["ai_clips_videos"] = archives
    context.user_data["ai_clips_page"] = 0
    active = db.get_active_ai_clips_job(user_id)
    await context.bot.send_message(
        chat,
        t("ai_clips_pick_vod", lang, max_clips=MAX_CLIPS),
        parse_mode=ParseMode.HTML,
        reply_markup=_vod_pick_keyboard(
            lang,
            archives,
            page=0,
            show_status=active is not None,
            auto_on=_auto_on(db, user_id),
        ),
    )


async def on_ai_clips_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    data = (query.data or "").strip()
    chat = reply_chat_id(update)
    db: Database = context.application.bot_data["db"]

    if data == "ai_clips:status":
        job = db.get_active_ai_clips_job(user_id)
        if not job:
            await query.answer(t("ai_clips_status_idle", lang), show_alert=True)
            return
        await query.answer(
            t(
                "ai_clips_status_text",
                lang,
                title=(job.vod_title or job.vod_id)[:60],
                pct=max(0, min(100, int(job.progress_pct or 0))),
                status=job.status,
            ),
            show_alert=True,
        )
        return

    if data == "ai_clips:auto":
        beta_ok, premium_ok = await _entitled(context.bot, db, user_id)
        if not beta_ok or not premium_ok:
            await query.answer(t("ai_clips_failed", lang), show_alert=True)
            return
        twitch: TwitchClient = context.application.bot_data["twitch"]
        token = await _ensure_clips_token(db, twitch, user_id)
        if not token:
            await query.answer(t("ai_clips_need_auth", lang), show_alert=True)
            await _send_oauth_prompt(context.bot, twitch, user_id, lang)
            return
        currently = _auto_on(db, user_id)
        if currently:
            db.set_ai_clips_auto_enabled(user_id, enabled=False)
            await query.answer(t("ai_clips_auto_disabled", lang))
        else:
            baseline = ""
            try:
                newest = await asyncio.to_thread(
                    twitch.get_videos_by_user,
                    token["twitch_user_id"],
                    first=1,
                )
                if newest:
                    baseline = str(newest[0].get("id") or "").strip()
            except Exception:
                logger.warning(
                    "ai_clips auto baseline failed owner=%s", user_id, exc_info=True
                )
            db.set_ai_clips_auto_enabled(
                user_id,
                enabled=True,
                twitch_user_id=token["twitch_user_id"],
                last_vod_id=baseline,
            )
            await query.answer(t("ai_clips_auto_enabled", lang))
        videos: list[dict[str, Any]] = context.user_data.get("ai_clips_videos") or []
        page = int(context.user_data.get("ai_clips_page") or 0)
        active = db.get_active_ai_clips_job(user_id)
        if videos:
            try:
                await query.edit_message_reply_markup(
                    reply_markup=_vod_pick_keyboard(
                        lang,
                        videos,
                        page=page,
                        show_status=active is not None,
                        auto_on=_auto_on(db, user_id),
                    )
                )
            except BadRequest:
                pass
        return

    await query.answer()

    if data == "ai_clips:cancel":
        context.user_data.pop("ai_clips_videos", None)
        context.user_data.pop("ai_clips_page", None)
        try:
            await query.edit_message_text(t("ai_clips_canceled", lang))
        except BadRequest:
            pass
        await context.bot.send_message(
            chat, t("menu_other", lang), reply_markup=other_menu(lang)
        )
        return

    if data.startswith("ai_clips:page:"):
        videos = context.user_data.get("ai_clips_videos") or []
        if not videos:
            try:
                await query.edit_message_text(t("ai_clips_failed", lang))
            except BadRequest:
                pass
            return
        try:
            page = int(data.rsplit(":", 1)[-1])
        except ValueError:
            page = 0
        context.user_data["ai_clips_page"] = page
        active = db.get_active_ai_clips_job(user_id)
        try:
            await query.edit_message_text(
                t("ai_clips_pick_vod", lang, max_clips=MAX_CLIPS),
                parse_mode=ParseMode.HTML,
                reply_markup=_vod_pick_keyboard(
                    lang,
                    videos,
                    page=page,
                    show_status=active is not None,
                    auto_on=_auto_on(db, user_id),
                ),
            )
        except BadRequest:
            pass
        return

    if data == "ai_clips:back":
        await _show_vod_picker(
            query, context, lang=lang, db=db, user_id=user_id
        )
        return

    if data.startswith("ai_clips:rerun:"):
        vod_id = data.split(":", 2)[-1].strip()
        videos_map = {
            str(v["id"]): v
            for v in (context.user_data.get("ai_clips_videos") or [])
            if v.get("id")
        }
        video = videos_map.get(vod_id) or {
            "id": vod_id,
            "title": "Clip",
            "duration": "",
        }
        await _start_vod_job(
            query,
            context,
            user_id=user_id,
            lang=lang,
            db=db,
            chat=chat,
            vod_id=vod_id,
            video=video,
        )
        return

    if not data.startswith("ai_clips:vod:"):
        return

    vod_id = data.split(":", 2)[-1].strip()
    videos_map = {
        str(v["id"]): v
        for v in (context.user_data.get("ai_clips_videos") or [])
        if v.get("id")
    }
    video = videos_map.get(vod_id)
    beta_ok, premium_ok = await _entitled(context.bot, db, user_id)
    if not beta_ok or not premium_ok or not video:
        try:
            await query.edit_message_text(t("ai_clips_failed", lang))
        except BadRequest:
            pass
        await context.bot.send_message(
            chat, t("menu_other", lang), reply_markup=other_menu(lang)
        )
        return

    done = db.get_done_ai_clips_job(user_id, vod_id=vod_id)
    if done:
        twitch: TwitchClient = context.application.bot_data["twitch"]
        token = await _ensure_clips_token(db, twitch, user_id)
        broadcaster_id = (token or {}).get("twitch_user_id") or ""
        if not broadcaster_id:
            sync = db.get_twitch_sync(user_id)
            broadcaster_id = (sync.twitch_user_id if sync else "") or ""
        started = _parse_rfc3339(str(video.get("created_at") or ""))
        if started is None:
            started = _parse_rfc3339(done.created_at)
        stored = _clips_from_json(done.clips_json)
        clips: list[dict[str, Any]] = []
        helix_ok = False
        if broadcaster_id:
            clips, helix_ok = await _fetch_live_vod_clips(
                twitch,
                broadcaster_id=broadcaster_id,
                vod_id=vod_id,
                stored=stored,
                started_at=started,
            )
        # Only use the DB payload when Helix could not be queried — never when
        # Helix answered (including empty: user deleted clips on Twitch).
        if not clips and stored and not helix_ok:
            clips = [
                {
                    "id": c.clip_id,
                    "url": c.url or clip_watch_url(c.clip_id),
                    "vod_offset": _stored_clip_start(c),
                }
                for c in stored
            ]
        try:
            await query.edit_message_text(
                _format_clips_message(lang, clips),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=_existing_clips_keyboard(lang, vod_id),
            )
        except BadRequest:
            pass
        return

    await _start_vod_job(
        query,
        context,
        user_id=user_id,
        lang=lang,
        db=db,
        chat=chat,
        vod_id=vod_id,
        video=video,
    )


async def _start_vod_job(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_id: int,
    lang: str,
    db: Database,
    chat: int,
    vod_id: str,
    video: dict[str, Any],
) -> None:
    if db.count_active_ai_clips_jobs() > 0:
        try:
            await query.edit_message_text(t("ai_clips_busy", lang))
        except BadRequest:
            pass
        await context.bot.send_message(
            chat, t("menu_other", lang), reply_markup=other_menu(lang)
        )
        return

    title = html_escape(str(video.get("title") or vod_id)[:80])
    job_id = db.create_ai_clips_job(
        user_id,
        vod_id=vod_id,
        vod_title=str(video.get("title") or "Clip"),
    )
    try:
        await query.edit_message_text(
            t("ai_clips_working", lang, title=title),
            parse_mode=ParseMode.HTML,
        )
    except BadRequest:
        pass

    context.user_data.pop("ai_clips_videos", None)
    context.user_data.pop("ai_clips_page", None)
    asyncio.create_task(
        _run_job(
            context.application,
            job_id,
            lang=lang,
        )
    )


async def complete_ai_clips_oauth(
    application: Application,
    owner_id: int,
    error: str | None,
    token_info: dict[str, str] | None,
) -> None:
    db: Database = application.bot_data["db"]
    twitch: TwitchClient = application.bot_data["twitch"]
    lang = db.get_user_locale(owner_id) or DEFAULT_LOCALE
    if error or not token_info:
        await application.bot.send_message(
            owner_id,
            t("ai_clips_oauth_failed", lang),
            reply_markup=other_menu(lang),
        )
        return
    from oauth_tokens import apply_oauth_success_tokens, restore_after_twitch_oauth

    apply_oauth_success_tokens(db, owner_id, token_info)
    await restore_after_twitch_oauth(application, owner_id)
    access = token_info.get("access_token") or ""
    if not access or not twitch.token_has_scope(access, CLIPS_MANAGE_SCOPE):
        await _send_oauth_prompt(application.bot, twitch, owner_id, lang)
        return
    await application.bot.send_message(
        owner_id,
        t("ai_clips_oauth_done", lang),
        reply_markup=other_menu(lang),
    )


def _create_clips_blocking(
    twitch: TwitchClient,
    *,
    access: str,
    broadcaster_id: str,
    vod_id: str,
    vod_title: str,
    peaks: list[ClipCandidate] | list[LoudPeak],
    lang: str,
    occupied_starts: list[int] | None = None,
    max_clips: int = MAX_CLIPS,
) -> list[CreatedClip]:
    """Create up to ``max_clips``; skip peaks that already have a nearby VOD clip."""
    out: list[CreatedClip] = []
    occupied = [int(o) for o in (occupied_starts or [])]
    for peak in peaks:
        if len(out) >= max_clips:
            break
        start = clip_start_offset(int(peak.vod_offset))
        if start_is_occupied(start, occupied):
            logger.info(
                "ai_clips skip existing clip vod=%s start=%s",
                vod_id,
                start,
            )
            continue
        title = t(
            "ai_clips_clip_title",
            lang,
            n=len(out) + 1,
            title=(vod_title or "Clip")[:60],
        )
        try:
            row = twitch.create_clip_from_vod(
                access,
                editor_id=broadcaster_id,
                broadcaster_id=broadcaster_id,
                vod_id=vod_id,
                vod_offset=peak.vod_offset,
                title=title,
                duration=float(CLIP_DURATION_SEC),
            )
        except Exception:
            logger.warning(
                "ai_clips create failed vod=%s offset=%s",
                vod_id,
                peak.vod_offset,
                exc_info=True,
            )
            continue
        cid = str(row.get("id") or "").strip()
        if not cid:
            continue
        occupied.append(start)
        out.append(
            CreatedClip(
                clip_id=cid,
                edit_url=str(row.get("edit_url") or "").strip(),
                vod_offset=peak.vod_offset,
                url=clip_watch_url(cid),
            )
        )
    return out


def _peaks_from_json(raw: str) -> list[ClipCandidate]:
    try:
        data = json.loads(raw or "[]")
    except Exception:
        return []
    out: list[ClipCandidate] = []
    if not isinstance(data, list):
        return out
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            offset = int(item.get("vod_offset") or 0)
            score = float(item.get("score") or 0.0)
        except (TypeError, ValueError):
            continue
        source: ClipSource = normalize_clip_source(
            str(item.get("source") or "audio")
        )
        if offset > 0:
            out.append(
                ClipCandidate(vod_offset=offset, score=score, source=source)
            )
    return out


def _peaks_to_json(peaks: list[ClipCandidate] | list[LoudPeak]) -> str:
    rows: list[dict[str, Any]] = []
    for p in peaks:
        if isinstance(p, ClipCandidate):
            rows.append(
                {
                    "vod_offset": p.vod_offset,
                    "score": p.score,
                    "source": p.source,
                }
            )
        else:
            rows.append(
                {
                    "vod_offset": p.vod_offset,
                    "score": p.score,
                    "source": "audio",
                }
            )
    return json.dumps(rows, ensure_ascii=False)


def _clips_to_json(clips: list[CreatedClip]) -> str:
    return json.dumps(
        [
            {
                "clip_id": c.clip_id,
                "edit_url": c.edit_url,
                "vod_offset": c.vod_offset,
                "url": c.url,
            }
            for c in clips
        ],
        ensure_ascii=False,
    )


def _clips_from_json(raw: str) -> list[CreatedClip]:
    try:
        data = json.loads(raw or "[]")
    except Exception:
        return []
    out: list[CreatedClip] = []
    if not isinstance(data, list):
        return out
    for item in data:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("clip_id") or "").strip()
        if not cid:
            continue
        out.append(
            CreatedClip(
                clip_id=cid,
                edit_url=str(item.get("edit_url") or "").strip(),
                vod_offset=int(item.get("vod_offset") or 0),
                url=str(item.get("url") or clip_watch_url(cid)).strip(),
            )
        )
    return out


async def _run_job(
    application: Application,
    job_id: int,
    *,
    lang: str | None = None,
) -> None:
    async with _RUNNING_LOCK:
        if job_id in _RUNNING_JOB_IDS:
            return
        _RUNNING_JOB_IDS.add(job_id)
    db: Database = application.bot_data["db"]
    twitch: TwitchClient = application.bot_data["twitch"]
    try:
        job = db.get_ai_clips_job(job_id)
        if not job or job.status in ("done", "failed"):
            return
        user_id = job.owner_id
        loc = lang or db.get_user_locale(user_id) or DEFAULT_LOCALE
        token = await _ensure_clips_token(db, twitch, user_id)
        if not token:
            db.update_ai_clips_job(job_id, status="failed", error="oauth")
            await application.bot.send_message(
                user_id, t("ai_clips_need_auth", loc)
            )
            await _send_oauth_prompt(application.bot, twitch, user_id, loc)
            return

        peaks = _peaks_from_json(job.peaks_json)
        if job.status in ("queued", "analyzing") or not peaks:
            db.update_ai_clips_job(job_id, status="analyzing", progress_pct=1)
            vod_duration = 0
            vod_started: datetime | None = None
            try:
                videos = await asyncio.to_thread(
                    twitch.get_videos_by_user, token["twitch_user_id"], first=20
                )
                for v in videos:
                    if str(v.get("id") or "") == job.vod_id:
                        vod_duration = parse_helix_duration(
                            str(v.get("duration") or "")
                        )
                        vod_started = _parse_rfc3339(
                            str(v.get("created_at") or "")
                        )
                        break
            except Exception:
                logger.warning(
                    "ai_clips duration lookup failed job=%s", job_id, exc_info=True
                )
            max_sec = min(
                MAX_VOD_ANALYZE_SEC,
                vod_duration if vod_duration > 0 else MAX_VOD_ANALYZE_SEC,
            )
            occupied_starts: list[int] = []
            prev_done = db.get_done_ai_clips_job(user_id, vod_id=job.vod_id)
            if prev_done:
                occupied_starts.extend(
                    _stored_clip_start(c)
                    for c in _clips_from_json(prev_done.clips_json)
                )
            try:
                since = vod_started or (
                    datetime.now(timezone.utc) - timedelta(days=7)
                )
                if vod_started is not None:
                    since = vod_started - timedelta(hours=6)
                existing_rows = await asyncio.to_thread(
                    twitch.get_clips_for_video,
                    token["twitch_user_id"],
                    job.vod_id,
                    started_at=since,
                )
                for start in helix_vod_clip_starts(existing_rows):
                    if not start_is_occupied(start, occupied_starts):
                        occupied_starts.append(start)
                if occupied_starts:
                    logger.info(
                        "ai_clips existing clips vod=%s n=%s",
                        job.vod_id,
                        len(occupied_starts),
                    )
            except Exception:
                logger.warning(
                    "ai_clips existing clips lookup failed vod=%s",
                    job.vod_id,
                    exc_info=True,
                )

            def _chat_prog(done: int, total: int) -> None:
                pct = 1
                if total > 0:
                    pct = max(1, min(55, int(done * 55 / total)))
                db.update_ai_clips_job(
                    job_id, status="analyzing", progress_pct=pct
                )

            chat_msgs = await asyncio.to_thread(
                fetch_vod_chat_messages,
                job.vod_id,
                max_seconds=max_sec,
                progress=_chat_prog,
            )
            phrases = filter_unoccupied(
                find_phrase_peaks(chat_msgs), occupied_starts
            )
            spikes = filter_unoccupied(
                find_message_spikes(chat_msgs, n=SPIKE_SEED_N), occupied_starts
            )
            audio_peaks: list[LoudPeak] = []
            db.update_ai_clips_job(job_id, status="analyzing", progress_pct=56)

            def _audio_prog(done: int, total: int) -> None:
                pct = 56
                if total > 0:
                    pct = max(56, min(85, 56 + int(done * 29 / total)))
                db.update_ai_clips_job(
                    job_id, status="analyzing", progress_pct=pct
                )

            try:
                rms = await asyncio.to_thread(
                    analyze_vod_rms,
                    job.vod_id,
                    max_seconds=max_sec,
                    progress=_audio_prog,
                )
                audio_peaks = filter_unoccupied(
                    find_loud_peaks(rms, n=AUDIO_SEED_N), occupied_starts
                )
            except Exception:
                logger.exception(
                    "ai_clips analyze failed owner=%s vod=%s",
                    user_id,
                    job.vod_id,
                )
                if not phrases and not spikes:
                    db.update_ai_clips_job(
                        job_id, status="failed", error="analyze"
                    )
                    await application.bot.send_message(
                        user_id, t("ai_clips_analyze_failed", loc)
                    )
                    return

            emotion_peaks: list[ClipCandidate] = []
            game_peaks: list[ClipCandidate] = []
            remain_audio: list[LoudPeak] = list(audio_peaks)
            remain_spikes: list[ClipCandidate] = list(spikes)
            if groq_api_ready():
                db.update_ai_clips_job(
                    job_id, status="analyzing", progress_pct=88
                )
                if remain_audio and groq_asr_ready():
                    try:
                        emotion_peaks, remain_audio = await asyncio.to_thread(
                            score_audio_peaks_with_groq,
                            job.vod_id,
                            remain_audio,
                        )
                    except Exception:
                        logger.warning(
                            "ai_clips groq emotion failed owner=%s vod=%s",
                            user_id,
                            job.vod_id,
                            exc_info=True,
                        )
                if remain_spikes:
                    # Helix VOD has no game_id; use channel last/current category.
                    stream_category = ""
                    try:
                        channel = await asyncio.to_thread(
                            twitch.get_channel, token["twitch_user_id"]
                        )
                        if channel:
                            stream_category = str(
                                channel.get("game_name") or ""
                            ).strip()
                    except Exception:
                        logger.warning(
                            "ai_clips category lookup failed vod=%s",
                            job.vod_id,
                            exc_info=True,
                        )
                    try:
                        game_peaks, remain_spikes = await asyncio.to_thread(
                            classify_spikes_with_groq,
                            chat_msgs,
                            remain_spikes,
                            category=stream_category,
                            title=job.vod_title or "",
                        )
                    except Exception:
                        logger.warning(
                            "ai_clips groq context failed owner=%s vod=%s",
                            user_id,
                            job.vod_id,
                            exc_info=True,
                        )
            # Spare slots so create can skip late-appearing Helix clips.
            peaks = merge_clip_candidates(
                phrases,
                emotion_peaks,
                game_peaks,
                remain_audio,
                remain_spikes,
                n=max(MAX_CLIPS, AUDIO_SEED_N),
                occupied_starts=occupied_starts,
            )
            if not peaks:
                db.update_ai_clips_job(job_id, status="failed", error="no_peaks")
                await application.bot.send_message(
                    user_id, t("ai_clips_no_peaks", loc)
                )
                return
            db.update_ai_clips_job(
                job_id,
                status="creating",
                peaks_json=_peaks_to_json(peaks),
                progress_pct=95,
            )

        clips = _clips_from_json(job.clips_json)
        if not clips:
            db.update_ai_clips_job(job_id, status="creating", progress_pct=95)
            create_occupied: list[int] = []
            prev_done = db.get_done_ai_clips_job(user_id, vod_id=job.vod_id)
            if prev_done:
                create_occupied.extend(
                    _stored_clip_start(c)
                    for c in _clips_from_json(prev_done.clips_json)
                )
            try:
                live_rows = await asyncio.to_thread(
                    twitch.get_clips_for_video,
                    token["twitch_user_id"],
                    job.vod_id,
                    started_at=datetime.now(timezone.utc) - timedelta(days=7),
                )
                for start in helix_vod_clip_starts(live_rows):
                    if not start_is_occupied(start, create_occupied):
                        create_occupied.append(start)
            except Exception:
                logger.warning(
                    "ai_clips refresh existing clips failed vod=%s",
                    job.vod_id,
                    exc_info=True,
                )
            clips = await asyncio.to_thread(
                _create_clips_blocking,
                twitch,
                access=token["access_token"],
                broadcaster_id=token["twitch_user_id"],
                vod_id=job.vod_id,
                vod_title=job.vod_title,
                peaks=peaks,
                lang=loc,
                occupied_starts=create_occupied,
                max_clips=MAX_CLIPS,
            )
            if not clips:
                db.update_ai_clips_job(
                    job_id, status="failed", error="create"
                )
                await application.bot.send_message(
                    user_id, t("ai_clips_create_failed", loc)
                )
                return
            db.update_ai_clips_job(
                job_id,
                status="done",
                clips_json=_clips_to_json(clips),
                progress_pct=100,
            )
        else:
            db.update_ai_clips_job(job_id, status="done", progress_pct=100)

        title = html_escape(str(job.vod_title or job.vod_id)[:80])
        lines = [t("ai_clips_done_title", loc, title=title)]
        for i, c in enumerate(clips, start=1):
            link = html_escape(c.url or c.edit_url)
            lines.append(
                t(
                    "ai_clips_done_line",
                    loc,
                    n=i,
                    offset=format_clip_timecode(_stored_clip_start(c)),
                    url=link,
                )
            )
        await application.bot.send_message(
            user_id,
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        analytics.capture(
            user_id,
            "ai_clips_created",
            {"vod_id": job.vod_id, "clips": len(clips), "job_id": job_id},
        )
    finally:
        async with _RUNNING_LOCK:
            _RUNNING_JOB_IDS.discard(job_id)


def resume_ai_clips_jobs(application: Application) -> None:
    """Re-queue unfinished jobs after bot restart."""
    db: Database = application.bot_data["db"]
    jobs = db.list_resumable_ai_clips_jobs()
    if not jobs:
        return
    logger.info("ai_clips resume %s job(s)", len(jobs))
    for job in jobs:
        asyncio.create_task(_run_job(application, job.id))


async def poll_ai_clips_auto(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every ~20m: enqueue one job for the oldest new archive per enabled user."""
    application = context.application
    db: Database = application.bot_data["db"]
    twitch: TwitchClient = application.bot_data["twitch"]
    if not ai_clips_ready():
        return
    if db.count_active_ai_clips_jobs() > 0:
        return
    prefs = db.list_enabled_ai_clips_auto()
    if not prefs:
        return
    for pref in prefs:
        if db.count_active_ai_clips_jobs() > 0:
            return
        owner_id = int(pref.owner_id)
        beta_ok, premium_ok = await _entitled(application.bot, db, owner_id)
        if not beta_ok or not premium_ok:
            db.set_ai_clips_auto_enabled(owner_id, enabled=False)
            continue
        token = await _ensure_clips_token(db, twitch, owner_id)
        if not token:
            continue
        channel_id = (pref.twitch_user_id or token["twitch_user_id"] or "").strip()
        if not channel_id:
            continue
        try:
            videos = await asyncio.to_thread(
                twitch.get_videos_by_user, channel_id, first=5
            )
        except Exception:
            logger.warning(
                "ai_clips auto list VODs failed owner=%s", owner_id, exc_info=True
            )
            continue
        archives = _archive_videos(videos)
        if not archives:
            continue
        live_stream: dict[str, Any] | None = None
        try:
            live_map = await asyncio.to_thread(
                twitch.get_live_streams, [channel_id]
            )
            live_stream = live_map.get(channel_id)
        except Exception:
            logger.warning(
                "ai_clips auto live check failed owner=%s", owner_id, exc_info=True
            )
            # Fail closed: treat as live so we never clip a still-recording VOD.
            live_stream = {"id": ""}
        ready = _archives_excluding_live_recording(archives, live_stream)
        if not ready:
            continue
        last = (pref.last_vod_id or "").strip()
        if not last:
            # Baseline to newest *ready* archive (not the live recording).
            db.update_ai_clips_auto_last_vod(
                owner_id, last_vod_id=str(ready[0]["id"])
            )
            continue
        newer: list[dict[str, Any]] = []
        for v in ready:
            if str(v["id"]) == last:
                break
            newer.append(v)
        if not newer:
            continue
        # Helix is newest-first; process oldest unseen first (one per tick).
        pick = newer[-1]
        vod_id = str(pick["id"])
        job_id = db.create_ai_clips_job(
            owner_id,
            vod_id=vod_id,
            vod_title=str(pick.get("title") or "Clip"),
        )
        db.update_ai_clips_auto_last_vod(owner_id, last_vod_id=vod_id)
        logger.info(
            "ai_clips auto enqueued owner=%s vod=%s job=%s",
            owner_id,
            vod_id,
            job_id,
        )
        asyncio.create_task(_run_job(application, job_id))
        return
