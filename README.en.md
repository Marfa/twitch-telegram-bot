# Twitch → Telegram stream notifications

**Stream goes live — the bot notifies wherever you choose.** One-minute setup in Telegram.

Live bot: [@twitch2telegram_bot](https://t.me/twitch2telegram_bot)

Русский: [README.md](README.md)

| Feature | How it works |
|---|---|
| Live bot | [@twitch2telegram_bot](https://t.me/twitch2telegram_bot) — `/start` to set up |
| Languages | Russian and English — picked on first `/start`, change in **⚙️ Settings** |
| Destinations | DM, channel, group or community (with topics) |
| Twitch channel | Link, `m.twitch.tv`, or username |
| Message template | Placeholders; examples `{username}`, `{game}`, `{name}` — [full list](https://bot.themarfa.name/placeholders?lang=en) |
| 🎲 I'm feeling lucky | One-tap AI template: **Groq → Hugging Face → local pool** (last 100) |
| Image | Optional alert image — caption above or below; link preview then off |
| Delay after go-live | Send notification N minutes after stream start |
| Repeat suppression | Skip repeat alerts for X minutes after the first one |
| Schedule reminders | If the streamer has a Twitch schedule — remind N minutes before |
| Subscriptions | List, edit all fields, enable/disable, delete |
| Import from Twitch | OAuth → one-time or periodic sync; new follows only, manual subs kept |
| Stream schedule | **📅 Create schedule** wizard — weekly text for publication |
| System alerts | Toggle admin “bot update” and “bot availability” broadcasts |
| Admin | Scheduled broadcast, DeepL auto-translate, statistics |
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

On first `/start` the bot asks you to choose a language (Russian or English).

### New subscription

`/start` or **➕ New subscription** — setup wizard:

1. Twitch channel (if an alert already exists — open editor or continue)
2. Message template — write your own or tap **🎲 I'm feeling lucky** (AI)
3. Image (add / skip; if added — position: start or end of caption)
4. Ignore keywords (optional)
5. Link preview (skipped when an image is set)
6. Delay notification after stream start (yes/no, minutes)
7. Allow repeat notifications (yes/no; if no — mute minutes)
8. Twitch schedule reminders (if the streamer has a schedule)
9. Destination: DM / channel / group or community
10. For channel or group — add the bot and confirm the chat
11. Delete previous bot message on each new stream? (yes/no)

Each step has **Back**, **Cancel**, and **Main menu**. When editing a subscription — only those three reply buttons.

**Message template** — the wizard shows examples `{username}`, `{game}`, `{name}`. Full placeholder list (including `started_at`, `viewer_count`, `thumbnail_url`, `tags`, …): [`PUBLIC_BASE_URL/placeholders`](https://bot.themarfa.name/placeholders?lang=en) (prod: `https://bot.themarfa.name`).

**🎲 I'm feeling lucky** builds a template with placeholders. Chain: **Groq** (if keyed) → on failure **Hugging Face** → if both are down, a random template from the local DB pool (up to 100 recent successful generations per language). The Example block fills in a random [IGDB](https://api-docs.igdb.com/) game (same Twitch API credentials) and a stream title derived from it. After preview: continue, try again, or full wizard.

**Group or community** — send:
- topic link: `https://t.me/c/name/30`
- group `@username`
- group ID (`-100…`)
- forwarded message from the group (“Forwarded from: …”)

Bot permissions in a group: **send messages** (admin is not required). Also needs permission to **delete its own messages**.

After setup the bot sends **“✅ Setup complete!”** to DM and a test message to the chosen chat.

### Import from Twitch

**⬇️ Import from Twitch** — Twitch OAuth, then choose **one-time import** or **sync**:

- one-time — same as before, token not stored;
- sync — period in days, refresh token stored encrypted; each run adds new follows (**enabled**) and removes unfollowed imports (manual subscriptions untouched);
- on import, alerts are created **paused** (DM to self); Settings → **Subscription sync** (change period / disable).

Twitch Console needs Redirect URL: `https://<service>/oauth/twitch/callback` (see `PUBLIC_BASE_URL`).

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
| `/start` | New subscription / menu |
| `/help` | Help |
| `/cancel` | Cancel current wizard |
| `/schedule` | Create schedule |
| `/feedback` | Feedback |
| `/settings` | Settings |
| ➕ New subscription | Add another channel |
| ⬇️ Import from Twitch | OAuth → one-time or sync |
| 📋 Manage subscriptions | List, edit, delete |
| 📅 Create schedule | Weekly stream schedule text |
| ⚙️ Settings | System notifications and language |
| ↳ 🔔 System notifications | Bot update and availability alerts |
| ↳ 🌐 Language | Russian / English |
| ⚙️ Admin | Broadcast, stats (`ADMIN_USER_IDS` only) |
| ↳ 📣 Broadcast | “Bot updates” or “Bot availability”, scheduled send |
| ↳ 📊 Statistics | Users, subscriptions, languages |
| 🐛 Report a problem | @immarfa or [Issues](https://github.com/Marfa/twitch-telegram-bot/issues) |

**Edit subscription** — same order as creation: template, image, ignore keywords, link preview (hidden when an image is set), delay, repeats, schedule reminders (if enabled at creation), destination, delete old messages.

Notification template example:

```
{username} is live!
{name}
Category: {game}
```

## Deploy

### VPS (auto-deploy)

Server checkout: `/opt/twitch-telegram-bot` (with `.env` beside it).

On push to `main`, GitHub Actions SSHs in, runs `git fetch` + `reset --hard origin/main`, then `scripts/vps-deploy.sh`: `docker compose -f compose.vps.yml up -d --build`, `/health` check, nightly pg-backup cron. Secrets: `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`.

Manual run: Actions → **Deploy VPS** → **Run workflow**.

VPS `.env` needs `POSTGRES_PASSWORD` (Postgres via `compose.vps.yml`) and `PUBLIC_BASE_URL` for OAuth (e.g. `https://bot.themarfa.name`).

### Local / Docker

Leave `DATABASE_URL` unset — SQLite is used (`DATABASE_PATH`, volume in `compose.yml`).

## Environment variables

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather token |
| `TWITCH_CLIENT_ID` | Twitch Client ID |
| `TWITCH_CLIENT_SECRET` | Twitch Client Secret |
| `ADMIN_USER_IDS` | Admin Telegram user IDs (comma-separated) |
| `CHECK_INTERVAL` | Twitch poll interval, seconds (default 60) |
| `POSTGRES_PASSWORD` | Postgres password on VPS (`compose.vps.yml`) |
| `DATABASE_URL` | PostgreSQL. If unset — SQLite (`compose.vps.yml` sets it) |
| `DATABASE_PATH` | SQLite: local `data/bot.db`, Docker `/data/bot.db` |
| `MAX_SUBSCRIPTIONS_PER_OWNER` | Subscription limit per user (default 25) |
| `PUBLIC_BASE_URL` | Public HTTPS origin: OAuth (`…/oauth/twitch/callback`) and placeholder docs (`…/placeholders`). Prod: `https://bot.themarfa.name` |
| `TOKEN_ENCRYPTION_KEY` | Optional Fernet key for refresh tokens (else derived from `TELEGRAM_BOT_TOKEN`) |
| `PORT` | Health/OAuth port (default 8080) |
| `DEEPL_API_KEY` | DeepL — auto-translate admin broadcasts to recipient language |
| `GROQ_API_KEY` | Groq — primary LLM for **I'm feeling lucky** (aliases: `GROQ_API`, `GROK_API`) |
| `GROQ_TEXT_MODEL` | Groq model (default `llama-3.1-8b-instant`) |
| `HF_TOKEN` | Hugging Face — fallback LLM (alias: `HUGGING_FACE_API`) |
| `HF_TEXT_MODEL` | HF model (default `Qwen/Qwen2.5-7B-Instruct`) |

Without Groq/HF keys, **I'm feeling lucky** still works from the local template pool in the DB.

## Architecture

| Module | Role |
|---|---|
| `bot.py` | Wizard, menu, notifications, admin broadcast, schedule |
| `i18n.py` | Strings and keyboards (ru/en) |
| `hf_text.py` | AI templates: Groq → HF → local pool |
| `twitch.py` | Helix API, templates |
| `translate.py` | DeepL for admin broadcasts |
| `links.py` | `t.me/c/…/topic` parsing |
| `health.py` | `/health`, `/placeholders`, Twitch OAuth callback |
| `db.py` | SQLite or PostgreSQL, `lucky_templates` pool |

Twitch Helix poll ~60 s, Telegram polling, no public webhook.

## License

**CC BY-NC-SA 4.0** — see [LICENSE](LICENSE)

---

Built with Cursor

Support: [Donate](https://www.donationalerts.com/r/themarfa) · [Crypto](https://nowpayments.io/donation/themarfa) · [Telegram Tribute](https://t.me/tribute/app?startapp=dBlc)
