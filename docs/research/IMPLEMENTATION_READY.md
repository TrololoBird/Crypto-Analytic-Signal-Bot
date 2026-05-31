# Implementation ready gate

**Status: PR1–PR10 structural gate PASSED** (2026-05-31)

Structural refactor and catalog wiring are enforced by `scripts/verify_refactor_gate.py` and `tests/live/test_strategy_catalog_wiring.py`.

**Not in scope of “complete”:** continuous per-strategy threshold calibration (ops loop: `live_check_strategies`, `config/strategies/*.toml`). Wave calibration 2026-05-31: spec detectors + 23 strategy TOMLs; `wyckoff_spring` / `ema_bounce` / `bos_choch` verified on live slice.

---

## PR checklist (TARGET_REPOSITORY_LAYOUT §7)

| PR | Scope | Status |
|----|--------|--------|
| **PR1** | Phase A deletes, `bot/__init__` → `runtime.bot`, scripts imports | ✅ |
| **PR2** | `application/*` → `runtime/*`, delete `application/` | ✅ |
| **PR3** | `core/engine` → `engine/`, `lanes.py` | ✅ |
| **PR4** | Market bodies, delete ws_manager/binance_client/public_intelligence root | ✅ |
| **PR5** | P0 scheduler + screener + WS union | ✅ |
| **PR6** | `delivery/` package + merge + tiers + watch | ✅ |
| **PR7** | `features/` package | ✅ |
| **PR8** | `persistence/repository` schema split | ✅ (queries in `memory.py` optional) |
| **PR9** | `dashboard/` + diagnostics slim | ✅ (`signals.py` ~600 LOC; quality in `diagnostics/quality.py`) |
| **PR10** | Strategy waves 1–5 wiring (38 detectors) | ✅ `catalog_spec.py` + gate tests |

---

## Phases (REFACTOR_PLAN)

| Phase | Status |
|-------|--------|
| 0 Platform | ✅ |
| 1 De-bloat + scaffold | ✅ |
| 2 Data plane structure | ✅ (`rest.py` still large; rate_limit extracted) |
| 3 Runtime structure | ✅ (`symbol_analyzer` entry slim + `analyzer/*`) |
| 4 Strategy catalog | ✅ wiring; calibration ongoing |
| 5 Dashboard/ops | ✅ optional app; quality monitor retained |

---

## Verification

```powershell
python -m compileall -q bot
python scripts/validate_config.py --config config.toml
python scripts/verify_refactor_gate.py
$env:PYTEST_LIVE=1; pytest tests/live/ -v
make check
```

Pipeline: `validate_signal_contract` → `hard_confluence_gate` → `delivery.deliver`.
