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
    { label: "Alt Season Index", value: altIdx, hint: altIdx > 60 ? "Alt season" : "BTC dominance" },
    { label: "Market Strength", value: strength, hint: data.regime || "unknown" },
    { label: "BTC Phase", value: confidence, hint: data.btc_phase || "-" },
    { label: "Confidence", value: confidence, hint: (data.risk_on_off || "neutral").replace("_", " ") },
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
    simpleRow("Regime", data.volatility_regime || "-", data.regime || "unknown", "blue"),
    simpleRow("BTC Bias", data.btc_bias || "-", "", "cyan"),
    simpleRow("ETH Bias", data.eth_bias || "-", "", "cyan"),
    simpleRow("BTC Phase", data.btc_phase || "-", "", "orange"),
    simpleRow("Risk", data.risk_on_off || "neutral", "", data.risk_on_off === "risk_on" ? "green" : "yellow"),
    simpleRow("Dominance 24h", "BTC change", (data.dominance_24h || 0).toFixed(2) + "%", Number(data.dominance_24h || 0) >= 0 ? "green" : "red"),
  ];
  container.replaceChildren(...rows);
}

function _renderFunding(data) {
  const container = document.getElementById("weather-funding");
  if (!container) return;
  const fundSent = data.funding_sentiment || "neutral";
  const oiMom = data.oi_momentum || "stable";
  const rows = [
    simpleRow("Funding Sentiment", "aggregate", fundSent.replace("_", " "),
      fundSent === "long_heavy" ? "red" : fundSent === "short_heavy" ? "green" : "muted"),
    simpleRow("OI Momentum", "", oiMom,
      oiMom === "rising" ? "green" : oiMom === "falling" ? "red" : "muted"),
    simpleRow("Alt Season", "0-100 index", text(data.altcoin_season_index ?? "-"),
      (data.altcoin_season_index || 0) > 60 ? "green" : "yellow"),
    simpleRow("Top Gainer", "24h", (data.top_gainer_pct || 0).toFixed(2) + "%", "green"),
    simpleRow("Top Loser", "24h", (data.top_loser_pct || 0).toFixed(2) + "%", "red"),
  ];
  container.replaceChildren(...rows);
}

function _renderSessions(data) {
  const container = document.getElementById("weather-sessions");
  if (!container) return;
  const now = new Date();
  const hour = now.getUTCHours() + now.getUTCMinutes() / 60;
  const sessions = [
    { name: "Asia", active: hour >= 0 && hour < 9, quality: "medium", color: "yellow" },
    { name: "London", active: hour >= 8 && hour < 17, quality: "high", color: "blue" },
    { name: "NY", active: hour >= 13 && hour < 22, quality: "high", color: "green" },
  ];
  const rows = sessions.map((s) =>
    simpleRow(
      s.name + (s.active ? " ●" : ""),
      (s.active ? "active" : "closed") + " / " + s.quality,
      s.active ? "trading" : "waiting",
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
}
