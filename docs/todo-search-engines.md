# TODO: Добавить сайт в Google Search и Яндекс.Вебмастер

Дата: 2026-11-10  
Статус: **ожидает ручных шагов** (нужны аккаунты владельца домена)  
Сайт: https://bot.themarfa.name/  
Артефакты в коде: [`webapp/site/sitemap.xml`](../webapp/site/sitemap.xml), [`webapp/site/robots.txt`](../webapp/site/robots.txt) → публично `/sitemap.xml`, `/robots.txt`

---

## Зачем

Лендинг, `/guide`, `/webhook` должны индексироваться. Sitemap и robots уже отдаются ботом; осталось подтвердить владение в консолях поисковиков и отправить sitemap.

---

## Чеклист

### Google Search Console

1. Открыть [Google Search Console](https://search.google.com/search-console) под аккаунтом владельца.
2. Добавить ресурс типа **URL-префикс**: `https://bot.themarfa.name/`  
   (или **Домен** `themarfa.name`, если DNS-верификация удобнее для всех поддоменов).
3. Подтвердить владение одним из способов:
   - DNS TXT у регистратора / Cloudflare; или
   - HTML-файл / meta-тег (если выбран URL-префикс — согласовать с деплоем, не коммитить секреты верификации в публичный репозиторий без нужды).
4. **Sitemaps** → добавить `https://bot.themarfa.name/sitemap.xml` → дождаться «Успешно».
5. Запросить индексирование главной и `/guide` через URL Inspection (по желанию).
6. Проверить Coverage / Pages через несколько дней: нет лишнего `/health`, `/oauth/*`, `/app/*` (они в `Disallow`).

### Яндекс.Вебмастер

1. Открыть [Яндекс.Вебмастер](https://webmaster.yandex.ru/).
2. Добавить сайт `https://bot.themarfa.name/`.
3. Подтвердить права (DNS / meta / файл — тот же принцип, что у Google).
4. **Индексирование → Файлы Sitemap** → `https://bot.themarfa.name/sitemap.xml`.
5. При необходимости: «Переобход страниц» для `/`, `/guide`, `/webhook`.
6. Проверить регион / главную зеркала, если когда-нибудь появится www или другой хост.

### После подтверждения

| Шаг | Статус |
|-----|--------|
| GSC ресурс создан + verified | ☐ |
| GSC sitemap принят | ☐ |
| Яндекс.Вебмастер сайт + verified | ☐ |
| Яндекс sitemap принят | ☐ |
| Выборочная проверка сниппета `/` и `/guide` в выдаче | ☐ |

---

## Не делать из агента без запроса

- Не публиковать verification tokens / DNS-секреты в чат или git.
- Не менять `robots.txt`, чтобы открыть `/health` или OAuth.
- Не слать availability-рассылку ради SEO-работ.

---

## Готово в репозитории

- Лендинг Dazzle: `/` (`webapp/site/index.html`), i18n + locale браузера
- `/guide`, `/webhook` в том же chrome
- `/sitemap.xml`, `/robots.txt`
