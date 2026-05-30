let alertsInterval = null;

function renderAlerts() {
  _fetchAlerts();
  if (alertsInterval) clearInterval(alertsInterval);
  alertsInterval = setInterval(_fetchAlerts, 15000);
}

function _fetchAlerts() {
  fetch("/api/v1/alerts?limit=100", { cache: "no-store" })
    .then(r => r.ok ? r.json() : [])
    .then(data => {
      const alerts = Array.isArray(data) ? data : [];
      _renderAlertList(alerts);
      _renderAlertKPIs(alerts);
    })
    .catch(err => console.warn("alerts fetch error", err));
}

function _renderAlertKPIs(alerts) {
  const critical = alerts.filter(a => (a.severity || "info") === "critical").length;
  const warning = alerts.filter(a => (a.severity || "info") === "warning").length;
  const info = alerts.filter(a => (a.severity || "info") === "info").length;
  setChildren("alerts-kpis", [
    kpi("Total", alerts.length, "alert history", "blue"),
    kpi("Critical", critical, "must inspect", critical ? "red" : "green"),
    kpi("Warnings", warning, "needs review", warning ? "yellow" : "green"),
    kpi("Info", info, "context", "cyan"),
  ]);
}

function _renderAlertList(alerts) {
  const container = document.getElementById("alerts-list");
  if (!container) return;
  const items = alerts.slice(0, 100).map(a => {
    const sev = (a.severity || "info").toLowerCase();
    return el("div", { class: "finding-card " + (sev === "critical" ? "critical" : sev === "warning" ? "warning" : "info") }, [
      el("div", { class: "finding-head" }, [
        el("div", { class: "finding-title", text: a.title || a.reason || a.type || "-" }),
        el("div", { class: "finding-meta", text: sev + " | " + (a.ts || a.timestamp || "").slice(0, 19) }),
      ]),
      el("div", { class: "finding-detail", text: a.detail || a.message || "-" }),
    ]);
  });
  container.replaceChildren(
    items.length ? ...items : el("div", { class: "empty", text: "No alerts in current run" })
  );
}
