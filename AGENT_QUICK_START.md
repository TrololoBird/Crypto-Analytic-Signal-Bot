# AGENT QUICK START

## What This Is

Signal-only Binance public-data analytics bot. It sends manual trading signals to Telegram.
No auto-trading, no order placement, no private Binance endpoints.

## Read First

1. `bot/domain/config.py` — settings, pinned symbols, thresholds.
2. `bot/runtime/shortlist_service.py` — shortlist refresh and enrichment.
3. `bot/market/universe.py` — dynamic shortlist scoring and strategy routing.
4. `bot/features/prepare_frame.py` + `bot/features/prepare.py` — Polars indicator pipeline.
5. `bot/strategies/` — 38 canonical detectors.
6. `bot/runtime/delivery_orchestrator.py` — contract, confluence, delivery gate.
7. `bot/delivery/contract.py` — immutable signal contract rules.

## Session hygiene

Before each live run or smoke test, wipe stale artifacts:

```bash
python scripts/clean_session_data.py --mode full --config config.toml
```

Modes: `telemetry` · `smoke` (+ logs/live_watch) · `full` (+ reset `bot.db`).

## Rules That Matter

- Never add auto-trading.
- Never bypass `validate_signal_contract()`.
- Never send a signal that fails the hard 3-of-5 confluence gate.
- Keep PAXGUSDT in pinned symbols with BTCUSDT, ETHUSDT, SOLUSDT, XAUUSDT, XAGUSDT.
- Use Wilder smoothing for ATR/RSI. BB std uses `ddof=1`.
- Do not use `shift(-N)` or future bars for live signals.

## Run

```bash
source .venv/bin/activate
pip install -e ".[live,dev,test]"
python scripts/validate_config.py --config config.toml
python main.py run
```

Ops Makefile targets: `make nightly-calibration`, `reconcile-defaults`, `shortlist-matrix`, `graphify-update`.

**graphify (architecture queries):** `make graphify-install` once → `graphify query "<question>"`. See [docs/GRAPHIFY_SETUP.md](docs/GRAPHIFY_SETUP.md).

Dry run without Telegram: `provider = "none"` in config or `BOT_NOTIFIER_PROVIDER=none`.

## Verify

```bash
python scripts/clean_session_data.py --mode full --config config.toml
pytest tests/ -q --ignore=tests/live
PYTEST_LIVE=1 pytest tests/live/ -v
python scripts/live_check_pipeline.py --symbols BTCUSDT ETHUSDT SOLUSDT --limit 3
python scripts/live_smoke_bot.py --runtime-seconds 960 --clean-mode full --keep-session-data
python scripts/funnel_report.py
```

## Delivery Trace To Preserve

```text
strategy Signal
  -> validate_signal_contract
  -> hard_confluence_gate
  -> delivery.deliver
```

## Canonical docs

- Architecture: `docs/research/ARCHITECTURE_CANONICAL.md`
- Improvement backlog: `docs/IMPROVEMENT_PLAN.md`
- Indicators: `docs/features/INDICATORS.md`
