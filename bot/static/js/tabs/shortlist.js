function renderShortlist() {
  const s = App.state.shortlist || {};
  setChildren("shortlist-kpis", [
    kpi("Total", s.total || 0, "current memory", "blue"),
    kpi("Dynamic", s.dynamic || 0, "non-pinned", Number(s.dynamic || 0) >= 30 ? "green" : "yellow"),
    kpi("Pinned", s.pinned || 0, "protected symbols", "cyan"),
    kpi("Priority", (s.priority_in_telemetry || 0) + "/" + (s.priority_total || 0),
      (s.priority_missing || []).join(", ") || "covered", Number(s.priority_missing?.length || 0) ? "red" : "green"),
    kpi("Avg fits", number(s.avg_fit || 0, 1), "setups per symbol", "green"),
    kpi("Source", s.source || "-", "refresh path", "orange"),
  ]);
  document.getElementById("shortlist-source").textContent = "source " + text(s.source);
  const priorityColumns = [
    { label: "symbol", get: (r) => r.symbol, class: "mono" },
    { label: "shortlist", get: (r) => r.in_latest_telemetry ? "yes" : (r.in_memory ? "memory" : "missing") },
    { label: "score", get: (r) => r.score ? Number(r.score).toFixed(4) : "-" },
    { label: "fits", get: (r) => r.strategy_fit_count || 0 },
    { label: "decisions", get: (r) => r.decision_rows || 0 },
    { label: "candidates", get: (r) => r.candidate_rows || 0 },
    { label: "selected", get: (r) => r.selected_rows || 0 },
    { label: "top reject", get: (r) => r.top_rejection ? (r.top_rejection.reason + " (" + r.top_rejection.count + ")") : "-" },
  ];
  setChildren("priority-assets-table", [table(priorityColumns, s.priority_assets || [])]);
  const columns = [
    { label: "symbol", get: (r) => r.symbol, class: "mono" },
    { label: "bucket", get: (r) => r.bucket },
    { label: "source", get: (r) => r.source },
    { label: "score", get: (r) => r.score ? Number(r.score).toFixed(4) : "-" },
    { label: "24h %", get: (r) => r.price_change_pct ? Number(r.price_change_pct).toFixed(2) : "-" },
    { label: "priority", get: (r) => r.priority ? "yes" : "-" },
    { label: "fits", get: (r) => r.strategy_fit_count },
    { label: "sample fits", get: (r) => (r.strategy_fits || []).join(", ") },
  ];
  setChildren("shortlist-table", [table(columns, s.items || [])]);
}
