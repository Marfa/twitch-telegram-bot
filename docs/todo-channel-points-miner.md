# TODO: Twitch Channel Points Miner как пользовательская фича

Источник оценки: [rdavydov/Twitch-Channel-Points-Miner-v2](https://github.com/rdavydov/Twitch-Channel-Points-Miner-v2)  
Дата: 2026-09-28  
Статус: анализ — **не делать** как продукт на текущем VPS; ToS/GQL — главный блокер  
Связано: [`todo-twitch-drops-miner.md`](todo-twitch-drops-miner.md) (тот же класс: AFK farm через неофициальный GQL)

---

## Контекст

TCPM v2 — скрипт AFK-«просмотра» стримов ради Channel Points: watch / watch streak, claim бонуса (+50), raid (+250), predictions/bets, moments, автоclaim drops, опционально join IRC. Без скачивания видео.

Стек апстрима:

- cookies / `auth-token` (не Helix user OAuth бота);
- неофициальный **`gql.twitch.tv`** (persisted queries: claim, minute-watched, raid, predictions, …);
- PubSub WebSocket (`wss://pubsub-edge.twitch.tv`);
- встроенный Client-ID веб/TV-клиента Twitch, не наше Helix-приложение.

Ограничение апстрима: **один процесс на один Twitch-аккаунт / cookies**; не multi-tenant из коробки. Авторы disclaimer: soft/hard ban.

У бота уже есть: алерты, follow-import (Helix OAuth scopes), Mini App чат (Helix/IRC send) — **без фарма очков**. Helix умеет manage rewards у броадкастера, не «смотреть за зрителя».

Политика репо (`api-license-compliance`): не использовать неофициальный `gql.twitch.tv`.

---

## Два варианта развёртывания

### A. Sidecar / worker pool + тонкий бот

- Оркестратор: start/stop, квоты, изоляция data-dir на user.
- В боте: кнопка, статус, уведомления (bonus/claim/prediction).
- Бот не майнит.

### B. Вся работа в процессе бота

- Логин/настройки/статус в Telegram UI.
- Майнинг в том же контейнере, что алерты.

---

## Текущий VPS (`bot.themarfa.name`)

Замер: 2026-09-28.

| Ресурс | Факт |
|--------|------|
| Хост | `vm-nano`, 2 vCPU |
| RAM | **1.8 GiB**, ~0.8 GiB available, **~435 MiB в swap** |
| Уже крутится | bot (limit 768, ~180 MiB), Postgres (512, ~270 MiB), FreshRSS, Vikunja, ghost-translator, Artalk, marfabot |
| Юзеры бота (порядок) | ~380 users, ~280 enabled subscriptions |
| CPU | почти свободен — узкое место RAM и один исходящий IP |

Один инстанс TCPM ≈ **десятки–~100+ MB** + долгоживущие WS/GQL (ниже типичного TDM 100–200 MB, но того же порядка при многих стримерах).

PubSub с одного IP: ~50 topics / connection, рекомендация Twitch ≈ **≤10 одновременных соединений** — жёсткий потолок concurrent mining с shared VPS.

**Вывод по текущему VPS:** как пользовательская фича — **не тянет**. Реалистично максимум **1 персональный** sidecar; даже 5–10 активных воркеров съедят остаток RAM и упрутся в IP/PubSub. Для продукта — отдельный хост + hard cap, и только после go/no-go по ToS.

---

## Сравнение вариантов

| Критерий | A (sidecar + тонкий бот) | B (всё в боте) |
|----------|--------------------------|----------------|
| Совпадение с TCPM | Высокое при форке/обвязке | Низкое: UX и cookies в чате |
| Multi-tenant | N воркеров + gateway | Тот же пул + давление на bot |
| Изоляция сбоев | Падение майнера ≠ алерты | Общий процесс / память с прод |
| Безопасность | Cookies в отдельном сервисе | Токены рядом с ботом и БД |
| Масштаб | Отдельный хост | Упирается в bot mem_limit 768 |
| Auth | Cookies/session, не наши Helix scopes | То же + хуже изоляция |
| Ресурсы на 1 юзера | ~50–150 MB на воркер | То же + риск OOM бота |

---

## Рекомендация

| Сценарий | Решение |
|----------|---------|
| Текущий VPS, личный фарм 1 аккаунта | Теоретически sidecar; **лучше не** рядом с публичным ботом (IP/риск Client ID) |
| Текущий VPS, фича для пользователей | **Не делать** |
| Отдельный мощный хост + ToS go | Только **вариант A** + hard cap; всё равно GQL/ToS |
| Вариант B | Не делать |
| Совместимый продукт без фарма | Документация / self-host «запусти miner сам»; бот не фармит |

Мощность снимает RAM, не ToS и не модель **1 user ≈ 1 воркер**.

---

## Целевая схема (A, только если go)

```
Telegram-бот          → кнопка → Telegram Login → JWT/session
Gateway (наш код)     → квоты Premium, start/stop, статус
Worker pool           → 1 процесс на активного user (TCPM или форк)
Notifications         → callback в основного бота
Storage               → изолированный data-dir (cookies); шифрование at rest
```

Бот не майнит. Стоковый TCPM не multi-tenant — нужна обвязка.

---

## Блокеры (апгрейд VPS не снимает)

1. **ToS Twitch** — Community Guidelines: cheat the rewards system (Drops / channel points). Риск бана аккаунтов пользователей и Helix-приложения бота. Главный продуктовый блокер.
2. **Политика репо** — запрет `gql.twitch.tv`; нужно явное исключение owner или отказ.
3. **Нет compliant Helix-пути** — watch/claim points не входят в документированный API для зрителя.
4. **1 user ≈ 1 воркер** + лимиты PubSub/IP — десятки активных максимум даже на 8–16 GiB.
5. **Секреты** — cookies/`auth-token` сильнее текущего refresh-token OAuth; отдельное хранение, revoke, не логировать.
6. **Форк/обвязка** — не `docker run` апстрима; оркестратор, квоты, Telegram auth.

---

## Открытые решения перед разработкой

- [ ] ToS go/no-go от owner (публичная фича vs только self-host docs / отказ)
- [ ] Исключение из правила «no gql.twitch.tv» или отказ от интеграции в этот репо
- [ ] Хост: апгрейд текущего VPS vs **отдельная** машина только под майнеры (рекомендуется отдельная)
- [ ] Лимиты: max активных воркеров, Premium gate (`FEATURE_IDS`?), idle timeout
- [ ] Хранение Twitch cookies/session: шифрование, TTL, revoke
- [ ] Связь с [`todo-twitch-drops-miner.md`](todo-twitch-drops-miner.md): один «farm hub» или раздельные решения
- [ ] Attribution / disclaimer (не affiliated; риск бана в UI)

---

## Черновик этапов (если go)

1. **Spike** — 1 воркер TCPM за proxy; замер RAM/CPU/соединений 24h; не на prod Helix Client ID.
2. **Оркестратор** — start/stop по `user_id`, volume, health, hard cap, mem_limit.
3. **Бот** — кнопка, статус, уведомления через основного бота.
4. **Premium / квоты** — gate + i18n + self_check.
5. **Ops** — метрики, runbook при бане/429, изоляция IP от Helix-бота.
6. **Docs** — user-facing risk notice.

Не делать до go/no-go по ToS и выбора **отдельного** хоста.

---

## Не делать в рамках этой фичи

- Встраивать фарм в `check_streams` / процесс алертов.
- Использовать прод `TWITCH_CLIENT_ID` / Helix app для GQL miner traffic.
- Multi-user майнинг на текущем 1.8 GiB VPS рядом с ботом.
- Хранить cookies в plaintext рядом с `bot.db` / без encrypt+revoke.
- Обещать «официальную» интеграцию Twitch — пути нет.
