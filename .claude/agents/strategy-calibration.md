---
name: strategy-calibration
description: Calibrates strategy thresholds from telemetry, zero-hit triage, and shortlist matrix. Use when a strategy has no hits or after live_watch runs.
tools: Bash, Read, Grep, Glob
---

## Tools

- `scripts/strategy_shortlist_matrix.py`
- `scripts/calibration_pipeline.py`
- `scripts/live_check_strategies.py`
- `config/strategies/*.toml`
- `docs/research/STRATEGY_CATALOG.md`

## Process

1. Identify strategy + rejection reasons from telemetry
2. Verify `PreparedSymbol` feature deps
3. Propose minimal threshold/config change with evidence
4. Live check when REST available

Never bypass confluence or contract validation.
