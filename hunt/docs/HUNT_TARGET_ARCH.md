# Hunter — target architecture (H-B rewrite)

> **Status:** wave 2 shrink complete (2026-06-14).  
> **Packages:** `hunt_core/` (~88 files, ~43k hot LOC) · `hunt_research/` (verify + calibrate offline).  
> **Product:** [HUNT_PRODUCT_DEFINITION.md](HUNT_PRODUCT_DEFINITION.md)

Canonical detail: [HUNT_ARCHITECTURE.md](HUNT_ARCHITECTURE.md).

## Production loop

```
ingest → features → detect → gate → deliver → track → outcomes
```

Offline: `hunt_research/logic_verify.py`, `hunt_research/calibrate/*`.

## Contracts

Single module: [hunt_core/contract.py](../hunt_core/contract.py)

- Trade plan: `validate_signal_contract`, `TrackerFeatureVector`
- Feature payload: `PUBLIC_FEATURE_FIELDS`, `build_setup_delivery_contract`
- TickRow / SignalRecord / OutcomeRecord TypedDicts

## Module map (`hunt_core/`)

| Layer | Modules |
|-------|---------|
| entry | `__main__.py` → `watch` \| `verify` |
| runtime | `runtime/cycle/_impl.py`, `settings.py`, `telegram_commands.py` |
| data | `collect.py`, `universe.py`, `lake.py`, `completeness.py` |
| market | `factory.py`, `client.py`, `network.py`, `cross.py`, `streams.py` |
| features | `prepare.py`, `prepare_frame.py`, `prepare_columns.py`, `levels.py` |
| detect | `engine.py`, `lifecycle.py`, `setup_candidates.py` |
| gate | `delivery.py`, `policy.py` |
| deliver | `dispatch.py`, `telegram.py` |
| track | `tracker.py`, `events.py`, `outcomes.py`, … |
| analysis | `pinned_deep.py`, `deep_signal.py` |
| setups | `detectors.py`, `catalog.py` |
| domain | `config.py` (HuntSettings), `schemas.py`, `policy.py` |
| verify | `calibrate/verify.py` → `hunt_research.logic_verify` |

## Entrypoints

```bash
python -m hunt_core watch --interval 60
python -m hunt_core verify
```

No `hunt/scripts/`. No `engine.*` / `bot.*` imports.
