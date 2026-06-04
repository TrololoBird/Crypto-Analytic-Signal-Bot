"use strict";

let trackingInterval = null;

const TRACKING_STATUS_RU = {
  pending: "Ждём входа",
  active: "В сделке",
  closed: "Закрыт",
  expired: "Истёк срок",
  cancelled: "Отменён",
};

function renderTracking() {
  _fetchTracking();
  if (trackingInterval) clearInterval(trackingInterval);
  trackingInterval = setInterval(_fetchTracking, 10000);
}

function _fetchTracking() {
  fetch("/api/v1/signals/active", { cache: "no-store" })
    .then((r) => (r.ok ? r.json() : []))
    .then((rows) => {
      _renderTrackingKPIs(rows);
      _renderTrackingCards(rows);
      _updateTrackingBadge(rows);
    })
    .catch((err) => console.warn("tracking fetch error", err));
}

function _updateTrackingBadge(rows) {
  const open = rows.filter((r) => ["pending", "active"].includes(String(r.status || "")));
  const badge = document.getElementById("tracking-badge");
  if (!badge) return;
  if (open.length > 0) {
    badge.textContent = String(open.length);
    badge.style.display = "inline-flex";
  } else {
    badge.style.display = "none";
  }
}

function _renderTrackingKPIs(rows) {
  const pending = rows.filter((r) => r.status === "pending").length;
  const active = rows.filter((r) => r.status === "active").length;
  const closed = rows.filter((r) => r.status === "closed").length;
  setChildren("tracking-kpis", [
    kpi("Всего", rows.length, "открытые планы", "blue"),
    kpi("Ждут входа", pending, "pending", pending ? "yellow" : "muted"),
    kpi("В сделке", active, "active", active ? "green" : "muted"),
    kpi("Закрыты", closed, "за сессию", "cyan"),
  ]);
}

function _renderTrackingCards(rows) {
  const container = document.getElementById("tracking-cards");
  if (!container) return;
  if (!rows.length) {
    container.replaceChildren(
      el("div", { class: "empty" }, [
        el("div", { text: "Нет отслеживаемых сигналов" }),
        el("div", {
          class: "soft",
          style: "margin-top:8px;font-size:13px",
          text: "Когда бот отправит сигнал, здесь появятся вход, стоп, цели и прогресс до них.",
        }),
      ])
    );
    return;
  }
  container.replaceChildren(...rows.map((row, idx) => _buildTrackingCard(row, idx)));
  rows.forEach((row, idx) => {
    const canvas = document.getElementById("track-chart-" + idx);
    if (canvas) paintSignalChart(canvas, row);
  });
}

function _buildTrackingCard(row, idx) {
  const dir = String(row.direction || "long").toLowerCase();
  const dirRu = dir === "short" ? "Шорт" : "Лонг";
  const dirColor = dir === "short" ? "var(--red)" : "var(--green)";
  const status = String(row.status || "unknown");
  const statusRu = TRACKING_STATUS_RU[status] || status;
  const statusClass =
    status === "active" ? "green" : status === "pending" ? "yellow" : "muted";

  const fmt = (v) => (v == null || v === "" ? "—" : _fmtPrice(v));
  const current = row.current_price != null ? fmt(row.current_price) : "—";

  const tpHits = [];
  if (row.tp1_hit_at) tpHits.push("TP1 ✓");
  if (row.tp2_hit_at) tpHits.push("TP2 ✓");

  return el("div", {
    class: "signal-card",
    style: "cursor:pointer",
    onclick: () => showSignalDetail(row),
  }, [
    el("div", { class: "sc-head" }, [
      el("div", { style: "display:flex;align-items:center;gap:8px;flex-wrap:wrap" }, [
        el("span", { class: "sc-symbol", style: "color:" + dirColor, text: row.symbol || "?" }),
        el("span", { style: "font-size:12px;color:" + dirColor, text: dirRu }),
        el("span", { class: "badge " + statusClass, text: statusRu }),
      ]),
      el("span", {
        class: "mono soft",
        style: "font-size:12px",
        text: row.setup_id || "—",
      }),
    ]),
    el("canvas", {
      id: "track-chart-" + idx,
      width: 340,
      height: 150,
      style: "width:100%;height:150px;border-radius:8px;margin-top:4px",
    }),
    progressBar(row.progress_label || "Прогресс", row.progress_pct, row.progress_tone),
    el("div", { class: "sc-zone" }, [
      _zoneItemRu("Вход", fmt(row.entry_price)),
      _zoneItemRu("Стоп", fmt(row.stop_price), "red"),
      _zoneItemRu("Сейчас", current, "yellow"),
    ]),
    el("div", { class: "sc-zone", style: "margin-top:6px" }, [
      _zoneItemRu("Цель 1", fmt(row.tp1_price), "green"),
      _zoneItemRu("Цель 2", fmt(row.tp2_price), "green"),
      _zoneItemRu("Цель 3", fmt(row.tp3_price), "green"),
    ]),
    el("div", { class: "sc-meta" }, [
      el("span", { text: "R:R " + (row.risk_reward != null ? Number(row.risk_reward).toFixed(2) : "—") }),
      el("span", {
        text:
          row.unrealized_pnl_pct != null
            ? (row.unrealized_pnl_pct >= 0 ? "+" : "") + row.unrealized_pnl_pct + "%"
            : "—",
      }),
      tpHits.length ? el("span", { text: tpHits.join(" · ") }) : null,
      el("span", { text: _timeAgoRu(row.timestamp) }),
    ].filter(Boolean)),
  ]);
}

function _fmtPrice(v) {
  const n = Number(v);
  if (Number.isNaN(n)) return String(v);
  if (n >= 1000) return n.toFixed(2);
  if (n >= 1) return n.toFixed(4);
  return n.toFixed(6);
}

function _zoneItemRu(label, price, color) {
  return el("div", { class: "sc-zone-item" }, [
    el("div", { class: "sc-zone-label", text: label }),
    el("div", {
      class: "sc-zone-price",
      style: color ? "color:var(--" + color + ")" : "",
      text: String(price),
    }),
  ]);
}

function _timeAgoRu(ts) {
  if (!ts) return "";
  const diff = Date.now() - new Date(ts).getTime();
  if (Number.isNaN(diff)) return "";
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "только что";
  if (mins < 60) return mins + " мин назад";
  const hrs = Math.floor(mins / 60);
  return hrs + " ч назад";
}
