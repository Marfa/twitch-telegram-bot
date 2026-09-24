(function () {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) {
    try {
      tg.ready();
      tg.expand();
    } catch (_) {}
  }

  const el = (id) => document.getElementById(id);
  const i18n = {
    en: {
      live: "Live now",
      empty: "No live streams among your active subscriptions.",
      emptyNone: "No active subscriptions.",
      emptyOffline: "No one is live ({n} active subscriptions).",
      stats: "{live} live · {n} subscriptions",
      searchPh: "Name or twitch.tv / m.twitch.tv link",
      go: "Go",
      login: "Log in Twitch",
      send: "Send",
      sendPh: "Message",
      linked: "Twitch: @{login}",
      notLinked: "Twitch: not linked (needed to send)",
      offline: "Streamer is offline",
      notFound: "Streamer not found",
      badQuery: "Enter a name or Twitch link",
      beta: "Enable «Twitch stream chat» in Settings → Beta mode.",
      authFail: "Open Chat from the bot: /start, then the «Chat» button.",
      authEmpty:
        "No login token. Close this window, tap /start in the bot, then open «Chat» from the keyboard.",
      simple: "Simple",
      embed: "Embed",
      quota: "{n} messages left today",
      unlimited: "Unlimited sends",
      limitHit: "Daily limit reached. Premium unlocks unlimited chat.",
      needAuth: "Log in with Twitch to send messages.",
      oauthPrivacyHint:
        "OAuth grants the bot Twitch scopes you approve. Tokens are stored encrypted; we do not sell your data. Not affiliated with Twitch.",
      sendFail: "Could not send message.",
      loadFail: "Could not load data ({error}). Close and open Chat again.",
      connecting: "Connecting to chat…",
      disconnected: "Chat disconnected. Reconnecting…",
      loading: "Loading…",
      otherStreamers: "Other streamers",
      homeAdd: "Add to Home Screen",
      homeAdded: "On Home Screen",
      statusLive: "Live",
      statusOffline: "Offline",
        embedHint:
        "Twitch login/Drops in the embed UI do not work here. Send below after linking Twitch in the header.",
    },
    ru: {
      live: "Сейчас в эфире",
      empty: "Нет эфиров среди ваших активных подписок.",
      emptyNone: "Нет активных подписок.",
      emptyOffline: "Сейчас никто не в эфире ({n} активных подписок).",
      stats: "В эфире: {live} · подписок: {n}",
      searchPh: "Имя или ссылка twitch.tv / m.twitch.tv",
      go: "Найти",
      login: "Войти в Twitch",
      send: "Отправить",
      sendPh: "Сообщение",
      linked: "Twitch: @{login}",
      notLinked: "Twitch: не привязан (нужен для отправки)",
      offline: "Стример оффлайн",
      notFound: "Стример не найден",
      badQuery: "Введите имя или ссылку Twitch",
      beta: "Включите «Чат стримов Twitch» в Настройки → Режим бета.",
      authFail: "Откройте «Чат» из бота: /start, затем кнопка «Чат».",
      authEmpty:
        "Нет токена входа. Закройте окно, нажмите /start в боте, затем «Чат» на клавиатуре.",
      simple: "Простой",
      embed: "Embed",
      quota: "Осталось сообщений сегодня: {n}",
      unlimited: "Безлимитная отправка",
      limitHit: "Дневной лимит. Premium снимает ограничение.",
      needAuth: "Войдите в Twitch, чтобы писать.",
      oauthPrivacyHint:
        "OAuth даёт боту выбранные права Twitch. Токены хранятся зашифрованно; данные не продаём. Не аффилированы с Twitch.",
      sendFail: "Не удалось отправить.",
      loadFail: "Не удалось загрузить данные ({error}). Закройте и снова откройте «Чат».",
      connecting: "Подключение к чату…",
      disconnected: "Чат отключён. Переподключение…",
      loading: "Загрузка…",
      otherStreamers: "Другие стримеры",
      homeAdd: "На экран «Домой»",
      homeAdded: "Уже на «Домой»",
      statusLive: "В эфире",
      statusOffline: "Оффлайн",
      embedHint:
        "Вход и Drops в UI Twitch внутри embed не работают. Пишите в поле ниже (вход Twitch в шапке).",
    },
    uk: {
      live: "Зараз в ефірі",
      empty: "Немає ефірів серед ваших активних підписок.",
      emptyNone: "Немає активних підписок.",
      emptyOffline: "Зараз ніхто не в ефірі ({n} активних підписок).",
      stats: "В ефірі: {live} · підписок: {n}",
      searchPh: "Імʼя або посилання twitch.tv / m.twitch.tv",
      go: "Знайти",
      login: "Увійти в Twitch",
      send: "Надіслати",
      sendPh: "Повідомлення",
      linked: "Twitch: @{login}",
      notLinked: "Twitch: не привʼязано (потрібно для надсилання)",
      offline: "Стрімер офлайн",
      notFound: "Стрімера не знайдено",
      badQuery: "Введіть імʼя або посилання Twitch",
      beta: "Увімкніть «Чат стрімів Twitch» у Налаштування → Режим бета.",
      authFail: "Відкрийте «Чат» з бота: /start, потім кнопка «Чат».",
      authEmpty:
        "Немає токена входу. Закрийте вікно, натисніть /start у боті, потім «Чат» на клавіатурі.",
      simple: "Простий",
      embed: "Embed",
      quota: "Залишилось повідомлень сьогодні: {n}",
      unlimited: "Безлімітне надсилання",
      limitHit: "Денний ліміт. Premium знімає обмеження.",
      needAuth: "Увійдіть у Twitch, щоб писати.",
      oauthPrivacyHint:
        "OAuth дає боту обрані права Twitch. Токени зберігаються зашифровано; дані не продаємо. Не афілійовані з Twitch.",
      sendFail: "Не вдалося надіслати.",
      loadFail: "Не вдалося завантажити дані ({error}). Закрийте й знову відкрийте «Чат».",
      connecting: "Підключення до чату…",
      disconnected: "Чат відключено. Перепідключення…",
      loading: "Завантаження…",
      otherStreamers: "Інші стрімери",
      homeAdd: "На екран «Додому»",
      homeAdded: "Вже на «Додому»",
      statusLive: "В ефірі",
      statusOffline: "Офлайн",
      embedHint:
        "Вхід і Drops у UI Twitch всередині embed не працюють. Пишіть у поле нижче (вхід Twitch у шапці).",
    },
    it: {
      live: "In diretta ora",
      empty: "Nessuna diretta tra i tuoi avvisi attivi.",
      emptyNone: "Nessun avviso attivo.",
      emptyOffline: "Nessuno è in diretta ({n} avvisi attivi).",
      stats: "{live} in diretta · {n} avvisi",
      searchPh: "Nome o link twitch.tv / m.twitch.tv",
      go: "Vai",
      login: "Accedi a Twitch",
      send: "Invia",
      sendPh: "Messaggio",
      linked: "Twitch: @{login}",
      notLinked: "Twitch: non collegato (serve per inviare)",
      offline: "Streamer offline",
      notFound: "Streamer non trovato",
      badQuery: "Inserisci un nome o un link Twitch",
      beta: "Attiva «Chat stream Twitch» in Impostazioni → Modalità beta.",
      authFail: "Apri Chat dal bot: /start, poi il pulsante «Chat».",
      authEmpty:
        "Nessun token di accesso. Chiudi questa finestra, tocca /start nel bot, poi apri «Chat» dalla tastiera.",
      simple: "Semplice",
      embed: "Embed",
      quota: "{n} messaggi rimasti oggi",
      unlimited: "Invii illimitati",
      limitHit: "Limite giornaliero raggiunto. Premium sblocca la chat illimitata.",
      needAuth: "Accedi con Twitch per inviare messaggi.",
      oauthPrivacyHint:
        "OAuth concede al bot gli ambiti Twitch che approvi. I token sono memorizzati crittografati; non vendiamo i tuoi dati. Non affiliati a Twitch.",
      sendFail: "Impossibile inviare il messaggio.",
      loadFail: "Impossibile caricare i dati ({error}). Chiudi e riapri Chat.",
      connecting: "Connessione alla chat…",
      disconnected: "Chat disconnessa. Riconnessione…",
      loading: "Caricamento…",
      otherStreamers: "Altri streamer",
      homeAdd: "Aggiungi alla schermata Home",
      homeAdded: "Sulla schermata Home",
      statusLive: "In diretta",
      statusOffline: "Offline",
      embedHint:
        "Accesso Twitch/Drops nell'UI embed non funzionano qui. Scrivi sotto dopo aver collegato Twitch in alto.",
    },
  };

  let lang = "en";
  let t = i18n.en;
  let session = null;
  let current = null;
  let useFallback = false;
  let ircSocket = null;
  let ircTimer = null;
  let ircStatusEl = null;
  let appToken = "";
  let urlLang = "";
  const SECURE_TOKEN_KEY = "chat_t";
  const DEVICE_MODE_KEY = "chat_mode"; // "simple" | "embed"

  function setLang(code) {
    const raw = String(code || "").toLowerCase();
    if (raw.startsWith("ru")) lang = "ru";
    else if (raw.startsWith("uk")) lang = "uk";
    else if (raw.startsWith("it")) lang = "it";
    else lang = "en";
    t = i18n[lang] || i18n.en;
    document.documentElement.lang = lang;
    el("online-title").textContent = t.live;
    el("online-empty").textContent = t.empty;
    el("search-input").placeholder = t.searchPh;
    el("search-form").querySelector('button[type="submit"]').textContent = t.go;
    el("btn-login").textContent = t.login;
    el("btn-fallback").textContent = useFallback ? t.embed : t.simple;
    const embedHint = el("embed-hint");
    if (embedHint) embedHint.textContent = t.embedHint;
    const sendInput = el("send-input");
    const sendBtn = el("btn-send");
    if (sendInput) sendInput.placeholder = t.sendPh;
    if (sendBtn) sendBtn.textContent = t.send;
    const homeBtn = el("btn-home");
    if (homeBtn && !homeBtn.classList.contains("hidden")) {
      homeBtn.textContent = homeBtn.dataset.state === "added" ? t.homeAdded : t.homeAdd;
    }
    if (current) updateChatStatus(current);
  }

  function secureStorage() {
    return (tg && tg.SecureStorage) || null;
  }

  function deviceStorage() {
    return (tg && tg.DeviceStorage) || null;
  }

  function storageGet(store, key) {
    return new Promise((resolve) => {
      if (!store || typeof store.getItem !== "function") {
        resolve(null);
        return;
      }
      try {
        store.getItem(key, (err, value, canRestore) => {
          if (!err && value != null && value !== "") {
            resolve(String(value));
            return;
          }
          if (
            !err &&
            canRestore &&
            typeof store.restoreItem === "function"
          ) {
            try {
              store.restoreItem(key, (e2, v2) => {
                resolve(!e2 && v2 ? String(v2) : null);
              });
            } catch (_) {
              resolve(null);
            }
            return;
          }
          resolve(null);
        });
      } catch (_) {
        resolve(null);
      }
    });
  }

  function storageSet(store, key, value) {
    return new Promise((resolve) => {
      if (!store || typeof store.setItem !== "function") {
        resolve(false);
        return;
      }
      try {
        store.setItem(key, String(value), (err) => resolve(!err));
      } catch (_) {
        resolve(false);
      }
    });
  }

  async function persistToken(token) {
    if (!token) return;
    appToken = token;
    const ok = await storageSet(secureStorage(), SECURE_TOKEN_KEY, token);
    if (!ok) {
      try {
        sessionStorage.setItem(SECURE_TOKEN_KEY, token);
      } catch (_) {}
    }
    try {
      localStorage.removeItem(SECURE_TOKEN_KEY);
      localStorage.removeItem("chat_t");
    } catch (_) {}
  }

  async function loadPersistedToken() {
    let token = await storageGet(secureStorage(), SECURE_TOKEN_KEY);
    if (!token) {
      try {
        token = sessionStorage.getItem(SECURE_TOKEN_KEY) || "";
      } catch (_) {
        token = "";
      }
    }
    if (token) appToken = token;
    return appToken;
  }

  async function persistChatMode(simple) {
    const mode = simple ? "simple" : "embed";
    const ok = await storageSet(deviceStorage(), DEVICE_MODE_KEY, mode);
    if (!ok) {
      try {
        sessionStorage.setItem(DEVICE_MODE_KEY, mode);
      } catch (_) {}
    }
  }

  async function loadChatMode() {
    let mode = await storageGet(deviceStorage(), DEVICE_MODE_KEY);
    if (!mode) {
      try {
        mode = sessionStorage.getItem(DEVICE_MODE_KEY) || "";
      } catch (_) {
        mode = "";
      }
    }
    if (mode === "simple") useFallback = true;
    else if (mode === "embed") useFallback = false;
  }

  async function takeTokenFromUrl() {
    const params = new URLSearchParams(location.search);
    let token = params.get("t") || params.get("token") || "";
    const langParam = params.get("lang") || "";
    if (langParam) urlLang = langParam;
    if (!token && location.hash) {
      const hp = new URLSearchParams(location.hash.replace(/^#\/?/, "").replace(/^\?/, ""));
      token = hp.get("t") || hp.get("token") || "";
      if (!urlLang) urlLang = hp.get("lang") || "";
    }
    if (token) {
      await persistToken(token);
      params.delete("t");
      params.delete("token");
      const q = params.toString();
      const clean = location.pathname + (q ? "?" + q : "");
      try {
        history.replaceState(null, "", clean);
      } catch (_) {}
    } else {
      await loadPersistedToken();
    }
    if (urlLang) {
      try {
        sessionStorage.setItem("chat_lang", urlLang);
      } catch (_) {}
    }
    return appToken;
  }

  function detectLang() {
    if (urlLang) return urlLang;
    const q = new URLSearchParams(location.search).get("lang");
    if (q) return q;
    const tgLang =
      (tg &&
        tg.initDataUnsafe &&
        tg.initDataUnsafe.user &&
        tg.initDataUnsafe.user.language_code) ||
      "";
    return tgLang;
  }

  function initData() {
    return (tg && tg.initData) || "";
  }

  async function api(path, opts) {
    const options = opts || {};
    const headers = Object.assign({}, options.headers || {});
    const idata = initData();
    if (idata) {
      headers.Authorization = "tma " + idata;
      headers["X-Telegram-Init-Data"] = idata;
    }
    if (appToken) {
      headers["X-Chat-Token"] = appToken;
      if (!headers.Authorization) headers.Authorization = "Bearer " + appToken;
    }
    const url = path;
    const res = await fetch(url, Object.assign({}, options, { headers }));
    let body = {};
    try {
      body = await res.json();
    } catch (_) {
      body = { ok: false, error: "bad_json" };
    }
    return { status: res.status, body };
  }

  function showFatal(msg) {
    el("fatal").textContent = msg;
    el("fatal").classList.remove("hidden");
    el("view-home").classList.add("hidden");
    el("view-chat").classList.add("hidden");
  }

  function authErrorMessage(error) {
    if (error === "beta_required") return t.beta;
    if (error === "unauthorized_empty") return t.authEmpty;
    return t.authFail;
  }

  function renderAuth() {
    const btn = el("btn-login");
    const status = el("auth-status");
    if (!session) return;
    if (session.twitch_linked) {
      status.textContent = t.linked.replace("{login}", session.twitch_login || "…");
      btn.classList.add("hidden");
    } else {
      status.textContent = t.notLinked;
      btn.classList.remove("hidden");
    }
    updateQuota();
  }

  function channelUnlimited(login) {
    if (!session) return false;
    if (session.unlimited) return true;
    const promo = session.promo_channels || [];
    const key = String(login || "").toLowerCase();
    return promo.some((c) => String(c || "").toLowerCase() === key);
  }

  function updateQuota() {
    const box = el("send-quota");
    const login = current && current.login;
    if (!session || channelUnlimited(login)) {
      box.textContent = "";
      box.classList.add("hidden");
      return;
    }
    const n = session.remaining != null ? session.remaining : 0;
    box.textContent = t.quota.replace("{n}", String(n));
    box.classList.toggle("hidden", n <= 0);
  }

  function appendStreamCard(list, s) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "card";
    const avatar = document.createElement("img");
    avatar.className = "card-avatar";
    avatar.alt = "";
    avatar.loading = "lazy";
    avatar.src = s.profile_image_url || "";
    avatar.addEventListener("error", () => {
      avatar.removeAttribute("src");
    });
    const body = document.createElement("div");
    body.className = "card-body";
    body.innerHTML =
      "<strong>" +
      escapeHtml(s.display_name || s.login) +
      "</strong><div class=\"meta\">" +
      escapeHtml(s.game_name || "") +
      (s.viewer_count ? " · " + s.viewer_count : "") +
      "</div><div class=\"meta\">" +
      escapeHtml(s.title || "") +
      "</div>";
    btn.appendChild(avatar);
    btn.appendChild(body);
    btn.addEventListener("click", () => openChat(s));
    list.appendChild(btn);
  }

  function renderOnline(streams, meta) {
    const list = el("online-list");
    list.innerHTML = "";
    const empty = el("online-empty");
    const stats = el("online-stats");
    const subscribed = meta && meta.subscribed != null ? meta.subscribed : 0;
    const live = meta && meta.live != null ? meta.live : streams.length;
    if (stats) {
      if (subscribed > 0) {
        stats.textContent = t.stats
          .replace("{live}", String(live))
          .replace("{n}", String(subscribed));
        stats.classList.remove("hidden");
      } else {
        stats.textContent = "";
        stats.classList.add("hidden");
      }
    }
    if (!streams.length) {
      empty.classList.remove("hidden");
      if (subscribed <= 0) empty.textContent = t.emptyNone;
      else if (live <= 0)
        empty.textContent = t.emptyOffline.replace("{n}", String(subscribed));
      else empty.textContent = t.empty;
      return;
    }
    empty.classList.add("hidden");
    streams.forEach((s) => appendStreamCard(list, s));
  }

  function renderOtherStreams(streams) {
    const section = el("other-section");
    const list = el("other-list");
    list.innerHTML = "";
    if (!session || session.unlimited || !streams || !streams.length) {
      section.classList.add("hidden");
      return;
    }
    section.classList.remove("hidden");
    el("other-title").textContent = t.otherStreamers;
    streams.forEach((s) => appendStreamCard(list, s));
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }



  function streamIsOnline(stream) {
    if (!stream) return false;
    // Online list cards omit `online`; resolve always sets it.
    if (stream.online == null) return true;
    return Boolean(stream.online);
  }

  function updateChatStatus(stream) {
    const box = el("chat-status");
    if (!box) return;
    if (!stream) {
      box.classList.add("hidden");
      box.textContent = "";
      box.removeAttribute("title");
      box.removeAttribute("aria-label");
      return;
    }
    const online = streamIsOnline(stream);
    const viewers = Math.max(0, parseInt(stream.viewer_count, 10) || 0);
    box.classList.remove("hidden");
    box.classList.toggle("is-live", online);
    box.classList.toggle("is-offline", !online);
    box.replaceChildren();
    const dot = document.createElement("span");
    dot.className = "chat-status-dot";
    dot.setAttribute("aria-hidden", "true");
    box.appendChild(dot);
    const label = document.createElement("span");
    label.className = "chat-status-label";
    if (online) {
      label.textContent = String(viewers);
      box.title = t.statusLive + " · " + viewers;
      box.setAttribute("aria-label", t.statusLive + ", " + viewers);
    } else {
      label.textContent = t.statusOffline;
      box.title = t.statusOffline;
      box.setAttribute("aria-label", t.statusOffline);
    }
    box.appendChild(label);
  }

  function openChat(stream) {
    current = stream;
    el("view-home").classList.add("hidden");
    el("view-chat").classList.remove("hidden");
    el("chat-title").textContent = stream.display_name || stream.login;
    el("chat-sub").textContent = stream.title || "";
    updateChatStatus(stream);
    el("btn-fallback").classList.remove("hidden");
    setLang(lang);
    applyChatMode();
    updateQuota();
  }

  function closeChat() {
    stopIrc();
    current = null;
    updateChatStatus(null);
    el("embed-frame").src = "about:blank";
    el("view-chat").classList.add("hidden");
    el("view-home").classList.remove("hidden");
    el("embed-hint").classList.add("hidden");
    el("send-form").classList.add("hidden");
    showSendFeedback("");
    loadOnlineList();
  }

  async function loadOnlineList() {
    try {
      const online = await api("/app/chat/api/online");
      if (online.body.ok) {
        renderOnline(online.body.streams || [], online.body);
        renderOtherStreams(online.body.other_streams || []);
        return;
      }
      renderOnline([], { subscribed: 0, live: 0 });
      renderOtherStreams([]);
      const hint = el("search-hint");
      hint.classList.remove("hidden");
      hint.textContent = t.loadFail.replace(
        "{error}",
        online.body.error || "error"
      );
    } catch (_) {
      renderOnline([], { subscribed: 0, live: 0 });
      renderOtherStreams([]);
    }
  }

  function applyChatMode() {
    el("btn-fallback").textContent = useFallback ? t.embed : t.simple;
    const hint = el("embed-hint");
    const sendForm = el("send-form");
    if (sendForm) sendForm.classList.remove("hidden");
    if (useFallback) {
      el("embed-wrap").classList.add("hidden");
      el("fallback-wrap").classList.remove("hidden");
      el("embed-frame").src = "about:blank";
      if (hint) hint.classList.add("hidden");
      startIrc(current.login);
    } else {
      stopIrc();
      el("fallback-wrap").classList.add("hidden");
      el("embed-wrap").classList.remove("hidden");
      if (hint) {
        hint.textContent = t.embedHint;
        hint.classList.remove("hidden");
      }
      const parent = (session && session.embed_parent) || location.hostname;
      const login = encodeURIComponent(current.login);
      el("embed-frame").src =
        "https://www.twitch.tv/embed/" +
        login +
        "/chat?parent=" +
        encodeURIComponent(parent) +
        "&darkpopout";
    }
  }

  function showSendFeedback(text) {
    const box = el("send-feedback");
    if (!box) return;
    if (!text) {
      box.textContent = "";
      box.classList.add("hidden");
      return;
    }
    box.textContent = text;
    box.classList.remove("hidden");
  }

  function notifySend(text) {
    if (useFallback) appendMsg("", text, true);
    else showSendFeedback(text);
  }

  function clearIrcStatus() {
    if (ircStatusEl && ircStatusEl.parentNode) {
      ircStatusEl.parentNode.removeChild(ircStatusEl);
    }
    ircStatusEl = null;
  }

  function appendSystemMsg(text) {
    clearIrcStatus();
    const list = el("msg-list");
    const row = document.createElement("div");
    row.className = "msg system";
    row.textContent = text;
    list.appendChild(row);
    ircStatusEl = row;
    list.scrollTop = list.scrollHeight;
  }

  function appendMsg(nick, text, system) {
    const list = el("msg-list");
    if (!system) clearIrcStatus();
    const row = document.createElement("div");
    row.className = "msg" + (system ? " system" : "");
    if (system) {
      row.textContent = text;
    } else {
      row.innerHTML =
        '<span class="nick">' + escapeHtml(nick) + "</span>" + escapeHtml(text);
    }
    list.appendChild(row);
    list.scrollTop = list.scrollHeight;
    while (list.children.length > 300) {
      list.removeChild(list.firstChild);
    }
  }

  function stopIrc() {
    if (ircTimer) {
      clearTimeout(ircTimer);
      ircTimer = null;
    }
    if (ircSocket) {
      try {
        ircSocket.onclose = null;
        ircSocket.close();
      } catch (_) {}
      ircSocket = null;
    }
    el("msg-list").innerHTML = "";
    ircStatusEl = null;
  }

  function startIrc(channelLogin) {
    stopIrc();
    const chan = String(channelLogin || "").toLowerCase();
    if (!chan) return;
    appendSystemMsg(t.connecting);
    const nick = "justinfan" + String(Math.floor(80000 + Math.random() * 10000));
    const ws = new WebSocket("wss://irc-ws.chat.twitch.tv:443");
    ircSocket = ws;
    ws.onopen = () => {
      ws.send("CAP REQ :twitch.tv/tags twitch.tv/commands");
      ws.send("PASS justinfan");
      ws.send("NICK " + nick);
      ws.send("JOIN #" + chan);
    };
    ws.onmessage = (ev) => {
      const raw = String(ev.data || "");
      raw.split("\r\n").forEach((line) => {
        if (!line) return;
        if (line.startsWith("PING ")) {
          ws.send("PONG " + line.slice(5));
          return;
        }
        if (line.indexOf(" JOIN #") !== -1) {
          const joinPart = line.slice(line.indexOf(" JOIN #") + 7);
          const joinChan = joinPart.split(/\s/)[0].replace(/;.*$/, "").toLowerCase();
          if (joinChan === chan) {
            clearIrcStatus();
            return;
          }
        }
        const priv = line.indexOf(" PRIVMSG #");
        if (priv === -1) return;
        let from = "user";
        let msgPart = "";
        if (line.charAt(0) === "@") {
          const tagsEnd = line.indexOf(" ");
          const tags = line.slice(1, tagsEnd);
          const dm = tags.match(/(?:^|;)display-name=([^;]*)/);
          if (dm && dm[1]) from = dm[1];
          const rest = line.slice(tagsEnd + 1);
          const bang = rest.indexOf("!");
          if ((!dm || !dm[1]) && rest.charAt(0) === ":" && bang > 0) {
            from = rest.slice(1, bang);
          }
          msgPart = line.slice(line.lastIndexOf(" :") + 2);
        } else {
          const bang = line.indexOf("!");
          if (line.charAt(0) === ":" && bang > 0) from = line.slice(1, bang);
          msgPart = line.slice(line.indexOf(" :", priv) + 2);
        }
        appendMsg(from, msgPart, false);
      });
    };
    ws.onclose = () => {
      appendSystemMsg(t.disconnected);
      ircTimer = setTimeout(() => {
        if (useFallback && current && current.login === chan) startIrc(chan);
      }, 2500);
    };
    ws.onerror = () => {
      try {
        ws.close();
      } catch (_) {}
    };
  }

  async function boot() {
    try {
      await takeTokenFromUrl();
      await loadChatMode();
      setLang(detectLang() || "en");
      el("online-empty").classList.remove("hidden");
      el("online-empty").textContent = t.loading;
      if (!initData() && !appToken) {
        showFatal(t.authEmpty);
        return;
      }
      const { body } = await api("/app/chat/api/session");
      if (!body.ok) {
        showFatal(authErrorMessage(body.error));
        return;
      }
      session = body;
      setLang(urlLang || body.lang || detectLang() || "en");
      renderAuth();
      await loadOnlineList();
      const params = new URLSearchParams(location.search);
      const openLogin = (params.get("login") || "").trim();
      const autoOpen = params.get("open") === "1";
      if (autoOpen && openLogin) {
        try {
          const resolved = await api(
            "/app/chat/api/resolve?q=" + encodeURIComponent(openLogin)
          );
          if (resolved.body.ok && resolved.body.online) {
            openChat(resolved.body);
          }
        } catch (_) {}
      }
    } catch (err) {
      showFatal(
        t.loadFail.replace("{error}", (err && err.message) || "boot")
      );
    }
  }

  el("search-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = el("search-input").value.trim();
    const hint = el("search-hint");
    hint.classList.remove("hidden");
    if (!q) {
      hint.textContent = t.badQuery;
      return;
    }
    hint.textContent = "…";
    try {
      const { body } = await api(
        "/app/chat/api/resolve?q=" + encodeURIComponent(q)
      );
      if (!body.ok) {
        if (body.error === "bad_query") hint.textContent = t.badQuery;
        else if (body.error === "not_found") hint.textContent = t.notFound;
        else if ((body.error || "").startsWith("unauthorized"))
          hint.textContent = authErrorMessage(body.error);
        else if (body.error === "beta_required") hint.textContent = t.beta;
        else
          hint.textContent = t.loadFail.replace("{error}", body.error || "error");
        return;
      }
      if (!body.online) {
        hint.textContent = t.offline;
        return;
      }
      hint.classList.add("hidden");
      openChat(body);
    } catch (err) {
      hint.textContent = t.loadFail.replace(
        "{error}",
        (err && err.message) || "search"
      );
    }
  });

  el("btn-back").addEventListener("click", closeChat);
  el("btn-fallback").addEventListener("click", () => {
    useFallback = !useFallback;
    persistChatMode(useFallback);
    setLang(lang);
    applyChatMode();
  });

  el("btn-login").addEventListener("click", async () => {
    const { body } = await api("/app/chat/api/oauth-url");
    if (!body.ok || !body.url) return;
    const notice = (body.privacy_notice || t.oauthPrivacyHint || "").trim();
    if (notice) notifySend(notice);
    if (tg && tg.openLink) tg.openLink(body.url);
    else window.open(body.url, "_blank");
  });

  el("send-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!current) return;
    showSendFeedback("");
    if (!session || !session.twitch_linked) {
      notifySend(t.needAuth);
      return;
    }
    if (!session.unlimited && !channelUnlimited(current.login) && session.remaining === 0) {
      notifySend(t.limitHit);
      return;
    }
    const input = el("send-input");
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    const { body } = await api("/app/chat/api/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        broadcaster_login: current.login,
        message: text,
        token: appToken,
      }),
    });
    if (!body.ok) {
      if (body.error === "daily_limit") notifySend(t.limitHit);
      else if (body.error === "twitch_auth_required") notifySend(t.needAuth);
      else notifySend(t.sendFail);
      if (typeof body.remaining === "number") {
        session.remaining = body.remaining;
        session.sent_today = body.sent_today;
        updateQuota();
      }
      return;
    }
    session.remaining = body.remaining;
    session.sent_today = body.sent_today;
    if (body.unlimited) session.unlimited = true;
    updateQuota();
    // Embed: message appears in Twitch iframe; Simple: echo into IRC list.
    if (useFallback) appendMsg(session.twitch_login || "you", text, false);
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) boot();
  });

  function setupHomeScreenShortcut() {
    const btnHome = el("btn-home");
    if (!btnHome || !tg) return;
    const canCheck = typeof tg.checkHomeScreenStatus === "function";
    const canAdd = typeof tg.addToHomeScreen === "function";
    if (!canCheck && !canAdd) return;

    function showHome(status) {
      if (status === "unsupported") {
        btnHome.classList.add("hidden");
        return;
      }
      btnHome.classList.remove("hidden");
      if (status === "added") {
        btnHome.dataset.state = "added";
        btnHome.textContent = t.homeAdded;
        btnHome.disabled = true;
      } else {
        btnHome.dataset.state = "missed";
        btnHome.textContent = t.homeAdd;
        btnHome.disabled = !canAdd;
      }
    }

    if (typeof tg.onEvent === "function") {
      try {
        tg.onEvent("homeScreenChecked", (payload) => {
          const status =
            typeof payload === "string"
              ? payload
              : payload && payload.status
                ? payload.status
                : "unknown";
          showHome(status);
        });
        tg.onEvent("homeScreenAdded", () => showHome("added"));
      } catch (_) {}
    }

    btnHome.addEventListener("click", () => {
      if (btnHome.dataset.state === "added" || !canAdd) return;
      try {
        tg.addToHomeScreen();
      } catch (_) {}
    });

    if (canCheck) {
      try {
        tg.checkHomeScreenStatus((status) => showHome(status || "unknown"));
      } catch (_) {
        showHome("unknown");
      }
    } else {
      showHome("unknown");
    }
  }

  setupHomeScreenShortcut();
  boot();
})();
