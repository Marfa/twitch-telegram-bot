# TODO: Discord webhook как destination

Дата: 2026-11-10  
Статус: **дизайн, без реализации**  
Связанный код (целевой): `handlers/wizard.py` / `handlers/delivery.py` / `i18n.py` / `db/*` + новый `discord_webhook.py`  
Доки API: [Webhook Resource](https://docs.discord.com/developers/resources/webhook) (`Execute` / `Delete Webhook Message`, `wait`, `with_components`)  
Контекст чата: третья кнопка на шаге «куда публиковать»; настройка только в Telegram; в Discord уходит только оповещение (Incoming Webhook). Для этой подписки доставки в Telegram нет.

Связано по смыслу (другой мессенджер, другой scope): [`todo-max-messenger-bot.md`](todo-max-messenger-bot.md).

---

## Решение (зафиксировано)

| # | Решение |
|---|---|
| 1 | Транспорт: **Discord Incoming Webhook** (URL канала), не Discord-бот |
| 2 | Третья кнопка destination рядом с ЛС / чат |
| 3 | Одна destination на подписку: Discord **вместо** Telegram, не вместе |
| 4 | Free — без Premium-gate на сам destination |
| 5 | Webhook URL шифруется at rest (`encrypt_secret` / `try_decrypt_secret`) |
| 6 | Медиа: URL embed + multipart (GIF/MP4/`file_id` через Bot API download) |
| 7 | **Delete-previous** — да (`?wait=true` + `DELETE …/messages/{id}`, `last_message_id`) |
| 8 | **Custom URL-кнопки** (+ chat/remind как URL) — да (link buttons, `?with_components=true`) |
| 9 | **Pin** — нет (нужен Discord Bot + `PIN_MESSAGES`) → всегда `False` |
| 10 | Pause-callback / Telegram `web_app` — нет; только URL-эквиваленты |

---

## Главный вывод

Не отдельный Discord-продукт и не второй UI: только новый `dest_type` в существующем Telegram-мастере.  
Настройка webhook в ЛС бота → алерты POST в Discord. Pin и интерактивные кнопки webhook не умеет — не обещать в UX.

---

## Модель данных

Сейчас: `dest_type` + `chat_id` + `thread_id`.

Добавить:

- `dest_type = "discord"`
- `chat_id = 0`, `thread_id = None` (sentinel; `chat_id` остаётся `NOT NULL`)
- колонка `discord_webhook TEXT NOT NULL DEFAULT ''` — encrypted URL

Миграции ALTER в `db/sqlite.py` и `db/postgres.py`; поле в `Subscription`, `add_subscription` / `update_subscription` / mappers / `db/protocol.py`.

В UI/логах/analytics **никогда** не показывать полный URL (маска `…/webhooks/{id}/***`).

---

## UX мастера / edit

```
dest_prompt + dest_keyboard
  ├─ dest:dm        → finish DM
  ├─ dest:chat      → chat_setup → receive_dest_chat
  └─ dest:discord   → discord_setup (вставь webhook URL)
                      → validate + test POST
                      → delete_old если advanced (pin=off)
                      → finish
```

- URL только `https://discord.com/api/webhooks/{id}/{token}` или `discordapp.com`.
- Custom buttons уже в Extras до dest → на send мапятся в Discord link buttons.
- Edit destination: тот же путь (`start_edit_dest`).

---

## Доставка

Early path в `_send_notification` при `dest_type == "discord"`:

1. Decrypt webhook; пустой / undecryptable → pause + DM owner (unreachable), без утечки URL.
2. HTML шаблона → Discord markdown (`<b>`→`**`, `<i>`→`_`, `<a>`→`[text](url)`, остальное strip).
3. Медиа: публичный URL → embed; иначе bytes / `file_id` → multipart (~25 MB cap).
4. Delete-previous: `DELETE` старого `last_message_id`; POST с `?wait=true`, сохранить новый id. Sibling delete по тому же webhook + streamer.
5. Components: style `5` + `url` (цвета Telegram на link не переносятся).
6. 404/401 → unreachable; 429 → `Retry-After` / один retry.
7. Без pin / `note_outbound` DM path / Telegram send.

Модуль `discord_webhook.py`: validate, test, deliver text+file+components, delete by message id.

---

## Фазы

| Фаза | Содержание |
|------|------------|
| **1. Schema** | `discord_webhook` + model/mappers (sqlite + postgres) |
| **2. Module** | `discord_webhook.py`: validate, deliver, delete; HTML→markdown |
| **3. Wizard** | Третья кнопка, setup/test, delete_old без pin, edit dest |
| **4. Delivery** | Early path: media, delete-previous, URL components, errors |
| **5. Docs / checks** | locales, README, guide, user-flow-map, self_check; спросить flow-nav scenario |

---

## Не в scope (v1)

- [ ] Отдельный Discord-бот / OAuth / выбор канала в Discord UI  
- [ ] Двойная доставка Telegram+Discord на одну подписку  
- [ ] Pin в Discord  
- [ ] Интерактивные Discord-кнопки (callback) и Mini App `web_app`  
- [ ] Общий `/health/c/discord` (вебхуки пользовательские)  

---

## Риски

- Утечка webhook URL в логах/analytics — только маска.  
- Sibling delete: сравнивать webhook без логирования plaintext.  
- Telegram button styles не мапятся на Discord link buttons.  
- Файлы > ~25 MB — fallback на текст.  
- `TOKEN_ENCRYPTION_KEY` / bot-token-derived Fernet: смена ключа ломает decrypt → unreachable + notice.

---

## Definition of Done (Discord dest v1)

- [ ] На шаге destination есть Discord; после setup алерт уходит только в Discord  
- [ ] Медиа (URL / file_id / GIF/MP4 bytes) доезжает в канал  
- [ ] Delete-previous и custom URL-кнопки работают через webhook  
- [ ] Pin в Discord не предлагается и не включается  
- [ ] Webhook encrypted; полный URL не светится в UI/логах  
- [ ] README / guide / user-flow-map / self_check обновлены  
- [ ] Telegram DM/channel/group destinations без регрессий  

---

## Чеклист старта реализации

- [ ] Согласован текст setup (как создать webhook в Discord → вставить URL)  
- [ ] Схема `discord_webhook` + dual backend  
- [ ] `discord_webhook.py` + ветка в `delivery.py`  
- [ ] Wizard/edit + i18n en/ru  
- [ ] self_check: URL validate, markdown, mock POST (нет Telegram send)  
- [ ] Спросить про scenario в `checks_flow_nav.py` (§ destination)  
- [ ] После мержа предложить Viaduct: external system Discord Webhooks  
