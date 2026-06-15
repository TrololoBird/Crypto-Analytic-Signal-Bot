#!/usr/bin/env bash
# OPS-1: research harvest session (default 120 minutes).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON="${ROOT}/.venv/bin/python"
MINUTES="${HARVEST_MINUTES:-120}"
LOG="${ROOT}/data/logs/research_harvest.log"

mkdir -p "$(dirname "$LOG")"

{
  echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] harvest start minutes=$MINUTES"
  "$PYTHON" scripts/research_harvest_session.py --config config.toml --minutes "$MINUTES"
  echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] harvest complete"
} >>"$LOG" 2>&1
