## Crypto Signal Bot v9 — agent entry (lean)

**Product:** public Binance → Telegram trade plans. **No** auto-trade, **no** private API, **no** user registration.

**v1 status:** [docs/DEFINITION_OF_DONE.md](docs/DEFINITION_OF_DONE.md) — единственный backlog; **не** генерировать списки «50 улучшений».

**Token policy:** [docs/AGENT_TOKEN_POLICY.md](docs/AGENT_TOKEN_POLICY.md) — что читать / не читать.

**Quick start:** [AGENT_QUICK_START.md](AGENT_QUICK_START.md)

**Solo ops:** [docs/SOLO_OPERATOR_PLAYBOOK.md](docs/SOLO_OPERATOR_PLAYBOOK.md) — human directs, agent executes all commands.

**GitHub token / CI / Cursor MCP:** [docs/GITHUB_CURSOR_SETUP.md](docs/GITHUB_CURSOR_SETUP.md) — `GITHUB_TOKEN` in `.env`, `./scripts/verify_github_token.sh`.

## Invariants

- Delivery: `validate_signal_contract` → `hard_confluence_gate` (3/5) → `deliver`
- Python **3.14** `.venv` only
- Before live: `python scripts/clean_session_data.py --mode smoke --config config.toml`

## Verify

```bash
make check
pytest tests/test_wave_f9_agent_*.py tests/test_wave_f10_agent_*.py tests/test_wave_i_calibration.py -q
```

## graphify

If `graphify-out/graph.json` exists: `graphify query "<q>"` before broad grep; `graphify update .` after `bot/` edits.

## LLM in bot

Hot path: **no**. Optional layer: [docs/research/LLM_API_INTEGRATION.md](docs/research/LLM_API_INTEGRATION.md). Cursor API = dev only.

## Cloud / deps

See [docs/CURSOR_CLAUDE_DEV_SETUP.md](docs/CURSOR_CLAUDE_DEV_SETUP.md) § Cloud (Python 3.14 via uv).
