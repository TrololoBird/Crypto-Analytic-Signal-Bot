"use strict";
const App = {
  state: {
    activeTab: "signals",
    diagnosticsSubTab: "overview",
    overview: null,
    analytics: null,
    funnel: null,
    confluenceLegs: null,
    confluenceLegsByProfile: null,
    shortlist: null,
    decisions: null,
    rejections: null,
    delivery: null,
    runtime: null,
    telegram: null,
    audit: null,
    regime: null,
    strategies: null,
    signals: [],
    diary: [],
    alerts: [],
    summary: null,
    outcomes: null,
    labelMaps: null,
  },

  init() {
    this._bindTabs();
    this._bindRefresh();
    this._bindKeys();
    if (typeof bindDiagnosticsTabs === "function") bindDiagnosticsTabs();
    this._loadLabelMaps();
    this._initWS();
    this.refreshAll();
    setInterval(() => this.refreshAll(), 10000);
  },

  async _loadLabelMaps() {
    try {
      const resp = await fetch("/api/meta/labels");
      if (resp.ok) {
        this.state.labelMaps = await resp.json();
      }
    } catch (e) {
      console.warn("label maps load failed", e);
    }
  },

  _initWS() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${proto}//${location.host}/api/v1/ws`;
    let reconnectDelay = 1000;
    const connect = () => {
      const ws = new WebSocket(wsUrl);
      ws.onopen = () => {
        const dot = document.getElementById("status-dot");
        const text = document.getElementById("status-text");
        if (dot) dot.className = "dot ok";
        if (text) text.textContent = "онлайн";
        reconnectDelay = 1000;
      };
      ws.onmessage = (msg) => {
        try {
          const data = JSON.parse(msg.data);
          this._onWSMessage(data);
        } catch (e) {
          console.warn("WS parse error", e);
        }
      };
      ws.onclose = () => {
        const dot = document.getElementById("status-dot");
        const text = document.getElementById("status-text");
        if (dot) dot.className = "dot";
        if (text) text.textContent = "переподключение…";
        setTimeout(connect, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 2, 30000);
      };
      ws.onerror = () => ws.close();
      this._ws = ws;
    };
    connect();
  },

  _onWSMessage(data) {
    switch (data.type) {
      case "tracking_update":
        if (this.state.activeTab === "tracking") renderTracking();
        else this._refreshTrackingBadge();
        break;
      case "signal":
        if (!this.state.signals.find((s) => s.signal_id === data.payload.signal_id)) {
          this.state.signals.unshift(data.payload);
          if (this.state.signals.length > 200) this.state.signals.length = 200;
        }
        if (this.state.activeTab === "signals") _fetchRiverSignals();
        break;
      case "regime_update":
        this.state.regime = data.payload;
        if (this.state.activeTab === "market") renderWeather();
        break;
      case "cycle_complete":
        this.refreshAll();
        break;
      default:
        break;
    }
  },

  _bindTabs() {
    document.querySelectorAll(".tab[data-tab]").forEach((btn) => {
      btn.addEventListener("click", () => this.switchTab(btn.dataset.tab));
    });
    document.querySelectorAll(".mobile-nav button[data-tab]").forEach((btn) => {
      btn.addEventListener("click", () => this.switchTab(btn.dataset.tab));
    });
  },

  _bindRefresh() {
    document.getElementById("refresh-button")?.addEventListener("click", () => this.refreshAll());
  },

  _bindKeys() {
    document.addEventListener("keydown", (e) => {
      if (e.key === "r" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        this.refreshAll();
      }
      const map = {
        1: "signals",
        2: "tracking",
        3: "diary",
        4: "market",
        5: "diagnostics",
      };
      if (map[e.key] && !e.ctrlKey && !e.metaKey && !e.altKey) {
        this.switchTab(map[e.key]);
      }
    });
  },

  switchTab(name) {
    this.state.activeTab = name;
    document.querySelectorAll(".tab[data-tab]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.tab === name);
    });
    document.querySelectorAll(".mobile-nav button[data-tab]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.tab === name);
    });
    document.querySelectorAll(".section.main-section").forEach((sec) => {
      sec.classList.toggle("active", sec.id === name);
    });
    this._renderActiveTab();
  },

  _renderActiveTab() {
    const tab = this.state.activeTab;
    if (tab === "signals") {
      if (typeof renderTodayPanel === "function") renderTodayPanel();
      if (typeof renderMarketStatePanel === "function") renderMarketStatePanel();
      if (typeof renderRiver === "function") renderRiver();
      else if (typeof _fetchRiverSignals === "function") _fetchRiverSignals();
    }
    if (tab === "tracking" && typeof renderTracking === "function") renderTracking();
    if (tab === "diary" && typeof renderDiary === "function") renderDiary();
    if (tab === "market" && typeof renderWeather === "function") renderWeather();
    if (tab === "diagnostics" && typeof renderDiagnostics === "function") renderDiagnostics();
  },

  async refreshAll() {
    try {
      const endpoints = [
        ["overview", "/api/live/overview"],
        ["funnel", "/api/live/funnel"],
        ["shortlist", "/api/live/shortlist"],
        ["decisions", "/api/live/decisions"],
        ["rejections", "/api/live/rejections"],
        ["delivery", "/api/live/delivery"],
        ["runtime", "/api/live/runtime"],
        ["telegram", "/api/live/telegram-preview"],
        ["analytics", "/api/analytics/report?days=30&scope=rolling"],
        ["confluenceLegs", "/api/analytics/confluence_legs"],
        ["confluenceLegsByProfile", "/api/analytics/confluence_legs_by_profile"],
        ["summary", "/api/v1/summary"],
        ["outcomes", "/api/v1/analytics/outcomes?days=30"],
      ];
      const results = await Promise.allSettled(
        endpoints.map(([key, url]) =>
          fetch(url, { cache: "no-store" }).then((r) => {
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            return r.json().then((data) => ({ key, data }));
          })
        )
      );
      for (const result of results) {
        if (result.status === "fulfilled") {
          this.state[result.value.key] = result.value.data;
        }
      }
      this._renderAll();
      const dot = document.getElementById("status-dot");
      const text = document.getElementById("status-text");
      if (dot) dot.className = "dot ok";
      if (text) text.textContent = "онлайн";
      await this._refreshAudit();
      this._updateHeaderStats();
      this._refreshTrackingBadge();
    } catch (err) {
      console.error("refreshAll error", err);
    }
  },

  async _refreshTrackingBadge() {
    try {
      const res = await fetch("/api/v1/signals/active", { cache: "no-store" });
      if (!res.ok) return;
      const rows = await res.json();
      if (typeof _updateTrackingBadge === "function") _updateTrackingBadge(rows);
    } catch (_err) {
      /* ignore */
    }
  },

  async _refreshAudit() {
    try {
      const res = await fetch("/api/live/audit?max_rows=20000", { cache: "no-store" });
      if (res.ok) this.state.audit = await res.json();
    } catch (err) {
      console.warn("audit refresh failed", err);
    }
    if (this.state.activeTab === "diagnostics" && typeof renderAudit === "function") {
      renderAudit();
    }
  },

  _updateHeaderStats() {
    const o = this.state.overview || {};
    const shortlistEl = document.getElementById("header-shortlist");
    if (shortlistEl) {
      shortlistEl.textContent = o.shortlist_size != null ? o.shortlist_size + " пар" : "—";
    }
    const regimeEl = document.getElementById("header-market-regime");
    if (regimeEl) {
      const regime = String(o.market_regime || "unknown").toLowerCase();
      const map = { bull: "бычий", bear: "медвежий", ranging: "боковик", volatile: "vol" };
      regimeEl.textContent = map[regime] || regime;
    }
    const biasEl = document.getElementById("header-btc-bias");
    if (biasEl) {
      const bias = String(o.btc_bias || "neutral").toLowerCase();
      const map = { uptrend: "BTC ↑", downtrend: "BTC ↓", neutral: "BTC ↔" };
      biasEl.textContent = map[bias] || o.btc_bias || "BTC —";
    }
  },

  _renderAll() {
    if (typeof renderMarketStatePanel === "function") renderMarketStatePanel();
    this._renderActiveTab();
    const lastUpdate = document.getElementById("last-update");
    const runId = document.getElementById("run-id");
    if (lastUpdate) lastUpdate.textContent = "Обновлено: " + new Date().toLocaleTimeString();
    if (runId) runId.textContent = "сессия " + (this.state.overview?.run_id || "—");
  },
};

function text(value) {
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

function number(value, digits = 0) {
  const n = Number(value || 0);
  return n.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
}

function pct(value, digits = 2) {
  return number(Number(value || 0) * 100, digits) + "%";
}

function clsByValue(value) {
  const n = Number(value || 0);
  if (n > 0.25) return "green";
  if (n > 0.05) return "yellow";
  return "red";
}

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "title") node.title = value;
    else if (key === "html") node.innerHTML = value;
    else if (key === "style") node.setAttribute("style", value);
    else if (key === "onclick") node.onclick = value;
    else node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child === null || child === undefined) continue;
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

function setChildren(id, children) {
  const node = document.getElementById(id);
  if (!node) return;
  node.replaceChildren(...children);
}

function kpi(label, value, note = "", color = "") {
  return el("div", { class: "kpi-card" }, [
    el("div", { class: "kpi-label", text: label }),
    el("div", { class: "kpi-value " + color, text: text(value) }),
    el("div", { class: "kpi-note", text: text(note) }),
  ]);
}

function rowsOrEmpty(rows, renderer, emptyText) {
  if (!Array.isArray(rows) || rows.length === 0) {
    return [el("div", { class: "empty", text: emptyText })];
  }
  return rows.map(renderer);
}

function barList(rows, opts = {}) {
  const max = Math.max(...(rows || []).map((r) => Number(r.count || r.value || 0)), 1);
  return rowsOrEmpty(
    rows,
    (row) => {
      const value = Number(row.count || row.value || 0);
      const width = Math.max(2, (value / max) * 100);
      const fillClass = opts.fillClass || "";
      return el("div", { class: "bar-row" }, [
        el("div", { class: "bar-label", title: row.key || row.label || "—", text: row.key || row.label || "—" }),
        el("div", {}, [
          el("div", { class: "bar-track" }, [el("div", { class: "bar-fill " + fillClass, style: "width:" + width + "%" })]),
          el("div", { class: "mono muted", text: number(value, 0) }),
        ]),
      ]);
    },
    "Нет данных"
  );
}

function simpleRow(title, meta, value, valueClass = "") {
  return el("div", { class: "row" }, [
    el("div", { class: "row-main" }, [
      el("div", { class: "row-title", text: text(title) }),
      el("div", { class: "row-meta", text: text(meta) }),
    ]),
    el("div", { class: "row-value " + valueClass, text: text(value) }),
  ]);
}

function table(columns, rows) {
  if (!rows || rows.length === 0) return el("div", { class: "empty", text: "Нет строк" });
  const thead = el("thead", {}, [
    el("tr", {}, columns.map((c) => el("th", { text: c.label, style: c.width ? "width:" + c.width : "" }))),
  ]);
  const tbody = el("tbody", {}, rows.map((row) => el("tr", {}, columns.map((c) => el("td", { class: c.class || "", text: text(c.get(row)) })))));
  return el("table", { class: "table" }, [thead, tbody]);
}

function badge(textContent, cls = "") {
  return el("span", { class: "badge" + (cls ? " " + cls : ""), text: textContent });
}
