# Hunt reference deep-dive — Tier A external OSS

Scope: **external eligible signal-only CEX bots ↔ Hunt only**. No `bot/` main bot.  
Search was by **functional profile** (pump/dump, Telegram, CEX, manual) — never by name «hunter/охотник».

---

## A1 — brianleect/binance-pump-alerts (~153★)

| Field | Detail |
|-------|--------|
| **Eligibility** | ✅ Signal-only — REST ticker poll → Telegram |
| **Architecture** | `pumpAlerts.py` → `BinancePumpAndDumpAlerter.run()` · `config.yml` · `ReportGenerator` |
| **Core loop** | `extractInterval` (default 1s) → fetch all tickers → rolling price arrays per `chartIntervals` → compare % move vs `outlierIntervals` → TG alert or skip via `alertSkipThreshold` |
| **Key params** | `chartIntervals`: 1s…6h · `outlierIntervals`: e.g. 1s=0.02%, 5m=0.10%, 1h=0.30% · `topReportIntervals` + `topPumpEnabled` → digest top-N |
| **Latency class** | L0 instant (1s poll) — **not** human-manual confirm path |

### Borrow → Hunt

| Pattern | Hunt target | Notes |
|---------|-------------|-------|
| Multi-interval outlier matrix | `hunt_core/data/universe.py` `PrescanEngine` | Internal L0 queue, **not** TG at 1s |
| Top-N digest (3h/6h) | `hunt_core/deliver/digest.py` | Advisory batch when `HUNT_EARLY_DUMP_TG=0` |
| Watchlist / blacklist | `hunt_watch/watchlist_ops.py` | Already partial |
| New listing check | P2 listing sidecar | BPA `checkNewListingEnabled` |

### Reject

- 1s Telegram spam model (violates human latency)
- No lifecycle / closed-bar confirm
- No fuel / structural triggers

---

## A3 — gatiella/binance-trading-bot (~1★, Go)

| Field | Detail |
|-------|--------|
| **Eligibility** | ✅ TG signals with Entry/SL/TP — manual operator |
| **Architecture** | Single Go binary · CCXT · 15m scan loop |
| **Funnel** | All USDT perps → filter `vol≥$1M` + `change≥3%` → ~438 coins → 4TF score → keep `score≥60%` → top 10 → one TG message |
| **TG format** | Entry zone, SL, TP1/TP2, 4TF alignment summary, 10min cooldown per symbol |
| **Latency class** | L1 (~15m scan) — closer to Hunt screener than ignition |

### Borrow → Hunt

| Pattern | Hunt target | Notes |
|---------|-------------|-------|
| Hot-coin funnel 438→10 | `universe.funnel_hot_candidates()` | After `rank_hunt_candidates` |
| 4TF scorecard footer | `mtf_confluence.format_mtf_scorecard_footer()` | Append to confirm TG |
| 10min symbol cooldown | `dump_hunt_alert` / digest cooldown | Already partial |
| Entry/SL/TP block layout | `format_entry_telegram` | Style reference only |

### Reject

- Long-bias default framing (Hunt is dump-fade primary)
- No lifecycle FSM / pump_history
- No ARMED vs TRIGGERED tiers

---

## A8 — moo-22/opencrypto (ShieldGuard)

| Field | Detail |
|-------|--------|
| **Eligibility** | ⚠️ Has position manager + auto-trade path — **formulas only** |
| **Architecture** | `shield_guard.py` → `detect_manipulation(df)` before trade open |
| **9 checks** | Volume spike · wick analysis · wash 4σ · P&D pattern · consecutive candles · taker imbalance · liq cascade · spread/gap spoofing · OBI |

### Borrow → Hunt

| Check | Hunt target | Port |
|-------|-------------|------|
| Wash 4σ vol Z | `gate/wash.py` `wash_volume_z_score()` | Gate block code `wash_trading` |
| P&D pattern (pump window + dump window) | `gate/wash.py` `pump_dump_stage()` | Lifecycle advisory label |
| Taker imbalance | `microstructure.py` existing `signed_order_flow` | Threshold in gate |
| Liq cascade | `microstructure` liq events | Advisory tier |
| OBI | `tob_imbalance` column | Gate soft flag |

### Reject

- `position_manager.py` / trailing SL auto path
- Plugin telegram as primary delivery (Hunt has own pipeline)

---

## A10 — VL-mwb/shield-regime (~2★)

| Field | Detail |
|-------|--------|
| **Eligibility** | ✅ Regime labels + kinematics — signal-oriented |
| **Architecture** | Velocity Z · acceleration Z · WTI (wash-trading index) · P&D stage FSM |
| **Formulas** | `v_z = (v - μ_v) / σ_v` on log returns · WTI from vol/price divergence · stage: accumulation → pump → distribution → dump |

### Borrow → Hunt

| Pattern | Hunt target | Notes |
|---------|-------------|-------|
| Velocity/accel Z gate | `gate/wash.py` `kinematic_block_reason()` | Block chase entries |
| WTI wash index | `wash_trading_index()` | Combined with vol Z |
| P&D stage labels | lifecycle phase hints | Map to existing 9 phases |

### Reject

- Full regime FSM replacement (Hunt lifecycle is richer)
- Auto position sizing

---

## A9 — RaySatish/Market-Surveillance-System (~3★)

| Field | Detail |
|-------|--------|
| **Eligibility** | ✅ Surveillance dashboard — no auto-trade |
| **Architecture** | Binance aggTrades → Spark windows (5m) → wash vol Z · sequential PUMP→DUMP detector → Streamlit |
| **Wash** | Rolling Z-score on trade volume per symbol per window |
| **P&D** | Consecutive windows: PUMP = price spike + vol surge; DUMP = price crash after pump window |

### Borrow → Hunt

| Pattern | Hunt target | Notes |
|---------|-------------|-------|
| Vol Z wash flag | `wash_volume_z_score()` | Same formula family as A8/A10 |
| Sequential PUMP→DUMP | `pump_dump_stage()` | Validates lifecycle transitions |
| 5m rolling windows | screener / prescan | Aligns with Hunt 5m confirm TF |

### Reject

- Spark/Kafka/HDFS stack (overkill for Hunt solo operator)
- Batch-only (not live tick path)

---

## A7 — Xeron2000/pwatch (~18★)

| Field | Detail |
|-------|--------|
| **Eligibility** | ✅ Multi-CEX velocity + vol spike → TG |
| **Architecture** | YAML config · WS multi-exchange · quality filters before alert |
| **Quality gates** | `autoModeMinQuoteVolume24h` · `MinOpenInterestUsd` · `MinListingAgeDays` · `MaxRecentVolatilityPct` |

### Borrow → Hunt

| Gate | Hunt target | Default |
|------|-------------|---------|
| Min quote vol 24h | `UniverseConfig.min_quote_volume_usd` | $10M (existing) |
| Min OI USD | `min_open_interest_usd` | $500K |
| Min listing age | `min_listing_age_days` | 7 |
| Max recent vol % | `max_recent_volatility_pct` | 80% |

### Reject

- Full pwatch WS mesh (Hunt uses CCXT Pro selectively)
- Auto-trade modes if present in fork

---

## A17 — tripolskypetr/volume-anomaly (~4★)

| Field | Detail |
|-------|--------|
| **Eligibility** | ✅ Research/detection — Hawkes + CUSUM + BOCPD on trade flow |
| **Architecture** | Trade stream → changepoint detection → anomaly score |
| **CUSUM** | Cumulative sum of standardized vol deviations; alarm when exceeds threshold h |

### Borrow → Hunt

| Pattern | Hunt target | Notes |
|---------|-------------|-------|
| CUSUM on quote vol | `scripts/research/spike_cusum.py` | JSONL offline spike |
| Hawkes intensity | P2 research | Not hot path |
| BOCPD | P2 research | Heavier than CUSUM |

### Reject

- Live CUSUM on every tick (L0 internal only per plan)
- Replacing cluster_fuel with ML changepoint

---

## Cross-repo synthesis

| Hunt gap | Best refs | Priority |
|----------|-----------|----------|
| Advisory digest | A1 | P0 |
| Outlier prescan matrix | A1, A6 | P0 |
| Wash / manipulation gate | A8, A9, A10 | P0 |
| Hot-coin funnel | A3 | P1 |
| MTF scorecard footer | A3 | P1 |
| Universe quality gates | A7 | P1 |
| CUSUM flow L0 | A17 | P2 |

**Hunt unique (keep):** closed-bar confirm, lifecycle FSM, ARMED/TRIGGERED, pump_history, cluster_fuel, signal tracker, logic_verify.
