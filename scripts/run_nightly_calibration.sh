#!/usr/bin/env bash
# OPS-3: nightly calibration when REST is healthy.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON="${ROOT}/.venv/bin/python"
LOG="${ROOT}/data/logs/nightly_calibration.log"

mkdir -p "$(dirname "$LOG")"

{
  echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] nightly calibration start"
  export BOT_ALLOW_CALIBRATION=1
  "$PYTHON" scripts/nightly_strategy_calibration.py --config config.toml
  echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] nightly calibration done"
} >>"$LOG" 2>&1
