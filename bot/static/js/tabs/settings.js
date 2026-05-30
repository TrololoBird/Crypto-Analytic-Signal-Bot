let settingsState = { strategies: [], scoring: null, killzone: null };

function renderSettings() {
  _fetchSettings();
}

function _fetchSettings() {
  Promise.all([
    fetch("/api/v1/config/strategies").then(r => r.ok ? r.json() : []),
    fetch("/api/v1/config/scoring").then(r => r.ok ? r.json() : null),
    fetch("/api/v1/config/killzone").then(r => r.ok ? r.json() : null),
  ])
    .then(([strategies, scoring, killzone]) => {
      settingsState = { strategies, scoring, killzone };
      _renderStrategyToggles(strategies);
      _renderScoringSliders(scoring);
      _renderKillzone(killzone);
    })
    .catch(err => console.warn("settings fetch error", err));
}

function _renderStrategyToggles(strategies) {
  const container = document.getElementById("settings-strategies");
  if (!container) return;
  const items = strategies.map(s => {
    const card = el("div", { class: "signal-card", style: "padding:10px" }, [
      el("div", { style: "display:flex;justify-content:space-between;align-items:center" }, [
        el("span", { class: "mono", style: "font-weight:700", text: s.name || s.id || "-" }),
        el("span", { class: "badge", style: s.enabled ? "border-color:var(--green);color:var(--green)" : "", text: s.enabled ? "enabled" : "disabled" }),
      ]),
      el("div", { style: "margin-top:4px;font-size:12px;color:var(--soft)", text: "family: " + (s.family || "generic") + " | status: " + (s.status || "beta") }),
    ]);
    card.style.cursor = "pointer";
    card.onclick = () => _toggleStrategy(s.id, !s.enabled);
    return card;
  });
  container.replaceChildren(...items);
}

async function _toggleStrategy(id, enabled) {
  try {
    const res = await fetch("/api/v1/config/strategies", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ updates: { [id]: { enabled } } }),
    });
    if (res.ok) _fetchSettings();
  } catch (err) {
    console.error("toggle strategy error", err);
  }
}

function _renderScoringSliders(scoring) {
  const container = document.getElementById("settings-scoring");
  if (!container || !scoring?.weights) return;
  const weights = scoring.weights;
  const entries = Object.entries(weights);
  const items = entries.map(([key, val]) => {
    const pct = Math.round(Number(val) * 100);
    return el("div", { class: "bar-row" }, [
      el("div", { class: "bar-label", text: key.replace(/_/g, " ") }),
      el("div", {}, [
        el("input", {
          type: "range", min: 0, max: 50, value: pct,
          style: "width:100%",
          oninput: `document.getElementById('slider-${key}').textContent=this.value+'%'`,
        }),
        el("span", { id: "slider-" + key, class: "mono muted", text: pct + "%" }),
      ]),
    ]);
  });
  container.replaceChildren(...items);
  container.appendChild(
    el("button", { class: "tab", text: "Apply Weights", style: "margin-top:10px;width:100%", onclick: "_onApplyScoring()" })
  );
}

function _onApplyScoring() {
  const sliders = document.querySelectorAll("#settings-scoring input[type=range]");
  const labels = ["mtf_alignment", "volume_quality", "structure_clarity", "risk_reward", "crowd_position", "oi_momentum"];
  const weights = {};
  sliders.forEach((s, i) => {
    if (i < labels.length) weights[labels[i]] = parseInt(s.value) / 100;
  });
  fetch("/api/v1/config/scoring", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ weights }),
  }).then(r => r.json()).then(res => {
    document.getElementById("scoring-status").textContent = "Applied: " + JSON.stringify(res);
  }).catch(err => {
    document.getElementById("scoring-status").textContent = "Error: " + err;
  });
}

function _renderKillzone(kz) {
  const container = document.getElementById("settings-killzone");
  if (!container || !kz) return;
  const items = Object.entries(kz).map(([name, cfg]) =>
    el("div", { class: "row" }, [
      el("div", { class: "row-title", text: name.charAt(0).toUpperCase() + name.slice(1) }),
      el("div", { class: "row-mono", text: cfg.start + " - " + cfg.end + " UTC" }),
    ])
  );
  container.replaceChildren(...items);
}
