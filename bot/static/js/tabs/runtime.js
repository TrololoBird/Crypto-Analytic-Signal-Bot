function renderRuntime() {
  const r = App.state.runtime || {};
  document.getElementById("ws-json").textContent = JSON.stringify(r.ws_snapshot || {}, null, 2);
  document.getElementById("quality-json").textContent = JSON.stringify({
    data_quality: r.latest_data_quality || {},
    fallback: r.latest_fallback || {},
    public_intelligence: r.latest_public_intelligence || {},
  }, null, 2);
  document.getElementById("diagnostics-json").textContent = JSON.stringify(r.signal_diagnostics || {}, null, 2);
}
