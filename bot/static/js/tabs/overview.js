function renderOverview() {
  const o = App.state.overview || {};
  const a = App.state.analytics?.summary || {};
  const deliveryCount =
    App.state.delivery?.delivery_success_count ??
    App.state.delivery?.delivery_count ??
    o.session_delivered ??
    0;
  const topBlocker = o.top_blocker || o.top_rejection || {};
  setChildren("overview-kpis", [
    kpi("Shortlist", o.shortlist_size, o.shortlist_source, "blue"),
    kpi("Режим", _regimeLabel(o.market_regime), o.btc_phase || "фаза BTC", _regimeClass(o.market_regime)),
    kpi("BTC bias", o.btc_bias || "-", "market context", "cyan"),
    kpi("ETH bias", o.eth_bias || "-", "alt context", "cyan"),
    kpi("Detector rows", number(o.decision_rows || 0), "strategy decisions", "cyan"),
    kpi("Raw signal rate", pct(o.decision_signal_rate || 0), "pre-filter detector surface", clsByValue(o.decision_signal_rate)),
    kpi("Candidates", (o.cycle_totals || {}).candidates || o.last_cycle_candidates || 0, "session total", "yellow"),
    kpi("Delivered", deliveryCount, "delivery.jsonl", "green"),
    kpi("Avg R", number(a.avg_r_multiple || a.avg_rr || 0, 2), "30d outcomes", clsByValue(a.avg_r_multiple || a.avg_rr)),
    kpi("Win rate", pct(App.state.outcomes?.summary?.win_rate || 0), "SL анализ → вкладка Outcomes", clsByValue(App.state.outcomes?.summary?.win_rate)),
    kpi("Avg MAE", pct(a.avg_mae || 0), "adverse excursion", "red"),
    kpi("Avg MFE", pct(a.avg_mfe || 0), "favorable excursion", "green"),
    kpi("Top blocker", topBlocker.label_ru || topBlocker.key || "-", (topBlocker.count || 0) + " rows", "orange"),
  ]);
  _renderOverviewFunnelWidget(o.funnel_widget || App.state.funnel?.funnel_widget);
  const cycles = (App.state.funnel?.latest_cycles || []).slice(0, 10);
  setChildren("cycle-list", rowsOrEmpty(cycles, (row) =>
    simpleRow(
      "shortlist " + text(row.shortlist_size) + " / detectors " + text(row.detector_runs),
      "source " + text(row.shortlist_source) + " / candidates " + text(row.candidate_count),
      text(row.delivery_success_count ?? row.delivered_count ?? 0),
      Number(row.delivery_success_count ?? row.delivered_count ?? 0) ? "green" : "muted"
    ), "No cycle telemetry"));
  setChildren("overview-rejections", barList(
    (App.state.rejections?.reasons || []).map((row) => ({
      key: row.label_ru || row.key,
      count: row.count,
    })),
    { fillClass: "red" }
  ));
}

function _renderOverviewFunnelWidget(widget) {
  const container = document.getElementById("overview-funnel-widget");
  if (!container) return;
  const stages = widget?.stages || [];
  if (!stages.length) {
    container.replaceChildren();
    return;
  }
  const nodes = [];
  stages.forEach((stage, index) => {
    if (index > 0) {
      nodes.push(el("div", { class: "funnel-arrow", text: "→" }));
    }
    const delta = Number(stage.session_delta || 0);
    nodes.push(
      el("div", { class: "funnel-stage" }, [
        el("div", { class: "funnel-stage-label", text: stage.label_ru || stage.key || "—" }),
        el("div", { class: "funnel-stage-value", text: number(stage.count || 0, 0) }),
        el("div", {
          class: "funnel-stage-delta",
          text: delta ? "+" + number(delta, 0) + " last cycle" : "— last cycle",
        }),
      ])
    );
  });
  container.replaceChildren(...nodes);
}

function _regimeLabel(regime) {
  const map = { bull: "bull", bear: "bear", ranging: "range", volatile: "vol", unknown: "—" };
  return map[String(regime || "unknown").toLowerCase()] || regime || "—";
}

function _regimeClass(regime) {
  const r = String(regime || "").toLowerCase();
  if (r === "bull") return "green";
  if (r === "bear") return "red";
  return "muted";
}
