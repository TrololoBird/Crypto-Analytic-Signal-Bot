# Canonical architecture (bot2 v9)

> Post-refactor snapshot: **2026-06-02**. Signal-only Binance USDⓈ-M public bot, Python 3.14, Polars pipeline.

## Package map

| Package | Role |
|---------|------|
| `bot/domain/` | Config (`BotSettings`), schemas, contracts, strategy catalog |
| `bot/market/` | REST, WS, universe, scheduler, enrichments |
| `bot/features/` | Polars feature pipeline (`prepare.py`, `prepare_frame`, `prepare_cache`, …) |
| `bot/engine/` | `SignalEngine`, strategy registry, lanes |
| `bot/strategies/` | **Single** strategy tree — 38 setup classes + spec detectors (`_common`, `_roadmap`) |
| `bot/setups/` | Shared primitives only: `base`, `spec_runtime`, `utils`, `smc` (no `detectors/` duplicate) |
| `bot/runtime/` | Bot loop, kline handler, symbol analyzer, delivery orchestrator |
| `bot/delivery/` | Contract validation, confluence, filters, trade plan, Telegram |
| `bot/persistence/` | SQLite tracking, outcomes, diary, public audit |
| `bot/diagnostics/` | Signals telemetry, config audit, runtime health |
| `bot/dashboard/` | Optional FastAPI + live audit / WS broadcast |
| `bot/cli.py`, `main.py` | Entry points |

**Scale:** ~**180** `.py` files under `bot/` (down from ~233 after dedup).

## Import rules

1. **New code** imports from v9 packages (`bot.market`, `bot.runtime`, …), not deleted legacy roots.
2. **Strategies** live only under `bot/strategies/`; wire via `SpecDetectorSetup` + `bot/setups/spec_runtime.py`.
3. **Forbidden** (enforced by `scripts/verify_refactor_gate.py`): `bot/application/`, `bot/telegram/`, `bot/websocket/`, `bot/infrastructure/`, `bot/core/engine`, root shims (`delivery.py`, `features.py`, `messaging.py`, `ws_manager.py`, …).
4. **Delivery path** (never bypass): `validate_signal_contract` → `hard_confluence_gate` (3-of-5) → `delivery.deliver`.
5. **Features** on hot path: Polars / `polars_ta`; no pandas; no `shift(-N)` on live bars.

## What was removed

| Removed | Replaced by |
|---------|-------------|
| `bot/setups/detectors/` (~40 duplicate modules) | Logic merged into `bot/strategies/*` + `_common` |
| `bot/application/`, `bot/telegram/`, `bot/websocket/` | `bot/runtime/`, `bot/delivery/telegram.py` |
| `bot/core/engine/`, `bot/core/memory/` | `bot/engine/`, `bot/persistence/` |
| Root monoliths: `signal_diagnostics.py`, `startup_reporter.py`, `config_audit.py`, `quality_monitor.py`, `live_audit.py`, `public_intelligence.py`, `universe.py`, `ws_manager.py`, `features.py`, `delivery.py`, … | Package modules + `scripts/validate_config.py`, `bot/diagnostics/`, `bot/market/` |
| Phase 0 orphans: `autotuner.py`, `config_loader.py`, `dashboard_ui.py`, `monitor_bot.py` | Deleted |

## Verification

```powershell
python -m compileall -q bot
python scripts/validate_config.py --config config.toml
python scripts/verify_refactor_gate.py
python -m pytest tests/ -q --ignore=tests/live
# Truth (network): PYTEST_LIVE=1 pytest tests/live/ -v
```

See also: [REFACTOR_PLAN.md](../REFACTOR_PLAN.md), [GAP_ANALYSIS_BOT2.md](GAP_ANALYSIS_BOT2.md).
