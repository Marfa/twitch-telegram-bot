# Twitch → Telegram stream notifications

**Stream goes live — the bot notifies wherever you choose.** One-minute setup in Telegram.

Live bot: [@twitch2telegram_bot](https://t.me/twitch2telegram_bot)

Русский: [README.md](README.md)

```bash
cp .env.example .env
docker compose up -d --build
```

| Feature | How it works |
|---|---|
| Live bot | [@twitch2telegram_bot](https://t.me/twitch2telegram_bot) — `/start` to set up |
| Languages | Russian and English — picked on first `/start`, change in **⚙️ Settings** |
| Destinations | DM, channel, group or community (with topics) |
| Twitch channel | Link, `m.twitch.tv`, or username |
| Message template | Custom text with `{username}`, `{game}`, `{name}` |
| Delay after go-live | Send notification N minutes after stream start |
| Repeat suppression | Skip repeat alerts for X minutes after the first one |
| Subscriptions | List, edit all fields, enable/disable, delete |
| Stream schedule | **📅 Create schedule** wizard — weekly text for publication |
| System alerts | Toggle admin “bot update” and “bot availability” broadcasts |
| Hosting status | Render maintenance and Aiven outage alerts (RSS) |
| Admin | Scheduled broadcast, DeepL auto-translate, statistics |
| Commands | `/start`, `/help`, `/cancel` |
| Deploy | Render Free + Aiven PostgreSQL, Fly.io, Docker |

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
2. **OAuth Redirect URLs** — `http://localhost` (required field, not used by the bot)
3. **Client ID** → `TWITCH_CLIENT_ID`
4. **New Secret** → `TWITCH_CLIENT_SECRET`

The bot uses **Client Credentials** — no user OAuth flow.

## Usage

On first `/start` the bot asks you to choose a language (Russian or English).

### New subscription

`/start` or **➕ New subscription** — setup wizard:

1. Twitch channel
2. Message template (`{username}`, `{game}`, `{name}`)
3. Link preview on/off
4. Delay notification after stream start (yes/no, minutes)
5. Allow repeat notifications (yes/no; if no — mute minutes)
6. Destination: DM / channel / group or community
7. For channel or group — add the bot and confirm the chat
8. Delete previous bot message on each new stream? (yes/no)

Each step has **Back**, **Cancel**, and **Main menu**. When editing a subscription — only those three reply buttons.

**Group or community** — send:
- topic link: `https://t.me/c/name/30`
- group `@username`
- group ID (`-100…`)
- forwarded message from the group (“Forwarded from: …”)

Bot permissions in a group: **send messages** (admin is not required).

After setup the bot sends **“✅ Setup complete!”** to DM and a test message to the chosen chat.

### Stream schedule

**📅 Create schedule** — wizard for publication text for the upcoming week (nearest Monday through Sunday):

1. Description and format example
2. Confirm “Create the schedule?”
3. For each day: game/stream title and time (`15:30`)
4. **No stream planned** — skip the day
5. From day 2 — **Finish schedule** (not shown on the last day)

Result — ready-to-post text, for example:

```
- 20 Jul 15:30 Sovereign Syndicate
- 21 Jul 18:00 Just Chatting
```

Dates and month names follow the user’s language.

### Menu and commands

| Button / command | Action |
|---|---|
| `/start` | New subscription |
| `/help` | Help |
| `/cancel` | Cancel current wizard |
| ➕ New subscription | Add another channel |
| 📋 Manage subscriptions | List, edit, delete |
| 📅 Create schedule | Weekly stream schedule text |
| ⚙️ Settings | System notifications and language |
| ↳ 🔔 System notifications | Bot update and availability alerts |
| ↳ 🌐 Language | Russian / English |
| ⚙️ Admin | Broadcast, stats (`ADMIN_USER_IDS` only) |
| ↳ 📣 Broadcast | “Bot updates” or “Bot availability”, scheduled send |
| ↳ 📊 Statistics | Users, subscriptions, languages |
| 🐛 Report a problem | @immarfa or [Issues](https://github.com/Marfa/twitch-telegram-bot/issues) |

**Edit subscription** — template, destination, delay, repeats, delete old messages, link preview.

Notification template example:

```
{username} is live!
{name}
Category: {game}
```

## Deploy

### Render Free + Aiven PostgreSQL (free tier, persistent subscriptions)

Bot on Render Free, data in external PostgreSQL on [Aiven](https://aiven.io/free-postgresql-database) (no card required).

**1. Aiven**

1. Sign up at [aiven.io](https://aiven.io)
2. **Create service** → PostgreSQL → **Free** plan
3. Copy **Service URI** (`postgres://…`)

**2. Render**

1. GitHub → [Render Blueprint](https://dashboard.render.com/) (`render.yaml`)
2. Secrets:
   - `TELEGRAM_BOT_TOKEN`
   - `TWITCH_CLIENT_ID`
   - `TWITCH_CLIENT_SECRET`
   - `DATABASE_URL` — Aiven URI
   - `ADMIN_USER_IDS` — comma-separated Telegram user IDs
   - `DEEPL_API_KEY` — optional, auto-translate admin broadcasts
3. [UptimeRobot](https://uptimerobot.com/): **HTTP(s)** → `https://YOUR-SERVICE.onrender.com/health`, interval **5 min**

Subscriptions persist in Aiven across Render restarts.

### Local / Docker

Leave `DATABASE_URL` unset — SQLite is used (`DATABASE_PATH`, volume in `compose.yml`).

### Fly.io (data on volume)

```bash
fly launch --no-deploy
fly volumes create bot_data --size 1
fly secrets set TELEGRAM_BOT_TOKEN=... TWITCH_CLIENT_ID=... TWITCH_CLIENT_SECRET=...
fly deploy
```

## Environment variables

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather token |
| `TWITCH_CLIENT_ID` | Twitch Client ID |
| `TWITCH_CLIENT_SECRET` | Twitch Client Secret |
| `ADMIN_USER_IDS` | Admin Telegram user IDs (comma-separated) |
| `CHECK_INTERVAL` | Twitch poll interval, seconds (default 60) |
| `DATABASE_URL` | PostgreSQL (Aiven). If unset — SQLite |
| `DATABASE_PATH` | SQLite path (default `data/bot.db`) |
| `MAX_SUBSCRIPTIONS_PER_OWNER` | Subscription limit per user (default 25) |
| `PORT` | Health-check port (set by Render) |
| `STATUS_CHECK_INTERVAL` | Status RSS poll interval, seconds (default 1800) |
| `RENDER_STATUS_RSS` | Render RSS (default status.render.com) |
| `AIVEN_STATUS_RSS` | Aiven RSS (default status.aiven.io) |
| `DEEPL_API_KEY` | DeepL — auto-translate admin broadcasts to recipient language |

The bot sends **Render planned maintenance** and **Aiven outage** alerts to users with availability notifications enabled.

## Architecture

| Module | Role |
|---|---|
| `bot.py` | Wizard, menu, notifications, admin broadcast, schedule |
| `i18n.py` | Strings and keyboards (ru/en) |
| `twitch.py` | Helix API, templates |
| `translate.py` | DeepL for admin broadcasts |
| `links.py` | `t.me/c/…/topic` parsing |
| `render_status.py` | Render and Aiven RSS |
| `health.py` | `/health` for Render and UptimeRobot |
| `db.py` | SQLite or PostgreSQL |

Twitch Helix poll ~60 s, Telegram polling, no public webhook.

## License

**CC BY-NC-SA 4.0** — see [LICENSE](LICENSE)

---

Built with Cursor

Support: [Telegram Tribute](https://t.me/tribute/app?startapp=dBlc) · [Crypto](https://nowpayments.io/donation/themarfa)
