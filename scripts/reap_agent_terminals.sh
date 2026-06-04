#!/usr/bin/env bash
# Reap stale agent terminal sessions (macOS/Linux). Append exit_code footers where missing.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TERMS="${CURSOR_TERMINALS_DIR:-$HOME/.cursor/projects/*/terminals}"
shopt -s nullglob 2>/dev/null || true

for dir in $HOME/.cursor/projects/*/terminals; do
  [ -d "$dir" ] || continue
  for f in "$dir"/*.txt; do
    [ -f "$f" ] || continue
    if ! grep -q '^exit_code:' "$f" 2>/dev/null; then
      pid="$(grep '^pid:' "$f" | head -1 | awk '{print $2}')"
      if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
      fi
      echo "exit_code: 143" >> "$f"
      echo "reaped_by: scripts/reap_agent_terminals.sh" >> "$f"
    fi
  done
done

echo "reap_agent_terminals: done"
