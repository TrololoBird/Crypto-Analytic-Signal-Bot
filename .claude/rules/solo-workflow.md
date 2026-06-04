# Solo workflow (Claude Code)

Human = direction + acceptance. Claude = all execution.

1. Multi-file or unclear scope → plan first (read `docs/SOLO_OPERATOR_PLAYBOOK.md`)
2. Implement with minimal diff
3. Run skill `verify-after-change` or script `scripts/agent_quick_verify.sh`
4. Live work → `clean_session_data --mode smoke` first
5. Session end → summarize next P0 step from `docs/PROJECT_ROADMAP_AND_STATUS.md`

Subagents: `/orchestrator` for routing, `verifier` after de-bloat.

Do not ask the human to run terminal commands.
