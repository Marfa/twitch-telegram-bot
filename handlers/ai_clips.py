"""AI clips: chat cues + loud VOD peaks → Helix Create Clip From VOD (Premium)."""
from __future__ import annotations

import asyncio
import json
import logging
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
    CLIP_DURATION_SEC,
    MAX_CLIPS,
    MAX_VOD_ANALYZE_SEC,
    ClipCandidate,
    ClipSource,
    CreatedClip,
    LoudPeak,
    ai_clips_ready,
    analyze_vod_rms,
    clip_watch_url,
    fetch_vod_chat_messages,
    find_loud_peaks,
    find_message_spikes,
    find_phrase_peaks,
    format_clip_timecode,
    merge_clip_candidates,
    parse_helix_duration,
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
    lang: str, videos: list[dict[str, Any]], *, page: int
) -> InlineKeyboardMarkup:
    total = len(videos)
    pages = max(1, (total + VOD_PAGE_SIZE - 1) // VOD_PAGE_SIZE)
    page = max(0, min(int(page), pages - 1))
    start = page * VOD_PAGE_SIZE
    chunk = videos[start : start + VOD_PAGE_SIZE]
    rows: list[list[InlineKeyboardButton]] = []
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
            }
        )
    return out


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
    await context.bot.send_message(
        chat,
        t("ai_clips_pick_vod", lang, max_clips=MAX_CLIPS),
        parse_mode=ParseMode.HTML,
        reply_markup=_vod_pick_keyboard(lang, archives, page=0),
    )


async def on_ai_clips_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    data = (query.data or "").strip()
    await query.answer()
    chat = reply_chat_id(update)
    db: Database = context.application.bot_data["db"]

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
        videos: list[dict[str, Any]] = context.user_data.get("ai_clips_videos") or []
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
        try:
            await query.edit_message_text(
                t("ai_clips_pick_vod", lang, max_clips=MAX_CLIPS),
                parse_mode=ParseMode.HTML,
                reply_markup=_vod_pick_keyboard(lang, videos, page=page),
            )
        except BadRequest:
            pass
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
) -> list[CreatedClip]:
    out: list[CreatedClip] = []
    for i, peak in enumerate(peaks, start=1):
        title = t(
            "ai_clips_clip_title",
            lang,
            n=i,
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
        src_raw = str(item.get("source") or "audio").strip().lower()
        source: ClipSource = (
            src_raw if src_raw in ("phrase", "spike", "audio") else "audio"
        )  # type: ignore[assignment]
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
            db.update_ai_clips_job(job_id, status="analyzing")
            vod_duration = 0
            try:
                videos = await asyncio.to_thread(
                    twitch.get_videos_by_user, token["twitch_user_id"], first=20
                )
                for v in videos:
                    if str(v.get("id") or "") == job.vod_id:
                        vod_duration = parse_helix_duration(
                            str(v.get("duration") or "")
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
            chat_msgs = await asyncio.to_thread(
                fetch_vod_chat_messages, job.vod_id, max_seconds=max_sec
            )
            phrases = find_phrase_peaks(chat_msgs)
            spikes = find_message_spikes(chat_msgs, n=MAX_CLIPS)
            audio_peaks: list[LoudPeak] = []
            need_audio = len(phrases) + len(spikes) < MAX_CLIPS
            if need_audio:
                try:
                    rms = await asyncio.to_thread(
                        analyze_vod_rms, job.vod_id, max_seconds=max_sec
                    )
                    audio_peaks = find_loud_peaks(rms, n=MAX_CLIPS)
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
            peaks = merge_clip_candidates(phrases, spikes, audio_peaks, n=MAX_CLIPS)
            if not peaks:
                db.update_ai_clips_job(job_id, status="failed", error="no_peaks")
                await application.bot.send_message(
                    user_id, t("ai_clips_no_peaks", loc)
                )
                return
            db.update_ai_clips_job(
                job_id, status="creating", peaks_json=_peaks_to_json(peaks)
            )

        clips = _clips_from_json(job.clips_json)
        if not clips:
            db.update_ai_clips_job(job_id, status="creating")
            clips = await asyncio.to_thread(
                _create_clips_blocking,
                twitch,
                access=token["access_token"],
                broadcaster_id=token["twitch_user_id"],
                vod_id=job.vod_id,
                vod_title=job.vod_title,
                peaks=peaks,
                lang=loc,
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
                job_id, status="done", clips_json=_clips_to_json(clips)
            )
        else:
            db.update_ai_clips_job(job_id, status="done")

        lines = [t("ai_clips_done_title", loc, count=len(clips))]
        for i, c in enumerate(clips, start=1):
            link = html_escape(c.url or c.edit_url)
            lines.append(
                t(
                    "ai_clips_done_line",
                    loc,
                    n=i,
                    offset=format_clip_timecode(
                        max(0, c.vod_offset - CLIP_DURATION_SEC)
                    ),
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
