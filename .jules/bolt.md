## 2026-04-10 - [Vectorized kline frame construction]
**Learning:** Row-based Python loops for converting list-of-lists (e.g., from Binance REST API) to Polars DataFrames are a significant performance bottleneck. Even for relatively small datasets (~1000 rows), the overhead of creating Python dictionaries and handling individual objects is high. Vectorizing the construction and using Polars' native type casting and epoch conversion provides a ~75-80% speedup.
**Action:** Always prefer `pl.DataFrame(rows, schema, orient="row")` followed by `with_columns` for type conversion when processing structured data from APIs. Avoid `pl.DataFrame([{"col": val, ...}, ...])`.

## 2026-04-12 - [Vectorizing Wilder's Smoothing]
**Learning:** Wilder's Smoothing (used in RSI, ATR, ADX) is mathematically equivalent to an EWMA with alpha = 1/period, but traditionally requires a Simple Moving Average (SMA) as the seed value for the first window. Vectorizing this in Polars by prepending the seed SMA to the subsequent raw values and applying `ewm_mean(alpha=1/period, adjust=False)` provides a ~10-20x speedup compared to Python-level loops and eliminates expensive `to_list()` conversions.
**Action:** Replace iterative financial indicators with Polars-native `ewm_mean` or `rolling_map` (if non-recursive) to maintain data in Arrow format and maximize throughput.
