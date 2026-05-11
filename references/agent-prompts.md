# Agent Prompt Structure

Every prompt in `assets/prompts/` follows this structure:

```
1. Identity     — Name, emoji, one-line role
2. Prime Dir    — "Read AGENTS.md first"
3. Concern      — What this agent cares about
4. Files Owned  — Glob patterns it may edit
5. Do Not Touch — Files reserved for others
6. What To Do   — Specific scan/fix instructions
7. Verification — Build/test command to run
```

## Customizing Prompts

Copy `assets/prompts/<agent>.md` to `.prompts/jules/<agent>.md` in your repo. Then edit:

- **Files You Own** — adjust paths to match your project layout
- **What To Do** — add repo-specific tasks or focus areas
- **Verification** — set the correct build/test command

The identity, prime directive, and "do not touch" sections rarely need changes.

## Adding a Custom Agent

1. Create `assets/prompts/<name>.md` following the structure above
2. Add it to the dispatch script or run manually
3. Update `references/file-ownership.md` with the new agent's column

## Agent Interactions

Agents communicate via TODO comments when they find issues outside their scope:

```
// TODO: [Sentinel] this API call has no error handling
// TODO: [Palette] hardcoded color should use theme token
// TODO: [Architect] this logic belongs in the domain layer
```

After a swarm run, search for these TODOs to find cross-cutting issues that need follow-up.
