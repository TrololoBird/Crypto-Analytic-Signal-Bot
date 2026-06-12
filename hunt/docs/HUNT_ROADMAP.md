# Hunter (Охотник) — Roadmap развития

> **⚠️ SUPERSEDED (2026-06-12).** Метрики North Star и prep_shadow WR из этого документа
> **не подтверждены** данными. Единственный доверенный источник: **[HUNT_TRUTH_AUDIT.md](HUNT_TRUTH_AUDIT.md)**.
> Актуальный мастер-план: `.claude/plans/elegant-skipping-cloud.md` (v3).
>
> Источник: согласованный план развития от 2026-06-11. Фазовый, edge-first.
> Сопутствующие документы: [HUNT_CHANGELOG.md](HUNT_CHANGELOG.md),
> [BINANCE_API_AUDIT.md](BINANCE_API_AUDIT.md), `hunt/ARCHITECTURE.md`.

## Context

Охотник (`hunt/`, пакет `hunt_watch`) — живой детектор pump/dump для memecoins.
- **Вход:** REST раз в ~60с + async WS (`!forceOrder@arr`, `aggTrade`, `!markPrice@arr`, kline_5m).
- **Логика:** 8-фазный lifecycle FSM → short(dump-fade)/long(bounce) score → cluster-capped
  `fuel` → структурные hard-confirm → delivery gates → Telegram.
- **Tracking:** `signal_tracker` (intrabar SL/TP, MFE), `feature_latch` (векторы open/peak/close),
  `prep_shadow_tracker` (forming-стадии без TG).
- **Калибровка:** `hunt_calibration.json`, `calibrate_all.py`, `gate_edge.py`,
  `backtest_signals.py`, intel-dossier loop (`hunt/intel/`).
- **North Star:** tracker WR ≥70%, thesis_success ≥88%.

**Главное ограничение — выборка.** Live n=8 закрытых сигналов, prep_shadow n=160. При такой
выборке наивное «добавить ещё индикаторов» = переобучение. Поэтому план: сначала строим
измерение edge, затем обогащаем данные/фичи только там, где edge подтверждён, параллельно
чистим архитектуру (`watch.py` ~3400 LOC). Каждое добавление спарено с измерением.

### Принципы исполнения
1. **Edge-first.** Новая фича/фактор сначала латчится в `feature_latch` и измеряется через
   `gate_edge`/backtest — и только потом влияет на live-confirm. Прецедент: `dump_init_score`
   протестирован → 62% SL vs 52% baseline → НЕ подключён.
2. **Guardrails сохраняются.** n_tracker<30 блокирует ослабление; backtest sl_hit>30% блокирует
   ослабление; prep_shadow WR<50% → ужесточение (`calibration.py`).
3. **Переписывать можно**; поведенческие изменения live-confirm включаются после подтверждения
   на данных. Измерительные изменения (латч полей) — сразу.
4. Один коммит на фазу: `phase-hunt-<X>: <описание>`. `hunt` импортирует только `engine.*`.

---

## Phase H0 — Фундамент измерения (P0, первым)

- **Унифицировать label-store.** Свести `backtest_outcomes.jsonl`,
  `backtest_outcomes_enriched.jsonl`, `gate_edge_outcomes.jsonl`, `signal_history.jsonl`,
  `feature_latch`-векторы к единому набору grade-функций. Источник правды outcome —
  `signal_tracker` intrabar (R1 truth signal).
- **Feature-edge harness.** Расширить `gate_edge.py` до per-feature edge: для каждого поля
  `feature_latch` считать WR/SL conditional на пороге (winners vs losers, как
  `hunt/intel/dossier.py::_feature_win_loss_table`). → ранжированная таблица «какая фича
  разделяет исходы». Вход для H3.
- **Нарастить выборку.** `backtest_signals.py --include-pump-events --enrich` по расширенному
  окну (pump_history ~265 legs) → n≥30 на ключевые phase×direction бакеты.
- **North Star дашборд.** `outcomes_report.py` + `analyze_session.py` → один отчёт
  (thesis_success, SL-rate, gate-edge, fuel-bucket WR, per-phase WR).

Файлы: `hunt/hunt_watch/calibration.py`, `hunt/hunt_watch/feature_latch.py`,
`hunt/scripts/gate_edge.py`, `hunt/scripts/backtest_signals.py`, `hunt/intel/dossier.py`.

## Phase H1 — Обогащение data-plane (P1)

Подключить доступные, но не читаемые поля `PreparedSymbol` — **сначала только латч и измерение**.
Новых Binance-эндпоинтов не нужно: данные уже отдаёт engine (`engine/domain/schemas.py`).

Неиспользуемые сигналы для pump/dump:
- `oi_slope_5m` — онсет распада OI.
- `premium_slope_5m` / `mark_index_spread_bps` — разворот фандинга / сжатие contango.
- `spot_lead_return_1m` / `spot_futures_spread_bps` — спот опережает фьюч в дамп.
- `depth_wall_pressure` + динамика снятия стен — отвод ликвидности.
- `microprice_bias` — сдвиг микроструктуры buy→sell.
- `top_position_ls_ratio` vs `global_ls_ratio` gap — про-трейдеры vs ритейл.
- `aggression_shift` — ускорение taker buy→sell.
- Ликвидации: добавить magnitude/направление к boolean `liquidation_cascade_5m`.
- Быстрый OI: 5m/15m дельты.

Действия: расширить tick-схему `dump_minute_watch.jsonl` (`market`) и `feature_latch`; чтение из
`PreparedSymbol` в точке сбора тика. **Confirm не менять.**

Файлы: `hunt/hunt_watch/feature_latch.py`, модуль сбора тика в `hunt_watch/`,
`engine/domain/schemas.py` (чтение).

## Phase H2 — Индикаторы и Polars-фичи для pump/dump (P2)

- **CVD-дивергенция / signed order-flow** (`session_cvd`/`rolling_cvd_24h` есть, не читаются).
- **OI-momentum** — z-score и slope OI 5m/15m как колонки.
- **Premium/basis z-score**.
- **Liquidation-cluster интенсивность** — rolling notional/направление, не флаг.
- **Spot-perp lead-lag**.
- **Volatility-of-volatility / range-expansion**.
- Прунинг существующего набора (RSI14, ATR, BB-width, Donchian-width, MACD, pivot-wick, Fib) —
  по результатам H0-harness убрать шумные (anti-bloat).

Файлы: `engine/features/prepare_columns.py`/`prepare_frame.py` (если общее), feature-сборка в
`hunt_watch/`, `hunt/hunt_watch/levels.py`.

## Phase H3 — Редизайн scoring и confirm-гейтов (P3, ядро)

- **Диагностика fuel 80–95** (WR 26%, n=27): переразвесить триггеры / non-monotonic
  fuel→confidence.
- **Edge-взвешенный confirm** вместо «сумма триггеров ≥ confirm_min» (образец
  `bot/delivery/confluence.py`, обучаемый на hunt-исходах). Hard-структурный confirm остаётся «И».
- **Подключение H1/H2-фич** в confirm только после edge-валидации (SL ниже baseline).
- **bias_flip thesis_fail** (VELVET +2.73% без TP): отдельный счётчик и правило в `signal_tracker`.

Файлы: `hunt/hunt_watch/signal_engine.py`, `dump_init_score.py`, `signal_tracker.py`,
`param_store.py`, `calibration.py`.

## Phase H4 — Фильтры, шортлист, universe (P3)

- **Pump/dump radar** — шортлист под импульс (range-expansion, OI-spike, funding-extreme,
  spot-lead) вместо общего волюм-ранжира.
- **Распаковка distribution-phase** (prep_shadow WR 61.9% n=42, 0 live) — только при tracker n≥30.
- **HMSTRUSDT-класс** — явный «no-structure» veto.
- **Per-regime гейтинг** через `market_regime.json`.

Файлы: `hunt/hunt_watch/watchlist_ops.py`, `early_alert.py`, `levels.py`,
`hunt/data/hunt_calibration.json`.

## Phase H5 — Архитектура и наблюдаемость (P2, параллельно)

- **Разбить `watch.py` (~3400 LOC):** цикл/диспетчер тика, сбор market-блока, delivery/Telegram,
  persistence. Тонкая оркестрация в watch.py (как `bot/runtime/bot.py`).
- **Backtest = live parity** — общий confirm-модуль для `backtest_synthetic.py`/`jsonl_replay.py` и live.
- **Feature store** — формализовать `feature_latch` (open/peak/close).
- **Автоматизировать intel-loop** — `analyze_session.py` → dossier; suggestions со schema-валидацией
  (`hunt/intel/schema.py`, confidence>0.7 только при n≥30).
- **Наблюдаемость** — North Star rollup, алерт на просадку WR/рост SL.

Файлы: `hunt/scripts/watch.py`, `hunt/hunt_watch/backtest_synthetic.py`,
`hunt/scripts/jsonl_replay.py`, `hunt/scripts/analyze_session.py`, `hunt/intel/*`.

---

## Приоритеты

| Фаза | Тема | Приоритет | Блокирует |
|------|------|-----------|-----------|
| H0 | Измерение edge, label-store, рост выборки | P0 | всё остальное |
| H1 | Латч неиспользуемых engine-полей | P1 | H2/H3 |
| H5 | Разбить watch.py, backtest=live parity | P2 (параллельно) | валидность измерений |
| H2 | Polars-фичи pump/dump + прунинг | P2 | H3 |
| H3 | Edge-взвешенный scoring/confirm, fuel-фикс | P3 | — |
| H4 | Radar/шортлист/distribution-unlock | P3 | — |

## Кросс-фазовая калибровка (постоянно)

После каждой supervised-сессии (`supervised_session.py --duration-hours 6`) →
`calibrate_all.py` → `outcomes_report.py` → `analyze_session.py` → ревью. Пороги меняются
только через `param_store`/`hunt_calibration.json`, с guardrails.

## Verification (каждая фаза)

1. `python -m py_compile $(find hunt -name "*.py")`.
2. `PYTHONPATH=hunt python hunt/scripts/verify_logic.py` (цель 100/100).
3. Edge (H1–H4): `gate_edge.py` + `backtest_signals.py` — новая фича/гейт должна дать
   SL-rate ≤ baseline на n≥30, иначе НЕ wiring в live.
4. Live: `supervised_session.py --duration-hours 6` → `outcomes_report.py`; North Star.
5. Архитектура (H5): `verify_logic` 100% + backtest=live parity.
