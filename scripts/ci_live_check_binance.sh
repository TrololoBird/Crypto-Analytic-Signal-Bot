#!/usr/bin/env bash
# CI wrapper: live Binance public REST/WS smoke (requires reachable egress).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  if [[ -x "${ROOT}/.venv/bin/python" ]]; then
    PYTHON="${ROOT}/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON="python3"
  else
    PYTHON="python"
  fi
fi

export BOT_DISABLE_HTTP_SERVERS="${BOT_DISABLE_HTTP_SERVERS:-1}"

exec "$PYTHON" scripts/live_check_binance_api.py "$@"
