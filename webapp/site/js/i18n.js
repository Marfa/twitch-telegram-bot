/* Client locale: ?lang= → localStorage → navigator → en */
(function () {
  var SUPPORTED = ["en", "ru", "uk", "it"];
  var STORAGE_KEY = "site_lang";

  var STRINGS = {
    en: {
      meta_title: "Twitch → Telegram bot — stream alerts",
      meta_desc:
        "Get Twitch live, category, schedule, and end-of-stream alerts in Telegram, Discord, or your HTTPS webhook. Free plan + Premium Stars.",
      nav_home: "Home",
      nav_about: "Features",
      nav_pricing: "Pricing",
      nav_guide: "Guide",
      nav_start: "Open bot",
      hero_eyebrow: "Twitch alerts in Telegram",
      hero_title: "Never miss a stream you care about.",
      hero_cta_bot: "Open in Telegram",
      hero_cta_guide: "Read the guide",
      about_title: "What the bot does",
      about_lead:
        "Pick a Twitch channel, choose when to notify, and where to send it. The bot watches Helix and delivers the message you configured — DM, channel, Discord, or a signed webhook.",
      feat_alerts_t: "Alert types",
      feat_alerts_d:
        "Stream start, category change, upcoming from Twitch schedule, stream end, Drops, game alerts, release dates, and free giveaways.",
      feat_dest_t: "Anywhere you need",
      feat_dest_d:
        "Private chat, Telegram channel/group (topics supported), Discord Incoming Webhook, or your own HTTPS JSON webhook with HMAC.",
      feat_tpl_t: "Templates & covers",
      feat_tpl_d:
        "Message templates with placeholders, stream preview, game cover, GIF/video preview, and optional AI cover on Premium.",
      feat_import_t: "Import follows",
      feat_import_d:
        "OAuth once and pull Twitch follows into subscriptions. Premium can keep them in sync automatically.",
      feat_extra_t: "Extras",
      feat_extra_d:
        "Lucky pick, stream chat Mini App, schedule tools, AI clips from VOD, channel stats — open more with Premium.",
      feat_langs_t: "Four languages",
      feat_langs_d:
        "Russian, English, Ukrainian, and Italian in the bot UI. This site follows your browser locale by default.",
      how_title: "How it works",
      how_1_t: "Open the bot",
      how_1_before: "Start ",
      how_1_or: " or ",
      how_1_after: " and pick a language.",
      how_2_t: "Add a streamer",
      how_2_d: "New subscription → alert type → Twitch link or username → destination.",
      how_3_t: "Tune the message",
      how_3_d: "Template, image, buttons, advanced filters — keep free defaults or unlock Premium options.",
      how_4_t: "Get notified",
      how_4_d: "When the event fires, the bot sends exactly where you pointed it.",
      pricing_title: "Free and Premium",
      pricing_lead:
        "Free covers stream-start alerts with up to 5 active subscriptions. Premium unlocks more types, sync, advanced options, history, AI clips, and more — paid in Telegram Stars.",
      plan_free_t: "Free",
      plan_free_price: "0",
      plan_free_per: "Stars",
      plan_free_1: "5 active alerts",
      plan_free_2: "Stream start type",
      plan_free_3: "DM / channel / Discord / webhook",
      plan_free_4: "7-day alert history",
      plan_free_cta: "Start free",
      plan_prem_t: "Premium",
      plan_prem_price: "from 20",
      plan_prem_per: "Stars",
      plan_prem_1: "All alert types",
      plan_prem_2: "Twitch follow sync",
      plan_prem_3: "Advanced options & AI tools",
      plan_prem_4: "Month / year / lifetime / à la carte",
      plan_prem_cta: "See plans in bot",
      cta_title: "Ready when the stream goes live",
      cta_lead:
        "Open the bot, add your first streamer, and keep the guide handy for every menu.",
      cta_bot: "Open bot",
      cta_guide: "User guide",
      cta_status: "Status page",
      footer_blurb:
        "Twitch stream alerts for Telegram and Discord. Built by Marfa. Not affiliated with Twitch.",
      footer_contact: "Contact",
      footer_links: "Site links",
      footer_support: "Support the project",
      footer_home: "Home",
      footer_guide: "Guide",
      footer_webhook: "Webhook docs",
      footer_privacy: "Privacy",
      footer_placeholders: "Placeholders",
      footer_github: "GitHub",
      footer_donate: "Donate",
      footer_crypto: "Crypto donate",
      footer_copy: "© Marfa — Twitch → Telegram bot",
      footer_design: "Design by",
      doc_back: "Home",
    },
    ru: {
      meta_title: "Twitch → Telegram бот — оповещения о стримах",
      meta_desc:
        "Оповещения о старте, смене категории, расписании и конце стрима Twitch в Telegram, Discord или на ваш HTTPS webhook. Бесплатно и Premium за Stars.",
      nav_home: "Главная",
      nav_about: "Возможности",
      nav_pricing: "Тарифы",
      nav_guide: "Инструкция",
      nav_start: "Открыть бота",
      hero_eyebrow: "Оповещения Twitch в Telegram",
      hero_title: "Не пропустите стрим, который ждёте.",
      hero_cta_bot: "Открыть в Telegram",
      hero_cta_guide: "Читать инструкцию",
      about_title: "Что умеет бот",
      about_lead:
        "Выберите канал Twitch, момент оповещения и куда слать. Бот смотрит Helix и доставляет сообщение туда, куда вы указали — в личку, канал, Discord или подписанный webhook.",
      feat_alerts_t: "Типы оповещений",
      feat_alerts_d:
        "Старт стрима, смена категории, предстоящий по расписанию Twitch, конец эфира, Drops, оповещения по игре, релизы и раздачи.",
      feat_dest_t: "Куда угодно",
      feat_dest_d:
        "Личка, канал/группа Telegram (темы), Discord Incoming Webhook или свой HTTPS JSON webhook с HMAC.",
      feat_tpl_t: "Шаблоны и обложки",
      feat_tpl_d:
        "Шаблоны с плейсхолдерами, превью стрима, обложка игры, GIF/видео-превью и ИИ-обложка на Premium.",
      feat_import_t: "Импорт фолловов",
      feat_import_d:
        "OAuth один раз — подтянуть фолловы в подписки. Premium может синхронизировать их автоматически.",
      feat_extra_t: "Ещё",
      feat_extra_d:
        "«Мне повезёт», Mini App чата, расписание, AI-клипы из VOD, статистика канала — больше на Premium.",
      feat_langs_t: "Четыре языка",
      feat_langs_d:
        "Русский, English, українська и italiano в боте. Сайт по умолчанию берёт язык браузера.",
      how_title: "Как это работает",
      how_1_t: "Откройте бота",
      how_1_before: "Старт ",
      how_1_or: " или ",
      how_1_after: " и выбор языка.",
      how_2_t: "Добавьте стримера",
      how_2_d: "Новая подписка → тип → ссылка/ник Twitch → куда слать.",
      how_3_t: "Настройте текст",
      how_3_d: "Шаблон, картинка, кнопки, фильтры — бесплатные опции или Premium.",
      how_4_t: "Получайте оповещения",
      how_4_d: "Когда событие случилось, бот пишет туда, куда вы указали.",
      pricing_title: "Бесплатно и Premium",
      pricing_lead:
        "Бесплатно: старт стрима и до 5 активных подписок. Premium открывает типы, sync, продвинутые опции, историю, AI-клипы и другое — оплата Stars в Telegram.",
      plan_free_t: "Бесплатно",
      plan_free_price: "0",
      plan_free_per: "Stars",
      plan_free_1: "5 активных оповещений",
      plan_free_2: "Тип «начало стрима»",
      plan_free_3: "ЛС / канал / Discord / webhook",
      plan_free_4: "История 7 дней",
      plan_free_cta: "Начать бесплатно",
      plan_prem_t: "Premium",
      plan_prem_price: "от 20",
      plan_prem_per: "Stars",
      plan_prem_1: "Все типы оповещений",
      plan_prem_2: "Синхронизация фолловов",
      plan_prem_3: "Продвинутые опции и AI",
      plan_prem_4: "Месяц / год / forever / а-ля карт",
      plan_prem_cta: "Тарифы в боте",
      cta_title: "Готово к выходу в эфир",
      cta_lead:
        "Откройте бота, добавьте первого стримера и держите инструкцию под рукой.",
      cta_bot: "Открыть бота",
      cta_guide: "Инструкция",
      cta_status: "Статус",
      footer_blurb:
        "Оповещения о стримах Twitch для Telegram и Discord. Сделано Marfa. Не аффилировано с Twitch.",
      footer_contact: "Контакты",
      footer_links: "Ссылки",
      footer_support: "Поддержка проекта",
      footer_home: "Главная",
      footer_guide: "Инструкция",
      footer_webhook: "Документация webhook",
      footer_privacy: "Конфиденциальность",
      footer_placeholders: "Плейсхолдеры",
      footer_github: "GitHub",
      footer_donate: "Донат",
      footer_crypto: "Донат криптой",
      footer_copy: "© Marfa — Twitch → Telegram бот",
      footer_design: "Дизайн",
      doc_back: "Главная",
    },
    uk: {
      meta_title: "Twitch → Telegram бот — сповіщення про стріми",
      meta_desc:
        "Сповіщення про старт, зміну категорії, розклад і кінець стріму Twitch у Telegram, Discord або на ваш HTTPS webhook.",
      nav_home: "Головна",
      nav_about: "Можливості",
      nav_pricing: "Тарифи",
      nav_guide: "Інструкція",
      nav_start: "Відкрити бота",
      hero_eyebrow: "Сповіщення Twitch у Telegram",
      hero_title: "Не пропустіть стрім, на який чекаєте.",
      hero_cta_bot: "Відкрити в Telegram",
      hero_cta_guide: "Читати інструкцію",
      about_title: "Що вміє бот",
      about_lead:
        "Оберіть канал Twitch, момент сповіщення і куди надсилати. Бот стежить за Helix і доставляє повідомлення туди, куди ви вказали.",
      feat_alerts_t: "Типи сповіщень",
      feat_alerts_d:
        "Старт стріму, зміна категорії, майбутній за розкладом, кінець ефіру, Drops, гра, релізи та роздачі.",
      feat_dest_t: "Куди завгодно",
      feat_dest_d:
        "Особисті, канал/група Telegram, Discord Incoming Webhook або власний HTTPS JSON webhook з HMAC.",
      feat_tpl_t: "Шаблони та обкладинки",
      feat_tpl_d:
        "Шаблони з плейсхолдерами, прев’ю стріму, обкладинка гри, GIF/відео та AI-обкладинка на Premium.",
      feat_import_t: "Імпорт фолловів",
      feat_import_d:
        "OAuth один раз — підтягнути фоллови. Premium може синхронізувати їх автоматично.",
      feat_extra_t: "Ще",
      feat_extra_d:
        "«Мені пощастить», Mini App чату, розклад, AI-кліпи з VOD, статистика каналу.",
      feat_langs_t: "Чотири мови",
      feat_langs_d:
        "Українська, російська, English та italiano в боті. Сайт за замовчуванням бере мову браузера.",
      how_title: "Як це працює",
      how_1_t: "Відкрийте бота",
      how_1_before: "Старт ",
      how_1_or: " або ",
      how_1_after: " і вибір мови.",
      how_2_t: "Додайте стрімера",
      how_2_d: "Нова підписка → тип → посилання/нік Twitch → куди слати.",
      how_3_t: "Налаштуйте текст",
      how_3_d: "Шаблон, зображення, кнопки, фільтри — безкоштовно або Premium.",
      how_4_t: "Отримуйте сповіщення",
      how_4_d: "Коли подія сталась, бот пише туди, куди ви вказали.",
      pricing_title: "Безкоштовно і Premium",
      pricing_lead:
        "Безкоштовно: старт стріму і до 5 активних підписок. Premium відкриває більше типів і опцій — оплата Stars у Telegram.",
      plan_free_t: "Безкоштовно",
      plan_free_price: "0",
      plan_free_per: "Stars",
      plan_free_1: "5 активних сповіщень",
      plan_free_2: "Тип «початок стріму»",
      plan_free_3: "Особисті / канал / Discord / webhook",
      plan_free_4: "Історія 7 днів",
      plan_free_cta: "Почати безкоштовно",
      plan_prem_t: "Premium",
      plan_prem_price: "від 20",
      plan_prem_per: "Stars",
      plan_prem_1: "Усі типи сповіщень",
      plan_prem_2: "Синхронізація фолловів",
      plan_prem_3: "Розширені опції та AI",
      plan_prem_4: "Місяць / рік / forever / à la carte",
      plan_prem_cta: "Тарифи в боті",
      cta_title: "Готово до виходу в ефір",
      cta_lead:
        "Відкрийте бота, додайте першого стрімера і тримайте інструкцію під рукою.",
      cta_bot: "Відкрити бота",
      cta_guide: "Інструкція",
      cta_status: "Статус",
      footer_blurb:
        "Сповіщення про стріми Twitch для Telegram і Discord. Зроблено Marfa. Не афілійовано з Twitch.",
      footer_contact: "Контакти",
      footer_links: "Посилання",
      footer_support: "Підтримка проєкту",
      footer_home: "Головна",
      footer_guide: "Інструкція",
      footer_webhook: "Документація webhook",
      footer_privacy: "Конфіденційність",
      footer_placeholders: "Плейсхолдери",
      footer_github: "GitHub",
      footer_donate: "Донат",
      footer_crypto: "Донат криптою",
      footer_copy: "© Marfa — Twitch → Telegram бот",
      footer_design: "Дизайн",
      doc_back: "Головна",
    },
    it: {
      meta_title: "Bot Twitch → Telegram — avvisi live",
      meta_desc:
        "Avvisi Twitch (live, categoria, schedule, fine stream) su Telegram, Discord o il tuo webhook HTTPS. Piano gratis + Premium con Stars.",
      nav_home: "Home",
      nav_about: "Funzioni",
      nav_pricing: "Prezzi",
      nav_guide: "Guida",
      nav_start: "Apri il bot",
      hero_eyebrow: "Avvisi Twitch su Telegram",
      hero_title: "Non perdere lo stream che aspetti.",
      hero_cta_bot: "Apri in Telegram",
      hero_cta_guide: "Leggi la guida",
      about_title: "Cosa fa il bot",
      about_lead:
        "Scegli un canale Twitch, quando avvisare e dove inviare. Il bot guarda Helix e consegna il messaggio dove indichi tu.",
      feat_alerts_t: "Tipi di avviso",
      feat_alerts_d:
        "Inizio live, cambio categoria, upcoming dallo schedule Twitch, fine stream, Drops, giochi, uscite e giveaway.",
      feat_dest_t: "Dove serve",
      feat_dest_d:
        "Chat privata, canale/gruppo Telegram, Discord Incoming Webhook o il tuo webhook JSON HTTPS con HMAC.",
      feat_tpl_t: "Template e cover",
      feat_tpl_d:
        "Template con placeholder, preview dello stream, cover del gioco, GIF/video e cover AI su Premium.",
      feat_import_t: "Import follow",
      feat_import_d:
        "OAuth una volta e importa i follow Twitch. Premium può sincronizzarli automaticamente.",
      feat_extra_t: "Extra",
      feat_extra_d:
        "Lucky pick, Mini App chat, schedule, AI clip da VOD, stats canale — di più con Premium.",
      feat_langs_t: "Quattro lingue",
      feat_langs_d:
        "Italiano, inglese, russo e ucraino nel bot. Il sito usa la lingua del browser di default.",
      how_title: "Come funziona",
      how_1_t: "Apri il bot",
      how_1_before: "Avvia ",
      how_1_or: " o ",
      how_1_after: " e scegli la lingua.",
      how_2_t: "Aggiungi uno streamer",
      how_2_d: "Nuova iscrizione → tipo → link/username Twitch → destinazione.",
      how_3_t: "Configura il messaggio",
      how_3_d: "Template, immagine, pulsanti, filtri — gratis o opzioni Premium.",
      how_4_t: "Ricevi gli avvisi",
      how_4_d: "Quando scatta l’evento, il bot scrive dove hai indicato.",
      pricing_title: "Gratis e Premium",
      pricing_lead:
        "Gratis: avvisi di inizio live fino a 5 iscrizioni attive. Premium sblocca tipi, sync, opzioni avanzate e altro — paghi in Stars su Telegram.",
      plan_free_t: "Gratis",
      plan_free_price: "0",
      plan_free_per: "Stars",
      plan_free_1: "5 avvisi attivi",
      plan_free_2: "Tipo inizio stream",
      plan_free_3: "DM / canale / Discord / webhook",
      plan_free_4: "Cronologia 7 giorni",
      plan_free_cta: "Inizia gratis",
      plan_prem_t: "Premium",
      plan_prem_price: "da 20",
      plan_prem_per: "Stars",
      plan_prem_1: "Tutti i tipi di avviso",
      plan_prem_2: "Sync dei follow Twitch",
      plan_prem_3: "Opzioni avanzate e AI",
      plan_prem_4: "Mese / anno / lifetime / à la carte",
      plan_prem_cta: "Piani nel bot",
      cta_title: "Pronto per il go-live",
      cta_lead:
        "Apri il bot, aggiungi il primo streamer e tieni la guida a portata di mano.",
      cta_bot: "Apri il bot",
      cta_guide: "Guida",
      cta_status: "Stato",
      footer_blurb:
        "Avvisi Twitch per Telegram e Discord. Fatto da Marfa. Non affiliato a Twitch.",
      footer_contact: "Contatti",
      footer_links: "Link",
      footer_support: "Supporta il progetto",
      footer_home: "Home",
      footer_guide: "Guida",
      footer_webhook: "Docs webhook",
      footer_privacy: "Privacy",
      footer_placeholders: "Placeholder",
      footer_github: "GitHub",
      footer_donate: "Donazione",
      footer_crypto: "Donazione crypto",
      footer_copy: "© Marfa — bot Twitch → Telegram",
      footer_design: "Design di",
      doc_back: "Home",
    },
  };

  function normalize(raw) {
    if (!raw) return null;
    var code = String(raw).trim().toLowerCase().replace("_", "-");
    if (SUPPORTED.indexOf(code) >= 0) return code;
    var primary = code.split("-")[0];
    if (SUPPORTED.indexOf(primary) >= 0) return primary;
    return null;
  }

  function fromNavigator() {
    var list = [];
    if (navigator.languages && navigator.languages.length) {
      list = navigator.languages;
    } else if (navigator.language) {
      list = [navigator.language];
    }
    for (var i = 0; i < list.length; i++) {
      var hit = normalize(list[i]);
      if (hit) return hit;
    }
    return "en";
  }

  function detect() {
    var params = new URLSearchParams(window.location.search);
    var fromQuery = normalize(params.get("lang"));
    if (fromQuery) return fromQuery;
    try {
      var stored = normalize(localStorage.getItem(STORAGE_KEY));
      if (stored) return stored;
    } catch (e) {}
    return fromNavigator();
  }

  function apply(lang) {
    var pack = STRINGS[lang] || STRINGS.en;
    document.documentElement.lang = lang;
    document.querySelectorAll("[data-i18n]").forEach(function (el) {
      var key = el.getAttribute("data-i18n");
      if (pack[key] != null) el.textContent = pack[key];
    });
    document.querySelectorAll("[data-i18n-placeholder]").forEach(function (el) {
      var key = el.getAttribute("data-i18n-placeholder");
      if (pack[key] != null) el.setAttribute("placeholder", pack[key]);
    });
    var title = pack.meta_title;
    if (title) document.title = title;
    var meta = document.querySelector('meta[name="description"]');
    if (meta && pack.meta_desc) meta.setAttribute("content", pack.meta_desc);
    document.querySelectorAll(".lang-switch a[data-lang]").forEach(function (a) {
      a.classList.toggle("is-active", a.getAttribute("data-lang") === lang);
    });
    var langPages = {
      guide: "/guide",
      privacy: "/privacy",
      placeholders: "/placeholders",
    };
    document.querySelectorAll("a[data-lang-page]").forEach(function (a) {
      var key = a.getAttribute("data-lang-page");
      var base = langPages[key];
      if (!base) return;
      a.setAttribute("href", base + "?lang=" + encodeURIComponent(lang));
    });
    try {
      localStorage.setItem(STORAGE_KEY, lang);
    } catch (e) {}
    try {
      var url = new URL(window.location.href);
      url.searchParams.set("lang", lang);
      window.history.replaceState({}, "", url.pathname + url.search + url.hash);
    } catch (e) {}
  }

  function bindSwitcher() {
    document.querySelectorAll(".lang-switch a[data-lang]").forEach(function (a) {
      a.addEventListener("click", function (ev) {
        var next = a.getAttribute("data-lang");
        // Doc pages keep server-rendered body copy — follow ?lang= href.
        if (document.body.classList.contains("page-docs")) {
          try {
            localStorage.setItem(STORAGE_KEY, next);
          } catch (e) {}
          return;
        }
        ev.preventDefault();
        apply(next);
      });
    });
  }

  var lang = detect();
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      apply(lang);
      bindSwitcher();
    });
  } else {
    apply(lang);
    bindSwitcher();
  }

  window.SiteI18n = { detect: detect, apply: apply, supported: SUPPORTED };
})();
