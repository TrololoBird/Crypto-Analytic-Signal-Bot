# Prime v9 context

Load project context before coding. **You execute everything** — user is architect only.

## Read (in order)

1. @AGENT_QUICK_START.md
2. @docs/PROJECT_ROADMAP_AND_STATUS.md — current P0–P4 priorities
3. @docs/research/ARCHITECTURE_CANONICAL.md — package map
4. If `graphify-out/graph.json` exists: run `graphify query "delivery orchestrator pipeline shortlist"` and use the subgraph.

## Confirm

- Python **3.14.5** via `.venv` (`source .venv/bin/activate`)
- `config.toml` exists (copy from `config.toml.example` if missing)
- Delivery invariant: `validate_signal_contract` → `hard_confluence_gate` → `deliver`
- No auto-trading, no private Binance endpoints

Reply with: current phase (F11/F12/P0), top 3 open tasks from roadmap, and which verification you will run after edits.
