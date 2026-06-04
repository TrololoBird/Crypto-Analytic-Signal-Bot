"use strict";

const DETAIL_HELP = {
  long: "Лонг — ставка на рост цены. Прибыль, если цена идёт вверх к целям.",
  short: "Шорт — ставка на падение. Прибыль, если цена идёт вниз к целям.",
  entry: "Вход — зона, где планируется открыть позицию.",
  stop: "Стоп — уровень, где идея отменяется и фиксируется убыток.",
  tp: "Цель (TP) — уровень частичной или полной фиксации прибыли.",
  rr: "R:R — соотношение потенциальной прибыли к риску. 1.9 ≈ прибыль в 1.9× больше риска.",
};

function showSignalDetail(row) {
  if (!row) return;
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay detail-overlay";
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) overlay.remove();
  });

  const dir = String(row.direction || "long").toLowerCase();
  const dirRu = dir === "short" ? "Шорт" : "Лонг";

  const modal = el("div", { class: "modal detail-modal" }, [
    el("h2", { text: (row.symbol || "?") + " · " + dirRu }),
    el("p", { class: "muted", text: row.setup_id || "—" }),
    el("canvas", {
      id: "detail-chart-canvas",
      width: 480,
      height: 220,
      style: "width:100%;height:220px;border-radius:8px;margin:12px 0",
    }),
    el("div", { class: "help-grid" }, [
      _helpBlock("Направление", DETAIL_HELP[dir] || DETAIL_HELP.long),
      _helpBlock("Вход", DETAIL_HELP.entry + " " + _fmt(row.entry_price || row.entry_mid)),
      _helpBlock("Стоп", DETAIL_HELP.stop + " " + _fmt(row.stop_price)),
      _helpBlock(
        "Цели",
        DETAIL_HELP.tp +
          " TP1 " +
          _fmt(row.tp1_price) +
          (row.tp2_price ? " · TP2 " + _fmt(row.tp2_price) : "") +
          (row.tp3_price ? " · TP3 " + _fmt(row.tp3_price) : "")
      ),
      row.risk_reward != null
        ? _helpBlock("R:R", DETAIL_HELP.rr + " " + Number(row.risk_reward).toFixed(2))
        : null,
      row.progress_label
        ? _helpBlock("Сейчас", row.progress_label)
        : row.result_ru
          ? _helpBlock("Исход", row.result_ru)
          : null,
    ].filter(Boolean)),
    el("div", { class: "modal-buttons" }, [
      el("button", {
        type: "button",
        text: "TradingView",
        onclick: () => window.open(_tradingViewUrl(row.symbol), "_blank"),
      }),
      row.tracking_id
        ? el("button", {
            type: "button",
            class: "primary",
            text: "Записать в дневник",
            onclick: () => {
              overlay.remove();
              showDiaryEntryModal(row);
            },
          })
        : null,
      el("button", {
        type: "button",
        text: "Закрыть",
        onclick: () => overlay.remove(),
      }),
    ].filter(Boolean)),
  ]);

  overlay.appendChild(modal);
  document.body.appendChild(overlay);
  requestAnimationFrame(() => {
    const canvas = document.getElementById("detail-chart-canvas");
    if (canvas) paintSignalChart(canvas, row, { width: 480, height: 220 });
  });
}

function _helpBlock(title, body) {
  return el("div", { class: "help-block" }, [
    el("div", { class: "help-title", text: title }),
    el("div", { class: "help-body", text: body }),
  ]);
}

function _fmt(v) {
  if (v == null || v === "") return "—";
  const n = Number(v);
  return Number.isNaN(n) ? String(v) : n.toFixed(n >= 1 ? 4 : 6);
}

function _tradingViewUrl(symbol) {
  const sym = String(symbol || "BTCUSDT").replace("USDT", "");
  return "https://www.tradingview.com/chart/?symbol=BINANCE:" + sym + "USDT.P&interval=15";
}
