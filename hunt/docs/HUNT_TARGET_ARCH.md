# Hunter — target architecture (H-B rewrite)

> **Status:** fusion cutover complete (2026-06-20).  
> **Package:** `hunt_core/` (~105 files, ~36k hot LOC).

Canonical detail: [HUNT_ARCHITECTURE.md](HUNT_ARCHITECTURE.md).

## Production loop

```
ingest → features/lake → detect/fusion → delivery_bridge → gate → deliver → track → outcomes
```

Offline: `hunt_core/_dev/check_*` + `check_logic` + `replay_fusion` + CI live-smoke.

## Contracts

Single module: [hunt_core/contract.py](../hunt_core/contract.py)

- Trade plan: `validate_signal_contract`, `TrackerFeatureVector`
- Feature payload: `PUBLIC_FEATURE_FIELDS`, `build_setup_delivery_contract`
- TickRow / SignalRecord / OutcomeRecord TypedDicts

## Module map (`hunt_core/`)

| Layer | Modules |
|-------|---------|
| entry | `__main__.py` → `watch` |
| runtime | `runtime/cycle/_impl.py`, `runtime/state.py`, `tick_assembly.py`, `telegram_commands.py` |
| data | `collect.py`, `universe.py`, `completeness.py`, `lake.py`, `scanner.py` |
| market | `factory.py`, `client.py`, `network.py`, `cross.py`, `streams.py` |
| features | `prepare_frame.py`, `snapshot.py`, `factors.py`, `structure.py`, `fib.py` |
| detect | `calibrate.py`, `windows.py`, `factors.py`, `fusion.py`, `phase.py`, `result.py`, `live.py`, `delivery_bridge.py`, `routing.py`, `market_cycle.py`, `deep/*` |
| scan | `scanner.py` (shim → `detect/routing`) |
| levels | `levels.py` (canonical SL/TP) |
| confluence | `confluence.py`, `mtf.py` |
| gate | `delivery.py`, `policy.py`, `_delivery_helpers.py` |
| deliver | `dispatch.py`, `telegram.py`, `templates.py` |
| track | `tracker.py`, `events.py`, `outcomes.py`, `candidates.py` |
| analysis | `pinned_deep.py`, `deep_signal.py`, `confluence_grid.py` |
| setups | `catalog.py`, `detectors.py` |
| domain | `config.py`, `schemas.py`, `policy.py` |
| dev gates | `_dev/budget`, `check_*`, `check_logic`, `replay_fusion`, `smoke_signals`, `watch_once_smoke` |

## Entrypoints

```bash
python -m hunt_core watch --interval 60
python -m hunt_core watch --once --no-telegram --symbols BTCUSDT
python -m hunt_core._dev.replay_fusion --all --q-gate 0.92
python -m hunt_core._dev.smoke_signals --baseline data/baseline/hunt_baseline.json BTCUSDT ETHUSDT
scripts/supervised_session.py --hours 8 --watch-interval 30
```

No `hunt/scripts/` on hot path. No `engine.*` / `bot.*` imports.
