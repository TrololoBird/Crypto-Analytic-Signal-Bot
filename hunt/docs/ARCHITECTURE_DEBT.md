# hunt_core architecture debt (2026-06-14)

Target: standalone hunter per [HUNT_ARCHITECTURE.md](HUNT_ARCHITECTURE.md).

## Wave 2 results

| Metric | Before | After wave 2 |
|--------|--------|--------------|
| `.py` files in `hunt_core/` | 137 | **88** |
| hot path LOC | 43 626 | **~43 000** |
| `calibrate/` in core | 13 files | **2** (`verify.py` + `__init__.py`) |
| `detect/` | 8 | **4** |
| `data/` | 16 | **5** |
| `logic_verify` | in core (3755 LOC) | **`hunt_research/logic_verify.py`** |

LOC budget gate: **43 000** hot path (`_dev/budget.py` excludes `calibrate/`, `_dev/`, `cycle/_impl.py`).  
Stretch target **32 000** — needs engine trim + further feature cuts (no god-module splits).

## God modules — keep whole

| Module | LOC | Policy |
|--------|-----|--------|
| `runtime/cycle/_impl.py` | ~2533 | watch loop |
| `detect/engine.py` | ~3422 | scoring + early + adaptive |
| `data/collect.py` | ~2300 | ingest |
| `gate/delivery.py` | ~2070 | delivery |
| `deliver/telegram.py` | ~1800 | TG |
| `track/tracker.py` | ~1844 | FSM |
| `features/prepare_frame.py` | ~1200 | polars_ta orchestration |

## Open (wave 3 candidates)

| Item | Notes |
|------|-------|
| hot LOC 43k → 32k | trim `engine.py` dead paths; levels/volume_profile library eval |
| `logic_verify` size | stays in `hunt_research/` — not hot path |
| Long TG | off until n≥30 |

## Verification

```bash
python -m compileall -q hunt/hunt_core hunt/hunt_research
python -m hunt_core verify
python -m hunt_core._dev.budget
python -m hunt_core watch --once --no-telegram
```
