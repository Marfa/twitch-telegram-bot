# TODO: Универсальный webhook-destination + тонкий бот MAX

Дата: 2026-11-10 (обновлено: 2026-11-26 — фаза 3 ✅ каркас)  
Статус: **фазы 0–3 ✅** — Max каркас: таблицы, `/hooks/max/updates` + `/hooks/max/d/{id}`, `/start` → link code + DM dest, send в ЛС, bootstrap `subscribe`. Далее фазы **4–6** (группы/каналы, UX «Привязать Max», ops).  
Связанный код: `outbound_webhook.py`, `max_api.py`, `max_bot.py`, `handlers/delivery.py`, wizard dest / `subscriptions`  
Доки: контракт [`outbound-webhook.md`](outbound-webhook.md); Max — [создание бота](https://dev.max.ru/docs/chatbots/bots-create/create/), [управление / токен](https://dev.max.ru/docs/chatbots/bots-create/manage/), [Bot API](https://dev.max.ru/docs-api), [POST /subscriptions](https://dev.max.ru/docs-api/methods/POST/subscriptions/)  
Контекст: вся настройка алертов остаётся в Telegram; destination **Webhook** (универсальный URL); бот MAX — онбординг + привязка куда слать + приём нашего webhook.  
Ассет аватара для Max: [`assets/max-bot-logo-500.png`](../assets/max-bot-logo-500.png) (500×500).  
Публичная ссылка бота: `https://max.ru/id760403963548_bot` (`@id760403963548_bot`).

Связано (другой контракт payload): Discord Incoming Webhook уже в коде (`dest_type=discord`) — не универсальный JSON; этот файл про общий HTTPS webhook (+ Max).

---

## Решение (зафиксировано)

| # | Решение |
|---|---|
| 1 | Telegram остаётся основным UI (настройка подписок, шаблоны, Premium) |
| 2 | Новый destination: **Webhook** — POST на **любой** HTTPS URL пользователя/сервиса |
| 3 | Бот MAX — **не** копия UI: только онбординг и настройка **куда** доставлять (ЛС / группа / канал) |
| 4 | Связка: Telegram ↔ Max (или любой приёмник) через webhook; общение ботов — HTTP |
| 5 | Пользователи/чаты Max **отдельные**; миграции TG→Max нет; одна Postgres ок |
| 6 | Первый официальный consumer webhook — **наш Max-бот**; тот же контракт — Make, свой сервер и т.д. |
| 7 | Копия UI Max / рублёвый Premium на Max — **сняты** с этого плана (см. «Не в scope») |
| 8 | Профиль MAX верифицирован; бот `@id760403963548_bot` опубликован (тип «Добавление в чаты»); группы разрешены; без мини-приложения; `MAX_BOT_TOKEN` в local + VPS, `GET /me` 200 |
| 9 | Реальная оплата на Max не нужна в этой схеме (Premium = Telegram Stars / как сейчас) |

---

## Главный вывод

Вместо порта всего меню в Max: **один новый тип назначения в TG** + тонкий Max как приёмник.  
Поддержка UI не удваивается. Max-пользователь без Telegram **не** настраивает алерты — только dest после связи.

Схема **C** (универсальный хук) шире «только Max»: один контракт доставки наружу.

```
[Пользователь] ──настройка──► Telegram-бот
                                    │
                         alert готов (текст / медиа / кнопки-ссылки)
                                    │ POST HTTPS + HMAC (идемпотентный)
                                    ▼
                    ┌───────────────┴───────────────┐
                    ▼                               ▼
             наш Max-бот                    любой URL
             (онбординг + send               (Make, свой
              в ЛС/группу/канал)              сервер, …)
```

---

## Продукт

### Telegram

- В мастере / редактировании «куда слать»: пункт **Webhook** (рядом с ЛС / группа / канал / Discord).
- Поля: URL (`https://…`), секрет выдаёт бот (показывается один раз), тест «отправить пробный».
- Для Max: UX «Привязать Max» — ввод кода из Max-бота или deep link; под капотом пишется URL нашего Max-приёмника + `chat_ref` / binding id (пользователь может не видеть сырой URL). **Ещё не сделано.**
- Рендер алерта как сейчас; в webhook уходит **уже собранный** payload (не сырой Twitch event).
- Ошибки доставки: один retry на 429; pause + notice владельцу при 401/403/404/410.

### Max (тонкий бот)

- ✅ `/start` / `bot_started`: DM dest + код связи + URL ingest (секрет один раз).
- ✅ HTTP ingest того же контракта → send в ЛС (`max_bot.py`).
- ❌ Привязка группа/канал, список dest, отвязка (фаза 4).
- **Нет** wizard подписок, Premium UI, drops, schedule и пр.

### Связка TG ↔ Max — ещё не сделано

1. Max: пользователь получает код / ссылку.  
2. TG: dest Webhook → «Max» → код (или открыть Max).  
3. В БД: binding (`owner_tg`, `max_chat_id` / dest id, secret) **или** явный URL на `https://bot…/hooks/max/...`.  
4. Алерт → POST → Max → сообщение в чат.

---

## Контракт webhook (зафиксировано в коде)

| Требование | Деталь |
|------------|--------|
| Метод | `POST`, HTTPS only |
| Auth | HMAC-SHA256, заголовок `X-Bot-Signature: sha256=<hex>` по телу + secret назначения |
| Идемпотентность | `delivery_id` (UUID) + заголовок `X-Bot-Delivery-Id` |
| Тело | JSON `schema_version=1`: text / text_format (`plain`\|`html`), optional `photo_url`, `buttons[]` link-only, meta (`twitch_login`, `alert_type`, `stream_id`, …) |
| Ответ | `2xx` = успех; иначе retry/fail |
| SSRF | только `https`, block private/link-local/loopback/metadata IP |
| Лимиты | один retry на 429; Max ≤ 2 msg/s — на стороне приёмника (фаза 3) |

Модуль: `outbound_webhook.py`. Self_check: `self_check/checks_outbound_webhook.py`.

Кнопки callback «настроить в боте» в универсальном хуке не предполагать: только **link**.

---

## Модель данных

Telegram-сторона (**сделано**):

- `dest_type = webhook`
- `webhook_url`, `webhook_secret` (encrypted)
- pause после фатальных HTTP + notice владельцу

Max-сторона (**фаза 3 ✅**, группы/каналы — фаза 4):

- `max_users`, `max_destinations` (пока `kind=dm`)
- `max_link_codes` (код → max user/dest, TTL)
- `max_delivery_log` (`delivery_id` идемпотентность)

---

## Runtime

| Кусок | Статус |
|-------|--------|
| TG delivery `dest_type=webhook` | ✅ |
| `max_api.py` subscribe/list/unsubscribe/send/me | ✅ |
| Max процесс (receiver + send DM) | ✅ `max_bot.py` + `/hooks/max/*` |
| Health `/health/c/webhook_delivery` / `/health/c/max` | ❌ фаза 6 |
| PingZen монитор Max/hooks | ❌ фаза 6 |

---

## Фазы

| Фаза | Содержание | Статус |
|------|------------|--------|
| **0. Доступ Max** | Профиль ✅ → `@id760403963548_bot` опубликован ✅ → группы ✅ → без мини-приложения ✅ → `MAX_BOT_TOKEN` local+VPS ✅ → `GET /me` 200 ✅ | ✅ |
| **1. Webhook dest в TG** | UI dest, URL/secret, POST delivery, тест, SSRF, notice, i18n, flow-nav | ✅ в коде |
| **2. Контракт + docs** | schema + README + [`docs/outbound-webhook.md`](outbound-webhook.md) + [`examples/outbound_webhook_receiver.py`](../examples/outbound_webhook_receiver.py) | ✅ |
| **3. Max каркас** | таблицы, `/start`, link code, webhook receiver, send в ЛС | ✅ |
| **4. Max destinations** | группа/канал, права, отвязка | ❌ после 3 |
| **5. UX «Привязать Max»** | в TG wizard без ручного URL (код / deep link) | ❌ после 1+3 |
| **6. Ops** | health probes, PingZen, deploy, README Max-as-example | ❌ к релизу Max |

Дополнительно уже есть (вне фаз выше): клиент `max_api.py` под [POST/GET/DELETE /subscriptions](https://dev.max.ru/docs-api/methods/POST/subscriptions/) — готов к bootstrap после токена.

---

## Что осталось сделать (короткий backlog)

1. **Фаза 4:** dest группа/канал + права + отвязка в Max.
2. **Фаза 5:** в TG wizard «Привязать Max» (код / deep link) без ручного URL.
3. **Фаза 6:** `/health/c/…`, PingZen, maintenance при disruptive deploy.
4. Ops: `MAX_UPDATES_SECRET` в local+VPS если ещё не задан (нужен для subscribe).

Не в этом backlog: копия UI/Premium на Max, миграция TG→Max, рублёвая оплата.

---

## Сравнение со старым планом (копия UI)

| | **C: универсальный webhook** (этот файл) | Копия UI в Max (отменено здесь) |
|--|------------------------------------------|----------------------------------|
| Объём | недели после токена (Max) / раньше — только TG webhook | месяцы |
| Где настройка | только Telegram | Telegram + Max |
| Поддержка UI | один контур | два |
| Max без TG | нельзя настроить алерты | можно |
| Другие интеграции | да (любой URL) | нет |

---

## Не в scope

- Копия меню / wizard / Premium UI в Max  
- Миграция или обязательный линк всех TG пользователей в Max  
- Рублёвая оплата / Prodamus на Max  
- Сырой Twitch event в webhook вместо отрендеренного алерта  
- Callback-кнопки управления подпиской через чужой webhook  
- Вынос всего TG на Messenger protocol  

---

## Риски

- SSRF при пользовательских URL — ✅ схема + block private IP в `outbound_webhook`.  
- Чужие приёмники ломаются / дублируют — `delivery_id` + тест при setup; пример verify: `docs/outbound-webhook.md`.  
- Max 2 msg/s — очередь на приёмнике (фаза 3).  
- Пользователь ждёт «полноценный бот в Max» — продуктово это **доставка**, не второй UI.  
- Фаза 0 закрыта; фазы 3–5 — каркас Max receiver / UX.

---

## Definition of Done

- [x] В TG можно выбрать destination Webhook, указать URL, получить тестовый POST  
- [x] Боевой алерт уходит на URL с HMAC; retry/notice при фейле  
- [x] Документированный JSON-контракт + пример приёмника (verify) — `docs/outbound-webhook.md` + `examples/outbound_webhook_receiver.py`  
- [ ] Max-бот: онбординг, привязка ЛС/группы/канала, приём того же webhook, публикация  
- [ ] UX «Привязать Max» из TG без обязательного ручного URL  
- [x] SSRF-защиты + self_check; Telegram-регрессий нет (self_check green)  
- [x] README / guide / flow-map обновлены под Webhook (Max как пример — позже)  

---

## Чеклист старта реализации

- [x] Имя dest в UI: **Webhook** + i18n (ru/en/uk/it)  
- [x] JSON schema + заголовок подписи (`X-Bot-Signature`)  
- [x] (Max) профиль верифицирован; бот `@id760403963548_bot` опубликован; группы разрешены; без мини-приложения  
- [x] (Max) `MAX_BOT_TOKEN` local encrypted + VPS plaintext; `GET /me` 200 (`id760403963548_bot`)  

- [x] Не путать с отменённой «копией UI Max» — этот файл канон  
