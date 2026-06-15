# Hunt library stack (canonical)

Signal-only hunter on **Polars + CCXT + aiogram**. No `engine`/`bot` imports.

## Install

```bash
pip install -e hunt/
pip install -e "hunt/[dev]"          # ruff, mypy
pip install -e "hunt/[calibrate]"    # polarbt, ml4t-engineer (offline only)
```

## Install gate

```bash
python -c "from hunt_core.bootstrap import require_feature_stack; require_feature_stack()"
```

## Core dependencies

| Package | Role |
|---------|------|
| `polars` | OHLCV frame pipeline |
| `polars_ta` | TA/tdx/wq/candles Expr (RSI, MACD, ADX, patterns) |
| `polars-ols` | Rolling least squares (trend_slope, close_ols_slope) |
| `polars-ds` | Entropy, KS tests (regime features) |
| `polars-trading` | Sharpe / drawdown research columns |
| `bottleneck` | Fast rolling in microstructure |
| `numpy` | PSAR / Fisher numeric loops |
| `ccxt` | Binance USD-M REST + Pro WS |
| `aiohttp` + `aiohttp-socks` | Proxy + intel HTTP |
| `aiogram` | Telegram `/signal`, `/signals` |
| `pydantic` | Settings / schemas |
| `structlog` | JSON session logs |
| `tenacity` | REST/TG retries |
| `python-dotenv` | Secrets |

**Removed (unused):** `msgspec`, `tomlkit`, `scipy` optional group, `perf`/`research` extras.

**Transitive (do not import in hunt):** `pandas`, `numba` via `polars_ta`.

## Optional `[calibrate]`

| Package | Role |
|---------|------|
| `polarbt` | Polars backtest for threshold sweeps |
| `ml4t-engineer` | VPIN, dollar bars, triple-barrier labels |

Wired in `hunt_research/calibrate_extras.py` only — not hot path.

## Anti-patterns (do not add)

- `pandas` / `pandas-ta` / `TA-Lib` / `polars_talib` — legacy Freqtrade stack
- `vectorbt` / `backtrader` — pandas-centric backtesters
- `fastapi` / `sqlalchemy` / `redis` / `celery` — execution-bot infra
- `engine.*` / `bot.*` — architectural violation

## GitHub reference survey (25 projects)

### Execution bots (lessons only)

| Repo | Stack takeaway |
|------|----------------|
| [freqtrade](https://github.com/freqtrade/freqtrade) | CCXT + enableRateLimit; avoid pandas-ta monolith |
| [jesse-ai/jesse](https://github.com/jesse-ai/jesse) | numpy indicators — heavy; not Polars |
| [OctoBot](https://github.com/Drakkar-Software/OctoBot) | CCXT via adapter layer |
| [grid_trading_bot](https://github.com/jordantete/grid_trading_bot) | asyncio + ccxt + sqlite recovery |
| [kaspa-trading-bot](https://github.com/lorine93s/kaspa-trading-bot) | pydantic + structlog pattern |

### Signal-only alerts

| Repo | Stack takeaway |
|------|----------------|
| [CryptoSignal/Crypto-Signal](https://github.com/CryptoSignal/Crypto-Signal) | Alert product model; pandas debt |
| [Eptelligence/Candlestick-Signal-Bot](https://github.com/Eptelligence/Candlestick-Signal-Bot) | Read-only ccxt screener |

### Polars-native TA

| Repo | Hunt decision |
|------|---------------|
| [wukan1986/polars_ta](https://github.com/wukan1986/polars_ta) | **Core** |
| [abstractqqq/polars_ds_extension](https://github.com/abstractqqq/polars_ds_extension) | **Core** |
| [ngriffiths13/polars-trading](https://github.com/ngriffiths13/polars-trading) | **Core** |
| [ml4t/engineer](https://github.com/ml4t/engineer) | **Calibrate optional** |
| [nikkisora/PolarBT](https://github.com/nikkisora/PolarBT) | **Calibrate optional** |
| [Yvictor/polars_pbv](https://github.com/Yvictor/polars_pbv) | **Watch** — POC parity vs Prizrak |
| [lavs9/quantwave](https://github.com/lavs9/quantwave) | Watch — overlaps polars_ta |

## Python 3.14 notes

- Polars 1.38+ supports cp314 ([pola-rs/polars#25035](https://github.com/pola-rs/polars/issues/25035)).
- `polars_ta.ta.SMA/WMA/KAMA/LINEARREG` may fail on 3.14 — keep pure-Polars in `prepare_frame` for those only (`BROKEN_PLTA_FUNCTIONS`).

## LOC cuts (library-first)

| Module | Action |
|--------|--------|
| `prepare_frame.py` | polars_ta-only core indicators |
| `polars_ta_bridge.py` | direct imports, no optional fallbacks |
| `research_plugins.py` | core deps required |
| `candle_patterns.py` | `polars_ta.candles` only |
| `pivots.py` | removed numba path |
| `volume_profile.py` | custom VP until `polars_pbv` parity proven |
