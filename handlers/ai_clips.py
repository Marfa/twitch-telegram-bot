"""AI clips: loud VOD peaks → Helix Create Clip From VOD (Premium)."""
from __future__ import annotations

import asyncio
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
    CreatedClip,
    ai_clips_ready,
    analyze_vod_rms,
    clip_watch_url,
    end_job,
    find_loud_peaks,
    parse_helix_duration,
    try_begin_job,
)
from bot_helpers import _user_lang, reply_chat_id, with_oauth_legal
from db import Database
from i18n import DEFAULT_LOCALE, other_menu, t
from twitch import CLIPS_MANAGE_SCOPE, TwitchClient

logger = logging.getLogger(__name__)

BETA_FEATURE_ID = "ai-clips"
FEATURE_ID = "ai_clips"
_VOD_LIST_FIRST = 8


def _oauth_ready() -> bool:
    from config import twitch_oauth_redirect_uri

    return bool(twitch_oauth_redirect_uri())


async def _entitled(bot: Any, db: Database, user_id: int) -> tuple[bool, bool]:
    beta_ok = beta_features.is_enabled(db, user_id, BETA_FEATURE_ID)
    if not beta_ok:
        return False, False
    premium_ok = await prem.has_feature(bot, db, user_id, FEATURE_ID)
    return True, premium_ok


def _vod_pick_keyboard(lang: str, videos: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for v in videos:
        vid = str(v.get("id") or "").strip()
        if not vid:
            continue
        title = str(v.get("title") or vid).strip() or vid
        dur = str(v.get("duration") or "").strip()
        label = f"{title[:40]} ({dur})" if dur else title[:48]
        rows.append(
            [InlineKeyboardButton(label, callback_data=f"ai_clips:vod:{vid}")]
        )
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
        "twitch_login": "",
    }


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
            first=_VOD_LIST_FIRST,
        )
    except Exception:
        logger.exception("ai_clips list VODs failed owner=%s", user_id)
        await context.bot.send_message(
            chat,
            t("ai_clips_list_failed", lang),
            reply_markup=other_menu(lang),
        )
        return

    archives = [
        v
        for v in videos
        if str(v.get("id") or "").isdigit()
        and parse_helix_duration(str(v.get("duration") or "")) >= CLIP_DURATION_SEC
    ]
    if not archives:
        await context.bot.send_message(
            chat,
            t("ai_clips_no_vods", lang),
            reply_markup=other_menu(lang),
        )
        return

    context.user_data["ai_clips_token"] = token
    context.user_data["ai_clips_videos"] = {
        str(v["id"]): v for v in archives if v.get("id")
    }
    await context.bot.send_message(
        chat,
        t("ai_clips_pick_vod", lang, max_clips=MAX_CLIPS),
        parse_mode=ParseMode.HTML,
        reply_markup=_vod_pick_keyboard(lang, archives),
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

    if data == "ai_clips:cancel":
        context.user_data.pop("ai_clips_token", None)
        context.user_data.pop("ai_clips_videos", None)
        try:
            await query.edit_message_text(t("ai_clips_canceled", lang))
        except BadRequest:
            pass
        await context.bot.send_message(
            chat, t("menu_other", lang), reply_markup=other_menu(lang)
        )
        return

    if not data.startswith("ai_clips:vod:"):
        return

    vod_id = data.split(":", 2)[-1].strip()
    videos: dict[str, Any] = context.user_data.get("ai_clips_videos") or {}
    token: dict[str, str] | None = context.user_data.get("ai_clips_token")
    video = videos.get(vod_id)
    db: Database = context.application.bot_data["db"]
    beta_ok, premium_ok = await _entitled(context.bot, db, user_id)
    if not beta_ok or not premium_ok or not video or not token:
        try:
            await query.edit_message_text(t("ai_clips_failed", lang))
        except BadRequest:
            pass
        await context.bot.send_message(
            chat, t("menu_other", lang), reply_markup=other_menu(lang)
        )
        return

    if not try_begin_job(user_id):
        try:
            await query.edit_message_text(t("ai_clips_busy", lang))
        except BadRequest:
            pass
        await context.bot.send_message(
            chat, t("menu_other", lang), reply_markup=other_menu(lang)
        )
        return

    title = html_escape(str(video.get("title") or vod_id)[:80])
    try:
        await query.edit_message_text(
            t("ai_clips_working", lang, title=title),
            parse_mode=ParseMode.HTML,
        )
    except BadRequest:
        pass

    context.user_data.pop("ai_clips_token", None)
    context.user_data.pop("ai_clips_videos", None)
    asyncio.create_task(
        _run_job(
            context.application,
            user_id,
            lang,
            token=token,
            vod_id=vod_id,
            vod_title=str(video.get("title") or "Clip"),
            vod_duration=parse_helix_duration(str(video.get("duration") or "")),
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
    peaks: list,
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


async def _run_job(
    application: Application,
    user_id: int,
    lang: str,
    *,
    token: dict[str, str],
    vod_id: str,
    vod_title: str,
    vod_duration: int,
) -> None:
    twitch: TwitchClient = application.bot_data["twitch"]
    try:
        max_sec = min(
            MAX_VOD_ANALYZE_SEC,
            vod_duration if vod_duration > 0 else MAX_VOD_ANALYZE_SEC,
        )
        try:
            rms = await asyncio.to_thread(
                analyze_vod_rms, vod_id, max_seconds=max_sec
            )
        except Exception:
            logger.exception(
                "ai_clips analyze failed owner=%s vod=%s", user_id, vod_id
            )
            await application.bot.send_message(
                user_id, t("ai_clips_analyze_failed", lang)
            )
            return
        peaks = find_loud_peaks(rms, n=MAX_CLIPS)
        if not peaks:
            await application.bot.send_message(
                user_id, t("ai_clips_no_peaks", lang)
            )
            return
        clips = await asyncio.to_thread(
            _create_clips_blocking,
            twitch,
            access=token["access_token"],
            broadcaster_id=token["twitch_user_id"],
            vod_id=vod_id,
            vod_title=vod_title,
            peaks=peaks,
            lang=lang,
        )
        if not clips:
            await application.bot.send_message(
                user_id, t("ai_clips_create_failed", lang)
            )
            return
        lines = [t("ai_clips_done_title", lang, count=len(clips))]
        for i, c in enumerate(clips, start=1):
            link = html_escape(c.url or c.edit_url)
            lines.append(
                t(
                    "ai_clips_done_line",
                    lang,
                    n=i,
                    offset=max(0, c.vod_offset - CLIP_DURATION_SEC),
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
            {"vod_id": vod_id, "clips": len(clips)},
        )
    finally:
        end_job(user_id)
