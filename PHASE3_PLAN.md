# Phase 3 Plan

Generated: 2026-05-06

## Success Criteria

- Strategy registry imports 37 classes and all have asset-fit profiles.
- Config parses with priority asset overrides present in `config.toml` and `config.toml.example`.
- `btc_correlation` is rejected for `BTCUSDT`.
- Metals use 1h primary timeframe and exclude strategies that are not appropriate for the available data.
- BOS/CHoCH no longer rejects solely because the external swing stop anchor is missing when a valid fallback exists.
- Telemetry analyzer completes on the latest large telemetry run.
- Targeted and full regression tests pass before push.

## File Change List

- `bot/strategy_asset_fit.py`: asset-fit contract and fit scoring.
- `bot/setup_base.py`: strategy decision metadata and asset-fit rejection path.
- `bot/core/engine/engine.py`: runtime asset-fit enforcement.
- `bot/universe.py`: shortlist strategy-fit scoring.
- `bot/config.py`, `config.toml`, `config.toml.example`: pinned asset and per-symbol override contracts.
- `bot/strategies/bos_choch.py`: stop fallback selection.
- `scripts/telemetry_analyzer.py`: scalable telemetry analysis.
- `tests/test_strategy_asset_fit.py`, `tests/test_strategies.py`: regression coverage.

## Verification Plan

- `python -m scripts.validate_config`.
- `python scripts\telemetry_analyzer.py`.
- `python scripts\telemetry_analyzer.py --hours 72 --output logs\telemetry_calibration_analysis_72h.md`.
- `python -m pytest -q tests/test_strategies.py tests/test_strategy_asset_fit.py`.
- `python -m ruff check scripts\telemetry_analyzer.py tests\test_strategies.py bot\strategies\bos_choch.py`.
- Full `python -m pytest -q`.
- Full `python -m ruff check .`.

## Risk And Rollback

- Main risk: strict live validation is not clean because an earlier memory-sampled run logged handled strategy timeouts.
- Rollback path: revert the final commit or isolate runtime timeout changes from asset-fit/telemetry analyzer changes.
- No signed Binance endpoints, account endpoints, order endpoints, private WS, or auto-trading hooks are part of the plan.
