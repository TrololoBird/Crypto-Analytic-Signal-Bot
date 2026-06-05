# План улучшений по существующим пакетам `bot/`

> Опора: [ARCHITECTURE_CANONICAL.md](research/ARCHITECTURE_CANONICAL.md), [REFACTOR_PLAN.md](REFACTOR_PLAN.md).  
> Детальный аудит по файлам: [PY_FILE_AUDIT_NEW_FINDINGS.md](PY_FILE_AUDIT_NEW_FINDINGS.md).  
> **Принцип:** дорабатывать действующие модули, не плодить параллельные деревья (`application/`, `telegram/`, `websocket/` запрещены gate).

---

## Статус внедрения (2026-06-04)

| Пакет | Сделано в коде | Следующий шаг |
|-------|----------------|---------------|
| `diagnostics/` | `assess_radar_store` → `runtime/health.py`; audit `[universe.radar]`; `live_watch` индексирует radar JSONL | F12 split `quality.py` |
| `runtime/` | ✅ reconnect→shortlist; emergency radar throttle; Prometheus radar | F12 `pipeline`, `delivery_orchestrator`, `bot.py` |
| `market/` | `MarketRadarStore.iter_states()`; depth_source в `ws_cache` | F12 `ws.py`, `rest_impl.py`, warm RSI REST |
| `dashboard/` | ✅ `/api/radar/summary` + operator health | — |
| `domain/` | `send_radar_watch_candidate` | — |
| `ops/` | `startup_report` + `radar_watch` count | — |
| `strategies/` | fix `price_velocity` imports | калибровка по matrix (38 файлов) |
| `config_audit` | radar + operator DM consistency | — |

Удалены отдельные файлы: `diagnostics/radar_health.py`, `runtime/radar_watch.py` (логика в существующих модулях).

---

## `bot/market/` — data plane

**Роль:** REST, WS, universe, radar funnel, subscriptions.

| Приоритет | Действие | Модуль |
|-----------|----------|--------|
| P0 | ✅ tier ingest + promotion + screener | `radar_state`, `universe_screener`, `promotion_engine` |
| P0 | ✅ aggTrade priority HOT/DEEP | `subscription_planner`, `bot._sync_ws_tracked_symbols` |
| P1 | REST weight guard для warm 1h RSI | `universe.py`, `rate_limit.py` |
| P1 | `depth_source` на L1 fallback | `ws_enrichment.py` → `PreparedSymbol` |
| P2 | hot-only `@kline_1m` | `ws_subscriptions.py`, `ws.py` |
| F12 | split | `ws.py` (1741 LOC), `rest_impl.py` (1646), `enrichment.py` (1131), `universe.py` (1226) |

---

## `bot/runtime/` — orchestration

**Роль:** bot loop, shortlist, cycles, delivery orchestration, health.

| Приоритет | Действие | Модуль |
|-----------|----------|--------|
| P0 | ✅ radar health/watch wiring | `shortlist_service`, `watch_escalation`, `health_manager` |
| P0 | ✅ radar_promoted data readiness | `data_readiness.py`, `analyzer/pipeline.py` |
| P0 | ✅ intra_candle 0.5× throttle radar | `intra_candle_scanner.py` |
| P0 | ✅ reconnect → shortlist resync | `bot.py` `_on_reconnect` |
| P0 | ✅ emergency radar 2× interval | `cycle_runner.py` |
| P0 | ✅ radar Prometheus gauges | `metrics.py`, `health_manager.py` |
| F12 | split | `pipeline.py`, `delivery_orchestrator.py`, `shortlist_service.py`, `market_context_updater.py`, `bot.py` |

---

## `bot/delivery/` — gates (never bypass)

**Роль:** contract → confluence → deliver; Telegram; WATCH funnel.

| Приоритет | Действие | Модуль |
|-----------|----------|--------|
| P0 | invariant path only | `deliver.py`, `delivery_orchestrator.py` |
| P0 | ✅ `enabled_filter_stages` в config_audit | `filter_stages.py`, `config_audit.py` |
| P1 | DM rate limit radar + escalation | `telegram.py`, `telegram_routing.py` |
| F12 | split | `filters.py`, `formatting.py` |

---

## `bot/features/` — Polars hot path

| Приоритет | Действие | Модуль |
|-----------|----------|--------|
| P0 | canonical `prepare_frame.py` | все strategies |
| P1 | microstructure → prepare_frame only | `microstructure.py`, `prepare.py` |
| P1 | cache key + OI revision | `prepare.py` |
| F12 | split | `prepare_frame.py` (1417 LOC) |

---

## `bot/engine/` + `bot/strategies/` + `bot/setups/`

| Приоритет | Действие | Модуль |
|-----------|----------|--------|
| P0 | 38 setups via registry only | `engine/registry.py`, `strategies/__init__.py` |
| P0 | ✅ `price_velocity` import order | `strategies/price_velocity.py` |
| P1 | merge `strategies/common.py` → `_common.py` | 4 strategies |
| P1 | SMC single library | `setups/smc.py` + `order_block`, `fvg`, `liquidity_sweep` |
| P2 | per-setup calibration | `scripts/strategy_shortlist_matrix.py` |
| F12 | split | `bos_choch.py`, `setups/__init__.py`, `setups/smc.py` |

---

## `bot/persistence/` — SQLite + tracking

| Приоритет | Действие | Модуль |
|-----------|----------|--------|
| P0 | JSON-safe `benchmark_context` | `repository/memory.py` (regime_frame dict) |
| P1 | queries → `repository/queries/` | `memory.py` (1825 LOC) |
| P1 | tracking backlog alert | `tracking.py` (1853 LOC) |
| F12 | split | `tracking.py`, `memory.py`, `outcomes.py` |

---

## `bot/diagnostics/` — telemetry & audit

| Приоритет | Действие | Модуль |
|-----------|----------|--------|
| P0 | ✅ `assess_radar_store` in `runtime/health.py` | |
| P0 | ✅ universe.radar config audit | `config_audit.py` |
| P1 | wire or remove dead `HealthChecker` | `runtime/health.py` |
| P1 | `radar_stale` in metrics exporter | `runtime/metrics.py` |
| F12 | split | `quality.py` (1257 LOC), `runtime/strategy_audit.py` |

---

## `bot/dashboard/` — operator UI

| Приоритет | Действие | Модуль |
|-----------|----------|--------|
| P0 | ✅ radar in operator health | `operator_context.py` |
| P0 | shortlist API radar fields | `live.py` |
| P0 | ✅ radar summary endpoint | `app.py` + `live.py` |
| P2 | remove sandbox backtest stub or wire | `app.py` |
| F12 | split | `app.py` (1597 LOC), `live.py` (1104) |

---

## `bot/regime/` — macro context

| Приоритет | Действие | Модуль |
|-----------|----------|--------|
| P0 | ✅ JSON-safe regime_frame | `composite_regime.py`, `market_context_updater.py` |
| P1 | optional HMM/GMM off hot path | `hmm_regime.py`, `gmm_var.py` |
| P1 | btc_phase ↔ `btc_correlation` | `market.py`, `family_gates.py` |

---

## `bot/domain/` — config & contracts

| Приоритет | Действие | Модуль |
|-----------|----------|--------|
| P0 | strict models + radar validators | `config.py` |
| P1 | `PUBLIC_FEATURE_FIELDS` completeness | `contracts.py` |
| P0 | catalog wiring CI | `strategy_catalog.py`, `catalog_guards.py` |

---

## `bot/ops/` + `bot/cli.py` + root helpers

| Приоритет | Действие | Модуль |
|-----------|----------|--------|
| P0 | ✅ session_meta auto | `live_supervised_session.py` (scripts) |
| P0 | ✅ radar in startup snapshot | `startup_report.py` |
| P1 | `make live-detached-6h` | Makefile + `launch_detached.py` |

---

## `bot/core/` + `bot/backtest/`

| Приоритет | Действие | Модуль |
|-----------|----------|--------|
| P2 | export `BookTickerEvent` | `core/__init__.py` |
| P2 | backtest catalog parity | `backtest/engine.py` vs 38 setups |
| P2 | feature parity with `prepare_frame` | `backtest/engine._prepare_frame` |

---

## Порядок внедрения (агент)

1. **Wave G (done):** wiring radar в существующие runtime/diagnostics/dashboard/ops.
2. **Wave H (done):** radar API, metrics, emergency throttle, filter_stages audit, `main.py` autonomous pipeline log.
3. **Wave I (in progress):** `telemetry_strategy_analysis`, auto `post_session_calibration` после 6h, `zero_hit_triage.json`.
4. **F12:** по REFACTOR_PLAN — memory → queries, ws split, pipeline split.

## Единая точка входа (`python main.py`)

```
main.py → bot.cli.run() → SignalBot.start() → run_forever()
```

**`start()` (синхронно до WS):** proxy bootstrap → storage/delivery preflight → SQLite → config audit → stale cleanup → tracking repair → shortlist build → WS start → dashboard (если включён) → preload frames.

**`run_forever()` (фон без ручных скриптов):** EventBus (kline / reconnect / bookTicker) → shortlist_refresh → heartbeat → health_telemetry → health_monitor → emergency_fallback → oi_refresh → spot_companion → tracking_review → market_regime → [intelligence] → [telegram_operator].

**Radar:** `!ticker@arr` → `MarketRadarStore` → tier cycle в shortlist refresh → promotion → WATCH в `watch_escalation` → health/metrics/dashboard.

```bash
python scripts/_archive/audit_py_deep_findings.py  # archived; перегенерация файлового аудита
make check && pytest tests/test_wave_f9_agent_*.py tests/test_wave_f10_agent_*.py tests/test_wave_i_calibration.py tests/test_radar_phase_g.py tests/test_radar_funnel.py -q
```
