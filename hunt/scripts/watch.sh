#!/usr/bin/env bash
# Canonical hunt watch launcher — avoids venv sys.prefix RuntimeWarning from hunt/../.venv.
set -euo pipefail
HUNT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$HUNT_DIR/.." && pwd)"
cd "$HUNT_DIR"
mkdir -p data
export HUNT_WATCHDOG_S="${HUNT_WATCHDOG_S:-900}"
if pgrep -f "[h]unt_core watch" >/dev/null 2>&1; then
  echo "hunt watch already running" >&2
  exit 1
fi

PY=(
  "$REPO_ROOT/.venv/bin/python"
  -W "ignore:Unexpected value in sys.prefix:RuntimeWarning"
  -W "ignore:Unexpected value in sys.exec_prefix:RuntimeWarning"
  -m hunt_core watch
  "$@"
)

if [[ "${HUNT_WATCH_SUPERVISE:-1}" == "1" ]]; then
  restart_s="${HUNT_WATCH_RESTART_S:-15}"
  sup_log="$HUNT_DIR/data/watch_supervisor.log"
  while true; do
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) hunt_watch_start pid=$$" >>"$sup_log"
    if "${PY[@]}"; then
      ec=0
    else
      ec=$?
    fi
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) hunt_watch_exit code=$ec restart_in=${restart_s}s" >>"$sup_log"
    sleep "$restart_s"
  done
else
  exec "${PY[@]}"
fi
