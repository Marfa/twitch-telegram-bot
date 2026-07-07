# Twitch → Telegram — уведомления о старте стримов

**Стрим начался — бот сам напишет туда, куда вы скажете.** Настройка за минуту в Telegram.

Готовый бот: [@twitch2telegram_bot](https://t.me/twitch2telegram_bot)

English: [README.en.md](README.en.md)

```bash
cp .env.example .env
docker compose up -d --build
```

| Возможность | Как работает |
|---|---|
| Готовый бот | [@twitch2telegram_bot](https://t.me/twitch2telegram_bot) — `/start` и настройка |
| Куда слать | Личка, канал, группа или сообщество (с темами) |
| Канал Twitch | Ссылка, `m.twitch.tv` или username |
| Текст | Свой шаблон: `{username}`, `{game}`, `{name}` |
| Подписки | Список, вкл/выкл, удаление, опция «удалять старые» |
| Команды | `/start`, `/help`, `/cancel` |
| Деплой | Render + UptimeRobot, Fly.io, Docker |

## Quick Start

1. Бот у [@BotFather](https://t.me/BotFather) → `TELEGRAM_BOT_TOKEN`
2. Приложение на [Twitch Developer Console](https://dev.twitch.tv/console) → `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` (см. ниже)
3. `cp .env.example .env` — заполните переменные
4. `docker compose up -d --build`

Локально:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

## Twitch API ключи

1. [Twitch Developer Console](https://dev.twitch.tv/console) → **Register Your Application**
2. **OAuth Redirect URLs** — `http://localhost` (для бота не используется, поле обязательное)
3. **Client ID** → `TWITCH_CLIENT_ID`
4. **New Secret** → `TWITCH_CLIENT_SECRET`

Бот использует **Client Credentials** — OAuth пользователя не нужен.

## Использование

`/start` — мастер настройки:

1. Канал Twitch
2. Формат сообщения (`{username}`, `{game}`, `{name}`)
3. Куда слать: личка / канал / группа или сообщество
4. Для канала или группы — добавьте бота и подтвердите чат
5. Удалять предыдущие сообщения бота при новом стриме? (да/нет)

**Группа или сообщество** — отправьте:
- ссылку на тему: `https://t.me/c/название/30`
- `@username` группы
- ID группы (`-100…`)
- пересланное сообщение из группы («Переслано из: …»)

Права бота в группе: **отправка сообщений** (админ не обязателен).

После настройки бот пришлёт **«✅ Настройка завершена!»** в личку и тестовое сообщение в выбранный чат.

### Меню и команды

| Кнопка / команда | Действие |
|---|---|
| `/start` | Новая подписка |
| `/help` | Справка |
| `/cancel` | Отменить настройку |
| ➕ Новая подписка | Ещё один канал |
| 📋 Мои подписки | Список, вкл/выкл |
| 🗑 Удалить подписку | Удалить |
| 🐛 Сообщить о проблеме | @immarfa или [Issues](https://github.com/Marfa/twitch-telegram-bot/issues) |

Пример шаблона:

```
{username} в эфире!
{name}
Категория: {game}
```

## Деплой

### Render + UptimeRobot (бесплатно)

1. GitHub → [Render Blueprint](https://dashboard.render.com/) (`render.yaml`)
2. Секреты: `TELEGRAM_BOT_TOKEN`, `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`
3. [UptimeRobot](https://uptimerobot.com/): **HTTP(s)** → `https://ВАШ-СЕРВИС.onrender.com/health`, интервал **5 min**

Подписки хранятся в SQLite на диске `/data` (см. `render.yaml`). **Persistent Disk** на Render требует план Starter ($7/мес) — без диска данные сбрасываются при каждом перезапуске.

### Fly.io (данные на volume)

```bash
fly launch --no-deploy
fly volumes create bot_data --size 1
fly secrets set TELEGRAM_BOT_TOKEN=... TWITCH_CLIENT_ID=... TWITCH_CLIENT_SECRET=...
fly deploy
```

## Переменные окружения

| Переменная | Описание |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен BotFather |
| `TWITCH_CLIENT_ID` | Twitch Client ID |
| `TWITCH_CLIENT_SECRET` | Twitch Client Secret |
| `CHECK_INTERVAL` | Опрос Twitch, сек (по умолчанию 60) |
| `DATABASE_PATH` | SQLite (по умолчанию `data/bot.db`) |
| `PORT` | Health-check (Render задаёт сам) |
| `STATUS_CHECK_INTERVAL` | Опрос RSS Render, сек (по умолчанию 1800) |

Бот присылает уведомление о **запланированных работах Render** (RSS status.render.com) всем, кто писал `/start`.

## Архитектура

| Модуль | Назначение |
|---|---|
| `bot.py` | Wizard, меню, уведомления |
| `twitch.py` | Helix API, шаблоны |
| `links.py` | Парсинг `t.me/c/…/тема` |
| `health.py` | `/health` для Render и UptimeRobot |
| `db.py` | SQLite подписки |

Опрос Twitch Helix ~60 сек, polling Telegram, без публичного webhook.

## Заимствования

Изучены [twitchrise](https://github.com/driftywinds/twitchrise), [lajujabot](https://github.com/ria4/lajujabot), [twitch-telegram-bot](https://github.com/mehdizebhi/twitch-telegram-bot). **Их код не копировался** — только идеи (polling API, подписки, отправка в канал/группу).

## Лицензия

**Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International (CC BY-NC-SA 4.0)**

См. [LICENSE](LICENSE) · https://creativecommons.org/licenses/by-nc-sa/4.0/

---

Код подготовлен с помощью Cursor

Поддержка: [DonationAlerts](https://www.donationalerts.com/r/themarfa) · [Криптой](https://nowpayments.io/donation/themarfa)
