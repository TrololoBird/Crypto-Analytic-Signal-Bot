#!/usr/bin/env bash
# beforeShellExecution — block obviously dangerous shell patterns.
set -euo pipefail
exec python3 "$(dirname "$0")/guard_shell.py"
