# Hunter — target architecture (H-B rewrite)

> **Status:** redesign cutover (2026-06-15).  
> **Package:** `hunt_core/` (~113 files, ~44k hot LOC budget).  
> **Product:** [HUNT_PRODUCT_DEFINITION.md](HUNT_PRODUCT_DEFINITION.md)

Canonical detail: [HUNT_ARCHITECTURE.md](HUNT_ARCHITECTURE.md).

## Production loop

```
ingest → features → scan → regime → gate → deliver → track → outcomes
```

Offline: `hunt_core/_dev/check_*` + `check_logic` + CI live-smoke (no separate `hunt_research/` package).

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
| scan | `_engine_impl.py` (facade), `routing.py`, `predump.py`, `prepump.py`, `presqueeze.py`, `early.py`, `scanner.py` |
| regime | `leg_fsm.py`, `regime.py` |
| levels | `levels.py` (canonical SL/TP) |
| confluence | `confluence.py`, `mtf.py` |
| gate | `delivery.py`, `policy.py` |
| deliver | `dispatch.py`, `telegram.py`, `templates.py` |
| track | `tracker.py`, `events.py`, `outcomes.py`, `candidates.py` |
| analysis | `pinned_deep.py`, `deep_signal.py`, `confluence_grid.py` |
| setups | `catalog.py`, `scan/detectors.py` |
| domain | `config.py`, `setup_registry.py`, `schemas.py` |
| dev gates | `_dev/budget`, `check_*`, `check_logic`, `smoke_signals`, `watch_once_smoke` |

## Entrypoints

```bash
python -m hunt_core watch --interval 60
python -m hunt_core watch --once --no-telegram --symbols BTCUSDT
python -m hunt_core._dev.smoke_signals --baseline data/baseline/hunt_baseline.json BTCUSDT ETHUSDT
```

No `hunt/scripts/`. No `engine.*` / `bot.*` imports.
