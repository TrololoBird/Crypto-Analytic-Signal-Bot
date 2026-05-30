let confluenceInterval = null;

const SIM_LABELS = [
  ["mtf_alignment", "MTF Alignment", 25],
  ["volume_quality", "Volume Quality", 20],
  ["structure_clarity", "Structure Clarity", 20],
  ["risk_reward", "Risk/Reward", 15],
  ["crowd_position", "Crowd Position", 10],
  ["oi_momentum", "OI Momentum", 10],
];

function renderConfluenceLab() {
  _initSimSliders();
  _fetchConfluenceData();
  if (confluenceInterval) clearInterval(confluenceInterval);
  confluenceInterval = setInterval(_fetchConfluenceData, 20000);
}

function _initSimSliders() {
  const container = document.getElementById("sim-sliders");
  if (!container || container.children.length) return;
  const items = SIM_LABELS.map(([key, label, defaultVal]) =>
    el("div", { class: "bar-row" }, [
      el("div", { class: "bar-label", text: label }),
      el("div", {}, [
        el("input", { type: "range", id: "sim-" + key.split("_")[0], min: 0, max: 50, value: defaultVal, style: "width:100%" }),
        el("span", { class: "mono muted", text: defaultVal + "%" }),
      ]),
    ])
  );
  container.replaceChildren(...items);
}

function _fetchConfluenceData() {
  Promise.all([
    fetch("/api/v1/strategies/health").then(r => r.ok ? r.json() : []),
    fetch("/api/v1/confluence/vetos?limit=30").then(r => r.ok ? r.json() : []),
    fetch("/api/v1/confluence/distribution?hours=24").then(r => r.ok ? r.json() : null),
  ])
    .then(([strategies, vetos, distribution]) => {
      _renderStrategyCards(strategies);
      _renderVetos(vetos);
    })
    .catch(err => console.warn("confluence fetch error", err));
}

function _renderStrategyCards(strategies) {
  const container = document.getElementById("confluence-strategies");
  if (!container) return;
  const cards = strategies.slice(0, 41).map(s => {
    const enabled = s.enabled !== false;
    const color = enabled ? "var(--green)" : "var(--soft)";
    return el("div", { class: "kpi-card", style: "min-height:70px" }, [
      el("div", { style: "display:flex;justify-content:space-between;align-items:center" }, [
        el("span", { class: "mono", style: "font-size:13px;font-weight:700", text: s.name || s.id }),
        el("span", { class: "badge", style: enabled ? "border-color:" + color : "", text: enabled ? "ON" : "OFF" }),
      ]),
      el("div", { style: "margin-top:6px;display:flex;gap:10px;font-size:12px;color:var(--soft)" }, [
        el("span", { text: "family: " + (s.family || "generic") }),
        el("span", { text: "status: " + (s.status || "beta") }),
        el("span", { text: "risk: " + (s.risk_profile || "-") }),
      ]),
    ]);
  });
  container.replaceChildren(...cards);
}

function _renderVetos(vetos) {
  const container = document.getElementById("confluence-vetos");
  if (!container) return;
  const items = Array.isArray(vetos) ? vetos : (vetos?.reasons || []);
  const rows = items.slice(0, 20).map(v =>
    simpleRow(
      v.key || v.reason || "-",
      (v.count || 0) + " occurrences",
      (v.pct || 0).toFixed(1) + "%",
      "yellow"
    )
  );
  setChildren("confluence-vetos", rowsOrEmpty(rows, r => r, "No veto entries"));
}

function _onConfluenceSimulate() {
  const weights = {
    mtf_alignment: parseFloat(document.getElementById("sim-mtf")?.value || 25) / 100,
    volume_quality: parseFloat(document.getElementById("sim-vol")?.value || 20) / 100,
    structure_clarity: parseFloat(document.getElementById("sim-struct")?.value || 20) / 100,
    risk_reward: parseFloat(document.getElementById("sim-rr")?.value || 15) / 100,
    crowd_position: parseFloat(document.getElementById("sim-crowd")?.value || 10) / 100,
    oi_momentum: parseFloat(document.getElementById("sim-oi")?.value || 10) / 100,
  };
  const total = Object.values(weights).reduce((a, b) => a + b, 0);
  const status = document.getElementById("sim-status");
  if (Math.abs(total - 1.0) > 0.01) {
    status.textContent = "Weights must sum to 1.0 (currently " + total.toFixed(2) + ")";
    status.style.color = "var(--red)";
    return;
  }
  status.textContent = "Simulating with " + JSON.stringify(weights) + "...";
  status.style.color = "var(--green)";
  fetch("/api/v1/confluence/simulate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ weights, disabled_setups: [] }),
  }).then(r => r.json()).then(res => {
    document.getElementById("sim-result").textContent = JSON.stringify(res, null, 2);
    status.textContent = "Simulation complete";
  }).catch(err => {
    status.textContent = "Error: " + err;
    status.style.color = "var(--red)";
  });
}
