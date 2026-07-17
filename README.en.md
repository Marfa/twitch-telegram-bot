# Twitch → Telegram stream notifications

**Stream goes live — the bot notifies wherever you choose.** One-minute setup in Telegram.

Live bot: [@twitch2telegram_bot](https://t.me/twitch2telegram_bot)

```bash
cp .env.example .env
docker compose up -d --build
```

| Feature | How it works |
|---|---|
| Live bot | [@twitch2telegram_bot](https://t.me/twitch2telegram_bot) — `/start` to set up |
| Destinations | DM, channel, group or community (with topics) |
| Delay after go-live | Send notification N minutes after stream start |
| Repeat suppression | Skip repeat alerts for X minutes after the first one |
| Subscriptions | List, edit, enable/disable, delete |
| System alerts | Toggle admin “bot update” broadcasts |
| Admin | Scheduled “Bot updates” broadcast, statistics |
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

## Usage

`/start` — setup wizard:

1. Twitch channel
2. Message template (`{username}`, `{game}`, `{name}`)
3. Link preview on/off
4. Delay notification after stream start (yes/no, minutes)
5. Allow repeat notifications (yes/no; if no — mute minutes)
6. Destination: DM / channel / group or community
7. For channel or group — add the bot and confirm the chat
8. Delete previous bot message on each new stream? (yes/no)

Each step has **Back**, **Cancel**, and **Main menu**. When editing a subscription — only those three reply buttons.

### Menu and commands

| Button / command | Action |
|---|---|
| `/start` | New subscription |
| `/help` | Help |
| `/cancel` | Cancel setup |
| ➕ New subscription | Add another channel |
| 📋 Manage subscriptions | List, edit, delete |
| 🔔 System notifications | Toggle bot update alerts |
| ⚙️ Admin | Scheduled broadcast, stats (admins only) |
| 🐛 Report a problem | @immarfa or [Issues](https://github.com/Marfa/twitch-telegram-bot/issues) |

Template example:

```
{username} is live!
{name}
Category: {game}
```

## Environment variables

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather token |
| `TWITCH_CLIENT_ID` | Twitch Client ID |
| `TWITCH_CLIENT_SECRET` | Twitch Client Secret |
| `CHECK_INTERVAL` | Twitch poll interval, seconds (default 60) |
| `DATABASE_URL` | PostgreSQL (Aiven). If unset — SQLite |
| `DATABASE_PATH` | SQLite path (default `data/bot.db`) |
| `PORT` | Health-check port (set by Render) |

## Architecture

| Module | Role |
|---|---|
| `bot.py` | Wizard, menu, notifications, admin broadcast |
| `twitch.py` | Helix API, templates |
| `links.py` | `t.me/c/…/topic` parsing |
| `health.py` | `/health` for Render and UptimeRobot |
| `db.py` | SQLite or PostgreSQL subscriptions |

## License

**CC BY-NC-SA 4.0** — see [LICENSE](LICENSE)

---

Built with Cursor

Support: [Telegram Tribute](https://t.me/tribute/app?startapp=dBlc) · [Crypto](https://nowpayments.io/donation/themarfa)
