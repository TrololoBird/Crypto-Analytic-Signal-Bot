function renderEngineRoom() {
  _renderEngineSparklines();
}

function _renderEngineSparklines() {
  const wsData = App.state.runtime?.ws_snapshot || {};
  const health = App.state.runtime?.latest_runtime || App.state.runtime?.latest_health || {};
  const metrics = [
    { label: "WS Latency", value: wsData.avg_latency_ms ?? health.ws_latency_ms, unit: "ms", warn: 500, canvasId: "eng-ws-latency" },
    { label: "Cycle Duration", value: health.last_cycle_duration_ms, unit: "ms", warn: 100, canvasId: "eng-cycle-duration" },
    { label: "Reconnects", value: wsData.reconnect_count ?? 0, unit: "", warn: 3, canvasId: "eng-reconnects" },
    { label: "Active Streams", value: wsData.active_stream_count ?? 0, unit: "", warn: 50, canvasId: "eng-streams" },
    { label: "Queue Depth", value: wsData.buffer_message_count ?? 0, unit: "msgs", warn: 100, canvasId: "eng-queue" },
    { label: "Fresh Tickers", value: wsData.fresh_tickers ?? 0, unit: "", warn: 10, canvasId: "eng-tickers" },
  ];
  const container = document.getElementById("engine-metrics");
  if (!container) return;
  const cards = metrics.map(m => {
    const val = m.value ?? 0;
    const isWarn = val > m.warn;
    return el("div", { class: "kpi-card" }, [
      el("div", { class: "kpi-label", text: m.label }),
      el("div", { class: "kpi-value " + (isWarn ? "red" : "green"), text: text(val) + (m.unit ? " " + m.unit : "") }),
      el("canvas", { id: m.canvasId, width: 200, height: 40, style: "width:100%;height:40px;margin-top:6px" }),
    ]);
  });
  container.replaceChildren(...cards);

  _drawMockSparklines(metrics);
}

function _drawMockSparklines(metrics) {
  requestAnimationFrame(() => {
    metrics.forEach(m => {
      const canvas = document.getElementById(m.canvasId);
      if (!canvas) return;
      const base = m.value ?? 50;
      const mock = Array.from({ length: 20 }, (_, i) => ({ value: base + Math.sin(i * 0.5) * base * 0.2 + (Math.random() - 0.5) * base * 0.1 }));
      const isWarn = (m.value ?? 0) > m.warn;
      window.chart.sparkline(canvas, mock, { width: 200, height: 40, color: isWarn ? "#ff5b6b" : "#2fd17c" });
    });
  });
}
