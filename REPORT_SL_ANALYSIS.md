# SL Analysis Report — 2026-06-05

**Database:** `data/bot/bot.db`  
**Analyst:** automated diagnostic (D1–D6)  
**Scope:** 44 rows in `signal_outcomes`, 76 rows in `active_signals`  
**Status:** Pre-fix baseline — recommendations listed, not implemented.

---

## Methodology notes

The prompt queries referenced `sl_hit` and `active_signals.features`. The live schema differs:

| Prompt assumption | Actual schema |
|-------------------|---------------|
| `result = 'sl_hit'` | `stop_loss`, `breakeven_stop`, `trailing_stop` |
| `active_signals.features` | `signal_outcomes.features` (JSON) |
| `time_to_sl_min` | Derived: `time_to_exit_min - time_to_entry_min` |
| TP comparison (D4) | **0 TP outcomes** in `signal_outcomes` (no `tp1_hit` / `tp2_hit` rows) |

All statistics below use the corrected schema. SL counts include `stop_loss` + `breakeven_stop` + `trailing_stop` (n=10).

**Sample-size warning:** n=10 executed SL outcomes is too small for high-confidence strategy-level conclusions. Treat strategy and symbol breakdowns as directional only.

---

## Overall statistics

### Outcome distribution (all 44 rows)

| Result | Count | % |
|--------|------:|--:|
| expired_pending | 18 | 40.9% |
| expired_active | 15 | 34.1% |
| stop_loss | 9 | 20.5% |
| breakeven_stop | 1 | 2.3% |
| unactivated_close | 1 | 2.3% |

### Executed vs expired

| Metric | Value |
|--------|------:|
| Total outcomes | 44 |
| SL outcomes (stop + BE + trailing) | 10 |
| TP outcomes (`tp1_hit`, `tp2_hit`, `tp3_hit`) | **0** |
| Expired (pending + active) | 33 (75.0%) |
| SL rate among executed exits | **100%** (10/10) |
| `tracking_stats`: signals_sent / activated / tp1 / stop_loss / expired | 76 / 49 / 2 / 10 / 33 |

Two TP1 touches exist in `active_signals.tp1_hit_at` (RENDERUSDT expired after TP1; ADAUSDT breakeven_stop after TP1), but neither closed as a TP outcome row — pipeline ends in SL/expired, not TP wins.

### Direction summary

| Direction | Total outcomes | SL count | SL % |
|-----------|-------------:|---------:|-----:|
| short | 42 | 10 | 23.8% |
| long | 2 | 0 | 0.0% |

All 10 SL hits are **short**. No long SL in this dataset.

### Timeframe

| TF | Total | SL | SL % | Avg PnL % |
|----|------:|---:|-----:|----------:|
| 15m | 43 | 10 | 23.3% | -0.498 |
| 15m+1h | 1 | 0 | 0.0% | 0.0 |

---

## Strategy breakdown

### Strategies with ≥5 outcomes

| Strategy | Total | SL | SL % | Avg PnL % | Avg R |
|----------|------:|---:|-----:|----------:|------:|
| whale_walls | 21 | 4 | 19.0% | -0.633 | -0.19 |
| spread_strategy | 12 | 2 | 16.7% | -0.310 | -0.17 |

### All strategies (full table)

| Strategy | Total | SL | Expired | Avg score |
|----------|------:|---:|--------:|----------:|
| whale_walls | 21 | 4 | 17 | 0.634 |
| spread_strategy | 12 | 2 | 10 | 0.660 |
| aggression_shift | 3 | 1 | 2 | 0.687 |
| btc_correlation | 2 | 2 | 0 | 0.663 |
| depth_imbalance | 2 | 1 | 0 | 0.585 |
| cvd_divergence | 1 | 0 | 1 | 0.606 |
| funding_reversal | 1 | 0 | 1 | 0.727 |
| indicator_divergence | 1 | 0 | 1 | 0.580 |
| ls_ratio_extreme | 1 | 0 | 1 | 0.860 |

**Observation:** `whale_walls` dominates volume (21/44 outcomes) and contributes 4/10 SL hits. No strategy meets the Cause E threshold (SL > 75% with n ≥ 10). Highest SL contributors: `whale_walls` (4), `btc_correlation` (2), `spread_strategy` (2).

---

## Symbol breakdown

No symbol has ≥5 outcomes. Per-symbol SL (all rows):

| Symbol | Total | SL | SL % |
|--------|------:|---:|-----:|
| 1000FLOKIUSDT | 1 | 1 | 100% |
| ASTERUSDT | 1 | 1 | 100% |
| BCHUSDT | 1 | 1 | 100% |
| 1000PEPEUSDT | 2 | 1 | 50% |
| ADAUSDT | 2 | 1 | 50% |
| LINKUSDT | 2 | 1 | 50% |
| PENGUUSDT | 2 | 1 | 50% |
| TAOUSDT | 2 | 1 | 50% |
| TRUMPUSDT | 2 | 1 | 50% |
| ZECUSDT | 2 | 1 | 50% |
| *(25 symbols)* | 1 each | 0 | 0% |

**Consecutive SL streaks ≥ 3:** none. Max `consecutive_sl` in `symbol_stats` = 1.

---

## Score quality

### SL rate by score quartile

| Band | Total | SL | SL % | TP % |
|------|------:|---:|-----:|-----:|
| Q1: <0.40 | 0 | 0 | — | — |
| Q2: 0.40–0.55 | 1 | 1 | 100% | 0% |
| Q3: 0.55–0.70 | 34 | 9 | 26.5% | 0% |
| Q4: >0.70 | 9 | 0 | 0% | 0% |

**Key finding:** Zero SL in Q4 (score > 0.70, n=9). Bulk of SL sits in Q3 (scores 0.55–0.70). One low-score SL at 0.545 (`depth_imbalance` / 1000FLOKIUSDT).

### Current filter thresholds (`config.toml`)

| Setting | Value |
|---------|------:|
| `filters.min_score` | **0.53** |
| `delivery.watch_min_score` | 0.55 |
| `delivery.action_min_score` | 0.65 |

SL-hit scores range 0.545–0.696 (mean ≈ 0.637). Most SL signals passed `min_score` comfortably; the filter is not blocking the losing cohort.

---

## Time-to-SL distribution

Derived active minutes = `time_to_exit_min - time_to_entry_min`.

| Bucket | n | Avg PnL % | Avg MFE % | Avg MAE % | MFE/MAE |
|--------|--:|----------:|----------:|----------:|--------:|
| A: <15 min | 5 | -2.908 | 0.009 | 2.840 | **0.003** |
| B: 15–60 min | 2 | -1.860 | 0.000 | 1.914 | 0.000 |
| C: 1–4 h | 3 | -1.047 | 1.133 | 1.048 | 1.081 |
| D/E: >4 h | 0 | — | — | — | — |

**50% of SL hits close within 15 minutes** with near-zero MFE — price never moved favorably before stop.

### Individual SL outcomes

| Strategy | Symbol | Dir | Score | R:R | ATR% | Bias 4h | Result | MFE | MAE | Active min | R |
|----------|--------|-----|------:|----:|-----:|---------|--------|----:|----:|-----------:|--:|
| whale_walls | ASTERUSDT | short | 0.663 | 1.9 | 0.69 | neutral | stop_loss | 0.0 | 1.05 | 10 | -1.0 |
| whale_walls | ZECUSDT | short | 0.629 | 1.9 | 8.97 | neutral | stop_loss | 0.0 | 9.84 | 13 | -1.0 |
| depth_imbalance | 1000FLOKIUSDT | short | 0.545 | 1.9 | 1.31 | downtrend | stop_loss | 0.0 | 0.27 | 1 | -1.02 |
| whale_walls | LINKUSDT | short | 0.644 | 1.9 | 1.38 | downtrend | stop_loss | 0.0 | 1.35 | 9 | -1.01 |
| spread_strategy | PENGUUSDT | short | 0.586 | 1.9 | 1.86 | downtrend | stop_loss | 0.0 | 1.95 | 37 | -1.0 |
| btc_correlation | TRUMPUSDT | short | 0.689 | 1.9 | 1.83 | downtrend | stop_loss | 0.0 | 1.67 | 64 | -1.03 |
| aggression_shift | 1000PEPEUSDT | short | 0.607 | 1.9 | 1.71 | downtrend | stop_loss | 0.04 | 1.69 | 8 | -1.0 |
| btc_correlation | ADAUSDT | short | 0.638 | 1.9 | 1.88 | downtrend | breakeven_stop | 3.40 | 0.0 | 102 | 0.0 |
| spread_strategy | TAOUSDT | short | 0.696 | 1.9 | 1.83 | downtrend | stop_loss | 0.0 | 1.88 | 23 | -1.0 |
| whale_walls | BCHUSDT | short | 0.676 | 1.9 | 1.47 | downtrend | stop_loss | 0.0 | 1.47 | 120 | -1.0 |

---

## Regime correlation

### SL rate by BTC bias (`bias_4h`)

| Bias | Total | SL | SL % |
|------|------:|---:|-----:|
| downtrend | 30 | 8 | 26.7% |
| neutral | 14 | 2 | 14.3 |

### Direction × BTC bias

| Direction | Bias | Total | SL % |
|-----------|------|------:|-----:|
| long | downtrend | 2 | 0.0% |
| short | downtrend | 28 | 28.6% |
| short | neutral | 14 | 14.3% |

Shorts in downtrend show ~2× SL rate vs shorts in neutral bias, but sample is small. No long SL to test bear-long hypothesis. All SL `market_regime` in features JSON = `neutral` (regime field may not reflect `bias_4h`).

### ATR% at entry vs outcome

| ATR band | Total | SL % | Avg PnL % |
|----------|------:|-----:|----------:|
| high_vol: 1.5–3.0% | 7 | **71.4%** | -1.009 |
| extreme_vol: >3.0% | 2 | 50.0% | -4.709 |
| mid_vol: 0.5–1.5% | 34 | 11.8% | -0.145 |
| low_vol: <0.5% | 1 | 0.0% | 0.0 |

High-volatility entries (ATR% 1.5–3.0%) concentrate SL risk: 5 of 7 outcomes in this band are SL.

### SL root-cause codes (`sl_diagnostics`)

| Code | Count | Label |
|------|------:|-------|
| immediate_adverse_entry | 6 | Вход сразу против движения (MFE≈0) |
| stop_hunt_post_recovery | 3 | Stop hunt — после SL цена шла к TP1 |
| thesis_failed | 1 | Тезис не реализовался |

Post-SL recovery data: avg `post_sl_favorable_pct` = 1.02%, avg `post_sl_tp1_room_pct` = 6.36% — thesis often remained valid after stop.

---

## Feature comparison (SL vs TP)

| Metric | Value |
|--------|------:|
| SL signals with features | 10 |
| TP signals with features | **0** |

Cross-cohort feature diff (D4) **not possible** — no TP outcomes persisted.

### SL-only feature means (n=10)

| Feature | Mean |
|---------|-----:|
| score / base_score | 0.637 |
| risk_reward | 1.900 |
| stop_distance_pct | 2.354% |
| post_sl_favorable_pct | 1.019% |
| post_sl_tp1_room_pct | 6.364% |

---

## Risk/Reward structure

### R:R band for SL hits

| R:R band | Count |
|----------|------:|
| bad_rr: <1.0 | 0 |
| ok_rr: 1.0–1.5 | 0 |
| good_rr: 1.5–2.5 | **10** |
| great_rr: >2.5 | 0 |

All SL hits had R:R = 1.9 (contract minimum met). Losses are **not** caused by structurally bad R:R at entry.

### MAE / MFE (SL hits only)

| Metric | Value |
|--------|------:|
| Avg MAE | 2.117% |
| Avg MFE | 0.344% |
| MFE/MAE ratio | **0.16** |
| n | 10 |

**Interpretation:** MFE/MAE = 0.16 (< 0.1 threshold for bucket A) → price rarely moved in our favor before SL. This points to **entry timing / direction**, not insufficient R:R or a wide stop giving room.

Exception: bucket C (1–4 h, n=3) shows MFE/MAE ≈ 1.08 — ADAUSDT breakeven_stop had MFE 3.4% before giving back gains.

---

## Root cause classification

```
ROOT CAUSE CLASSIFICATION:

Cause A (entry timing):   CONFIRMED
  evidence: 5/10 SL in <15 min bucket; MFE/MAE=0.003 in that bucket;
            6/10 sl_root_cause=immediate_adverse_entry (MFE≈0);
            3/10 stop_hunt_post_recovery (post_sl_tp1_room avg 6.36%);
            avg MFE across all SL = 0.34% vs avg MAE = 2.12%

Cause B (stop too tight): POSSIBLE
  evidence: high_vol ATR band (1.5–3.0%) has 71.4% SL rate;
            ZECUSDT extreme ATR 8.97% → MAE 9.84% instant stop;
            3 stop_hunt_post_recovery cases (price recovered toward TP1 after SL);
            BUT overall MFE/MAE=0.16 argues primary issue is entry not shake-out;
            no consecutive_sl streaks ≥ 3

Cause C (regime mismatch): POSSIBLE
  evidence: short+downtrend bias SL 28.6% vs short+neutral 14.3%;
            8/10 SL in downtrend bias_4h;
            all SL are short (no long SL to compare);
            regime soft-penalties may not be blocking marginal shorts;
            NOT CONFIRMED: no long-in-bear SL cluster, n too small

Cause D (weak signals):   NOT EVIDENT
  evidence: Q4 (>0.70) has 0% SL (9 signals, 0 SL);
            SL mean score 0.637 > min_score 0.53;
            only 1 signal in Q2 with SL;
            min_score 0.53 is permissive but SL cohort scores mid-range not bottom

Cause E (strategy bug):     NOT EVIDENT
  evidence: no strategy with SL > 75% and n ≥ 10;
            whale_walls highest contributor (4 SL / 21 total = 19%);
            SL spread across 6 strategies — not isolated to one detector;
            directional asymmetry: 100% short SL (may be session bias not bug)
```

### Primary diagnosis

**Dominant failure mode: Cause A (late / adverse entry timing).**  
Half of stops fire within 15 minutes with zero favorable excursion. Post-SL recovery data suggests the thesis was often still valid — classic chase / activation-at-extreme pattern.

**Secondary: Cause B (stop sizing in high vol)** for symbols like ZECUSDT and high-ATR band entries.

**Not supported by data:** Cause D (filters are loose but SL scores are mid-tier), Cause E (no single broken strategy at scale).

---

## Recommended fixes (not implemented)

Listed by priority. Requires longer live sample before threshold changes.

### P1 — Entry timing (Cause A)

1. Tighten `tracking.late_entry_chase_pct` (currently 0.8% default) for publish-time chase rejection.
2. Add post-activation guard: if MFE stays 0 for N minutes, flag for telemetry (early adverse entry detector).
3. Review limit-zone placement for `whale_walls`, `spread_strategy`, `btc_correlation` — 7/10 SL from these three families.
4. Persist `entry_vs_zone_pct` and `activation_delay_min` in outcome features for future A/B.

### P2 — Stop sizing in high vol (Cause B)

1. Raise `sl_buffer_atr` floor when `atr_pct > 1.5%` (config per strategy or global guard).
2. Cap universe inclusion for `atr_pct > 3%` symbols (ZECUSDT case: 8.97% ATR → 9.84% MAE).
3. Expand `stop_hunt_post_recovery` handling — consider wider buffer when `post_sl_tp1_room` historically high.

### P3 — Regime alignment (Cause C)

1. Convert ADX/regime soft-penalties in `_analyzer_gates.py` to hard blocks for short continuation setups when `bias_4h=downtrend` AND score < 0.65.
2. Log `market_regime` consistently into outcome features (currently `neutral` despite `bias_4h=downtrend`).

### P4 — Filter calibration (Cause D)

1. Raise `filters.min_score` from **0.53 → 0.58** (Q3 SL band starts at 0.55; Q4 with 0 SL begins at 0.70).
2. Require **4/5 hard confluence** for ACTION tier (currently 3/5).
3. Do **not** lower confluence gates to force signals during calibration.

### P5 — Strategy audit (Cause E)

1. Run `strategy_shortlist_matrix.py` after 6h supervised session for zero-hit / high-SL setups.
2. Priority audit: `whale_walls` (volume leader, 4 SL), `depth_imbalance` (1-min SL on FLOKI).
3. Collect n ≥ 50 executed outcomes before declaring structural bugs.

### P6 — Data pipeline

1. **Critical:** 0 TP rows in `signal_outcomes` — verify outcome writer persists `tp1_hit`/`tp2_hit` correctly (2 TP1 touches in `active_signals` not reflected as wins).
2. 75% expired rate (33/44) — review TTL / activation funnel; many plans never fill or expire without resolution.

---

## Appendix: query adaptations

```sql
-- SL result filter (use instead of sl_hit)
result IN ('stop_loss', 'breakeven_stop', 'trailing_stop')

-- Features source
signal_outcomes.features  -- not active_signals.features

-- Time to SL
(time_to_exit_min - time_to_entry_min)  -- not time_to_sl_min
```

---

*Generated from live SQLite `data/bot/bot.db`. Re-run after next supervised 6h session for statistically meaningful strategy breakdowns.*
