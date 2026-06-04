"use strict";

function renderTodayPanel() {
  const summary = App.state.summary || {};
  const today = summary.today || {};
  const hint = summary.funnel_hint || today.funnel_hint || {};

  setChildren("today-kpis", [
    kpi("Отправлено", today.session_delivered ?? "—", "за сессию", "green"),
    kpi("В работе", today.active ?? 0, "active", today.active ? "green" : "muted"),
    kpi("Ждут входа", today.pending ?? 0, "pending", today.pending ? "yellow" : "muted"),
    kpi("TP1", today.tp1_hit ?? 0, "всего за DB", "cyan"),
    kpi("Стопы", today.stop_loss ?? 0, "всего за DB", "red"),
  ]);

  const hintEl = document.getElementById("funnel-hint-text");
  if (hintEl) {
    hintEl.textContent = hint.text || "Загрузка контекста…";
  }

  _renderHistory(summary.history || []);
  _renderSlHint();
}

function _renderSlHint() {
  const el = document.getElementById("today-sl-hint");
  if (!el) return;
  fetch("/api/v1/outcomes/analytics?days=7", { cache: "no-store" })
    .then((res) => (res.ok ? res.json() : null))
    .then((data) => {
      if (!data || data.error) {
        el.textContent = "";
        return;
      }
      const causes = data.sl_root_causes || {};
      const labels = data.sl_root_cause_labels || {};
      const top = Object.entries(causes).sort((a, b) => b[1] - a[1])[0];
      if (!top) {
        el.textContent = "SL root-cause: данных пока нет.";
        return;
      }
      const label = labels[top[0]] || top[0];
      el.textContent = `Топ причина SL (7d): ${label} (${top[1]}) · /sl в Telegram`;
    })
    .catch(() => {
      el.textContent = "";
    });
}

function _renderHistory(rows) {