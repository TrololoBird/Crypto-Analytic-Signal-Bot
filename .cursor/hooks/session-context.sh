#!/usr/bin/env bash
# sessionStart — minimal v9 context (token-efficient). Full policy: docs/AGENT_TOKEN_POLICY.md
set -euo pipefail
cat <<'EOF'
{
  "additional_context": "v9 signal-only bot | v1 DONE per docs/DEFINITION_OF_DONE.md — do NOT generate new 50-item lists; only backlog IDs V1.1-* OPS-* OPT-*. Token: read AGENT_QUICK_START + DEFINITION_OF_DONE only; graphify query before grep. Delivery: contract→confluence→deliver. Python 3.14 .venv. Agent runs all commands. Harvest: main.py harvest. Calibration: BOT_ALLOW_CALIBRATION=1. LLM: hot path forbidden — docs/research/LLM_API_INTEGRATION.md."
}
EOF
