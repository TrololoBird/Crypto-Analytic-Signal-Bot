# GitHub setup for Cursor agent

Read and follow **docs/GITHUB_CURSOR_SETUP.md**.

## Quick verify (after user adds GITHUB_TOKEN to `.env`)

```bash
chmod +x scripts/gh_with_env_token.sh scripts/github_push.sh scripts/verify_github_token.sh
./scripts/verify_github_token.sh
```

## Agent push when `gh auth` lacks workflow scope

```bash
./scripts/github_push.sh main
```

## MCP (optional)

```bash
cp .cursor/mcp.json.example ~/.cursor/mcp.json
# export GITHUB_TOKEN in shell profile; restart Cursor
```

Do **not** commit raw tokens. `.env` is gitignored.
