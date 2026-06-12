# Hunter — целевая архитектура и контракты (G3 / H-B rewrite)

> **Статус:** утверждено под G2=H-B + G1=rewrite.  
> **Пакет:** `hunt_core/` (production-core ≤8k LOC) · `hunt_research/` (offline) · `hunt/_legacy/hunt_watch/` (freeze после cutover).  
> **Продукт:** [HUNT_PRODUCT_DEFINITION.md](HUNT_PRODUCT_DEFINITION.md) — H-B «Широкий хантер».

## 1. Целевой конвейер (production loop)

```
ingest → features → detect → gate → deliver → track → outcomes → calibrate
```

Research loop читает те же контракты офлайн: lake → edge_harness → walk_forward → suggest.

## 2. Стабильные контракты

Реализация: [hunt_core/contracts.py](../hunt_core/contracts.py).

### TickRow
`ts, symbol, price, chg_24h_pct, range_24h_pct, lifecycle{...}, dump{...}, long{...},
market{...}, regime{...}, session{...}, book_walls{...}`

**Правило writer:** не дублировать `positioning` == `market` (humble §23).

### SignalRecord
`symbol, direction, entry_lo/hi, stop_loss, tp1/tp2, invalidation_*, fuel,
entry_lifecycle_phase/bias, close_reason, exit_price, pnl_pct, features_open/peak/close`

### OutcomeRecord
`symbol, direction, lifecycle_phase` (обязательно), `fuel, entry_*, stop_loss, tp1, tp2,
bt_outcome ∈ {tp1_hit,tp2_hit,sl_hit,timeout}, bt_mfe_pct, bt_mae_pct, opened_at`

### FeatureVector
Канон из `feature_latch` — open/peak/close snapshots.

## 3. Целевая карта модулей (`hunt_core/`)

| Слой | Модуль | Источник (порт) |
|------|--------|-----------------|
| contracts | `contracts.py` | NEW |
| runtime | `runtime/bot.py`, `runtime/cycle.py` | watch.py оркестрация |
| data | `data/feed.py`, `universe.py`, `store.py`, `completeness.py` | ws_feed, screener, paths |
| features | `features/latch.py`, `levels.py`, `indicators.py` | feature_latch, levels |
| detect | `detect/router.py`, `short_dump.py`, `long_bounce.py`, `early_advisory.py`, `lifecycle.py` | signal_engine, lifecycle |
| gate | `gate/pipeline.py`, `edge_policy.py`, `phase_matrix.py` | 7 gate modules |
| deliver | `deliver/telegram.py`, `explain.py` | deliver/, alert_explain |
| track | `track/tracker.py`, `events.py`, `reconcile.py` | signal_tracker |
| calibrate | `calibrate/runner.py` | calibration×4 |
| params | `params/store.py` | param_store |

## 4. Detect router (H-B)

```
TickRow → detect/router.py
  ├─ short_dump     → SetupCandidate (all phases, confirm path)
  ├─ long_bounce    → SetupCandidate (edge-gated TG)
  └─ early_advisory → prep_shadow only (no TG until promoted)
```

`gate/edge_policy.py`:
- Short slices: promote when gate_edge SL ≤ baseline.
- Long TG: **disabled** until `gate_edge long n≥30` and SL ≤35%.

## 5. Runtime entry

- `hunt/scripts/watch.py` — thin CLI → `hunt_core.runtime.bot.main`
- `hunt_core/runtime/cycle.py` — per-tick loop (ex `_run_tick` / `_run_loop`)

## 6. Research separation (`hunt_research/`)

`labels.py`, `logic_verify` (re-export), `backtest_synthetic`, `jsonl_replay`, reports.

## 7. Anti-bloat

- Production-core `hunt_core/`: **≤8 000 LOC**
- CI: `hunt/scripts/check_core_budget.py` — reachability + LOC
- `hunt/_archive/`, `hunt_research/` вне бюджета

## 8. Порядок cutover

1. contracts + lake + labels (фаза 1)
2. runtime shell + data/features port (фаза 2a–b)
3. detect router + gate pipeline (фаза 2c)
4. track + deliver + calibrate (фаза 2d)
5. research split + parity (фаза 3)
6. multi-source + ops (фазы 4–5)
7. freeze `hunt_watch` → `_legacy/`
