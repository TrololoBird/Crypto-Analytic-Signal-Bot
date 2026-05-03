# Shortlist audit (2026-04-28)

## 2026-05-03 recheck

Confirmed current state:

- `rest_full`, `ws_light`, `cached`, and `pinned_fallback` sources are still
  distinct in `bot/application/shortlist_service.py`.
- The regression import contract for shortlist fallback constants had drifted:
  `FALLBACK_REASON_LIVE_EMPTY`, `FALLBACK_REASON_REFRESH_EXCEPTION`,
  `FALLBACK_REASON_USING_CACHED`, and
  `normalize_shortlist_fallback_reason(...)` were referenced by regression
  suites but missing from the active module.
- This pass restored that public test contract and emits structured
  `source_before`, `source_after`, `fallback_reason`, `full_refresh_due`,
  `ws_cache_warm`, `has_symbol_meta`, `cached_shortlist_age_s`, and
  `cached_shortlist_size` fields into `shortlist.jsonl`.
- Regression check passed:
  `python -m pytest -q tests/test_regression_suite_tracking_delivery.py`.

Residual risk:

- This recheck validates fallback telemetry contract and live WS smoke on
  BTC/ETH, but it does not prove that every degraded live market condition is
  represented by a perfect reason code.

## Scope

Проверка выполнена по call-path shortlist в рантайме:
- `bot/application/shortlist_service.py`
- `bot/universe.py`
- `bot/websocket/cache.py`
- `bot/ws_manager.py`
- `bot/application/telemetry_manager.py`
- релевантные регрессионные тесты shortlist.

## Confirmed facts

### 1) Источники shortlist и этапы агрегации/фильтрации

- Источники данных shortlist:
  - `rest_full`: `fetch_exchange_symbols()` + `fetch_ticker_24h()` в `build_live_shortlist()`.
  - `ws_light`: кеш WS (`get_global_ticker_data()`) + ранее сохранённый `symbol_meta` в `build_light_shortlist()`.
  - `pinned_fallback`: статические pinned symbols через `build_pinned_shortlist()`.
  - `cached`: предыдущий `_last_live_shortlist`, если новый live refresh не удался.
- Enrichment-этап выполняется перед `build_shortlist()` в `_enrich_shortlist_rows()`:
  - WS-метрики: `ticker_age_seconds`, mark/funding/basis, spread/book_age.
  - REST-кеш метрики (через `BinanceFuturesMarketData` accessors): OI/L-S/basis/oi_current.
- Фильтрация/агрегация в `build_shortlist()`:
  - фильтр по metadata: TRADING + PERPETUAL + quote_asset + whitelist символов.
  - фильтр ликвидности/возраста листинга (кроме pinned).
  - расчёт composite score + reasons.
  - bucket allocation (`trend/breakout/reversal`) + fill до `shortlist_limit`.
  - summary: `eligible`, `dynamic_pool`, `pinned`, bucket counters, `avg_score`.

### 2) Разделение `ws_light` и `rest_full`

- Разделение реализовано на уровне entrypoints:
  - `build_light_shortlist()` всегда вызывает `build_shortlist(..., seed_source="ws_light")`.
  - `build_live_shortlist()` всегда вызывает `build_shortlist(..., seed_source="rest_full")`.
- В `do_refresh_shortlist()` режим выбирается по расписанию full refresh:
  - между full-refresh сначала пробуется `ws_light`;
  - если пусто/недоступно — `rest_full`;
  - при исключении — `cached`, иначе `pinned_fallback`.
- В `UniverseSymbol.seed_source` сохраняется provenance конкретной записи shortlist.

### 3) Fallback-переходы: когда и почему

Переходы в `do_refresh_shortlist()`:
- `ws_light` -> `rest_full`, если:
  - наступил full refresh interval;
  - либо `ws_light` вернул пустой список (cache cold, нет meta, ws не прогрет).
- `rest_full` -> `cached`, если произошла ошибка refresh, но есть `_last_live_shortlist`.
- `rest_full/ws_light` -> `pinned_fallback`, если ошибка и `cached` отсутствует.

Триггеры пустого `ws_light`:
- `ws is None`;
- отсутствует `bot._symbol_meta_by_symbol`;
- `not ws.is_ticker_cache_warm()`.

### 4) Телеметрия/логи деградации

- Каждое обновление shortlist пишет `shortlist.jsonl` с полями:
  - `source`, `mode`, `size`, shortlist preview,
  - `eligible/dynamic_pool/pinned/avg_score`,
  - `enrich_errors_by_stage`, `enrich_errors_total`, `enrich_errors_last_cycle`,
  - `top_scores` с `seed_source`.
- При ошибке refresh пишутся warning-логи:
  - `shortlist refresh failed, using cached shortlist`;
  - `shortlist refresh failed, using pinned fallback`.
- Ошибки enrichment логируются как `shortlist enrich degraded` (первый раз и далее каждые 50 случаев).
- `shortlist_source` прокидывается в `cycles.jsonl` и `symbol_analysis.jsonl` через `TelemetryManager`, поэтому деградация доступна downstream-диагностике.

### 5) Риски silent fallback и точки явного логирования

Риски:
- Нет явного event/log о причине выбора `rest_full` вместо `ws_light` (cold cache vs interval due).
- Нет явного structured telemetry-поля `fallback_reason`; причина fallback восстанавливается косвенно.
- Переход в `cached` может быть «тихим» для внешнего наблюдателя, если смотрят только итоговый `source` без exception/reason breakdown.
- `ws_manager.rebuild_shortlist_on_demand()` не используется в текущем call-path shortlist; возможна расходимость ожиданий по поведению fallback при будущей интеграции.

Рекомендуемые точки явного логирования/телеметрии:
1. Перед ветвлением refresh добавить structured `decision`-лог:
   - `full_refresh_due`, `last_full_age_s`, `ws_cache_warm`, `has_symbol_meta`.
2. В `shortlist.jsonl` добавить поля:
   - `fallback_reason` (`ws_cache_cold`, `full_refresh_due`, `refresh_exception`, `live_empty`, `using_cached`, `using_pinned`),
   - `source_before` -> `source_after`.
3. При выборе `cached` записывать hash/age последнего live shortlist:
   - `cached_shortlist_age_s`, `cached_shortlist_size`.
4. При выборе `pinned_fallback` писать отдельный warning c размером pinned и отсутствием cache.
5. Для `ws_light` добавить метрику качества входа:
   - `ws_ticker_rows`, `ws_fresh_ratio`, `ws_mark/book_coverage`.

## Unverified inferences

- Не проверялись live-данные Binance в реальном рантайме; выводы о поведении fallback основаны на статическом анализе и существующих unit/regression тестах.
- Не проверялась полнота dashboard-визуализации новых/существующих shortlist fallback сигналов вне текущего audit-path.
