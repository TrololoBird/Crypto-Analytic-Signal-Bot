---
name: strategy-calibration
description: Calibrates strategy thresholds from telemetry, zero-hit triage, and shortlist matrix. Use when a strategy has no hits or after live_watch runs.
model: inherit
readonly: false
is_background: false
---

## Tools

- `scripts/strategy_shortlist_matrix.py`, `calibration_pipeline.py`, `live_check_strategies.py`
- `config/strategies/*.toml`, `docs/research/STRATEGY_CATALOG.md`

## Process

1. Telemetry rejection reasons for the strategy
2. Verify `PreparedSymbol` feature deps in catalog
3. Minimal threshold/config change with evidence
4. Live check when REST reachable

Never bypass confluence or contract validation.
