#!/usr/bin/env bash
# Bot supervisor: stop → clean smoke session → run → verify (report-5 ops)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${ROOT}/.venv/bin/python"
PID_FILE="${ROOT}/data/bot.pid"
SUP_LOG="${ROOT}/data/logs/bot_supervisor.log"
BOT_LOG="${ROOT}/data/logs/bot_run.log"
STARTUP_WAIT_SECONDS="${BOT_SUPERVISOR_STARTUP_WAIT:-8}"

mkdir -p "$(dirname "$SUP_LOG")" "$(dirname "$BOT_LOG")"

log() {
  echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] $*" | tee -a "$SUP_LOG"
}

if [[ ! -x "$PYTHON" ]]; then
  log "ERROR: missing venv python at $PYTHON"
  exit 1
fi

log "Stopping any running bot via CLI"
"$PYTHON" main.py stop >>"$SUP_LOG" 2>&1 || true
sleep 1

if [[ -f "$PID_FILE" ]]; then
  OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "${OLD_PID:-}" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
    log "Force stopping stale pid=$OLD_PID"
    kill "$OLD_PID" 2>/dev/null || true
    sleep 2
    kill -9 "$OLD_PID" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
fi

log "Cleaning smoke session data"
"$PYTHON" scripts/clean_session_data.py --mode smoke --config config.toml >>"$SUP_LOG" 2>&1

log "Starting bot"
nohup "$PYTHON" main.py run >>"$BOT_LOG" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"
log "Bot started pid=$NEW_PID log=$BOT_LOG"

sleep "$STARTUP_WAIT_SECONDS"
if kill -0 "$NEW_PID" 2>/dev/null; then
  log "Bot healthy after ${STARTUP_WAIT_SECONDS}s"
  exit 0
fi

log "ERROR: bot exited during startup — tail $BOT_LOG"
tail -n 40 "$BOT_LOG" >>"$SUP_LOG" 2>&1 || true
rm -f "$PID_FILE"
exit 1
