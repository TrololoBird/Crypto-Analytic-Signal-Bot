window.chart = (() => {
  const DPR = window.devicePixelRatio || 1;

  function setup(canvas, width, height) {
    canvas.width = width * DPR;
    canvas.height = height * DPR;
    canvas.style.width = width + "px";
    canvas.style.height = height + "px";
    const ctx = canvas.getContext("2d");
    ctx.scale(DPR, DPR);
    return ctx;
  }

  function sparkline(canvas, data, { width = 200, height = 48, color = "#63a5ff", min, max, fill = true } = {}) {
    if (!data || data.length < 2) return;
    const ctx = setup(canvas, width, height);
    const values = data.map(v => (typeof v === "object" ? v.value : v));
    const lo = min ?? Math.min(...values);
    const hi = max ?? Math.max(...values);
    const range = hi - lo || 1;
    const pad = 2;
    const w = width - pad * 2;
    const h = height - pad * 2;
    const points = values.map((v, i) => ({
      x: pad + (i / (values.length - 1)) * w,
      y: pad + h - ((v - lo) / range) * h,
    }));

    ctx.beginPath();
    ctx.moveTo(points[0].x, points[0].y);
    for (let i = 1; i < points.length; i++) {
      ctx.lineTo(points[i].x, points[i].y);
    }
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.stroke();

    if (fill) {
      ctx.lineTo(points[points.length - 1].x, pad + h);
      ctx.lineTo(points[0].x, pad + h);
      ctx.closePath();
      const grad = ctx.createLinearGradient(0, pad, 0, pad + h);
      grad.addColorStop(0, color + "40");
      grad.addColorStop(1, color + "05");
      ctx.fillStyle = grad;
      ctx.fill();
    }
  }

  function bar(ctx, x, y, w, h, color, radius = 2) {
    const r = Math.min(radius, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + w - r, y);
    ctx.quadraticCurveTo(x + w, y, x + w, y + r);
    ctx.lineTo(x + w, y + h);
    ctx.lineTo(x, y + h);
    ctx.lineTo(x, y + r);
    ctx.quadraticCurveTo(x, y, x + r, y);
    ctx.closePath();
    ctx.fillStyle = color;
    ctx.fill();
  }

  function barChart(canvas, data, { width = 300, height = 160, color = "#63a5ff", barWidth, gap = 2 } = {}) {
    if (!data || data.length === 0) return;
    const ctx = setup(canvas, width, height);
    const maxVal = Math.max(...data.map(d => d.value || d.count || 0), 1);
    const pad = { t: 8, r: 8, b: 20, l: 8 };
    const w = width - pad.l - pad.r;
    const h = height - pad.t - pad.b;
    const bw = barWidth ?? Math.max(4, Math.floor(w / data.length) - gap);
    const fullBw = bw + gap;

    data.forEach((d, i) => {
      const val = d.value ?? d.count ?? 0;
      const bh = (val / maxVal) * h;
      const x = pad.l + i * fullBw;
      const y = pad.t + h - bh;
      bar(ctx, x, y, bw, bh, d.color || color);
    });
  }

  function radarChart(canvas, data, { width = 240, height = 240, levels = 5, color = "#63a5ff" } = {}) {
    if (!data || data.length < 3) return;
    const ctx = setup(canvas, width, height);
    const cx = width / 2;
    const cy = height / 2;
    const radius = Math.min(cx, cy) - 20;
    const angleStep = (Math.PI * 2) / data.length;
    const maxVal = Math.max(...data.map(d => d.value ?? 1), 1);

    for (let l = 1; l <= levels; l++) {
      const r = (radius / levels) * l;
      ctx.beginPath();
      for (let i = 0; i <= data.length; i++) {
        const a = -Math.PI / 2 + i * angleStep;
        const x = cx + Math.cos(a) * r;
        const y = cy + Math.sin(a) * r;
        i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
      }
      ctx.strokeStyle = "#1d2631";
      ctx.lineWidth = 1;
      ctx.stroke();
    }

    for (let i = 0; i < data.length; i++) {
      const a = -Math.PI / 2 + i * angleStep;
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.lineTo(cx + Math.cos(a) * radius, cy + Math.sin(a) * radius);
      ctx.strokeStyle = "#1d2631";
      ctx.stroke();
    }

    ctx.beginPath();
    data.forEach((d, i) => {
      const a = -Math.PI / 2 + i * angleStep;
      const r = (d.value / maxVal) * radius;
      const x = cx + Math.cos(a) * r;
      const y = cy + Math.sin(a) * r;
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.closePath();
    ctx.fillStyle = color + "30";
    ctx.fill();
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.stroke();

    data.forEach((d, i) => {
      const a = -Math.PI / 2 + i * angleStep;
      const r = (d.value / maxVal) * radius;
      const x = cx + Math.cos(a) * r;
      const y = cy + Math.sin(a) * r;
      ctx.beginPath();
      ctx.arc(x, y, 3, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();
    });
  }

  function gauge(canvas, value, { width = 160, height = 90, min = 0, max = 100, color } = {}) {
    const ctx = setup(canvas, width, height);
    const cx = width / 2;
    const cy = height - 8;
    const r = Math.min(cx, cy) - 4;
    const clamped = Math.max(min, Math.min(max, value));
    const pct = (clamped - min) / (max - min || 1);
    const startAngle = Math.PI;
    const endAngle = 0;
    const valueAngle = startAngle + pct * Math.PI;
    const gaugeColor = color || (pct >= 0.8 ? "#2fd17c" : pct >= 0.4 ? "#f5bf4f" : "#ff5b6b");

    ctx.beginPath();
    ctx.arc(cx, cy, r * 0.7, startAngle, endAngle);
    ctx.strokeStyle = "#1d2631";
    ctx.lineWidth = 12;
    ctx.lineCap = "round";
    ctx.stroke();

    ctx.beginPath();
    ctx.arc(cx, cy, r * 0.7, startAngle, valueAngle);
    ctx.strokeStyle = gaugeColor;
    ctx.lineWidth = 12;
    ctx.lineCap = "round";
    ctx.stroke();

    ctx.fillStyle = "#e8edf5";
    ctx.font = "bold 20px " + (getComputedStyle(document.body).fontFamily || "monospace");
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(Math.round(clamped) + "%", cx, cy + 6);
  }

  function drawHLine(ctx, y, x1, x2, color, label) {
    ctx.save();
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = color;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x1, y);
    ctx.lineTo(x2, y);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = color;
    ctx.font = "10px monospace";
    ctx.textAlign = "left";
    ctx.fillText(label, x1 + 2, y - 3);
    ctx.restore();
  }

  function signalChart(canvas, klines, zones, { width = 340, height = 150 } = {}) {
    if (!klines || klines.length < 2) return;
    const ctx = setup(canvas, width, height);
    ctx.fillStyle = "#0c1118";
    ctx.fillRect(0, 0, width, height);

    const pad = { t: 8, r: 8, b: 14, l: 8 };
    const plotW = width - pad.l - pad.r;
    const plotH = height - pad.t - pad.b;

    const prices = [];
    klines.forEach((k) => {
      prices.push(k.high, k.low, k.open, k.close);
    });
    ["entry", "stop", "tp1", "tp2", "tp3", "current"].forEach((key) => {
      const v = Number(zones[key]);
      if (v > 0) prices.push(v);
    });
    let lo = Math.min(...prices);
    let hi = Math.max(...prices);
    const span = hi - lo || hi * 0.01 || 1;
    lo -= span * 0.08;
    hi += span * 0.08;

    const yFor = (price) => pad.t + plotH - ((price - lo) / (hi - lo)) * plotH;
    const bandH = Math.max(3, plotH * 0.045);

    function fillBand(price, color) {
      if (!price || price <= 0) return;
      const y = yFor(price) - bandH / 2;
      ctx.fillStyle = color;
      ctx.fillRect(pad.l, y, plotW, bandH);
    }

    fillBand(zones.stop, "rgba(255,91,107,0.18)");
    fillBand(zones.entry, "rgba(99,165,255,0.18)");
    fillBand(zones.tp1, "rgba(47,209,124,0.16)");
    fillBand(zones.tp2, "rgba(47,209,124,0.10)");
    fillBand(zones.tp3, "rgba(47,209,124,0.08)");

    const candleW = Math.max(2, plotW / klines.length - 1);
    klines.forEach((k, i) => {
      const x = pad.l + i * (plotW / (klines.length - 1));
      const openY = yFor(k.open);
      const closeY = yFor(k.close);
      const highY = yFor(k.high);
      const lowY = yFor(k.low);
      const up = k.close >= k.open;
      const color = up ? "#2fd17c" : "#ff5b6b";
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, highY);
      ctx.lineTo(x, lowY);
      ctx.stroke();
      const top = Math.min(openY, closeY);
      const bodyH = Math.max(1, Math.abs(closeY - openY));
      ctx.fillStyle = color;
      ctx.fillRect(x - candleW / 2, top, candleW, bodyH);
    });

    if (zones.stop > 0) drawHLine(ctx, yFor(zones.stop), pad.l, pad.l + plotW, "#ff5b6b", "SL");
    if (zones.entry > 0) drawHLine(ctx, yFor(zones.entry), pad.l, pad.l + plotW, "#63a5ff", "Entry");
    if (zones.tp1 > 0) drawHLine(ctx, yFor(zones.tp1), pad.l, pad.l + plotW, "#2fd17c", "TP1");
    if (zones.current > 0) drawHLine(ctx, yFor(zones.current), pad.l, pad.l + plotW, "#f5bf4f", "Now");
  }

  return { sparkline, barChart, radarChart, gauge, signalChart };
})();
