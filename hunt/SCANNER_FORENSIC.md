# SCANNER_FORENSIC.md

Forensic map of the Scanner (pre-pump / pre-dump) module: every computation
from data acquisition to signal generation, with inputs, outputs, formulas,
weights, block conditions, and dependencies. **Read-only audit — no code
changed.** Date: 2026-06-24.

Scope note on completeness: the computational spine (factors → fusion → gate →
delivery → playbook authority) is traced to source with exact formulas/weights.
The gate **sub-module bodies** (`_move`, `_tradability`, `_wash`, `_mission`
thresholds) are mapped at responsibility + block-code level; their internal
threshold constants are flagged as Phase-2 depth where not quoted.

---

## 0. The headline findings (read this first)

1. **There are TWO parallel "fusion" systems, and the elaborate one is bypassed
   for the scanner's core mission.**
   - `analysis/manipulation_fusion.py` — archetype checklist (predump / coil /
     ignition), ~30 weighted factors, N-of-M playbook. Prominent in logs, deep
     panels, and `hunt_scan.jsonl`.
   - `scanner/detect/fusion.py` — statistical magnitude engine (signed median of
     factor z-scores). **This is the live decision authority.**
   - In `gate/_policy_decl.py::_decl_check_playbook` (line 287–292): when
     `setup.signal_type == "pre_phase"` (the scanner's whole reason to exist),
     the playbook/`manipulation_fusion` gate is **skipped** — only a rate-limit
     applies. The statistical Dual-Gate decides. So the archetype system gates
     only *non*-pre-phase signals.

2. **`manipulation_fusion.primary_score` is an unweighted count ratio**, not the
   weighted sum. `playbook_checks.best_archetype_by_ratio` →
   `100 × pass_count / len(required_keys)`. The domain weights (22, 18, 16, 14,
   12, 10, 8 …) feed only `score_predump/coil/ignition`, which are consumed
   **only by `deep/fusion_panel.py` for display**. The weights have **zero
   decision impact**. (Verified: scan-log `primary_score 42.9 = 100×3/7`.)

3. **The live `p_win` is uncalibrated.** `detect/fusion.py`:
   `fusion_score = magnitude × 25` (capped 100); `delivery_setup.py:119` stores
   `p_win = fusion_score / 100 = magnitude × 0.25`. A linear rescale of the
   factor-median magnitude — never mapped to realized win frequency. Code itself
   labels it "NOT calibrated P(win)", yet it ships as `p_win`.

4. **The gate is self-referential.** `detect/fusion.py::gate` opens when the
   symbol's `vol_adjusted_magnitude ≥ max(symbol's own recent q90 quantile,
   global_floor)`. By construction ~10% of a symbol's own bars qualify. It
   detects "unusual relative to this symbol's recent self", **not** "a state
   that historically precedes a move". Same for `pre_phase_gate` (structure-based
   thresholds, not outcome-derived).

5. **Verifiability boundary** (from the historical event study): the directional
   factors `book` (depth_imbalance, microprice) and `flow` (CVD, taker) — and the
   archetype checks `bid_absorption`, `vp_accumulation`, `va_contraction`,
   liquidity magnets — rest on **orderbook/VP data with no historical archive**.
   They can never be backtested; only validated forward via the outcome tracker.

---

## 1. Pipeline data-flow map

```
WS 15m kline close
  → tick_assembly (build result row: market/structure/lifecycle/timeframes/maps…)
      → features/feature_engine.build_feature_vector(prepared, tf=15m, closed)
      → runtime/tick_fusion.run_fusion_detection
          → scanner/detect/live.build_live_detection
              → detect/factors.compute_factors(window)      [11 production factors]
              → detect/fusion.fuse(factors)                 [signed-median magnitude]
              → detect/fusion.gate / pre_phase_gate          [self-calibrated authority]
          → detect/delivery_setup.build_delivery_setup       [setup dict: side, p_win, geom]
      → analysis/manipulation_fusion.stamp_fusion_on_row     [archetype checklist — parallel]
  → scanner/gate pipeline_pre_blockers + _policy_decl checks  [filter stack]
  → scanner/delivery/arbiter + scanner/telegram               [emit]
```

Two scorers run every tick on the same row. Only the `detect/*` branch governs
`pre_phase` delivery; `manipulation_fusion` is stamped for logging/display and
gates non-pre-phase only.

---

## 2. Live factors — the real decision inputs (`detect/factors.py`)

Each factor is a **robust-z vs the symbol's own trailing window** (`calibrate.py`),
or abstains (`active=False`) on missing/stale/cold-start inputs. No absolute
thresholds anywhere.

### Directional (sign picks side; `>0` ⇒ long/pre-pump, `<0` ⇒ short/pre-dump)
| factor | inputs | formula | backtestable? |
|---|---|---|---|
| `book` | depth_imbalance, microprice_bias | mean of robust-z | ❌ orderbook |
| `structure` | rsi14, bb_pct_b, zscore30 | −mean(robust-z) (mean-reversion) | ✅ klines |
| `funding` | funding_rate | −robust-z (contrarian) | ✅ (long hist) |
| `flow` | rolling_cvd_24h/session_cvd, delta_ratio | mean(ols_slope, robust-z) | ❌ CVD/taker |
| `oi_acceleration` | oi 2nd-derivative (lake) | robust-z | ⚠️ ~30d OI |
| `funding_velocity` | funding slope | robust-z | ✅ |
| `poc_migration` | POC drift | robust-z | ❌ VP |
| `liquidity_void_path` | liq void map | robust-z | ❌ liq map |

### Amplifier (unsigned, scales conviction via `tanh`; never picks side)
| factor | inputs | formula | backtestable? |
|---|---|---|---|
| `oi_pressure` | oi_change_pct, oi_slope_5m | max(\|robust-z\|) | ⚠️ ~30d OI |
| `compression` | bb_width, atr_pct | max(level_coil, vel_coil) | ✅ klines |
| `va_contraction` | value-area width | robust-z | ❌ VP |

**Quarantined (computed, NOT in production fuse):** `market_maker_trap`,
`whale_activity`, `cross_exchange_divergence`, `cross_funding_consensus`,
`spot_futures_pressure` — "OOS gate pending" / "lake persist pending".

### Fusion (`detect/fusion.py::fuse`)
```
z_dir     = median(score of active directional factors)      # signed
amp       = mean(tanh(max(0, score)) for active amplifiers)  # in [0,1)
magnitude = |z_dir| × (1 + amp)
side      = sign(z_dir)
fusion_score = min(100, magnitude × 25)
```
- **Signed median** (not weighted sum) → every directional factor has equal
  vote; collinear factors (book+flow both orderbook-derived) are robust-handled
  but also means orderbook data effectively gets 2 of ~6 votes.
- `structure` is pure **mean-reversion**: an extended-up symbol scores *short*.
  On a pre-**pump** detector this is directionally adversarial to catching an
  upside breakout — a conceptual tension worth confirming against outcomes.

---

## 3. Gate authority (`detect/fusion.py`)

| gate | opens when | notes |
|---|---|---|
| `gate` (momentum) | n_active ≥ min, agreement, `vol_adj_mag ≥ max(symbol q90, global_floor)` | self-referential quantile |
| `pre_phase_gate` | energy_hits ≥ 3, structure_score ≥ 0.18, magnitude ≥ 0.15 | structure thresholds, not outcome-derived |

`structure_score = |raw depth_imbalance|` — independent of z_dir (so not pure
redundancy with magnitude), but still orderbook-derived (un-backtestable).
`confirmed = detection.gate_open or detection.pre_gate_open` (`tick_fusion` →
`delivery_setup.py:116`).

---

## 4. manipulation_fusion archetype system (`analysis/manipulation_fusion.py`)

Three domains scored by additive weights, then ranked by **unweighted N-of-M**.

**predump_short** (req 4/6): distribution_phase(+22) · pos_near_high(+18) ·
oi_distribution(+16) · bear_cvd_div(+14) · sweep_reclaim(+12) · anti_squeeze ·
[leg_gain≥40 +10]. squeeze_block ⇒ `predump ×= 0.35`.

**prepump_long** (req 5/7): vp_accumulation(+20) · coil_phase(+18) ·
va_contraction(+12) · bid_absorption(+10) · bull_cvd_div(+10) · vah_break_5m(+8) ·
vol_above_median_5m(+8).

**ignition_long** (req 5/5): neg_funding(+18) · short_liq_above(+16) ·
squeeze_regime(+14) · cvd_absorption(+12) · obi_bid(+10).

**Findings:**
- Weights are dead (see §0.2). Each check is effectively worth `100/len(keys)`.
- `prepump_long` requires `vah_break_5m` **and** `vol_above_median_5m` — both are
  *breakout-in-progress* confirmations. A "pre"-pump archetype that needs the
  breakout to already print is conceptually catching the move, not preceding it.
- `vol_above_median_5m` ≥1.5× — the event study found volume-ratio is **noise**
  (AUC 0.52). It is 1 of 7 equal votes here.
- Display-only checks `vol_oi_sane`, `flow_aligned` (`SMART_MONEY_DISPLAY_CHECKS`)
  are computed and shown but explicitly **not** in any N-of-M set → influence
  nothing. (Hidden-vs-shown inversion: shown, no effect.)
- `manipulation_fusion` gates only non-pre-phase (§0.1) → for the core mission it
  is **shown but does not decide**.

---

## 5. Gate / filter stack (`scanner/gate/`, 5,761 LOC)

Builtin sequence (`_registry.py::_register_builtin_gates`, first failure
short-circuits): `edge_policy → mission → data_completeness → stale → wash →
kinematic → move_significance → tradability → squeeze_predump`, plus
`_policy_decl` checks (playbook, ev_delivery, …).

| gate | blocks when | authority |
|---|---|---|
| edge_policy | long ramp not reached (n<30 outcomes) → routes to **lab lane** (not block); short edge block | `policy.long_tg_allowed` |
| mission | preparation-readiness fails | `_mission.assess_preparation_readiness` (Phase-2 depth) |
| data_completeness | derivatives incomplete for tier | `data.completeness` |
| stale | price past TP1 / no entry geometry | `_freshness.delivery_hard_block` |
| wash | wash/volume-manip suspicion | `_wash.wash_block_reason` (thresholds Phase-2) |
| kinematic | move too fast = late chase | `_wash.kinematic_block_reason` |
| move_significance | move below significance | `_move` — **shadow unless `strategic_gates_hard()`** |
| tradability | spread/liquidity untradable | `_tradability` — **shadow unless `strategic_gates_hard()`** |
| squeeze_predump | short + crowded shorts + neg funding | `manipulation_fusion._squeeze_blocks_predump` |
| playbook (`_policy_decl`) | non-pre-phase N-of-M fail; **pre_phase → skipped** | §0.1 |

**Findings:**
- `move_significance` and `tradability` run in **shadow mode** (warn-only) unless
  `strategic_gates_hard()` is enabled — verify whether they actually block in the
  live config or merely log.
- `squeeze_predump` logic duplicates `_squeeze_blocks_predump` already applied as
  a `predump ×0.35` penalty inside fusion → the same squeeze signal is consumed
  twice (once as score penalty, once as hard gate). Potential double-count.

---

## 6. Delivery → output (`detect/delivery_setup.py`, `scanner/telegram.py`)

Setup dict carries: `side`, `confirmed (gate_open|pre_gate_open)`,
`fusion_score`, `p_win (=fusion_score/100)`, `phase`, geometry (entry/SL/TP from
`levels.py`), `intrabar_confirmed`, archetype (from manipulation_fusion). Telegram
renders side/score/levels.

**Shown-but-doesn't-decide:** archetype label, `score_predump/coil/ignition`,
`primary_score`, smart-money checks. **Decides-but-shown-as-`p_win`:** the raw
`magnitude × 0.25`.

---

## 7. Candidate issues for follow-up (ranked)

1. **Decommission or demote `manipulation_fusion` for pre-phase** — it is a large
   surface (361 LOC + playbook + panels) computed every tick but bypassed for the
   core mission. Either make it authoritative or stop presenting it as the score.
2. **Calibrate `p_win`** — replace `magnitude × 0.25` with an outcome-derived
   mapping (decile → realized win-rate) once the outcome tracker has data.
3. **The weights are dead** — either wire `score_*` into the decision or delete
   them; current state implies tuning that does nothing.
4. **`vol_above_median_5m` / volume-ratio factors** — event study says noise;
   reconsider their inclusion as equal votes.
5. **Double-counted squeeze** (§5) and **mean-reversion `structure` on a pump
   detector** (§2) — resolve directional/semantic conflicts.
6. **Self-referential gate** — the q90-of-self design guarantees a base firing
   rate regardless of predictive value; this is the central thing the outcome
   tracker must vindicate or refute.

---

## Phase-2 (remaining depth, not yet mapped to constants)
- `_mission.assess_preparation_readiness` exact criteria.
- `_wash` / `kinematic` / `_move` / `_tradability` threshold constants.
- `calibrate.py` robust_z / quantile_gate sample floors and window sizes.
- `scanner/delivery/arbiter.py` final ranking/dedup logic.
- `levels.py` entry/SL/TP geometry source.

After this, `DEEP_FORENSIC.md` for the Deep module (`verdict_v2` spine).
