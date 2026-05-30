function renderAudit() {
  const audit = App.state.audit || {};
  const summary = audit.summary || {};
  const bySeverity = summary.by_severity || {};
  setChildren("audit-kpis", [
    kpi("Health", (audit.score ?? 0) + "/100", audit.status || "unknown",
      Number(audit.score || 0) >= 80 ? "green" : Number(audit.score || 0) >= 50 ? "yellow" : "red"),
    kpi("Critical", bySeverity.critical || 0, "must inspect", Number(bySeverity.critical || 0) ? "red" : "green"),
    kpi("Warnings", bySeverity.warning || 0, "degraded areas", Number(bySeverity.warning || 0) ? "yellow" : "green"),
    kpi("Info", bySeverity.info || 0, "context", "blue"),
    kpi("Findings", summary.total || 0, "audit rows", "cyan"),
    kpi("Generated", audit.generated_at ? new Date(audit.generated_at).toLocaleTimeString() : "-", "server time", "orange"),
  ]);
  document.getElementById("audit-status").textContent = audit.status || "unknown";
  document.getElementById("audit-brief").textContent = audit.operator_brief || "No audit report available.";
  setChildren("audit-action-plan", rowsOrEmpty(
    (audit.action_plan || []).map((item, index) => ({ item, index: index + 1 })),
    (row) => simpleRow("Action " + row.index, row.item, "", "cyan"),
    "No action plan"
  ));
  setChildren("audit-findings", rowsOrEmpty(audit.findings || [], (finding) => {
    const severity = text(finding.severity).toLowerCase();
    return el("div", { class: "finding-card " + severity }, [
      el("div", { class: "finding-head" }, [
        el("div", { class: "finding-title", text: finding.title || finding.code || "-" }),
        el("div", { class: "finding-meta", text: text(finding.severity) + " / " + text(finding.area) }),
      ]),
      el("div", { class: "finding-detail", text: finding.detail || "-" }),
      el("div", { class: "finding-recommendation", text: finding.recommendation || "No recommendation" }),
    ]);
  }, "No audit findings"));
}
