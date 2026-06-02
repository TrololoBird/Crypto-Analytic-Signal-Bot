# Full Polish Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the repository-wide refactor to the target v9 architecture and strategy model from research specs, then run verification as a final stage.

**Architecture:** Execute in four dependent subprojects: P0 core wiring, P1 delivery pipeline, P2 strategy rewrites, P3/P5 persistence-dashboard-ops cleanup. Keep the non-negotiable signal delivery chain and public-only Binance boundary intact while removing remaining legacy structure drift.

**Tech Stack:** Python 3.14, asyncio, Polars, pydantic, aiohttp, websockets, aiogram, FastAPI.

---

## File Structure (execution map)

- `bot/runtime/*` — orchestration and scheduler flow only.
- `bot/market/*` — public market data ingestion, shortlist, timeframe union.
- `bot/engine/*` — registry + bounded-concurrency execution + lanes.
- `bot/delivery/*` — contract, confluence, scoring, tiers, trade plan, telegram delivery.
- `bot/strategies/*` + `bot/setups/detectors/*` — detector logic aligned to strategy catalog semantics.
- `bot/persistence/*` + `bot/dashboard/*` + `bot/diagnostics/*` — tracking/storage/ops surface.
- `scripts/*` and imports — migrated to final package paths.

---

### Task 1: P0 Core Wiring (runtime + market + lanes)

**Files:**
- Modify: `bot/runtime/kline_handler.py`
- Modify: `bot/runtime/bot.py`
- Modify: `bot/runtime/symbol_analyzer.py`
- Modify: `bot/market/ws.py`
- Modify: `bot/market/universe.py`
- Modify: `bot/market/scheduler.py`
- Modify: `bot/engine/lanes.py`
- Modify: `bot/domain/config.py`

- [ ] Step 1: Add/normalize config types for lanes, shortlist target, and interval policy in `bot/domain/config.py`.
- [x] Step 2: Implement lanes selection API in `bot/engine/lanes.py` for 8-15 strategy families per symbol/event.
- [ ] Step 3: Update `bot/market/universe.py` to produce shortlist (40-55) + pinned anchors.
- [ ] Step 4: Update `bot/market/ws.py` and `bot/market/scheduler.py` to compute interval union from enabled setups.
- [x] Step 5: Rewrite `bot/runtime/kline_handler.py` routing to trigger only strategies matching `trigger_tf`.
- [ ] Step 6: Slim `bot/runtime/symbol_analyzer.py` to orchestration boundary (prepare -> run -> rank/deliver).

---

### Task 2: P1 Delivery Pipeline (merge + tiers + trade plan)

**Files:**
- Create/Modify: `bot/runtime/merge.py`
- Modify: `bot/runtime/delivery_orchestrator.py`
- Modify: `bot/delivery/tiers.py`
- Modify: `bot/delivery/trade_plan.py`
- Modify: `bot/delivery/deliver.py`
- Modify: `bot/delivery/telegram.py`
- Modify: `bot/alerts.py` (if still participates in tier routing)

- [x] Step 1: Implement `MetaSignalMerger` in `bot/runtime/merge.py` with one ACTION per symbol/direction/window behavior.
- [ ] Step 2: Unify trade plan builder behavior in `bot/delivery/trade_plan.py` for zone/SL/TP/TTL/invalidation.
- [x] Step 3: Finalize WATCH/ACTION caps policy in `bot/delivery/tiers.py`.
- [x] Step 4: Rewire `bot/runtime/delivery_orchestrator.py` to call merge -> contract -> confluence -> tiers -> deliver.
- [x] Step 5: Ensure `bot/delivery/deliver.py` and `bot/delivery/telegram.py` use final payload format and rate-limit-safe queueing.

---

### Task 3: P2 Strategy Rewrites (logic alignment, no calibration)

**Files:**
- Modify: `bot/strategies/*.py` (wave-by-wave)
- Modify: `bot/strategies/catalog_spec.py`
- Modify: `bot/setups/detectors/*.py` (shared primitives where required)

- [ ] Step 1: Build rewrite waves (6-8 strategies per wave) grouped by family: structure/trend/breakout/reversal/micro/positioning/cross.
- [ ] Step 2: For each strategy, enforce `trigger_tf`, `pattern_tf`, `required_tfs` semantics from catalog.
- [ ] Step 3: Move duplicated detection fragments into `bot/setups/detectors/*` when shared across strategies.
- [ ] Step 4: Keep threshold values as current defaults unless hard-coded values violate catalog semantics (logic-first, not calibration).
- [ ] Step 5: Update `bot/strategies/catalog_spec.py` metadata to match rewritten detector contracts.

---

### Task 4: P3/P5 Persistence + Dashboard + Ops cleanup

**Files:**
- Modify: `bot/persistence/repository/*` or split where still monolithic
- Modify: `bot/persistence/tracking.py`, `bot/persistence/journal.py`, `bot/persistence/outcomes.py`
- Modify: `bot/dashboard/app.py`, `bot/dashboard/live.py`, `bot/dashboard/ws_broadcast.py`
- Modify: `bot/diagnostics/signals.py`
- Modify: `scripts/live_check_*.py` import paths
- Delete legacy-only files identified by final grep after migrations

- [ ] Step 1: Finish repository split boundaries (schema/queries/migrations hook) if any mixed responsibilities remain.
- [ ] Step 2: Ensure journal/tracking writes are called in correct order around delivery emission.
- [ ] Step 3: Consolidate dashboard entry into `bot/dashboard/app.py` and remove stale duplicate flow.
- [ ] Step 4: Slim diagnostics signal funnel to current runtime/delivery contracts.
- [ ] Step 5: Remove leftover legacy modules only after import migration is complete.

---

### Task 5: Final Integration + Verification Pass (deferred until requested)

**Files:**
- N/A (execution commands + final cleanup)

- [ ] Step 1: `python -m compileall -q bot`
- [ ] Step 2: `python scripts/validate_config.py --config config.toml`
- [ ] Step 3: `python scripts/verify_refactor_gate.py`
- [ ] Step 4: `$env:PYTEST_LIVE=1; pytest tests/live/ -v`
- [ ] Step 5: `graphify update .`

---

## Self-Review

- Spec coverage: includes TARGET_REPOSITORY_LAYOUT, TARGET_ARCHITECTURE, STRATEGY_CATALOG, and REFACTOR_PLAN operational finish.
- Placeholder scan: no TBD/TODO markers in tasks.
- Type consistency: each task targets concrete package boundaries and final import directions.
