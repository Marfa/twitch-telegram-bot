# Twitch → Telegram stream notifications

**Stream goes live — the bot notifies wherever you choose.** One-minute setup in Telegram.

```bash
cp .env.example .env
docker compose up -d --build
```

| Feature | How it works |
|---|---|
| Destinations | DM, channel, group or community (with topics) |
| Twitch channel | URL, `m.twitch.tv`, or username |
| Message text | Custom template: `{username}`, `{game}`, `{name}` |
| Subscriptions | List, enable/disable, delete |
| Commands | `/start`, `/help`, `/cancel` |
| Deploy | Render + UptimeRobot, Fly.io, Docker |

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

1. Open [Twitch Developer Console](https://dev.twitch.tv/console) → **Register Your Application**
2. **OAuth Redirect URLs** — `http://localhost` (not used by this bot, but required by the form)
3. Copy **Client ID** → `TWITCH_CLIENT_ID`
4. Click **New Secret** → `TWITCH_CLIENT_SECRET`

The bot uses **Client Credentials** only — no user OAuth.

## Usage

`/start` — setup wizard:

1. Twitch channel
2. Message template (`{username}`, `{game}`, `{name}`)
3. Destination: DM / channel / group or community
4. For channel or group — add the bot and confirm the chat
5. Delete previous bot message on each new stream? (yes/no)

**Group or community** — send one of:
- topic link: `https://t.me/c/name/30`
- group `@username`
- group ID (`-100…`)
- a message forwarded from the group (“Forwarded from: …”)

Bot permissions in groups: **send messages** (admin not required).

After setup you get **“✅ Setup complete!”** in DM plus a test message in the target chat.

### Menu and commands

| Button / command | Action |
|---|---|
| `/start` | New subscription |
| `/help` | Help |
| `/cancel` | Cancel setup |
| ➕ New subscription | Add another channel |
| 📋 My subscriptions | List, toggle on/off |
| 🗑 Delete subscription | Remove |
| 🐛 Report a problem | @immarfa or [Issues](https://github.com/Marfa/twitch-telegram-bot/issues) |

Template example:

```
{username} is live!
{name}
Category: {game}
```

## Deploy

### Render + UptimeRobot (free)

1. Push to GitHub → [Render Blueprint](https://dashboard.render.com/) (`render.yaml`)
2. Set secrets: `TELEGRAM_BOT_TOKEN`, `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`
3. [UptimeRobot](https://uptimerobot.com/): **HTTP(s)** → `https://YOUR-SERVICE.onrender.com/health`, interval **5 min**

On Render free tier SQLite lives in `/tmp` — subscriptions reset on redeploy.

### Fly.io (persistent volume)

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
| `CHECK_INTERVAL` | Twitch poll interval, seconds (default 60) |
| `DATABASE_PATH` | SQLite path (default `data/bot.db`) |
| `PORT` | Health-check port (set by Render) |

## Architecture

| Module | Role |
|---|---|
| `bot.py` | Wizard, menu, notifications |
| `twitch.py` | Helix API, templates |
| `links.py` | `t.me/c/…/topic` parsing |
| `health.py` | `/health` for Render and UptimeRobot |
| `db.py` | SQLite subscriptions |

Twitch Helix polling ~60s, Telegram polling, no public webhook.

## Third-party inspiration

Studied [twitchrise](https://github.com/driftywinds/twitchrise), [lajujabot](https://github.com/ria4/lajujabot), [twitch-telegram-bot](https://github.com/mehdizebhi/twitch-telegram-bot). **No code was copied** — ideas only (API polling, subscriptions, channel/group delivery).

## License

**Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International (CC BY-NC-SA 4.0)**

See [LICENSE](LICENSE) · https://creativecommons.org/licenses/by-nc-sa/4.0/

---

Built with Cursor

Support: [DonationAlerts](https://www.donationalerts.com/r/themarfa) · [Crypto](https://nowpayments.io/donation/themarfa)
