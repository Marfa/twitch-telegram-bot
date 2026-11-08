# TODO: HTMLBubbles для «Статистика канала»

Дата: 2026-11-09  
Статус: **отложено** — канон снова **PNG-карточка**; bubble-пилот откатили (не совпал с дизайном)  
Связанный код: [`handlers/channel_stats.py`](../handlers/channel_stats.py) (`render_channel_stats_card`), [`telegram_widgets.py`](../telegram_widgets.py) (сейчас только admin digest)  
Связано: [`todo-htmlbubbles-alert-history.md`](todo-htmlbubbles-alert-history.md)

---

## Решение

Пользовательская статистика канала остаётся на **PNG** (аватар, 4 KPI в ряд, недельный график, игры + часы, footer).

HTMLBubble-попытка (2026-11-09) откатили: вёрстка не совпадала с дизайном — нет лого, обрезка текстов, 2×2 вместо 4 KPI, клип без scroll.

Админский Premium digest на bubbles **оставлен** (отдельный узкий UX).

---

## Почему bubble не тянет эту карточку

| Дизайн (PNG ~900×1200) | HTMLBubbles |
|---|---|
| Аватар с Twitch URL | Нет сети; `data:` съедает бюджет ≤4 KB zlib |
| Плотная типографика, 4 KPI + 6 баров + игры/часы | `chat_data` ≤ **1024 байт** → trunc / drop секций |
| Высота контента большая | Bubble ≤600 px, **без внутренней прокрутки** |

Пока лимиты такие — parity с PNG невозможна. Альтернативы позже: Mini App / оставить PNG / ждать рост state.

---

## Когда вернуться

1. Stable Bot API 10.4 + заметно больший `chat_data` **или** согласие на упрощённый dashboard (без аватара и без полного паритета PNG).
2. Либо отдельный Mini App под stats (сеть + нормальный layout).

---

## Чеклист (если снова пилотировать)

- [ ] Согласован упрощённый UX ≠ pixel-parity с PNG **или** другой канал доставки
- [ ] Аватар: skip / крошечный `data:` / ссылка вне bubble
- [ ] Фоллбек PNG обязателен
- [ ] self_check: zlib HTML + `chat_data` ≤ лимитов
- [ ] README + flow-map
