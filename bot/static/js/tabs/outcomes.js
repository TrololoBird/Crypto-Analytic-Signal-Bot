"use strict";

async function fetchOutcomesInsights() {
  try {
    const res = await fetch("/api/v1/analytics/outcomes?days=30", { cache: "no-store" });
    if (!res.ok) return null;
    return await res.json();
  } catch (_err) {
    return null;
  }
}

function renderOutcomes() {
  const data = App.state.outcomes;
  if (!data || data.error) {
    setChildren("outcomes-kpis", [el("div", { class: "empty", text: "Нет данных outcomes" })]);
    return;
  }

  const summary = data.summary || {};
  const cmp = data.comparisons || {};
  const quality = data.data_quality || {};

  const exportBtn = el("button", {
    class: "btn btn-sm",
    text: "CSV",
    title: "Скачать последние стоп-лоссы в CSV",
    onclick: () => { window.open("/api/v1/analytics/export?days=30", "_blank"); },
  });

  fetch("/api/v1/analytics/operator-kpi?days=7", { cache: "no-store" })
    .then((res) => (res.ok ? res.json() : null))
    .then((kpi) => {
      if (!kpi || kpi.error) {
        setChildren("outcomes-operator-kpi", [
          el("div", { class: "empty", text: "Operator KPI недоступен" }),
        ]);
        return;
      }
      const m = kpi.metrics || {};
      const slReg = m.sl_rate_by_regime_4h || {};
      setChildren("outcomes-operator-kpi", [
        kpi("SL bear", pct(slReg.bear || 0), `bull ${pct(slReg.bull || 0)}`, "red"),
        kpi("Expired%", pct(m.expired_rate || 0), `${m.expired_count || 0} plans`, "yellow"),
        kpi("MFE=0 SL", m.zero_mfe_before_sl || 0, pct(m.zero_mfe_share_of_sl || 0), "red"),
        kpi(
          "Exit min",
          number(m.time_to_exit_min_median || 0, 0),
          `p90 ${number(m.time_to_exit_min_p90 || 0, 0)}`,
          "muted"
        ),
        kpi("Score R²", number(m.score_vs_outcome_r_squared || 0, 3), "score vs win", "green"),
      ]);
    })
    .catch(() => {
      setChildren("outcomes-operator-kpi", [
        el("div", { class: "empty", text: "Operator KPI error" }),
      ]);
    });

  setChildren("outcomes-kpis", [exportBtn,
    kpi("Win rate", pct(summary.win_rate || 0), `${summary.wins || 0}W / ${summary.stop_losses || 0}SL`, clsByValue(summary.win_rate)),
    kpi("Avg R", number(summary.avg_r_multiple || 0, 2), "закрытые сделки", clsByValue(summary.avg_r_multiple)),
    kpi("Score SL", number(cmp.avg_score_stop_loss || 0, 3), `wins ${number(cmp.avg_score_wins || 0, 3)}`, "red"),
    kpi("ATR% SL", number(cmp.avg_atr_pct_stop_loss || 0, 2), `wins ${number(cmp.avg_atr_pct_wins || 0, 2)}`, "orange"),
    kpi("MFE=0", cmp.zero_mfe_stop_losses || 0, "стоп без профита", "red"),
    kpi("Post-SL room", cmp.post_sl_thesis_room || 0, "до TP1 после стопа", "orange"),
    kpi("Sample", quality.trade_outcomes || 0, quality.sufficient_for_analysis ? "достаточно" : "мало данных", quality.sufficient_for_analysis ? "green" : "yellow"),
  ]);

  const patterns = (data.patterns || []).map((p) =>
    simpleRow(p.label || p.key, `${number((p.share || 0) * 100, 0)}%`, p.count || 0, "red")
  );
  setChildren(
    "outcomes-patterns",
    patterns.length ? patterns : [el("div", { class: "empty", text: "Паттерны появятся после 5+ закрытых сделок" })]
  );

  const matrix = (data.by_setup || []).filter((row) => row.total > 0);
  setChildren(
    "outcomes-setup-matrix",
    table(
      [
        { label: "Стратегия", get: (r) => r.setup_id },
        { label: "SL", get: (r) => r.stop_loss, class: "red" },
        { label: "Win", get: (r) => r.wins, class: "green" },
        { label: "WR", get: (r) => pct(r.win_rate || 0) },
      ],
      matrix
    )
  );

  const recs = (data.recommendations || []).map((text, idx) =>
    el("div", { class: "row" }, [
      el("div", { class: "row-main" }, [
        el("div", { class: "row-title", text: `${idx + 1}. ${text}` }),
      ]),
    ])
  );
  setChildren(
    "outcomes-recommendations",
    recs.length ? recs : [el("div", { class: "empty", text: "Рекомендаций пока нет" })]
  );

  const causeLabels = data.sl_root_cause_labels || {};
  const causes = Object.entries(data.sl_root_causes || {}).map(([key, count]) =>
    simpleRow(causeLabels[key] || key, String(count), count, "red")
  );
  setChildren(
    "outcomes-sl-causes",
    causes.length ? causes : [el("div", { class: "empty", text: "Причины появятся после SL в outcomes" })]
  );

  fetch("/api/v1/mobile/summary", { cache: "no-store" })
    .then((res) => (res.ok ? res.json() : null))
    .then((mobile) => {
      if (!mobile || mobile.error) {
        setChildren("outcomes-mobile", [el("div", { class: "empty", text: "Mobile summary недоступен" })]);
        return;
      }
      const urls = mobile.dashboard_urls || {};
      const remote = urls.remote_access || {};
      const cmdPreview = (remote.commands || []).slice(0, 4).join(" ");
      const rows = [
        simpleRow(
          "Remote (Telegram)",
          remote.mode === "telegram_operator"
            ? `DM боту: ${cmdPreview} … /help`
            : "Добавьте TELEGRAM_OPERATOR_USER_IDS в .env",
          "",
          "green"
        ),
        simpleRow("Local Mac", urls.local || "—", "", "muted"),
        simpleRow("Hint", urls.mobile_hint || mobile.rejections_hint || "", "", "yellow"),
      ];
      setChildren("outcomes-mobile", rows);
    })
    .catch(() => {
      setChildren("outcomes-mobile", [el("div", { class: "empty", text: "Mobile summary error" })]);
    });

  const recent = data.recent_stop_losses || [];
  setChildren(
    "outcomes-recent-sl",
    table(
      [
        { label: "Symbol", get: (r) => r.symbol },
        { label: "Setup", get: (r) => r.setup_id },
        { label: "Dir", get: (r) => r.direction },
        { label: "PnL%", get: (r) => number(r.pnl_pct || 0, 2), class: "red" },
        { label: "R", get: (r) => number(r.pnl_r_multiple || 0, 2) },
        { label: "MAE", get: (r) => number(r.mae || 0, 2) },
        { label: "MFE", get: (r) => number(r.mfe || 0, 2) },
        { label: "Причина", get: (r) => r.sl_root_cause_label || r.sl_root_cause || "—" },
        { label: "Post-SL%", get: (r) => number(r.post_sl_favorable_pct || 0, 2) },
      ],
      recent
    )
  );
}
