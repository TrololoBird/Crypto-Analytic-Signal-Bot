# Remediation Completion Plan (v3 follow-up)

## Goal

Close remaining architecture and reliability gaps from the v3 review while keeping runtime behavior stable.

## Status snapshot

| Track | Current status | Notes |
|---|---|---|
| Task 1: `bot.py` monolith | ✅ Completed | `bot/application/bot.py` reduced below 35 KB via handler/runner extraction. |
| Task 2: Regime naming clarity | ✅ Completed | `CompositeRegimeAnalyzer` uses `rule_based`/`centroid` internals with back-compat aliases. |
| Task 3: ML filter safety | 🟡 In progress | Centroid fallback now explicitly treated as baseline and disabled for live ML scoring. |
| Task 4: Microstructure | ✅ Completed | Extracted to `bot/features_microstructure.py`. |
| Task 5: Self-learning stubs | ✅ Completed | `WalkForwardOptimizer`, `RegimeAwareParams`, `OutcomeStore` implemented and wired. |
| Task 6: Test decomposition | ✅ Completed | Remediation regressions are split into thematic `test_regression_suite_*` suites, including dedicated runtime/intra-candle and indicators suites plus legacy compatibility shims. |
| Task 7: Backtest | ✅ Completed | Already present and unchanged in this sequence. |
| Task 8: Docs | ✅ Completed | Architecture/operations/remediation docs are synchronized with current module/test topology and CI doc-change guardrails. |
| ws_manager decomposition | 🟡 In progress | Enrichment + cache/update helpers extracted; connection/subscriptions extraction remains. |
| features decomposition | 🟡 In progress | Microstructure + structure helpers extracted; core/advanced/oscillator splits remain. |

## Execution phases

1. **Safety and correctness first**
   - Keep runtime guardrails for ML fallback strict.
   - Maintain targeted regression tests for each extracted component.

2. **Monolith splitting by seams**
   - Continue extracting `ws_manager.py` into `bot/websocket/{connection,subscriptions,cache,enrichment}.py`.
   - Continue extracting `features.py` into thematic modules (`core`, `advanced`, `oscillators`, `structure`, `microstructure`).

3. **Cleanup and consolidation**
   - Keep remediation suites topic-oriented with the `test_regression_suite_*` naming pattern and marker-based triage (`regression_remediation*`).
   - Reconcile docs after each extraction chunk.

## Definition of done

- No single orchestration file acts as a god-class for mixed concerns.
- ML live-path guardrails are explicit and fail-safe for baseline fallback models.
- Learning components are operational without Optuna.
- Docs and remediation tests reflect the actual module topology.
