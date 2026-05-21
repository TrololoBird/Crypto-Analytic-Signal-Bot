# Strategies

## Overview

Strategies are implemented in `bot/strategies/` and registered in the modern registry.  
Each strategy produces a `StrategyDecision` that can be accepted, rejected, skipped, or errored.
Roadmap-era detectors are no longer embedded in a monolithic `roadmap.py` file:
each setup has its own module, shared helpers live in
`bot/strategies/roadmap_base.py`, and `roadmap.py` remains only as a
compatibility re-export for old imports.

## Pipeline

1. Frame preparation (`bot/features.py`)
2. Strategy detection (`bot/core/engine.py`)
3. Family precheck + confirmation (`bot/application/symbol_analyzer.py`)
4. Global filters (`bot/filters.py`)
5. Confluence scoring (`bot/confluence.py`)
6. Delivery + tracking (`bot/application/delivery_orchestrator.py`)

## Family model

- `continuation`
- `breakout`
- `reversal`
- `orderbook`
- `orderflow`
- `liquidity`
- `volatility`
- `sentiment`
- `multi_asset`

Family and confirmation profile metadata are attached per strategy and used by symbol-level context checks.
- Continuation/breakout families treat crowd positioning as confirmation/headwind context.
- Reversal families treat crowd positioning as exhaustion context and should not be rejected by continuation-style crowd rules.
- Roadmap families currently use the same confirmation profiles (`trend_follow`,
  `breakout_acceptance`, `countertrend_exhaustion`) and public-data fields on
  `PreparedSymbol`; they are signal detectors, not execution modules.

## Operational notes

- Keep strategy params in `[bot.filters.setups]` config scope.
- Keep setup enable flags in `[bot.setups]`.
- The bot is a Binance public USDⓈ-M market-data and Polars strategy engine.
  Strategy logic should consume `PreparedSymbol` fields and prepared Polars
  frames built from public klines, funding, OI, long/short, taker, depth,
  aggregate-trade, and force-order sources. Do not add account/private endpoints
  to make a detector work.
- Prefer existing Polars feature columns and installed Polars/polars_ta-based
  feature layers over local indicator reimplementation inside strategies.
- All registered setup flags must remain enabled unless a strategy is removed
  from `STRATEGY_CLASSES`; broken strategies are fixed through detector logic,
  explicit reject reasons, or config calibration rather than hidden disablement.
- Strategy status labels such as `experimental` or `beta` are descriptive
  metadata only. They are not a runtime permission to disable a detector.
- A setup that does not produce signals must be diagnosed through decision
  telemetry before thresholds are changed. Classify the cause as missing data,
  stale enrichment, insufficient history, required feature missing, filter gate,
  threshold calibration, context conflict, implementation bug, or market
  condition.
- Generated tests are not evidence of live edge or live correctness. Use
  source tracing, config validation, telemetry, replay/live diagnostics, Binance
  public docs, and real GitHub references when rewriting strategy logic.
- No-signal diagnosis must distinguish market absence from code failure. A
  pattern strategy may reject when the pattern is absent in the current market,
  but it must not reject because of inverted zones, ignored config, stale
  enrichment, impossible confirmation timing, or metadata labels.
- `bot.filters.min_risk_reward` is the default RR floor. Per-setup `min_rr`
  values explicitly override it for that detector.
- Trend-following and breakout signals that oppose confirmed 1h context are
  rejected by `trend_conflict_1h`. Countertrend/exhaustion families
  (`reversal`, `orderflow`, `liquidity`, `sentiment`) are not hard-blocked by
  that conflict; they receive a score penalty and must still pass RR, stop, and
  final-score gates.
- Telegram `why now` companion messages are optional and controlled by
  `bot.notifiers.send_analytics_companion`; the main signal card remains the
  canonical delivery format. The canonical signal card is a compact limit
  signal: entry range, one stop, TP1/TP2, RR, TTL, and tracking reference.
- Add regression coverage when changing decision contracts or metadata.
- When changing structural target logic, preserve the short-side rule that stop anchors come from resistance above entry, not from the nearest arbitrary structure.
- Runtime params must affect detection or target construction, not just defaults:
  - `funding_reversal`: `funding_trend_bars`, `min_delta_threshold`, `sl_buffer_atr`
  - `cvd_divergence`: `min_delta_threshold`, `sl_buffer_atr`
  - `hidden_divergence`: `rsi_divergence_lookback`, `rsi_divergence_threshold`, `min_delta_threshold`, `sl_buffer_atr`
  - `squeeze_setup`: `bb_squeeze_threshold`, `min_bb_compression_width`, `bb_pct_b_threshold`, `volume_threshold`, `sl_buffer_atr`
  - `wick_trap_reversal`: `wick_through_atr_mult`, `closed_back_threshold`, `sl_buffer_atr`

## 2026-05-03 Roadmap Expansion

Added detectors:

- Orderbook: `whale_walls`, `spread_strategy`, `depth_imbalance`
- Order Flow: `absorption`, `aggression_shift`
- Liquidity: `liquidation_heatmap`, `stop_hunt_detection`
- Trend Following: `multi_tf_trend` plus existing `vwap_trend`, `supertrend_follow`
- Pump & Dump: existing `volume_anomaly`, `price_velocity`
- Bottom/Top Picking: `indicator_divergence`, `rsi_divergence_bottom`,
  `wyckoff_spring`, existing `volume_climax_reversal`
- Volatility: `bb_squeeze`, `atr_expansion`, existing `squeeze_setup`
- Sentiment: `ls_ratio_extreme`, `oi_divergence`
- Multi-Asset: `btc_correlation`, `altcoin_season_index`

Implementation caveat: several detectors use the public Binance USDⓈ-M data
surface, not private account/order flow. `whale_walls` uses depth/microprice
imbalance from public WS partial-depth when fresh and public REST
`/fapi/v1/depth` as the cold/stale fallback. `liquidation_heatmap` should
prefer recent public `forceOrder` websocket liquidation sentiment. If a
REST-only diagnostic lacks that stream and uses an exhaustion proxy, the reason
must explicitly label the source, e.g. `source=volume_wick_proxy`; never label a
proxy as `force_order`.

## No-Signal Triage

Use this sequence before changing a strategy:

1. Confirm the setup is exported in `STRATEGY_CLASSES`, present in
   `[bot.setups]`, and has params under `[bot.filters.setups]`.
2. Run a non-test diagnostic that prepares a symbol and records every
   `StrategyDecision` reason for that setup.
3. Verify required columns and enrichment fields on `PreparedSymbol`.
4. Trace missing enrichment to the producer: REST collector, WS cache,
   `SymbolAnalyzer.ws_cache_enrichments`, market context updater, or OI runner.
5. If data exists but the setup never signals, inspect each gate in order and
   calibrate named config thresholds rather than hardcoding one-off values.
6. If the strategy concept itself is weak or mislabeled, rewrite it from a
   real public-data-compatible implementation and update this document.

## 2026-05-21 Spec Contract Pass

The runtime now has a shared Polars spec layer in
`bot/strategies/spec_patterns.py` for the 38-strategy public-data contract.
Strategy modules still own scoring/metadata, but the first detector path for
the price-action, volume, volatility, orderflow, and divergence families uses
the explicit spec thresholds and rejection codes from the current operator
specification.

- Rate limiting: public REST calls go through a client-side request-weight
  budget before hitting Binance, while `/futures/data/*` remains separately
  paced by request-count limits.
- Warmup/data quality: `5m`, `15m`, `1h`, and `4h` frames are required before
  strategy analysis; incomplete kline tails are dropped by `close_time`.
- Shortlist L1: non-pinned symbols must pass `PERPETUAL`/USDT/trading metadata,
  50M quote volume, 0.5 percent 24h movement, 10k 24h trades, and 90 days of
  listing history.
- Diagnostics: every spec miss is a structured `StrategyDecision` rejection;
  hourly rejection rollups are written to `rejection_stats.jsonl`.
- Regime handling: ADX-based trend/range prechecks are family-aware. Trend
  continuation setups are hard-rejected in weak regimes; range/exhaustion
  setups receive a score penalty in very strong trends.
- Market state output must use public-data proxies or explicit diagnostic
  wording. It must not render unknown placeholders such as `n/a`/`н/д` in the
  market-state message.

## 2026-05-21 Zero-Hit Strategy Remediation

Before this pass, the live detector surface had 15 zero-hit strategies on a
20-symbol Binance USD-M sample. Most misses were not missing features; they
were early returns after a strict `spec_patterns` miss, which made the older
config-driven detectors unreachable.

| Strategy | Classification | Concrete issue | Fix |
|---|---|---|---|
| absorption | implementation_bug | strict spec miss hid orderflow/candle fallback | fall through to fallback |
| aggression_shift | implementation_bug | strict delta-vs-price spec hid configured shift proxy | fall through to fallback |
| atr_expansion | implementation_bug | fixed spec TR threshold hid configured recent ATR expansion | fall through to fallback |
| bb_squeeze | implementation_bug | last-bar spec release hid squeeze memory window | fall through to fallback |
| depth_imbalance | source_gate | `rest_book_l1`/`l1_book` public sources were hard-rejected | accept as lower-scored explicit proxy |
| fvg_setup | implementation_bug | strict FVG retest hid SMC zone scanner | fall through to fallback |
| hidden_divergence | implementation_bug | strict 15m pivot spec hid 1h swing scan | fall through to fallback |
| liquidation_heatmap | implementation_bug | documented public volume/wick proxy params were unused | add `source=volume_wick_proxy`; keep `force_order` reserved for real stream data |
| rsi_divergence_bottom | implementation_bug | strict oversold spec hid configured divergence window | fall through to fallback |
| squeeze_setup | implementation_bug | strict BB/KC spec hid prepared squeeze fallback | fall through to fallback |
| stop_hunt_detection | implementation_bug | strict stop-hunt spec hid recent sweep/wick fallback | fall through to fallback |
| structure_pullback | implementation_bug | strict fib-window spec hid trend/pullback-level fallback | fall through to fallback |
| volume_anomaly | implementation_bug | strict latest-candle spec hid recent-bar anomaly fallback | fall through to fallback |
| volume_climax_reversal | implementation_bug | strict climax spec hid configured reclaim fallback | fall through to fallback |
| whale_walls | source_gate | persistent L2 wall pressure absent on REST/L1 diagnostics | allow lower-scored `*_depth_proxy` signal with explicit reason |
| wick_trap_reversal | implementation_bug | strict wick-trap spec hid 1h swing trap fallback | fall through to fallback |
| wyckoff_spring | implementation_bug | strict spring/upthrust spec hid range sweep fallback | fall through to fallback |

Verification on 2026-05-21:

- `python -m compileall bot/strategies` passed.
- `python -m scripts.validate_config` passed.
- `python -m scripts.live_check_strategies --limit 20 --concurrency 3`
  prepared 20 symbols, ran 760 detectors, reported `strategy_errors=[]`, and
  produced `detector_hits > 0` for 35/38 strategies.

Documented market-condition zeroes in that run:

- `funding_reversal`: sampled funding rates were below the configured
  `funding_threshold=0.001`, so `indicator.funding_not_extreme` is expected.
- `ls_ratio_extreme`: current symbols either were not extreme enough or lacked
  contrarian price-position confirmation.
- `supertrend_follow`: current symbols lacked a valid SuperTrend pullback or
  failed volume/ADX gates.

## 2026-05-19 Strategy Logic Remediation

Current remediation target: keep all 38 registered strategies active and repair
detectors that had zero/near-zero participation because of incorrect trading
logic or broken data contracts.

- SMC zone handling: fixed order-block `Top/Bottom` normalization and
  mitigation/invalidation checks. `bos_choch` now accepts both BOS and CHoCH
  structure events and supports fresh retests instead of throwing away BOS.
- Liquidity/false-breakout group: `liquidity_sweep`, `turtle_soup`,
  `wick_trap_reversal`, `stop_hunt_detection`, and
  `volume_climax_reversal` now evaluate recent confirmation windows instead of
  requiring the final candle to contain the full setup.
- Funding/sentiment group: `funding_reversal` uses public Binance funding
  context plus recent price-action confirmation; funding trend, volume, and
  orderflow conflicts are scoring context rather than absolute blockers when
  the reversal setup remains valid.
- Divergence group: `indicator_divergence` detects regular bearish divergence
  and bullish convergence across prepared `rsi14`, `macd_hist`, `obv`, and
  `delta_ratio` columns. It is separate from `hidden_divergence`: hidden
  divergence remains a continuation setup, while regular divergence/convergence
  is a reversal/exhaustion setup. `hidden_divergence` scans recent swing pairs
  rather than only the final two pivots; weak volume is a scoring penalty, not a
  detector-level hard blocker.
- Volatility/momentum group: `squeeze_setup` prefers prepared Polars
  `squeeze_on/squeeze_off/squeeze_hist` columns; `price_velocity` and
  `volume_anomaly` use ATR/body/close-position confirmation with penalties for
  weak context instead of brittle last-candle-only gates.
- Historical note: this pass previously treated `5m` and `4h` as contextual.
  The current runtime contract above supersedes that: all four runtime
  timeframes must be warmed before strategy analysis.
- Outcome/tracking contract: pending signals that never activate are
  `expired_pending` or `unactivated_close` and are excluded from trade
  expectancy and adaptive setup scoring. A stop after TP1 is classified as
  `breakeven_stop`/`trailing_stop` when the realized R confirms it, not as a raw
  loss.
- Risk filter contract: strategy-local structural stops may be tighter than the
  runtime noise floor. Global filters normalize such stops to
  `tracking.min_stop_distance_pct` and recalculate TP1/TP2 to preserve the
  configured minimum RR, instead of rejecting otherwise valid setups as
  `stop_too_tight`.
- Global filter contract: `trend_conflict_1h` remains a hard gate for
  continuation/breakout/trend-following logic, but countertrend setups use
  `trend_conflict_1h_penalized` and `trend_conflict_1h_penalty_applied` so valid
  exhaustion/reversal patterns are judged by confluence instead of suppressed
  before scoring.
- Session/multi-timeframe group: `session_killzone` now merges config/default
  session windows correctly, includes the 22:00-03:00 UTC Asia window, and uses
  pre-open London/NY windows so the bot can monitor the preparation phase;
  `multi_tf_trend` uses vote-based 4h/1h alignment instead of requiring every
  context label to be exactly identical.
- Dashboard interpretation: a strategy with `trades=0` but nonzero
  `detector_hits` is not unverified; it is active at detector level and may be
  waiting for delivery/tracking outcomes. A strategy with `detector_runs>0` and
  `detector_hits=0` needs reason triage before code changes.
- Second pass live-surface fixes: `vwap_trend`, `bb_squeeze`,
  `atr_expansion`, `structure_break_retest`, `absorption`, `wyckoff_spring`,
  `whale_walls`, `spread_strategy`, `aggression_shift`,
  `btc_correlation`, and `altcoin_season_index` now use recent Polars windows
  and score penalties for non-essential context weakness instead of
  last-candle-only hard rejects. `liquidation_heatmap` keeps force-order
  priority and labels diagnostic proxy signals explicitly.
- Verification note: on 2026-05-19, `scripts/live_check_strategies.py --limit
  35 --concurrency 4` registered all 38 strategies, reported
  `strategy_errors=[]`, and produced at least one signal for every registered
  setup on prepared Binance symbols. This is live-surface evidence only, not a
  profitability claim.

Reference implementations checked during the remediation include
`joshyattridge/smart-money-concepts` for SMC concepts,
`SpiralDevelopment/RSI-divergence-detector` for regular/hidden divergence
detection patterns,
`hackingthemarkets/ttm-squeeze` for squeeze release semantics, and
`ntalegeofrey/Supertrend-Strategy-with-Python` for SuperTrend direction/shift
semantics. Indicator calculation should stay on the installed Polars stack,
including `polars_ta` where its output scale matches the bot contract or is
normalized at feature-preparation time. Binance public data constraints were
checked against the official USDⓈ-M market-data docs for klines, funding, OI,
long/short, aggregate-trade, depth, and force-order endpoints/streams.

## 2026-05-14 Live Audit

See `docs/strategy_live_audit_2026-05-14.md` for the live audit against the
running process, telemetry, outcome database, Telegram delivery format, Binance
public-data docs, and the then-current 37-strategy matrix. The current runtime
matrix contains 38 strategies after `indicator_divergence`.
