# Feature pipeline (v9)

**Hot path:** `bot/features/prepare.py` → `prepare_frame.py` → `PreparedSymbol`.

| Layer | Implementation |
|---|---|
| Core TA | Pure Polars + Wilder smoothing (`shared.wilder_mean`) |
| Hunt accel | `polars_ta` (`ta`/`tdx`/`wq`/`candles`) via `hunt_core/features/polars_ta_bridge.py` |
| Rolling OLS | `polars-ols` (pinned slope features) |
| Optional research | `polars-trading`, `polars-ds` via `hunt/[research]` — offline only |
| Not used | TA-Lib, pandas, shift(-N) on live bars |

BB rolling std uses **ddof=0** (population std, TradingView/Binance parity). RSI/ATR/ADX follow Wilder (RMA seed). Secondary rolling stats (e.g. vwap deviation z-score) may use ddof=1 where noted in `prepare_frame.py`.

Strategies read columns from `PreparedSymbol` / frame tails — no duplicate indicator trees.

Spot lead-lag (optional): enable `[bot.spot_companion]` — fills `spot_lead_return_1m`, `spot_futures_spread_bps`.
