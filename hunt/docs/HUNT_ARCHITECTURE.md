# Hunt architecture (canonical)

Standalone **crypto-hunter** package (`hunt/`, import `hunt_core`). No `engine.*`, no `bot.*`, no `hunt/scripts/`.

## Product — three runtime modes

| Mode | Trigger | Pipeline branch | Telegram |
|------|---------|-----------------|----------|
| **Hunt scan** | `python -m hunt_core watch` every N sec | fuel → lifecycle → confirm short/long | ARMED / TRIGGERED confirm |
| **Deep analysis** | pinned tick + `/signal SYM` | Prizrak POC, PP, MTF panel, liquidity | Brief + 2 scenarios |
| **Catalog** | `/signals [SYMS]` | 7 setup detectors + probability | Setup list + tracker block |

**North star (short):** gate_edge hold-to-target SL ≤30%, TP1+ ≥50% (n≥30). Long TG off by default until n≥30.

## Metrics (wave 2, 2026-06-14)

| Metric | Value |
|--------|-------|
| `hunt_core/` `.py` files | **~88** (was 137) |
| hot path LOC | **~43k** (budget gate 43k; stretch 32k) |
| `hunt_research/` | logic_verify + calibrate offline |

## Package layout

```
hunt/
├── hunt_core/              # production hot path (~88 .py)
│   ├── __main__.py         # python -m hunt_core [watch|verify]
│   ├── contract.py         # trade plan + feature payload + delivery contract
│   ├── market/             # factory, client, network, cross, streams (9 files)
│   ├── data/               # collect, universe, lake, completeness (5 files)
│   ├── features/           # prepare, prepare_frame, prepare_columns, levels (~14 files)
│   ├── regime/             # leg_fsm (canonical lifecycle), regime.py
│   ├── levels/             # levels.py (canonical SL/TP builder)
│   ├── confluence/         # MTF family-vote + must-pass
│   ├── scan/               # _engine_impl, predump/prepump/presqueeze, scanner
│   ├── gate/               # delivery, policy (3 files)
│   ├── deliver/            # dispatch, telegram, digest (4 files)
│   ├── track/              # tracker, events, outcomes, …
│   ├── analysis/           # pinned_deep, deep_signal, adx_thresholds, trend_engine
│   ├── setups/             # detectors, catalog
│   ├── runtime/            # cycle/_impl, settings, telegram_commands
│   ├── domain/             # config, schemas, policy, regime, limit_entry
│   └── calibrate/          # verify.py only (CLI → hunt_research.logic_verify)
├── hunt_research/          # logic_verify, calibrate/, labels, dollar_bars
├── docs/
└── data/
```

## Hot path (one watch tick)

```
run_loop → market.factory → data.collect.snapshot_symbol
  → features.prepare → scan._engine_impl + regime.leg_fsm
  → track (prep_shadow, candidates, reconcile)
  → [confirmed] gate.delivery → deliver.dispatch → deliver.telegram
  → [pinned] analysis.pinned_deep + deep_signal
```

### Delivery invariant

```
validate_signal_contract(setup) → evaluate_delivery (gate) → telegram.send
```

## Entrypoints

```bash
python -m hunt_core watch --interval 60
python -m hunt_core watch --once --no-telegram
python -m hunt_core verify
python -m hunt_core._dev.budget
```

## Merge policy (post-redesign 2026-06-15)

| File | LOC | Policy |
|------|-----|--------|
| `runtime/cycle/_impl.py` | ~2500 | watch loop |
| `scan/_engine_impl.py` | ~3400 | scanner core (shim: `detect/engine.py`) |
| `regime/leg_fsm.py` | ~1600 | lifecycle FSM |
| `levels/levels.py` | ~1200 | SL/TP geometry |
| `gate/delivery.py` | ~2100 | delivery gates |
| `deliver/telegram.py` | ~1800 | transport + formatters |
| `track/tracker.py` | ~1800 | FSM |

Merge small modules into fewer files; never split these.

## Library-first

`polars` + `polars_ta` + `polars-ols` + `polars-ds` + `polars-trading` + `bottleneck` + `ccxt`. See [LIBRARY_STACK.md](LIBRARY_STACK.md).

## Forbidden

- `from engine.*` / `from bot.*`
- `hunt/scripts/`
- Auto-trading, private Binance auth
- Indicator fallback chains on hot path

## Hunt setup catalog (7 detectors)

Metadata in `hunt_core/setups/catalog.py` — `HUNT_SETUP_IDS` + `HUNT_SETUP_META`.
