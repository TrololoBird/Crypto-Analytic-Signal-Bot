#!/usr/bin/env bash
# Independent hunt audit every 90s — resilient to empty ticks and exit code 1.
set +e
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LOG="$ROOT/hunt/data/independent_verify.log"
PY="$ROOT/.venv/bin/python"
cd "$ROOT" || exit 1

while true; do
  echo "=== verify $(date -u +%H:%M:%S) ===" >>"$LOG"
  TMP=$(mktemp)
  "$PY" hunt/scripts/independent_verify.py --min-fuel 50 >"$TMP" 2>>"$LOG"
  RC=$?
  if [[ ! -s "$TMP" ]]; then
    echo "empty_output rc=$RC" >>"$LOG"
  else
    export TMP RC
    "$PY" - >>"$LOG" 2>&1 <<'PY'
import json, os
from pathlib import Path
raw = Path(os.environ["TMP"]).read_text(encoding="utf-8")
start, end = raw.find("{"), raw.rfind("}")
if start < 0 or end <= start:
    print(f"rc={os.environ['RC']} parse_error=no_json_blob")
    raise SystemExit(0)
d = json.loads(raw[start : end + 1])
bad = [r for r in d.get("reports", []) if not r.get("ok")]
ok = [r for r in d.get("reports", []) if r.get("ok")]
print(f"rc={os.environ['RC']} ok={len(ok)} bad={len(bad)} note={d.get('note', '')}")
for r in bad:
    print("BAD", r.get("symbol"), r.get("issues"))
for r in ok[:8]:
    print("OK", r.get("symbol"), r.get("hunt_phase"))
PY
  fi
  rm -f "$TMP"
  sleep 90
done
