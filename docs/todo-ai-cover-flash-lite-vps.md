# TODO / заметка: gemini-3.1-flash-lite-image и VPS

Дата: 2026-09-26  
Статус: **реализовано через BotHub API** (не self-host модели на VPS)  
Связанный код: `bothub.py`, `handlers/delivery.py`, таблица `ai_game_covers`, env `BOTHUB_*`

---

## Главный вывод

**`gemini-3.1-flash-lite-image` (Flash Lite / Nano Banana Lite) на VPS разворачивать не нужно.**

Модель крутится у провайдера (BotHub → Google). Бот на `bot.themarfa.name` только:

1. дергает OpenAI-совместимый HTTP API (`BOTHUB_BASE_URL` + `BOTHUB_API_KEY`);
2. один раз сохраняет JPEG/PNG в Postgres (`ai_game_covers` по Twitch `game_id`);
3. при следующих алертах той же категории читает байты из БД — без повторной генерации.

Self-host той же модели (GPU, TPU, vLLM, Ollama и т.п.) — **другой проект**: другие деньги, диск, driver stack и ToS. Текущий VPS (≈2 CPU, ≈1.8 GiB RAM, bot `mem_limit` 768 MiB) для локального инференса Flash Lite **не подходит**.

---

## Что реально нужно на VPS

| Ресурс | Нужно для текущего контура | Комментарий |
|--------|----------------------------|-------------|
| CPU | Низкий | HTTP + запись/чтение BYTEA; генерация в `asyncio.to_thread`, event loop не крутит инференс |
| RAM (bot) | +десятки MiB пик | Буфер ответа BotHub (~0.2–1.5 MB на картинку) + пул Postgres |
| RAM / disk (db) | ~0.2–0.5 MB × число категорий | Сейчас 1 cover ≈ **186 KB**; 1000 категорий ≈ **200 MB** в `ai_game_covers` |
| GPU | **Нет** | Модель не локальная |
| Сеть egress | Да | HTTPS к `bothub.chat` (timeout до 120 с на первую генерацию категории) |
| Секреты | `BOTHUB_API_KEY`, опционально `BOTHUB_*` | Не коммитить; на VPS уже в `.env` |

Снимок VPS на 2026-09-26: 2 vCPU, ~1.8 GiB RAM, bot ~88 MiB / 768 MiB limit, db ~367 MiB / 512 MiB — запас для кэша обложек есть, для локальной LLM/image-модели — нет.

---

## Техническая реализация (как сделано)

```
Алерт с image_file_id = __ai_game_cover__
        │
        ▼
delivery → bothub.generate_alert_cover_bytes(db=…)
        │
        ├─ SELECT ai_game_covers WHERE twitch_game_id = Helix game_id
        │     и model == BOTHUB_IMAGE_MODEL  →  send_photo(bytes)
        │
        └─ miss → BotHub images/generations (Flash Lite, size 1280×720)
                  → upsert ai_game_covers
                  → send_photo(bytes)
```

| Слой | Детали |
|------|--------|
| Модель по умолчанию | `gemini-3.1-flash-lite-image` (`config.BOTHUB_IMAGE_MODEL`) |
| Размер запроса | `1280×720` + `aspect_ratio=16:9` (API может отдать ~1376×768) |
| Промпт | Категория + IGDB summary; **без** логина стримера; жёсткий no-text |
| Ключ кэша | Twitch Helix `game_id` (пример: Games + Demos = `66082`) |
| Таблица | `ai_game_covers (twitch_game_id PK, game_name, image_bytes, content_type, model, created_at)` |
| Dual backend | `db/sqlite.py` + `db/postgres.py` + `db/protocol.py` |
| Concurrency | per-`game_id` `threading.Lock` — двойной cold-start не бьёт BotHub дважды |
| Смена модели | если `model` в строке ≠ текущему env — регенерация и overwrite |

Обычная обложка игры (`__game_cover__`) по-прежнему URL из IGDB dumps / Twitch box art — **отдельный путь**, рядом в продукте, не в той же строке таблицы.

---

## Если когда-нибудь понадобится self-host

Не делать на текущем bot VPS. Минимальный ориентир (порядок величины, не смета):

- GPU с ≥8–16 GB VRAM (зависит от рантайма/квантизации),
- отдельный хост / контейнер с image-serving API,
- свой rate-limit и кэш (можно оставить ту же `ai_game_covers`),
- проверка лицензии весов Google / BotHub (часто **нельзя** просто «скачать Flash Lite и крутить у себя»).

До того момента self-host в backlog **не планируем**.

---

## Чеклист сопровождения

- [x] BotHub Flash Lite + size 1280×720
- [x] Кэш `ai_game_covers` + get-or-generate в delivery
- [x] Prefill Games + Demos (`66082`) с эталонного JPEG
- [x] VPS/local `BOTHUB_IMAGE_MODEL=gemini-3.1-flash-lite-image`
- [ ] (опционально) TTL / лимит размера таблицы, если категорий станут десятки тысяч
- [ ] (опционально) админ-команда «сбросить AI cover для game_id»
- [ ] **Не** ставить GPU-рантайм Flash Lite на bot VPS
