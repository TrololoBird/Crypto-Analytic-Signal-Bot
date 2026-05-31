---
name: live-binance-verify
description: Runs live Binance public API verification after code changes. Use when validating market data, REST/WS, indicators, pipeline, or when the user asks to verify the bot against real Binance.
---

# Live Binance Verification

Truth tests only — no mocked unit tests.

## Prerequisites

```powershell
pip install -e ".[live,dev,test]"
$env:PYTEST_LIVE=1
```

## Quick smoke (2 tests, ~1 min)

```powershell
pytest tests/live/ -v -m live
```

## Full live audit sequence

```powershell
python -m compileall -q bot
python scripts/validate_config.py --config config.toml
pytest tests/live/ -v -m live
python -m scripts.live_check_enrichments --limit 5
python -m scripts.live_check_indicators --symbols BTCUSDT ETHUSDT --concurrency 2
python -m scripts.live_check_pipeline --symbols BTCUSDT --limit 1
python -m scripts.live_check_strategies --limit 10 --concurrency 2
```

## Public-only boundary

Live checks must use only public USDⓈ-M endpoints. If adding endpoints, whitelist in `scripts/live_check_binance_api.py` `PUBLIC_FAPI_PATHS`.

## On failure

1. Distinguish network/rate-limit from code regression
2. Check `MarketDataUnavailable` operation field in logs
3. Do not weaken gates to make tests pass — fix data path or code
