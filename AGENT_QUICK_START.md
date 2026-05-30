# AGENT QUICK START

## What This Is

Signal-only Binance public-data analytics bot. It sends manual trading signals to Telegram.
No auto-trading, no order placement, no private Binance endpoints.

## Read First

1. `bot/domain/config.py` - settings, pinned symbols, thresholds.
2. `bot/application/shortlist_service.py` - how market candidates are enriched.
3. `bot/universe.py` - dynamic shortlist scoring and strategy routing.
4. `bot/features.py` and `bot/features_shared.py` - indicator math.
5. `bot/strategies/__init__.py` and `bot/strategies/` - 38 strategies.
6. `bot/application/delivery_orchestrator.py` - final validation and Telegram delivery gate.
7. `bot/signal_contract.py` - immutable signal contract rules.

## Rules That Matter

- Never add auto-trading.
- Never bypass `validate_signal_contract()`.
- Never send a signal that fails the hard 3-of-5 confluence gate.
- Keep PAXGUSDT in pinned symbols with BTCUSDT, ETHUSDT, SOLUSDT, XAUUSDT, XAGUSDT.
- Use Wilder smoothing for ATR/RSI. RSI seed must be SMA seed, then Wilder smoothing.
- Bollinger Band std uses `ddof=1`.
- Do not use `shift(-N)` or future bars for live signals.

## Run

```powershell
python -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
pip install -e ".[live,dev,test]"
python scripts\\validate_config.py
python main.py
```

Production needs Telegram token/chat configuration in the local environment or config.

## Verify

```powershell
pytest -q
python scripts\\live_check_binance_api.py
python scripts\\live_check_indicators.py
python scripts\\live_check_pipeline.py --symbols BTCUSDT ETHUSDT SOLUSDT --limit 3 --concurrency 2
python scripts\\live_check_strategies.py --symbols BTCUSDT ETHUSDT SOLUSDT --limit 3 --concurrency 2 --require-signal-contract --summary-json data\\bot\\telemetry\\strategy_audit_after_codex.json --print-summary-json
python scripts\\historical_strategy_audit.py --symbols BTCUSDT ETHUSDT SOLUSDT BNBUSDT XRPUSDT DOGEUSDT ADAUSDT LINKUSDT AVAXUSDT LTCUSDT --days 30 --warmup-days 45 --window-step-bars 48 --max-windows-per-symbol 8 --concurrency 3 --require-registered 38 --require-contract-clean --require-no-zero-signals
```

Expected baseline on 2026-05-26:

- 30 tests pass.
- 38 strategies registered/evaluated.
- Strategy errors: 0.
- Signal contract failures: 0.
- Top-10 historical rolling audit: 80 windows, 3040 detector runs, 38/38 strategies hit, 0 contract failures.

## Delivery Trace To Preserve

```text
strategy Signal
  -> signal_contract.validate
  -> hard_confluence_gate
  -> delivery.deliver
```

Any path that reaches `delivery.deliver` without the first two steps is a critical bug.

## Current Audit Scripts

- `scripts/live_check_pipeline.py` proves the dry delivery funnel on current public data.
- `scripts/live_check_strategies.py` proves live detector surface and signal-contract rows.
- `scripts/historical_strategy_audit.py` proves closed-candle rolling activity for all 38 strategies.
- `scripts/live_smoke_bot.py --runtime-seconds 1200` runs the 20-minute end-to-end public-data smoke without Telegram sends.

## Hooks

Local hooks are installed under `.git/hooks/`. Tracked copies live under `scripts/git-hooks/`.
Use them if the local `.git` directory is recreated.
