# Zero-hit strategy triage

Diagnose why a strategy produces no signals.

## Input

Strategy id or TOML name (e.g. `order_block`, `session_killzone`).

## Workflow

1. Read `config/strategies/<name>.toml` and `bot/strategies/<module>.py`
2. Check `docs/research/STRATEGY_CATALOG.md` for feature deps on `PreparedSymbol`
3. Run live check when REST available:
   ```bash
   python scripts/live_check_strategies.py --strategies <NAME> --limit 5
   ```
4. Inspect telemetry / `strategy_audit` rejection reasons
5. Propose **minimal** threshold or feature fix — never bypass delivery gates

Skill: `zero-hit-strategy-triage`

Do not disable the strategy silently.
