[DECISION] Runtime | Keep `main.py -> bot.cli -> SignalBot` | Confirmed active call path; no alternate entry point needed.
[DECISION] Binance markets | USD-M Futures primary | Live REST found 563 trading USDT USD-M symbols and funding/OI endpoints; current strategies consume futures-only context.
[DECISION] Spot market | Companion/context only | Live Spot has 432 trading USDT symbols, but no funding/OI/long-short context for current futures strategies.
[DECISION] Coin-M market | Optional benchmark, not primary universe | Live Coin-M has 20 trading perpetuals and inverse volume fields; it is too small and structurally different for the main USDT signal channel.
[DECISION] WebSocket routing | Keep routed USD-M public/market endpoints | Live WS checks received Spot, USD-M `/public` bookTicker, USD-M `/market` ticker, and Coin-M ticker payloads.
[DECISION] Shortlist | Per-strategy seeds plus per-symbol strategy_fits | Volume-only is not the final model; shortlist must include trend, breakout, reversal, liquidity, funding, and crowding proxy pools.
[DECISION] Strategy execution | Gate strategies by `UniverseSymbol.strategy_fits` when present | This makes the universe per-strategy instead of running all setups over one shared pool.
[DECISION] Indicators | No direct `talib` import | `polars_ta` is installed (`ta` 89 public names, `tdx` 209, `wq` 242), but TA-Lib remains a brittle native dependency on Windows/Python 3.13.
[DECISION] Stop placement | Structure + ATR floor | Prior telemetry had `stop_too_tight` and target-distance rejects; tiny 0.15/0.2/0.4 ATR buffers are too fragile for wick-touch tracking.
[DECISION] Targets | Use RR fallback when pattern is valid and nearest structure is too close | Rejecting only because TP1 is missing/near hides otherwise useful raw setup telemetry.
[DECISION] Telegram | Deliver RAW/CANDIDATE/SELECTED with tracking refs | Audit text without unique refs can be suppressed by duplicate hashing.
[DECISION] ML | Keep disabled for live scoring | Confirmed `use_ml_in_live=false`; only 21 recorded outcomes from `status`, so enabling a trained model would be weakly supported.
[DECISION] Dashboard | Keep as local monitor, not signal product | Telegram is the production output; dashboard is secondary and not on the critical signal path.
[ACTION] `bot/universe.py` | Add per-strategy fits and seed coverage | Build different symbol pools for different setup types.
[ACTION] `bot/core/engine/engine.py` | Execute only fitted strategies when fits exist | Enforce per-strategy universe at runtime.
[ACTION] `bot/delivery.py` | Include tracking ref/time in audit text | Prevent false Telegram duplicate suppression of raw/candidate/selected stages.
[ACTION] `bot/features.py` | Remove direct TA-Lib dependency gate | Keep indicator preparation portable on Windows/Python 3.13.
[ACTION] `bot/strategies/*` | Widen configured ATR buffers and RR fallbacks | Reduce noise stops and target-distance dropouts.
[LIVE_TEST] External API | Spot/USD-M/Coin-M REST+WS verified | Evidence: Spot 432 USDT trading; USD-M 563 USDT trading and 567 perpetual; Coin-M 20 perpetual; WS payloads received from all checked hosts.
[LIVE_TEST] Shortlist | Per-strategy coverage verified | Evidence: manual live rebuild size=45, strategy_seed=15, fvg_setup=41, funding_reversal=12, every setup fit count >0.
[LIVE_TEST] Bounded runtime | RAW/CANDIDATE/SELECTED delivery verified | Evidence: run `20260502_021805_10972`, Telegram message IDs 4405-4476, selected=5, log-level errors/warnings=0, HTTP 429/418 status evidence=0.
