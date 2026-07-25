# Twitch → Telegram — уведомления о старте стримов

**Стрим начался — бот сам напишет туда, куда вы скажете.** Настройка за минуту в Telegram.

Готовый бот: [@twitch2telegram_bot](https://t.me/twitch2telegram_bot)

English: [README.en.md](README.en.md)

| Возможность | Как работает |
|---|---|
| Готовый бот | [@twitch2telegram_bot](https://t.me/twitch2telegram_bot) — `/start` и настройка |
| Языки | Русский и English — выбор при первом `/start`, смена в **⚙️ Настройки** |
| Куда слать | Личка, канал, группа или сообщество (с темами) |
| Канал Twitch | Ссылка, `m.twitch.tv` или username |
| Текст | Свой шаблон: `{username}`, `{game}`, `{name}` |
| 🎲 Мне повезёт | AI-шаблон одной кнопкой: **Groq → Hugging Face → локальный пул** (последние 100) |
| Картинка | Опционально к уведомлению — в начале или в конце подписи; превью ссылок тогда выкл |
| Отложенная отправка | Уведомление через N минут после старта стрима |
| Заглушка повторов | Не слать повторно X минут после первого уведомления |
| Подписки | Список, вкл/выкл, редактирование всех полей, удаление |
| Импорт из Twitch | OAuth → фолловы как оповещения на паузе; дубли пропускаются |
| Расписание стримов | Мастер **📅 Создать расписание** — текст на неделю для публикации |
| Системные оповещения | Вкл/выкл рассылок об обновлениях и доступности бота |
| Админка | Рассылка с отложенной отправкой, авто-перевод DeepL, статистика |
| Команды | `/start`, `/help`, `/cancel`, `/schedule`, `/feedback`, `/settings` |
| Deploy | VPS (Docker) |

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
2. **OAuth Redirect URLs** — `https://<ваш-сервис>/oauth/twitch/callback` (для импорта фолловов; локально — публичный HTTPS через ngrok/`PUBLIC_BASE_URL`)
3. **Client ID** → `TWITCH_CLIENT_ID`
4. **New Secret** → `TWITCH_CLIENT_SECRET`

Опрос стримов идёт через **Client Credentials**. Импорт подписок из Twitch — через user OAuth (`user:read:follows`).

## Использование

При первом `/start` бот предложит выбрать язык (русский или English).

### Новая подписка

`/start` или **➕ Новая подписка** — мастер настройки:

1. Канал Twitch
2. Формат сообщения — свой текст или **🎲 Мне повезёт** (AI)
3. Картинка (добавить / пропустить; при добавлении — позиция: в начале или в конце)
4. Превью ссылок (шаг пропускается, если есть картинка)
5. Отложить отправку после начала стрима (да/нет, минуты)
6. Разрешить повторные уведомления (да/нет; при «нет» — минуты заглушки)
7. Куда слать: личка / канал / группа или сообщество
8. Для канала или группы — добавьте бота и подтвердите чат
9. Удалять предыдущие сообщения бота при новом стриме? (да/нет)

На каждом шаге доступны **« Назад**, **Отмена** и **Главное меню**. При редактировании подписки — только эти три кнопки.

**🎲 Мне повезёт** — генерирует шаблон с плейсхолдерами. Цепочка: **Groq** (если задан ключ) → при сбое **Hugging Face** → если оба недоступны, случайный шаблон из локального пула в БД (до 100 последних удачных генераций на язык). В блоке «Пример» подставляются случайная игра из [IGDB](https://api-docs.igdb.com/) (те же Twitch API-ключи) и название стрима на её основе. После превью: продолжить, ещё раз, или полный мастер.

**Группа или сообщество** — отправьте:
- ссылку на тему: `https://t.me/c/название/30`
- `@username` группы
- ID группы (`-100…`)
- пересланное сообщение из группы («Переслано из: …»)

Права бота в группе: **отправка сообщений** (админ не обязателен). Нужно право **удалять свои сообщения**.

После настройки бот пришлёт **«✅ Настройка завершена!»** в личку и тестовое сообщение в выбранный чат.

### Импорт из Twitch

**⬇️ Импорт подписок из Twitch** — OAuth на Twitch, затем импорт каналов из `helix/channels/followed`:

- шаблон по умолчанию, превью включено;
- оповещения создаются **на паузе** (DM себе);
- уже существующие каналы пропускаются;
- после импорта — короткий итог, **Включить все** и кнопки редактирования только для новых каналов.

В Twitch Console нужен Redirect URL: `https://<сервис>/oauth/twitch/callback` (см. `PUBLIC_BASE_URL`).

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
| `/start` | Новая подписка / меню |
| `/help` | Справка |
| `/cancel` | Отменить текущий мастер |
| `/schedule` | Создать расписание |
| `/feedback` | Обратная связь |
| `/settings` | Настройки |
| ➕ Новая подписка | Ещё один канал |
| ⬇️ Импорт подписок из Twitch | OAuth + импорт фолловов (на паузе) |
| 📋 Управление подписками | Список, редактирование, удаление |
| 📅 Создать расписание | Текст расписания на неделю |
| ⚙️ Настройки | Системные уведомления и язык |
| ↳ 🔔 Системные уведомления | Оповещения об обновлениях и доступности бота |
| ↳ 🌐 Выбор языка | Русский / English |
| ⚙️ Админка | Рассылка, статистика (только `ADMIN_USER_IDS`) |
| ↳ 📣 Рассылка | «Обновления бота» или «Доступность бота», отложенная отправка |
| ↳ 📊 Статистика | Пользователи, подписки, языки |
| 🐛 Сообщить о проблеме | @immarfa или [Issues](https://github.com/Marfa/twitch-telegram-bot/issues) |

**Редактирование подписки** — шаблон, картинка (добавить / обновить / удалить), куда слать, задержка, повторы, удаление старых сообщений, превью ссылок (скрыто, если есть картинка).

Пример шаблона уведомления:

```
{username} в эфире!
{name}
Категория: {game}
```

## Деплой

### VPS (автодеплой)

Репозиторий на сервере: `/opt/twitch-telegram-bot` (рядом лежит `.env`).

При пуше в `main` GitHub Actions по SSH делает `git fetch` + `reset --hard origin/main`, затем `scripts/vps-deploy.sh`: `docker compose -f compose.vps.yml up -d --build`, проверка `/health`, cron ночного pg-backup. Secrets: `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`.

Ручной запуск: Actions → **Deploy VPS** → **Run workflow**.

В `.env` на VPS нужны `POSTGRES_PASSWORD` (Postgres из `compose.vps.yml`) и `PUBLIC_BASE_URL` для OAuth (например `https://bot.themarfa.name`).

### Локально / Docker

`DATABASE_URL` не задавайте — используется SQLite (`DATABASE_PATH`, volume в `compose.yml`).

## Переменные окружения

| Переменная | Описание |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен BotFather |
| `TWITCH_CLIENT_ID` | Twitch Client ID |
| `TWITCH_CLIENT_SECRET` | Twitch Client Secret |
| `ADMIN_USER_IDS` | Telegram user ID админов (через запятую) |
| `CHECK_INTERVAL` | Опрос Twitch, сек (по умолчанию 60) |
| `POSTGRES_PASSWORD` | Пароль Postgres на VPS (`compose.vps.yml`) |
| `DATABASE_URL` | PostgreSQL. Если не задан — SQLite (`compose.vps.yml` задаёт сам) |
| `DATABASE_PATH` | SQLite: локально `data/bot.db`, в Docker `/data/bot.db` |
| `MAX_SUBSCRIPTIONS_PER_OWNER` | Лимит подписок на пользователя (по умолчанию 25) |
| `PUBLIC_BASE_URL` | Публичный HTTPS origin для OAuth (`…/oauth/twitch/callback`) |
| `PORT` | Порт health/OAuth (по умолчанию 8080) |
| `DEEPL_API_KEY` | DeepL — авто-перевод админ-рассылок на язык получателя |
| `GROQ_API_KEY` | Groq — основной LLM для **Мне повезёт** (алиасы: `GROQ_API`, `GROK_API`) |
| `GROQ_TEXT_MODEL` | Модель Groq (по умолчанию `llama-3.1-8b-instant`) |
| `HF_TOKEN` | Hugging Face — запасной LLM (алиас: `HUGGING_FACE_API`) |
| `HF_TEXT_MODEL` | Модель HF (по умолчанию `Qwen/Qwen2.5-7B-Instruct`) |

Без ключей Groq/HF кнопка **Мне повезёт** всё равно работает — из локального пула шаблонов в БД.

## Архитектура

| Модуль | Назначение |
|---|---|
| `bot.py` | Wizard, меню, уведомления, админ-рассылка, расписание |
| `i18n.py` | Тексты и клавиатуры (ru/en) |
| `hf_text.py` | AI-шаблоны: Groq → HF → локальный пул |
| `twitch.py` | Helix API, шаблоны |
| `translate.py` | DeepL для админ-рассылок |
| `links.py` | Парсинг `t.me/c/…/тема` |
| `health.py` | `/health` + Twitch OAuth callback |
| `db.py` | SQLite или PostgreSQL, пул `lucky_templates` |

Опрос Twitch Helix ~60 сек, polling Telegram, без публичного webhook.

## Заимствования

Изучены [twitchrise](https://github.com/driftywinds/twitchrise), [lajujabot](https://github.com/ria4/lajujabot), [twitch-telegram-bot](https://github.com/mehdizebhi/twitch-telegram-bot). **Их код не копировался** — только идеи (polling API, подписки, отправка в канал/группу).

## Лицензия

**Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International (CC BY-NC-SA 4.0)**

См. [LICENSE](LICENSE) · https://creativecommons.org/licenses/by-nc-sa/4.0/

---

Код подготовлен с помощью Cursor

Поддержка проекта: [Донат](https://www.donationalerts.com/r/themarfa) · [Донат криптой](https://nowpayments.io/donation/themarfa) · [Telegram Tribute](https://t.me/tribute/app?startapp=dBlc)
