function renderFunnel() {
  const f = App.state.funnel || {};
  const totals = f.cycle_totals || {};
  const decisions = f.decisions || {};
  setChildren("funnel-kpis", [
    kpi("Cycles", number(totals.cycles || 0), "tail window", "blue"),
    kpi("Detector runs", number(totals.detector_runs || 0), "cycles tail", "cyan"),
    kpi("Candidates", number(totals.candidates || 0), "post-filter", "yellow"),
    kpi("Selected", number(totals.selected || 0), "delivery queue", "orange"),
    kpi("Delivered", number(totals.delivered || 0), "notifier accepted", "green"),
    kpi("Raw signal rate", pct(decisions.signal_rate || 0), "strategy decisions", clsByValue(decisions.signal_rate)),
  ]);
  const statusRows = Object.entries(decisions.status_counts || {}).map(([key, count]) => ({ key, count }));
  setChildren("decision-status", barList(statusRows, { fillClass: "green" }));
  setChildren("rejection-stages", barList(App.state.rejections?.stages || [], { fillClass: "red" }));
  const columns = [
    { label: "shortlist", get: (r) => r.shortlist_size },
    { label: "source", get: (r) => r.shortlist_source },
    { label: "detectors", get: (r) => r.detector_runs },
    { label: "candidates", get: (r) => r.candidate_count },
    { label: "selected", get: (r) => r.selected_count || r.selected_signals || 0 },
    { label: "delivered", get: (r) => r.delivered_count || 0 },
  ];
  setChildren("cycle-table", [table(columns, f.latest_cycles || [])]);
}
