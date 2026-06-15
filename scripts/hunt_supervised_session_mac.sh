#!/usr/bin/env bash
# Detached 6h Hunt Watch (pump/dump) supervisor on macOS.
#
# Usage:
#   ./scripts/hunt_supervised_session_mac.sh
#   ./scripts/hunt_supervised_session_mac.sh --hours 6
#   ./scripts/hunt_supervised_session_mac.sh --status
#   ./scripts/hunt_supervised_session_mac.sh --stop

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

HOURS="${HOURS:-6}"
WATCH_INTERVAL="${WATCH_INTERVAL:-60}"
VERIFY_INTERVAL="${VERIFY_INTERVAL:-900}"
PID_FILE="${ROOT}/logs/hunt_supervised_mac.pid"
LOG_FILE="${ROOT}/logs/hunt_supervised_mac_$(date -u +%Y%m%dT%H%M%SZ).log"

usage() {
  cat <<EOF
Detached macOS Hunt Watch supervisor (pump/dump module).

  ./scripts/hunt_supervised_session_mac.sh [--hours N]
  ./scripts/hunt_supervised_session_mac.sh --status
  ./scripts/hunt_supervised_session_mac.sh --stop

Env: HOURS, WATCH_INTERVAL, VERIFY_INTERVAL
Data: hunt/data/sessions/<run_id>/
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hours) HOURS="$2"; shift 2 ;;
    --status)
      if [[ -f "$PID_FILE" ]]; then
        pid="$(cat "$PID_FILE")"
        if kill -0 "$pid" 2>/dev/null; then
          echo "running pid=$pid log=$(ls -t "${ROOT}"/logs/hunt_supervised_mac_*.log 2>/dev/null | head -1)"
          exit 0
        fi
        echo "stale pid file: $PID_FILE"
        exit 1
      fi
      echo "not running"
      exit 1
      ;;
    --stop)
      if [[ -f "$PID_FILE" ]]; then
        pid="$(cat "$PID_FILE")"
        kill "$pid" 2>/dev/null || true
        rm -f "$PID_FILE"
        echo "stopped supervisor pid=$pid"
      else
        echo "no supervisor pid file"
      fi
      exit 0
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1"; usage; exit 1 ;;
  esac
done

if [[ ! -x "${ROOT}/.venv/bin/python" ]]; then
  echo "missing .venv"
  exit 1
fi

if [[ -f "$PID_FILE" ]]; then
  old_pid="$(cat "$PID_FILE")"
  if kill -0 "$old_pid" 2>/dev/null; then
    echo "supervisor already running pid=$old_pid (use --stop first)"
    exit 1
  fi
  rm -f "$PID_FILE"
fi

mkdir -p "${ROOT}/logs"

CMD=(
  .venv/bin/python hunt/scripts/supervised_session.py
  --hours "$HOURS"
  --watch-interval "$WATCH_INTERVAL"
  --verify-interval "$VERIFY_INTERVAL"
)

sup_pid="$(
  .venv/bin/python scripts/launch_detached.py \
    --log "$LOG_FILE" \
    --pid-file "$PID_FILE" \
    --cwd "$ROOT" \
    -- "${CMD[@]}"
)"
caffeinate -i -w "$sup_pid" >/dev/null 2>&1 &

echo "started hunt supervised pid=$sup_pid (detached, ${HOURS}h)"
echo "log=$LOG_FILE"
echo "sessions: hunt/data/sessions/"
echo "status: $0 --status"
