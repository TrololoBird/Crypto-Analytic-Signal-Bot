# Hunter — целевая архитектура и контракты (Track 2 / Gate G3 предложение)

> Статус: **предложение** под гейт G3. Цель — задать North Star для split `watch.py`
> и консолидации sprawl. Контракты основаны на **реальной** текущей схеме тика
> (прочитана из `hunt/data/*.jsonl`, не из доков). Продукт: **H-A Снайпер**
> (short fade в `dump_active`, см. `HUNT_PRODUCT_DEFINITION.md`).

## 1. Целевой конвейер (production loop)

```
ingest → features → detect → score/gate → deliver → track → (outcomes) → calibrate
```

Каждый слой — узкая ответственность и стабильный контракт на стыке. Research loop
(backtest/feature-store/edge) читает те же контракты офлайн.

## 2. Стабильные контракты (точки стыковки research↔production)

Менять эти структуры — только осознанно (миграция данных). Это ядро дисциплины.

### TickRow (что пишется в `dump_minute_watch.jsonl`, реальные ключи)
`ts, symbol, price, chg_24h_pct, range_24h_pct,
lifecycle{phase, recommended_bias, short_entry_ok, fall_from_high_pct, ...},
dump{phase, score, fuel, triggers, confirm_hard, confirmed, entry_zone,
  support_break_level, stop_loss, tp1, tp2, invalidation_above, levels_viable, levels_veto},
long{...},  market{taker_5m, oi_chg_1h, oi_z_score, funding_pct, top_ls_1h,
  depth_imbalance, liquidation_score_5m, microprice_bias, ...},
regime{regime, adx_1h, adx_4h, bear_div_1h, ...}, session{high_24h, low_24h, pos_in_range},
book_walls{bid_levels, ask_levels}`

### SignalRecord (active/closed; реальные ключи `signal_history.jsonl`)
`symbol, direction, entry_lo, entry_hi, stop_loss, tp1, tp2, invalidation_above/below,
fuel, entry_lifecycle_phase/bias, close_reason, exit_price, pnl_pct, mfe_pct,
duration_min, extreme_hi/lo, entry_message_id, opened_at, closed_at`

### OutcomeRecord (backtest/gate_edge; реальные ключи)
`symbol, direction, lifecycle_phase, fuel, entry_lo/hi, stop_loss, tp1, tp2,
bt_outcome∈{tp1_hit,tp2_hit,sl_hit,timeout}, bt_mfe_pct, bt_mae_pct, bt_candles_to_tp1, opened_at`

### FeatureVector (`feature_latch`, open/peak/close) — канон для edge-харнеса/ML.

> Замечание из аудита: `lifecycle_phase` заполнен в `gate_edge_outcomes.jsonl`, но
> почти пуст в `backtest_outcomes.jsonl`. **Контракт обязывает** писать `lifecycle_phase`
> во ВСЕ outcome-записи — иначе H-A срез не измерить.

## 3. Целевая карта модулей (куда сходится текущее)

| Слой | Целевой модуль(и) | Сейчас (консолидировать) |
|------|-------------------|--------------------------|
| ingest/data | `data/feed`, `data/universe` | ws_feed, session_state, data_completeness, frame_fallback, screener, scanner_runner, watchlist_ops, symbol_probe, ignition |
| features | `features` | indicators, feature_latch, levels, targets |
| detect | `detect` (один short-path для снайпера) | signal_engine, early_alert, dump_hunt_alert, dump_init_score, lifecycle, lifecycle_sticky |
| score/gate | `gate` (один прозрачный слой) | mtf_policy, directional_filters, phase_matrix_gate, liquidity_gate, btc_alignment, regime_ensemble, adaptive_thresholds |
| deliver | `deliver` | (в watch.py) + telegram_commands, alert_explain, signal_audit |
| track | `track` | signal_tracker, prep_shadow_tracker, tracker_outcomes, signal_events, pump_history |
| calibrate | `calibrate` (один путь) | calibration, param_calibration, level_calibration, autotune_runner |
| params | `param_store` | param_store (оставить) |
| research/tooling | `hunt/research/` | logic_verify, verify_diff, monitor, jsonl_replay, backtest_synthetic, *_report |

## 4. Split `watch.py` (3402 LOC → тонкая оркестрация)

Извлечь по слоям, оркестрация остаётся в `watch.py` (образец `bot/runtime/bot.py`):
- `deliver/telegram.py` — форматтеры (emoji-мапы 1526/1648, `_format_followup_telegram`,
  `format_dump_hunt_telegram`) + sender (`_send_telegram_chunks`, `maybe_send_*`).
- `deliver/gate.py` — `_should_alert`/`evaluate_alert_gate` + снайпер-гейт (HUNT_SNIPER_MODE).
- `runtime/cycle.py` — главный per-symbol цикл тика.
- `data/collect.py` — сбор market/lifecycle/dump/long блоков тика (`_dump_analysis` и т.п.).

## 5. Консолидация дублей (приоритет, осторожно)
- **Калибровка ×4 модуля + 2 скрипта → один `calibrate`** с guardrails.
- **Verify ×5 → один регресс-прогон** (`logic_verify` 2093 LOC — раздуть на кейс-группы).
- **Детекторы:** для снайпера live нужен только short-dump путь; long/early/ignition →
  research/shadow, не в production detect.

## 6. Anti-bloat бюджет (предложение для G3)
- Активный **production-core** (ingest→track, без research/tooling): **цель ≤ 8 000 LOC**
  (сейчас активное дерево 24 327; tooling/verify/research выносятся в `hunt/research/`).
- Правило: модуль в production-core обязан быть reachable из `watch.py` И иметь edge-обоснование.
- reachability-проверка в CI; `hunt/_archive/` и `hunt/research/` вне бюджета.

## 7. Порядок исполнения (после G3)
1. Split `watch.py` по §4 (поведение неизменно, только перенос) + verify_logic зелёный.
2. Консолидация calibrate (§5) с guardrails.
3. Консолидация verify (§5) в один прогон.
4. Detect упростить под снайпер; long/early → research.
5. Контракт OutcomeRecord: дописать `lifecycle_phase` везде (§2).
