#!/usr/bin/env bash
# Smoke test workflow — export BEFORE reset, run bot, export AFTER.
set -euo pipefail

RUNTIME="${1:-480}"
NOTES="${2:-}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="${ROOT}/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY=python3
fi

echo "=== SMOKE TEST WORKFLOW ==="

echo ""
echo "[1/5] Exporting outcomes to forensic archive (pre-reset)..."
"$PY" scripts/sl_forensic/export_to_archive.py --run-notes "${NOTES:-pre-smoke export}" || {
  echo "EXPORT FAILED — aborting to preserve data"
  exit 1
}

echo ""
echo "[2/5] Analyzing forensic archive..."
"$PY" scripts/sl_forensic/analyze_archive.py

echo ""
echo "[3/5] Review TIER 1 findings above."
echo "      Apply any df[-2] fixes if needed, then press Enter to continue..."
read -r _

echo ""
echo "[4/5] Starting bot for ${RUNTIME}s..."
ts="$(date +%Y%m%d_%H%M%S)"
logfile="logs/live_run_${ts}.log"
mkdir -p logs

"$PY" scripts/clean_session_data.py --mode smoke --config config.toml 2>/dev/null || true

if command -v timeout >/dev/null 2>&1; then
  timeout "$RUNTIME" "$PY" main.py run 2>&1 | tee "$logfile" || true
else
  "$PY" main.py run 2>&1 | tee "$logfile" &
  bot_pid=$!
  sleep "$RUNTIME"
  kill "$bot_pid" 2>/dev/null || true
  wait "$bot_pid" 2>/dev/null || true
fi

echo ""
echo "[5/5] Exporting new outcomes to forensic archive..."
"$PY" scripts/sl_forensic/export_to_archive.py --run-notes "post-run ${ts}"

echo ""
echo "=== WORKFLOW COMPLETE ==="
echo "Log: $logfile"
echo "Archive report: REPORT_FORENSIC_ARCHIVE.md"
