# План внедрения улучшений (аудит 2026-06-04)

> **NEW (уникальные находки):** [PY_FILE_AUDIT_NEW_FINDINGS.md](PY_FILE_AUDIT_NEW_FINDINGS.md) — `python scripts/_archive/audit_py_deep_findings.py` (archived)  
> Шаблонный каталог: [PY_FILE_AUDIT_5X.md](PY_FILE_AUDIT_5X.md)

---

## Насколько обоснованы выявленные проблемы

| Находка | Обоснование | Приоритет |
|---------|-------------|-----------|
| **Radar funnel** (Tier 0→4) | Binance: один `!ticker@arr` дешёвый; deep path на 500+ пар ломает weight/WS/CPU. Сравнение с OSS (firehose + local filter) — industry pattern. Live: shortlist **50**, не `pinned_fallback`. | **P0 — сделано** |
| **`FrozenInstanceError` в merge_shortlist** | Реальный crash при `rest_full` refresh; dataclass frozen. Подтверждено логом и тестом. | **P0 — сделано** |
| **`regime_frame_4h` DataFrame в SQLite JSON** | Повторяющийся `memory market context update failed` на 6h run; блокирует regime в operator memory. | **P0 — внедряется** |
| **`radar` / `radar_tier_cycle` не в `shortlist_build.jsonl`** | Док RADAR_FUNNEL обещает telemetry; calibration_pipeline не видит funnel без полей. | **P0 — внедряется** |
| **`nohup` из Cursor shell умирает** | Процесс живёт <2 мин; `launch_detached` / фоновый терминал — обосновано. | **P1** |
| **`emit_watch_candidates=false`** | Сигнальный продукт: WATCH не bypass delivery; включать только с operator DM path. | **P2** |
| **Real RSI на warm (REST 1h)** | Сейчас light RSI proxy; улучшает prescore, +REST weight — после калибровки. | **P2** |
| **Hot-only `@kline_1m` WS** | Экономия stream budget; нужен subscription planner change. | **P2** |
| **Dashboard radar API** | Operator visibility; не блокер торговли (нет auto-trade). | **P2** |
| **F12 de-bloat** (`memory.py`, `pipeline.py`, `ws.py`, `bot.py`) | REFACTOR_PLAN + health audit >500 LOC; снижает риск регрессий. | **P1–P2** |

**Не обосновано сейчас:** переписывать все 38 стратегий «под Crypto-Signal RSI alerts» — продукт **setup-based**, не indicator firehose (см. `SIGNAL_ONLY_PRODUCT.md`).

---

## Фазы внедрения

### Фаза 0 — P0 hotfix (1–2 ч агента) ✅ частично

1. ✅ Radar modules + config + tests `test_radar_funnel.py`
2. ✅ `promotion_engine.replace()` для frozen symbols
3. 🔄 `regime_frame_4h` JSON-safe (`market_context_updater` + `composite_regime`)
4. 🔄 `shortlist_build.jsonl`: `radar`, `radar_tier_cycle`
5. Regression: `pytest tests/test_wave_f10_agent_n.py tests/test_radar_funnel.py -q`

### Фаза 1 — Live ops & telemetry (2–3 ч)

1. `live_supervised_session` → `launch_detached.py` по умолчанию в playbook/Makefile
2. `session_meta.json` автозапись в supervisor
3. Post-6h: `calibration_pipeline --run-id`, `live_watch_rollup_report`
4. Grep telemetry: `radar_tier_cycle` в `shortlist_build.jsonl`
5. `live_session_status.py` — указатель на `live_radar_6h_*.log`

### Фаза G — Coverage expansion (все модули, не только radar core)

| Область | NEW действие | Статус |
|---------|--------------|--------|
| `subscription_planner` + `bot.py` | aggTrade priority: radar HOT/DEEP | ✅ |
| `intra_candle_scanner` | 0.5× throttle для radar_promoted | ✅ |
| `dashboard/live.py` | radar_tier_cycle + reasons в shortlist API | ✅ |
| `diagnostics/runtime/health.py` | `assess_radar_store` + `radar_health.jsonl` | ✅ |
| `runtime/watch_escalation.py` | `radar_watch.jsonl` + optional operator DM | ✅ |
| `data_readiness` + `pipeline` | radar_promoted skips strict derivatives | ✅ |
| `health_manager` + `startup_report` | radar in `/api/health` + suspicious modules | ✅ |
| `live_supervised_session` | auto `session_meta.json` | ✅ |
| Makefile | `make live-detached-6h` | ✅ |
| `watch_escalation` | merge with radar_watch (separate path) | backlog |
| 38× `bot/strategies/*.py` | Калибровка + catalog + matrix per setup | backlog |
| F12 LOC>800 | memory, ws, pipeline, tracking, filters | backlog |

Перечень **5 уникальных пунктов на каждый файл**: `audit_py_deep_findings.py` (не дублирует шаблон пакета).

### Фаза 2 — Radar P2 (4–8 ч)

1. `emit_watch_candidates` + `watch_escalation` (WATCH-only, no delivery bypass)
2. REST warm pool 1h klines для RSI (weight guard в `universe.py`)
3. `ws.py` / planner: kline_1m только для `tier=hot`
4. Dashboard `/api/radar/summary` из `MarketRadarStore.snapshot_summary()`
5. Документировать в `RADAR_FUNNEL.md` + пример telemetry

### Фаза 3 — F12 structural (multi-PR)

| PR | Модули | Критерий готовности |
|----|--------|---------------------|
| F12a | `memory.py` → `persistence/queries/` | LOC <500 в memory.py, gate green |
| F12b | `pipeline.py` dispatch split | `test_wave_f10_agent_l` |
| F12c | `ws.py` remainder → `ws_connection` | live WS catalog test |
| F12d | `bot.py` thin | только wiring в container |

### Фаза 4 — Calibration loop (ongoing)

1. `strategy_shortlist_matrix.py --run-id <6h>`
2. Zero-hit triage per strategy (skill)
3. `use_weighted_confluence` после review confluence JSONL
4. Outcome derank + wash gate tuning из telemetry
5. Обновить `PROJECT_ROADMAP_AND_STATUS.md`

---

## Как пользоваться `PY_FILE_AUDIT_5X.md`

- Для **конкретного файла** — 5 bullets в секции `### path (N LOC)`.
- **FILE_OVERRIDES** в `generate_py_audit_5x.py` — точечные P0/P2 для ключевых модулей.
- Остальные файлы — **package template** (ротация техдолга пакета); при правке файла уточняйте override в скрипте.

Перегенерация после изменений дерева:

```bash
python scripts/_archive/generate_py_audit_5x.py
```

---

## Следующий шаг агента (без участия оператора)

1. Завершить P0 тесты и `compileall`
2. Фаза 1: Makefile `live-detached-6h` + hook в `live_supervised_session`
3. После текущего 6h run — calibration rollup по `run_id`
