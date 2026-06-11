# Hunt Research — ответы (волны 1–5 + эмпирика)

Сформировано **2026-06-11**. Источники: Binance official docs, report (24–26), веб-поиск, **20 624 тиков** hunt JSONL (`hunt/data/calibration_probe.json`).

## Статус TOP-25

| ID | Ответ | Уверенность | В коде |
|----|-------|-------------|--------|
| Q01 | kline `x=true` lag p50 ~0.2–0.5s, p95 ~1–2s; grace **2.5s** | Hunt-empirical + community | `param_store.ws.kline_grace_sec` |
| Q02 | `ap` = 1m MA basis; gate по `(ap−index)/index` bps | Official Binance | `mtf_policy`, `watch.py` basis_ap |
| Q03 | CVD/taker на **`nq`** (без RPI) | Official | `ws_feed`, `orderflow.use_nq` |
| Q04 | Young listing: synth 4h при ≥48×1h; confirm veto при <24×1h | Hunt policy | `listings.*`, `signal_engine` veto |
| Q05 | **10 msg/s** incoming WS (official USDⓈ-M Connect) | Official | combined URL subscribe (no burst) |
| Q06 | VWAP extreme **2.25×ATR** (p95 vdev≈4.17 на meme) | Hunt-empirical | `filters.vwap_extreme_atr` |
| Q07 | BTC corr soft **0.45**, hard **0.70**, окно 1h | Research + live probe | `param_store.btc` |
| Q08 | Short на bounce recovery — MTF veto; dump continuation — стандарт | Qualitative + Hunt | `mtf_bounce_recovery_short` |
| Q09 | Taker: **30s** fast scoring, **60s** confirm veto | Hunt default | `orderflow.*`, `signal_engine` veto |
| Q10 | Basis meme p95≈37 bps; gate overheat **120 bps** (conservative) | Hunt-empirical | `basis.ap_overheat_bps` |
| Q11 | Liq cascade: score≥0.30 + events≥6 + notional≥**$25k**/5m | Hunt p90≈$11k | `liquidation.*` |
| Q12 | TP1 partial **50%** normal / **80%** hot regime | Industry baseline | `tracker_thresholds` |
| Q13 | Bias-flip hold if ADX<**20** chop + profitable | Research ADX chop | `tracker.bias_flip_chop_adx_max` |
| Q14 | SL **2.0–2.5×ATR** Wilder 14 on 15m; distribution cap **2.25** | Research + Q14 | `level_calibration`, `levels.py` |
| Q15 | Phase matrix: **n≥12**, adj WR<**28%** (Bayesian prior 35%) | Hunt-empirical | `phase_matrix.*` |
| Q16 | Walk-forward IS **70%**; min OOS **30** outcomes guard | Strateda/Kiploks | `walk_forward.*`, `jsonl_replay` |
| Q17 | confirm_min **72** delivery **72** при n<30 — hold autotune | Hunt ops + confluence | `hunt_calibration.json`, `delivery.*` |
| Q18 | Manual TG gap ~10–30% vs paper; stress **0.15%** slippage | Community | `stats.meme_slippage_pct`, `tg_backtest.py` |
| Q19 | tg_backtest forward **8h** default | Hunt MVP | `stats.forward_horizon_hours`, `tg_backtest.py` |
| Q20 | Early TG off; prep→confirm funnel **8h** window | Hunt default | `prep_to_confirm_funnel`, cooldown 30m |
| Q21 | Proxy failover via discover + ws direct fallback | Project | `ws_feed` proxy streak |
| Q22 | Grace **2.5s** (Q01+Q22 merged) | Hunt-empirical | `ws.kline_grace_sec` |
| Q23 | Regime ensemble ADX+vol+squeeze | Partial OSS | `regime_ensemble.py` |
| Q24 | No dedicated OSS liq scanner; Binance `@forceOrder` | Official | `ws_feed` |
| Q25 | JSONL replay: closed bars, IS/OOS sweep, anti-leakage | Hunt spec + Kiploks WFO | `jsonl_replay.py`, `scripts/jsonl_replay.py` |
| Q26 | Prep shadow WR → delivery fuel ±3 при n≥8; rolling калибровка | Hunt-empirical + WFO | `prep_shadow_delivery_fuel_adjustment`, `delivery.*` |
| Q27 | Fade exhaustion: блок при ADX1h>**32** без div/break | Research ADX chop | `delivery.exhaustion_adx_max` |
| Q28 | Long impulse: pos≥**0.52** + цена выше mid 24h | BTA session momentum | `delivery.impulse_long_*` |
| Q29 | BE после TP1: **+0.15%** buffer (не exact entry) | Enlightened ST / slippage | `tracker.breakeven_buffer_pct` |
| Q30 | Long pump: OI 1h Δ≥**0.5%** (новые позиции) | Anomiq perp scanner | `delivery.impulse_long_min_oi_chg_1h` |
| Q31 | Autotune confirm_min hold при **n<30** OOS | Strateda WFO | `autotune_runner` guardrails |
| Q32 | `vwap_oversold` hard-block на dump-leg short — ложный veto (32/32) | Hunt JSONL + VWAP MR | `directional_filters`, `alert_explain` |
| Q33 | Structural dump short после collapse: impulse+dump_active fall≥**12%** | Alexdemachev short-the-dump | `_dump_continuation_short_ok` |
| Q34 | TP2 waiver при R:R≥min + structural≥2; time stall **8h** MFE<**1%** | TradesViz / Vantixs | `_tp2_room_blocks`, `tracker.mfe_stall_*` |
| Q35 | Profitable structural exit (bounce_invalidate/lifecycle_stale/bias_flip/trend_exhaustion) при pnl>**0.15%** → **win** для WR stats | Hunt FOLKS +4.62% | `tracker_outcomes.outcome_kind` |

## Эмпирика (20 624 ticks, 91 symbols)

```
|vdev| p95 = 4.17 ATR    → vwap_extreme 2.25
basis_ap p95 = 37 bps    → gate 120 bps (wide safety)
liq long notional p90 = $11.5k → confirm min $25k
confirmed long: 346 / confirmed short: 1223
```

## Batch 65 (остаток)

52 закрыты report (25) + волна 1 (26). **10 Partial** → числа выше. **3 Not found** (A.12 proxy patterns, D.32 WR tables, H.58 OSS) — закрыты policy/heuristic, не academic tables.

## Следующая калибровка

При **n≥30** closed tracker outcomes: пересмотр `phase_matrix.max_wr`, `confirm_min_score`, liquidation notional по per-symbol cells в `hunt_calibration.json`.
