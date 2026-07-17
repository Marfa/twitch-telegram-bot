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
| Языки | Русский и English — выбор при первом `/start`, смена в **⚙️ Настройки** |
| Куда слать | Личка, канал, группа или сообщество (с темами) |
| Канал Twitch | Ссылка, `m.twitch.tv` или username |
| Текст | Свой шаблон: `{username}`, `{game}`, `{name}` |
| Отложенная отправка | Уведомление через N минут после старта стрима |
| Заглушка повторов | Не слать повторно X минут после первого уведомления |
| Подписки | Список, вкл/выкл, редактирование всех полей, удаление |
| Расписание стримов | Мастер **📅 Создать расписание** — текст на неделю для публикации |
| Системные оповещения | Вкл/выкл рассылок об обновлениях и доступности бота |
| Статус хостинга | Уведомления о работах Render и сбоях Aiven (RSS) |
| Админка | Рассылка с отложенной отправкой, авто-перевод DeepL, статистика |
| Команды | `/start`, `/help`, `/cancel` |
| Deploy | Render Free + Aiven PostgreSQL, Fly.io, Docker |

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

При первом `/start` бот предложит выбрать язык (русский или English).

### Новая подписка

`/start` или **➕ Новая подписка** — мастер настройки:

1. Канал Twitch
2. Формат сообщения (`{username}`, `{game}`, `{name}`)
3. Превью ссылок в уведомлениях
4. Отложить отправку уведомления после начала стрима (да/нет, минуты)
5. Разрешить повторные уведомления (да/нет; при «нет» — минуты заглушки)
6. Куда слать: личка / канал / группа или сообщество
7. Для канала или группы — добавьте бота и подтвердите чат
8. Удалять предыдущие сообщения бота при новом стриме? (да/нет)

На каждом шаге доступны **« Назад**, **Отмена** и **Главное меню**. При редактировании подписки — только эти три кнопки.

**Группа или сообщество** — отправьте:
- ссылку на тему: `https://t.me/c/название/30`
- `@username` группы
- ID группы (`-100…`)
- пересланное сообщение из группы («Переслано из: …»)

Права бота в группе: **отправка сообщений** (админ не обязателен).

После настройки бот пришлёт **«✅ Настройка завершена!»** в личку и тестовое сообщение в выбранный чат.

### Расписание стримов

**📅 Создать расписание** — мастер для текста публикации на следующую неделю (с ближайшего понедельника по воскресенье):

1. Описание и пример формата
2. Подтверждение «Сформировать расписание?»
3. Для каждого дня: игра/название стрима и время (`15:30`)
4. **Стрим не планируется** — пропустить день
5. Со 2-го дня — **Завершить создание расписания** (на последнем дне кнопки нет)

Итог — готовый текст, например:

```
- 20 июля 15:30 Sovereign Syndicate
- 21 июля 18:00 Just Chatting
```

Даты и месяцы формируются на языке пользователя.

### Меню и команды

| Кнопка / команда | Действие |
|---|---|
| `/start` | Новая подписка |
| `/help` | Справка |
| `/cancel` | Отменить текущий мастер |
| ➕ Новая подписка | Ещё один канал |
| 📋 Управление подписками | Список, редактирование, удаление |
| 📅 Создать расписание | Текст расписания на неделю |
| ⚙️ Настройки | Системные уведомления и язык |
| ↳ 🔔 Системные уведомления | Оповещения об обновлениях и доступности бота |
| ↳ 🌐 Выбор языка | Русский / English |
| ⚙️ Админка | Рассылка, статистика (только `ADMIN_USER_IDS`) |
| ↳ 📣 Рассылка | «Обновления бота» или «Доступность бота», отложенная отправка |
| ↳ 📊 Статистика | Пользователи, подписки, языки |
| 🐛 Сообщить о проблеме | @immarfa или [Issues](https://github.com/Marfa/twitch-telegram-bot/issues) |

**Редактирование подписки** — шаблон, куда слать, задержка, повторы, удаление старых сообщений, превью ссылок.

Пример шаблона уведомления:

```
{username} в эфире!
{name}
Категория: {game}
```

## Деплой

### Render Free + Aiven PostgreSQL (бесплатно, подписки сохраняются)

Бот на Render Free, данные — во внешней PostgreSQL на [Aiven](https://aiven.io/free-postgresql-database) (без карты).

**1. Aiven**

1. Регистрация на [aiven.io](https://aiven.io) (карта не нужна)
2. **Create service** → PostgreSQL → план **Free**
3. Скопируйте **Service URI** (`postgres://…`)

**2. Render**

1. GitHub → [Render Blueprint](https://dashboard.render.com/) (`render.yaml`)
2. Секреты:
   - `TELEGRAM_BOT_TOKEN`
   - `TWITCH_CLIENT_ID`
   - `TWITCH_CLIENT_SECRET`
   - `DATABASE_URL` — URI из Aiven
   - `ADMIN_USER_IDS` — Telegram user ID админов через запятую
   - `DEEPL_API_KEY` — опционально, авто-перевод админ-рассылок
3. [UptimeRobot](https://uptimerobot.com/): **HTTP(s)** → `https://ВАШ-СЕРВИС.onrender.com/health`, интервал **5 min**

Подписки живут в Aiven и не сбрасываются при рестарте Render.

### Локально / Docker

`DATABASE_URL` не задавайте — используется SQLite (`DATABASE_PATH`, volume в `compose.yml`).

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
| `ADMIN_USER_IDS` | Telegram user ID админов (через запятую) |
| `CHECK_INTERVAL` | Опрос Twitch, сек (по умолчанию 60) |
| `DATABASE_URL` | PostgreSQL (Aiven). Если не задан — SQLite |
| `DATABASE_PATH` | Путь к SQLite (по умолчанию `data/bot.db`) |
| `MAX_SUBSCRIPTIONS_PER_OWNER` | Лимит подписок на пользователя (по умолчанию 25) |
| `PORT` | Health-check (Render задаёт сам) |
| `STATUS_CHECK_INTERVAL` | Опрос RSS статусов, сек (по умолчанию 1800) |
| `RENDER_STATUS_RSS` | RSS Render (по умолчанию status.render.com) |
| `AIVEN_STATUS_RSS` | RSS Aiven (по умолчанию status.aiven.io) |
| `DEEPL_API_KEY` | DeepL — авто-перевод админ-рассылок на язык получателя |

Бот присылает уведомления о **запланированных работах Render** и **сбоях Aiven** пользователям с включёнными оповещениями о доступности.

## Архитектура

| Модуль | Назначение |
|---|---|
| `bot.py` | Wizard, меню, уведомления, админ-рассылка, расписание |
| `i18n.py` | Тексты и клавиатуры (ru/en) |
| `twitch.py` | Helix API, шаблоны |
| `translate.py` | DeepL для админ-рассылок |
| `links.py` | Парсинг `t.me/c/…/тема` |
| `render_status.py` | RSS Render и Aiven |
| `health.py` | `/health` для Render и UptimeRobot |
| `db.py` | SQLite или PostgreSQL |

Опрос Twitch Helix ~60 сек, polling Telegram, без публичного webhook.

## Заимствования

Изучены [twitchrise](https://github.com/driftywinds/twitchrise), [lajujabot](https://github.com/ria4/lajujabot), [twitch-telegram-bot](https://github.com/mehdizebhi/twitch-telegram-bot). **Их код не копировался** — только идеи (polling API, подписки, отправка в канал/группу).

## Лицензия

**Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International (CC BY-NC-SA 4.0)**

См. [LICENSE](LICENSE) · https://creativecommons.org/licenses/by-nc-sa/4.0/

---

Код подготовлен с помощью Cursor

Поддержка: [Telegram Tribute](https://t.me/tribute/app?startapp=dBlc) · [Криптой](https://nowpayments.io/donation/themarfa)
