# DEEP_FORENSIC.md — Forensic map of Verdict V2

Date: 2026-06-24
Source: full code audit of `hunt/hunt_core/deep/verdict_v2/` (33 files, ~4,196 LOC)

---

## 0. The headline findings

1. **Direction and strength come from different subsystems.**
   The operator sees 7 engines (structural, positioning, macro_trend, derivatives,
   flow, execution_pressure, cross_consensus) and assumes they DRIVE the direction.
   In reality, the direction is decided by **pattern generators** (4 heuristics in
   `patterns.py`), and the 7 engines only affect **signal strength** (via horizon
   blending). This is the Deep equivalent of the Scanner dual-system problem.

2. **`_gen_distribution` has hardcoded "short" direction.**
   `patterns.py:93-94`: the distribution pattern starts with `direction: str = "short"`
   and score=0.32. If funding_z > 1.0 (+0.15) or context is bull_distribution (+0.22),
   it can reach 0.54-0.69 and win the pattern competition — forcing SHORT regardless
   of what the engines say.

3. **No temporal decay in horizon blending.**
   The same engine outputs feed into horizons A (near, 8-72h), B (medium), and C (far).
   A depth_imbalance that appeared 30 seconds ago has the same weight in the 36-hour
   forecast as in the immediate one. The `flow` engine uses `taker_5m` and
   `taker_15m` — sub-hour data driving multi-hour predictions.

4. **`dominant_side` margin (0.08) can amplify noise.**
   If blended long=0.54, short=0.46 (difference 0.08), `dominant_side` returns "short"
   categorically. This is a binary decision on a margin that's within rounding noise
   of the 0.5/0.5 baseline.

---

## 1. Pipeline data-flow map

```
deep_pinned_loop (15m cycle)
  → assemble_deep_tick (REST + snapshot)
    → ensure_verdict_v2
      → build_scenario_verdict (orchestrator.py:27)
          [L0] run_all_engines → 7 × EngineOutput
          [L1] blend_horizons → A/B/C forecasts
          [L2] classify_topology → aligned/mixed/compression...
          [L3] classify_disagreement → consensus/divergence...
          [L4] generate_patterns → PatternConfidence  ← DIRECTION
          [L5] map_to_expected_path → ExpectedPath     ← DIRECTION + expected move
          [L6] build_catalyst, trade_plan, fragility, strength
          [L7] reconcile_context → DOM/band/POC conflicts
          [L8] path_shadow → optional direction flip (off by default)
          [L9] decide_signal → LONG/SHORT/WAIT
```

---

## 2. The 7 engines (`engines.py`)

Each engine starts from **long=0.5, short=0.5** and adds small deltas from its inputs.

| Engine | Weight A | Weight B | Weight C | Directional inputs | Delta range |
|--------|----------|----------|----------|--------------------|-------------|
| macro_trend | 0.26 | 0.16 | 0.06 | 1W/1D/4h trend via `trend_scores_from_snap` | ±0.14–0.40 |
| structural | 0.22 | 0.16 | 0.08 | HTF trend, structure_bias, BOS, CHoCH | ±0.10–0.20 |
| positioning | 0.20 | 0.20 | 0.18 | Target asymmetry, POC, liq magnets | ±0.08–0.35 |
| derivatives | 0.14 | 0.14 | 0.10 | Funding z-score, OI z-score, LS gap, premium | ±0.06–0.25 |
| flow | 0.10 | 0.14 | 0.20 | Taker 5m/15m/1h accel, agg_trade_delta, CVD | ±0.02–0.20 |
| execution_pressure | 0.04 | 0.08 | 0.16 | DOM imbalance, microprice, microstructure | ±0.06–0.15 |
| cross_consensus | 0.04 | 0.08 | 0.14 | Cross-venue taker flow, book walls | ±0.08–0.18 |

**Critical observation:** `execution_pressure` (which reads DOM imbalance — the buyer/seller
ratio) has weight **0.04 in horizon A** and only reaches 0.16 in horizon C. The operator
sees "buyers advantage in DOM" but this barely affects the near-term forecast.

**All engines have `coverage_quality` multiplier** in `_blend()`. If an engine's input
columns are missing from the row, its effective weight drops. Missing orderbook data
(DOM, microprice) → `execution_pressure` and `cross_consensus` approach zero effective
weight → even their small priority disappears.

---

## 3. Direction comes from PATTERNS, not engines

`patterns.py:118-150` runs 4 generators:

### `_gen_trend` (score baseline 0.45, bias +0.1, topo +0.2-0.25)
```
topology = aligned_trend → direction = a_dominant
topology = bull_pullback → direction = "long"
topology = bear_rally → direction = "short"
structure_bias → direction override
```

### `_gen_mean_reversion` (score baseline 0.35)
```
compression → +0.25
far_from_POC → +0.15, direction = towards POC
```

### `_gen_liquidity` (score baseline 0.30)
```
liq_magnet_pull_short_pct > liq_magnet_pull_long_pct → direction = "short"
liq_magnet_pull_long_pct > 0.5 → direction = "long"
direction determines pid = long_squeeze/short_squeeze/liquidity_sweep
```

### `_gen_distribution` (score baseline 0.32, direction="short" HARDCODED)
```
direction: str = "short"  ← patterns.py:93-94
bull_distribution context → +0.22
funding_z > 1.0 → +0.15
```

The winner is the pattern with highest `raw_score`. Its ID is mapped to a path type
via `_PATTERN_PATH` dict in `path_mapper.py:19-33`:

```python
"distribution" → "pullback_down"   → direction = "short"
"long_squeeze" → "squeeze_down"    → direction = "short"
"short_squeeze" → "squeeze_up"     → direction = "long"
"trend_continuation" → "continuation_up" → direction = "long"
"bear_continuation" → "continuation_down" → direction = "short"
"accumulation" → "pullback_up"     → direction = "long"
```

**The 7 engines have ZERO influence on direction.** They only affect strength
(via horizon B conviction → `signal_strength`).

---

## 4. How SHORT happens at neutral TFs with buyer DOM

Scenario: 1W, 1D, 15M all neutral, DOM shows buyer advantage.

1. `macro_trend` engine: all TF neutral → long=0.5, short=0.5 → **no signal**
2. `structural` engine: no BOS, CHoCH, structure_bias → **0.5/0.5**
3. `execution_pressure` engine: DOM buyer advantage → **long=0.62, short=0.5**
4. Horizon A blend: long ≈ 0.51, short ≈ 0.50 → **dominant_side = neutral**

5. **Pattern competition:**
   - `_gen_trend`: topology="mixed" (neutral), bias="wait" → score = **0.45**, direction = neutral
   - `_gen_distribution`: if funding_z > 1.0 → score **0.47**, direction = **"short"** (hardcoded)
   - `_gen_mean_reversion`: no compression, close to POC → score **0.35**
   - `_gen_liquidity`: weak liq magnets → score **~0.30**

6. **Winner: `_gen_distribution` at 0.47 vs `_gen_trend` at 0.45** → difference 0.02
   → path = `pullback_down` → direction = **SHORT**

7. **Reconcile:**
   - If DOM buyer advantage < 0.28 (strong threshold) → mild_conflict → strength × 0.78
   - If DOM buyer advantage < 0.15 (conflict threshold) → no conflict → pass
   - Signal passes with strength reduced by reconcile multiplier

8. **Result: SHORT signal despite 0/3 neutral TFs and buyer DOM advantage.**

The entire SHORT determination rests on a **0.02 pattern score difference** and a
**hardcoded "short" in `_gen_distribution`**.

---

## 5. Key findings

### P0 — Direction from patterns, strength from engines

Система разделяет direction и strength, но оператор этого не видит.

| | Direction | Strength |
|---|---|---|
| **Source** | `patterns.py` (4 generators) | `engines.py` (7 engines → horizon blend) |
| **What operator sees** | — | book, flow, funding, structure |
| **What actually decides** | highest `raw_score` among 4 patterns | `signal_decision.py` checks strength ≥ 0.50 |
| **Validation** | Heuristic only (0.32-0.75 baseline) | Heuristic only (0.5 baseline + small deltas) |

**P0 — `_gen_distribution` hardcoded SHORT direction**

`patterns.py:93-94`:
```python
direction: str = "short"
```

The distribution pattern ALWAYS votes short. When neutral TFs produce weak trend and
liquidity patterns (scores 0.30-0.45), distribution can win at 0.47 with just
funding_z > 1.0. **No engine output can challenge this direction.**

### P0 — No temporal decay by horizon

`blender.py:31-49`: all three horizons (A/B/C) use the same engine outputs. The
`_blend()` function applies the same `eng.long` and `eng.short` values to all
horizons, only varying the priority weights. A `depth_imbalance` from 30 seconds ago
has the same value in the 36-hour forecast as in the 8-hour forecast.

The `timing_gate.py` adds a 15m/5m check at the end, but this only delays WAIT until
the bar closes — it doesn't decay the engine values.

### P1 — `dominant_side` margin amplifies noise

`_helpers.py:28-43`:
```python
def dominant_side(long, short, margin=0.08, weak_margin=0.04):
    if long >= short + margin: return "long"
    if short >= long + margin: return "short"
    if long >= short + weak_margin: return "weak_long"
    if short >= long + weak_margin: return "weak_short"
    return "neutral"
```

With 7 engines at ~0.5 baseline, a 0.08 difference is a 4-5% shift from baseline.
This converts a tiny statistical fluctuation into a categorical "short" decision.

### P1 — `reconcile.py:229` uses `or` for DOM imbalance

```python
imb = safe_float(market.get("depth_imbalance") or market.get("map_book_imbalance_1pct"))
```

If `depth_imbalance` is 0.0 (balanced book, no conflict), `safe_float` returns 0.0
which is falsy → falls through to `map_book_imbalance_1pct`. If both are 0.0 → `imb=0`
→ no conflict detected. But a balanced book should mean "no conflict", not "no data"
— the current code works coincidentally but is fragile. If one value is 0.0 and the
other is missing, the `or` logic silently gives wrong results.

More importantly: if `depth_imbalance` is missing (None) but `map_book_imbalance_1pct`
is 0.0 (fetched but balanced), `imb=0.0` → no conflict. Correct behavior, but the
logic is fragile.

### P1 — Pattern baselines are inconsistent

| Pattern | Baseline | Max possible | Direction |
|---------|----------|-------------|-----------|
| `_gen_trend` | 0.45 | ~0.75 | from topology/bias |
| `_gen_mean_reversion` | 0.35 | ~0.75 | towards POC |
| `_gen_liquidity` | 0.30 | ~0.65 | from liq magnet |
| `_gen_distribution` | 0.32 | ~0.69 | **"short" hardcoded** |

The 0.45 vs 0.32 baseline gap means `_gen_trend` has a structural advantage even when
trend is absent. With neutral TFs, `_gen_trend` at 0.45 usually wins unless specific
conditions fire for other patterns. This is a hidden bias towards whatever the trend
pattern says.

### P2 — `SignalStrength` explicitly says "not win probability"

`types.py:161`:
```python
disclaimer: str = "rank only — not win probability"
```

Unlike Scanner's misleading `p_win`, Deep correctly labels strength as a rank. This
is honest.

### P2 — `path_shadow.py:34-39`: direction flip off by default

```python
def reconcile_flip_path_enabled():
    return os.getenv("HUNT_RECONCILE_FLIP_PATH", "").strip().lower() in {"1", "true", "yes", "on"}
```

The shadow path (which would flip direction when DOM conflicts with primary path) is
**off by default** and only logs. The reconcile warns about DOM conflict but doesn't
change the direction.

### P2 — `timing_gate.py` 15m check is permissive

`timing_gate.py:48-55`:
```python
if t15 != "bear" and rsi15 < 55:
    evidence.append("15m_not_bear")
    return True  # ← confirmed even though 15m is not aligned
```

For a SHORT path, the 15m check confirms if:
- 15m trend is bear (= aligned) OR
- 15m is NOT bull AND rsi < 55 (= not strongly counter) OR
- 15m ADX < 22 (= weak/no trend)

This means SHORT signals can pass even when 15m is neutral or weakly bullish.
The timing gate is not a hard filter.

---

## 6. Comparison with Scanner

| Aspect | Scanner | Deep |
|--------|---------|------|
| **Dual systems** | P0: two fusion engines, one decides, one displays | P0: patterns decide direction, engines decide strength |
| **p_win** | Mislabeled: `magnitude × 0.25` | Honest: `disclaimer = "rank only"` |
| **Direction source** | `median(book, flow, structure, funding)` | `patterns.py` winner (4 heuristics) |
| **Strength source** | Same as direction (magnitude) | `engines.py` blended → `signal_strength.py` |
| **Self-referential gate** | q90 of own magnitude history | `signal_decision.py` fixed thresholds (0.50 strength, 0.35 catalyst) |
| **Temporal decay** | Windowed by lookback (240 bars) | None — same values for all 3 horizons |
| **DOM conflict handling** | N/A | reconcile blocks strong conflict, mild reduces strength |

---

## 7. How to verify

1. **Direction source isolation test:**
   Collect 100 deep signals. For each, log:
   - The winning pattern and its raw_score
   - The top engine's direction votes
   - The final path direction
   Expected: when engines vote long but pattern votes short → final is short.
   This proves direction comes from patterns, not engines.

2. **`_gen_distribution` SHORT bias test:**
   Collect signals where `_gen_distribution` won. Expected: 100% have
   `direction == "short"` (true by design).

3. **Temporal horizon exposure test:**
   For 50 ticks, log each engine's values and the 3 horizon forecasts.
   Expected: A, B, C horizons use identical long/short values, only weights differ.
   This confirms no temporal decay.

4. **Pattern baseline bias test:**
   Run on 100 neutral-TF ticks. Expected: `_gen_trend` wins >60% of the time
   (due to 0.45 vs 0.32 baseline advantage), even on genuinely trendless data.

---

## 8. What it means for the user's question

> "Why does Deep short BTC at neutral TFs with buyer DOM?"

Tracing through the pipeline:

1. `macro_trend` and `structural` engines → 0.5/0.5 (neutral TFs = no signal)
2. `execution_pressure` → 0.62/0.50 (buyer DOM → long)
3. But this engine has weight 0.04 in horizon A → **negligible influence on blend**
4. **Pattern competition:**
   - `_gen_trend` at 0.45 (neutral topology, no bias)
   - `_gen_distribution` at 0.47 (funding_z > 1.0 → SHORT direction)
   - Score difference: 0.02 → `_gen_distribution` wins
5. Path: `pullback_down` → direction = **SHORT**
6. Reconcile: DOM buyer advantage < 0.28 → mild_conflict → strength × 0.78
7. All gates pass → **SHORT signal**

The SHORT is driven by `_gen_distribution`'s hardcoded "short" direction combined
with funding_z > 1.0, not by any engine signal. The DOM buyer advantage is visible
in `execution_pressure` but has weight 0.04 — it barely affects anything.

---

## Summary

| ID | Severity | File | Issue |
|----|----------|------|-------|
| P0.1 | Direction | `patterns.py` + `engines.py` | Direction from patterns, strength from engines — different systems |
| P0.2 | Direction | `patterns.py:93-94` | `_gen_distribution` hardcoded "short" direction |
| P0.3 | Temporal | `blender.py:27-50` | No temporal decay between horizon A/B/C |
| P1.1 | Threshold | `_helpers.py:28-43` | `dominant_side` margin (0.08) amplifies noise to category |
| P1.2 | Data | `reconcile.py:229` | `or` operator for DOM imbalance — correct accidentally |
| P1.3 | Baseline | `patterns.py` | Inconsistent pattern baselines (0.45 vs 0.35 vs 0.30 vs 0.32) |
| P2.1 | Honesty | `types.py:161` | SignalStrength correctly labeled as rank |
| P2.2 | Gate | `path_shadow.py:34-39` | DOM flip off by default, only logs |
| P2.3 | Gate | `timing_gate.py:48-55` | 15m confirmation is permissive |
