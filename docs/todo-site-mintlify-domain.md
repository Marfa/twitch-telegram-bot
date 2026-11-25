# TODO: Публичный сайт на отдельный домен + guide на Mintlify + поисковики

Дата: 2026-11-12 (обновлено 2026-11-25)  
Статус: **Phase A done** (осталось DA callback + ручная проверка OAuth после DNS). Phase B (Mintlify) и Phase C (GSC/Яндекс) — отдельно.  
Канонический origin: **https://twitchbot.games**  
Старый: https://bot.themarfa.name → **301** на новый origin  
Было: `todo-search-engines.md` (влит сюда; файл удалён)  
Связано: [`webapp/guide/`](../webapp/guide/), [`web_site.py`](../web_site.py), [`webapp/site/`](../webapp/site/), `health.py`, `PUBLIC_BASE_URL`, PingZen monitors, [`deploy/nginx/`](../deploy/nginx/), `.cursor/rules/user-guide.mdc`, [Mintlify](https://www.mintlify.com/)

---

## Зачем

1. **Всё на отдельный домен** — один канонический origin вместо `bot.themarfa.name`: лендинг, guide, webhook-дока, placeholders/privacy, sitemap/robots **и** runtime (`/health`, OAuth, Mini App, EventSub и т.д.).
2. **Guide → Mintlify** — MDX/Git, поиск, i18n-навигация, agent-friendly docs вместо самописного HTML в `webapp/guide/` (на том же новом домене или path).
3. **Google / Яндекс** — индексировать публичные страницы на новом домене; 301 со старого хоста.

Порядок: ✅ имя домена → ✅ DNS/TLS cutover всего origin → Mintlify guide → SEO на каноническом URL.

---

## Решения

| # | Вопрос | Решение |
|---|--------|---------|
| 1 | Имя нового домена | ✅ `twitchbot.games` (apex; `www` → 301 на apex) |
| 2 | Scope переезда | ✅ **Всё** с `bot.themarfa.name` → новый домен (сайт + runtime). Не split origins. |
| 3 | Старый хост | ✅ nginx 301 `https://bot.themarfa.name/*` → `https://twitchbot.games$request_uri` |
| 4 | Guide host | Mintlify на новом домене (custom domain / path) — **Phase B, отдельно** (пока HTML `/guide`) |
| 5 | `PUBLIC_BASE_URL` | ✅ `https://twitchbot.games` (VPS plaintext + local dotenvx) |
| 6 | SEO | GSC/Вебмастер на `twitchbot.games` + 301 — **Phase C, отдельно** |
| 7 | PingZen / status | ✅ monitors → `https://twitchbot.games/health` (+ `/health/c/*`); status title `twitchbot.games` (slug `bot-themarfa` пока) |
| 8 | DNS / CDN | ✅ Cloudflare full zone, **Proxied**, SSL Full (strict), Cache Bypass page rule; NS: `coco` / `jim`.ns.cloudflare.com |

---

## Фаза A — весь origin на отдельный домен

| # | Шаг | Статус |
|---|-----|--------|
| 1 | Домен + DNS A `@` → `152.114.195.134`, `www` CNAME → apex (CF) | ✅ |
| 2 | TLS: LE на VPS (`twitchbot.games`) + CF Universal SSL; Full strict; Proxied | ✅ |
| 3 | nginx dual: `deploy/nginx/twitchbot.games.conf` + `cloudflare-real-ip.conf`; старый vhost → redirect | ✅ |
| 4 | Cutover runtime: `PUBLIC_BASE_URL`, sitemap/robots/landing/README, PostHog webhook URLs | ✅ |
| 5 | Twitch OAuth Redirect URI `https://twitchbot.games/oauth/twitch/callback` (старый + localhost пока оставлены) | ✅ |
| 6 | DonationAlerts callback `https://twitchbot.games/oauth/donationalerts/callback` | ⏳ позже |
| 7 | 301 со старого хоста | ✅ |
| 8 | `/health` ok (PingZen UP via CF) | ✅ |
| 9 | Twitch OAuth round-trip на новом host | ⏳ после стабильной пропагации DNS у клиента |
| 10 | DA OAuth round-trip | ⏳ после п.6 |

**Заметки ops**

- NS у webnames: `coco.ns.cloudflare.com` / `jim.ns.cloudflare.com` (вместо nameself).
- Резолверы могут ещё кэшировать парковочный `88.212.206.251` — сброс DNS / `@8.8.8.8`; авторитетные CF NS отдают anycast.
- Samples: [`deploy/nginx/twitchbot.games.conf`](../deploy/nginx/twitchbot.games.conf), [`bot.themarfa.name-redirect.conf`](../deploy/nginx/bot.themarfa.name-redirect.conf), [`cloudflare-real-ip.conf`](../deploy/nginx/cloudflare-real-ip.conf).

---

## Фаза B — перенос guide на Mintlify

1. Mintlify Starter / Git-backed repo (или папка `docs/` в этом репо — без путаницы с внутренними `docs/todo-*`).
2. Перенести контент `webapp/guide/{ru,en,uk,it}.html` → MDX (TOC + секции, attribution IGDB где нужно).
3. Custom domain / path на **том же** новом домене из фазы A (не второй origin).
4. В боте: **Справка** / `guide_url` → новый URL; `disable_web_page_preview` как сейчас.
5. Обновить `.cursor/rules/user-guide.mdc` и `authority-map.md`: SoR guide = Mintlify MDX, не `webapp/guide/*.html`.
6. Старый `/guide` на origin — redirect на Mintlify-страницу (после cutover домена).
7. Self-check: не ходить в локальный HTML guide, если источник переехал.
8. Не дублировать долго два источника правды (HTML + Mintlify).

---

## Фаза C — Google Search Console и Яндекс.Вебмастер

*(из бывшего `todo-search-engines.md`; артефакты: [`webapp/site/sitemap.xml`](../webapp/site/sitemap.xml), [`webapp/site/robots.txt`](../webapp/site/robots.txt) → `/sitemap.xml`, `/robots.txt` — canonical уже `twitchbot.games`)*

Делать на **`https://twitchbot.games`**. Старый `bot.themarfa.name` — только 301.

### Google Search Console

1. [Google Search Console](https://search.google.com/search-console) под аккаунтом владельца.
2. Ресурс **URL-префикс** `https://twitchbot.games` **или** **Домен** `twitchbot.games` (DNS TXT).
3. Подтвердить владение: DNS TXT / HTML-файл / meta (не коммитить verification-секреты без нужды).
4. **Sitemaps** → `https://twitchbot.games/sitemap.xml` → «Успешно».
5. URL Inspection для `/` и guide (по желанию).
6. Coverage: нет лишнего `/health`, `/oauth/*`, `/app/*` (`Disallow` в robots).

### Яндекс.Вебмастер

1. [Яндекс.Вебмастер](https://webmaster.yandex.ru/).
2. Добавить `https://twitchbot.games`.
3. Подтвердить права (тот же принцип, что у Google).
4. Sitemap → `https://twitchbot.games/sitemap.xml`.
5. Переобход `/`, guide, `/webhook` при необходимости.
6. Зеркала / www — apex канон; www уже 301.

### Чеклист SEO

| Шаг | Статус |
|-----|--------|
| Канонический публичный домен выбран | ✅ `twitchbot.games` |
| 301 со старого хоста | ✅ |
| Sitemap/robots на новом origin в репо | ✅ |
| GSC ресурс + verified | ☐ |
| GSC sitemap принят | ☐ |
| Яндекс.Вебмастер + verified | ☐ |
| Яндекс sitemap принят | ☐ |
| После переезда: старый ресурс GSC не путает выдачу | ☐ |
| Сниппет `/` и guide в выдаче | ☐ |

---

## Не делать из агента без запроса

- Не покупать домен / не менять prod DNS без явного «да».
- Не заводить Mintlify org и не пушить docs-репо без согласования.
- Не публиковать verification tokens / DNS-секреты в чат или git.
- Не открывать в `robots.txt` `/health` или OAuth.
- Не слать `availability`-рассылку ради SEO; при DNS/TLS cutover runtime — оценить downtime (>60 с → спросить про `availability` + PingZen window по правилам).
- Не оставлять dual-write guide HTML + Mintlify после cutover.
- Не оставлять runtime на `bot.themarfa.name`, а сайт на другом домене — решение: **всё вместе** (Phase A выполнено).

---

## Готово в репозитории / проде (сейчас)

- Канон: `https://twitchbot.games` — лендинг, `/guide`, `/webhook`, `/placeholders`, `/privacy`, sitemap/robots, `/health*`, Mini App `/app/*`, OAuth/hooks
- `PUBLIC_BASE_URL=https://twitchbot.games` (prod + local encrypted `.env`)
- nginx samples + CF real_ip + old-host 301 sample в [`deploy/nginx/`](../deploy/nginx/)
- PingZen monitors на новый host; PostHog issue webhooks на новый host
- Справка из бота: `PUBLIC_BASE_URL/guide?lang=…`
- Guide пока HTML в `webapp/guide/` (Mintlify = Phase B)
