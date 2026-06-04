function renderRuntime() {
  const r = App.state.runtime || {};
  document.getElementById("ws-json").textContent = JSON.stringify(r.ws_snapshot || {}, null, 2);
  document.getElementById("quality-json").textContent = JSON.stringify({
    data_quality: r.latest_data_quality || {},
    fallback: r.latest_fallback || {},
    public_intelligence: r.latest_public_intelligence || {},
  }, null, 2);
  document.getElementById("diagnostics-json").textContent = JSON.stringify(r.signal_diagnostics || {}, null, 2);
  _renderTelemetryMismatchPanel(r.telemetry_mismatch || {});
}

function _renderTelemetryMismatchPanel(payload) {
  const panel = document.getElementById("telemetry-mismatch-panel");
  const countsEl = document.getElementById("telemetry-mismatch-counts");
  if (!panel || !countsEl) return;
  if (!payload.available) {
    panel.style.display = "none";
    countsEl.replaceChildren();
    return;
  }
  panel.style.display = "block";
  const rows = (payload.counts || []).map((row) => ({
    key: row.key || "unknown",
    count: row.count || 0,
  }));
  if (payload.total_rows != null) {
    rows.unshift({ key: "total rows", count: payload.total_rows });
  }
  setChildren("telemetry-mismatch-counts", barList(rows, { fillClass: "orange" }));
}
