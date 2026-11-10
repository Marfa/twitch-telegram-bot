# TODO: Бот в мессенджере MAX (копия UI + алерты)

Дата: 2026-11-10  
Статус: **дизайн, без реализации**  
Связанный код (целевой): новый Max-контур рядом с `bot.py` / `handlers/delivery.py` / `premium.py`; общая БД Postgres  
Доки API: [создание бота](https://dev.max.ru/docs/chatbots/bots-create/create/), [Bot API](https://dev.max.ru/docs-api) (`platform-api2.max.ru`)  
Контекст чата: полноценный бот в MAX с копией UI Telegram; доставка алертов в ЛС / группы / каналы; Telegram не трогаем.

---

## Решение (зафиксировано)

| # | Решение |
|---|---|
| 1 | Telegram остаётся как есть |
| 2 | **Нет миграции** TG→Max: отдельные пользователи, подписки, каналы/группы |
| 3 | Одна БД (новые таблицы / `platform='max'`) |
| 4 | Доставка: ЛС, группы, каналы Max |
| 5 | UI — **копия** сценариев Telegram |
| 6 | Premium на Max — **заглушка** полного плана: **100 ₽/мес** или **1000 ₽/год** |
| 7 | Логика реальной оплаты (Prodamus и пр.) — **отдельный план**, не этот файл |
| 8 | Профиля на [MAX для партнёров](https://dev.max.ru/docs/chatbots/bots-create/create/) пока **нет** → API-код после модерации и токена |

---

## Главный вывод

Полноценный порт UI + алерты — **месяцы**, не «адаптер за выходные».  
Чтобы не получить два расходящихся продукта: domain (Twitch → событие → шаблон → entitlement) общий; UI Max — отдельный контур с теми же ключами `i18n` где возможно; identity TG и Max **не смешивать**.

Big-bang «вынести весь Telegram на Messenger protocol» до токена Max **не делать**. Adapter и общий send резать по мере порта экранов.

---

## Архитектура

```
shared: Twitch poll, шаблоны алертов, IGDB, entitlement helpers, jobs
        ├── Telegram (текущий код)
        └── Max: adapter + свои handlers/UI, те же domain-вызовы
```

| Слой | Где живёт |
|------|-----------|
| Domain | Общий: `check_streams`, шаблоны, `has_feature` / pause map |
| UI / callbacks | Отдельный Max-контур (копия экранов), тексты из `i18n` |
| Transport | Max adapter: send / edit / delete / pin / upload / answer callback |
| Identity | Только Max ids в Max-таблицах; TG-таблицы без изменений |

Опциональная ручная связка TG↔Max аккаунтов — **вне** этого плана.

---

## Модель данных (эскиз)

Новое (или зеркало с префиксом / `platform`):

- `max_users` — Max user id, locale, premium stub fields, timestamps
- `max_subscriptions` — смысл как у TG-подписок; `owner_id` → `max_users`
- destinations: `dest_type` ∈ `dm|group|channel`, `chat_id` Max
- при необходимости: `max_alert_history` / delivery log — отдельно от TG
- Premium stub: семантика full plan **без** Stars / `charge_id`

TG-схему не мигрируем и не объединяем identity в одних колонках без `platform`.

---

## Runtime Max

1. `MAX_BOT_TOKEN`; процесс тот же или отдельный + webhook `POST /max/webhook`.
2. Prod: webhook HTTPS :443 (рекомендация Max); long polling — только локалка.
3. Rate limit: очередь ≤ **2 msg/s** на чат; глобально учитывать ~30 rps API.
4. Health: `/health/c/max` рядом с telegram.
5. PingZen: монитор Max; maintenance window при деплое с даунтаймом бота.
6. Клиент к `platform-api2.max.ru`; учесть доверенный сертификат Минцифры в образе при необходимости.

Python: community `maxapi` или тонкий HTTP-клиент по OpenAPI — выбор на фазе 1 (новая зависимость только с явным approval).

---

## UI (копия)

Порт 1:1 по сценариям; эмуляция того, чего нет в Max API:

| Telegram | Max |
|----------|-----|
| Reply keyboard / pulse | inline + кнопки `message` / повтор меню |
| Inline callback | `callback` + `POST /answers` |
| HTML | `format: html` или markdown-маппинг |
| Topics (`t.me/c/…/n`) | нет → только chat/channel id |
| WebApp / MenuButton | `open_app` или url; chat Mini App — подэтап |
| Setup групп только в ЛС | то же правило |

Порядок экранов:

1. `/start`, главное меню, язык, помощь  
2. Подписки: список, вкл/выкл, удаление  
3. Wizard нового алерта (live + dest ЛС/группа/канал)  
4. Редактирование шаблона / advanced (по stub entitlement)  
5. Settings, pause notifications  
6. Остальные разделы меню (drops, watch, schedule, …) по продуктовому приоритету  
7. Premium-заглушка: 100 ₽ / 1000 ₽, «оплата скоро» / disabled  

Критерий «копия»: те же ветки и ключи i18n; не обязательно пиксель-в-пиксель виджеты Max.  
Риск расхождения меню: при каждом UX-PR в TG — чеклист sync Max (или общий screen-layer позже).

---

## Доставка алертов

1. Shared job видит TG- и Max-подписки (или параллельный reader тех же Twitch-событий).  
2. Ветка Max: `MaxDelivery.send(sub, payload)` через adapter.  
3. Где API позволяет: delete previous, pin, custom buttons (link/callback).  
4. Ошибки доступа → пометка чата/юзера; notice в ЛС владельца Max.  
5. Дедуп/history Max отдельно от TG `alert_history`.

---

## Premium-заглушка

| План | Цена | Что открывает |
|------|------|----------------|
| Месяц | 100 ₽ | полный Premium (все `FEATURE_IDS`) |
| Год | 1000 ₽ | то же |

- UI с ценами; оплата → «скоро» (без Prodamus в этом плане).  
- На время разработки unlock: админ/флаг в БД и/или Twitch-саб (`PREMIUM_TWITCH_LOGIN`), если решим включить.  
- Pause map / entitlement — как у full plan в TG.  
- À la carte / gift / партнёрка Stars на Max — **не в scope**.  
- Реальная оплата (в т.ч. [Prodamus](https://prodamus.ru/)) — **отдельный TODO**, не этот файл.

---

## Фазы

| Фаза | Содержание | Зависимость |
|------|------------|-------------|
| **0. Доступ** | Профиль MAX для партнёров → бот → модерация → токен | блокер |
| **1. Каркас** | Таблицы Max, config, webhook, adapter hello / `/start` | после 0 |
| **2. Delivery MVP** | Seed/ручная Max-подписка → live-алерт в ЛС | после 1 |
| **3. Destinations** | Привязка группы/канала, права админа, публикация | после 2 |
| **4. UI core** | Меню + подписки + wizard live | после 1–3 |
| **5. UI rest** | Остальные разделы как в TG | после 4 |
| **6. Premium stub** | Экран 100/1000 + admin/(опц.) Twitch unlock | параллельно 4–5 |
| **7. Ops** | health, PingZen, self_check Max smoke, README / guide / flow-map | к релизу |
| **∞** | Prodamus / рекуррент | отдельный план |

До фазы 0 допустимы только эскиз схемы и точечный вынос шаблонов алертов — без Max SDK.

---

## Не в scope

- [ ] Миграция или обязательный линк TG↔Max аккаунтов  
- [ ] Реальная оплата / Prodamus / автопродление  
- [ ] Паритет Stars à la carte / gift / партнёрка на Max  
- [ ] Выключение или перепись Telegram  
- [ ] Обязательный полный rewrite `delivery.py` / protocol до токена Max  

---

## Риски

- Копия UI без общего screen-layer → два меню разъедутся.  
- Reply keyboard / topics / WebApp — не 1:1; эмуляцию зафиксировать до порта.  
- 2 msg/s на чат — очереди на multi-dest и рассылки.  
- Юр. ограничения MAX (резидент РФ, модерация до ~48 ч, ник вида `idИНН_bot`).  
- Без профиля/токена фазы 1+ стоят.

---

## Definition of Done (релиз Max v1)

- [ ] Пользователь Max проходит основной UI-контур как в TG (с оговорками эмуляции)  
- [ ] Алерты уходят в ЛС, группы и каналы Max  
- [ ] Telegram-бот без регрессий  
- [ ] Premium: заглушка 100/1000; full plan только stub/admin/(опц. Twitch)  
- [ ] `/health` + PingZen знают Max  
- [ ] Оплата явно «не сделано» → отдельная задача  

---

## Чеклист старта реализации (когда снимем «без реализации»)

- [ ] Верифицированный профиль MAX для партнёров  
- [ ] Бот создан, модерация пройдена, токен в `.env` / VPS (не в git)  
- [ ] Согласован выбор клиента (`maxapi` vs тонкий HTTP)  
- [ ] Схема `max_users` / `max_subscriptions` + dual backend SQLite/Postgres  
- [ ] Webhook + PingZen maintenance на первый disruptive деплой  
- [ ] Отдельный TODO на оплату (Prodamus), если пора  
