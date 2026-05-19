# Architecture Audit 2026-05-19

Scope: runtime code, config, dependencies, strategy orchestration, telemetry from
`data/bot/telemetry/runs/20260519_083816_19992`, and current public external
contracts. Generated tests were not used as proof.

## Confirmed Findings

| Finding | Evidence | Action |
| --- | --- | --- |
| Disabled signal-ML was still imported on every bot startup. | `bot/application/bot.py` constructed `MLFilter`; `ConfluenceEngine` accepted an optional ML filter; `config.toml` had `[bot.ml] use_ml_in_live=true`, but `MLConfig.enabled` defaulted false, so the path was operationally disabled. | Removed `bot/ml`, `bot/ml_filter.py`, `MLConfig`, `[bot.ml]`, and ML telemetry fields from confluence. |
| Signal ML required pandas/model dependencies outside the Polars runtime contract. | `bot/ml/filter.py` converted Polars to pandas for model input; `requirements.txt` carried `joblib`, `lightgbm`, `xgboost`, `optuna`, `pandas`, and `pyarrow`. Polars documents `to_pandas()` as requiring pandas and pyarrow and copying data unless PyArrow extension arrays are used. | Removed those dependencies and made `prepare_symbol` reject pandas inputs explicitly. |
| Self-learning/Optuna path was dead code. | `rg` found `SelfLearner`, `WalkForwardOptimizer`, and `RegimeAwareParams` only in their own modules and stale docs, not in runtime call paths. | Removed `bot/core/self_learner.py`, `bot/learning/*`, and `optuna`. |
| Strategy execution queue was global across symbols. | Runtime telemetry showed `runtime.strategy_queue_stale` with `queue_wait_ms` around 45-51 seconds. Code used one `SignalEngine._semaphore` for all symbols, so one symbol's strategy batch blocked unrelated symbols. | Replaced the global strategy semaphore with a per-symbol semaphore while keeping the global threadpool and existing `analysis_concurrency` symbol limit. |
| Stale remediation docs contradicted current runtime. | April remediation docs still described live ML guardrails and old dependency state. | Removed the stale remediation/dependency audit bundle and replaced the README link with this current audit. |
| Tracked `coverage.xml` was generated and stale. | It referenced removed modules such as `core/self_learner.py`. | Removed it from the repo. |

## External Verification

- Binance USD-M klines remain public market data at `GET /fapi/v1/klines`;
  official docs list default limit 500 and max 1500:
  https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Kline-Candlestick-Data
- Binance USD-M open interest history remains public at
  `GET /futures/data/openInterestHist`; official docs list max 500 and the
  public request limit:
  https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Open-Interest-Statistics
- Polars lazy execution remains the preferred path for larger query plans and
  avoids unnecessary intermediate work:
  https://docs.pola.rs/user-guide/concepts/lazy-api/
- Polars `DataFrame.to_pandas()` requires pandas and pyarrow, so it is not part
  of the runtime signal path:
  https://docs.pola.rs/api/python/stable/reference/dataframe/api/polars.DataFrame.to_pandas.html

## Removed

- `bot/ml/*`: disabled signal classifier, training pipeline, guardrails, and
  volatility gate.
- `bot/ml_filter.py`: legacy shim for the removed ML filter.
- `bot/core/self_learner.py` and `bot/learning/*`: uncalled Optuna/walk-forward
  optimization scaffolding.
- Heavy unused dependencies: `joblib`, `lightgbm`, `xgboost`, `optuna`,
  `pandas`, `pyarrow`.
- `scripts/validate_local.ps1`: stale test-centric script with ML test targets.
- Stale remediation/dependency docs and generated `coverage.xml`.

## Refactored

- `ConfluenceEngine` is now deterministic: it blends setup prior and scoring
  components only. No ML probability, confidence, skip reason, or model boost is
  present in `ConfluenceResult`.
- `SignalEngine` now uses per-symbol strategy concurrency. This keeps each
  symbol's detector batch bounded without turning global backlog into false
  no-signal rows.
- Runtime config and docs now describe strategy concurrency as per-symbol and
  separate from global symbol analysis concurrency.

## Still Active Runtime Contracts

- Signal-only scope: no order execution or private Binance endpoints.
- Public Binance USD-M market data only.
- Polars-native runtime feature frames.
- Strategy decisions must emit a signal or a precise structured reason.
- Generated tests remain diagnostics only and were not staged in this pass.

## Follow-Up Risks

- Existing live telemetry collected before this refactor remains mixed with old
  behavior. Restart the running bot before judging the new strategy distribution.
- Some zero-signal strategy rows can still be valid market-condition rejects;
  after restart, inspect `strategy_decisions.jsonl` for reasons other than
  `runtime.strategy_queue_stale`.
- `scikit-learn` remains because `bot/regime/gmm_var.py` uses
  `GaussianMixture` for the regime detector. That is separate from the removed
  signal-ML path.
