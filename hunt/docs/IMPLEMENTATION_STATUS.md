# Hunt Two-Module Rebuild — implementation status

> Canon: **Module 1 = Deep** · **Module 2 = Scanner** · Plan: `abstract-chasing-cerf.md`

## Summary (2026-06-22)

**Plan complete (R1–R11 + phases 0–9 + P0/P0').** Phase 9: mechanical LOC-splits reverted; semantic splits only (`tracker` FSM, `policy` gate).

## Module 1 Deep redesign

| Item | Status |
|------|--------|
| R1 Evidence-integrated engines + factor contributions | **done** — `cross_consensus` engine + `build_factor_contributions` |
| R2 Reconciliation gate | **done** — `verdict_v2/reconcile.py` (synthetic liq ignored) |
| R3 Single plan-geometry authority | **done** — `deep/plan.finalize_plan_geometry` |
| R4 Activation lifecycle | **done** — forming→armed→active + R recompute + TG activation block |
| R5 Scenario taxonomy | **done** — continuation guard; alt dedup |
| R6 Provenance `as_of` | **done** — assembly stamp + query/render |
| R7 Cross-venue DOM | **done** — per-venue levels; top-N imbalance |
| R8 Equivalence + vocabulary | **done** — XAU≡PAXG; «Сила сигнала» / «Приоритет очереди» |
| R9 Liq map honesty | **done** — `leverage_tier_estimate` provenance; suppress placeholder 0.35 conf |
| R10 Stop buffer + DOM scale | **done** — structural SL buffer; display-level imbalance |
| R11 Queue scope | **done** — global pinned TOP-N; peers footer; gold collapse |

## Phase completion

| Phase | Status |
|-------|--------|
| 0–9 Two-module rebuild | **done** |
| P0 / P0' | **done** |
| Legacy shims | **removed** (`analysis/deep_signal`, `analysis/deep`, `expansion_engine` → `_dev/expansion_lab`) |
| E2E Module-1 checks | **done** — `check_deep_e2e.py` (synthetic + live `BTCUSDT` via `HUNT_LIVE=1`) |
| Duplicate `detect/deep` tree | **resolved** → `scanner/detect/lake_panel` (offline lake smoke only) |
| Plan completion gate | **done** — `_dev/check_plan_complete.py` |
| Phase 8 Longs ramp | **done** — uncalibrated long → lab lane + ledger geometry (`long_ramp_reason`) |
| Phase 9 Architectural debt | **done** — prescan + liq docs gate; **semantic splits only** (`tracker`+`_tracker_fsm`, `policy`+`_policy_*`); LOC/file-count splits **reverted** (2026-06-22) |
| Trend facts | **moved** — `shared/facts/trend.py`, `shared/facts/adx_thresholds.py` |

## Verification

```bash
bash hunt/scripts/verify_hunt_rebuild.sh   # includes HUNT_LIVE=1 assemble_deep_tick BTCUSDT
python -m hunt_core._dev.check_deep_e2e --live
```

**2026-06-22 fix:** `features/snapshot.py` import `analysis.trend_engine` → `shared.facts.trend` (live E2E gate was failing).

## Legacy marker audit (Phase 4)

Production `hunt_core/` (excl. `_dev/`): **~25** `legacy|compat|deprecated|shim` hits remain — mostly stable API names (`legacy_trend_label`) and routing comments, not active bridge code. Shims deleted: `analysis/deep_signal`, `analysis/deep`, `expansion_engine`, `phase_compat`.

## Live soak (ongoing, not blocking)

- Ledger accrual → OOS promotion of quarantine factors (`need_n=172`)
