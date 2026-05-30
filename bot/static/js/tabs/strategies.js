function renderStrategies() {
  const d = App.state.decisions || {};
  const zero = d.zero_signal_setups || [];
  setChildren("strategy-kpis", [
    kpi("Rows", number(d.total_rows || 0), "decision telemetry", "cyan"),
    kpi("Signals", number((d.status_counts || {}).signal || 0), "raw hits", "green"),
    kpi("Rejects", number((d.status_counts || {}).reject || 0), "detector rejects", "red"),
    kpi("Signal rate", pct(d.signal_rate || 0), "raw detector surface", clsByValue(d.signal_rate)),
    kpi("Zero-hit setups", zero.length, "needs review", zero.length ? "yellow" : "green"),
    kpi("Families", (d.reason_family_counts || []).length, "reason groups", "blue"),
  ]);
  const columns = [
    { label: "setup", get: (r) => r.setup_id },
    { label: "rows", get: (r) => r.total },
    { label: "signals", get: (r) => r.signals },
    { label: "rate", get: (r) => pct(r.signal_rate || 0) },
    { label: "top blocker", get: (r) => (r.top_blockers && r.top_blockers[0] && r.top_blockers[0].key) || "-" },
  ];
  setChildren("strategy-table", [table(columns, d.setup_reports || [])]);
  setChildren("zero-signal-list", rowsOrEmpty(zero, (row) =>
    simpleRow(row.setup_id, ((row.top_blockers || [])[0] || {}).key || "no blocker", row.total, "yellow"),
    "No zero-signal setups in current decision telemetry"));
}
