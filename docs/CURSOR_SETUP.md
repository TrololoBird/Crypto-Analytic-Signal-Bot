# Cursor setup — crypto-signal-bot v9

> **Solo playbook:** [SOLO_OPERATOR_PLAYBOOK.md](SOLO_OPERATOR_PLAYBOOK.md)  
> **Extended plan:** [CURSOR_CLAUDE_DEV_SETUP.md](CURSOR_CLAUDE_DEV_SETUP.md)

## Rules (`.cursor/rules/`)

| File | Scope |
|------|--------|
| `project-core.mdc` | Always — identity, guardrails, graphify, live tests |
| `agent-sole-executor.mdc` | Always — agent runs all commands |
| `python-core.mdc` | `bot/`, `scripts/`, `tests/` |
| `strategies.mdc` | Strategy detectors |
| `features.mdc` | Polars feature pipeline |
| `delivery.mdc` | Contract → confluence → deliver |
| `cursor-dev-workflow.mdc` | `.cursor/`, Claude config |

## Slash commands (`.cursor/commands/`)

**Workflow:** `plan-task` → `implement-plan` → `verify` → `handoff`  
**Ops:** `prime-context`, `live-smoke`, `supervised-6h`, `calibrate-run`, `fix-and-verify`  
**Quality:** `wave-tests`, `lint-fix`, `delivery-audit`, `zero-hit`, `de-bloat`, `health-audit`, `graphify`

## Subagents (`.cursor/agents/`)

| Agent | Mode | Use |
|-------|------|-----|
| `orchestrator` | write | Route vague multi-area tasks |
| `live-ops` | write | Supervised sessions, proxy, rollup |
| `de-bloat` | write | F12 module splits |
| `strategy-calibration` | write | Thresholds, zero-hit |
| `delivery-guardian` | **readonly** | Delivery path audit |
| `verifier` | fast | Post-change tests |

Also mirrored in `.claude/agents/` for Claude Code CLI.

## Hooks (`.cursor/hooks.json`)

| Event | Effect |
|-------|--------|
| `sessionStart` | v9 context injection |
| `beforeReadFile` | Block `.env`, `config.toml`, `data/` reads |
| `beforeShellExecution` / `preToolUse` Shell | Guard dangerous git/rm |
| `postToolUse` | Verify hints after bot/ edits |
| `afterFileEdit` | `ruff format` on edited `.py` |

Cloud agents: `sessionStart` does not run on cursor.com/agents — use `/prime-context` instead.

## Skills (`.cursor/skills/`)

`live-binance-verify`, `refactor-module`, `zero-hit-strategy-triage`, `validate-delivery-path`, `supervised-live-session`, `calibration-wave`, `graphify-navigate`

## Workspace (`.vscode/`)

Committed team settings: Ruff format on save, pytest, `.venv` interpreter.

Install extensions: **Command Palette → Extensions: Show Recommended Extensions**

## Indexing (`.cursorignore`)

Excludes `data/`, `telemetry/`, `graphify-out/`, `*.jsonl`, `.venv/` from AI index.

## Python 3.14 (macOS)

```bash
uv python install 3.14 && uv venv .venv --python 3.14
source .venv/bin/activate
uv pip install -e ".[live,dev,test]"
```

## Claude Code

- `CLAUDE.md`, `.claude/settings.json`, `.claude/rules/delivery-invariant.md`
- `/hooks` in CLI to inspect hooks

## Git hooks (local)

Tracked copies: `scripts/git-hooks/pre-push` (auto-trading guard). Not the same as Cursor hooks.

## Not configured (intentional)

- MCP servers — agent tools sufficient for this repo
- Cursor Automations — optional via UI
