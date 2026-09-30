# TODO / заметка: DeepL → LibreTranslate на VPS

Дата: 2026-09-30  
Статус: **анализ, без реализации**  
Связанный код: `translate.py`, `config.DEEPL_API_KEY`, callers в `handlers/broadcast.py`, `handlers/monitoring.py`, `handlers/drops.py`, `twitch.localize_igdb_summary`  
Связано: [`todo-ai-cover-flash-lite-vps.md`](todo-ai-cover-flash-lite-vps.md) (тот же VPS: ≈2 vCPU, ≈1.8 GiB RAM)

---

## Главный вывод

Миграция **простая в коде** (один адаптер `translate.py` + compose/env), но **дорогая по RAM** и **хуже по качеству**, чем DeepL Free/Pro.

Имеет смысл только если цель — убрать внешний DeepL **и** на VPS есть **≥ ~2 GB свободной RAM** после `bot`+`db`. На снимке 2026-09-26 VPS ≈ **1.8 GiB** суммарно при лимитах bot 768 MiB + db 512 MiB — **sidecar LibreTranslate на том же хосте сейчас не влезает** без апгрейда VPS. Иначе оставить DeepL.

---

## Что сейчас переводит DeepL

Единая точка: `translate.py` → `translate_text` / `build_translations`. Без `DEEPL_API_KEY` текст возвращается как есть.

| Место | Направление | Режим |
|--------|-------------|--------|
| Админ-рассылка `handlers/broadcast.py` | source → локали получателей | HTML (`<b>`, `<a>`, …) |
| PostHog issues `handlers/monitoring.py` | EN → RU | имя plain; описание HTML |
| IGDB summary `twitch.localize_igdb_summary` | EN → ru/uk/it | plain only (`preserve_html=False`) + L1/L2 cache |
| Drops how-to `handlers/drops.py` | EN → locale | plain → потом `html.escape` |

Локали бота: `en`, `ru`, `uk`, `it`. DeepL-коды: `EN`/`EN-US`, `RU`, `UK`, `IT`.

```
broadcast / PostHog / IGDB / drops
        │
        ▼
   translate.py
        │
        ▼
   DeepL API (cloud)
```

---

## Что даёт LibreTranslate

- Self-hosted Docker `libretranslate/libretranslate`, движок Argos Translate.
- Языки `en` / `ru` / `uk` / `it` есть в каталоге.
- API: `POST /translate` JSON `{q, source, target, format: "text"|"html"}` — HTML помечен как **beta**.
- **`LT_LOAD_ONLY=en,ru,uk,it` обязателен** (иначе 30+ языков → 8 GB+ RAM).
- Volume для моделей (иначе re-download при каждом recreate).
- Порт наружу не публиковать — только Docker-сеть к `bot`.

Практический бюджет LT для 4 языков: **~1.5–2.5 GB** steady + диск моделей + медленный first boot.

---

## Качество и поведение

| Аспект | DeepL (сейчас) | LibreTranslate |
|--------|----------------|----------------|
| Качество EN↔RU / UK | сильное | заметно слабее (особенно uk, игровой жаргон IGDB) |
| HTML для Telegram | зрелый `tag_handling=html` | `format=html` beta — риск поломки тегов/ссылок |
| Plain text entities | обойдён (`preserve_html=False`) | обычно ок; проверить апострофы |
| Latency | сеть + API, обычно &lt;1–2 s | CPU NMT: секунды; broadcast ×3 locale может упереться в timeout |
| Availability | внешний SaaS | свой контейнер; OOM → fallback на исходный текст |
| Стоимость | Free/Pro quota | $0 API, платишь RAM/CPU |

Кэш IGDB (`igdb_summary_translation`) останется: старые DeepL-строки не пересчитаются; новые пойдут через LT. Рассылки/дропы без кэша сразу увидят смену качества.

---

## Целевая схема (если когда-нибудь идти)

Жёсткая замена без DeepL-fallback:

```
bot ──internal :5000──► libretranslate
bot ──────────────────► db
```

1. **Compose** — сервис в `compose.vps.yml`: image LT, `LT_LOAD_ONLY=en,ru,uk,it`, `LT_DISABLE_WEB_UI=true`, `LT_UPDATE_MODELS=false`, `LT_THREADS=1|2`, volume моделей, `mem_limit` ≥ 2g, без host ports.
2. **Config** — вместо `DEEPL_API_KEY`: `LIBRETRANSLATE_URL=http://libretranslate:5000` (пусто = no-op).
3. **Код** — в основном `translate.py` (+ `.env.example`, README, `docs/authority-map.md`, rule `twitch-client-secret.mdc`). API `translate_text` / `build_translations` не менять.
4. **Verify** — `/languages`, `/translate` en→ru/uk/it text+html из контейнера `bot`; smoke рассылки и IGDB `{game_description}`.
5. **Viaduct** — при реализации: DeepL в authority-map → container LibreTranslate.

Объём кода маленький; риск — **ops/ресурсы**, не refactor.

---

## Когда не стоит менять

- VPS без запаса ~2 GB RAM (текущий ≈1.8 GiB — уже стоп)
- Критичны качество админ-рассылок и IGDB-описаний на ru/uk
- DeepL Free хватает по квоте (перевод не hot-path: рассылки редки, IGDB кэшируется)

---

## Prefight перед реализацией

- [ ] `free -h` и `docker stats` на `bot.themarfa.name`
- [ ] Пробный one-shot LT с `LT_LOAD_ONLY=en,ru,uk,it` — RSS после load + latency en→ru (IGDB prose + HTML рассылки)
- [ ] Go / no-go по RAM и qualitatively acceptable на ru/uk
- [ ] При go: апгрейд VPS **или** отдельный хост — не пытаться втиснуть в текущие 1.8 GiB

---

## Итог

| | |
|---|---|
| Код | Низкая сложность |
| Ops | Высокая: +1.5–2.5 GB RAM, volume, slower CPU translate |
| Качество | Регрессия vs DeepL, HTML beta |
| Рекомендация | **Не мигрировать на текущем VPS.** Сначала апгрейд/замер; иначе оставить DeepL |

До go/no-go после prefight реализацию **не планируем**.
