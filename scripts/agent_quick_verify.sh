#!/usr/bin/env bash
# Quick verify for agent stop hook — offline only, ~30s max.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi
python -m compileall -q bot 2>&1 | tail -5
python scripts/verify_refactor_gate.py 2>&1 | tail -10
pytest tests/test_wave_i_calibration.py -q --tb=no 2>&1 | tail -5
echo "agent_quick_verify: OK"
