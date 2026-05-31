# PROJECT MAP - Binance public signal-only bot

Updated: 2026-05-31

## Mission

This project is a strictly analytical Binance public-data signal bot. It must never place
orders. The runtime prepares market data, evaluates strategy signals, validates the signal
contract, applies hard delivery gates, and sends only manual-trading signal messages to
Telegram.

## Data Flow (v9)

```text
Binance REST/WS
  -> bot/market/rest.py + bot/market/ws.py (+ ws_* helpers)
  -> bot/runtime/shortlist_service.py + bot/market/universe.py
  -> bot/features/prepare.py (+ feature submodules)
  -> bot/engine/ + bot/strategies/*
  -> bot/delivery/filters.py + bot/delivery/confluence.py
  -> bot/runtime/delivery_orchestrator.py
  -> bot/delivery/contract.py validate_signal_contract()
  -> hard 3-of-5 delivery confluence gate
  -> bot/delivery/deliver.py + bot/delivery/telegram.py
  -> Telegram group for manual execution
```

## Non-bypassable Delivery Path

The enforced delivery path is:

```text
strategy Signal
  -> DeliveryOrchestrator._contract_issue_rows()
  -> signal_contract.validate_signal_contract()  # bot/delivery/contract.py
  -> DeliveryOrchestrator._hard_confluence_gate()
  -> cooldown/tracking/quality checks
  -> delivery.deliver()
```

Runtime proof from 2026-05-26:

```text
valid_trace=signal_contract.validate -> hard_confluence_gate -> delivery.deliver
  -> tracker.features -> repo.set_cooldown -> repo.set_cooldown
  -> tracker.arm -> alerts.confirmed -> ws.sync

invalid_trace=signal_contract.validate
invalid_delivered=0 stage=contract reason=invalid_signal_contract

weak_score_trace_after_gate=[]
weak_delivered=0 stage=confluence reason=hard_confluence_gate_failed
```

If a signal is appended for delivery without contract validation or hard gate pass,
`DeliveryOrchestrator` raises `ValueError`.

## Core Files

| Area | File | Role |
|---|---|---|
| Config | `bot/domain/config.py` | Pinned symbols (incl. XRPUSDT), thresholds, runtime settings. |
| Contract | `bot/delivery/contract.py` | Signal format and level validation. |
| Delivery gate | `bot/runtime/delivery_orchestrator.py` | Final validation, hard confluence gate, cooldown, delivery. |
| Universe | `bot/market/universe.py` | Multifactor shortlist scoring and strategy fit routing. |
| Shortlist service | `bot/runtime/shortlist_service.py` | Enriches candidates with funding, spread, OI, basis, liquidation data. |
| Features | `bot/features/prepare.py` | Main indicator pipeline, swing points, ATR/RSI/BB features. |
| Shared math | `bot/features/shared.py` | Wilder smoothing and common indicator primitives. |
| Strategy engine | `bot/engine/engine.py` | Executes enabled strategies (lanes cap per symbol). |
| Strategy registry | `bot/strategies/__init__.py` | Exports all 38 strategy classes. |
| Spec patterns | `bot/strategies/spec_patterns.py` | Shared detectors for order blocks, pivots, squeeze, divergences. |
| Historical audit | `scripts/historical_strategy_audit.py` | Replays closed Binance Futures klines in rolling windows across all 38 strategies. |
| Telegram formatting | `bot/delivery/formatting.py` | Manual signal message formatting. |
| Tracking | `bot/persistence/tracking.py`, `bot/persistence/outcomes.py` | Signal lifecycle and outcome features. |
| Cache | `bot/persistence/repository/cache.py` | Parquet time-series cache with monthly compaction. |
| PID lock | `bot/ops/pid_utils.py` | Shared process lock helpers (cli + supervised sessions). |

## Strategies

| # | Strategy | Class | File | Current audit status |
|---|---|---|---|---|
| 1 | structure_pullback | StructurePullbackSetup | `bot/strategies/structure_pullback.py` | ran, no errors |
| 2 | structure_break_retest | StructureBreakRetestSetup | `bot/strategies/structure_break_retest.py` | ran, no errors |
| 3 | wick_trap_reversal | WickTrapReversalSetup | `bot/strategies/wick_trap_reversal.py` | ran, no errors |
| 4 | squeeze_setup | SqueezeSetup | `bot/strategies/squeeze_setup.py` | ran, no errors |
| 5 | ema_bounce | EmaBounceSetup | `bot/strategies/ema_bounce.py` | historical hits, contract clean |
| 6 | fvg_setup | FVGSetup | `bot/strategies/fvg.py` | ran, no errors |
| 7 | order_block | OrderBlockSetup | `bot/strategies/order_block.py` | ran, no errors |
| 8 | liquidity_sweep | LiquiditySweepSetup | `bot/strategies/liquidity_sweep.py` | rejected by pattern filters, no errors |
| 9 | bos_choch | BOSCHOCHSetup | `bot/strategies/bos_choch.py` | ran, no errors |
| 10 | hidden_divergence | HiddenDivergenceSetup | `bot/strategies/hidden_divergence.py` | rejected by pattern filters, no errors |
| 11 | indicator_divergence | IndicatorDivergenceSetup | `bot/strategies/indicator_divergence.py` | ran, no errors |
| 12 | funding_reversal | FundingReversalSetup | `bot/strategies/funding_reversal.py` | ran, no errors |
| 13 | cvd_divergence | CVDDivergenceSetup | `bot/strategies/cvd_divergence.py` | rejected by pattern filters, no errors |
| 14 | session_killzone | SessionKillzoneSetup | `bot/strategies/session_killzone.py` | explicit schedule skip when inactive |
| 15 | breaker_block | BreakerBlockSetup | `bot/strategies/breaker_block.py` | ran, no errors |
| 16 | turtle_soup | TurtleSoupSetup | `bot/strategies/turtle_soup.py` | live hits on top-volume slice (2026-05-31) |
| 17 | vwap_trend | VWAPTrendSetup | `bot/strategies/vwap_trend.py` | rejected by pattern filters, no errors |
| 18 | supertrend_follow | SuperTrendFollowSetup | `bot/strategies/supertrend_follow.py` | rejected by pattern filters, no errors |
| 19 | price_velocity | PriceVelocitySetup | `bot/strategies/price_velocity.py` | ran, no errors |
| 20 | volume_anomaly | VolumeAnomalySetup | `bot/strategies/volume_anomaly.py` | ran, no errors |
| 21 | volume_climax_reversal | VolumeClimaxReversalSetup | `bot/strategies/volume_climax_reversal.py` | ran, no errors |
| 22 | keltner_breakout | KeltnerBreakoutSetup | `bot/strategies/keltner_breakout.py` | ran, no errors |
| 23 | whale_walls | WhaleWallsSetup | `bot/strategies/whale_walls.py` | ran, no errors |
| 24 | spread_strategy | SpreadStrategySetup | `bot/strategies/spread_strategy.py` | ran, no errors |
| 25 | depth_imbalance | DepthImbalanceSetup | `bot/strategies/depth_imbalance.py` | ran, no errors |
| 26 | absorption | AbsorptionSetup | `bot/strategies/absorption.py` | rejected by pattern filters, no errors |
| 27 | aggression_shift | AggressionShiftSetup | `bot/strategies/aggression_shift.py` | recent closed-bar detector, historical hits |
| 28 | liquidation_heatmap | LiquidationHeatmapSetup | `bot/strategies/liquidation_heatmap.py` | ran, no errors |
| 29 | stop_hunt_detection | StopHuntDetectionSetup | `bot/strategies/stop_hunt_detection.py` | bounded recent sweep window, historical hits |
| 30 | multi_tf_trend | MultiTFTrendSetup | `bot/strategies/multi_tf_trend.py` | rejected by pattern filters, no errors |
| 31 | rsi_divergence_bottom | RSIDivergenceBottomSetup | `bot/strategies/rsi_divergence_bottom.py` | ran, no errors |
| 32 | wyckoff_spring | WyckoffSpringSetup | `bot/strategies/wyckoff_spring.py` | ran, no errors |
| 33 | bb_squeeze | BBSqueezeSetup | `bot/strategies/bb_squeeze.py` | ran, no errors |
| 34 | atr_expansion | ATRExpansionSetup | `bot/strategies/atr_expansion.py` | recent closed-bar detector, historical hits |
| 35 | ls_ratio_extreme | LSRatioExtremeSetup | `bot/strategies/ls_ratio_extreme.py` | ran, no errors |
| 36 | oi_divergence | OIDivergenceSetup | `bot/strategies/oi_divergence.py` | ran, no errors |
| 37 | btc_correlation | BTCCorrelationSetup | `bot/strategies/btc_correlation.py` | ran, no errors |
| 38 | altcoin_season_index | AltcoinSeasonIndexSetup | `bot/strategies/altcoin_season_index.py` | rejected by pattern filters, no errors |

## Key Guardrails

| Guardrail | Implementation |
|---|---|
| No auto-trading | Hooks scan for order-placement names; project is delivery-only. |
| Contract validation | `validate_signal_contract()` runs before delivery selection. |
| Hard confluence gate | 3 of 5: trend, momentum, volume, HTF, microstructure. |
| ATR-based levels | Strategy signal contracts require ordered entries, SL, TP1/TP2/TP3, RR gate. |
| HTF protection | Pipeline filters reject trend conflicts; delivery gate blocks opposing HTF regimes. |
| Cooldown | Delivery orchestrator enforces setup and symbol-direction cooldowns. |
| Pinned symbols | BTCUSDT, ETHUSDT, SOLUSDT, XAUUSDT, XAGUSDT, PAXGUSDT. |
| Shortlist scoring | Liquidity, spread/freshness, OI, funding/basis, crowding, microstructure. |

## Verification Snapshot

2026-05-31 checks (`.venv` Python 3.14.5):

| Check | Result |
|---|---|
| `verify_refactor_gate.py` | passed |
| `compileall bot` | passed |
| `validate_config.py` | passed |
| `pytest tests/live/` (PYTEST_LIVE=1) | 6 passed |
| supervised session (2 min, takeover) | cycles=14, delivered=2, no pid conflict |

Run with: `.\.venv\Scripts\python.exe` (requires-python 3.14).

## Known Operational Limits

- Public Binance futures-data endpoints can exhaust request budgets during broad enrichment.
- Book-ticker and microprice columns may be neutral/null in REST-only checks without live WS state.
- Session-bound strategies can be inactive outside their market window; this is now reported as an explicit skip.
- Historical audit uses closed public klines; OI/funding snapshots are attached for detector coverage, while live delivery still uses current enrichment.
- Win-rate claims remain conservative: the rolling audit proves detector activity and contract safety, not production profitability.
