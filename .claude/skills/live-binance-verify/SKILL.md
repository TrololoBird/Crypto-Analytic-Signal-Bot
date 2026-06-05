---
name: live-binance-verify
description: Runs live Binance public API verification after code changes. Use when validating market data, REST/WS, indicators, pipeline, or when the user asks to verify the bot against real Binance.
---

# Live Binance Verification

Truth tests only — no mocked unit tests.

## Prerequisites

```bash
pip install -e ".[live,dev,test]"
export PYTEST_LIVE=1
```

## Quick smoke (~1 min)

```bash
pytest tests/live/ -v -m live
```

## Full live audit sequence

```bash
python -m compileall -q bot
python scripts/validate_config.py --config config.toml
pytest tests/live/ -v -m live
python scripts/live_check_enrichments.py --limit 5
python scripts/live_check_indicators.py --symbols BTCUSDT ETHUSDT --concurrency 2
python scripts/live_check_pipeline.py --symbols BTCUSDT --limit 1
python scripts/live_check_strategies.py --limit 10 --concurrency 2
```

## Public-only boundary

Live checks must use only public USDⓈ-M endpoints. If adding endpoints, whitelist in `scripts/live_check_binance_api.py` `PUBLIC_FAPI_PATHS`.

## On failure

1. Distinguish network/rate-limit from code regression
2. Check `MarketDataUnavailable` operation field in logs
3. Do not weaken gates to make tests pass — fix data path or code
