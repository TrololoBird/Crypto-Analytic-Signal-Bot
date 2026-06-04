#!/usr/bin/env bash
# Detached supervised live session on macOS.
# - Process survives display sleep / screen lock (caffeinate -i ties to supervisor PID).
# - Does NOT force the display to stay on (no -d flag).
#
# Usage:
#   ./scripts/live_supervised_session_mac.sh
#   ./scripts/live_supervised_session_mac.sh --hours 2 --minutes 120
#   ./scripts/live_supervised_session_mac.sh --status
#   ./scripts/live_supervised_session_mac.sh --stop

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

HOURS="${HOURS:-2}"
MINUTES="${MINUTES:-120}"
SNAPSHOT_INTERVAL="${SNAPSHOT_INTERVAL:-60}"
CONFIG="${CONFIG:-config.toml}"
PID_FILE="${ROOT}/logs/live_supervised_mac.pid"
LOG_FILE="${ROOT}/logs/live_supervised_mac_$(date -u +%Y%m%dT%H%M%SZ).log"

usage() {
  cat <<EOF
Detached macOS live supervisor (signal-only bot).

  ./scripts/live_supervised_session_mac.sh [--hours N] [--minutes M] [--config path]
  ./scripts/live_supervised_session_mac.sh --status
  ./scripts/live_supervised_session_mac.sh --stop

Env overrides: HOURS, MINUTES, SNAPSHOT_INTERVAL, CONFIG
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hours) HOURS="$2"; shift 2 ;;
    --minutes) MINUTES="$2"; shift 2 ;;
    --config) CONFIG="$2"; shift 2 ;;
    --status)
      if [[ -f "$PID_FILE" ]]; then
        pid="$(cat "$PID_FILE")"
        if kill -0 "$pid" 2>/dev/null; then
          echo "running pid=$pid log=$(ls -t "${ROOT}"/logs/live_supervised_mac_*.log 2>/dev/null | head -1)"
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
  echo "missing .venv — create with: py -3.14 -m venv .venv && pip install -e '.[live,dev,test]'"
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
# shellcheck source=/dev/null
source "${ROOT}/.venv/bin/activate"

python scripts/clean_session_data.py --mode smoke --config "$CONFIG"
python scripts/validate_config.py --config "$CONFIG"

CMD=(
  python -m scripts.live_supervised_session
  --hours "$HOURS"
  --minutes "$MINUTES"
  --snapshot-interval "$SNAPSHOT_INTERVAL"
  --config "$CONFIG"
  --takeover
)

sup_pid="$(
  python scripts/launch_detached.py \
    --log "$LOG_FILE" \
    --pid-file "$PID_FILE" \
    --cwd "$ROOT" \
    -- "${CMD[@]}"
)"
# Keep the Mac awake for the supervisor only; display may still lock/sleep.
caffeinate -i -w "$sup_pid" >/dev/null 2>&1 &

echo "started supervisor pid=$sup_pid (detached, caffeinate -w attached)"
echo "log=$LOG_FILE"
echo "live_watch sessions under data/live_watch/"
echo "status: $0 --status"
