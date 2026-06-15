# hunt_core architecture debt (2026-06-15)

Target: standalone hunter per [HUNT_ARCHITECTURE.md](HUNT_ARCHITECTURE.md).

## Wave 3 status (post-redesign cutover)

| Metric | Before wave 2 | After cutover |
|--------|---------------|---------------|
| `.py` files in `hunt_core/` | 137 | **~113** |
| hot path LOC | 43 626 | **~44k budget** |
| `detect/engine.py` | 3441 LOC | **split** → `predump`/`prepump`/`early` + `_confirm_shared` (wave 3C done) |
| `detect/lifecycle.py` | 1623 LOC | **`regime/leg_fsm.py`** (canonical) |
| `hunt_research/` | logic_verify offline | **removed** — `_dev/check_*` + live-smoke |
| `hunt/scripts/` | ~50 ops scripts | **removed** |

LOC budget gate: **44 000** hot path (`_dev/budget.py` excludes `_dev/`, `cycle/_impl.py`, `_engine_impl.py`, `_dump_core.py`).  
Stretch target **32 000** — needs full physical split into predump/prepump/presqueeze (wave 3C).

## God modules — keep whole until split

| Module | LOC | Policy |
|--------|-----|--------|
| `runtime/cycle/_impl.py` | ~2556 | watch loop |
| `scan/_engine_impl.py` | ~15 | compat facade only |
| `scan/predump.py` | ~340 | dump confirm path |
| `scan/prepump.py` | ~260 | long confirm path |
| `scan/_confirm_shared.py` | ~1150 | shared fuel/confirm helpers |
| `scan/early.py` | ~1000 | adaptive + early alerts |
| `regime/leg_fsm.py` | ~1615 | lifecycle FSM |
| `gate/delivery.py` | ~2072 | delivery |
| `deliver/telegram.py` | ~2477 | TG |
| `track/tracker.py` | ~1848 | FSM |
| `levels/levels.py` | ~1212 | SL/TP geometry |
| `features/snapshot.py` | ~1125 | TF snapshot assembly |

## Open (wave 3 candidates)

| Item | Notes |
|------|-------|
| hot LOC 44k → 32k | further cycle/_impl + gate/delivery splits (wave 4) |
| `data/lake.py` | **done** — feature parquet + tracker/tick batch flush |
| config unification | **done** — `domain/config.py` canonical; shims for `config_defaults` + `runtime/settings` |
| Long TG | off until n≥30 |

## Verification

```bash
cd hunt && PYTHONPATH=.
python -m compileall -q hunt_core
python -m hunt_core._dev.budget
python -m hunt_core._dev.check_scenarios
python -m hunt_core._dev.check_logic
python -m hunt_core._dev.smoke_signals --baseline data/baseline/hunt_baseline.json BTCUSDT
python -m hunt_core._dev.watch_once_smoke BTCUSDT --timeout 180
```
