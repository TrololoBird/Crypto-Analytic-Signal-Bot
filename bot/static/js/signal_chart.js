"use strict";

async function paintSignalChart(canvas, row, { width = 340, height = 150 } = {}) {
  if (!canvas || !row?.symbol || !window.chart?.signalChart) return;
  const interval = (row.timeframe || "15m").split(/[+,\s]/)[0] || "15m";
  const url =
    "/api/v1/chart/klines?symbol=" +
    encodeURIComponent(row.symbol) +
    "&interval=" +
    encodeURIComponent(interval) +
    "&limit=80";
  try {
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) return;
    const data = await res.json();
    if (!data.klines || data.klines.length < 2) return;
    window.chart.signalChart(
      canvas,
      data.klines,
      {
        entry: Number(row.entry_price || row.entry_mid || 0),
        stop: Number(row.stop_price || 0),
        tp1: Number(row.tp1_price || 0),
        tp2: Number(row.tp2_price || 0),
        tp3: Number(row.tp3_price || 0),
        current: Number(row.current_price || data.mark_price || 0),
        direction: row.direction || "long",
      },
      { width, height }
    );
  } catch (err) {
    console.warn("signal chart error", err);
  }
}

function progressBar(label, pct, tone) {
  const width = Math.max(2, Math.min(100, Number(pct || 0)));
  const fillClass =
    tone === "green" ? "green" : tone === "red" ? "red" : tone === "yellow" ? "yellow" : "";
  return el("div", { style: "margin-top:8px" }, [
    el("div", { class: "soft", style: "font-size:12px;margin-bottom:4px", text: label || "—" }),
    el("div", { class: "bar-track" }, [
      el("div", { class: "bar-fill " + fillClass, style: "width:" + width + "%" }),
    ]),
  ]);
}
