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
  _renderConfluenceLegs();
  _renderConfluenceLegsByProfile();
  const columns = [
    { label: "shortlist", get: (r) => r.shortlist_size },
    { label: "source", get: (r) => r.shortlist_source },
    { label: "detectors", get: (r) => r.detector_runs },
    { label: "candidates", get: (r) => r.candidate_count },
    { label: "selected", get: (r) => r.selected_count || r.selected_signals || 0 },
    { label: "delivered", get: (r) => r.delivery_success_count ?? r.delivered_count ?? 0 },
  ];
  setChildren("cycle-table", [table(columns, f.latest_cycles || [])]);
}

function _renderConfluenceLegs() {
  const legs = App.state.confluenceLegs?.leg_failures || [];
  const legLabels = App.state.labelMaps?.confluence_legs || {};
  const rows = legs.map((row) => ({
    key: row.label_ru || legLabels[row.key] || row.key,
    count: row.count,
  }));
  setChildren("confluence-legs", barList(rows, { fillClass: "orange" }));
}

function _renderConfluenceLegsByProfile() {
  const payload = App.state.confluenceLegsByProfile || {};
  const profiles = payload.profiles || [];
  const legLabels = App.state.labelMaps?.confluence_legs || {};
  const profileLabels = App.state.labelMaps?.confirmation_profiles || {};
  const legKeys = (App.state.confluenceLegs?.leg_failures || []).map((row) => row.key);
  const columns = [
    {
      label: "profile",
      get: (row) => row.label_ru || profileLabels[row.key] || row.key,
    },
    { label: "gate", get: (row) => number(row.gate_rejects || 0, 0) },
    ...legKeys.map((leg) => ({
      label: legLabels[leg] || leg,
      get: (row) => {
        const match = (row.leg_failures || []).find((item) => item.key === leg);
        return number(match?.count || 0, 0);
      },
    })),
    { label: "Σ legs", get: (row) => number(row.total_leg_failures || 0, 0) },
    { label: "recommendation", get: (row) => row.recommendation || "—" },
  ];
  setChildren("confluence-legs-by-profile", [table(columns, profiles)]);
}
