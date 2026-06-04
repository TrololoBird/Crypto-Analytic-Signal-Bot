# Feature pipeline (v9)

**Hot path:** `bot/features/prepare.py` → `prepare_frame.py` → `PreparedSymbol`.

| Layer | Implementation |
|---|---|
| Core TA | Pure Polars + Wilder smoothing (`shared.wilder_mean`) |
| Optional accel | `polars_ta` in `[live]` extra — EMA, ROC, OBV only |
| Not used | TA-Lib, pandas, shift(-N) on live bars |

BB rolling std uses **ddof=1**. RSI/ATR/ADX follow Wilder (RMA seed).

Strategies read columns from `PreparedSymbol` / frame tails — no duplicate indicator trees.

Spot lead-lag (optional): enable `[bot.spot_companion]` — fills `spot_lead_return_1m`, `spot_futures_spread_bps`.
