"""Channel stats card (Premium à la carte): Helix samples + TwitchTracker enrich."""
from __future__ import annotations

import io
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import escape as html_escape
from typing import Any
from urllib.request import Request, urlopen

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes

import analytics
import premium as prem
from bot_helpers import _user_lang, reply_chat_id, with_oauth_legal
from db import ChannelStatsSample, ChannelStatsTrack, Database
from i18n import DEFAULT_LOCALE, other_menu, t
from twitch import TwitchClient

logger = logging.getLogger(__name__)

FEATURE_ID = "channel_stats"
SAMPLE_INTERVAL_SEC = 600
SAMPLE_HOURS = SAMPLE_INTERVAL_SEC / 3600.0
SAMPLE_TTL_DAYS = 56
TT_CACHE_TTL_SEC = 6 * 3600
TT_API = "https://twitchtracker.com/api/channels/summary/{login}"

_tt_cache: dict[str, tuple[float, dict[str, Any]]] = {}


@dataclass
class WeekAgg:
    week_start: datetime
    viewers_sum: int = 0
    samples: int = 0
    peak: int = 0
    game_hours: dict[str, float] = field(default_factory=dict)

    @property
    def avg(self) -> float:
        return (self.viewers_sum / self.samples) if self.samples else 0.0

    @property
    def hours(self) -> float:
        return self.samples * SAMPLE_HOURS

    @property
    def top_game(self) -> str:
        if not self.game_hours:
            return ""
        return max(self.game_hours.items(), key=lambda kv: kv[1])[0]


@dataclass
class ChannelStatsSnapshot:
    login: str
    display_name: str
    profile_image_url: str
    avg_4w: float | None
    peak_all: int | None
    hours_per_week: float | None
    growth_pct: float | None
    weeks: list[WeekAgg]
    games: list[tuple[str, float]]
    tt: dict[str, Any]
    cold_start: bool
    sample_count: int


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _oauth_ready() -> bool:
    from config import twitch_oauth_redirect_uri

    return bool(twitch_oauth_redirect_uri())


def _parse_iso(raw: str) -> datetime | None:
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
    return dt.astimezone(timezone.utc)


def _week_monday(dt: datetime) -> datetime:
    d = dt.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return d - timedelta(days=d.weekday())


def aggregate_samples(
    samples: list[ChannelStatsSample],
    *,
    now: datetime | None = None,
) -> tuple[list[WeekAgg], dict[str, float], int]:
    """Return (weeks newest-last), game hours, peak."""
    now = now or datetime.now(timezone.utc)
    by_week: dict[datetime, WeekAgg] = {}
    games: dict[str, float] = defaultdict(float)
    peak = 0
    for s in samples:
        dt = _parse_iso(s.sampled_at)
        if dt is None:
            continue
        wk = _week_monday(dt)
        agg = by_week.get(wk)
        if agg is None:
            agg = WeekAgg(week_start=wk)
            by_week[wk] = agg
        vc = max(0, int(s.viewer_count))
        agg.viewers_sum += vc
        agg.samples += 1
        if vc > agg.peak:
            agg.peak = vc
        if vc > peak:
            peak = vc
        gname = (s.game_name or "").strip() or (s.game_id or "").strip() or "?"
        hours = SAMPLE_HOURS
        agg.game_hours[gname] = agg.game_hours.get(gname, 0.0) + hours
        games[gname] += hours
    weeks = sorted(by_week.values(), key=lambda w: w.week_start)
    # Drop empty future weeks; keep last 8
    weeks = [w for w in weeks if w.week_start <= _week_monday(now)][-8:]
    return weeks, dict(games), peak


def build_snapshot(
    *,
    track: ChannelStatsTrack,
    samples: list[ChannelStatsSample],
    profile_image_url: str = "",
    tt: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> ChannelStatsSnapshot:
    now = now or datetime.now(timezone.utc)
    weeks, games_map, peak = aggregate_samples(samples, now=now)
    last4 = weeks[-4:] if weeks else []
    prev4 = weeks[-8:-4] if len(weeks) >= 5 else []
    avg_4w: float | None = None
    if last4:
        tot_v = sum(w.viewers_sum for w in last4)
        tot_n = sum(w.samples for w in last4)
        avg_4w = (tot_v / tot_n) if tot_n else 0.0
    hours_per_week: float | None = None
    if last4:
        hours_per_week = sum(w.hours for w in last4) / len(last4)
    growth: float | None = None
    if last4 and prev4:
        a = sum(w.viewers_sum for w in last4) / max(1, sum(w.samples for w in last4))
        b = sum(w.viewers_sum for w in prev4) / max(1, sum(w.samples for w in prev4))
        if b > 0:
            growth = ((a - b) / b) * 100.0
    games = sorted(games_map.items(), key=lambda kv: kv[1], reverse=True)[:5]
    cold = len(samples) < 6
    return ChannelStatsSnapshot(
        login=track.twitch_login,
        display_name=track.display_name or track.twitch_login,
        profile_image_url=profile_image_url,
        avg_4w=avg_4w,
        peak_all=peak if samples else None,
        hours_per_week=hours_per_week,
        growth_pct=growth,
        weeks=weeks,
        games=games,
        tt=tt or {},
        cold_start=cold,
        sample_count=len(samples),
    )


def fetch_twitchtracker_summary(login: str) -> dict[str, Any]:
    """Public TT basic API; empty dict on miss/error. No HTML scrape."""
    key = (login or "").strip().lower()
    if not key:
        return {}
    now = time.time()
    cached = _tt_cache.get(key)
    if cached and now - cached[0] < TT_CACHE_TTL_SEC:
        return dict(cached[1])
    url = TT_API.format(login=key)
    try:
        req = Request(url, headers={"User-Agent": "twitch-telegram-bot/1.0"})
        with urlopen(req, timeout=8) as resp:  # noqa: S310 — fixed HTTPS host
            raw = resp.read().decode("utf-8", errors="replace")
        import json

        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            data = {}
    except Exception as exc:
        logger.info("twitchtracker summary failed login=%s: %s", key, type(exc).__name__)
        data = {}
    _tt_cache[key] = (now, data)
    return dict(data)


def _fmt_num(v: float | int | None, *, digits: int = 1) -> str:
    if v is None:
        return "—"
    if isinstance(v, int) or float(v).is_integer():
        return str(int(v))
    return f"{v:.{digits}f}".replace(".", ",")


def _fmt_hours(h: float | None) -> str:
    if h is None:
        return "—"
    return f"{_fmt_num(h)} ч"


def render_channel_stats_card(snap: ChannelStatsSnapshot, lang: str) -> bytes:
    """Dark Lanol-like PNG card (no market rank / best hours)."""
    from PIL import Image, ImageDraw, ImageFont

    W, H = 900, 1200
    bg = (12, 12, 16)
    card = (22, 22, 28)
    cream = (245, 240, 232)
    muted = (160, 155, 148)
    coral = (255, 70, 120)
    bar = (80, 140, 255)

    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)

    def font(size: int, bold: bool = False) -> ImageFont.ImageFont:
        for name in (
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else None,
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else None,
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ):
            if not name:
                continue
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                continue
        return ImageFont.load_default()

    title_f = font(42, bold=True)
    kpi_f = font(36, bold=True)
    label_f = font(18)
    body_f = font(22)
    small_f = font(16)

    # Avatar
    ax, ay, asize = 40, 40, 96
    draw.rounded_rectangle((ax, ay, ax + asize, ay + asize), radius=20, fill=card)
    if snap.profile_image_url:
        try:
            req = Request(
                snap.profile_image_url,
                headers={"User-Agent": "twitch-telegram-bot/1.0"},
            )
            with urlopen(req, timeout=6) as resp:  # noqa: S310
                avatar = Image.open(io.BytesIO(resp.read())).convert("RGB")
            avatar = avatar.resize((asize, asize))
            mask = Image.new("L", (asize, asize), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                (0, 0, asize, asize), radius=20, fill=255
            )
            img.paste(avatar, (ax, ay), mask)
        except Exception:
            pass

    name = snap.display_name or snap.login
    draw.text((ax + asize + 24, ay + 12), name, fill=cream, font=title_f)
    draw.text(
        (ax + asize + 24, ay + 60),
        f"twitch.tv/{snap.login}",
        fill=muted,
        font=label_f,
    )

    # KPI row
    kpi_y = 170
    boxes = [
        (
            t("channel_stats_kpi_avg", lang),
            _fmt_num(snap.avg_4w),
            t("channel_stats_kpi_avg_sub", lang),
        ),
        (
            t("channel_stats_kpi_peak", lang),
            _fmt_num(snap.peak_all, digits=0),
            t("channel_stats_kpi_peak_sub", lang),
        ),
        (
            t("channel_stats_kpi_air", lang),
            _fmt_hours(snap.hours_per_week),
            t("channel_stats_kpi_air_sub", lang),
        ),
        (
            t("channel_stats_kpi_growth", lang),
            (
                f"{_fmt_num(snap.growth_pct)} %"
                if snap.growth_pct is not None
                else "—"
            ),
            t("channel_stats_kpi_growth_sub", lang),
        ),
    ]
    # Prefer TT numbers when cold and TT has data
    if snap.cold_start and snap.tt:
        if snap.avg_4w is None and "avg_viewers" in snap.tt:
            boxes[0] = (
                boxes[0][0],
                _fmt_num(snap.tt.get("avg_viewers")),
                "TwitchTracker · 30d",
            )
        if snap.peak_all is None and "max_viewers" in snap.tt:
            boxes[1] = (
                boxes[1][0],
                _fmt_num(snap.tt.get("max_viewers"), digits=0),
                "TwitchTracker · 30d",
            )
        mins = snap.tt.get("minutes_streamed")
        if snap.hours_per_week is None and mins is not None:
            try:
                boxes[2] = (
                    boxes[2][0],
                    _fmt_hours(float(mins) / 60.0 / 4.0),
                    "TwitchTracker · 30d",
                )
            except (TypeError, ValueError):
                pass

    gap = 16
    box_w = (W - 80 - 3 * gap) // 4
    for i, (label, value, sub) in enumerate(boxes):
        x0 = 40 + i * (box_w + gap)
        draw.rounded_rectangle(
            (x0, kpi_y, x0 + box_w, kpi_y + 130), radius=16, fill=card
        )
        draw.text((x0 + 14, kpi_y + 14), label, fill=muted, font=small_f)
        draw.text((x0 + 14, kpi_y + 48), value, fill=cream, font=kpi_f)
        draw.text((x0 + 14, kpi_y + 98), sub, fill=muted, font=small_f)

    # Weekly bars
    chart_top = 340
    draw.text(
        (40, chart_top),
        t("channel_stats_weeks_title", lang, name=name),
        fill=cream,
        font=body_f,
    )
    bars_y = chart_top + 50
    bars_h = 220
    weeks = snap.weeks[-6:] if snap.weeks else []
    if weeks:
        max_avg = max((w.avg for w in weeks), default=1.0) or 1.0
        bw = (W - 80) // max(len(weeks), 1)
        for i, w in enumerate(weeks):
            x0 = 40 + i * bw + 10
            h = int((w.avg / max_avg) * (bars_h - 40))
            y1 = bars_y + bars_h - 30
            y0 = y1 - max(h, 4)
            draw.rounded_rectangle((x0, y0, x0 + bw - 24, y1), radius=8, fill=bar)
            draw.text(
                (x0, y1 + 8),
                w.week_start.strftime("%d.%m"),
                fill=muted,
                font=small_f,
            )
            draw.text(
                (x0, y0 - 22),
                _fmt_num(w.avg),
                fill=cream,
                font=small_f,
            )
    else:
        draw.text(
            (40, bars_y + 60),
            t("channel_stats_no_weeks", lang),
            fill=muted,
            font=body_f,
        )

    # Games
    gy = bars_y + bars_h + 50
    draw.text((40, gy), t("channel_stats_games_title", lang), fill=cream, font=body_f)
    gy += 40
    if snap.games:
        for gname, hours in snap.games:
            draw.rounded_rectangle((40, gy, W - 40, gy + 44), radius=12, fill=card)
            draw.text((56, gy + 10), gname[:42], fill=cream, font=body_f)
            draw.text(
                (W - 140, gy + 10),
                _fmt_hours(hours),
                fill=coral,
                font=body_f,
            )
            gy += 56
    else:
        draw.text((40, gy), t("channel_stats_no_games", lang), fill=muted, font=body_f)
        gy += 40

    footer_y = H - 70
    foot = t("channel_stats_footer", lang)
    if snap.tt:
        foot = t("channel_stats_footer_tt", lang)
    draw.text((40, footer_y), foot, fill=muted, font=small_f)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


async def _send_oauth_prompt_with_twitch(
    bot: Any, twitch: TwitchClient, user_id: int, lang: str
) -> None:
    from config import twitch_oauth_redirect_uri
    from health import create_pending_login_state

    if not _oauth_ready():
        await bot.send_message(user_id, t("channel_stats_oauth_unavailable", lang))
        return
    redirect = twitch_oauth_redirect_uri()
    state = create_pending_login_state(user_id, lang, purpose="channel_stats")
    url = twitch.build_authorize_url(
        redirect_uri=redirect,
        state=state,
        scopes="",
        force_verify=True,
    )
    await bot.send_message(
        user_id,
        with_oauth_legal(t("channel_stats_oauth_prompt", lang), lang),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        t("channel_stats_oauth_button", lang), url=url
                    )
                ]
            ]
        ),
    )


async def maybe_pause_channel_stats(
    bot: Any, db: Database, user_id: int, *, job_queue: Any = None
) -> None:
    if await prem.has_feature(bot, db, user_id, FEATURE_ID):
        return
    row = db.get_channel_stats_track(user_id)
    if not row or not row.enabled:
        return
    db.set_channel_stats_track_enabled(user_id, False)
    if job_queue is not None:
        from handlers.background_jobs import sync_optional_jobs

        sync_optional_jobs(job_queue, db)
    analytics.capture(
        user_id, "channel_stats_paused", {"reason": "unentitled"}
    )


async def _deliver_card(
    bot: Any,
    db: Database,
    twitch: TwitchClient,
    user_id: int,
    lang: str,
    track: ChannelStatsTrack,
) -> None:
    since = (datetime.now(timezone.utc) - timedelta(days=SAMPLE_TTL_DAYS)).isoformat()
    samples = db.list_channel_stats_samples(user_id, since_iso=since)
    tt = fetch_twitchtracker_summary(track.twitch_login)
    profile_url = ""
    try:
        user = twitch.get_user(track.twitch_login)
        if user:
            profile_url = str(user.get("profile_image_url") or "")
            display = str(user.get("display_name") or track.display_name)
            if display and display != track.display_name:
                db.upsert_channel_stats_track(
                    user_id,
                    enabled=track.enabled,
                    twitch_user_id=track.twitch_user_id,
                    twitch_login=track.twitch_login,
                    display_name=display,
                )
                track = db.get_channel_stats_track(user_id) or track
    except Exception as exc:
        logger.info("channel_stats profile fetch: %s", type(exc).__name__)
    snap = build_snapshot(
        track=track,
        samples=samples,
        profile_image_url=profile_url,
        tt=tt,
    )
    png = render_channel_stats_card(snap, lang)
    caption = t(
        "channel_stats_caption",
        lang,
        channel=html_escape(snap.display_name or snap.login),
        login=html_escape(snap.login),
    )
    if snap.cold_start:
        caption += "\n\n" + t("channel_stats_cold_start", lang)
    if tt:
        caption += "\n" + t("channel_stats_caption_tt", lang)
    await bot.send_photo(
        chat_id=user_id,
        photo=InputFile(io.BytesIO(png), filename="channel_stats.png"),
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=other_menu(lang),
    )
    analytics.capture(
        user_id,
        "channel_stats_card_sent",
        {"samples": snap.sample_count, "tt": bool(tt)},
    )


async def start_channel_stats(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user_id = update.effective_user.id
    lang = _user_lang(context, user_id)
    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    chat = reply_chat_id(update)

    if not await prem.has_feature(context.bot, db, user_id, FEATURE_ID):
        await maybe_pause_channel_stats(
            context.bot, db, user_id, job_queue=context.application.job_queue
        )
        from premium_handlers import send_premium_screen

        await send_premium_screen(
            context.bot,
            user_id,
            lang,
            db,
            update=update,
            context=context,
            source="channel_stats",
            feature=FEATURE_ID,
        )
        return

    track = db.get_channel_stats_track(user_id)
    if not track or not track.twitch_user_id:
        if not _oauth_ready():
            await context.bot.send_message(
                chat, t("channel_stats_oauth_unavailable", lang)
            )
            return
        await _send_oauth_prompt_with_twitch(context.bot, twitch, user_id, lang)
        return

    if not track.enabled:
        db.set_channel_stats_track_enabled(user_id, True)
        from handlers.background_jobs import sync_optional_jobs

        sync_optional_jobs(context.application.job_queue, db)
        track = db.get_channel_stats_track(user_id) or track

    await context.bot.send_message(chat, t("channel_stats_building", lang))
    try:
        await _deliver_card(context.bot, db, twitch, user_id, lang, track)
    except Exception:
        logger.exception("channel_stats card failed owner=%s", user_id)
        await context.bot.send_message(
            chat, t("channel_stats_failed", lang), reply_markup=other_menu(lang)
        )


async def complete_channel_stats_oauth(
    application: Application,
    owner_id: int,
    error: str | None,
    token_info: dict[str, str] | None,
) -> None:
    db: Database = application.bot_data["db"]
    twitch: TwitchClient = application.bot_data["twitch"]
    lang = db.get_user_locale(owner_id) or DEFAULT_LOCALE
    if error or not token_info:
        key = "oauth_denied" if error == "access_denied" else "channel_stats_failed"
        await application.bot.send_message(
            owner_id, t(key, lang), reply_markup=other_menu(lang)
        )
        return
    login = str(token_info.get("twitch_login") or "").strip().lower()
    tid = str(token_info.get("twitch_user_id") or "").strip()
    display = str(
        token_info.get("twitch_display_name") or login
    ).strip() or login
    if not login or not tid:
        await application.bot.send_message(
            owner_id, t("channel_stats_failed", lang), reply_markup=other_menu(lang)
        )
        return
    if not await prem.has_feature(application.bot, db, owner_id, FEATURE_ID):
        from premium_handlers import send_premium_screen

        await send_premium_screen(
            application.bot,
            owner_id,
            lang,
            db,
            source="channel_stats_oauth",
            feature=FEATURE_ID,
        )
        return
    # Identity-only OAuth (empty scopes) — do not fan-out refresh tokens or
    # we overwrite full BOT_TWITCH_OAUTH_SCOPES and trip twitch reauth.
    db.upsert_channel_stats_track(
        owner_id,
        enabled=True,
        twitch_user_id=tid,
        twitch_login=login,
        display_name=display,
        linked_at=_now_iso(),
    )
    from handlers.background_jobs import sync_optional_jobs

    sync_optional_jobs(application.job_queue, db)
    analytics.capture(owner_id, "channel_stats_linked", {"login": login})
    track = db.get_channel_stats_track(owner_id)
    if not track:
        await application.bot.send_message(
            owner_id, t("channel_stats_failed", lang), reply_markup=other_menu(lang)
        )
        return
    await application.bot.send_message(
        owner_id, t("channel_stats_linked", lang, channel=display)
    )
    try:
        await _deliver_card(application.bot, db, twitch, owner_id, lang, track)
    except Exception:
        logger.exception("channel_stats card after oauth owner=%s", owner_id)
        await application.bot.send_message(
            owner_id, t("channel_stats_failed", lang), reply_markup=other_menu(lang)
        )


async def poll_channel_stats(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sample live viewer_count for entitled + enabled tracks (~10 min)."""
    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    tracks = db.list_enabled_channel_stats_tracks()
    if not tracks:
        return
    active: list[ChannelStatsTrack] = []
    for tr in tracks:
        if not prem.has_feature_sync(db, tr.owner_id, FEATURE_ID):
            db.set_channel_stats_track_enabled(tr.owner_id, False)
            continue
        active.append(tr)
    if not active:
        from handlers.background_jobs import sync_optional_jobs

        sync_optional_jobs(context.application.job_queue, db)
        return
    try:
        live = twitch.get_live_streams([tr.twitch_user_id for tr in active])
    except Exception as exc:
        logger.warning("channel_stats poll helix: %s", type(exc).__name__)
        return
    now = _now_iso()
    for tr in active:
        stream = live.get(tr.twitch_user_id)
        if not stream:
            continue
        db.add_channel_stats_sample(
            tr.owner_id,
            sampled_at=now,
            viewer_count=int(stream.get("viewer_count") or 0),
            game_id=str(stream.get("game_id") or ""),
            game_name=str(stream.get("game_name") or ""),
            stream_id=str(stream.get("id") or ""),
        )
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=SAMPLE_TTL_DAYS)
    ).isoformat()
    try:
        db.prune_channel_stats_samples(cutoff)
    except Exception as exc:
        logger.info("channel_stats prune: %s", type(exc).__name__)
