# V2 Work Cycle After V1

## 0) Confirmed inputs from V1

Confirmed (from repository docs):
- Shared SMC layer was introduced, and five SMC strategies were migrated to shared helpers (`fvg`, `order_block`, `liquidity_sweep`, `bos_choch`, `breaker_block`).
- Some contract-cleanup work was explicitly deferred as follow-up.

Source: `docs/remediation_ledger.md`.

Assumptions (to verify in execution):
- V1 did not fully close strategy-level guard/contract parity across all non-SMC strategies.
- V1 likely focused on runtime safety + SMC parity, so V2 should prioritize broad strategy contract coverage and regression hardening.

---

## 1) Priority risk order for `bot/strategies/` and `bot/setups/`

Risk model used for prioritization:
- **Blast radius** (shared helper impact across many strategies)
- **Decision complexity** (multi-step structure/target logic)
- **Contract sensitivity** (`Signal` fields, `setup_id`, rejection pathways)
- **Regression surface** (historically remediated areas from V1)

1. `bot/setups/smc.py` — shared SMC primitives, highest blast radius.
2. `bot/setups/utils.py` — shared helper surface used by multiple strategies.
3. `bot/strategies/fvg.py` — SMC strategy migrated in V1, contract-critical.
4. `bot/strategies/order_block.py` — SMC strategy migrated in V1, contract-critical.
5. `bot/strategies/liquidity_sweep.py` — SMC strategy migrated in V1, target/stop risk-sensitive.
6. `bot/strategies/bos_choch.py` — SMC structure break logic + TP/RR guards.
7. `bot/strategies/breaker_block.py` — SMC breaker logic + stop/target validity.
8. `bot/strategies/structure_pullback.py` — deeper multi-branch structure logic.
9. `bot/strategies/squeeze_setup.py` — volatility + breakout target-building complexity.
10. `bot/strategies/structure_break_retest.py` — structure/retest branch surface.
11. `bot/strategies/hidden_divergence.py` — divergence thresholds + target validity.
12. `bot/strategies/cvd_divergence.py` — flow divergence + threshold sensitivity.
13. `bot/strategies/funding_reversal.py` — funding-flow conditions + reversals.
14. `bot/strategies/session_killzone.py` — time/session gating risk.
15. `bot/strategies/turtle_soup.py` — sweep/reversal edges.
16. `bot/strategies/wick_trap_reversal.py` — wick traps with rejection sensitivity.
17. `bot/strategies/ema_bounce.py` — comparatively narrower trend-bounce contract.

---

## 2) Per-file checklist (inputs, guards, signal output, shortlist/freshness dependency)

> Note: direct dependencies on shortlist/freshness are expected mostly outside strategy files (universe/filter pipeline). For these files, validate **indirect contract compatibility** (strategy output must remain filter-safe).

### `bot/setups/smc.py`
- [ ] **Input data**: required OHLCV/structure columns and minimum frame height for all exported helpers.
- [ ] **Guards**: no unsafe indexed access; explicit handling for empty/missing/non-finite values.
- [ ] **Signal output contract**: helper outputs used by SMC strategies keep stable field semantics.
- [ ] **Shortlist/freshness**: verify helper outputs do not assume stale symbol context; remain deterministic for pre-filtered prepared data.

### `bot/setups/utils.py`
- [ ] **Input data**: helper arguments validate required columns/types.
- [ ] **Guards**: explicit null/NaN/empty protections.
- [ ] **Signal output contract**: helper return shape unchanged for existing callers.
- [ ] **Shortlist/freshness**: no hidden dependence on shortlist ordering or freshness metadata.

### `bot/strategies/fvg.py`
- [ ] **Input data**: required columns + minimum bars for gap detection.
- [ ] **Guards**: branch-safe checks before `item(-1/-2/-3)` style access.
- [ ] **Signal output contract**: valid `Signal(direction, entry_low/high, stop, take_profit_1/2)` and stable `setup_id`.
- [ ] **Shortlist/freshness**: strategy remains agnostic to shortlist source; emits outputs compatible with freshness filters.

### `bot/strategies/order_block.py`
- [ ] Input data completeness for OB zone + trend context.
- [ ] Guard all indexed/zone lookups and invalid zone-state transitions.
- [ ] Output `Signal` levels are monotonic/valid for side (long/short).
- [ ] No direct shortlist/freshness assumptions; filter-stage compatibility preserved.

### `bot/strategies/liquidity_sweep.py`
- [ ] Input data supports liquidity-pool/sweep detection.
- [ ] Guards for invalid stop/entry distances and risk <= 0 paths.
- [ ] Output enforces min RR and correct side-aware target direction.
- [ ] Indirect shortlist/freshness compatibility confirmed.

### `bot/strategies/bos_choch.py`
- [ ] Input columns for swings + break confirmation.
- [ ] Guard risk-positive and TP-distance constraints.
- [ ] Output `Signal` fields aligned with `bot.models.Signal` semantics.
- [ ] No shortlist/freshness coupling beyond prepared frame quality.

### `bot/strategies/breaker_block.py`
- [ ] Input columns for breaker confirmation and context.
- [ ] Guard path for non-positive risk and missing targets.
- [ ] Output signal side/levels internally consistent.
- [ ] Shortlist/freshness independence preserved.

### `bot/strategies/structure_pullback.py`
- [ ] Input requirements for structure levels + ATR/momentum dependencies.
- [ ] Guard multi-branch path for empty/intermediate frames.
- [ ] Output stop/TP logic consistent with configured RR.
- [ ] Filter compatibility under stale/fresh prepared data remains intact.

### `bot/strategies/squeeze_setup.py`
- [ ] Input columns for BB compression, expansion, and volume confirmation.
- [ ] Guards for compression/threshold edge cases and zero-risk paths.
- [ ] Output preserves side-correct stop/TP with min RR checks.
- [ ] No direct shortlist/freshness dependency.

### `bot/strategies/structure_break_retest.py`
- [ ] Input columns for break + retest confirmation.
- [ ] Guards for insufficient bars and invalid retest zones.
- [ ] Output `Signal` respects contract and side-safe ranges.
- [ ] Freshness-safe behavior verified via filter integration tests.

### `bot/strategies/hidden_divergence.py`
- [ ] Input columns for RSI/delta divergence windows.
- [ ] Guard lookback bounds and threshold parameter handling.
- [ ] Output signal and setup metadata contract stable.
- [ ] Indirect shortlist/freshness compatibility.

### `bot/strategies/cvd_divergence.py`
- [ ] Input columns for CVD/delta calculations.
- [ ] Guard insufficient history and non-finite metric values.
- [ ] Output entry/stop/TP ordering valid for side.
- [ ] No direct shortlist/freshness dependency.

### `bot/strategies/funding_reversal.py`
- [ ] Input columns for funding trend + price context.
- [ ] Guard missing funding windows / low-confidence reversals.
- [ ] Output aligns with `Signal` contract and configured buffers.
- [ ] Freshness compatibility checked via pipeline tests.

### `bot/strategies/session_killzone.py`
- [ ] Input requires valid time/session fields and intraday bars.
- [ ] Guards around timezone/session boundary handling.
- [ ] Output contract stability for entry/stop/targets.
- [ ] No shortlist-source assumptions.

### `bot/strategies/turtle_soup.py`
- [ ] Input supports sweep-failure pattern detection.
- [ ] Guards for lookback length and false-break edge cases.
- [ ] Output side-consistent levels and RR validity.
- [ ] Indirect freshness compatibility.

### `bot/strategies/wick_trap_reversal.py`
- [ ] Input includes wick/body metrics and trend context.
- [ ] Guards for ambiguous candles and low-liquidity anomalies.
- [ ] Output preserves strict `Signal` contract.
- [ ] No direct shortlist dependency.

### `bot/strategies/ema_bounce.py`
- [ ] Input requires EMA stack + basic trend metrics.
- [ ] Guards for missing EMA columns and flat/noise regimes.
- [ ] Output keeps side-aware stop/target placement.
- [ ] Freshness compatibility validated in integration filter pass.

---

## 3) V2 completion criteria

### A. Coverage by strategies
- [ ] Every file in `bot/strategies/*.py` and `bot/setups/*.py` has a checked contract checklist.
- [ ] Regression coverage includes:
  - strategy metadata/contract smoke (`tests/test_strategies.py`),
  - remediation regressions (`tests/test_remediation_regressions.py`),
  - focused additions for newly fixed defects.
- [ ] Minimum target: each high-risk file (priority 1-10) has at least one explicit regression assertion tied to the repaired behavior.

### B. Critical defect closure
- [ ] Closed: unsafe indexed access on insufficient frames.
- [ ] Closed: invalid side-aware stop/TP or non-positive risk paths.
- [ ] Closed: `setup_id`/config-key drift and signal-contract mismatches.
- [ ] Closed: helper contract drift affecting multiple strategy callers.

### C. No contract mismatches
- [ ] `detect()` / `calculate()` pathways return `Signal | None` in contract shape.
- [ ] `get_optimizable_params()` remains stable and config-driven.
- [ ] Strategy exports/registry/config alignment remains intact.
- [ ] No regressions against shortlist `rest_full` vs `ws_light` pipeline expectations (validated indirectly via integration tests).

---

## 4) Mapping V1 risks to concrete V2 files

| V1 risk theme | V2 files to own closure |
| --- | --- |
| Shared SMC migration may leave edge-case parity gaps | `bot/setups/smc.py`, `bot/strategies/fvg.py`, `bot/strategies/order_block.py`, `bot/strategies/liquidity_sweep.py`, `bot/strategies/bos_choch.py`, `bot/strategies/breaker_block.py` |
| Deferred feature/contract cleanup across strategy surface | `bot/setups/utils.py`, all non-SMC strategies (`structure_pullback.py`, `squeeze_setup.py`, `structure_break_retest.py`, `hidden_divergence.py`, `cvd_divergence.py`, `funding_reversal.py`, `session_killzone.py`, `turtle_soup.py`, `wick_trap_reversal.py`, `ema_bounce.py`) |
| Guarding against frame/column insufficiency | all `bot/strategies/*.py` + `bot/setups/smc.py` + `bot/setups/utils.py` |
| Signal shape and side-valid RR/target consistency | all `bot/strategies/*.py` |
| Shortlist/freshness contract drift (indirect, pipeline-level) | all strategy files via integration checks in `tests/test_remediation_regressions.py` and shortlist-related tests |

---

## 5) Status tracker (`todo / in-progress / done / blocked`)

| Work item | Status | Owner | Notes |
| --- | --- | --- | --- |
| Build per-file contract audit sheet for priorities 1-10 | todo | v2 | Start with shared helpers, then SMC strategies |
| Add/refresh targeted regressions for high-risk files | todo | v2 | Prefer focused tests over broad end-to-end |
| Validate config/setup_id/export alignment | todo | v2 | Check `bot/strategies/__init__.py` + `config.toml(.example)` |
| Verify indirect shortlist/freshness compatibility | todo | v2 | Use remediation + shortlist regression tests |
| Close critical defects found during audit | in-progress | v2 | Execute in small PR slices by risk block |
| Publish final V2 closure report (fact vs inference) | todo | v2 | Include defect IDs, tests, residual risks |
| External dependency/data access blockers | blocked | n/a | None currently identified |

