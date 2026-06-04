# Lint and format (Ruff)

Fix lint on changed Python files. Agent runs commands.

```bash
source .venv/bin/activate
ruff check bot/ scripts/ tests/ --fix
ruff format bot/ scripts/ tests/
```

If only specific files changed, scope ruff to those paths. Report remaining violations.
