"use strict";
const App = {
  state: {
    activeTab: "overview",
    overview: null,
    funnel: null,
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
  },

  init() {
    this._bindTabs();
    this._bindRefresh();
    this._bindKeys();
    this._initWS();
    this.refreshAll();
    setInterval(() => this.refreshAll(), 10000);
  },

  _initWS() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${proto}//${location.host}/api/v1/ws`;
    let reconnectDelay = 1000;
    const connect = () => {
      const ws = new WebSocket(wsUrl);
      ws.onopen = () => {
        document.getElementById("status-dot").className = "dot ok";
        document.getElementById("status-text").textContent = "online";
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
        document.getElementById("status-dot").className = "dot";
        document.getElementById("status-text").textContent = "reconnecting";
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
      case "signal":
        if (!this.state.signals.find((s) => s.signal_id === data.payload.signal_id)) {
          this.state.signals.unshift(data.payload);
          if (this.state.signals.length > 200) this.state.signals.length = 200;
        }
        if (this.state.activeTab === "river" && typeof renderRiver === "function") {
          _fetchRiverSignals();
        }
        break;
      case "regime_update":
        this.state.regime = data.payload;
        if (this.state.activeTab === "weather" && typeof renderWeather === "function") {
          fetch("/api/v1/market/regime").then(r => r.ok && r.json()).then(d => { App.state.regime = d; renderWeather(); });
        }
        break;
      case "shortlist_update":
        break;
      case "telemetry_update":
        break;
      case "cycle_complete":
        this.refreshAll();
        break;
    }
  },

  _bindTabs() {
    document.querySelectorAll(".tab").forEach((btn) => {
      btn.addEventListener("click", () => this.switchTab(btn.dataset.tab));
    });
  },

  _bindRefresh() {
    document.getElementById("refresh-button")?.addEventListener("click", () => this.refreshAll());
  },

  _bindKeys() {
    document.addEventListener("keydown", (e) => {
      if ((e.key === "r" && (e.ctrlKey || e.metaKey))) {
        e.preventDefault();
        this.refreshAll();
      }
      const map = {
        1: "overview", 2: "funnel", 3: "audit", 4: "shortlist",
        5: "strategies", 6: "delivery", 7: "runtime", 8: "river",
        9: "diary", 0: "weather", l: "lab", a: "alerts",
        s: "settings", b: "sandbox",
      };
      if (map[e.key] && !e.ctrlKey && !e.metaKey && !e.altKey) {
        this.switchTab(map[e.key]);
      }
    });
  },

  switchTab(name) {
    this.state.activeTab = name;
    document.querySelectorAll(".tab").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.tab === name);
    });
    document.querySelectorAll(".section").forEach((sec) => {
      sec.classList.toggle("active", sec.id === name);
    });
    if (name === "river" && typeof renderRiver === "function") renderRiver();
    if (name === "diary" && typeof renderDiary === "function") renderDiary();
    if (name === "weather" && typeof renderWeather === "function") renderWeather();
    if (name === "lab" && typeof renderConfluenceLab === "function") renderConfluenceLab();
    if (name === "settings" && typeof renderSettings === "function") renderSettings();
    if (name === "alerts" && typeof renderAlerts === "function") renderAlerts();
    if (name === "sandbox" && typeof renderSandbox === "function") renderSandbox();
    if (name === "runtime" && typeof renderEngineRoom === "function") renderEngineRoom();
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
      document.getElementById("status-dot").className = "dot ok";
      document.getElementById("status-text").textContent = "online";
      this._refreshAudit();
    } catch (err) {
      console.error("refreshAll error", err);
    }
  },

  async _refreshAudit() {
    try {
      const res = await fetch("/api/live/audit?max_rows=20000", { cache: "no-store" });
      if (res.ok) {
        this.state.audit = await res.json();
      }
    } catch (err) {
      console.warn("audit refresh failed", err);
      this.state.audit = {
        status: "unavailable", score: 0,
        summary: { total: 1, by_severity: { warning: 1 } },
        operator_brief: "Audit temporarily unavailable.",
        action_plan: ["Retry refresh or inspect server logs for /api/live/audit."],
        findings: [{ severity: "warning", area: "dashboard", code: "audit_unavailable",
          title: "Audit endpoint unavailable.", detail: String(err), recommendation: "Keep using other panels." }],
      };
    }
    if (typeof renderAudit === "function") renderAudit();
  },

  _renderAll() {
    if (typeof renderOverview === "function") renderOverview();
    if (typeof renderFunnel === "function") renderFunnel();
    if (typeof renderShortlist === "function") renderShortlist();
    if (typeof renderStrategies === "function") renderStrategies();
    if (typeof renderDelivery === "function") renderDelivery();
    if (typeof renderRuntime === "function") renderRuntime();
    document.getElementById("last-update").textContent = "Last update: " + new Date().toLocaleTimeString();
    document.getElementById("run-id").textContent = "run " + (this.state.overview?.run_id || "-");
  },
};

function text(value) {
  if (value === null || value === undefined || value === "") return "-";
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
        el("div", { class: "bar-label", title: row.key || row.label || "-", text: row.key || row.label || "-" }),
        el("div", {}, [
          el("div", { class: "bar-track" }, [el("div", { class: "bar-fill " + fillClass, style: "width:" + width + "%" })]),
          el("div", { class: "mono muted", text: number(value, 0) }),
        ]),
      ]);
    },
    "No telemetry rows"
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
  if (!rows || rows.length === 0) return el("div", { class: "empty", text: "No rows" });
  const thead = el("thead", {}, [
    el("tr", {}, columns.map((c) => el("th", { text: c.label, style: c.width ? "width:" + c.width : "" }))),
  ]);
  const tbody = el("tbody", {}, rows.map((row) => el("tr", {}, columns.map((c) => el("td", { class: c.class || "", text: text(c.get(row)) })))));
  return el("table", { class: "table" }, [thead, tbody]);
}

function badge(textContent, cls = "") {
  return el("span", { class: "badge" + (cls ? " " + cls : ""), text: textContent });
}

function smallBtn(label, onClick) {
  const btn = el("button", { class: "tab", text: label });
  btn.addEventListener("click", onClick);
  return btn;
}

function renderRiver() {}  /* placeholder — implemented in Phase 2 */
function renderWeather() {} /* placeholder — implemented in Phase 3 */
