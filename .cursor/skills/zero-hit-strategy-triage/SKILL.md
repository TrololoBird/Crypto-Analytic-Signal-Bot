---
name: zero-hit-strategy-triage
description: Diagnoses why a strategy produces zero signals using live checks and telemetry. Use when a strategy has no hits, before changing thresholds, or when calibrating detectors.
---

# Zero-Hit Strategy Triage

## Steps (in order)

1. Confirm registered in `STRATEGY_CLASSES` and enabled in `config.toml`
2. Run: `python -m scripts.live_check_strategies --limit 35 --concurrency 3 --print-summary-json`
3. Inspect `StrategyDecision` rejection reasons in telemetry JSONL
4. Verify required columns exist on `PreparedSymbol` / work frames
5. Trace missing enrichment: REST → WS cache → `SymbolAnalyzer` → market context → OI runner
6. Calibrate **named config thresholds** — not hardcoded magic numbers in strategy files

## Distinguish

| Symptom | Likely cause |
|---------|----------------|
| All strategies zero | Feature pipeline or data feed broken |
| One strategy zero | Detector thresholds or missing column |
| Zero only some symbols | Universe / asset-fit / HTF filter |

## Do not

- Disable strategy without telemetry justification
- Lower confluence/contract gates to force signals
- Add `shift(-N)` or future-bar logic
