# Static Dependency Audit

Last updated: 2026-05-03

## Scope

- `pyproject.toml`
- `requirements.txt`

## Confirmed State

- The project now keeps one requirements file: `requirements.txt`.
- `requirements.txt` is intentionally all-in-one: runtime, dev/test, live-check, dashboard, telemetry, regime, and ML dependencies.
- `pyproject.toml` still keeps runtime dependencies and optional extras for package metadata / editable installs.
- The consolidated set was resolved on CPython 3.13.13 with pip dry-run.

## Compatibility Notes

- Python target remains `>=3.13,<3.14`.
- Native or platform-sensitive packages are present: `numpy`, `polars`, `aiohttp`, `orjson`, `msgspec`, `hmmlearn`, `scikit-learn`, `statsmodels`, `lightgbm`, `xgboost`, `pyarrow`, and `psutil`.
- `websockets` is constrained to `>=16.0,<17.0`; Binance USD-M routed WebSocket endpoints and the local `websockets.connect(...)` call path were checked against current docs.
- `polars_ta` is included without `TA-Lib`; runtime feature generation now keeps `_HAS_TALIB=False` and `_USE_POLARS_TA_BACKEND=False`, so installed `polars_ta` does not change core indicator warm-up semantics. The local no-TA-Lib smoke test confirmed `polars_ta.ta.SMA` and project EMA fallback work without importing native `talib`.

## Residual Risk

- There is no populated lock file in this repo, so transitive dependency versions can still drift over time.
- Compatibility claims here are based on the current PyPI resolver state and local CPython 3.13.13 Windows environment, not a full clean virtualenv install.
