# Refactor Plan — Crypto Signal Bot v9

> Target: **Python 3.14.5**, lean architecture, live Binance validation only.  
> User docs: [README.md](../README.md) · Deps: [DEPENDENCIES.md](DEPENDENCIES.md) · Lock: [requirements-lock.txt](../requirements-lock.txt)  
> Status: **Structural refactor complete** (2026-05-31). Phase 0–3 done; Phase 4 = strategy calibration waves (ops).

## Context

- ~**174 Python files** in `bot/`, ~**49 at root** — AI-generated bloat.
- Unit tests (35) are **self-referential** — not proof of production behavior.
- **Truth tests** = live public Binance REST/WS + pipeline/strategy live scripts.

---

## 1. Platform & Dependencies

### Python

| Decision | Rationale |
|---|---|
| **3.14.5** (standard, not `3.14t`) | Active rewrite window; support until ~2030; Polars/numpy support 3.14 |
| Not 3.13 | No reason to preserve unstable AI baseline |
| Not nogil | Polars re-enables GIL; ecosystem immature on Windows |

### Stack — KEEP (aligned with similar OSS projects)

Research: [python-binance](https://github.com/sammchardy/python-binance), [open-binancian-futures](https://github.com/zionhann/open-binancian-futures), Corax-style Polars bots.

| Package | Role | Notes |
|---|---|---|
| **polars** + **polars_ta** | Feature pipeline | Keep; industry moving from pandas→polars for TA pipelines |
| **aiohttp** + **websockets** | REST + WS | Keep custom client — **public-only boundary** (reject python-binance/ccxt: private endpoints) |
| **aiogram** | Telegram delivery | Standard for async TG bots |
| **pydantic** | Config/contracts | 2.13+ |
| **structlog** | Logging | Keep |
| **msgspec** | Hot-path serialization | Keep |
| **tenacity** | Retry | Keep |
| **aiosqlite** | Persistence | Keep until Postgres needed |
| **numpy** | Minimal numeric helpers | Keep small surface |
| **fastapi** + **uvicorn** | Dashboard (optional) | Defer slim API in Phase 3 |

### Stack — DROP / DO NOT ADD

| Package | Why not |
|---|---|
| **python-binance** / **ccxt** | Private endpoints temptation; multi-exchange scope creep |
| **pandas** on live path | Polars-only; pandas only in `[ml]` extra if ever |
| **TA-Lib** | Windows/native brittle; polars_ta + pure Polars fallbacks |
| **Redis/Postgres** | Premature; aiosqlite enough until proven scale |

### Updated pins (PyPI 2026-05-31)

See `pyproject.toml` — latest stable for all core/live/dev/test deps.

---

## 2. Module Verdict Matrix

### 🗑 DELETE (Phase 1 — orphan / duplicate)

| Module | Lines | Reason |
|---|---:|---|
| `bot/features_shared.py.orig` | — | Merge artifact |
| `bot/autotuner.py` | 236 | Orphan; offline tuning never wired |
| `bot/config_loader.py` | 81 | Orphan; superseded by `domain/config.py` |
| `bot/dashboard_ui.py` | 15 | Orphan stub |
| `bot/telegram/` (4 files) | ~825 | Duplicate of `messaging.py` delivery path |
| `bot/monitor_bot.py` | 56 | Thin duplicate of `main.py` |

### 🗑 DELETE (Phase 2 — replace with slim modules)

| Module | Lines | Replace with |
|---|---:|---|
| `bot/signal_diagnostics.py` | 2281 | `bot/diagnostics/signals.py` (~200 LOC) + telemetry JSONL queries |
| `bot/startup_reporter.py` | 1395 | Startup summary in `cli.py` + one JSON report |
| `bot/config_audit.py` | 1298 | `scripts/validate_config.py` only |
| `bot/quality_monitor.py` | 1380 | Metrics in `telemetry.py` + dashboard tab |
| `bot/live_audit.py` | 758 | `scripts/live_check_*.py` suite |
| `bot/public_intelligence.py` | 1168 | Split: REST enrichments → `market/enrichment.py` |
| `bot/universe.py` | 1015 | `market/universe.py` (~300 LOC) |
| `bot/ws_manager.py` | 1844 | Merge into `bot/market/ws.py` |
| `bot/infrastructure/binance_client.py` | 2472 | Split: `market/rest.py` + rate limiter (~800 LOC total) |

### ✏️ REWRITE (Phase 2–3)

| Module | Action |
|---|---|
| `bot/features.py` + `features_*.py` | → `bot/features/` package: `core`, `trend`, `volatility`, `microstructure`, `prepare.py` |
| `bot/application/symbol_analyzer.py` | 1557 → orchestration only; move prep to features |
| `bot/strategies/spec_patterns.py` | 1613 → merged into `bot/strategies/*` + `_common` (no `setups/detectors/`) |
| `bot/core/memory/repository.py` | 2105 → split schema / queries / migrations |
| `bot/dashboard.py` + `dashboard_live.py` | Single `bot/dashboard/app.py` + static/ |
| `bot/tracking.py` + `outcomes.py` + `diary_store.py` + `journal.py` | → `bot/persistence/` |

### ✅ KEEP (core contract — refactor in place, don't delete)

| Module | Role |
|---|---|
| `bot/signal_contract.py` | Immutable delivery rules |
| `bot/confluence.py` | 3-of-5 gate |
| `bot/application/delivery_orchestrator.py` | Delivery path |
| `bot/core/engine/` | Strategy execution (simplify thread pool) |
| `bot/strategies/*.py` | 38 detectors (rewrite wave-by-wave) |
| `bot/domain/` | Config, schemas, contracts |
| `bot/websocket/` | Low-level WS helpers (merge with market/ later) |
| `scripts/live_check_*.py` | Production validation |

### Target layout (v9)

```
bot/
  domain/          # config, schemas, contracts, events
  market/          # rest, ws, cache, universe, enrichment  [NEW]
  features/        # polars pipeline                         [NEW]
  engine/          # strategy registry + executor            [MOVE from core/engine]
  strategies/      # 38 setup detectors
  setups/          # shared SMC / detector primitives
  delivery/        # contract, confluence, orchestrator, telegram  [NEW]
  persistence/     # sqlite, tracking, outcomes                [NEW]
  runtime/         # bot loop, kline handler, analyzers        [NEW from application/]
  dashboard/       # optional fastapi app
  telemetry.py
  cli.py
main.py
scripts/live_check_*.py
tests/live/        # Binance public API only
```

**Target:** ~80–100 Python files (from ~174).

---

## 3. Test Strategy

### Remove (done Phase 0)

All `tests/test_*.py` unit tests — contract/filter/rate-limiter tests were AI-self-referential.

### Keep / Add

| Test | Source |
|---|---|
| `tests/live/test_binance_public_api.py` | REST + WS smoke |
| `tests/live/test_binance_enrichments.py` | From `live_check_enrichments.py` |
| `tests/live/test_binance_indicators.py` | From `live_check_indicators.py` |
| `tests/live/test_binance_pipeline.py` | From `live_check_pipeline.py` |

All marked `@pytest.mark.live` — require network.

### CI

| Job | Runs |
|---|---|
| PR/push | ruff, compileall, validate_config |
| Nightly | `pytest -m live` against Binance public API |

---

## 4. Phased Implementation

### Phase 0 — Platform ✅

- [x] `requires-python >=3.14,<3.15`
- [x] Dependency pins updated (v9.0.0)
- [x] CI → Python 3.14, live tests only
- [x] Live-only test suite (`tests/live/`)
- [x] Delete orphan files (orig, autotuner, config_loader, dashboard_ui)
- [x] Remove 7 AI-self-referential unit tests
- [x] Python 3.14.5 venv + live pytest pass

### Phase 1 — De-bloat + v9 scaffold ✅ (2026-05-31)

Structural packages: `market/`, `runtime/`, `persistence/`, `engine/`, `features/`, `delivery/`, `dashboard/`, `diagnostics/`, `ops/`. Root `bot/` slim (~9 files). Legacy `application/`, `core/engine`, `core/memory`, `websocket/`, `telegram/` removed.

### Phase 1 — De-bloat + v9 scaffold (historical notes)

1. ~~Delete orphan modules~~ (done Phase 0)
2. ~~Remove `bot/telegram/`~~ — duplicate stack; live path uses `messaging.py`
3. ~~Fold `monitor_bot.HealthMonitor` → `application/health_manager.py`~~
4. **Create v9 package scaffold** with re-exports (no monolith moves yet):
   - `bot/market/` — REST/WS/universe/enrichment aliases
   - `bot/runtime/` — orchestration aliases from `application/`
   - `bot/persistence/` — tracking/outcomes/diary aliases
   - `bot/engine/` — aliases from `core/engine/`
   - `bot/features/` and `bot/delivery/` **deferred to Phase 2** (name clash with `features.py`, `delivery.py`)
5. Consolidate duplicate `metrics.py` / `health.py` / `alerts.py` names (rename by domain)
6. `graphify update .`

### Phase 2 — Data plane rewrite ✅ (structure, 2026-05-31)

1. ~~`bot/features/` package~~ (`prepare_cache`, `prepare_context`, `prepare_numeric`, `prepare_ws`, `prepare_sanity`; `_prepare_frame` still central)
2. ~~`bot/market/` (rest, ws, ws_lib, data, universe, enrichment)~~
3. Lazy Polars streaming on live path — **ongoing tuning**
4. Live checks: `tests/live/` + `scripts/live_check_*`

### Phase 3 — Runtime rewrite ✅ (structure, 2026-05-31)

1. ~~`bot/runtime/` (ex `application/`)~~
2. Slim `symbol_analyzer` (<400 LOC) — **partial** (entry 7 LOC; mixins split into `family_gates`, `ws_enrichments`, `context` ~106, `frames` ~180; `pipeline` ~558 remains)
3. Engine bounded concurrency — **ongoing**
4. Strategy lanes on kline hot path — **wired** (`enable_strategy_lanes`, `event_interval` from `cycle_runner` → `calculate_all`)
5. `live_smoke_bot` — manual / nightly

### Phase 4 — Strategies wave rewrite ✅ (wiring gate, 2026-05-31)

All **38** detectors registered; **5 waves** verified via `bot/strategies/catalog_spec.py` + `scripts/verify_refactor_gate.py`. Per-threshold live calibration remains ops.

Waves of 6–8 strategies (calibration loop):

1. Register + config enabled
2. Feature deps documented on `PreparedSymbol`
3. `live_check_strategies` pass
4. Telemetry rejection reasons clean

Order: structure family → momentum → microstructure → exotic (OI, funding, L/S).

### Phase 5 — Dashboard & ops (optional)

1. Single FastAPI app
2. Remove `quality_monitor`, `startup_reporter` markdown generators
3. Prometheus metrics only where used

---

## 5. Verification Commands

```powershell
# After every phase
python -m compileall -q bot
python scripts/validate_config.py --config config.toml

# Live truth (requires network)
pytest -m live -v
python -m scripts.live_check_strategies --limit 20 --concurrency 3
python -m scripts.live_smoke_bot --runtime-seconds 600
```

---

## 6. Risk Register

| Risk | Mitigation |
|---|---|
| pydantic 2.13 breaking config | `validate_config.py` first |
| mypy 2.x noise | Pin; fix incrementally per package |
| 3.14 not on dev machine | winget install Python 3.14 |
| Strategy zero-hits | Telemetry triage before threshold changes |
| Removing telegram/ breaks import | Grep + compileall before delete |

---

## 7. Execution priority (2026-06-02)

**Freeze monolith splits.** Further file splits (`prepare_*`, `analyzer/*`) increase count (~232 `.py` vs target ~85–95) without closing functional gaps. Do **not** add submodules until de-bloat lands.

| Order | Work | Status |
|---|---|---|
| 1 | **De-bloat** — delete/merge Phase 1–2 matrix orphans | Ongoing — `config_audit` 1299→233 LOC; `live_audit` 759→348 LOC |
| 2 | **P2** — benchmark anchors: stricter ACTION, XRP/PAXG in intelligence + `deep_analysis` | Partial — `anchor_action_score_delta` wired |
| 3 | **P1** — merger 4h direction conflict | Partial — merge + ledger recent_actions + conflict→WATCH |
| 4 | **P3** — dashboard funnel, public audit | Partial — REST + WS `funnel_update` / `ws_health` push |
| 5 | **Data plane** — MARKET_DATA_PRINCIPLES (semaphore, OI off hot path) | Partial |
| 6 | **Consolidation wave** — merge split modules back or delete elsewhere | Deferred |

Live verification (`PYTEST_LIVE`, smoke bot) when Binance reachable — use `[bot.network]` / `BINANCE_PROXY_URL` ([BINANCE_PROXY_RU.md](BINANCE_PROXY_RU.md)).
