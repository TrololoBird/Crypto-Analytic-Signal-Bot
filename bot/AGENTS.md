# Bot Package Agent

## Scope

Work in top-level modules directly under `bot/`.

## Key Files

- `domain/config.py`: settings loading and config contracts
- `domain/schemas.py`: shared dataclasses and signal/prepared-symbol contracts
- `features.py`: Polars feature preparation and derived market context
- `market_data.py`, `ws_manager.py`: external market I/O and stream handling
- `tracking.py`, `delivery.py`, `messaging.py`: signal lifecycle and notification plumbing
- `setup_base.py`: BaseSetup adapter between strategy classes and engine
- `setups/`: shared strategy helpers with broad blast radius

## Local Rules

- Treat top-level `bot/*.py` modules as shared boundaries. Trace callers and consumers before changing public shapes.
- Model or config changes require checking strategies, application flow, runtime
  config loading, and telemetry impact in the same pass.
- Keep market I/O async and time-sensitive paths lightweight.
- Preserve the runtime logging contract: expected throttling/fallback is
  `info`/`debug`; actionable failures are `error`/`exception`/`critical`.
  Do not add generic warning-level console noise.
- Do not duplicate logic that already belongs in `bot/core/`, `bot/application/`, or `bot/setups/`.
- Changes to `bot/setups/`, `bot/setup_base.py`, or `bot/domain/schemas.py` usually affect multiple strategies; verify representative callers.
- Strategy status labels are descriptive. Do not turn off strategies because of
  `experimental`/`beta` metadata; repair data contracts and detector logic.
- In global filters, keep 1h trend conflict as a hard gate for continuation and
  breakout logic, but only score-penalize countertrend exhaustion setups.
- Generated tests are not proof of trading behavior. Prefer compile/import
  checks, config validation, telemetry replay, and live read-only diagnostics.

## Token Discipline

- Open the specific module first, then follow only direct imports and callers.
- For large files such as `market_data.py`, `ws_manager.py`, or `tracking.py`, read only the exact function or class region you need.

## Verification

- Prefer import/call-site review, `python -m compileall`, config validation, and
  read-only live/telemetry diagnostics proportional to the blast radius.
