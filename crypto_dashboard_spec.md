# Crypto Signal Bot — Advanced Dashboard Specification
> **Version:** 1.0  
> **Date:** 2026-05-27  
> **Target:** Event-driven signal bot (38 strategies, Polars pipeline, ConfluenceEngine, no auto-trading)  
> **Platform:** Windows 11 / Python 3.14.3  
> **Output Format:** Silent markdown spec for incremental implementation

---

## 1. Executive Summary

The dashboard is not a trading terminal. It is a **Signal Intelligence & Decision Support System** designed to:
- Maximize trader confidence per signal (3-second comprehension, 30-second deep analysis).
- Provide post-decision analytics (trader diary vs. bot signals).
- Audit the health of all 38 strategies and the Polars feature pipeline in real time.
- Enforce the "zones, not points" philosophy for entries, SL, and TP.

---

## 2. Core Module Architecture (10 Modules)

### Module 1: Live Signal Feed ("The River")
**Purpose:** Real-time stream of all generated signals across all symbols.

| Feature | Description |
|---------|-------------|
| **Smart Grouping** | Group signals by symbol; collapse duplicate timeframe signals. |
| **Confluence Badge** | 0-100 score with color gradient (cold → hot). Not binary good/bad. |
| **Strategy Pills** | Visual tags showing which of the 38 strategies fired (color-coded per strategy). |
| **Killzone Indicator** | Icon showing if signal landed inside London / NY / Asia session. |
| **TTL Countdown** | Time-to-live for the signal based on ATR and timeframe (auto-calculated). |
| **Confidence Breakdown** | Mini stacked-bar on hover: contribution weight per strategy. |
| **Regime Tag** | Trending / Ranging / Volatile / Breakout — derived from feature pipeline. |

**Layout:** Bento-grid on desktop; vertical card list on mobile. Auto-refresh via WebSocket.

---

### Module 2: Pair Deep-Dive ("The Microscope")
**Purpose:** Single-symbol forensic analysis when a trader clicks a signal.

| Feature | Description |
|---------|-------------|
| **Confluence Compass** | Radar/polar chart: each axis = 1 of 38 strategies. Value = strategy confidence (0-1). Round shape = independent confirmation; spiky = dominant single strategy. |
| **Indicator Snapshot** | Current raw + normalized values: RSI (0-100), ATR, VWAP (daily UTC reset), Supertrend state, Keltner channel position. |
| **Entry/SL/TP Zones** | Horizontal band visualization on price chart — **never single lines**. Zones calculated from ATR and confluence volatility model. |
| **Session Overlay** | Background shading on chart for London (blue), NY (green), Asia (yellow). |
| **Correlation Warning** | If multiple strategies that fired are highly correlated (>0.8), show amber warning: "Confluence may be inflated by similar strategies." |
| **Feature Vector Table** | Raw Polars feature values: `price_velocity`, `btc_correlation`, `volume_anomaly`, etc. Collapsible. |
| **Historical Context** | Last 10 signals on this pair: timestamp, confluence score, outcome (if logged). |

---

### Module 3: Strategy Performance Center ("The Arena")
**Purpose:** Per-strategy health monitoring and comparative analytics.

| Feature | Description |
|---------|-------------|
| **Strategy Grid** | 38 cards, each showing: win rate, avg confluence contribution, signals/hour, current status (Active / Suppressed / Error). |
| **Confluence Contribution Heatmap** | Matrix: rows = strategies, columns = symbols. Color = average weight contribution. Identifies which strategies drive which pairs. |
| **Strategy Correlation Matrix** | 38×38 matrix. Detects redundant strategies. Critical for audit. |
| **Drawdown & Fatigue** | If a strategy hasn't fired in N hours despite market movement, flag as "Potentially Blind." |
| **Feature Sensitivity** | Which raw features most correlate with this strategy firing (Polars_ols regression output). |
| **Killzone Efficiency** | Win rate of strategy inside vs. outside killzone sessions. |

---

### Module 4: Confluence Audit & Scoring Lab ("The Black Box")
**Purpose:** Explainability and debugging of the ConfluenceEngine.

| Feature | Description |
|---------|-------------|
| **Score Decomposition** | For any signal, show exact formula: weighted sum, normalization steps, caps/floors applied. |
| **What-If Simulator** | Slider to adjust individual strategy weights → see recalculated confluence score in real time. |
| **Outlier Detection** | Flag signals where score >90 but only 2 strategies fired (risk of false concentration). |
| **Historical Score Distribution** | Histogram of all confluence scores over last 24h/7d. Identify if engine is drifting (e.g., all scores clustering in 40-60 range = loss of discriminative power). |
| **Veto Log** | If a strategy was mathematically vetoed (e.g., RSI normalization bug prevented firing), show reason. |

---

### Module 5: Market Regime & Macro Context ("The Weather")
**Purpose:** Tell the trader *when* to trust signals, not just *which*.

| Feature | Description |
|---------|-------------|
| **Global Regime Dashboard** | BTC dominance, total market cap trend, funding rates (perp), volatility index (custom from ATR aggregation). |
| **Correlation Regime** | Altcoin-BTC correlation heatmap. When correlation → 1, alt signals are just BTC leverage. |
| **Session Quality Score** | Composite metric: volatility + volume + spread for each session. NY session with low volume = "Low Quality." |
| **News/Event Overlay** | Manual or API-fed event calendar (FOMC, ETF approvals, token unlocks). Signals during high-impact events flagged. |
| **Volatility Forecast** | GARCH or simple ATR-based projection for next 4h/24h. |

---

### Module 6: Trader Diary ("The Logbook")
**Purpose:** Manual trade logging linked to bot signals. This is the **most critical module** for a signal bot without auto-trading.

#### 6.1 Trade Entry Flow
```
Signal Detected → Trader clicks "Log Decision" → Form opens:
  - Decision: [Took Signal / Ignored / Counter-Traded]
  - Entry Price: [manual input, auto-suggested from zone midpoint]
  - Position Size: [manual, with risk calculator: "Risking 1% of $X at Y leverage"]
  - Leverage: [slider]
  - SL / TP: [auto-populated from bot zones, editable]
  - Mood / Notes: [free text, tags]
  - Screenshot: [optional upload]
```

#### 6.2 Post-Trade Closure
```
Trade Closed → Form opens:
  - Exit Price & Time
  - PnL (%) and ($)
  - Outcome Tag: [TP1 / TP2 / TP3 / SL / Breakeven / Manual Close]
  - Deviation Analysis: "Bot suggested SL at X, you set at Y. Difference: Z%"
  - Lesson Tag: [FOMO / Fear / Patience / Over-leverage / Perfect Execution / etc.]
```

#### 6.3 Diary Analytics
| Report | Description |
|--------|-------------|
| **Signal vs. Reality** | For every bot signal, track if trader acted and what happened. |
| **Confluence → PnL Correlation** | Scatter plot: X = confluence score at entry, Y = realized PnL. Find your personal "minimum viable confluence." |
| **Strategy Attribution** | Which strategies' signals you trade most, and which generate your best PnL. |
| **Session PnL** | PnL breakdown by killzone session. Are you losing money in Asia? |
| **Psychology Heatmap** | Win rate by "Mood" tag. Do you trade worse when angry? |
| **Missed Signals Report** | Signals you ignored that later hit TP — painful but necessary feedback loop. |
| **Journal Timeline** | Calendar view with color-coded days (green = profitable, red = loss, gray = no trades). Click day → see all trades + bot signals. |

#### 6.4 Bot → Diary Linkage
Every logged trade MUST reference:
- `signal_id` (UUID generated by SignalEngine)
- `confluence_score` (snapshot at signal time)
- `active_strategies[]` (which of 38 fired)
- `feature_vector` (Polars snapshot at signal time)

This creates a **queryable dataset** for later ML or statistical analysis.

---

### Module 7: Backtest & Simulation Lab ("The Sandbox")
**Purpose:** Validate strategy weight adjustments before deploying to live ConfluenceEngine.

| Feature | Description |
|---------|-------------|
| **Historical Signal Replay** | Scroll through past 30 days. See what signals fired, with full context. |
| **Paper Trade Simulator** | Apply current confluence weights to historical data → see how many signals would have fired and at what scores. |
| **Strategy Toggle Sandbox** | Disable 5 strategies in simulation → observe change in signal frequency and hypothetical PnL (using diary data as ground truth). |
| **Polars Streaming Replay** | Replay a specific 1-hour window to debug pipeline behavior (feature values, VWAP reset, Supertrend state). |

---

### Module 8: System Telemetry & Pipeline Health ("The Engine Room")
**Purpose:** DevOps visibility into the event-driven architecture.

| Metric | Source | Alert Threshold |
|--------|--------|-----------------|
| **WS Latency** | WebSocket → Polars ingest | >500ms |
| **Feature Pipeline Lag** | Polars streaming engine | >1 tick behind |
| **Signal Engine Cycle** | Per-symbol async loop | >100ms per symbol |
| **VWAP Reset Delta** | Daily UTC reset | >5s deviation |
| **RSI Normalization Health** | 0-100 bounds check | Any value outside [0,100] |
| **Supertrend State Consistency** | Numpy loop state vs. Polars | Mismatch detected |
| **Strategy Error Rate** | Per-strategy exception counter | >3 errors/hour |
| **Memory / CPU** | Host machine (Windows 11) | >80% sustained |

**Visuals:** Real-time sparklines for each metric. Red/yellow/green status pills. Alert log with timestamps.

---

### Module 9: Alerts & Notifications Hub ("The Siren")
**Purpose:** Configurable, multi-channel alerting without spam.

| Alert Type | Trigger | Channel |
|------------|---------|---------|
| **High-Confluence Signal** | Score >85 + 5+ strategies + inside killzone | Push + Sound |
| **Strategy Death** | Strategy error rate > threshold or silent >6h | Dashboard banner + Email |
| **Pipeline Stall** | WS latency > threshold | Dashboard banner + Sound |
| **Diary Reminder** | Open trade >4h without closure update | Push |
| **Macro Event** | High-impact news in <15 min | Push |
| **Confluence Regime Shift** | Average score across all pairs drops >20 points in 1h | Dashboard indicator |

**Smart Throttling:** Same symbol + same direction alerts within 15 min are grouped.

---

### Module 10: Settings & Configuration ("The Control Panel")
**Purpose:** Dynamic tuning without restarting the bot.

| Feature | Description |
|---------|-------------|
| **Strategy Manager** | Toggle any of 38 strategies ON/OFF. Changes propagate to ConfluenceEngine in real time (TOML hot-reload). |
| **Confluence Weights** | Slider matrix: adjust per-strategy weight. Preview impact on last 100 signals. |
| **Killzone Editor** | Modify session time boundaries (UTC). Toggle session gating ON/OFF per strategy. |
| **Symbol Watchlist** | Add/remove symbols from async SignalEngine. |
| **Diary Export** | Export all trades + linked signal data to CSV/JSON for external analysis. |
| **Dashboard Theme** | Dark / Light / OLED (true black). Font size scaling. |

---

## 3. Data Architecture & API Contract

### 3.1 Backend → Dashboard Data Flow
```
WebSocket Tick
    ↓
Polars Streaming Engine (feature pipeline)
    ↓
SignalEngine (per-symbol async)
    ↓
ConfluenceEngine (scoring)
    ↓
[Dashboard API Layer] ← REST + WebSocket
    ↓
Dashboard Frontend
```

### 3.2 Required API Endpoints

| Endpoint | Method | Data |
|----------|--------|------|
| `/api/v1/signals/live` | WS | Real-time signal stream (JSON) |
| `/api/v1/signals/history` | GET | Paginated historical signals |
| `/api/v1/signal/{id}` | GET | Full signal context (indicators, features, strategies) |
| `/api/v1/strategies/health` | GET | All 38 strategies status |
| `/api/v1/strategies/correlation` | GET | 38×38 correlation matrix |
| `/api/v1/confluence/audit/{signal_id}` | GET | Score decomposition |
| `/api/v1/confluence/simulate` | POST | What-if with custom weights |
| `/api/v1/market/regime` | GET | Global regime snapshot |
| `/api/v1/telemetry/pipeline` | GET / WS | Real-time pipeline metrics |
| `/api/v1/diary/trades` | GET / POST | CRUD for trader diary |
| `/api/v1/diary/analytics` | GET | Aggregated diary reports |
| `/api/v1/config/strategies` | GET / PATCH | Strategy toggle/weights |
| `/api/v1/config/killzone` | GET / PATCH | Session boundaries |

### 3.3 Signal JSON Schema (Core)
```json
{
  "signal_id": "uuid",
  "timestamp_utc": "2026-05-27T18:24:00Z",
  "symbol": "BTCUSDT",
  "timeframe": "15m",
  "direction": "LONG",
  "confluence_score": 87,
  "confluence_max_possible": 100,
  "active_strategies": [
    {"id": "keltner_breakout", "weight": 0.15, "raw_score": 0.92},
    {"id": "supertrend", "weight": 0.20, "raw_score": 0.85}
  ],
  "entry_zone": {"low": 67200.00, "high": 67500.00},
  "sl_zone": {"low": 66800.00, "high": 66950.00},
  "tp_zones": [
    {"level": 1, "low": 68100.00, "high": 68300.00},
    {"level": 2, "low": 69000.00, "high": 69500.00}
  ],
  "killzone_status": {"london": false, "ny": true, "asia": false},
  "market_regime": "trending",
  "feature_snapshot": {
    "rsi_norm": 62.5,
    "atr": 450.00,
    "vwap_deviation": 0.003,
    "price_velocity": 12.3,
    "btc_correlation_24h": 0.88
  },
  "ttl_seconds": 1800
}
```

### 3.4 Trade Diary JSON Schema
```json
{
  "trade_id": "uuid",
  "linked_signal_id": "uuid",
  "decision": "TOOK_SIGNAL",
  "entry": {"price": 67350.00, "time": "2026-05-27T18:25:00Z"},
  "size": {"amount": 0.05, "leverage": 5, "risk_percent": 1.0},
  "sl": {"price": 66900.00, "source": "MODIFIED"},
  "tp": [{"price": 68200.00, "level": 1}],
  "exit": {"price": 68400.00, "time": "2026-05-27T20:15:00Z", "reason": "TP1"},
  "pnl": {"percent": 7.8, "usd": 262.65},
  "mood": "confident",
  "tags": ["patience", "followed_plan"],
  "notes": "Waited for retest of zone midpoint. Bot was right."
}
```

---

## 4. UI/UX Structure

### 4.1 Navigation (Left Sidebar, Collapsible)
```
📡 Live Feed          ← Default landing
🔬 Pair Detail        ← Context-aware (last clicked signal)
🏟️ Strategy Arena
⚖️ Confluence Lab
🌤️ Market Weather
📓 Trader Diary       ← Badge: open trades count
🏖️ Sandbox
⚙️ Engine Room        ← Red dot if alerts active
🔔 Alert Center
🎛️ Control Panel
```

### 4.2 Layout Philosophy
- **Desktop:** 1440px+ — 3-column layout. Left nav (240px), main content (fluid), right contextual panel (320px: order book-style detail for selected item).
- **Tablet:** 2-column. Nav + main.
- **Mobile:** Single column. Bottom tab bar (Feed, Diary, Alerts, Settings).

### 4.3 Color System
| Token | Hex | Usage |
|-------|-----|-------|
| `bg-primary` | `#0B0E11` | Main background (Binance-style dark) |
| `bg-secondary` | `#151A21` | Cards, panels |
| `bg-tertiary` | `#1C2128` | Hover states |
| `accent-long` | `#0ECB81` | Long signals, profit |
| `accent-short` | `#F6465D` | Short signals, loss |
| `accent-confluence` | `#F0B90B` | Confluence score, warning |
| `session-london` | `#3B82F6` | 20% opacity overlay |
| `session-ny` | `#10B981` | 20% opacity overlay |
| `session-asia` | `#F59E0B` | 20% opacity overlay |
| `text-primary` | `#EAECEF` | Headings |
| `text-secondary` | `#848E9C` | Body, labels |

### 4.4 Typography
- **Monospace:** `JetBrains Mono` or `Fira Code` — for all numbers, prices, scores.
- **Sans:** `Inter` or `SF Pro Display` — for UI text.
- **Font sizes:** 12px (labels), 14px (body), 18px (headings), 24px (hero metrics).

---

## 5. Analytics & Reporting Layer

### 5.1 Trader Performance Matrix (Weekly Auto-Report)
Auto-generated every Sunday 00:00 UTC. Delivered in-app and exportable to PDF.

**Sections:**
1. **Bot Fidelity Score:** % of bot signals you acted on vs. ignored.
2. **Confluence Efficiency:** Your win rate broken down by confluence score buckets (0-50, 51-70, 71-85, 86-100).
3. **Strategy Synergy:** Top 5 strategies by your PnL. Bottom 5 by loss.
4. **Session Report:** PnL by killzone + outside sessions.
5. **Risk Discipline:** Avg leverage used vs. bot suggestion. SL adherence score.
6. **Psychology Score:** Win rate by mood tag. "FOMO" trades vs. "Patient" trades.
7. **Missed Opportunities:** Top 3 ignored signals that hit TP. Top 3 taken signals that hit SL.

### 5.2 Bot Performance Matrix (System Report)
1. **Signal Frequency:** Signals/hour by symbol and session.
2. **Confluence Distribution:** Is the engine producing discriminative scores?
3. **Strategy Health:** Error rates, correlation drift, silent strategies.
4. **Pipeline Performance:** Polars streaming throughput (rows/sec), latency percentiles.

---

## 6. Implementation Roadmap

### Phase 1: Foundation (Week 1-2)
- [ ] API layer: `/signals/live`, `/signals/history`, `/signal/{id}`
- [ ] Dashboard shell: layout, nav, theme system
- [ ] Module 1: Live Signal Feed (basic cards)
- [ ] Module 2: Pair Deep-Dive (indicator snapshot + zones)
- [ ] WebSocket integration for real-time updates

### Phase 2: Intelligence (Week 3-4)
- [ ] Module 3: Strategy Arena (health grid + correlation matrix)
- [ ] Module 4: Confluence Lab (decomposition + simulator)
- [ ] Module 8: Engine Room (telemetry + alerts)
- [ ] Module 9: Alerts Hub

### Phase 3: Decision Support (Week 5-6)
- [ ] Module 6: Trader Diary (entry/exit flow + basic analytics)
- [ ] Module 5: Market Weather (regime dashboard)
- [ ] Diary → Signal linkage (full referential integrity)

### Phase 4: Advanced Analytics (Week 7-8)
- [ ] Weekly auto-reports (Trader + Bot matrices)
- [ ] Module 7: Sandbox (replay + simulation)
- [ ] Module 10: Control Panel (hot-reload config)
- [ ] Export functionality (CSV, JSON, PDF)

### Phase 5: Polish (Week 9-10)
- [ ] Mobile responsiveness
- [ ] Performance optimization (virtualized lists, WebSocket compression)
- [ ] Onboarding tutorial for new users
- [ ] Keyboard shortcuts

---

## 7. Integration Checklist with Existing Bot

| Bot Component | Dashboard Integration Point | Status to Track |
|---------------|----------------------------|-----------------|
| **38 Strategies** | Strategy Arena toggle + Confluence Lab weights | Enabled/disabled per TOML |
| **ConfluenceEngine** | Confluence Lab audit + Pair Deep-Dive compass | Score formula version |
| **Polars Pipeline** | Engine Room telemetry + Pair Detail feature vector | RSI norm bounds, VWAP reset, Supertrend state |
| **Killzone Gating** | Session overlay + Signal Feed badge + Strategy Arena efficiency | Session boundaries UTC |
| **Per-Symbol Async** | Feed grouping + Engine Room cycle latency | Symbols active in SignalEngine |
| **Entry/SL/TP Zones** | Pair Detail zone bands | Zone calculation version |
| **WebSocket+REST** | All real-time modules | Connection health, reconnect count |

---

## 8. Open Questions for You

1. **Frontend stack preference?** React/Vue/Svelte, or Python-native (Streamlit/Gradio/Panel) for rapid prototyping?
2. **Diary storage:** SQLite local file, or external DB (PostgreSQL)? Given Windows 11 + no venv, SQLite may be simplest.
3. **WebSocket fan-out:** Dashboard connects directly to bot's WS, or via separate relay/API layer?
4. **Multi-user?** Is this personal, or will multiple traders use it?
5. **Mobile necessity?** Phone-optimized view, or desktop-only?

---

*End of Specification*
