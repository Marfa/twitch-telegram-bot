# TODO: Скачивание / архивация публичных VOD (twitch-dlp)

Источник оценки: [DmitryScaletta/twitch-dlp](https://github.com/DmitryScaletta/twitch-dlp)  
Сравнение: [Zibbp/ganymede](https://github.com/Zibbp/ganymede) (отклонён как продукт; идеи очереди/UI — опционально позже)  
Дата: 2026-09-22 · обновлено: 2026-11-13  
Статус: **product scope зафиксирован** — ниже план фактической реализации; код не начат

---

## Решение

Интегрировать возможности **twitch-dlp** для **публичных** стримов/VOD.

**Явно не делаем:**

- Sub-only (нет user `auth-token` / cookies entitlement)
- Hidden / unpublished (нет CDN-path reconstruction, нет twitchtracker / streamscharts / sullygnome)

---

## Product scope (зафиксировано)

| Правило | Детали |
|--------|--------|
| Dedup | VOD **общие на стрим**: один `stream_id` / один публичный Twitch VOD → **одна** запись на сервере для всех пользователей |
| Retention на диске | **3 суток** с момента готовности файла (или с момента rehydrate с YouTube) |
| YouTube cold copy | После готовности — фоновый upload на канал владельца (credentials позже), **unlisted** («доступ только по ссылке»). Связь `local file ↔ youtube_video_id` в БД |
| После TTL | Файл с диска удаляется; по запросу — скачать копию с YouTube → снова отдать прямую ссылку с сервера → снова TTL 3 дня |
| Подписка «Запись стрима» | Premium / а-ля карт **`alert_types`** («другие типы оповещений»), как Drops / category / end |
| On-demand «Скачать VOD» | Free: **1 видео в календарную неделю**; Premium full plan — без лимита; а-ля карт отдельный id **`vod_download`** |
| Канал доставки алертов | Только **ЛС** (не канал/группа) |
| Публичность источника | Только публичный live/VOD; отказ при sub-only / 403 |

### Метаданные файла (mux / sidecar)

В контейнере (mkv/mp4 tags + chapters):

- имя стримера (login / display name)
- название стрима (title на старте; при смене title — по возможности обновить / зафиксировать финальный)
- attribution: создано при помощи бота (например `created_with=bot.themarfa.name` / comment)
- **главы** по сменам категорий: название категории + offset от начала записи

### Алерт «VOD готов» (ЛС)

```
Запись стрима готова          ← жирным
Стример: {name}               ← «Стример» жирным; имя — ссылка на канал
Стрим: {title}                ← «Стрим» жирным
{CategoryName}: {H}ч {M}м     ← только если были смены категорий; название жирным; время начала главы
VOD доступен по ссылке: {url} ← прямая download URL с нашего сервера
```

### История оповещений (строка со стримом)

Как сейчас для live, плюс action **VOD**:

```
ср, 11 ноября
• 19:55 — kinomaks 🙄
Стример kinomaks вышел в эфир с игрой Grand Theft Auto: Vice City Stories
https://twitch.tv/kinomaks
Перейти к стриму | VOD | Просмотрено | Просмотрено всё ниже
```

Нажатие **VOD** → бот присылает прямую ссылку на скачивание с сервера (rehydrate с YT при необходимости).

Фильтр в пагинации: кнопка **Показать только VOD** **перед** «К непросмотренным». Список — стримы/записи **с готовым VOD за всё время**, **без** окна 7/60 дней тарифа `alert_history`.

### UX: мастер «Новая подписка»

На экране выбора типа (рядом с Drops) — кнопка **Запись стрима**.

Шаги = как у **Начала стрима**, но:

| Шаг live | Запись стрима |
|----------|----------------|
| Канал Twitch | да |
| Куда слать | **фиксировано ЛС** (шаг выбора чата не нужен / только подтверждение ЛС) |
| Формат сообщения | **пропуск** |
| Дополнительно | только **Игнор ключевых слов** и **Категории** (остальные extras выкл / скрыты) |
| Сохранить | да |

Поведение runtime:

- Запись на сервере стартует **сразу при went_live** стримера (dedup-job), даже если у конкретного юзера фильтр категорий ещё не сработал — «на случай удаления VOD».
- Пользовательский алерт «готов» / постановка в очередь notify:
  - без категорий — после готовности файла по этому стриму;
  - с категориями — ловим **старт** и **смену категории на allowlist**; алерт о готовности шлём только если стрим (хотя бы в какой-то момент) попал под фильтр **и** подписка entitled.
- Ignore keywords — как у live (title/game), до отправки алерта о готовности.

### UX: 📦 Прочее

После **AI-клипы**:

1. **Скачать VOD** — пользователь присылает ссылку на стрим/VOD → job → прямая ссылка с сервера (тот же TTL + YouTube). Квота free / Premium / `vod_download`.
2. **Показать VOD** (если у пользователя уже были скачивания) — список как фильтр истории / те же download-ссылки.

---

## Блокеры перед стартом кода

1. **GQL** — twitch-dlp ходит на `gql.twitch.tv` за playback token / metadata. Политика (`api-license-compliance`) запрещает неофициальный GQL кроме уже оговорённого AI-clips chat. Нужно: **явное исключение в правиле** (как у `ai_clips.py`) + комментарий + self_check / upgrade path = официальный playback API, если появится.
2. **Диск / bandwidth** — полный VOD = гигабайты. Нужен отдельный volume / лимиты concurrent / max duration / мониторинг места. Текущий `mem_limit` bot-контейнера **не** тянет запись внутри процесса бота.
3. **YouTube credentials** — канал, OAuth refresh / service account, playlist — **укажет владелец позже**; в коде заложить интерфейс + env, без хардкода.
4. **ToS** — отдача файла только пользователям бота по signed/короткоживущей ссылке; не публичный CDN-листинг; disclaimer Twitch в copy.
5. Не путать с коротким preview (`stream_capture.py` + streamlink).

---

## План фактической реализации

### Фаза 0 — решения и инфраструктура

- [ ] License: exception в `api-license-compliance.mdc` для VOD worker (GQL только playback/metadata публичного VOD; не About/panels).
- [ ] Storage path на VPS (например `/var/lib/twitch-vod/` → volume в `compose.vps.yml`), отдельный sidecar/service `vod-worker`.
- [ ] Env: `VOD_STORAGE_DIR`, `VOD_TTL_SECONDS=259200`, `VOD_MAX_CONCURRENT`, `VOD_MAX_DURATION_HOURS`, `YOUTUBE_UPLOAD_*` (позже), `VOD_DOWNLOAD_SIGNING_SECRET` / nginx auth.
- [ ] Health: `/health/c/vod_worker` (+ позже youtube upload probe, если отдельный от multistream key).
- [ ] PingZen: при деплое worker с downtime — окно по правилам maintenance (типичный recreate ≤60s → без availability broadcast).

### Фаза 1 — модель данных (общий архив на стрим)

Таблицы (имена ориентировочные; dual SQLite/Postgres как в `db/`):

**`stream_vods`** — одна строка на стрим/архив:

| Поле | Смысл |
|------|--------|
| `id` | PK |
| `twitch_stream_id` | unique, Helix stream id |
| `twitch_vod_id` | Helix video id когда появится |
| `broadcaster_login` / `broadcaster_id` | |
| `title` | |
| `started_at` / `ended_at` | |
| `status` | `recording` / `processing` / `ready` / `uploaded` / `expired` / `failed` |
| `local_path` | nullable после purge |
| `local_ready_at` / `local_expires_at` | TTL 3 дня |
| `youtube_video_id` | unlisted |
| `youtube_url` | |
| `file_size_bytes` / `duration_sec` | |
| `chapters_json` | `[{name, start_sec}, …]` |
| `error` | last error |

**`stream_vod_chapters`** (опционально вместо JSON) — category name + `offset_sec` + `twitch_game_id`.

**`vod_notify_queue` / связь с подписками:**

- подписка: новый `alert_type` / флаг `notify_on_vod_record` (как drops), `chat_id` = user DM, ignore keywords, category filter.
- при `status=ready` — для каждого entitled подписчика стримера создать `alert_history` row `alert_type=vod` + отправить ЛС (dedup по `(user_id, stream_vod_id)`).

**`user_vod_downloads`** — on-demand / клики «VOD» / «Скачать VOD»:

| Поле | Смысл |
|------|--------|
| `user_id` | |
| `stream_vod_id` | FK |
| `source` | `alert` / `history` / `etc_download` / `etc_list` |
| `created_at` | для квоты «1 в неделю» |
| `last_link_issued_at` | |

Индекс: unique `(twitch_stream_id)` на `stream_vods`; unique notify `(owner_id, stream_vod_id)`.

### Фаза 2 — worker (запись + finalize + YouTube + TTL)

Отдельный процесс (не в PTB event loop):

1. **Enqueue on went_live** из `check_streams` / уже существующего live-detection: если ещё нет `stream_vods` для `stream_id` → создать `recording` и кинуть job (даже без подписчиков VOD — дешевле один раз; либо только если есть ≥1 активная vod-подписка **или** on-demand waitlist — выбрать в реализации: **предпочесть «есть хотя бы один заинтересованный»**, чтобы не писать все стримы мира; для подписки на конкретного стримера — всегда стартовать).
2. **Record**: twitch-dlp `--live-from-start` (или эквивалент) → сырой файл; параллельно трекать category changes из того же poll, что бот уже делает для `category` alerts → chapters.
3. **Finalize** on stream end / idle: remux, вписать tags + chapters (ffmpeg `metadata` / `chapters`), `status=ready`, выставить `local_expires_at = now+3d`, разбудить notify.
4. **YouTube upload** (фон): unlisted, title/description из метаданных + «via bot»; сохранить id; `status=uploaded` (local всё ещё `ready` до TTL).
5. **TTL janitor**: раз в N минут удалять local files с `local_expires_at < now`, path=null; YouTube id сохранить.
6. **Rehydrate**: если запрос download и local отсутствует, но есть `youtube_video_id` → скачать с YT → local → новый TTL 3 дня → выдать URL.
7. Очередь: лимит concurrent; backoff; partial fail → `failed` + admin/PostHog по желанию.

Прямые ссылки: `https://bot.themarfa.name/vod/{token}` или `/vod/d/{id}?sig=&exp=` — nginx/`health.py` route отдаёт файл; **не** directory listing.

### Фаза 3 — Premium / entitlement

| Id | Роль |
|----|------|
| `alert_types` | Создание/включение подписки **Запись стрима**; pause как другие non-live (`pause_unentitled_subscriptions`) |
| `vod_download` | Новый `FEATURE_IDS`: on-demand «Скачать VOD» без недельного лимита; full plan включает; free = 1/week без фичи |
| `alert_history` | Обычная история по-прежнему 7/60; **фильтр «только VOD» и download** — **без** этого окна (явное исключение в UI/query) |

Обновить:

- `premium.py` `FEATURE_IDS` + labels
- `locales` `premium_feat_vod_download` (+ `_desc`), тип алерта, кнопки
- `premium-beta-pause.mdc` pause map:  
  - `alert_types` — vod-подписки paused  
  - `vod_download` — on-demand недоступен / free quota 1/week остаётся как free tier (не путать с полной блокировкой)
- README / flow-map / `premium-feature-copy` checklist
- Beta: ветка `feat/vod-download`, `beta/manifest.json` при выкате за gate

Квота free on-demand: считать `user_vod_downloads` с `source=etc_download` (или любой issue link?) за ISO-week; **клик VOD в истории** у кого есть vod-подписка / уже был alert — **не** жечь квоту (уточнение: квота только для Прочее «Скачать VOD»; history VOD для получателей записи — без квоты). Зафиксировать в коде комментарием.

### Фаза 4 — мастер подписки + runtime алертов

- [ ] `i18n.alert_type_keyboard`: кнопка после Drops → `alert_type:vod_record` (имя ключа согласовать).
- [ ] Wizard branch в `handlers/wizard.py`: skip template/format; force DM; extras checklist только ignore + categories.
- [ ] Persist subscription fields (`notify_on_vod_record` / тип).
- [ ] `check_streams`:  
  - went_live → ensure `stream_vods` job;  
  - category change → append chapter + для vod-subs с filter проверить match;  
  - on ready → send «Запись стрима готова…» + `alert_history` type `vod`.
- [ ] Edit subscription: те же ограничения, что create; gate `alert_type_entitled`.

### Фаза 5 — История оповещений

- [ ] `_format_alert_history_block`: action link **VOD** если есть `stream_vod_id` / ready|uploaded.
- [ ] Deep-link / callback → issue download URL (rehydrate).
- [ ] `_alert_history_nav_keyboard`: кнопка **Показать только VOD** перед unwatched; query без day-window filter; пагинация.
- [ ] Хранение history rows для vod — не резать TTL истории раньше файла (строка может жить дольше файла; download идёт через `stream_vods`).

### Фаза 6 — Прочее

- [ ] Reply keyboard / `new_sub_other` / etc menu: после AI-клипы — **Скачать VOD**; условно **Показать VOD**.
- [ ] Conversation: принять URL (twitch.tv/…/videos/… или канал live) → Helix resolve → enqueue/reuse `stream_vods` → ждать ready → ссылка; квота.
- [ ] Список «Показать VOD» = `user_vod_downloads` пользователя (или все `stream_vods`, которые он когда-либо получал).

### Фаза 7 — i18n, docs, checks

- [ ] ru/en (+ остальные locale keys по процессу репо) для всех строк алерта и кнопок.
- [ ] Twitch disclaimer / not affiliated; attribution «создано ботом».
- [ ] `docs/user-flow-map.ru.md`, README.md / README.en.md.
- [ ] `self_check`: entitlement pause; dedup one row per stream; quota 2nd free download denied; history VOD filter ignores 7-day window; wizard skips format; chapters on category change.
- [ ] `checks_callback_wiring` / `checks_flow_nav` — новые кнопки.

### Порядок внедрения (рекомендуемый)

1. Фаза 0–1 (схема + worker skeleton + download route)  
2. Фаза 2 без YouTube (local only + TTL) → ручной E2E  
3. Фаза 4 подписка + notify  
4. Фаза 5 история  
5. Фаза 6 Прочее + квота  
6. YouTube upload + rehydrate  
7. Фаза 3 polish Premium copy / beta / docs  

---

## Целевые технические сценарии (twitch-dlp)

| # | Сценарий | Как |
|---|----------|-----|
| 1 | Live-запись с старта (подписка / интерес) | `--live-from-start` по channel URL |
| 2 | Докачка/финализация после конца | `https://www.twitch.tv/videos/{id}` если live-запись оборвалась |
| 3 | On-demand из Прочего | one-shot job по публичному video URL / id; reuse row если уже есть |

Discovery — **Helix** (`GetVideos` / streams), не scraping listing.

---

## Чеклист готовности к merge

- [ ] GQL exception в rules + комментарий в worker
- [ ] Dedup: один `stream_vods` на stream_id (self_check)
- [ ] TTL 3 дня + rehydrate с YouTube
- [ ] Подписка за `alert_types`; on-demand квота / `vod_download`
- [ ] Алерт copy + chapters
- [ ] История: VOD link + фильтр без tariff window
- [ ] Прочее: Скачать / Показать VOD
- [ ] Volume + worker в compose; health probe
- [ ] i18n, README, flow-map, premium pause map
- [ ] self_check green

---

## Out of scope (не возвращать без нового решения)

- Sub-only VOD
- Hidden / private / unpublished VOD через CDN path или сторонние трекеры
- Полноценный клон Ganymede (UI библиотеки, rendered chat)
- Запись **всех** стримов платформы без подписчиков
- Раздача файла в каналы/группы (только ЛС + download URL)
- Публичный YouTube (только unlisted)

---

## Открытые уточнения (не блокируют план, нужны до кода upload/квоты)

1. YouTube: channel id, OAuth app, кто владелец ролика при DMCA.
2. Квота free: только «Скачать VOD» из Прочего, или любой issue download-ссылки?
3. Стартовать recording, если на стримера нет vod-подписок, но кто-то может нажать VOD в обычной live-истории? (сейчас в плане: job при активной vod-подписке или on-demand; обычный live-history VOD = наш архив **или** fallback на twitch.tv/videos — **продуктово: кнопка VOD в истории ведёт на наш файл**, значит нужен архив; для стримов без vod-подписок кнопки VOD нет, пока кто-то не скачал через Прочее.)
4. Макс. длительность / качество (1080p60 vs 720p) — квота диска.
