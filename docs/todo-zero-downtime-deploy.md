# TODO: Zero-downtime deploy бота

Дата: 2026-10-15  
Статус: **дизайн, без реализации**  
Связанный код: `scripts/vps-deploy.sh`, `compose.vps.yml`, `main.py`, `bot.py` (`post_init` / `mark_ready`), `health.py`, `.github/workflows/deploy-vps.yml`  
Контекст: один инстанс на VPS, Telegram **long polling**, HTTP `:8080` (health / OAuth / EventSub / guide).

---

## Главный вывод

Сейчас деплой уже **минимизирует** простой (build при живом контейнере → `force-recreate` → wait `/health` → rollback), но это **не** zero downtime: между stop старого и `mark_ready()` нового есть окно ~5–30+ с, плюс рвутся in-flight хендлеры и **in-memory** мастера (`user_data` / ConversationHandler).

Настоящий ZDT для Telegram-бота на polling **невозможен** в классическом blue-green: два `getUpdates` → `409 Conflict`. Путь к «пользователь не замечает деплой»:

1. **Webhook** вместо polling (Telegram сам пушит updates).
2. **Blue-green** за reverse proxy (nginx) с cutover только после `/health == ok`.
3. **Graceful drain** старого инстанса (добить in-flight, потом stop).
4. **Persistence** состояния мастеров (иначе mid-wizard всё равно «зависнет» на новом процессе).
5. **Expand/contract** миграции схемы (старый и новый код кратко живут рядом).

Без (1)+(2) максимум — «near-zero» (секунды очереди Telegram). Без (4) мастера всё равно ломаются при любом recreate.

---

## Что есть сейчас (baseline)

| Кусок | Поведение | Влияние на UX |
|--------|-----------|----------------|
| `vps-deploy.sh` | `build` при живом bot → `up --force-recreate bot` | Gap на recreate + cold start |
| `drop_pending_updates=False` | Updates копятся у Telegram на время gap | Кнопки «доезжают» после старта, но с лагом |
| `mark_ready()` в `post_init` | `/health` = ok только после Application init | Deploy не режет traffic раньше polling |
| `stop_grace_period: 25s` | Docker ждёт SIGTERM | Нет явного drain хендлеров / stop polling |
| Job restore | `restore_pending_alert_jobs` / AI clips / broadcast в `post_init` | Фон переживает restart из БД |
| Conversation / `user_data` | В памяти процесса | Mid-wizard после деплоя — мёртвое состояние |
| Порт `8080:8080` | Один контейнер | Нельзя поднять второй bot на том же host-порту |
| OAuth / EventSub | Тот же HTTP | Во время gap callback’и падают |

Целевое UX: деплой **не** даёт «бот не отвечает», не рвёт оплату Stars mid-flow, не сбрасывает мастер подписки, не ломает OAuth callback.

---

## Жёсткие ограничения

1. **Telegram Bot API** — один consumer long polling на токен. Два контейнера с `run_polling` = Conflict (уже ловится в `error_handler`).
2. **Один публичный origin** (`PUBLIC_BASE_URL`) — OAuth redirect, EventSub, guide, webhooks.
3. **Один Postgres** — оба инстанса (на время cutover) пишут в одну БД → миграции только совместимые.
4. **VPS RAM** (~bot 768 MiB + db 512 MiB) — два полных bot на время blue-green нужны ~+768 MiB peak; иначе только near-zero на одном слоте.
5. **Фоновые джобы** (`check_streams`, preview refresh, dumps) — два инстанса без лидера = дубли алертов. Нужен leader lock или «jobs only on active».

---

## Целевая архитектура

```
Telegram ──POST /telegram/webhook──► nginx
                                      │
                         ┌────────────┴────────────┐
                         ▼                         ▼
                   bot-blue :8080            bot-green :8081
                         │                         │
                         └──────────┬──────────────┘
                                    ▼
                              Postgres (db)
```

- Снаружи один URL; nginx `upstream` указывает на **active**.
- Новый релиз поднимается как **standby**, ждёт `/health == ok`, затем upstream flip.
- Старый получает `SIGTERM` → drain → stop.
- Фоновые poll’ы Helix / JobQueue — **только на active** (env `BOT_ROLE=active|standby` или Postgres advisory lock).

### Почему webhook, а не dual-polling

| | Polling | Webhook |
|--|---------|---------|
| Два инстанса | Conflict | Оба могут слушать HTTP; Telegram бьёт в один URL (nginx) |
| Gap при recreate | Updates в очереди Telegram, UX ждёт | Cutover атомарный на proxy |
| Локальная отладка | Проще | Нужен публичный HTTPS (уже есть prod) |

---

## План реализации (фазы)

### Фаза 0 — Near-zero без смены модели (дешёвый выигрыш)

Сделать, если ZDT откладывается, но деплои раздражают:

1. **Явный graceful shutdown** на SIGTERM:
   - `application.stop()` / прекратить `getUpdates`;
   - дождаться in-flight handlers (с таймаутом &lt; `stop_grace_period`);
   - закрыть DB / analytics.
2. **Warm start**: не гонять тяжёлое в `post_init` до `mark_ready` (меню кнопок чатов уже в `create_task` — держать так).
3. **Deploy gate**: `force-recreate` только после успешного `build`; опционально `docker compose up` с pre-pulled image (уже почти так).
4. Метрика gap: timestamp stop → first `/health ok` в лог / PostHog.

Ожидание: gap ↓ до нескольких секунд; мастера mid-flow всё ещё сбрасываются.

### Фаза 1 — Persistence мастеров (нужна при любом recreate)

Без этого «zero downtime» врёт пользователю в середине wizard:

1. Включить PTB persistence (Postgres или файл на volume) для `user_data` / conversation state.
2. Self-check: recreate контейнера mid-wizard → следующий апдейт продолжает шаг.
3. Не персистить секреты (токены OAuth уже в DB encrypted).

Альтернатива попроще, но хуже UX: на старте слать «сессия сброшена, открой меню» — **не** цель этого TODO.

### Фаза 2 — Webhook + nginx blue-green (настоящий ZDT)

1. **nginx** (или существующий фронт на VPS) на `bot.themarfa.name`:
   - `/` → active bot upstream;
   - health у nginx отдельно от app (не резать OAuth из-за streams_stale).
2. **Compose**: сервисы `bot-blue` / `bot-green` (или один сервис + второй ephemeral), разные внутренние порты; host publish только через nginx.
3. **Код**:
   - `run_webhook` (или `Updater.start_webhook`) на path `/telegram/webhook` + secret token;
   - `setWebhook` один раз на `PUBLIC_BASE_URL/telegram/webhook`;
   - убрать `run_polling` в prod (оставить polling для локалки через env `TELEGRAM_MODE=polling|webhook`).
4. **`scripts/vps-deploy.sh`**:
   - определить idle цвет;
   - build + up idle;
   - wait idle `/health`;
   - nginx reload → idle = active;
   - drain + stop старого;
   - rollback = flip upstream назад + recreate failed color.
5. **Jobs**: только active (`BOT_ROLE` или `pg_try_advisory_lock`); standby отвечает на webhook/OAuth, но не шлёт stream alerts.
6. **EventSub / OAuth / PostHog hooks** — те же upstream’ы; на cutover не теряются.

### Фаза 3 — Безопасные миграции БД

Пока blue и green кратко сосуществуют:

| Правило | Пример |
|---------|--------|
| Expand | добавить колонку nullable / новую таблицу |
| Deploy | новый код читает оба формата |
| Contract | позже дропнуть старое (отдельным релизом) |

Запрещено в том же релизе, что и dual-run: rename column, drop column, NOT NULL без default, destructive backfill без совместимости.

Отдельный pre-deploy шаг: `migrate --expand` до cutover; `migrate --contract` после стабилизации (или ручной follow-up).

### Фаза 4 — Проверки и наблюдаемость

1. Self-check / smoke: deploy script dry-run; health на standby; Conflict невозможен (нет второго polling).
2. PostHog / лог: `deploy_cutover_ms`, `inflight_drained`, `webhook_4xx` во время cutover.
3. Сценарий ручной: mid-wizard callback, Stars `pre_checkout_query`, OAuth return, live alert — всё проходит через cutover.
4. Документация: README deploy section + этот файл → «сделано» когда фазы 1–2 в проде.

---

## Что сознательно не делать

- **Два polling-инстанса с «быстрым handoff»** — гонка Conflict, хрупко.
- **Kubernetes rolling** на текущем VPS — overkill; compose + nginx достаточно.
- **Aiven как live** во время деплоя — cold DR, не SoR ([`vps-db-query.mdc`](../.cursor/rules/vps-db-query.mdc)).
- **Скрывать mid-wizard loss** «очередью Telegram» — updates приедут, state уже мёртв.

---

## Порядок работ (когда брать в спринт)

| # | Задача | Зависимости | Оценка риска |
|---|--------|-------------|--------------|
| 1 | Graceful SIGTERM drain (фаза 0) | нет | низкий |
| 2 | Persistence conversation/`user_data` (фаза 1) | PTB API, volume/DB | средний (сессии) |
| 3 | `TELEGRAM_MODE=webhook` + path + secret (фаза 2 код) | PUBLIC_BASE_URL HTTPS | средний |
| 4 | nginx upstream blue/green + deploy script (фаза 2 ops) | RAM на 2× bot, nginx | высокий (ops) |
| 5 | Leader lock для JobQueue / check_streams | Postgres | средний |
| 6 | Expand/contract migrate policy + hook в deploy | схема db/ | средний |
| 7 | Метрики cutover + runbook rollback | 2–4 | низкий |

Минимальный осмысленный пакет для «пользователь не зависает»: **1 + 2 + 3 + 4**. Без 2 деплой «бесшовный» только для stateless тапов (меню, список), не для мастеров.

---

## Критерии готовности (Definition of Done)

- [ ] Деплой на VPS без окна, где бот не отвечает на callback / message &gt; ~1 s (кроме сетевых сбоев Telegram).
- [ ] Mid-wizard + Stars checkout + OAuth callback переживают cutover.
- [ ] Нет `409 Conflict` в логах при деплое.
- [ ] Нет двойных stream-алертов (leader / active-only jobs).
- [ ] Rollback = flip upstream &lt; 30 s без rebuild, если новый цвет unhealthy.
- [ ] `python -m self_check` покрывает webhook mode wiring + persistence smoke (хотя бы статически / mini-scenario).

---

## Связанные уже сделанные куски (не переписывать зря)

- Build-before-recreate и rollback по предыдущему image — `scripts/vps-deploy.sh`.
- `/health` не ok до `mark_ready()` — `bot.py` + `health.py`.
- `drop_pending_updates=False` — `main.py`.
- Restore alert / AI clips / broadcast jobs после старта — `post_init`.

Эти остаются полезны и после перехода на webhook (health gate, rollback image, job restore на active).
