# Twitch → Telegram — уведомления о старте стримов в личку, канал или группу с темами

**Стрим начался — бот сам напишет туда, куда вы скажете.** Настройка за минуту через диалог в Telegram.

```bash
cp .env.example .env   # заполните токены
docker compose up -d
```

| Возможность | Как работает |
|---|---|
| Куда слать | Личка, канал, группа с темами |
| Канал Twitch | Ссылка, m.twitch.tv или username |
| Текст уведомления | Свой шаблон с `{username}`, `{game}`, `{name}` |
| Подписки | Список, вкл/выкл, удаление |
| Деплой | Docker, Fly.io (бесплатно), Render |

## Quick Start

1. Создайте бота у [@BotFather](https://t.me/BotFather) → `TELEGRAM_BOT_TOKEN`
2. Получите Twitch API ключи (см. ниже) → `TWITCH_CLIENT_ID` и `TWITCH_CLIENT_SECRET`
3. Скопируйте `.env.example` в `.env` и заполните переменные
4. Запустите:

```bash
docker compose up -d --build
```

Локально без Docker:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

## Где взять TWITCH_CLIENT_ID и TWITCH_CLIENT_SECRET

1. Войдите на [Twitch Developer Console](https://dev.twitch.tv/console) под аккаунтом Twitch
2. **Register Your Application** (или откройте существующее)
3. Заполните форму:
   - **Name** — любое (например, `My Stream Notifier`)
   - **OAuth Redirect URLs** — для этого бота не нужны, можно `http://localhost`
   - **Category** — например, `Application Integration`
4. После создания на странице приложения:
   - **Client ID** — скопируйте в `TWITCH_CLIENT_ID`
   - **Client Secret** — нажмите **New Secret**, скопируйте в `TWITCH_CLIENT_SECRET`

Бот использует **Client Credentials** (app access token) — достаточно ID и Secret, OAuth-авторизация пользователя не требуется. Нужны права на чтение публичных данных: статус стрима, название, категория.

## Использование

`/start` — мастер настройки:

1. Канал Twitch (ссылка или username)
2. Формат сообщения (`{username}`, `{game}`, `{name}`)
3. Куда слать: личка / канал / группа с темами
4. Для канала или группы — добавьте бота и подтвердите чат

Меню:

- **➕ Новая подписка** — ещё один канал
- **📋 Мои подписки** — список, вкл/выкл кнопкой
- **🗑 Удалить подписку** — полное удаление

Пример шаблона:

```
🔴 {username} в эфире!
{name}
Категория: {game}
```

## Бесплатный деплой

### Fly.io (рекомендуется — данные сохраняются)

```bash
fly launch --no-deploy
fly volumes create bot_data --size 1
fly secrets set TELEGRAM_BOT_TOKEN=... TWITCH_CLIENT_ID=... TWITCH_CLIENT_SECRET=...
fly deploy
```

### Render + UptimeRobot (бесплатно, без засыпания)

Render усыпляет бесплатные **Web Service** через ~15 мин без запросов. Бот поднимает `/health`, а [UptimeRobot](https://uptimerobot.com/) пингует его каждые 5 минут.

1. Залейте репозиторий на GitHub
2. [Render Dashboard](https://dashboard.render.com/) → **New** → **Blueprint** → выберите репозиторий (`render.yaml`)
3. Задайте секреты: `TELEGRAM_BOT_TOKEN`, `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`
4. После деплоя скопируйте URL сервиса, например `https://twitch-telegram-bot.onrender.com`
5. UptimeRobot → **Add Monitor**:
   - Type: **HTTP(s)**
   - URL: `https://ВАШ-СЕРВИС.onrender.com/health`
   - Monitoring interval: **5 minutes**

На free-тарифе SQLite в `/tmp` — подписки сбросятся при редеплое или рестарте. Пока сервис не перезапускается, данные живут.

### Fly.io (данные сохраняются на volume)

```bash
docker compose up -d --build
```

## Переменные окружения

| Переменная | Описание |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен от BotFather |
| `TWITCH_CLIENT_ID` | Client ID приложения Twitch |
| `TWITCH_CLIENT_SECRET` | Client Secret приложения Twitch |
| `CHECK_INTERVAL` | Интервал опроса Twitch, сек (по умолчанию 60) |
| `DATABASE_PATH` | Путь к SQLite (по умолчанию `data/bot.db`) |
| `PORT` | Порт HTTP health-check (Render задаёт автоматически) |

## Cursor rules

Правила для AI-ассистента адаптированы из [awesome-cursorrules](https://github.com/PatrickJS/awesome-cursorrules) и лежат в `.cursor/rules/`:

| Файл | Источник | Область |
|---|---|---|
| `python-bot.mdc` | python best practices | `**/*.py` |
| `docker.mdc` | docker | Dockerfile, compose, render |
| `telegram-twitch.mdc` | telegram API patterns | bot, twitch, health |

## Как устроено

Опрос Twitch Helix API каждые ~60 сек — не нужен публичный webhook. Подписки в SQLite. Один процесс, polling Telegram.

## Заимствования и лицензии

Перед разработкой изучены три открытых проекта. **Их исходный код в этот репозиторий не включён** — ни файлов, ни фрагментов, ни форков. Всё написано с нуля на Python.

| Проект | Лицензия | Что взято | Что не взято |
|---|---|---|---|
| [twitchrise](https://github.com/driftywinds/twitchrise) | [GPL-3.0](https://github.com/driftywinds/twitchrise/blob/main/LICENSE) | Идея опроса Helix API (`/oauth2/token`, `/helix/streams`) вместо EventSub webhook | `head.py`, Apprise, цикл из репозитория |
| [lajujabot](https://github.com/ria4/lajujabot) | [GPL-3.0](https://github.com/ria4/lajujabot/blob/main/LICENSE) | Идея Telegram-бота с подписками и шаблоном текста | `bot.py`, `twitch.py`, EventSub, PicklePersistence |
| [twitch-telegram-bot](https://github.com/mehdizebhi/twitch-telegram-bot) | **Файл LICENSE отсутствует** | Идея отправки в канал/группу Telegram | Java/Spring-код, Twitch4J, SQLite-схема |

### Почему это не нарушает GPL-3.0

GPL-3.0 распространяется на **производные работы от исходного кода**. Здесь:

- нет копирования выражений из GPL-проектов (только общие идеи: «опрашивать API», «слать в Telegram»);
- вызовы публичного Twitch Helix API — стандартная интеграция, не защищённое авторским правом выражение;
- архитектура отличается: polling + SQLite + `python-telegram-bot`, а не EventSub webhook или Spring Boot.

Если сравнить `twitch.py` с `head.py` из twitchrise — совпадает только последовательность публичных HTTP-запросов к Twitch; классы, структура и остальной код разные.

### Лицензия этого репозитория

Код этого проекта — оригинальная реализация. При публикации рекомендуется добавить собственный файл `LICENSE` (например, MIT). До его появления распространяйте исходники на своих условиях с сохранением атрибуции проектов из таблицы выше.

---

Код подготовлен с помощью Cursor

Поддержка проекта: [DonationAlerts](https://www.donationalerts.com/r/themarfa) · [Криптой](https://nowpayments.io/donation/themarfa)
