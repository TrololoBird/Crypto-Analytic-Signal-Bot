#!/usr/bin/env bash
# Install graphify CLI + Cursor/Claude Code project integration + initial graph build.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PATH="${HOME}/.local/bin:${PATH}"

echo "=== graphify setup (Crypto Signal Bot v9) ==="

if ! command -v uv >/dev/null 2>&1; then
  echo "[ERROR] uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

echo "[1/5] Installing graphifyy CLI (uv tool)..."
uv tool install graphifyy

if ! command -v graphify >/dev/null 2>&1; then
  echo "[ERROR] graphify not on PATH after install. Add ~/.local/bin to PATH." >&2
  exit 1
fi
graphify --version

echo "[2/5] Cursor integration (project-scoped)..."
graphify cursor install --project

echo "[3/5] Claude Code integration (project-scoped)..."
graphify install --project

echo "[4/5] Building knowledge graph (AST-only)..."
graphify update .

echo "[5/5] Git hooks (post-commit AST rebuild)..."
if [ -d .git ]; then
  graphify hook install
else
  echo "[skip] not a git repo — hooks not installed"
fi

echo ""
echo "[OK] graphify ready."
echo "  graphify query \"delivery path confluence deliver\""
echo "  make graphify-update"
echo "  Docs: docs/GRAPHIFY_SETUP.md"
