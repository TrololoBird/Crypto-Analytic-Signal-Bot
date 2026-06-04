# Implement approved plan

Execute the plan from the current conversation. Agent owns all execution.

## Before coding

- `source .venv/bin/activate`
- If plan touches live: `python scripts/clean_session_data.py --mode smoke --config config.toml`

## During

- Read each file before editing
- Match project conventions (Polars, Wilder ATR/RSI, no `shift(-N)` on live path)
- One logical commit-worth of scope; do not expand scope

## After

1. `/lint-fix` on changed paths
2. `/verify` or delegate `verifier` subagent
3. `make graphify-update` if graphify installed
4. Report: what changed, verify evidence, open risks

If verify fails — fix and re-run; do not ask human to debug unless blocked (network/geo).
