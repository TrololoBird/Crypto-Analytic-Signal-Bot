# DEEP_FORENSIC.md

Forensic map of the Deep module (verdict_v2 spine): every computation
from data acquisition to signal generation and Telegram rendering, with
inputs, outputs, formulas, weights, block conditions, and dependencies.
**Read-only audit — no code changed.** Date: 2026-06-25.

---

## 0. Headline findings (read this first)

1. **Seven engines run, but two of them (`flow` + `execution_pressure`) call
   `_micro_nudge` independently — the same DOM features (absorption, footprint,
   iceberg, depth_imbalance) are added into BOTH engines' long/short scores.**
   Because the blender averages all engines with priority weights, DOM data
   effectively votes twice. This is the Deep module's primary double-count.

2. **Strength is an ordinal rank, not P(win).** The type literally carries
   `disclaimer = "rank only — not win probability"`. It is a composite of
   `probability_rank × 0.40 + horizon_B.conviction × 0.60`, with topology
   bonus/penalty, fragility/disagreement deductions, and a data-coverage cap.
   None of these terms are outcome-calibrated.

3. **Pattern generator has only four candidates** — `_gen_trend`,
   `_gen_mean_reversion`, `_gen_liquidity`, `_gen_distribution`. Every tick
   picks one of ~10 pattern IDs from this small set. The pattern drives the
   path type, which drives direction, which drives the trade plan. A one-of-four
   beauty contest where the highest `raw_score` wins.

4. **Engine priority weights are per-horizon and configurable, but the defaults
   are never tuned to outcomes.** Horizon A weights macro_trend 0.26, structural
   0.22; Horizon C flips to flow 0.20, execution_pressure 0.16. These numbers
   appear authoritative but have no empirical basis — they are hand-set priors.

5. **Context reconciliation (`reconcile.py`) is a post-hoc safety net, not a
   direction voter.** It checks whether DOM, band asymmetry, and liquidity
   magnets contradict the already-decided path. On `strong_conflict` the signal
   is vetoed (WAIT). It does not influence which direction was chosen — only
   whether to suppress it. This is architecturally sound but means DOM conflict
   can only block, never redirect (unless `HUNT_RECONCILE_FLIP_PATH` env is
   explicitly enabled — off by default).

6. **`signal.py` contains a parallel legacy direction-resolution system
   (`resolve_trade_direction`, `correlated_direction`) that runs OUTSIDE
   verdict_v2.** It examines `row["dump"]`, `row["long"]`, lifecycle phases,
   BTC correlation, and display-readiness scores. This code is alive (called
   by `probe_header`, `scenario_summary`, etc.) — it is NOT the verdict_v2
   decision authority, but it IS the `/signal SYM` Telegram header authority.
   Two direction systems coexist.

7. **`signal.py` also contains ~300 LOC of liquidity scenario + ~260 LOC of
   POC-level scenario ("Prizrak 7") builders.** These are display-only panels
   for the deep `/signal` command — they do NOT feed verdict_v2 direction or
   strength. They run their own weight accumulation, normalization, and
   probability ranking entirely independently.

8. **Timing gate (`timing_gate.py`) is a binary veto, not a quality signal.**
   It checks 15m/5m bar alignment with horizon C. If `require_timing_c` is
   `True` (default), a closed-bar confirmation must exist or the signal is
   WAIT. This prevents intrabar signals but also means the system can only
   emit at bar boundaries.

9. **The `path_shadow` system (off by default) is a prototype for DOM-driven
   direction reversal.** When enabled via `HUNT_RECONCILE_FLIP_PATH=1`, it
   rebuilds the entire plan/strength/reconcile pipeline for the opposite
   direction when DOM conflicts with the primary path. Currently shadow-log
   only — the flip is not production.

10. **Calibration (`calibration.py`) can auto-tune `strength_min` gate from
    observed passing signals via `suggest_gates`.** It clamps between floor
    0.40 and ceiling 0.54. This is the only feedback loop from historical
    ticks to gates — and it tunes the threshold, not the strength formula.

---

## 1. Pipeline data-flow map

```
row dict (from deep tick assembly: timeframes, market, structure, lifecycle,
          regime, maps, cross_microstructure, dump, long, btc_context)
  ↓
orchestrator.build_scenario_verdict(row)
  ├── L0: run_all_engines(row)          → 7 × EngineOutput
  ├── L0: run_data_quality(row)         → DataQualityReport
  ├── L1: blend_horizons(engines, cfg)  → A/B/C HorizonForecast
  ├── L1: build_conflict_matrix         → pairwise engine conflicts
  ├── L2: classify_topology(horizons)   → HorizonTopology (kind, coherence)
  ├── L2: classify_disagreement         → DisagreementState
  ├── L2: classify_market_context(row)  → str (bull_trend/bear_trend/range/…)
  ├── L2: extract_maturity(row)         → MaturityFeatures
  ├── L3: infer_driver (1st pass)       → MarketDriver
  ├── L3: generate_patterns(row,…)      → PatternConfidence (primary + alts)
  ├── L3: infer_driver (2nd pass)       → MarketDriver (pattern-aware)
  ├── L3: map_to_expected_path          → ExpectedPath (type, direction, move)
  ├── L4: build_trade_plan(row, path)   → TradePlan (zone, SL, TP1-3, RR)
  ├── L4: build_catalyst                → ScenarioCatalyst
  ├── L4: adjust_expected_move          → ExpectedPath (refined by plan)
  ├── L4: compute_fragility             → ScenarioFragility
  ├── L4: compute_signal_strength       → SignalStrength
  ├── L5: reconcile_context             → ReconciliationResult
  ├── L5: [path_shadow — if flip on]    → rebuilds L4 from flipped direction
  ├── L5: apply_reconcile_to_strength   → SignalStrength (×0.78 or ×0.50)
  ├── L5: compute_trade_quality         → TradeQuality
  ├── L5: assess_timing_gate            → TimingGate (binary veto)
  ├── L5: decide_signal                 → SignalDecision (LONG/SHORT/WAIT)
  └── → ScenarioVerdict (all above assembled)
        ↓
    format_pinned_signal (Telegram render)
    signal_queue (TOP-3 ranking across pinned symbols)
    evidence_trace (JSONL decomposition for validation)
```

---

## 2. Seven measurement engines (`engines.py`)

Each engine outputs `EngineOutput(long, short, conviction, blend_weight,
coverage_quality, information_value, evidence)`. Starting point: `long=0.5,
short=0.5` (prior neutral). Nudges accumulate additively.

### 2.1 `structural` (base_priority=0.18)
| input | source | nudge |
|---|---|---|
| htf_trend | `structure.htf_trend` (1w/1d derived) | bull→long+0.15, bear→short+0.15 |
| structure_bias | `structure.structure_bias` | long→+0.20, short→+0.20 |
| bos_direction | `structure.bos_direction` | bull→long+0.15, bear→short+0.15 |
| choch_detected | `structure.choch_detected` | if htf_bull: short+0.10 (counter); htf_bear: long+0.10 |

**Finding:** ChoCH nudges the **opposite** side from HTF — intentional (reversal
signal) but means one engine votes both long AND short in the same tick.

### 2.2 `positioning` (base_priority=0.22 — highest)
| input | source | nudge |
|---|---|---|
| upward targets | `collect_upward_targets(row, price)` | up_pct/(up+down) × 0.35 → short |
| downward targets | `collect_downward_targets(row, price)` | down_pct/(up+down) × 0.35 → long |
| POC position | regime.poc_1h or structure.key_levels.poc | below→long+0.12, above→short+0.08 |
| liq_magnet | market.liq_magnet_pull_{short,long}_pct | stronger side→+0.10 |

**Finding:** Asymmetric POC nudge (long+0.12 vs short+0.08). The `up/down_pct`
reward fraction nudges SHORT when upside is large — reward-asymmetry signal.
`upside_reward_pct` / `downside_reward_pct` are stored in EngineOutput and
consumed by reconcile `_band_conflicts`.

### 2.3 `macro_trend` (base_priority=0.18)
Reads `timeframes["1w"]`, `["1d"]`, `["4h"]` with weights 0.35, 0.35, 0.30.
Each TF snapshot → `trend_from_snapshot` → long/short scores via ADX-scaled
strength. Weighted average.

**Backtestable: ✅** (kline-derived trend + ADX).

### 2.4 `derivatives` (base_priority=0.15)
| input | formula | nudge |
|---|---|---|
| funding_zscore_48h | robust-z of funding rate | contrarian: high→short, low→long (max 0.25, scaled /8) |
| oi_z + oi_chg_1h | OI z-score + hourly change | crowded longs: short+0.12; OI drop: short+0.06 |
| top_vs_global_ls_gap | long/short ratio gap | \|gap\|>0.05: positive→short+0.08, neg→long+0.08 |
| premium_zscore_5m | basis z-score | \|z\|>0.5: pos→short+0.06, neg→long+0.06 |

**Backtestable:** funding ✅, OI ⚠️ (~30d), ls_ratio ⚠️, premium ❌ (5m depth).

### 2.5 `flow` (base_priority=0.15)
| input | formula |
|---|---|
| taker 5m/15m/1h | accel = (t5−t15) + (t15−t1h)×0.5; nudge min(0.2, \|accel\|) |
| agg_trade_delta | ≠0: nudge 0.08 |
| **`_micro_nudge`** | DOM features (see §2.8) |

### 2.6 `execution_pressure` (base_priority=0.12)
| input | formula |
|---|---|
| depth_imbalance / map_book_imbalance_1pct | nudge min(0.15, \|imb\|) |
| microprice_bias | nudge 0.08 |
| microstructure_by_direction.bias_score | nudge score×0.15 |
| **`_micro_nudge`** | DOM features (see §2.8) — **SAME call as flow** |

### 2.7 `cross_consensus` (base_priority=0.14)
Cross-venue taker consensus + cross book wall depth_imbalance. Low priority.

### 2.8 `_micro_nudge` — **shared by flow + execution_pressure** ⚠️
| DOM feature | nudge | backtestable? |
|---|---|---|
| map_cvd_divergence (bullish/bearish) | +0.10 | ❌ |
| map_absorption_count + map_accum_bid_absorption | long + min(0.12, 0.04×count) | ❌ |
| map_ask_thinning | long + 0.06 | ❌ |
| map_footprint_delta | signed min(0.10, \|Δ\|×0.08) | ❌ |
| iceberg + sticky_wall ≥ 2 | min(0.08, n×0.02) by depth_imbalance sign | ❌ |
| map_void_count | evidence only (no score nudge) | ❌ |

**CRITICAL FINDING:** `_micro_nudge` is called in BOTH `run_flow` (line 299) and
`run_execution_pressure` (line 340). The same DOM features vote in two engines.
With default priorities flow=0.15 + exec=0.12, DOM gets 0.27 of the 1.0 weight
budget — more than any single engine. Double-counting by construction.

---

## 3. Horizon blending (`blender.py`)

```
blend_weight_eff = base_priority × max(coverage_quality × horizon_decay, 0.05)
long_blend = Σ(engine.long × eff) / Σ(eff)      # coverage-weighted average
```

| horizon | decay | default priorities (descending) |
|---|---|---|
| A (~8h) | 1.00 | macro_trend 0.26, structural 0.22, positioning 0.20, derivatives 0.14, flow 0.10, exec 0.04, cross 0.04 |
| B (~18h) | 0.85 | positioning 0.20, macro_trend 0.16, structural 0.16, derivatives 0.14, flow 0.14, exec 0.08, cross 0.08 |
| C (~36h) | 0.70 | flow 0.20, positioning 0.18, exec 0.16, cross 0.14, derivatives 0.10, structural 0.08, macro_trend 0.06 |

**Finding:** Horizon C (long-term) weights flow+exec the highest (0.36 total),
which are the engines with the DOM double-count. DOM influence grows at longer
horizons — where DOM data is least informative. Temporal decay partially offsets
this via reduced coverage_quality, but the priority allocation is inverted
relative to data shelf-life.

**Finding:** `blend_weight` in EngineOutput = `base_priority × max(cov, 0.01)`.
This weight is NOT used by the blender — the blender uses its own per-horizon
priorities. The EngineOutput.blend_weight is consumed only by
`build_factor_contributions` for display. Another computed-but-not-deciding
field.

---

## 4. Topology & disagreement

### 4.1 `classify_topology` — A/B/C horizon alignment
| kind | condition |
|---|---|
| aligned_trend | A=B=C, all same non-neutral direction |
| bull_pullback | A=long, B=neutral or short |
| bear_rally | A=short, B=neutral or long |
| compression | A and B conviction < 0.12 |
| reversal_candidate | A ≠ C, both non-neutral |
| mixed | fallthrough |

Coherence = 0.35 + aligned_count×0.20 + B.conviction×0.30.

### 4.2 `classify_disagreement`
Score = mean of pairwise conflict matrix values. States: consensus (<0.15),
divergence (≥threshold 0.65), transition, compression, expansion, exhaustion.

---

## 5. Pattern → path → direction

### 5.1 Four pattern generators
| generator | id(s) | score range | key input |
|---|---|---|---|
| `_gen_trend` | trend_continuation / bear_continuation | 0.45–0.80 | topology.kind, structure_bias, maturity |
| `_gen_mean_reversion` | range_bound | 0.35–0.75 | topology=compression, POC distance |
| `_gen_liquidity` | long_squeeze / short_squeeze / liquidity_sweep | 0.30–0.65 | liq_magnet_pull_{short,long}_pct |
| `_gen_distribution` | distribution | 0.32–0.69 | context=bull_distribution, funding_z |

**Finding:** Only four generators. The primary pattern drives `path_type` via a
static map (e.g., `trend_continuation` → `continuation_up`). Direction is derived
mechanically from path type suffix (`_up` → long, `_down` → short). This means
direction is decided by whichever of 4 generators scores highest — NOT directly
by the seven engines. The engines influence direction only indirectly through
topology (which biases `_gen_trend`).

### 5.2 Driver-based filtering
`filter_patterns_by_driver` narrows candidates to those whitelisted for the
inferred driver. If all are filtered, full list is restored. This can suppress
viable patterns based on keyword evidence matching (e.g., `htf_` triggers
`trend_driven`, which excludes squeeze/sweep).

### 5.3 `map_to_expected_path`
- Direction from path type suffix
- Move bounds from `collect_upward/downward_targets(row, price)` — if targets
  exist, else ATR×0.8 to ATR×2.5 fallback
- Time bounds from path type (squeeze 4–18h, pullback 6–36h, default 8–72h)
- `probability_rank` = primary.raw_score + topology.coherence×0.2 − 0.1 if ambiguous

---

## 6. Trade plan (`trade_plan.py` + `plan.py` + `levels.py`)

### 6.1 Level sources (`levels.py`)
`canonical_levels(row, direction)` — single level set from:
- structure: `key_levels.support/resistance`, `last_swing_low/high`
- pools: `nearest_above/below`
- regime: `poc_1h`
- market: `map_vp_poc`, `map_vp_val/vah`

### 6.2 Entry zone
Anchored to structural reference (POC, support/resistance), padded by
`cfg.entry_atr_pad` (default 0.25 ATR). Different logic for long (pullback
below price) vs short (rally zone above price).

### 6.3 Stop
`pick_stop`: beyond catalyst level by buffer (`_STOP_BUFFER_ATR = 0.35`).
Fallback: structure level with buffer, or ATR×1.5.

### 6.4 Targets
`pick_targets` → `collect_{upward,downward}_targets(row, price)`. If empty,
ATR multiples (2, 3.5, 5). Filtered: long targets must be > zone_mid×1.0005;
short < zone_mid×0.9995. Padded to 3.

### 6.5 Geometry finalization (`plan.py::finalize_plan_geometry`)
- SL must be outside zone (auto-pushed if inside)
- TPs sorted nearest→farthest
- Monotonic RR enforced
- Zone width clamped to ATR×1.8 or price×1.8%
- R:R computed from **zone midpoint** (not worst edge)
- RR clamp: [0.3, 10.0]

### 6.6 Activation lifecycle
`assess_activation(row, summary)`: idle → near_entry/near_catalyst →
in_entry_zone/at_catalyst. When `plan_lifecycle = "active"` and price > 0,
R:R is recomputed from actual price as fill reference.

---

## 7. Signal strength (`signal_strength.py`)

```python
base = probability_rank × 0.40 + horizon_B.conviction × 0.60
# topology delta:
+0.07 aligned_trend, +0.04 bull_pullback/bear_rally, −0.03 compression
# penalties:
−fragility.score × 0.12
−disagree.score × 0.08
# data coverage cap:
if coverage < 0.55: base *= coverage / 0.55
# XAU/XAG override:
if coverage < 0.65 and probability_rank >= 0.50: base /= 0.90
# reconcile multiplier:
×0.78 mild_conflict, ×0.50 strong_conflict
# labels:
≥0.72 strong, ≥0.52 moderate, else weak
```

**Finding:** The 0.40/0.60 blend between `probability_rank` (pattern-driven)
and `horizon_B.conviction` (engine-driven) is the deepest coupling between
the two subsystems. Neither term is outcome-anchored.

---

## 8. Signal decision gate sequence (`signal_decision.py`)

Evaluated in this order (first failure → WAIT):

| gate | condition | notes |
|---|---|---|
| mid_leg | phase=mid + leg_gain≥8% | late-chase context block (env-gated) |
| context_conflict | reconcile=strong_conflict | DOM/liq/band contradiction |
| path_neutral | direction=neutral or path=range | no directional signal |
| timing_c | require_timing_c + !timing.ready | closed-bar gate (env/config) |
| strength | score < strength_min (0.50) | |
| fragility | score > fragility_max (0.65) | |
| coverage | data_coverage < 0.50 | |
| catalyst | confidence < 0.35 | |
| no_plan | plan is None | |
| plan_geometry | !plan_geometry_valid | |
| rr_primary | RR < rr_primary_min (0.75) | |

All gates must pass → action = path.direction (LONG or SHORT).

**Finding:** `trade_quality` is computed and displayed but **NOT a gate** —
`trade_quality_min` exists in config but is never checked in `decide_signal`.
Dead gate configuration.

---

## 9. Context reconciliation (`reconcile.py`)

Post-decision safety check against DOM, band asymmetry, liquidity magnets, POC.

| check | fires when | level |
|---|---|---|
| dom_buyers_vs_short | depth_imbalance > 0.15 on SHORT signal | mild; >0.28 strong |
| dom_sellers_vs_long | depth_imbalance < −0.15 on LONG signal | mild; <−0.28 strong |
| upside_band_vs_short | positioning.up_share > down_share + 0.35 | mild (strong if +DOM) |
| downside_band_vs_long | positioning.down_share > up_share + 0.35 | mild (strong if +DOM) |
| liq_magnet_above/below_stop | realized liquidation cluster beyond SL | strong |
| poc_cite_mismatch | pattern cites a POC direction that conflicts | mild |

`strong_conflict` → WAIT (via decide_signal early exit).
`mild_conflict` → strength ×0.78, trade_quality verdict cap.

---

## 10. Signal queue (`signal_queue.py`)

Global TOP-N ranking across pinned symbols.

```
opportunity_score = strength×0.45 + rr_norm×0.22 + (1−fragility)×0.18 + tq_score×0.15
  + 0.12 if action in {long, short}
  + 0.08 if in_entry_zone/at_catalyst
```

Queue entries have a TTL (default 2.5h). Registry tracks lifecycle transitions
(waiting → active = "promoted").

---

## 11. Telegram rendering (`format_pinned_signal.py`)

Renders: action badge, scenario (path type + narrative), catalyst, entry zone /
SL / TP levels with R:R, expected move/time, strength/fragility/trade_quality
labels, reconcile caveats, activation state, alt paths, verbose mode (patterns,
driver, topology).

**WAIT signals:** "НЕТ СДЕЛКИ" header, gate diagnostics, softened narrative,
advisory-only levels (only if trade_quality="favorable"), no activation block.

**Finding:** Narrative softening (`_use_soft_narrative`) weakens
flow-heavy pattern labels (distribution → "локальное сопротивление") when
action=WAIT or strength is weak. This is display-appropriate but means the
same underlying pattern is described differently depending on strength.

---

## 12. Parallel systems in `signal.py` (NOT verdict_v2)

`signal.py` (1,627 LOC) contains:

1. **`resolve_trade_direction`** — lifecycle-first direction picker. Checks
   recommended_bias, lifecycle phase, structure_bias, BTC correlation with
   tiered thresholds, then flip-on-geo-block logic. This is the authority for
   `/signal SYM` header (`probe_header`) and `scenario_summary` — NOT for
   verdict_v2 signals. Two coexisting direction systems.

2. **`build_liquidity_scenarios`** (LOC 763–1037) — six scenario paths
   (sweep_support_to_poc, breakdown, sweep_resistance, breakout, range_poc,
   continuation_htf). Accumulates raw weights from near_support/resistance,
   POC position, VA position, wall sizes, cascade risk, sticky walls, stacked
   imbalance. Normalizes to probabilities. Display-only panel.

3. **`build_poc_level_scenarios`** (LOC 1350–1557) — "Prizrak 7" patterns
   (reaction, base_shift, trap_flip, break_retest, base_on_break, grind_weak,
   saw). Inputs: POC distance, prokol detection, closes_beyond level, lifecycle
   phase, PP (переприор) signals, chart pattern signals. Display-only panel.

These systems share input data (row fields) with verdict_v2 but have zero
coupling to the signal decision — they are presentation modules.

---

## 13. Data quality and coverage

`run_data_quality` checks 7 groups:
funding_oi, ls_ratios, maps_liq, maps_vp, orderbook, tf_htf, cross_ms.
Score = present / 7. Used by:
- `compute_signal_strength`: coverage < 0.55 caps strength
- `decide_signal`: coverage < 0.50 → WAIT

Missing groups are listed but there is no per-group weighting — each group
contributes 1/7 regardless of its predictive value.

---

## 14. Summary of unused / duplicate / zero-influence features

| finding | location | status |
|---|---|---|
| **`_micro_nudge` double-call** | `run_flow` + `run_execution_pressure` | DOM votes in 2 of 7 engines |
| **`EngineOutput.blend_weight`** | computed as base_priority × cov | NOT used by blender — display only |
| **`trade_quality_min` config** | `SignalGates.trade_quality_min = 0.45` | config exists, never checked in `decide_signal` |
| **`information_value`** | computed per engine | stored in EngineOutput, never read by any downstream |
| **`map_void_count`** | `_micro_nudge` | appends evidence string only — no score nudge |
| **`liq_magnet_pull_*_pct`** used by both `run_positioning` and `_gen_liquidity` | two systems | same data influences direction via positioning engine AND pattern selection |
| **`ChoCH` counter-nudge** | `run_structural` | nudges short when htf_bull, long when htf_bear — same engine can vote both sides |
| **Parallel direction systems** | verdict_v2 path_mapper vs signal.resolve_trade_direction | verdict_v2 controls signal delivery; signal.py controls `/signal` TG header |
| **Liquidity/POC scenario panels** | signal.py | computed every deep tick, never feed verdict_v2 |

---

## 15. Backtestability boundary (by engine)

| engine | backtestable? | notes |
|---|---|---|
| structural | ✅ mostly | structure_bias, BOS, ChoCH from klines |
| positioning | ⚠️ partial | targets from kline levels ✅; POC/VAL/VAH from VP ❌; liq pools ❌ |
| macro_trend | ✅ | 1w/1d/4h kline trends + ADX |
| derivatives | ⚠️ | funding ✅ (long hist); OI/LS ~30d; premium 5m ❌ |
| flow | ❌ mostly | taker ratios from WS ❌; CVD ❌; all `_micro_nudge` ❌ |
| execution_pressure | ❌ | depth_imbalance, microprice, maps — all orderbook |
| cross_consensus | ❌ | cross-venue taker + book |

---

## 16. Candidate issues for follow-up (ranked)

1. **`_micro_nudge` double-count** (§2.8) — same DOM features in flow +
   execution_pressure. Either deduplicate (run `_micro_nudge` only in one
   engine) or reduce its effective weight.

2. **Outcome-calibrate strength** — currently a composite of uncalibrated
   terms. Once outcome tracker has data, map strength deciles to realized
   win rate and adjust the 0.40/0.60 blend or the topology/fragility
   penalty coefficients.

3. **Dead `trade_quality_min` gate** (§8) — decide whether to wire it or
   remove the config field.

4. **Dead `information_value` field** (§14) — computed per engine, stored,
   never consumed. Either use it (e.g., weight engines by info_value) or
   delete.

5. **Horizon C priority inversion** (§3) — long-range horizon weights
   DOM-heavy engines highest, but DOM data has the shortest shelf life.
   Consider reducing flow + exec_pressure priority in `priorities_c`.

6. **Two direction systems** (§12) — verdict_v2 and `signal.py` can disagree.
   The user sees the signal.py header on `/signal SYM` but the verdict_v2
   action on the automated pinned signal. Potential confusion source.

7. **Pattern generator narrowness** (§5.1) — only four generators produce
   all pattern candidates. Missing: breakout (non-squeeze), reversal,
   momentum-acceleration. The driver whitelist further constrains this.

---

## Phase-2 (remaining depth, not yet mapped to constants)

- `structural_forecast.py`: up/down forecast bands + confidence — display panel,
  no gate authority.
- `forecast_panel.py`: Telegram rendering of structural forecasts.
- `fusion_panel.py`: display of manipulation_fusion archetype scores (Scanner
  crossover into deep — display only).
- `arbiter.py`: deep delivery arbitration (signal dedup, cooldown).
- `features/maturity.py`: trend maturity extraction.
- `serialize.py`: JSONL roundtrip for verdict_v2.
- `conviction.py`, `telegram.py`, `format_telegram.py`: outer shell wrappers.

After this, comparison between Scanner and Deep forensics → architectural
synthesis.
