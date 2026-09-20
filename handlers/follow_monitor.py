"""Follow/Unfollow channel monitor (Premium à la carte, daily Helix sync)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from html import escape as html_escape
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, ContextTypes, ConversationHandler

import analytics
import beta as beta_features
import premium as prem
from bot_helpers import _user_lang, _user_notifications_paused, reply_chat_id, with_oauth_legal
from db import Database
from i18n import DEFAULT_LOCALE, follow_monitor_keyboard, other_menu, t
from twitch import FOLLOWERS_SCOPE, TwitchClient, twitch_login_link_html

logger = logging.getLogger(__name__)

BETA_FEATURE_ID = "follow-monitor"
FEATURE_ID = "follow_monitor"
SYNC_PERIOD_DAYS = 1
PAGE_SIZE = 40
FM_SEARCH = 1
_DIGEST_MAX_LINES = 40
NEW_LIST_DAYS = 30

_LIST_KIND_CURRENT = "current"
_LIST_KIND_NEW = "new"
_LIST_KIND_NEW_UNFOLLOW = "new_unfollow"
_LIST_KIND_UNFOLLOW = "unfollow"
_LIST_KINDS = frozenset(
    {
        _LIST_KIND_CURRENT,
        _LIST_KIND_NEW,
        _LIST_KIND_NEW_UNFOLLOW,
        _LIST_KIND_UNFOLLOW,
    }
)


def _oauth_ready() -> bool:
    from config import twitch_oauth_redirect_uri

    return bool(twitch_oauth_redirect_uri())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _next_sync_iso(*, days: int = SYNC_PERIOD_DAYS) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _kb_from_row(lang: str, row: Any | None) -> InlineKeyboardMarkup:
    enabled = bool(row and row.enabled)
    return follow_monitor_keyboard(
        lang,
        enabled=enabled,
        notify_follow=bool(row and row.notify_follow) if enabled else False,
        notify_unfollow=bool(row and row.notify_unfollow) if enabled else False,
    )


async def _entitled(bot: Any, db: Database, user_id: int) -> tuple[bool, bool]:
    """Returns (beta_ok, premium_ok)."""
    beta_ok = beta_features.is_enabled(db, user_id, BETA_FEATURE_ID)
    if not beta_ok:
        return False, False
    premium_ok = await prem.has_feature(bot, db, user_id, FEATURE_ID)
    return True, premium_ok


async def maybe_stop_follow_monitor_after_beta_exit(
    bot: Any, db: Database, user_id: int, *, job_queue: Any = None
) -> None:
    """Stop monitoring when leaving beta without paid/full Premium for the feature."""
    if await prem.has_feature(bot, db, user_id, FEATURE_ID):
        return
    row = db.get_follow_monitor(user_id)
    if not row or not row.enabled:
        return
    db.set_follow_monitor_enabled(user_id, False)
    if job_queue is not None:
        from handlers.background_jobs import sync_optional_jobs

        sync_optional_jobs(job_queue, db)
    analytics.capture(
        user_id,
        "follow_monitor_toggled",
        {"enabled": False, "reason": "beta_exit"},
    )


def _format_event_date(iso: str) -> str:
    raw = (iso or "").strip()
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw[:10] if len(raw) >= 10 else raw
    return dt.astimezone(timezone.utc).strftime("%d.%m.%Y")


def _new_list_since_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=NEW_LIST_DAYS)).isoformat()


def _format_follower_line(
    login: str, display_name: str, *, at: str | None = None
) -> str:
    login = (login or "").strip().lstrip("@")
    display = (display_name or "").strip()
    linked = twitch_login_link_html(login) if login else ""
    if display and display.lower() != login.lower():
        if linked:
            base = f"• {html_escape(display)} ({linked})"
        else:
            base = f"• {html_escape(display)}"
    elif linked:
        base = f"• {linked}"
    else:
        base = f"• {html_escape(display or '?')}"
    date_s = _format_event_date(at or "")
    if date_s:
        return f"{base} — {html_escape(date_s)}"
    return base


def _chunk_lines(title: str, lines: list[str]) -> list[str]:
    if not lines:
        return []
    chunks: list[str] = []
    buf = title
    for line in lines:
        candidate = f"{buf}\n{line}" if buf else line
        if buf and len(candidate) > 3900:
            chunks.append(buf)
            buf = f"{title}\n{line}" if title else line
            if len(buf) > 3900:
                buf = buf[:3890].rstrip() + "…"
        else:
            buf = (
                candidate
                if len(candidate) <= 3900
                else candidate[:3890].rstrip() + "…"
            )
    if buf:
        chunks.append(buf)
    return chunks


def _nav_keyboard(lang: str, page: int, total: int, kind: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if total > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton(
                    "‹",
                    callback_data=f"follow_monitor:page:{kind}:{page - 1}",
                )
            )
        nav.append(
            InlineKeyboardButton(
                f"{page + 1}/{total}",
                callback_data="follow_monitor:noop",
            )
        )
        if page < total - 1:
            nav.append(
                InlineKeyboardButton(
                    "›",
                    callback_data=f"follow_monitor:page:{kind}:{page + 1}",
                )
            )
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(
                t("follow_monitor_back_screen", lang),
                callback_data="follow_monitor:back",
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


async def open_follow_monitor_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    db.upsert_user(user_id)
    beta_ok, premium_ok = await _entitled(context.bot, db, user_id)
    if not beta_ok:
        await update.effective_message.reply_text(
            t("follow_monitor_beta_required", lang),
            reply_markup=other_menu(lang),
        )
        return
    # After GA (or without Premium): screen/lists stay open; settings need Premium.
    if not premium_ok:
        await maybe_stop_follow_monitor_after_beta_exit(
            context.bot,
            db,
            user_id,
            job_queue=context.application.job_queue,
        )
    row = db.get_follow_monitor(user_id)
    await update.effective_message.reply_text(
        t("follow_monitor_screen", lang),
        parse_mode=ParseMode.HTML,
        reply_markup=_kb_from_row(lang, row),
    )


async def _send_oauth_prompt(
    bot: Any, twitch: TwitchClient, user_id: int, lang: str
) -> None:
    from config import twitch_oauth_redirect_uri
    from health import create_pending_login_state

    if not _oauth_ready():
        await bot.send_message(user_id, t("follow_monitor_oauth_unavailable", lang))
        return
    redirect = twitch_oauth_redirect_uri()
    state = create_pending_login_state(user_id, lang, purpose="follow_monitor")
    # force_verify: cached Twitch consent without moderator:read:followers must re-prompt.
    url = twitch.build_authorize_url(
        redirect_uri=redirect,
        state=state,
        force_verify=True,
    )
    await bot.send_message(
        user_id,
        with_oauth_legal(t("follow_monitor_oauth_prompt", lang), lang),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        t("follow_monitor_oauth_button", lang), url=url
                    )
                ]
            ]
        ),
    )


def _access_has_followers_scope(twitch: TwitchClient, access: str) -> bool:
    return bool(access) and twitch.token_has_scope(access, FOLLOWERS_SCOPE)


async def on_follow_monitor_toggle(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    db.upsert_user(user_id)
    beta_ok, premium_ok = await _entitled(context.bot, db, user_id)
    if not beta_ok or not premium_ok:
        await query.answer()
        if not beta_ok:
            await context.bot.send_message(
                reply_chat_id(update),
                t("follow_monitor_beta_required", lang),
            )
        else:
            from premium_handlers import send_premium_screen

            await send_premium_screen(
                context.bot,
                user_id,
                lang,
                db,
                update=update,
                context=context,
                source="follow_monitor",
                feature=FEATURE_ID,
            )
        return

    row = db.get_follow_monitor(user_id)
    currently_on = bool(row and row.enabled)
    if currently_on:
        await query.answer()
        db.set_follow_monitor_enabled(user_id, False)
        analytics.capture(user_id, "follow_monitor_toggled", {"enabled": False})
        from handlers.background_jobs import sync_optional_jobs

        sync_optional_jobs(context.application.job_queue, db)
        await query.edit_message_reply_markup(
            reply_markup=_kb_from_row(lang, db.get_follow_monitor(user_id))
        )
        return

    if not _oauth_ready():
        await query.answer()
        await context.bot.send_message(
            reply_chat_id(update), t("follow_monitor_oauth_unavailable", lang)
        )
        return
    if not row or not row.refresh_token or row.needs_reauth:
        await query.answer()
        await _send_oauth_prompt(context.bot, twitch, user_id, lang)
        return
    try:
        token_data = twitch.refresh_user_token(row.refresh_token)
        access = token_data.get("access_token") or ""
        refresh = token_data.get("refresh_token") or row.refresh_token
        if not _access_has_followers_scope(twitch, access):
            db.set_follow_monitor_needs_reauth(user_id, True)
            if refresh and refresh != row.refresh_token:
                db.upsert_follow_monitor(
                    user_id,
                    enabled=False,
                    twitch_user_id=row.twitch_user_id,
                    twitch_login=row.twitch_login,
                    refresh_token=refresh,
                    needs_reauth=True,
                )
            await query.answer()
            await _send_oauth_prompt(context.bot, twitch, user_id, lang)
            return
        user = twitch.get_token_user(access) or {}
        twitch_user_id = str(user.get("id") or row.twitch_user_id)
        twitch_login = str(user.get("login") or row.twitch_login)
        now = _now_iso()
        db.upsert_follow_monitor(
            user_id,
            enabled=True,
            twitch_user_id=twitch_user_id,
            twitch_login=twitch_login,
            refresh_token=refresh,
            next_sync_at=now,
            needs_reauth=False,
        )
    except Exception as exc:
        logger.warning("follow_monitor enable failed: %s", type(exc).__name__)
        db.set_follow_monitor_needs_reauth(user_id, True)
        await query.answer()
        await _send_oauth_prompt(context.bot, twitch, user_id, lang)
        return
    await query.answer()
    analytics.capture(user_id, "follow_monitor_toggled", {"enabled": True})
    from handlers.background_jobs import sync_optional_jobs

    sync_optional_jobs(context.application.job_queue, db)
    await query.edit_message_reply_markup(
        reply_markup=_kb_from_row(lang, db.get_follow_monitor(user_id))
    )
    await context.bot.send_message(
        reply_chat_id(update), t("follow_monitor_enabled", lang)
    )
    asyncio.create_task(_sync_owner(context.application, user_id))


async def on_follow_monitor_notify_toggle(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    beta_ok, premium_ok = await _entitled(context.bot, db, user_id)
    if not beta_ok or not premium_ok:
        await query.answer()
        return
    parts = (query.data or "").split(":")
    # follow_monitor:notify:follow|unfollow
    kind = parts[2] if len(parts) >= 3 else ""
    row = db.get_follow_monitor(user_id)
    if not row or not row.enabled:
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=_kb_from_row(lang, row))
        return
    if kind == "follow":
        db.set_follow_monitor_notify(user_id, notify_follow=not row.notify_follow)
    elif kind == "unfollow":
        db.set_follow_monitor_notify(user_id, notify_unfollow=not row.notify_unfollow)
    else:
        await query.answer()
        return
    await query.answer()
    await query.edit_message_reply_markup(
        reply_markup=_kb_from_row(lang, db.get_follow_monitor(user_id))
    )


async def complete_follow_monitor_oauth(
    application: Application,
    owner_id: int,
    error: str | None,
    token_info: dict[str, str] | None,
) -> None:
    db: Database = application.bot_data["db"]
    twitch: TwitchClient = application.bot_data["twitch"]
    lang = db.get_user_locale(owner_id) or DEFAULT_LOCALE
    if error:
        key = (
            "oauth_denied"
            if error == "access_denied"
            else "follow_monitor_failed"
        )
        await application.bot.send_message(
            owner_id,
            t(key, lang),
            reply_markup=other_menu(lang),
        )
        return
    info = token_info or {}
    refresh = info.get("refresh_token") or ""
    access = info.get("access_token") or ""
    twitch_user_id = info.get("twitch_user_id") or ""
    twitch_login = info.get("twitch_login") or ""
    if not refresh or not twitch_user_id:
        await application.bot.send_message(
            owner_id,
            t("follow_monitor_failed", lang),
            reply_markup=other_menu(lang),
        )
        return
    from oauth_tokens import apply_oauth_success_tokens, restore_after_twitch_oauth

    apply_oauth_success_tokens(db, owner_id, info)
    await restore_after_twitch_oauth(application, owner_id)
    if not _access_has_followers_scope(twitch, access):
        # Consent without the followers scope (stale authorize) — ask again.
        if refresh:
            db.upsert_follow_monitor(
                owner_id,
                enabled=False,
                twitch_user_id=twitch_user_id,
                twitch_login=twitch_login,
                refresh_token=refresh,
                needs_reauth=True,
            )
        await _send_oauth_prompt(application.bot, twitch, owner_id, lang)
        return
    now = _now_iso()
    db.upsert_follow_monitor(
        owner_id,
        enabled=True,
        twitch_user_id=twitch_user_id,
        twitch_login=twitch_login,
        refresh_token=refresh,
        next_sync_at=now,
        needs_reauth=False,
    )
    analytics.capture(owner_id, "follow_monitor_toggled", {"enabled": True})
    from handlers.background_jobs import sync_optional_jobs

    sync_optional_jobs(application.job_queue, db)
    await application.bot.send_message(
        owner_id,
        t("follow_monitor_enabled", lang),
        reply_markup=other_menu(lang),
    )
    asyncio.create_task(_sync_owner(application, owner_id))


def _followers_from_helix(
    rows: list[dict[str, Any]],
) -> list[tuple[str, str, str, str]]:
    out: list[tuple[str, str, str, str]] = []
    for row in rows:
        tid = str(row.get("user_id") or "").strip()
        if not tid:
            continue
        login = str(row.get("user_login") or "").strip()
        display = str(row.get("user_name") or login).strip()
        followed = str(row.get("followed_at") or "").strip()
        out.append((tid, login, display, followed))
    return out


def _sync_owner_blocking(
    db: Database, twitch: TwitchClient, mon: Any
) -> tuple[list[tuple[str, str, str, str]], bool]:
    """Returns (events, ok). events: (type, tid, login, display). ok=False → reauth."""
    token_data = twitch.refresh_user_token(mon.refresh_token)
    access = token_data.get("access_token") or ""
    refresh = token_data.get("refresh_token") or mon.refresh_token
    if not access or not twitch.token_has_scope(access, FOLLOWERS_SCOPE):
        db.set_follow_monitor_needs_reauth(mon.owner_id, True)
        return [], False
    helix = twitch.get_channel_followers(access, mon.twitch_user_id)
    fresh = _followers_from_helix(helix)
    fresh_map = {
        tid: (login, display, followed) for tid, login, display, followed in fresh
    }
    now = _now_iso()
    next_at = _next_sync_iso()
    if not mon.baseline_done:
        db.replace_follow_monitor_followers(mon.owner_id, fresh)
        db.update_follow_monitor_sync(
            mon.owner_id,
            last_sync_at=now,
            next_sync_at=next_at,
            refresh_token=refresh,
            baseline_done=True,
            needs_reauth=False,
        )
        return [], True

    old_rows = db.list_follow_monitor_followers(mon.owner_id, limit=500_000, offset=0)
    old_map = {
        r.twitch_user_id: (r.login, r.display_name, r.followed_at) for r in old_rows
    }
    events: list[tuple[str, str, str, str]] = []
    for tid, (login, display, _followed) in fresh_map.items():
        if tid not in old_map:
            events.append(("follow", tid, login, display))
    for tid, (login, display, _followed) in old_map.items():
        if tid not in fresh_map:
            events.append(("unfollow", tid, login, display))
    db.replace_follow_monitor_followers(mon.owner_id, fresh)
    if events:
        db.add_follow_monitor_events(mon.owner_id, events, detected_at=now)
    db.update_follow_monitor_sync(
        mon.owner_id,
        last_sync_at=now,
        next_sync_at=next_at,
        refresh_token=refresh,
        baseline_done=True,
        needs_reauth=False,
    )
    return events, True


def _build_digest_text(
    lang: str,
    events: list[tuple[str, str, str, str]],
    *,
    notify_follow: bool,
    notify_unfollow: bool,
) -> str | None:
    follows = [e for e in events if e[0] == "follow"] if notify_follow else []
    unfollows = [e for e in events if e[0] == "unfollow"] if notify_unfollow else []
    if not follows and not unfollows:
        return None
    parts: list[str] = [t("follow_monitor_digest_title", lang)]
    if follows:
        parts.append(
            t("follow_monitor_digest_follows", lang, count=len(follows))
        )
        shown = follows[:_DIGEST_MAX_LINES]
        parts.extend(_format_follower_line(e[2], e[3]) for e in shown)
        if len(follows) > _DIGEST_MAX_LINES:
            parts.append(
                t(
                    "follow_monitor_digest_more",
                    lang,
                    count=len(follows) - _DIGEST_MAX_LINES,
                )
            )
    if unfollows:
        parts.append(
            t("follow_monitor_digest_unfollows", lang, count=len(unfollows))
        )
        shown = unfollows[:_DIGEST_MAX_LINES]
        parts.extend(_format_follower_line(e[2], e[3]) for e in shown)
        if len(unfollows) > _DIGEST_MAX_LINES:
            parts.append(
                t(
                    "follow_monitor_digest_more",
                    lang,
                    count=len(unfollows) - _DIGEST_MAX_LINES,
                )
            )
    text = "\n".join(parts)
    if len(text) > 4000:
        text = text[:3990].rstrip() + "…"
    return text


async def _sync_owner(application: Application, owner_id: int) -> None:
    db: Database = application.bot_data["db"]
    twitch: TwitchClient = application.bot_data["twitch"]
    mon = db.get_follow_monitor(owner_id)
    if not mon or not mon.enabled:
        return
    if not mon.refresh_token:
        from oauth_tokens import request_twitch_reauth

        await request_twitch_reauth(
            application, owner_id, reason="follow_monitor_empty_token"
        )
        return
    if not beta_features.is_enabled(db, owner_id, BETA_FEATURE_ID):
        db.set_follow_monitor_enabled(owner_id, False)
        return
    if not prem.has_feature_sync(db, owner_id, FEATURE_ID):
        db.set_follow_monitor_enabled(owner_id, False)
        return
    try:
        events, ok = await asyncio.to_thread(_sync_owner_blocking, db, twitch, mon)
    except Exception as exc:
        logger.warning(
            "follow_monitor sync failed owner=%s: %s",
            owner_id,
            type(exc).__name__,
        )
        from oauth_tokens import request_twitch_reauth

        await request_twitch_reauth(
            application, owner_id, reason="follow_monitor_exc"
        )
        return
    if not ok:
        from oauth_tokens import request_twitch_reauth

        await request_twitch_reauth(
            application, owner_id, reason="follow_monitor_scope"
        )
        return
    new_n = sum(1 for e in events if e[0] == "follow")
    un_n = sum(1 for e in events if e[0] == "unfollow")
    if new_n or un_n:
        analytics.capture(
            owner_id,
            "follow_monitor_synced",
            {"new_follows": new_n, "unfollows": un_n},
        )
    mon = db.get_follow_monitor(owner_id) or mon
    digest = _build_digest_text(
        db.get_user_locale(owner_id) or DEFAULT_LOCALE,
        events,
        notify_follow=bool(mon.notify_follow),
        notify_unfollow=bool(mon.notify_unfollow),
    )
    if not digest:
        return
    if db.is_bot_blocked(owner_id) or _user_notifications_paused(db, owner_id):
        return
    try:
        await application.bot.send_message(
            owner_id, digest, parse_mode=ParseMode.HTML
        )
    except Exception as exc:
        logger.warning(
            "follow_monitor digest send failed owner=%s: %s",
            owner_id,
            type(exc).__name__,
        )


async def sync_follow_monitors(context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    now = _now_iso()
    due = db.get_due_follow_monitors(now)
    for mon in due:
        await _sync_owner(context.application, mon.owner_id)


def _load_list_pages(
    db: Database, user_id: int, lang: str, kind: str
) -> list[str]:
    if kind == _LIST_KIND_CURRENT:
        total = db.count_follow_monitor_followers(user_id)
        title = t("follow_monitor_list_follow_title", lang, count=total)
        rows = db.list_follow_monitor_followers(
            user_id, limit=min(total, 5000) or 1, offset=0
        )
        lines = [
            _format_follower_line(r.login, r.display_name)
            for r in rows
        ]
    elif kind in (_LIST_KIND_NEW, _LIST_KIND_NEW_UNFOLLOW):
        event_type = "follow" if kind == _LIST_KIND_NEW else "unfollow"
        since = _new_list_since_iso()
        total = db.count_follow_monitor_events(
            user_id, event_type=event_type, since=since
        )
        title_key = (
            "follow_monitor_list_new_title"
            if kind == _LIST_KIND_NEW
            else "follow_monitor_list_new_unfollow_title"
        )
        title = t(title_key, lang, count=total)
        events = db.list_follow_monitor_events(
            user_id,
            event_type=event_type,
            since=since,
            limit=min(total, 2000) or 1,
            offset=0,
        )
        followed_at_by_id: dict[str, str] = {}
        if kind == _LIST_KIND_NEW and events:
            # Helix followed_at for still-active followers (= true subscription date).
            want = {e.twitch_user_id for e in events}
            for row in db.list_follow_monitor_followers(
                user_id, limit=500_000, offset=0
            ):
                if row.twitch_user_id in want and row.followed_at:
                    followed_at_by_id[row.twitch_user_id] = row.followed_at
        lines = [
            _format_follower_line(
                e.login,
                e.display_name,
                at=followed_at_by_id.get(e.twitch_user_id) or e.detected_at,
            )
            for e in events
        ]
    else:
        total = db.count_follow_monitor_events(user_id, event_type="unfollow")
        title = t("follow_monitor_list_unfollow_title", lang, count=total)
        events = db.list_follow_monitor_events(
            user_id, event_type="unfollow", limit=min(total, 2000) or 1, offset=0
        )
        lines = [_format_follower_line(e.login, e.display_name) for e in events]
    if not lines:
        return []
    # Split into PAGE_SIZE line pages for nav.
    pages: list[str] = []
    for i in range(0, len(lines), PAGE_SIZE):
        chunk_lines = lines[i : i + PAGE_SIZE]
        pages.extend(_chunk_lines(title, chunk_lines))
    return pages or _chunk_lines(title, lines)


async def _show_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    kind: str,
    *,
    edit: bool = False,
) -> None:
    query = update.callback_query
    user_id = (query.from_user if query else update.effective_user).id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    beta_ok, _premium_ok = await _entitled(context.bot, db, user_id)
    if not beta_ok:
        if query:
            await query.answer()
        return
    pages = _load_list_pages(db, user_id, lang, kind)
    context.user_data["fm_pages"] = pages
    context.user_data["fm_kind"] = kind
    if not pages:
        empty_key = {
            _LIST_KIND_CURRENT: "follow_monitor_list_follow_empty",
            _LIST_KIND_NEW: "follow_monitor_list_new_empty",
            _LIST_KIND_NEW_UNFOLLOW: "follow_monitor_list_new_unfollow_empty",
            _LIST_KIND_UNFOLLOW: "follow_monitor_list_unfollow_empty",
        }[kind]
        text = t(empty_key, lang)
        kb = _nav_keyboard(lang, 0, 1, kind)
        if edit and query:
            await query.answer()
            try:
                await query.edit_message_text(text, reply_markup=kb)
            except BadRequest:
                pass
            return
        if query:
            await query.answer()
        await context.bot.send_message(
            reply_chat_id(update), text, reply_markup=kb
        )
        return
    kb = _nav_keyboard(lang, 0, len(pages), kind)
    if edit and query:
        await query.answer()
        try:
            await query.edit_message_text(
                pages[0],
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise
        return
    if query:
        await query.answer()
    await context.bot.send_message(
        reply_chat_id(update),
        pages[0],
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


async def on_follow_monitor_list(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    data = (query.data or "").split(":")
    # follow_monitor:list:current|new|new_unfollow|unfollow
    kind = data[2] if len(data) >= 3 else _LIST_KIND_CURRENT
    if kind not in _LIST_KINDS:
        await query.answer()
        return
    await _show_list(update, context, kind, edit=True)


async def on_follow_monitor_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    parts = (query.data or "").split(":")
    # follow_monitor:page:kind:n
    if len(parts) < 4:
        return
    kind = parts[2]
    try:
        page = int(parts[3])
    except ValueError:
        return
    pages = context.user_data.get("fm_pages")
    if not isinstance(pages, list) or not pages or context.user_data.get("fm_kind") != kind:
        db: Database = context.application.bot_data["db"]
        pages = _load_list_pages(db, user_id, lang, kind)
        context.user_data["fm_pages"] = pages
        context.user_data["fm_kind"] = kind
    if not pages:
        return
    page = max(0, min(page, len(pages) - 1))
    kb = _nav_keyboard(lang, page, len(pages), kind)
    try:
        await query.edit_message_text(
            pages[page],
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


async def on_follow_monitor_noop(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await update.callback_query.answer()


async def on_follow_monitor_back(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    row = db.get_follow_monitor(user_id)
    await query.answer()
    context.user_data.pop("fm_pages", None)
    context.user_data.pop("fm_kind", None)
    try:
        await query.edit_message_text(
            t("follow_monitor_screen", lang),
            parse_mode=ParseMode.HTML,
            reply_markup=_kb_from_row(lang, row),
        )
    except BadRequest:
        await context.bot.send_message(
            reply_chat_id(update),
            t("follow_monitor_screen", lang),
            parse_mode=ParseMode.HTML,
            reply_markup=_kb_from_row(lang, row),
        )


async def start_follow_monitor_search(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    user_id = query.from_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    beta_ok, _premium_ok = await _entitled(context.bot, db, user_id)
    await query.answer()
    if not beta_ok:
        return ConversationHandler.END
    await context.bot.send_message(
        reply_chat_id(update),
        t("follow_monitor_search_prompt", lang),
    )
    return FM_SEARCH


async def receive_follow_monitor_search(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    beta_ok, _premium_ok = await _entitled(context.bot, db, user_id)
    if not beta_ok:
        return ConversationHandler.END
    query = (update.effective_message.text or "").strip()
    if not query:
        await update.effective_message.reply_text(
            t("follow_monitor_search_prompt", lang)
        )
        return FM_SEARCH
    hits = db.search_follow_monitor(user_id, query, limit=50)
    if not hits:
        row = db.get_follow_monitor(user_id)
        await update.effective_message.reply_text(
            t("follow_monitor_search_empty", lang, query=query),
            reply_markup=_kb_from_row(lang, row),
        )
        return ConversationHandler.END
    source_labels = {
        "current": t("follow_monitor_source_current", lang),
        "follow": t("follow_monitor_source_new", lang),
        "unfollow": t("follow_monitor_source_unfollow", lang),
    }
    lines = [
        t("follow_monitor_search_title", lang, query=html_escape(query), count=len(hits))
    ]
    for h in hits:
        src = source_labels.get(h["source"], h["source"])
        lines.append(
            f"{_format_follower_line(h['login'], h['display_name'])} — {html_escape(src)}"
        )
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990].rstrip() + "…"
    row = db.get_follow_monitor(user_id)
    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=_kb_from_row(lang, row),
    )
    return ConversationHandler.END


async def cancel_follow_monitor_search(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    row = db.get_follow_monitor(user_id)
    await update.effective_message.reply_text(
        t("cancelled", lang),
        reply_markup=_kb_from_row(lang, row),
    )
    return ConversationHandler.END
