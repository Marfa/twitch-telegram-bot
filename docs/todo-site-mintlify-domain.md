# TODO: Публичный сайт на отдельный домен + guide на Mintlify + поисковики

Дата: 2026-11-12  
Статус: **ожидает имя нового домена и ручных шагов**  
Сайт сейчас: https://bot.themarfa.name/  
Было: `todo-search-engines.md` (влит сюда; файл удалён)  
Связано: [`webapp/guide/`](../webapp/guide/), [`web_site.py`](../web_site.py), [`webapp/site/`](../webapp/site/), `health.py`, `PUBLIC_BASE_URL`, PingZen monitors, `.cursor/rules/user-guide.mdc`, [Mintlify](https://www.mintlify.com/)

---

## Зачем

1. **Всё на отдельный домен** — один канонический origin вместо `bot.themarfa.name`: лендинг, guide, webhook-дока, placeholders/privacy, sitemap/robots **и** runtime (`/health`, OAuth, Mini App, EventSub и т.д.).
2. **Guide → Mintlify** — MDX/Git, поиск, i18n-навигация, agent-friendly docs вместо самописного HTML в `webapp/guide/` (на том же новом домене или path).
3. **Google / Яндекс** — индексировать публичные страницы на новом домене; 301 со старого хоста.

Порядок: имя домена → DNS/TLS cutover всего origin → Mintlify guide → SEO на каноническом URL.

---

## Решения

| # | Вопрос | Решение |
|---|--------|---------|
| 1 | Имя нового домена | ☐ ещё не выбрано |
| 2 | Scope переезда | **Всё** с `bot.themarfa.name` → новый домен (сайт + runtime). Не оставляем split origins. |
| 3 | Старый хост | 301 / DNS cutover на новый; `bot.themarfa.name` не остаётся отдельным API-origin |
| 4 | Guide host | Mintlify на новом домене (custom domain / path) |
| 5 | `PUBLIC_BASE_URL` | Один новый origin для OAuth, guide, лендинга, health-ссылок |
| 6 | SEO | GSC/Вебмастер на новом домене + 301 со всех старых URL |
| 7 | PingZen / status | Мониторы и status page перевести на новый `/health` (и связанные URL) |

---

## Фаза A — весь origin на отдельный домен

1. Выбрать имя домена; DNS A/CNAME → тот же VPS / reverse proxy, что обслуживает бота сейчас.
2. TLS на новом хосте (и при необходимости на старом на время 301).
3. Cutover **всего** HTTP-origin бота на новый домен:
   - лендинг `/`, `/guide`, `/webhook`, `/placeholders`, `/privacy`, `/sitemap.xml`, `/robots.txt`;
   - `/health`, `/health/c/*`, OAuth callbacks, Mini App `/app/*`, EventSub, PostHog webhooks и прочий runtime.
4. Обновить env/prod: `PUBLIC_BASE_URL`, Twitch OAuth redirect URIs, DonationAlerts/прочие callback URL, README, лендинг, `guide_url`, sitemap canonical, PingZen monitors / status page.
5. 301 (или DNS alias) со старого `https://bot.themarfa.name/*` → новый origin (все пути).
6. Проверить: публичный `/health` ok, OAuth round-trip, guide/лендинг, webhook-дока; старые закладки редиректятся.

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

*(из бывшего `todo-search-engines.md`; артефакты в коде: [`webapp/site/sitemap.xml`](../webapp/site/sitemap.xml), [`webapp/site/robots.txt`](../webapp/site/robots.txt) → `/sitemap.xml`, `/robots.txt`)*

Делать на **новом каноническом домене** (после фазы A или сразу с DNS). Старый `bot.themarfa.name` — только 301; не держать два равных SEO-origin.

### Google Search Console

1. [Google Search Console](https://search.google.com/search-console) под аккаунтом владельца.
2. Ресурс **URL-префикс** канонического origin **или** **Домен** `themarfa.name` (DNS на все поддомены).
3. Подтвердить владение: DNS TXT / HTML-файл / meta (не коммитить verification-секреты без нужды).
4. **Sitemaps** → `…/sitemap.xml` → «Успешно».
5. URL Inspection для `/` и guide (по желанию).
6. Coverage: нет лишнего `/health`, `/oauth/*`, `/app/*` (`Disallow` в robots).

### Яндекс.Вебмастер

1. [Яндекс.Вебмастер](https://webmaster.yandex.ru/).
2. Добавить канонический сайт.
3. Подтвердить права (тот же принцип, что у Google).
4. Sitemap → тот же `…/sitemap.xml`.
5. Переобход `/`, guide, `/webhook` при необходимости.
6. Зеркала / www — если появятся.

### Чеклист SEO

| Шаг | Статус |
|-----|--------|
| Канонический публичный домен выбран | ☐ |
| GSC ресурс + verified | ☐ |
| GSC sitemap принят | ☐ |
| Яндекс.Вебмастер + verified | ☐ |
| Яндекс sitemap принят | ☐ |
| После переезда: 301 + новый sitemap + старый ресурс не путает выдачу | ☐ |
| Сниппет `/` и guide в выдаче | ☐ |

---

## Не делать из агента без запроса

- Не покупать домен / не менять prod DNS без явного «да».
- Не заводить Mintlify org и не пушить docs-репо без согласования.
- Не публиковать verification tokens / DNS-секреты в чат или git.
- Не открывать в `robots.txt` `/health` или OAuth.
- Не слать `availability`-рассылку ради SEO; при DNS/TLS cutover runtime — оценить downtime (>60 с → спросить про `availability` + PingZen window по правилам).
- Не оставлять dual-write guide HTML + Mintlify после cutover.
- Не оставлять runtime на `bot.themarfa.name`, а сайт на другом домене — решение: **всё вместе**.

---

## Готово в репозитории (сейчас)

- Лендинг Dazzle: `/` (`webapp/site/index.html`), i18n + locale браузера
- `/guide`, `/webhook` в том же chrome (`webapp/guide/*.html`)
- `/sitemap.xml`, `/robots.txt`
- Справка из бота: `PUBLIC_BASE_URL/guide?lang=…`
