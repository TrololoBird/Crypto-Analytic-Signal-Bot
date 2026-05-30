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

  function gauge(canvas, value, { width = 160, height = 90, min = 0, max = 100, color, threshold = 0.5 } = {}) {
    const ctx = setup(canvas, width, height);
    const cx = width / 2;
    const cy = height - 8;
    const r = Math.min(cx, cy) - 4;
    const clamped = Math.max(min, Math.min(max, value));
    const pct = (clamped - min) / (max - min);
    const startAngle = Math.PI;
    const endAngle = 0;
    const arcLength = Math.PI;
    const valueAngle = startAngle + pct * arcLength;
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

  return { sparkline, barChart, radarChart, gauge };
})();
