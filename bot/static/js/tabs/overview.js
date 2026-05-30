function renderOverview() {
  const o = App.state.overview || {};
  const topReject = o.top_rejection || {};
  setChildren("overview-kpis", [
    kpi("Shortlist", o.shortlist_size, o.shortlist_source, "blue"),
    kpi("BTC bias", o.btc_bias || "-", "market context", "cyan"),
    kpi("Detector rows", number(o.decision_rows || 0), "strategy decisions", "cyan"),
    kpi("Raw signal rate", pct(o.decision_signal_rate || 0), "pre-filter detector surface", clsByValue(o.decision_signal_rate)),
    kpi("Candidates", o.last_cycle_candidates || 0, "last cycle", "yellow"),
    kpi("Delivered", o.last_cycle_delivered || 0, "last cycle", "green"),
    kpi("Top reject", topReject.key || "-", (topReject.count || 0) + " rows", "orange"),
  ]);
  const cycles = (App.state.funnel?.latest_cycles || []).slice(0, 10);
  setChildren("cycle-list", rowsOrEmpty(cycles, (row) =>
    simpleRow(
      "shortlist " + text(row.shortlist_size) + " / detectors " + text(row.detector_runs),
      "source " + text(row.shortlist_source) + " / candidates " + text(row.candidate_count),
      text(row.delivered_count || 0),
      Number(row.delivered_count || 0) ? "green" : "muted"
    ), "No cycle telemetry"));
  setChildren("overview-rejections", barList(App.state.rejections?.reasons || [], { fillClass: "red" }));
}
