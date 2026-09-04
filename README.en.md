# Twitch → Telegram stream notifications

**Go-live, category change, upcoming, or stream end — the bot notifies wherever you choose.** Setup in Telegram.

> [!IMPORTANT]
> **Live bot:** [@twitch2telegram_bot](https://t.me/twitch2telegram_bot) — `/start` for the menu

Русский: [README.md](README.md)

| Main menu | Alert types |
|---|---|
| ![Main menu](assets/gallery/ph-gallery-01-main-menu.png) | ![Alert types](assets/gallery/ph-gallery-02-alert-types.png) |
| Custom template + placeholders |
| ![Template](assets/gallery/ph-gallery-03-templates.png) |
| Destination | Import from Twitch |
| ![Destination](assets/gallery/ph-gallery-05-destinations.png) | ![Import from Twitch](assets/gallery/ph-gallery-06-import-twitch.png) |

| Feature | How it works |
|---|---|
| Live bot | [@twitch2telegram_bot](https://t.me/twitch2telegram_bot) — `/start` for the menu |
| Languages | Russian and English — picked on first `/start`, change in **⚙️ Settings** |
| Alert types | Stream start · category change · upcoming (Twitch schedule) · stream end |
| Destinations | DM or channel/group/community (with topics) |
| Twitch channel | Link, `m.twitch.tv`, or username |
| Message template | Placeholders; examples `{username}`, `{game}`, `{name}` — [full list](https://bot.themarfa.name/placeholders?lang=en). **Clean title** — in Extras on create, and a checkbox in the template editor on edit: strips `@streamers` (only if the channel exists on Twitch) and `!commands` from `{name}` (off by default) |
| 🎲 What to watch? | In **📦 Other**: saved filters; live → else VOD; I'm feeling lucky (live → VOD for same games); button to watch new streams by filter |
| Image | Optional alert image — caption above or below; link preview then off |
| Delayed send | N minutes after go-live, category change, or going offline (Helix re-check); ⭐ on Extras |
| Repeat suppression | For stream start: skip repeats for X minutes after the first alert; ⭐ on Extras |
| Schedule reminders | If the streamer has a Twitch schedule — remind N minutes before |
| Alert history | DM only: last 7 days free, 60 days with Premium (or pay-per-feature); viewed / unviewed marks and “viewed all below” |
| Advanced options | Extras checklist for everyone: image, clean title, ignore / delay / repeat mute / delete previous (⭐ Premium), chat button (free) |
| Subscriptions | **📋 My subscriptions** in the main menu: paginated list; per sub — enable/disable, edit, delete, **Share** (🧪 beta); **🧺 Cart** and **⏸ Pause notifications** in the bottom menu; **💬 Stream chat** — Mini App with embed/fallback |
| Import from Twitch | OAuth → one-time or periodic sync; new follows only, manual subs kept |
| Stream schedule | **📅 Manage schedule** in **📦 Other**: weekly wizard or **fix slots for a day**, **Time zone** (UTC); publish to Twitch is **Premium** |
| System alerts | Toggle admin broadcasts (updates / availability / other); Twitch outages from status.twitch.com; Cursor incidents (status.cursor.com) — admins only |
| Premium | 7-day trial (alerts pause + DM notice when it ends); Stars month/year/lifetime; à la carte (advanced mode, 60-day history, 30-day cart, unlimited stream chat); Twitch channel sub (`PREMIUM_TWITCH_LOGIN`); one-time **Premium channel** for streamers (`PREMIUM_CHANNEL_STARS`) |
| Partner program | Referral link, 10% of invitees’ Stars Premium, manual withdrawal requests |
| Admin | Background broadcast with type footer; scheduled sends use MSK (UTC+3) by default, users with a saved UTC offset get local wall-clock time; stats after all UTC waves; “bot update” refreshes the main menu keyboard; DeepL, statistics, withdrawal handling, **Cancel subscription** (Stars refund by charge_id), **Demo mode** |
| Analytics | [PostHog](https://posthog.com): usage events, Error tracking, Logs (WARNING+), daily `daily_bot_stats` (03:00 UTC) |
| Commands | `/start`, `/help`, `/cancel`, `/schedule`, `/feedback`, `/settings` |
| Deploy | VPS (Docker) |

## Quick Start

1. Create a bot via [@BotFather](https://t.me/BotFather) → `TELEGRAM_BOT_TOKEN`
2. Register an app at [Twitch Developer Console](https://dev.twitch.tv/console) → `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`
3. Copy `.env.example` to `.env` and fill in values
4. Run `docker compose up -d --build`

Local run:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

## Twitch API keys

1. [Twitch Developer Console](https://dev.twitch.tv/console) → **Register Your Application**
2. **OAuth Redirect URLs** — `https://<your-service>/oauth/twitch/callback` (for follow import; locally use public HTTPS via ngrok/`PUBLIC_BASE_URL`)
3. **Client ID** → `TWITCH_CLIENT_ID`
4. **New Secret** → `TWITCH_CLIENT_SECRET`

Live checks use **Client Credentials**. Follow import uses user OAuth (`user:read:follows`).

## Usage

On first `/start` the bot asks for a language (Russian or English), then shows the welcome text and **main menu**.

### New subscription

**➕ New subscription** — pick an alert type first:

| Type | What it does |
|---|---|
| Stream start | Notify when the channel goes live |
| Category change | Watches the stream start silently; notifies on every category change until the stream ends (no repeat-suppression step; **Premium**) |
| Upcoming stream | Remind N minutes ahead if the streamer has a Twitch schedule (error if none) |
| Stream end | Notify when the stream ends (same wizard, no repeat-suppression step) |

Then the wizard (for stream start / category change / stream end):

1. Twitch channel (if an alert already exists — open editor or continue)
2. Message template — write your own with placeholders
3. **Extras** — checklist: image, clean title, ignore keywords ⭐, delayed send ⭐, repeat mute ⭐ (stream start), delete previous in channel/group ⭐, chat button; free users see ⭐ options but cannot enable them; unchecked steps are skipped
4. Image (if checked) — upload and position: start or end of caption
5. Link preview (skipped when an image is set)
6. Delay send (minutes) — if checked; after go-live / category change / offline; Helix re-checked before send
7. Repeat mute (minutes) — if checked; **stream start** only
8. Destination: DM or channel/group (one button)
9. For channel or group — add the bot and confirm the chat
10. Delete previous bot message? — if checked (category change defaults to its own alerts; if other subs for the same streamer share the destination — asks whether to delete those too)

Steps 4 / 6 / 7 / 10 only after checking Extras (and Premium for ⭐). Chat button, clean title, and image are free.

For **upcoming stream**, after the channel and schedule check — template and settings, then reminder minutes and destination (no “do you want reminders?” ask).

Each step has **Back**, **Cancel**, and **Main menu**. When editing a subscription — only those three reply buttons.

**Message template** — the wizard shows examples `{username}`, `{game}`, `{name}`. Full placeholder list (including `started_at`, `viewer_count`, `thumbnail_url`, `tags`, …): [`PUBLIC_BASE_URL/placeholders`](https://bot.themarfa.name/placeholders?lang=en) (prod: `https://bot.themarfa.name`). **Clean title**: on create — checkbox on **Extras**; on edit — in the template editor. Strips streamer mentions and commands from `{name}`: removes `@username` when that streamer exists on Twitch, and `!command`-style tokens. Off by default.


**Group or community** — send:
- group link: `https://t.me/name` (no topic — general chat)
- topic link: `https://t.me/c/name/30`
- group `@username`
- group ID (`-100…`)
- forwarded message from the group (“Forwarded from: …”)

Bot permissions in a group: **send messages** (admin is not required for alerts). Also needs permission to **delete its own messages**. During setup the bot must be an **administrator** so Telegram allows checking that you are an admin too; after binding, admin is optional if send/delete rights remain.

With “delete old” enabled, the bot removes the previous alert before a new one and also auto-deletes it after about 47 hours (Telegram’s ~48-hour limit), without waiting for the next stream.

After setup the bot sends **“✅ Setup complete!”** to DM and a test message to the chosen chat.

### What to watch?

**🎲 What to watch?** (in **📦 Other**) — random live streams matching your filters (available to everyone, no Premium). If none are live — recent VODs for the same categories.

If you have saved filters, the bot offers:

| Action | How |
|---|---|
| Pick a filter | Tap the name → get several streams |
| New search | Filter wizard from scratch |
| Delete filters | Separate button → multi-select (same pattern as subscription delete) → delete selected |

New search wizard:

1. Twitch categories (up to 5) — or **🎲 I'm feeling lucky** (IGDB random ×5 → live bot language / any; recently released ×5 → live; if empty → VOD for the same games; 18+ allowed)
2. Stream tags (optional; stream must include all listed)
3. Viewer range
4. Stream language (optional)
5. Exclude mature or allow
6. Save filter for later (up to 5) or just this once

After suggestions (or an empty result):

| Button | Action |
|---|---|
| **Watch new streams by this filter?** | Stream-start alert for the current filter (Helix by category); defaults only, delete-only |
| **Suggest again** | Another random set |
| **Filters / new search** | Pick again / wizard |

The bot polls live streams by `game_id` and notifies when a **new** matching stream appears.

### Import from Twitch

**⬇️ Import subscriptions** — Twitch OAuth, then choose **one-time import** or **sync**:

- one-time — same as before, token not stored;
- sync — period in days, refresh token stored encrypted; each run adds new follows (**enabled**) and removes unedited sync imports on unfollow; if an alert was **edited** or is **manual**, the bot asks “Delete alerts?” (Yes / No);
- on import, alerts are created **paused** (DM to self); Settings → **Sync** (change period / disable).

Twitch Console needs Redirect URL: `https://<service>/oauth/twitch/callback` (see `PUBLIC_BASE_URL`).

### Stream schedule

**📅 Manage schedule** in **📦 Other** (everyone) — choose a mode:

| Mode | What it does |
|---|---|
| **Create schedule for the week** | Weekly text wizard Monday through Sunday |
| **Fix slots for a day** | Pick one day → game → time → publish; only that day's segments are cleared on Twitch |
| **Time zone** | Set UTC offset (`UTC+3`, `UTC-5`, …); saved for Twitch publishing |

**Weekly wizard:**

1. Description and format example
2. Confirm “Create the schedule?”
3. For each day: game/stream title and time (`15:30`) — in **your** time zone
4. **No stream planned** — skip the day
5. From day 2 — **Finish** (not shown on the last day)

Result — ready-to-post text, for example:

```
- 20 Jul 15:30 Sovereign Syndicate
- 21 Jul 18:00 Just Chatting
```

Dates and month names follow the user’s language.

Optionally **publish to Twitch** (**Premium**):

1. Confirm “Publish schedule on Twitch?”
2. If no time zone is saved yet — enter UTC (example: New York `UTC-5`)
3. Slot duration: 1–4 h or “Not sure” (default 2 h)
4. OAuth / saved token with `channel:manage:schedule`
5. Existing segments are cleared before creating new ones (full clear for weekly mode; selected day only for "Fix slots for a day")
6. Segments are one-off (Partner/Affiliate) or weekly recurring (fallback); Helix gets the chosen UTC offset

### Deleted subscriptions cart

When a subscription is deleted (manually or via Twitch sync) it is saved to the cart. **📋 My subscriptions** includes **🧺 Cart**:

- If there are several alert types — pick a type first (same as view / edit / delete)
- Lists deleted subscriptions from the last **10 days** (free) or **30 days** (Premium / à la carte)
- Multi-select + **♻️ Restore selected**
- Partial restore with a message when the subscription limit is hit

### Twitch stream chat

**Chat** menu button next to the message field (set for everyone on deploy) and **📦 Other → 💬 Chat** → Mini App: live streams from active subscriptions, search by name/link, stream chat (Twitch embed + simple fallback). Free: read + up to 20 messages/day; Premium feature `stream_chat` / full plan — unlimited.

### Pause notifications

**📋 My subscriptions** includes **⏸ Pause notifications**: enter a number of days (0 turns them back on). Subscriptions stay active; the bot does not deliver stream or system messages until that date.

### Menu and commands

| Button / command | Action |
|---|---|
| `/start` | Main menu |
| `/help` | Help |
| `/cancel` | Cancel current wizard |
| `/schedule` | Manage schedule |
| `/feedback` | Feedback |
| `/settings` | Settings |
| ➕ New subscription | Alert type → wizard |
| ⬇️ Import subscriptions | OAuth → one-time or sync |
| 📋 My subscriptions | List with enable/disable, edit, delete, share; **🧺 Cart**; **⏸ Pause notifications** |
| 📜 Alert history | DM: 7 days free / 60 days Premium; 🙄/🫣 viewed marks, “viewed all below” |
| 📦 Other | Whisper alerts, schedule, what to watch, chat |
| ↳ 💬 Whisper alerts | On after Twitch OAuth; Telegram gets sender, text, conversation link |
| ↳ 📅 Manage schedule | Weekly text, time zone; Twitch sync — Premium |
| ↳ 🎲 What to watch? | Pick filter / new search / delete filters |
| ↳ 💬 Chat | Twitch stream chat Mini App |
| ⚙️ Settings | Premium, sync, ignored words, system alerts, language, partner program |
| ↳ ⭐ Premium | Stars or free via Twitch channel sub |
| ↳ 🧪 Beta mode | Opt-in for new features before public release; Premium features are free during beta |
| ↳ 🤝 Partner program | Stats, link, withdraw (≥ 500 Stars), your requests |
| ↳ 🔔 System notifications | Bot update, availability (bot / Twitch status), and sync alerts |
| ↳ 🌐 Language | Russian / English |
| ⚙️ Admin | Broadcast, stats, withdrawals, cancel subscription (refund), demo mode (`ADMIN_USER_IDS` only) |
| ↳ 📣 Broadcast | “Bot updates”, “Bot availability”, or “Other”; scheduled send (MSK default, per-user UTC offset when set); final stats after all UTC waves; footer with type and how to disable in Settings |
| ↳ 💸 Withdrawals | Partner requests: ✅ paid / ❌ reject (balance restored) |
| ↳ 📊 Statistics | Users, subscriptions, languages, paid Premium; same snapshot sent daily to PostHog (`daily_bot_stats`) |
| ↳ 🎬 Demo mode | Start from Admin: free-user menu without Premium, demo subscriptions; **Admin is hidden**, «Demo mode» stays on the main menu — press again to exit and wipe all demo data |
| ❓ Help | @immarfa or [Issues](https://github.com/Marfa/twitch-telegram-bot/issues) |

### Partner program

In **⚙️ Settings → 🤝 Partner program**:

1. **Get link** — `t.me/<bot>?start=ref_<your_id>`
2. Invitee opens the link → referrer is bound once
3. Each **Stars Premium** payment / renewal by the invitee credits **10%** Stars to the partner balance
4. **Request withdrawal** — full available balance if ≥ 500 Stars; admin gets a request with action buttons
5. **My requests** — statuses: pending / paid / rejected

Commission applies only to Stars Premium (not Twitch-sub Premium or external donations). Telegram Bot API cannot transfer Stars to users — the admin pays out manually and marks the request in the bot.

Weekly admin report: new users + Stars payers for the week.

**Edit** — same order as creation: template (**Clean title** checkbox), image, ignore / delay / repeats / delete (edit-menu items only in **advanced mode**), link preview (hidden when an image is set), schedule reminders (if enabled at creation), destination. For **stream category change** alerts with delete enabled — a separate «delete other alerts too» option. At the bottom: **Change alert type**, **Copy**, **Copy and change type** (cancel on type pick does not create a copy).

Notification template example:

```
{username} is live!
{name}
Category: {game}
```

## Deploy

### VPS (auto-deploy)

Server checkout: `/opt/twitch-telegram-bot` (with `.env` beside it).

On push to `main`, GitHub Actions SSHs in, runs `git fetch` + `reset --hard origin/main`, then `scripts/vps-deploy.sh`: build while the old bot still runs, recreate, `/health` check. If health fails, roll back to the previous image id (the workflow stays red, but the old bot answers again). Also nightly pg-backup cron, and (if `AIVEN_DATABASE_URL` is set) DR sync of that dump into Aiven at 03:15 UTC. Secrets: `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`.

Manual run: Actions → **Deploy VPS** → **Run workflow**.

VPS `.env` needs `POSTGRES_PASSWORD` (Postgres via `compose.vps.yml`), `PUBLIC_BASE_URL` for OAuth (e.g. `https://bot.themarfa.name`), and `ENABLE_PREMIUM=1`, `ENABLE_HELP=1`, `ENABLE_PARTNER=1` (off by default in source). Optional `AIVEN_DATABASE_URL` — cold Postgres mirror on Aiven (the bot does not use it).

### Local / Docker

Leave `DATABASE_URL` unset — SQLite is used (`DATABASE_PATH`, volume in `compose.yml`).

## Environment variables

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather token |
| `TWITCH_CLIENT_ID` | Twitch Client ID |
| `TWITCH_CLIENT_SECRET` | Twitch Client Secret |
| `ADMIN_USER_IDS` | Admin Telegram user IDs (comma-separated) |
| `CHECK_INTERVAL` | Twitch live poll interval, seconds (default 60) |
| `SCHEDULE_CHECK_INTERVAL` | Twitch schedule reminder poll, seconds (default 180) |
| `POSTGRES_PASSWORD` | Postgres password on VPS (`compose.vps.yml`) |
| `DATABASE_URL` | PostgreSQL. If unset — SQLite (`compose.vps.yml` sets it) |
| `AIVEN_DATABASE_URL` | VPS: nightly DR restore of the dump into Aiven (not used by the bot; usually `sslmode=require`) |
| `DATABASE_PATH` | SQLite: local `data/bot.db`, Docker `/data/bot.db` |
| `MAX_SUBSCRIPTIONS_PER_OWNER` | Subscription limit per user (default 25) |
| `ENABLE_PREMIUM` | Premium shop and gates (`0` default — all paid features free; set `1` on VPS) |
| `ENABLE_HELP` | Help button (`0` default — hidden, paid features free; set `1` on VPS) |
| `ENABLE_PARTNER` | Partner program (`0` default — off; set `1` on VPS) |
| `PREMIUM_FREE_ACTIVE_LIMIT` | Active alerts without Premium (default 5) |
| `PREMIUM_STARS_AMOUNT` | Monthly Stars subscription (default 100) |
| `PREMIUM_STARS_YEAR` | Yearly Stars one-shot (default 1000) |
| `PREMIUM_STARS_LIFETIME` | Lifetime Premium Stars (default 2000) |
| `PREMIUM_STARS_FEATURE` | Per-feature monthly Stars (default 20) |
| `PREMIUM_CHANNEL_STARS` | One-time Premium channel for streamers, Stars (default 1500) |
| `PREMIUM_TRIAL_DAYS` | Trial length in days (default 7) |
| `PREMIUM_SUBSCRIPTION_PERIOD` | Stars subscription period, seconds (default 2592000 ≈ 30 days) |
| `PREMIUM_TWITCH_LOGIN` | Twitch login for free Premium via sub (default `marfapr`) |
| `REFERRAL_COMMISSION_PERCENT` | Partner commission on Stars Premium, % (default 10) |
| `REFERRAL_WITHDRAW_MIN_STARS` | Minimum withdrawal request, Stars (default 500) |
| `PUBLIC_BASE_URL` | Public HTTPS origin: OAuth (`…/oauth/twitch/callback`) and placeholder docs (`…/placeholders`). Prod: `https://bot.themarfa.name` |
| `TOKEN_ENCRYPTION_KEY` | Optional Fernet key for refresh tokens (else derived from `TELEGRAM_BOT_TOKEN`) |
| `PORT` | Health/OAuth port (default 8080) |
| `DEEPL_API_KEY` | DeepL — auto-translate admin broadcasts to recipient language |
| `POSTHOG_API_KEY` | PostHog **Project API key** (`phc_…`). Analytics off if unset |
| `POSTHOG_HOST` | Ingestion host (default `https://us.i.posthog.com`; EU: `https://eu.i.posthog.com`) |
| `POSTHOG_ISSUE_WEBHOOK_SECRET` | Bearer secret for `POST /hooks/posthog-issues` (Issue + Inbox Report → admins) |
| `POSTHOG_API_KEY_PERSONAL` | Personal API key (`phx_…`) for Inbox reports polling. Empty = polling off |


### PostHog

Optional. Use the **Project API key** (`phc_…`) from Project settings → Project variables (not Personal `phx_` or Project secret `phs_`).

| What | Where in PostHog |
|---|---|
| `/start`, alerts, import, premium, blocks | Activity / Product analytics → Trends |
| Unhandled exceptions | Error tracking |
| New / reopened Issue | Alert → HTTP webhook → Telegram admins (RU via DeepL) |
| New Inbox Report (`$scout_report_emitted`, `outcome=surfaced`) | Webhook + poll `signals/reports/` every 5 min (also reports with no Scout event) |
| `logger.warning` / `logger.error` / `exception` | Logs (`service.name=twitch-telegram-bot`, no INFO) |
| Admin stats snapshot | `daily_bot_stats` event daily at 03:00 UTC |

`daily_bot_stats` properties: `users`, `notify_users`, `unique_owners`, `subscriptions_*`, `unique_twitch_channels`, `premium_paid`, `blocked_users`, `sys_*`, `locale_*`.

One-shot snapshot / approximate backfill: `python scripts/posthog-stats-snapshot.py [--backfill]` (on VPS inside the bot container).

## Architecture

| Module | Role |
|---|---|
| `bot.py` | App wiring (`build_application`), edit flow, handler re-exports |
| `handlers/` | Domain handlers: wizard, subscriptions, watch, broadcast, schedule, settings, … |
| `bot_helpers.py` | Shared UI helpers (menu, wizard, admin, DM) |
| `db/` | SQLite or PostgreSQL, watch filters, referrals |
| `self_check/` | Characterization checks + handler smoke (`python -m self_check`; CI: ruff F821) |
| `analytics.py` | PostHog: usage events, errors, WARNING+ Logs, daily `daily_bot_stats` |
| `locales/` + `i18n.py` | Strings (ru/en) and keyboards |
| `premium.py` / `premium_handlers.py` | Premium (Stars / Twitch), referral credits |
| `beta.py` | Beta catalog (`beta/manifest.json`), opt-in/out, runtime gate, Premium bypass |
| `demo_mode.py` | Admin Demo mode flag (free UX + wipe demo subscriptions) |
| `twitch.py` | Helix API, live discovery, templates, status.twitch.com |
| `translate.py` | DeepL for admin broadcasts |
| `links.py` | `t.me/c/…/topic` parsing |
| `health.py` | `/health`, `/placeholders`, Twitch OAuth callback, PostHog Issue/Report webhook |

Twitch Helix poll ~60 s, Statuspage (Twitch, PostHog, Cursor) ~120 s, Telegram polling; public HTTPS for OAuth / health / PostHog Issue+Report webhook.

## License

**CC BY-NC-SA 4.0** — see [LICENSE](LICENSE)

---

Built with Cursor

Support: [Donate](https://www.donationalerts.com/r/themarfa) · [Crypto](https://nowpayments.io/donation/themarfa) · [Telegram Tribute](https://t.me/tribute/app?startapp=dBlc)
