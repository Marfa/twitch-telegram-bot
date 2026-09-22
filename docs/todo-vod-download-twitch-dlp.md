# TODO: Скачивание / архивация публичных VOD (twitch-dlp)

Источник оценки: [DmitryScaletta/twitch-dlp](https://github.com/DmitryScaletta/twitch-dlp)  
Сравнение: [Zibbp/ganymede](https://github.com/Zibbp/ganymede) (отклонён как продукт; идеи очереди/UI — опционально позже)  
Дата: 2026-09-22  
Статус: scope зафиксирован, реализация не начата

---

## Решение

Интегрировать возможности **twitch-dlp** для **публичных** стримов/VOD.

**Явно не делаем:**

- Sub-only (нет user `auth-token` / cookies entitlement)
- Hidden / unpublished (нет CDN-path reconstruction, нет twitchtracker / streamscharts / sullygnome)

---

## Целевые сценарии

| # | Сценарий | Как (twitch-dlp) |
|---|----------|------------------|
| 1 | Live-запись на случай удаления VOD после эфира | `--live-from-start` по channel URL (растущий публичный VOD) и/или streamlink «с now» |
| 2 | Скачивание VOD после окончания стрима | `https://www.twitch.tv/videos/{id}` |
| 3 | Скачивание VOD по запросу пользователя | one-shot job по публичному video URL / id |

Discovery публичных VOD — через **Helix** (`GetVideos` и т.п.), не через обход listing.

---

## Блокеры перед реализацией

1. **GQL** — twitch-dlp ходит на `gql.twitch.tv` за playback token / metadata даже для публичных VOD. Политика репо (`api-license-compliance`) запрещает неофициальный GQL. Нужно: явное исключение в правиле **или** иной compliant путь к манифесту.
2. **Storage / bandwidth** — полные VOD = гигабайты; текущий VPS бота не рассчитан на multi-tenant архив. Нужны квоты, TTL, отдельный диск/хост или self-serve «скачай сам».
3. **ToS / redistribution** — отдача чужого видео через бота близка к перераспространению Program Materials; уточнить модель (ссылка на файл только владельцу подписки? временный download? только self-host?).
4. **Уже есть** короткий live-capture для превью (`stream_capture.py` + streamlink) — не путать с полным архивом.

---

## Черновик архитектуры (когда начнём)

- Worker/очередь (отдельный процесс или sidecar), не в основном bot-процессе с `mem_limit`.
- Триггеры: live start → optional live-from-start; stream end / Helix new archive → VOD job; user command → on-demand.
- Только публичные URL; отказ при 403 / sub-only / missing listing.
- Лимиты: размер, длительность, concurrent jobs, retention.
- Пользовательский UX в Telegram: статус job, прогресс, готовый файл/ссылка или «недоступно публично».

---

## Чеклист реализации (позже)

- [ ] Решение по GQL / license exception
- [ ] Модель хранения и квот
- [ ] Product UX (кто может скачивать, куда отдаём файл)
- [ ] Обёртка над twitch-dlp (или перенос нужных режимов) + ffmpeg
- [ ] Helix discovery для сценариев 2–3
- [ ] Live-from-start worker для сценария 1
- [ ] i18n, Premium-гейт (если платно), self_check
- [ ] Attribution / disclaimer Twitch в user-facing copy

---

## Out of scope (не возвращать без нового решения)

- Sub-only VOD
- Hidden / private / unpublished VOD через CDN path или сторонние трекеры
- Полноценный клон Ganymede (UI библиотеки, rendered chat) — не обязателен для MVP
