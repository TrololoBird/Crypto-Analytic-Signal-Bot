let diaryState = { trades: [], analytics: null, loading: false };

async function renderDiary() {
  if (diaryState.loading) return;
  diaryState.loading = true;
  try {
    const [trades, analytics] = await Promise.all([
      fetch("/api/v1/diary/trades?limit=50").then((r) => (r.ok ? r.json() : [])),
      fetch("/api/v1/diary/analytics?days=30").then((r) => (r.ok ? r.json() : null)),
    ]);
    diaryState.trades = trades;
    diaryState.analytics = analytics;
    _renderDiaryCalendar(analytics?.calendar || []);
    _renderDiaryTrades(trades);
    _renderDiaryAnalytics(analytics);
    _updateDiaryBadge(trades);
  } catch (err) {
    console.warn("diary fetch error", err);
  }
  diaryState.loading = false;
}

function _updateDiaryBadge(trades) {
  const openCount = trades.filter((t) => !t.exit_price).length;
  const badge = document.getElementById("diary-badge");
  if (badge) {
    if (openCount > 0) {
      badge.textContent = openCount;
      badge.style.display = "inline-flex";
    } else {
      badge.style.display = "none";
    }
  }
}

function _renderDiaryCalendar(calendarDays) {
  const container = document.getElementById("diary-calendar");
  if (!container) return;
  const now = new Date();
  const year = now.getFullYear();
  const month = now.getMonth();
  const firstDay = new Date(year, month, 1).getDay();
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  const dayMap = {};
  for (const d of calendarDays) {
    dayMap[d.day] = d;
  }
  const dayNames = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const children = dayNames.map((n) =>
    el("div", { class: "diary-day empty", text: n })
  );
  for (let i = 0; i < firstDay; i++) {
    children.push(el("div", { class: "diary-day empty" }));
  }
  for (let day = 1; day <= daysInMonth; day++) {
    const dateStr = `${year}-${String(month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
    const calDay = dayMap[dateStr];
    let cls = "diary-day neutral";
    if (calDay) {
      if ((calDay.pnl_usd || 0) > 0) cls = "diary-day profitable";
      else if ((calDay.pnl_usd || 0) < 0) cls = "diary-day loss";
    }
    children.push(el("div", { class: cls, text: String(day) }));
  }
  container.replaceChildren(...children);
}

function _renderDiaryTrades(trades) {
  const container = document.getElementById("diary-trades");
  if (!container) return;
  const items = trades.map((t) => {
    const signalInfo = t.bot_signal_snapshot?.symbol
      ? t.bot_signal_snapshot.symbol + " "
      : "";
    const statusText = t.exit_price
      ? (t.pnl_percent >= 0 ? "✅ " : "❌ ") + t.pnl_percent + "%"
      : "🔵 open";
    const statusClass = t.exit_price
      ? (t.pnl_percent >= 0 ? "green" : "red")
      : "blue";
    const meta = t.bot_signal_snapshot?.setup_id
      ? t.bot_signal_snapshot.setup_id
      : t.decision;
    return el("div", { class: "row" }, [
      el("div", { class: "row-main" }, [
        el("div", {
          class: "row-title",
          html: signalInfo + t.decision.replace("_", " "),
        }),
        el("div", {
          class: "row-meta",
          text: meta + " | " + (t.entry_time || t.created_at || ""),
        }),
      ]),
      el("div", {
        class: "row-value " + statusClass,
        text: statusText,
      }),
    ]);
  });
  setChildren("diary-trades", rowsOrEmpty(items, (i) => i, "No diary entries yet."));
}

function _renderDiaryAnalytics(analytics) {
  const container = document.getElementById("diary-analytics");
  if (!container || !analytics) {
    if (container) container.replaceChildren();
    return;
  }
  const summary = analytics.summary || {};
  const children = [
    el("div", { class: "grid kpi" }, [
      kpi("Total Trades", summary.total_trades || 0, "", "blue"),
      kpi("Win Rate", pct(summary.win_rate || 0), "last 30 days", clsByValue(summary.win_rate)),
      kpi("Avg PnL %", summary.avg_pnl_percent + "%", "", Number(summary.avg_pnl_percent || 0) >= 0 ? "green" : "red"),
      kpi("Avg PnL $", "$" + number(summary.avg_pnl_usd || 0, 2), "", Number(summary.avg_pnl_usd || 0) >= 0 ? "green" : "red"),
    ]),
  ];
  if (analytics.by_mood && analytics.by_mood.length) {
    const moodRows = analytics.by_mood.map((m) =>
      simpleRow(m.mood, "trades: " + m.count, (m.avg_pnl || 0).toFixed(2) + "%", Number(m.avg_pnl || 0) >= 0 ? "green" : "red")
    );
    children.push(
      el("div", { class: "panel", style: "margin-top:14px" }, [
        el("div", { class: "panel-header" }, [
          el("h2", { class: "panel-title", text: "Psychology Heatmap" }),
          el("span", { class: "panel-subtitle", text: "PnL by mood" }),
        ]),
        el("div", { class: "panel-body" }, [
          el("div", { class: "row-list" }, moodRows),
        ]),
      ])
    );
  }
  container.replaceChildren(...children);
}

function showDiaryEntryModal(signalData) {
  const modal = document.createElement("div");
  modal.className = "modal-overlay";
  modal.innerHTML = `
    <div class="modal">
      <h2>Log Decision</h2>
      <label>Decision</label>
      <select id="diary-decision">
        <option value="took_signal">Took Signal</option>
        <option value="ignored">Ignored</option>
        <option value="counter_traded">Counter-Traded</option>
      </select>
      <label>Entry Price</label>
      <input id="diary-entry-price" type="number" step="0.01" value="${signalData?.entry_price || ""}">
      <label>Position Size</label>
      <input id="diary-size" type="number" step="0.001" placeholder="BTC amount">
      <label>Leverage</label>
      <input id="diary-leverage" type="number" step="0.5" value="1" min="1" max="125">
      <label>Risk % of Account</label>
      <input id="diary-risk" type="number" step="0.1" value="1" min="0.1" max="100">
      <label>Stop Loss</label>
      <input id="diary-sl" type="number" step="0.01" value="${signalData?.stop_price || ""}">
      <label>Take Profit 1</label>
      <input id="diary-tp1" type="number" step="0.01" value="${signalData?.tp1_price || ""}">
      <label>Mood</label>
      <select id="diary-mood">
        <option value="">--</option>
        <option value="confident">Confident</option>
        <option value="cautious">Cautious</option>
        <option value="fomo">FOMO</option>
        <option value="fear">Fear</option>
        <option value="impatient">Impatient</option>
        <option value="patient">Patient</option>
      </select>
      <label>Notes</label>
      <textarea id="diary-notes"></textarea>
      <div class="modal-buttons">
        <button onclick="this.closest('.modal-overlay').remove()">Cancel</button>
        <button class="primary" onclick="window._saveDiaryEntry(this)">Save</button>
      </div>
    </div>`;
  document.getElementById("modal-container").replaceChildren(modal);
  modal.dataset.signalData = JSON.stringify(signalData || {});
}

window._saveDiaryEntry = async function (btn) {
  const modal = btn.closest(".modal-overlay");
  const signalData = JSON.parse(modal.dataset.signalData || "{}");
  const body = {
    linked_signal_id: signalData.tracking_id || signalData.signal_id || null,
    decision: document.getElementById("diary-decision").value,
    entry_price: parseFloat(document.getElementById("diary-entry-price").value) || null,
    size_amount: parseFloat(document.getElementById("diary-size").value) || null,
    leverage: parseFloat(document.getElementById("diary-leverage").value) || null,
    risk_percent: parseFloat(document.getElementById("diary-risk").value) || null,
    sl_price: parseFloat(document.getElementById("diary-sl").value) || null,
    tp_prices: [
      parseFloat(document.getElementById("diary-tp1").value) || null,
    ].filter(Boolean),
    mood: document.getElementById("diary-mood").value || null,
    notes: document.getElementById("diary-notes").value || null,
    bot_signal_snapshot: signalData,
  };
  try {
    const res = await fetch("/api/v1/diary/trades", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (res.ok) {
      modal.remove();
      renderDiary();
    }
  } catch (err) {
    console.error("save diary entry error", err);
  }
};

function showDiaryCloseModal(trade) {
  const modal = document.createElement("div");
  modal.className = "modal-overlay";
  const entryPrice = trade.entry_price || 0;
  const entryText = entryPrice ? "$" + Number(entryPrice).toFixed(2) : "-";
  modal.innerHTML = `
    <div class="modal">
      <h2>Close Trade</h2>
      <div class="row-list">
        <div class="row"><div class="row-title">Entry</div><div class="row-value mono">${entryText}</div></div>
        <div class="row"><div class="row-title">SL</div><div class="row-value mono">${trade.sl_price ? "$" + Number(trade.sl_price).toFixed(2) : "-"}</div></div>
      </div>
      <label>Exit Price</label>
      <input id="diary-exit-price" type="number" step="0.01" placeholder="0">
      <label>Exit Reason</label>
      <select id="diary-exit-reason">
        <option value="tp1">TP1</option>
        <option value="tp2">TP2</option>
        <option value="tp3">TP3</option>
        <option value="sl">Stop Loss</option>
        <option value="breakeven">Breakeven</option>
        <option value="manual_close">Manual Close</option>
      </select>
      <label>PnL %</label>
      <input id="diary-pnl-pct" type="number" step="0.1" placeholder="0">
      <label>PnL $</label>
      <input id="diary-pnl-usd" type="number" step="0.01" placeholder="0">
      <label>Mood</label>
      <select id="diary-close-mood">
        <option value="">--</option>
        <option value="confident">Confident</option>
        <option value="regret">Regret</option>
        <option value="satisfied">Satisfied</option>
        <option value="frustrated">Frustrated</option>
      </select>
      <label>Notes</label>
      <textarea id="diary-close-notes"></textarea>
      <div class="modal-buttons">
        <button onclick="this.closest('.modal-overlay').remove()">Cancel</button>
        <button class="primary" onclick="window._saveDiaryClose(this, '${trade.id}')">Close Trade</button>
      </div>
    </div>`;
  document.getElementById("modal-container").replaceChildren(modal);
}

window._saveDiaryClose = async function (btn, tradeId) {
  const modal = btn.closest(".modal-overlay");
  const body = {
    exit_price: parseFloat(document.getElementById("diary-exit-price").value) || 0,
    exit_reason: document.getElementById("diary-exit-reason").value,
    pnl_percent: parseFloat(document.getElementById("diary-pnl-pct").value) || null,
    pnl_usd: parseFloat(document.getElementById("diary-pnl-usd").value) || null,
    mood: document.getElementById("diary-close-mood").value || null,
    notes: document.getElementById("diary-close-notes").value || null,
  };
  try {
    const res = await fetch(`/api/v1/diary/trades/${tradeId}/close`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (res.ok) {
      modal.remove();
      renderDiary();
    }
  } catch (err) {
    console.error("close trade error", err);
  }
};
