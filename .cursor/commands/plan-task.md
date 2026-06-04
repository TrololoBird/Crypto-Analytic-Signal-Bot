# Plan task (no code changes)

**Plan mode behavior.** Explore and produce an implementation plan only. Do not edit files.

## Input

Use the user's message as the task. If vague, ask **one** clarifying question, then plan.

## Explore

1. Read `docs/PROJECT_ROADMAP_AND_STATUS.md` for priority alignment (P0–P4)
2. `graphify query` if graph exists
3. Read only files needed for the task (not whole modules)

## Plan output

```markdown
## Goal
## Scope (files)
## Risks (delivery, live, network)
## Steps (numbered)
## Verification (exact commands agent will run)
## Out of scope
```

## Rules

- Delivery path must stay: contract → confluence → deliver
- No "user should run X" — write commands the **agent** will run in implement phase
- Defer F12 splits unless task explicitly asks de-bloat

Human approves plan → then `/implement-plan` or "implement".
