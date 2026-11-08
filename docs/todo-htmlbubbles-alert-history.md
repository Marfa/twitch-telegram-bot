# TODO: HTMLBubbles для «История оповещений»

Дата: 2026-11-09  
Статус: **отложено (вариант B)** — не реализуем, пока Bot API 10.4 не в stable и/или не вырастут лимиты state  
Связанный код: [`handlers/alert_history.py`](../handlers/alert_history.py), [`telegram_widgets.py`](../telegram_widgets.py) (уже: channel stats + admin Premium digest)  
Контекст чата: оценка HTMLBubbles; для истории выбран **фоллбек = текущий текст**, без миграции сейчас.

---

## Решение

Историю **не** переводим на HTMLBubbles как drop-in. Текущий UX (страницы HTML до ~4000 символов, deep links `ah_v_` / `ah_u_` / `ah_vb_`, пагинация) остаётся каноном.

Уже на bubbles (с PNG/текст-фоллбеком): channel stats, admin Premium digest. История — отдельный backlog.

---

## Почему плохо ложится сейчас

| Ограничение HTMLBubbles | История сейчас |
|---|---|
| HTML после send **не** меняется; `chat_data` / ответ query ≤ **1024 байт** | Страницы до ~4000 символов с полным текстом алерта |
| Сжатый HTML ≤ 4 KB | Много строк + действия «просмотрено» в тексте |
| Bot API `sendWidget` пока **beta** (`TELEGRAM_WIDGET_API_ROOT`) | Стабильный `edit_message_text` + inline nav |

В 1 KB JSON влезает компактный список (время · логин · тип · viewed) **без** тел алертов — это другая UX, не замена.

---

## Когда вернуться

1. `sendWidget` / `editMessageWidgetData` в **stable** Bot API (не только `api.telegram.org/beta`).
2. И/или лимит `chat_data` заметно больше 1024 (или иной способ page payload).
3. Продуктовое согласие на **компактный** bubble-список + кнопка «текстом» / детали отдельным сообщением.

Пилот-форма (если лимиты вырастут): shell в HTML + страница в `chat_data` + `widget_query` вместо `/start ah_*`; фоллбек = нынешний `show_alert_history`.

---

## Не делать

- Не слать виджет истории без стабильного API / без явного флага и фоллбека.
- Не дублировать полные тела алертов в `chat_data` «через силу» (обрезка сломает смысл истории).
- Не путать с live-card / countdown / partner dashboard — те в отдельном бэклоге пилотов, не этот файл.

---

## Чеклист (когда снимем отложение)

- [ ] Stable Bot API 10.4 проверен на проде
- [ ] Согласован компактный UX (без полного текста в bubble) или новый лимит state
- [ ] `sendWidget` + фоллбек на текущий HTML-текст
- [ ] Mark viewed via `widget_query` (не только deep link)
- [ ] i18n + self_check на размер HTML/zlib/`chat_data`
- [ ] README / [`user-flow-map.ru.md`](user-flow-map.ru.md) §7
