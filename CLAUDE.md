# Crypto Signal Bot v9

Public Binance USDⓈ-M signal bot → Telegram. **No auto-trading.** No private Binance APIs.

## Solo operator

You execute everything. Human gives direction and acceptance only. Playbook: `docs/SOLO_OPERATOR_PLAYBOOK.md`.

**Loop:** plan → implement → verify (evidence: test output, not "looks done").

## Commands (Makefile)

```bash
make check          # compileall + refactor gate
make validate-config
make live-smoke
make nightly-calibration
```

## Verify (after code changes)

```bash
source .venv/bin/activate
python -m compileall -q bot
python scripts/verify_refactor_gate.py
pytest tests/test_wave_f9_agent_*.py tests/test_wave_f10_agent_*.py tests/test_wave_f11_*.py -q
```

Live (if REST OK): `PYTEST_LIVE=1 pytest tests/live/ -v`

## Delivery invariant

`validate_signal_contract` → `hard_confluence_gate` (3-of-5) → `delivery.deliver` — never bypass.

## Before live / smoke

`python scripts/clean_session_data.py --mode smoke --config config.toml`

## Progressive docs (read when relevant)

| Topic | File |
|-------|------|
| Roadmap P0–P4 | `docs/PROJECT_ROADMAP_AND_STATUS.md` |
| Hot paths | `AGENT_QUICK_START.md` |
| Architecture | `docs/research/ARCHITECTURE_CANONICAL.md` |
| Strategies | `docs/research/STRATEGY_CATALOG.md` |
| AI setup | `docs/CURSOR_CLAUDE_DEV_SETUP.md` |

## Subagents (`.claude/agents/`)

`orchestrator`, `live-ops`, `de-bloat`, `strategy-calibration`, `delivery-guardian`, `verifier`

## Skills (auto-load when relevant)

`.claude/skills/` — live verify, delivery audit, calibration, zero-hit, graphify.

## graphify

If `graphify-out/graph.json` exists: `graphify query "<q>"` before grep; `graphify update .` after edits.
