# Session handoff (solo operator)

End-of-session summary for the **next agent session**. Execute any quick checks yourself first.

## Agent does now

1. `git status -sb` and `git diff --stat` (if git repo)
2. Latest `data/live_watch/` run_id if any live work this session
3. Note uncommitted files (do not commit unless asked)

## Write handoff block

```markdown
## Done this session
- ...

## Verify status
- compileall: pass/fail
- wave pytest: pass/fail
- live: run / skipped (reason)

## Next session (priority)
1. ... (map to P0–P4 from PROJECT_ROADMAP)

## Blockers
- proxy / rate limit / ...

## Files touched
- ...
```

## Optional

If roadmap priorities shifted, propose a **minimal** edit to `docs/PROJECT_ROADMAP_AND_STATUS.md` (agent applies if user implied acceptance).

Human reads handoff only — no action items for human unless explicit blocker needing a secret/token.
