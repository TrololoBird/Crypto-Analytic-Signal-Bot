# Hunter rewrite — карта миграции (G1)

> **G1:** полный rewrite → `hunt_core/`.  
> **G2:** H-B «Широкий хантер».  
> **Источник inventory:** [HUNT_TRUTH_AUDIT.md](HUNT_TRUTH_AUDIT.md).

## Статус слоёв

| Слой | Целевой путь | Статус | Примечание |
|------|--------------|--------|------------|
| contracts | `hunt_core/contracts.py` | **done** | TypedDict |
| runtime | `hunt_core/runtime/` | **done** | thin `_impl` + `cycle.py` (~1.8k LOC) |
| data | `hunt_core/data/` | **done** | collect, rest_tiers, lake |
| market | `hunt_core/market/` | **done** | CCXT REST + Pro watch (no engine REST/WS) |
| features | `hunt_core/features/` | **done** | latch, levels (re-export) |
| detect | `hunt_core/detect/` | **done** | router H-B |
| gate | `hunt_core/gate/` | **done** | pipeline + edge_policy |
| deliver | `hunt_core/deliver/` | **done** | telegram + explain |
| track | `hunt_core/track/` | **done** | tracker + reconcile |
| calibrate | `hunt_core/calibrate/` | **done** | runner unify |
| research | `hunt_research/` | **done** | labels, verify re-exports |
| legacy | `hunt/_legacy/hunt_watch/` | pending | после полного cutover `_impl` |

## Модуль → модуль (hunt_watch → hunt_core)

| Старый модуль | Действие | Новый модуль |
|---------------|----------|--------------|
| `bootstrap.py` | keep import | `hunt_core/bootstrap.py` re-export |
| `paths.py` | port | `hunt_core/paths.py` |
| `ws_feed.py` | **deleted** | `market/streams.py` (CCXT watch*) |
| `session_state.py`, `frame_fallback.py` | keep in hunt_watch | logic unchanged |
| `screener.py`, `watchlist_ops.py`, `scanner_runner.py` | port | `data/universe.py` |
| `data_completeness.py` | port | `data/completeness.py` |
| `feature_latch.py` | port | `features/latch.py` |
| `levels.py`, `targets.py` | port | `features/levels.py` |
| `indicators.py` | port | `features/indicators.py` |
| `signal_engine.py`, `dump_hunt_alert.py` | port | `detect/short_dump.py` |
| long block in watch.py | extract | `detect/long_bounce.py` |
| `early_alert.py`, `ignition.py` | port | `detect/early_advisory.py` |
| `lifecycle.py`, `lifecycle_sticky.py` | port | `detect/lifecycle.py` |
| `mtf_policy.py`, `directional_filters.py`, … | merge | `gate/pipeline.py` |
| phase_matrix, liquidity, btc, regime, adaptive | merge | `gate/pipeline.py` |
| — | NEW | `gate/edge_policy.py` (H-B) |
| `deliver/sniper.py`, `deliver/telegram.py` | port | `deliver/` |
| `alert_explain.py` | port | `deliver/explain.py` |
| `signal_tracker.py` | port | `track/tracker.py` |
| `signal_events.py` | port | `track/events.py` |
| `reconcile_signals.py` logic | port | `track/reconcile.py` |
| `calibration.py`, `param_calibration.py`, … | merge | `calibrate/runner.py` |
| `param_store.py` | port | `params/store.py` |
| `logic_verify.py` | move | `hunt_research/logic_verify.py` |
| `backtest_synthetic.py` | move | `hunt_research/backtest_synthetic.py` |
| `jsonl_replay.py` | move | `hunt_research/jsonl_replay.py` |
| `beat_dump_lab.py`, `independent_short.py` | archive | `hunt/_archive/` |
| `dump_init_score.py` | offline only | `hunt_research/` (R5) |
| `monitor.py`, `verify_diff.py` | move | `hunt_research/` |

## Скрипты

| Скрипт | После rewrite |
|--------|---------------|
| `watch.py` | thin → `hunt_core.runtime.bot` |
| `migrate_contracts.py` | NEW — нормализация JSONL |
| `check_core_budget.py` | NEW — LOC + reachability CI |
| `health_rollup.py` | NEW — ops |
| `gate_edge.py`, `backtest_signals.py` | import `hunt_research.labels` |

## Данные

| Путь | Действие |
|------|----------|
| `hunt/data/` | канон (paths.py) |
| `data/` (repo root) | мигрировать дубли → hunt/data |
| `hunt/data/lake/` | SQLite + parquet |
| JSONL ticks | импорт в lake (batch) |

## Cutover checklist

- [x] `verify_logic` 125/125 green
- [x] `run_gate_pipeline` wired в `_impl` (H-B edge + sniper + alert gate)
- [x] `tick_io.append_tick_rows` — normalized JSONL writes
- [x] SQLite lake: 64 959 ticks (`hunt/data/lake/hunt_lake.sqlite`)
- [x] `unified_labels.jsonl` 578 rows
- [x] `watch.py` 13 LOC thin CLI
- [ ] jsonl_replay parity via lake (`replay_parity_check.py`) (sample-dependent; expand tick window)
- [x] `snapshot_symbol` → `data/collect.py` (~1.5k LOC)
- [x] `run_tick` / `run_loop` → `runtime/cycle.py` (watch smoke `--once` OK)
- [x] Market plane → `hunt_core/market/` (`ccxt>=4.4` in pyproject); `ws_feed.py` removed
- [ ] hunt_watch frozen в `_legacy/`
- [x] graphify update
