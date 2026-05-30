function renderDelivery() {
  const t = App.state.telegram || {};
  const preview = t.preview || {};
  setChildren("telegram-preview-meta", [
    simpleRow("Available", t.available ? "preview source exists" : (t.reason || "missing"),
      t.available ? "yes" : "no", t.available ? "green" : "yellow"),
    simpleRow("HTML length", (preview.errors || []).join("; ") || "valid", preview.chars || 0, preview.ok ? "green" : "red"),
    simpleRow("Parse mode", "Telegram Bot API", preview.parse_mode || "HTML", "cyan"),
  ]);
  document.getElementById("telegram-preview").textContent = preview.plain_preview || "No signal-like telemetry row available for preview.";
  const rows = App.state.delivery?.rows || [];
  setChildren("delivery-list", rowsOrEmpty(rows, (row) =>
    simpleRow(
      text(row.symbol) + " " + text(row.setup_id) + " " + text(row.direction),
      text(row.delivery_reason || row.source || row.ts),
      text(row.delivery_status || row.status || "selected"),
      row.delivery_status === "sent" ? "green" : "yellow"
    ), "No selected/delivery telemetry in this run"));
}
