let weatherInterval = null;

function renderWeather() {
  _fetchWeather();
  if (weatherInterval) clearInterval(weatherInterval);
  weatherInterval = setInterval(_fetchWeather, 30000);
}

function _fetchWeather() {
  fetch("/api/v1/market/regime", { cache: "no-store" })
    .then((r) => r.ok ? r.json() : null)
    .then((data) => {
      if (!data || data.error) return;
      _renderGauges(data);
      _renderRegime(data);
      _renderFunding(data);
      _renderSessions(data);
      _renderSparkline(data);
    })
    .catch((err) => console.warn("weather fetch error", err));
}

function _renderGauges(data) {
  const altIdx = data.altcoin_season_index ?? 50;
  const strength = (data.strength ?? 0.5) * 100;
  const confidence = (data.confidence ?? 0.5) * 100;

  const container = document.getElementById("weather-gauges");
  if (!container) return;

  const items = [
    { label: "Сезон альтов", value: altIdx, hint: altIdx > 60 ? "альты сильнее BTC" : "доминирует BTC" },
    { label: "Сила рынка", value: strength, hint: _regimeRu(data.regime) },
    { label: "Фаза BTC", value: confidence, hint: data.btc_phase || "—" },
    { label: "Уверенность", value: confidence, hint: _riskRu(data.risk_on_off) },
  ];

  const children = items.map((item) => {
    const card = el("div", { class: "kpi-card" }, [
      el("div", { class: "kpi-label", text: item.label }),
      el("canvas", { width: 160, height: 90, style: "width:100%;height:90px" }),
      el("div", { class: "kpi-note", text: item.hint }),
    ]);
    requestAnimationFrame(() => {
      const canvas = card.querySelector("canvas");
      if (canvas) window.chart.gauge(canvas, item.value, { width: 320, height: 90 });
    });
    return card;
  });
  container.replaceChildren(...children);
}

function _renderRegime(data) {
  const container = document.getElementById("weather-regime");
  if (!container) return;
  const rows = [
    simpleRow("Режим", data.volatility_regime || "—", _regimeRu(data.regime), "blue"),
    simpleRow("BTC", _biasRu(data.btc_bias), "", "cyan"),
    simpleRow("ETH", _biasRu(data.eth_bias), "", "cyan"),
    simpleRow("Фаза BTC", data.btc_phase || "—", "", "orange"),
    simpleRow("Риск", _riskRu(data.risk_on_off), "", data.risk_on_off === "risk_on" ? "green" : "yellow"),
    simpleRow("Доминация 24ч", "изменение BTC", (data.dominance_24h || 0).toFixed(2) + "%", Number(data.dominance_24h || 0) >= 0 ? "green" : "red"),
  ];
  container.replaceChildren(...rows);
}

function _renderFunding(data) {
  const container = document.getElementById("weather-funding");
  if (!container) return;
  const fundSent = data.funding_sentiment || "neutral";
  const oiMom = data.oi_momentum || "stable";
  const rows = [
    simpleRow("Funding", "настроение", _fundRu(fundSent),
      fundSent === "long_heavy" ? "red" : fundSent === "short_heavy" ? "green" : "muted"),
    simpleRow("Open Interest", "динамика", _oiRu(oiMom),
      oiMom === "rising" ? "green" : oiMom === "falling" ? "red" : "muted"),
    simpleRow("Сезон альтов", "0–100", text(data.altcoin_season_index ?? "—"),
      (data.altcoin_season_index || 0) > 60 ? "green" : "yellow"),
    simpleRow("Лидер роста", "24ч", (data.top_gainer_pct || 0).toFixed(2) + "%", "green"),
    simpleRow("Лидер падения", "24ч", (data.top_loser_pct || 0).toFixed(2) + "%", "red"),
  ];
  container.replaceChildren(...rows);
}

function _renderSessions(data) {
  const container = document.getElementById("weather-sessions");
  if (!container) return;
  const now = new Date();
  const hour = now.getUTCHours() + now.getUTCMinutes() / 60;
  const sessions = [
    { name: "Азия", active: hour >= 0 && hour < 9, quality: "средняя", color: "yellow" },
    { name: "Лондон", active: hour >= 8 && hour < 17, quality: "высокая", color: "blue" },
    { name: "Нью-Йорк", active: hour >= 13 && hour < 22, quality: "высокая", color: "green" },
  ];
  const rows = sessions.map((s) =>
    simpleRow(
      s.name + (s.active ? " ●" : ""),
      (s.active ? "активна" : "закрыта") + " · " + s.quality,
      s.active ? "можно торговать" : "ожидание",
      s.color
    )
  );
  container.replaceChildren(...rows);
}

function _renderSparkline(data) {
  const canvas = document.getElementById("weather-sparkline");
  if (!canvas) return;
  const mockData = [
    { bucket: "-12h", value: (data.strength || 0.5) * 100 },
    { bucket: "-6h", value: ((data.strength || 0.5) + 0.05) * 100 },
    { bucket: "-3h", value: ((data.strength || 0.5) - 0.02) * 100 },
    { bucket: "-1h", value: ((data.strength || 0.5) + 0.08) * 100 },
    { bucket: "now", value: (data.strength || 0.5) * 100 },
  ];
  window.chart.sparkline(canvas, mockData, { width: 300, height: 60, color: "#63a5ff" });
  _renderMarketShortlist();
}

function _regimeRu(regime) {
  const map = {
    bull: "бычий",
    bear: "медвежий",
    trending: "тренд",
    ranging: "боковик",
    volatile: "волатильность",
    breakout: "пробой",
    unknown: "неизвестно",
  };
  return map[String(regime || "unknown").toLowerCase()] || regime || "—";
}

function _biasRu(bias) {
  if (!bias) return "—";
  const map = { uptrend: "рост ↑", downtrend: "падение ↓", neutral: "боковик" };
  return map[String(bias).toLowerCase()] || bias;
}

function _riskRu(v) {
  const map = { risk_on: "риск вкл", risk_off: "осторожно", neutral: "нейтрально" };
  return map[String(v || "neutral")] || String(v || "neutral").replace(/_/g, " ");
}

function _fundRu(v) {
  const map = { long_heavy: "перегруз лонгов", short_heavy: "перегруз шортов", neutral: "нейтрально" };
  return map[String(v)] || String(v).replace(/_/g, " ");
}

function _oiRu(v) {
  const map = { rising: "растёт", falling: "падает", stable: "стабильно" };
  return map[String(v)] || v;
}

function _renderMarketShortlist() {
  const container = document.getElementById("market-shortlist");
  if (!container) return;
  const sl = App.state.shortlist || {};
  const rows = (sl.symbols || sl.items || []).slice(0, 8);
  if (!rows.length) {
    container.replaceChildren(el("div", { class: "empty", text: "Shortlist загружается вместе с ботом" }));
    return;
  }
  const tableRows = rows.map((row) => {
    const sym = row.symbol || row;
    const score = row.score ?? row.fit_score ?? row.priority_score;
    return simpleRow(
      typeof sym === "string" ? sym : sym.symbol || "?",
      row.setup_fits != null ? "стратегий: " + row.setup_fits : "в shortlist",
      score != null ? Number(score).toFixed(2) : "—",
      "blue"
    );
  });
  container.replaceChildren(el("div", { class: "row-list" }, tableRows));
}
