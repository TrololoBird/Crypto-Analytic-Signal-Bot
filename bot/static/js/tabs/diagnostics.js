"use strict";

function renderDiagnosticsSubTab(name) {
  App.state.diagnosticsSubTab = name;
  document.querySelectorAll(".diag-tab").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.diag === name);
  });
  document.querySelectorAll(".diag-section").forEach((sec) => {
    sec.classList.toggle("active", sec.id === "diag-" + name);
  });
  if (name === "overview" && typeof renderOverview === "function") renderOverview();
  if (name === "funnel" && typeof renderFunnel === "function") renderFunnel();
  if (name === "audit" && typeof renderAudit === "function") renderAudit();
  if (name === "shortlist" && typeof renderShortlist === "function") renderShortlist();
  if (name === "strategies" && typeof renderStrategies === "function") renderStrategies();
  if (name === "outcomes") {
    fetchOutcomesInsights().then((data) => {
      App.state.outcomes = data;
      if (typeof renderOutcomes === "function") renderOutcomes();
    });
  }
  if (name === "delivery" && typeof renderDelivery === "function") renderDelivery();
  if (name === "runtime" && typeof renderRuntime === "function") renderRuntime();
}

function renderDiagnostics() {
  renderDiagnosticsSubTab(App.state.diagnosticsSubTab || "overview");
}

function bindDiagnosticsTabs() {
  document.querySelectorAll(".diag-tab").forEach((btn) => {
    btn.addEventListener("click", () => renderDiagnosticsSubTab(btn.dataset.diag));
  });
}
