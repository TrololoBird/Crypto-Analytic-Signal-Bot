"use strict";

function renderMarketStatePanel() {
  const o = App.state.overview || {};
  const ms = o.market_state || {};
  const display = ms.display || {};
  const container = document.getElementById("market-state-panel");
  const detail = document.getElementById("market-context-detail");
  if (!container) return;

  if (!ms.available && !display.breadth_total) {
    container.replaceChildren(
      el("div", {
        class: "empty",
        text: "Контекст рынка появится через 1–2 мин после старта бота (REST + WS klines). Telegram: /market",
      })
    );
    if (detail) detail.replaceChildren();
    return;
  }

  const regime = String(display.regime || o.market_regime || ms.regime || "unknown");
  const strength = Number(o.market_strength ?? ms.strength ?? 0);
  const fear = display.fear_greed_value != null ? `${display.fear_greed_value} (${display.fear_greed_label || ""})` : "—";
  const breadth =
    display.breadth_total != null
      ? `${display.breadth_positive || 0}/${display.breadth_total} (${Math.round(display.breadth_pct || 0)}%)`
      : "—";

  const cards = [
    kpi("Режим", _marketRegimeRu(regime), display.risk_label || "risk proxy", _regimeClass(regime)),
    kpi("Fear/Greed", fear, "breadth + BTC proxy", _fearClass(display.fear_greed_value)),
    kpi("Breadth", breadth, "liquid futures 24h", _breadthClass(display.breadth_pct)),
    kpi("BTC bias", _marketBiasRu(o.btc_bias || ms.btc_bias), "24h + структура", _biasClass(o.btc_bias)),
    kpi("ETH bias", _marketBiasRu(o.eth_bias || ms.eth_bias), "контекст альтов", _biasClass(o.eth_bias)),
    kpi("Фаза BTC", _btcPhaseRu(o.btc_phase || ms.btc_phase), "impulse / decline", "orange"),
    kpi("Vol regime", _volRegimeRu(o.volatility_regime || ms.volatility_regime), "ATR expansion", "cyan"),
    kpi("Funding", _fundRu(o.funding_sentiment || ms.funding_sentiment), "crowding", "yellow"),
  ];
  container.replaceChildren(...cards);

  if (!detail) return;

  const lines = [];
  if (display.practical) lines.push(`Практически: ${display.practical}`);
  if (display.tf_4h) lines.push(display.tf_4h);
  if (display.tf_1h) lines.push(display.tf_1h);
  if (display.tf_15m) lines.push(display.tf_15m);
  if (display.btc_24h_pct != null) {
    lines.push(
      `Драйверы: BTC ${_pct(display.btc_24h_pct)} · ETH ${_pct(display.eth_24h_pct)} · SOL ${_pct(display.sol_24h_pct)}`
    );
  }
  if (display.volume_btc_pct != null) {
    lines.push(
      `Объём: BTC ${_pct(display.volume_btc_pct, false)} · ETH ${_pct(display.volume_eth_pct, false)} · ` +
        `альты ${_pct(display.volume_alts_pct, false)}`
    );
  }
  if (display.macro_line) lines.push(`Макро: ${display.macro_line}`);
  if (display.corr_line) lines.push(display.corr_line);
  if (display.corr_narrative) lines.push(display.corr_narrative);
  if (display.leaders) lines.push(`Лидеры: ${display.leaders}`);
  if (display.laggards) lines.push(`Аутсайдеры: ${display.laggards}`);
  if (display.tracking_active != null) {
    lines.push(`Сопровождение: active ${display.tracking_active} · pending ${display.tracking_pending || 0}`);
  }
  if (display.updated_at) lines.push(`Обновлено: ${display.updated_at}`);

  if (!lines.length) {
    detail.replaceChildren(
      el("div", { class: "panel-hint muted", text: "Детальный контекст — /market в Telegram или подождите прогрева." })
    );
    return;
  }

  detail.replaceChildren(
    el("div", { class: "market-context-detail-body" }, lines.map((text) => el("div", { class: "context-line", text })))
  );
}

function _pct(value, signed = true) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return signed ? `${n >= 0 ? "+" : ""}${n.toFixed(1)}%` : `${n.toFixed(1)}%`;
}

function _marketRegimeRu(regime) {
  const map = {
    bull: "бычий",
    bear: "медвежий",
    ranging: "боковик",
    volatile: "волатильность",
    unknown: "—",
  };
  return map[String(regime || "unknown").toLowerCase()] || regime;
}

function _marketBiasRu(bias) {
  const map = { uptrend: "рост ↑", downtrend: "падение ↓", neutral: "нейтрально" };
  return map[String(bias || "neutral").toLowerCase()] || bias || "—";
}

function _btcPhaseRu(phase) {
  const map = {
    impulse: "импульс",
    decline: "снижение",
    accumulation: "накопление",
    distribution: "распределение",
    sideways: "боковик",
    unknown: "—",
  };
  return map[String(phase || "unknown").toLowerCase()] || phase || "—";
}

function _volRegimeRu(v) {
  const map = { expanding: "расширение", contracting: "сжатие", stable: "стабильно" };
  return map[String(v || "stable").toLowerCase()] || v || "—";
}

function _fundRu(v) {
  const map = { long_heavy: "лонги перегреты", short_heavy: "шорты перегреты", neutral: "нейтрально" };
  return map[String(v || "neutral")] || String(v || "neutral").replace(/_/g, " ");
}

function _regimeClass(regime) {
  const r = String(regime || "").toLowerCase();
  if (r === "bull") return "green";
  if (r === "bear") return "red";
  if (r === "volatile") return "orange";
  return "muted";
}

function _biasClass(bias) {
  const b = String(bias || "").toLowerCase();
  if (b === "uptrend") return "green";
  if (b === "downtrend") return "red";
  return "muted";
}

function _fearClass(value) {
  const v = Number(value);
  if (!Number.isFinite(v)) return "muted";
  if (v <= 24) return "red";
  if (v <= 44) return "orange";
  if (v <= 55) return "muted";
  if (v <= 75) return "yellow";
  return "green";
}

function _breadthClass(pct) {
  const v = Number(pct);
  if (!Number.isFinite(v)) return "muted";
  if (v >= 60) return "green";
  if (v <= 40) return "red";
  return "yellow";
}
