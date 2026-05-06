# Phase 2 Analysis

Generated: 2026-05-06

## Confirmed Root Causes

1. HIGH: Strategy telemetry is too heavy for naive full-file analysis.
   Evidence: latest run is approximately 515 MB; `scripts/telemetry_analyzer.py` timed out when reading all JSONL files with `read_text()`.
   Fix applied: tail-limited JSONL reading with configurable `--max-lines-per-file`.

2. HIGH: Telemetry analyzer previously missed strategy rows in performance tables.
   Evidence: current telemetry rows use `setup_id`; analyzer filtered only `strategy_id`, producing zero-row tables despite loaded records.
   Fix applied: centralized strategy id extraction from `strategy_id` or `setup_id`.

3. MEDIUM: BOS/CHoCH unit coverage was stale after the stop fallback interface changed.
   Evidence: `tests/test_strategies.py` failed with missing keyword-only arguments `frame`, `break_level`, and `atr`.
   Fix applied: test now passes an empty frame plus zero ATR to force the intended internal-swing fallback path.

4. MEDIUM: Priority assets still show zero signals in the latest tail-limited calibration sample.
   Evidence: `logs/telemetry_calibration_analysis_72h.md` reports 0 signals for BTC, ETH, XAU, and XAG in the sampled rows.
   Status: not fully solved by this pass; current changes improve routing, config, asset-fit, and diagnostics but do not prove live signal-rate recovery.

## Confirmed Existing Improvements In Working Tree

- Asset-fit profiles exist for all 37 registered strategies.
- `btc_correlation` excludes BTC by asset-fit policy.
- `altcoin_season_index` excludes BTC and ETH by asset-fit policy.
- Orderbook/orderflow profiles are restricted by scope/liquidity context.
- Metals have per-asset config exclusions for high-frequency/orderbook strategies and use 1h primary timeframe.
- BOS/CHoCH stop selection has fallback logic beyond external swing anchors.
- WS config uses routed public and market endpoints.

## Inferences

- The high reject rates in latest telemetry may reflect conservative filters, missing contextual data, stale telemetry windows, or all three. The available data does not isolate one universal cause.
- `data.orderbook_context_missing` and `data.depth_imbalance_missing` support the need for strategy-aware routing and data freshness gates.
- Zero priority-asset signal counts remain a calibration risk until a new clean live run produces post-change telemetry.

## Uncertainty

- Current evidence does not prove that priority-asset signal frequency improved after these changes.
- Current evidence does not prove 60-minute memory stability or real Telegram delivery.
