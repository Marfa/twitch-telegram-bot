(/* global Telegram */)
(function () {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) {
    tg.ready();
    tg.expand();
  }

  const el = (id) => document.getElementById(id);
  const i18n = {
    en: {
      live: "Live now",
      empty: "No live streams among your active subscriptions.",
      searchPh: "Name or twitch.tv / m.twitch.tv link",
      go: "Go",
      login: "Log in Twitch",
      linked: "Twitch: @{login}",
      notLinked: "Twitch: not linked (needed to send)",
      offline: "Streamer is offline",
      notFound: "Streamer not found",
      badQuery: "Enter a name or Twitch link",
      beta: "Enable «Twitch stream chat» in Settings → Beta mode.",
      authFail: "Open this screen from the bot menu button «Chat».",
      simple: "Simple",
      embed: "Embed",
      quota: "{n} messages left today",
      unlimited: "Unlimited sends",
      limitHit: "Daily limit reached. Premium unlocks unlimited chat.",
      needAuth: "Log in with Twitch to send messages.",
      sendFail: "Could not send message.",
      connecting: "Connecting to chat…",
      disconnected: "Chat disconnected. Reconnecting…",
    },
    ru: {
      live: "Сейчас в эфире",
      empty: "Нет эфиров среди ваших активных подписок.",
      searchPh: "Имя или ссылка twitch.tv / m.twitch.tv",
      go: "Найти",
      login: "Войти в Twitch",
      linked: "Twitch: @{login}",
      notLinked: "Twitch: не привязан (нужен для отправки)",
      offline: "Стример оффлайн",
      notFound: "Стример не найден",
      badQuery: "Введите имя или ссылку Twitch",
      beta: "Включите «Чат стримов Twitch» в Настройки → Режим бета.",
      authFail: "Откройте экран кнопкой меню бота «Чат».",
      simple: "Простой",
      embed: "Embed",
      quota: "Осталось сообщений сегодня: {n}",
      unlimited: "Безлимитная отправка",
      limitHit: "Дневной лимит. Premium снимает ограничение.",
      needAuth: "Войдите в Twitch, чтобы писать.",
      sendFail: "Не удалось отправить.",
      connecting: "Подключение к чату…",
      disconnected: "Чат отключён. Переподключение…",
    },
  };

  let lang = "en";
  let t = i18n.en;
  let session = null;
  let current = null;
  let useFallback = false;
  let ircSocket = null;
  let ircTimer = null;

  function setLang(code) {
    lang = code === "ru" ? "ru" : "en";
    t = i18n[lang];
    el("online-title").textContent = t.live;
    el("online-empty").textContent = t.empty;
    el("search-input").placeholder = t.searchPh;
    el("search-form").querySelector('button[type="submit"]').textContent = t.go;
    el("btn-login").textContent = t.login;
    el("btn-fallback").textContent = useFallback ? t.embed : t.simple;
  }

  function initData() {
    return (tg && tg.initData) || "";
  }

  async function api(path, opts) {
    const options = opts || {};
    const headers = Object.assign(
      { "Authorization": "tma " + initData() },
      options.headers || {}
    );
    const res = await fetch(path, Object.assign({}, options, { headers }));
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

  function updateQuota() {
    const box = el("send-quota");
    if (!session) {
      box.textContent = "";
      return;
    }
    if (session.unlimited) {
      box.textContent = t.unlimited;
    } else {
      const n = session.remaining != null ? session.remaining : 0;
      box.textContent = t.quota.replace("{n}", String(n));
    }
  }

  function renderOnline(streams) {
    const list = el("online-list");
    list.innerHTML = "";
    const empty = el("online-empty");
    if (!streams.length) {
      empty.classList.remove("hidden");
      return;
    }
    empty.classList.add("hidden");
    streams.forEach((s) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "card";
      btn.innerHTML =
        "<strong>" +
        escapeHtml(s.display_name || s.login) +
        "</strong><div class=\"meta\">" +
        escapeHtml(s.game_name || "") +
        (s.viewer_count ? " · " + s.viewer_count : "") +
        "</div><div class=\"meta\">" +
        escapeHtml(s.title || "") +
        "</div>";
      btn.addEventListener("click", () => openChat(s));
      list.appendChild(btn);
    });
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function openChat(stream) {
    current = stream;
    useFallback = !(session && session.prefer_embed);
    el("view-home").classList.add("hidden");
    el("view-chat").classList.remove("hidden");
    el("chat-title").textContent = stream.display_name || stream.login;
    el("chat-sub").textContent = stream.title || "";
    el("btn-fallback").classList.remove("hidden");
    applyChatMode();
  }

  function closeChat() {
    stopIrc();
    current = null;
    el("embed-frame").src = "about:blank";
    el("view-chat").classList.add("hidden");
    el("view-home").classList.remove("hidden");
  }

  function applyChatMode() {
    el("btn-fallback").textContent = useFallback ? t.embed : t.simple;
    if (useFallback) {
      el("embed-wrap").classList.add("hidden");
      el("fallback-wrap").classList.remove("hidden");
      el("embed-frame").src = "about:blank";
      startIrc(current.login);
    } else {
      stopIrc();
      el("fallback-wrap").classList.add("hidden");
      el("embed-wrap").classList.remove("hidden");
      const parent = (session && session.embed_parent) || location.hostname;
      const login = encodeURIComponent(current.login);
      el("embed-frame").src =
        "https://www.twitch.tv/embed/" +
        login +
        "/chat?parent=" +
        encodeURIComponent(parent) +
        "&darkpopout";
      // If iframe stays blank / blocked, user can switch to Simple.
      window.setTimeout(() => {
        if (!useFallback && current) {
          // Soft nudge only once via button label already visible.
        }
      }, 4000);
    }
  }

  function appendMsg(nick, text, system) {
    const list = el("msg-list");
    const row = document.createElement("div");
    row.className = "msg" + (system ? " system" : "");
    if (system) {
      row.textContent = text;
    } else {
      row.innerHTML =
        '<span class="nick">' +
        escapeHtml(nick) +
        "</span>" +
        escapeHtml(text);
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
  }

  function startIrc(channelLogin) {
    stopIrc();
    const chan = String(channelLogin || "").toLowerCase();
    if (!chan) return;
    appendMsg("", t.connecting, true);
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
      appendMsg("", t.disconnected, true);
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
    if (!initData()) {
      showFatal(t.authFail);
      return;
    }
    const { status, body } = await api("/app/chat/api/session");
    if (status === 403 && body.error === "beta_required") {
      showFatal(t.beta);
      return;
    }
    if (!body.ok) {
      showFatal(t.authFail);
      return;
    }
    session = body;
    setLang(body.lang || "en");
    renderAuth();
    const online = await api("/app/chat/api/online");
    if (online.body.ok) renderOnline(online.body.streams || []);
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
    const { body } = await api(
      "/app/chat/api/resolve?q=" + encodeURIComponent(q)
    );
    if (!body.ok) {
      hint.textContent =
        body.error === "not_found"
          ? t.notFound
          : body.error === "bad_query"
            ? t.badQuery
            : t.notFound;
      return;
    }
    if (!body.online) {
      hint.textContent = t.offline;
      return;
    }
    hint.classList.add("hidden");
    openChat(body);
  });

  el("btn-back").addEventListener("click", closeChat);
  el("btn-fallback").addEventListener("click", () => {
    useFallback = !useFallback;
    applyChatMode();
  });

  el("btn-login").addEventListener("click", async () => {
    const { body } = await api("/app/chat/api/oauth-url");
    if (!body.ok || !body.url) return;
    if (tg && tg.openLink) tg.openLink(body.url);
    else window.open(body.url, "_blank");
  });

  el("send-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!current) return;
    if (!session || !session.twitch_linked) {
      appendMsg("", t.needAuth, true);
      return;
    }
    if (!session.unlimited && session.remaining === 0) {
      appendMsg("", t.limitHit, true);
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
      }),
    });
    if (!body.ok) {
      if (body.error === "daily_limit") appendMsg("", t.limitHit, true);
      else if (body.error === "twitch_auth_required") appendMsg("", t.needAuth, true);
      else appendMsg("", t.sendFail, true);
      if (typeof body.remaining === "number") {
        session.remaining = body.remaining;
        session.sent_today = body.sent_today;
        updateQuota();
      }
      return;
    }
    session.remaining = body.remaining;
    session.sent_today = body.sent_today;
    session.unlimited = !!body.unlimited;
    updateQuota();
    appendMsg(session.twitch_login || "you", text, false);
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) boot();
  });

  boot();
})();
