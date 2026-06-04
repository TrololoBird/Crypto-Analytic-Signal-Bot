let riverSignals = [];
let riverInterval = null;
const RIVER_COLORS = {
  breakout: "#63a5ff", reversal: "#ff5b6b", continuation: "#2fd17c",
  volatility: "#ff9f43", liquidity: "#53d5d5", orderflow: "#a78bfa",
  sentiment: "#f472b6", multi_asset: "#34d399", session: "#f5bf4f",
  orderbook: "#6b7280", generic: "#95a2b4",
};

function renderRiver() {
  _fetchRiverSignals();
  if (riverInterval) clearInterval(riverInterval);
  riverInterval = setInterval(_fetchRiverSignals, 15000);
}

function _fetchRiverSignals() {
  fetch("/api/v1/signals/live?limit=30", { cache: "no-store" })
    .then((r) => r.ok ? r.json() : [])
    .then((data) => {
      riverSignals = data;
      _renderRiverCards(data);
      _renderRiverKPIs(data);
    })
    .catch((err) => console.warn("river fetch error", err));
}

function _renderRiverKPIs(signals) {
  const delivered = signals.filter((s) => {
    const st = String(s.delivery_status || "");
    return st === "sent" || st === "logged" || st === "delivered";
  });
  const avgScore = signals.length
    ? signals.reduce((a, s) => a + (s.confluence_score || s.score || 0), 0) / signals.length
    : 0;
  setChildren("river-kpis", [
    kpi("Сигналов", signals.length, "в ленте", "blue"),
    kpi("Отправлено", delivered.length, "в Telegram / лог", "green"),
    kpi("Уверенность", avgScore.toFixed(0), "средняя оценка", clsByValue(avgScore / 100)),
    kpi("Лонг / Шорт", _directionSplitRu(signals), "", "cyan"),
    kpi("Сессия", _activeKillzoneRu(signals), "торговая зона UTC", "orange"),
  ]);
  _renderSignalsHint(signals);
}

function _renderSignalsHint(signals) {
  const hint = document.getElementById("signals-hint");
  if (!hint) return;
  if (signals.length > 0) {
    hint.replaceChildren();
    hint.style.display = "none";
    return;
  }
  const o = App.state.overview || {};
  const funnel = App.state.funnel || {};
  const totals = funnel.cycle_totals || {};
  const candidates = totals.candidates ?? o.last_cycle_candidates ?? 0;
  const delivered = App.state.delivery?.delivery_count ?? o.session_delivered ?? 0;
  const combined = funnel.top_blocker || funnel.combined_reject_hint || {};
  const reject = o.top_blocker?.key || o.top_rejection?.key || combined.key;
  const rejectRu = o.top_blocker?.label_ru || o.top_rejection?.label_ru || combined.label_ru;
  hint.style.display = "block";
  hint.replaceChildren(
    el("div", { class: "panel-hint" }, [
      el("strong", { text: "Сигналов пока нет. " }),
      el("span", {
        text:
          "Бот работает" +
          (o.btc_bias ? " · BTC " + _biasRu(o.btc_bias) : "") +
          (candidates ? " · кандидатов за цикл: " + candidates : "") +
          (delivered ? " · отправлено: " + delivered : "") +
          (reject ? " · частый фильтр: " + (rejectRu || _rejectRu(reject)) : "") +
          ".",
      }),
    ])
  );
}

function _biasRu(bias) {
  const map = { uptrend: "рост", downtrend: "падение", neutral: "боковик" };
  return map[String(bias).toLowerCase()] || bias;
}

function _rejectRu(key) {
  const map = App.state.labelMaps?.reject_reasons || {
    score_too_low: "низкая оценка",
    shortlist_not_routed: "не в shortlist",
    confirmation_failed: "нет подтверждения",
    tracking_blocked: "уже отслеживается",
    hard_confluence_gate: "слабый confluence",
    limit_publish_rejected: "план недействителен при публикации",
    limit_setup_invalidated: "план недействителен при публикации",
  };
  return map[String(key)] || String(key).replace(/_/g, " ");
}

function _directionSplitRu(signals) {
  const longs = signals.filter((s) => (s.direction || "long") === "long").length;
  return longs + " / " + (signals.length - longs);
}

function _activeKillzoneRu(signals) {
  const kz = signals[0]?.killzone || {};
  if (kz.london) return "Лондон";
  if (kz.ny) return "Нью-Йорк";
  if (kz.asia) return "Азия";
  return "вне сессии";
}

function _renderRiverCards(signals) {
  const container = document.getElementById("river-cards");
  if (!container) return;
  if (!signals.length) {
    container.replaceChildren(
      el("div", { class: "empty" }, [
        el("div", { text: "Пока нет сигналов в ленте" }),
        el("div", {
          class: "soft",
          style: "margin-top:8px;font-size:13px",
          text: "Новые сигналы появятся здесь сразу после отправки. Проверьте вкладку «Отслеживание» для активных планов.",
        }),
      ])
    );
    return;
  }
  const cards = signals.map(_buildSignalCard);
  container.replaceChildren(...cards);
  cards.forEach((card, i) => {
    const canvas = card.querySelector(".river-chart");
    if (!canvas) return;
    canvas.style.display = "block";
    paintSignalChart(canvas, signals[i], { width: 340, height: 120 });
  });
}

function _buildSignalCard(sig) {
  const score = sig.confluence_score || 0;
  const dir = (sig.direction || "long").toLowerCase();
  const dirRu = dir === "short" ? "Шорт" : "Лонг";
  const dirColor = dir === "short" ? "var(--red)" : "var(--green)";
  const kz = sig.killzone || {};
  const hasKZ = kz.london || kz.ny || kz.asia;
  const kzLabel = hasKZ
    ? (kz.london ? "Лондон" : kz.ny ? "Нью-Йорк" : "Азия")
    : null;

  const pillColors = { breakout: "#63a5ff", reversal: "#ff5b6b", continuation: "#2fd17c",
    volatility: "#ff9f43", liquidity: "#53d5d5", orderflow: "#a78bfa",
    sentiment: "#f472b6", multi_asset: "#34d399", session: "#f5bf4f",
    orderbook: "#6b7280" };

  const strategies = sig.active_strategies || [];
  if (sig.setup_id && !strategies.find((s) => s.id === sig.setup_id)) {
    strategies.unshift({ id: sig.setup_id, family: "generic" });
  }

  return el("div", { class: "signal-card", style: "cursor:pointer", onclick: () => showSignalDetail(sig) }, [
    el("div", { class: "sc-head" }, [
      el("div", { style: "display:flex;align-items:center;gap:8px" }, [
        el("span", { class: "sc-symbol", style: "color:" + dirColor, text: sig.symbol || "?" }),
        el("span", { style: "font-size:12px;color:" + dirColor + ";font-weight:700", text: dirRu }),
      ]),
      el("span", {
        class: "sc-confluence",
        style: "background:" + sig.confluence_color + "20;color:" + sig.confluence_color + ";border:1px solid " + sig.confluence_color + "40",
        text: score >= 10 ? Math.round(score) + "" : "<10",
      }),
    ]),
    el("div", { class: "sc-pills" }, strategies.slice(0, 8).map((s) => {
      const family = s.family || "generic";
      const c = pillColors[family] || pillColors.generic;
      return el("span", {
        class: "sc-pill",
        style: "background:" + c + "20;color:" + c + ";border:1px solid " + c + "40",
        text: s.id.length > 18 ? s.id.slice(0, 16) + ".." : s.id,
      });
    })),
    el("div", { class: "sc-meta" }, [
      kzLabel ? el("span", { text: "⚡ " + kzLabel }) : null,
      el("span", { text: sig.market_regime || "-" }),
      el("span", { text: sig.timeframe || "15m" }),
      el("span", { text: _timeAgo(sig.ts || sig.timestamp || sig.created_at) }),
    ].filter(Boolean)),
    el("div", { class: "sc-zone" }, [
      _zoneItem("Вход", sig.entry_price || sig.entry_low || "-"),
      _zoneItem("Стоп", sig.stop_price || sig.stop || "-", "red"),
      _zoneItem("Цель", sig.tp1_price || sig.take_profit_1 || "-", "green"),
    ]),
    sig.tracking_id
      ? el("div", { style: "margin-top:4px;display:grid;gap:6px" }, [
          el("button", {
            class: "tab",
            text: "Записать в дневник",
            style: "width:100%;border:1px solid var(--line);padding:6px;font-size:12px;cursor:pointer",
            onclick: (e) => {
              e.stopPropagation();
              showDiaryEntryModal(sig);
            },
          }),
        ])
      : null,
    el("canvas", {
      class: "river-chart",
      "data-symbol": sig.symbol || "",
      "data-timeframe": sig.timeframe || "15m",
      width: 340,
      height: 120,
      style: "width:100%;height:120px;border-radius:8px;display:none",
    }),
  ].filter(Boolean));
}

function _zoneItem(label, price, color) {
  return el("div", { class: "sc-zone-item" }, [
    el("div", { class: "sc-zone-label", text: label }),
    el("div", { class: "sc-zone-price", style: color ? "color:var(--" + color + ")" : "", text: typeof price === "number" ? price.toFixed(2) : String(price) }),
  ]);
}

function _timeAgo(ts) {
  if (!ts) return "";
  const diff = Date.now() - new Date(ts).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "только что";
  if (mins < 60) return mins + " мин";
  const hrs = Math.floor(mins / 60);
  return hrs + " ч";
}
