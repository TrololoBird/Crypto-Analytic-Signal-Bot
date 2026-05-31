# Cursor setup — crypto-signal-bot v9

Project-specific AI and editor configuration (created 2026-05-31).

## Rules (`.cursor/rules/`)

| File | Scope |
|------|--------|
| `project-core.mdc` | Always — identity, guardrails, graphify, live tests |
| `python-core.mdc` | `bot/`, `scripts/`, `tests/` |
| `strategies.mdc` | Strategy detectors |
| `features.mdc` | Polars feature pipeline |
| `delivery.mdc` | Contract → confluence → deliver |

## Skills (`.cursor/skills/`)

Invoke by name in chat or when task matches description:

| Skill | Use when |
|-------|----------|
| `live-binance-verify` | After changes; validate against real Binance |
| `refactor-module` | Deleting/rewriting bloated modules |
| `zero-hit-strategy-triage` | Strategy produces no signals |
| `validate-delivery-path` | Auditing signal delivery gates |

## Extensions (`.vscode/extensions.json`)

Recommended: Python, Pylance, Ruff, Even Better TOML, Error Lens, Todo Tree, Markdown All in One.

**Git:** use built-in Source Control (Cursor/VS Code) — no GitLens Pro needed. GitLens is listed as *unwanted* in `extensions.json`.

Install all: **Command Palette → "Extensions: Show Recommended Extensions"**

Or CLI:
```powershell
cursor --install-extension charliermarsh.ruff
```

## Workspace settings (`.vscode/settings.json`)

- Ruff format on save
- Pytest → `tests/live/` with `-m live`
- Interpreter: `.venv/Scripts/python.exe` (Python 3.14)

## User settings updated

`%APPDATA%\Cursor\User\settings.json` — Ruff formatter, Python analysis, format on save.

## Ignored from indexing

`.cursorignore` — data/, graphify-out/, caches, telemetry JSONL.

## Not configured (intentionally)

- MCP filesystem/git/python — redundant with agent tools
- Cursor Automations — create via Automations UI when needed

## Python 3.14

```powershell
winget install Python.Python.3.14
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[live,dev,test]"
```

Reload Cursor window after venv + extensions: **Ctrl+Shift+P → Developer: Reload Window**
