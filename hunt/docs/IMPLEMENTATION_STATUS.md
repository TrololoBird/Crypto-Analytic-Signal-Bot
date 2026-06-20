# Hunt fusion migration — implementation status

> Spec: [`ENGINE_DESIGN.md`](ENGINE_DESIGN.md) · Params: [`FUSION_PARAMS.md`](FUSION_PARAMS.md)

## Honest scope

**Threshold-free detection is impossible.** Self-calibration removes market-tuned constants
(RSI 66, fall 3%) and replaces them with distribution-relative statistics plus an explicit
small parameter set in `[fusion]` / `detect/config.py`.

**`fusion_score` is not P(win).** Calibrated delivery probability comes from geometry/catalog
EV (`delivery_p_win`) when levels exist; fusion outputs a 0–100 strength index only.

## Fusion engine (complete)

| Phase | Status | Notes |
|-------|--------|-------|
| 1 calibrate + windows | done | `detect/calibrate.py`, `detect/windows.py` |
| 2 factors + BB ddof=1 | done | six factors; `pivots.py` sample std |
| 3 fusion + phase + result | done | `build_detection`, PRE/MID by CUSUM band |
| 4 replay harness | done | `_dev/replay_fusion.py` (no lookahead) |
| 5–7 live wiring | done | `tick_assembly` → fusion always-on; legacy scan/FSM deleted |
| 6 deep path | done | `detect/deep/*`; pinned `/signal` via `build_deep_report_from_lake` |
| 8 cleanup | done | `market_cycle.py`, `config.defaults.toml [fusion]`, pruned `UNIVERSAL_DEFAULTS` |

## Architecture (current)

```
features/lake → detect/live → detect/delivery_bridge → validate_signal_contract → gate/delivery → deliver/*
```

- **Detection:** self-calibrating fusion (`detect/*`) — no magic RSI/OI/funding thresholds.
- **Delivery:** preserved declarative gates (`gate/*`, `deliver/*`) — liquidity, EV, RR, contract.
- **Deep query:** `detect/deep` (any phase, no watch gate).

## Verification (automated)

| Check | Command |
|-------|---------|
| compile | `python -m py_compile $(find hunt_core -name '*.py')` |
| fusion smoke | `python -m hunt_core._dev.check_factors_fusion` |
| logic | `python -m hunt_core._dev.check_logic` |
| deep | `python -m hunt_core._dev.check_deep` |
| replay | `python -m hunt_core._dev.replay_fusion --all --q-gate 0.92 --walk-forward 0.3` |
| CCXT plane | `python -m hunt_core._dev.check_ccxt` |

## Ops (manual)

| Step | Status |
|------|--------|
| Supervised live session | `scripts/supervised_session.py` — run after deploy |
| Parity vs old stack | waived — legacy path removed; replay is sole offline metric |
| graphify update | run at repo root when refreshing architecture graphs |

## Removed (phase-7)

- `scan/{prepump,predump,presqueeze,early,predump_dump_hunt,scoring,routing,_confirm_shared,pump_cycle,detectors}.py`
- `regime/{leg_fsm,_lifecycle_assess,_lifecycle_sticky}.py`
- `HUNT_FUSION_ENGINE` flag (engine is always on)

## Preserved

- `gate/delivery.py` + declarative delivery stack (not detection scoring)
- `market/*`, `features/*`, `data/lake.py`, `contract.py`, `deliver/*`
