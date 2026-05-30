let riverSignals = [];
let riverInterval = null;
const RIVER_COLORS = {
  breakout: "#63a5ff", reversal: "#ff5b6b", continuation: "#2fd17c",
  volatility: "#ff9f43", liquidity: "#53d5d5", orderflow: "#a78bfa",
  sentiment: "#f472b6", multi_asset: "#34d399", session: "#f5bf4f",
  orderbook: "#6b7280", generic: "#95a2b4",
};

function renderRiver() {
  _fetchRiverSignals();
  if (riverInterval) clearInterval(riverInterval);
  riverInterval = setInterval(_fetchRiverSignals, 15000);
}

function _fetchRiverSignals() {
  fetch("/api/v1/signals/live?limit=30", { cache: "no-store" })
    .then((r) => r.ok ? r.json() : [])
    .then((data) => {
      riverSignals = data;
      _renderRiverCards(data);
      _renderRiverKPIs(data);
    })
    .catch((err) => console.warn("river fetch error", err));
}

function _renderRiverKPIs(signals) {
  const active = signals.filter((s) => s.delivery_status !== "candidate");
  const avgScore = signals.length
    ? signals.reduce((a, s) => a + (s.confluence_score || 0), 0) / signals.length
    : 0;
  setChildren("river-kpis", [
    kpi("Live Signals", signals.length, "in feed", "blue"),
    kpi("Delivered", active.length, "notifier accepted", "green"),
    kpi("Avg Score", avgScore.toFixed(1), "last " + signals.length, clsByValue(avgScore / 100)),
    kpi("Candidates", signals.filter((s) => s.delivery_status === "candidate").length, "raw hits", "yellow"),
    kpi("Direction split", _directionSplit(signals), "", "cyan"),
    kpi("Killzone", _activeKillzone(signals), "current session", "orange"),
  ]);
}

function _directionSplit(signals) {
  const longs = signals.filter((s) => (s.direction || "long") === "long").length;
  return longs + "L / " + (signals.length - longs) + "S";
}

function _activeKillzone(signals) {
  const kz = signals[0]?.killzone || {};
  if (kz.london) return "London";
  if (kz.ny) return "NY";
  if (kz.asia) return "Asia";
  return "off";
}

function _renderRiverCards(signals) {
  const container = document.getElementById("river-cards");
  if (!container) return;
  if (!signals.length) {
    container.replaceChildren(el("div", { class: "empty", text: "No signals in feed. Waiting for WebSocket data..." }));
    return;
  }
  const cards = signals.map(_buildSignalCard);
  container.replaceChildren(...cards);
}

function _buildSignalCard(sig) {
  const score = sig.confluence_score || 0;
  const dir = (sig.direction || "long").toUpperCase();
  const dirColor = dir === "LONG" ? "var(--green)" : "var(--red)";
  const kz = sig.killzone || {};
  const hasKZ = kz.london || kz.ny || kz.asia;
  const kzLabel = hasKZ
    ? (kz.london ? "London" : kz.ny ? "NY" : "Asia")
    : null;

  const pillColors = { breakout: "#63a5ff", reversal: "#ff5b6b", continuation: "#2fd17c",
    volatility: "#ff9f43", liquidity: "#53d5d5", orderflow: "#a78bfa",
    sentiment: "#f472b6", multi_asset: "#34d399", session: "#f5bf4f",
    orderbook: "#6b7280" };

  const strategies = sig.active_strategies || [];
  if (sig.setup_id && !strategies.find((s) => s.id === sig.setup_id)) {
    strategies.unshift({ id: sig.setup_id, family: "generic" });
  }

  return el("div", { class: "signal-card" }, [
    el("div", { class: "sc-head" }, [
      el("div", { style: "display:flex;align-items:center;gap:8px" }, [
        el("span", { class: "sc-symbol", style: "color:" + dirColor, text: sig.symbol || "?" }),
        el("span", { style: "font-size:12px;color:" + dirColor + ";font-weight:700", text: dir }),
      ]),
      el("span", {
        class: "sc-confluence",
        style: "background:" + sig.confluence_color + "20;color:" + sig.confluence_color + ";border:1px solid " + sig.confluence_color + "40",
        text: score >= 10 ? Math.round(score) + "" : "<10",
      }),
    ]),
    el("div", { class: "sc-pills" }, strategies.slice(0, 8).map((s) => {
      const family = s.family || "generic";
      const c = pillColors[family] || pillColors.generic;
      return el("span", {
        class: "sc-pill",
        style: "background:" + c + "20;color:" + c + ";border:1px solid " + c + "40",
        text: s.id.length > 18 ? s.id.slice(0, 16) + ".." : s.id,
      });
    })),
    el("div", { class: "sc-meta" }, [
      kzLabel ? el("span", { text: "⚡ " + kzLabel }) : null,
      el("span", { text: sig.market_regime || "-" }),
      el("span", { text: sig.timeframe || "15m" }),
      el("span", { text: _timeAgo(sig.ts || sig.timestamp || sig.created_at) }),
    ].filter(Boolean)),
    el("div", { class: "sc-zone" }, [
      _zoneItem("Entry", sig.entry_price || sig.entry_low || "-"),
      _zoneItem("SL", sig.stop_price || sig.stop || "-", "red"),
      _zoneItem("TP", sig.tp1_price || sig.take_profit_1 || "-", "green"),
    ]),
    sig.tracking_id
      ? el("div", { style: "margin-top:4px" }, [
          el("button", {
            class: "tab",
            text: "Log Decision",
            style: "width:100%;border:1px solid var(--line);padding:6px;font-size:12px",
            onclick: "showDiaryEntryModal(" + JSON.stringify(sig).replace(/"/g, "'") + ")",
          }),
        ])
      : null,
  ].filter(Boolean));
}

function _zoneItem(label, price, color) {
  return el("div", { class: "sc-zone-item" }, [
    el("div", { class: "sc-zone-label", text: label }),
    el("div", { class: "sc-zone-price", style: color ? "color:var(--" + color + ")" : "", text: typeof price === "number" ? price.toFixed(2) : String(price) }),
  ]);
}

function _timeAgo(ts) {
  if (!ts) return "";
  const diff = Date.now() - new Date(ts).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return mins + "m ago";
  const hrs = Math.floor(mins / 60);
  return hrs + "h " + (mins % 60) + "m ago";
}
