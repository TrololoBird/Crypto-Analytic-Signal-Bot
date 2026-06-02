# Lanes hot-path execution (2026-06-01)

**Goal:** Close GAP P0 — 8–15 strategy families per kline event (not 38× `detect`).

## Done

- [x] `bot/engine/lanes.py` — family cap, interval filter, `apply_interval_filter`
- [x] `bot/engine/engine.py` — `_route_strategies`, `calculate_all(..., event_interval=)`
- [x] `bot/runtime/cycle_runner.py` → `kline_interval` into analysis
- [x] `runtime.enable_strategy_lanes` in `domain/config.py` + `config.toml`
- [x] Unit tests: `tests/test_engine_lanes.py`, `tests/test_engine_routing.py`
- [x] Docs: GAP_ANALYSIS, REFACTOR_PLAN Phase 3, polish plan Task 1/2 partial

## Deferred (next waves)

- [x] Split `bot/features/prepare.py` — `prepare_frame`, `prepare_cache`, `prepare_context`, `prepare_numeric`, `prepare_ws`, `prepare_sanity`; `prepare.py` ~300 LOC orchestration
- [x] Slim `runtime/analyzer/*` — `family_gates.py`, `ws_enrichments.py`; `context.py` ~106, `frames.py` ~180 (pipeline still ~558)
- [ ] Live smoke 10–20 min INFO — **blocked without VPN** (Binance unreachable); run manually when VPN on
- [x] `PYTEST_LIVE=1 pytest tests/live/` — passed when network OK (do not run without VPN)

## Override

`route_all_enabled_strategies = true` bypasses lanes (debug only).
