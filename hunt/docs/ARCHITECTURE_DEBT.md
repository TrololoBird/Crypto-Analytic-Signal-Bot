# hunt_core architecture debt (2026-06-20)

Target: standalone hunter per [HUNT_ARCHITECTURE.md](HUNT_ARCHITECTURE.md).

## Fusion cutover status

| Metric | Before fusion | After phase-7f/8 |
|--------|---------------|------------------|
| `.py` files in `hunt_core/` | ~113 | **~105** |
| hot path LOC | ~44k budget | **~36k** |
| Detection | `scan/*` + `regime/leg_fsm` (~8k LOC) | **`detect/*` fusion engine** |
| Legacy FSM lifecycle | `regime/leg_fsm.py` | **removed** — phase from fusion CUSUM band |
| `hunt_research/` | logic_verify offline | **removed** — `_dev/check_*` + `replay_fusion` |

LOC budget gate: **44 000** hot path (`_dev/budget.py` excludes `_dev/`, `cycle/_impl.py`, `_dump_core.py`).  
Stretch target **32 000** — wave 4 (`cycle/_impl`, `gate/delivery`, `deliver/telegram` splits).

## God modules — keep whole until split

| Module | LOC | Policy |
|--------|-----|--------|
| `runtime/cycle/_impl.py` | ~2556 | watch loop |
| `runtime/tick_assembly.py` | ~1200 | tick orchestration |
| `detect/fusion.py` + `factors.py` | ~800 | fusion core |
| `gate/delivery.py` | ~2072 | delivery |
| `gate/_delivery_helpers.py` | ~400 | delivery evidence helpers |
| `deliver/telegram.py` | ~2477 | TG |
| `track/tracker.py` | ~1848 | FSM |
| `levels/levels.py` | ~1212 | SL/TP geometry |
| `features/snapshot.py` | ~1125 | TF snapshot assembly |

## Open (wave 4 candidates)

| Item | Notes |
|------|-------|
| hot LOC 36k → 32k | further cycle/_impl + gate/delivery splits |
| dual deep path | `detect/deep/*` vs `analysis/deep/build.py` — consolidate |
| `data/lake.py` | **done** — feature parquet + tracker/tick batch flush |
| config unification | **done** — `domain/config.py` TOML + `[fusion]` section; `params/store.py` calibration overlay |
| scanner threshold drift | `data/scanner.py` hardcodes 45/60 — should read `[scanner]` from settings |
| Long TG | off until n≥30 |

## Verification

```bash
# repo root, after pip install -e "./hunt"
python -m compileall -q hunt/hunt_core
python -m hunt_core._dev.budget
python -m hunt_core._dev.check_factors_fusion
python -m hunt_core._dev.check_scenarios
python -m hunt_core._dev.check_logic
python -m hunt_core._dev.replay_fusion --all --q-gate 0.92
python -m hunt_core._dev.authority_audit
python -m hunt_core._dev.smoke_signals --baseline data/baseline/hunt_baseline.json BTCUSDT
python -m hunt_core._dev.watch_once_smoke BTCUSDT --timeout 180
scripts/supervised_session.py --hours 0.25 --watch-interval 30
```
