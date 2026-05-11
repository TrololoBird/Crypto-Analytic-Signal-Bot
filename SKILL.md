---
name: jules-swarm-orchestrator
description: >-
  Orchestrates parallel Jules agent swarms for codebases. Dispatches 8 specialized
  agents (Architect, Bolt, Navigator, Palette, Sentinel, Verifier, Janitor, Scribe),
  manages file ownership to prevent merge conflicts, and defines review/merge workflow.
  Use when user says "run jules swarm", "dispatch agents", "jules batch",
  "run maintenance agents", or "parallel code review".
license: MIT
metadata:
  author: Semester AI
  version: 1.0.0
  category: workflow-automation
  tags: [jules, google, multi-agent, swarm, code-quality]
---

# Jules Swarm Orchestrator

Dispatch and coordinate parallel Jules coding agents against a GitHub repository.

## How It Works

Eight specialized agents run simultaneously in isolated Jules sessions. Each agent owns a specific concern and set of files. After all sessions complete, results are reviewed and merged in priority order.

## Agent Roster

| Agent | Concern | Prompt |
|-------|---------|--------|
| **Architect 🧭** | Structure, routing, module boundaries | `assets/prompts/architect.md` |
| **Bolt ⚡** | Performance, lazy loading, memoization | `assets/prompts/bolt.md` |
| **Navigator 🧭** | UX flows, page logic, navigation | `assets/prompts/navigator.md` |
| **Palette 🎨** | Visual consistency, theming, a11y | `assets/prompts/palette.md` |
| **Sentinel 🛡️** | Error handling, validation, safety | `assets/prompts/sentinel.md` |
| **Verifier 🧪** | Test coverage, correctness | `assets/prompts/verifier.md` |
| **Janitor 🧹** | Dead code, lint, cleanup | `assets/prompts/janitor.md` |
| **Scribe 📝** | Docs, comments, changelog | `assets/prompts/scribe.md` |

## Step 1: Dispatch

Use the bundled dispatch script or run manually:

```bash
# All 8 agents
./scripts/dispatch.sh owner/repo

# Specific agents only
./scripts/dispatch.sh owner/repo sentinel verifier

# Manual single agent
jules new --repo owner/repo "$(cat assets/prompts/architect.md)"
```

Dispatch all agents in one batch so they work against the same base commit.

## Step 2: Monitor

```bash
jules remote list --session
```

## Step 3: Review and Merge

```bash
# List sessions
./scripts/review.sh

# Inspect a session's diff
./scripts/review.sh <session-id>

# Apply changes locally
./scripts/review.sh <session-id> apply
```

**Merge in this order** (higher priority first):
Architect > Sentinel > Navigator > Palette > Bolt > Verifier > Janitor > Scribe

If two PRs conflict, merge the higher-priority agent first, then rebase the other.

## File Ownership

Each agent may only edit files in its owned scope. See `references/file-ownership.md` for full matrices (Web, Android, Backend).

The pattern:
- **Config files** (`package.json`, build configs, CI): human-only
- **Entry/routing**: Architect
- **API/services**: Sentinel
- **Pages**: Navigator (Palette may adjust styling)
- **Components**: Palette (Navigator may adjust layout)
- **Tests**: Verifier
- **Docs**: Scribe
- **Dead code**: Janitor (except test/doc files)

Agents log out-of-scope issues as `// TODO: [AgentName] ...` comments.

## Agent Rules

1. Read `AGENTS.md` before starting — agent prompts supplement, never override
2. Only edit files in your owned scope
3. One concern per PR
4. Never add dependencies, change auth, or modify deploy config
5. Run the repo's build/test command before finishing
6. Log out-of-scope issues as TODOs

## Customizing

Drop agents that don't apply to your repo:

| Repo Type | Recommended |
|-----------|-------------|
| Frontend (React/Vue) | Architect, Navigator, Palette, Sentinel, Verifier, Janitor |
| Backend (API) | Architect, Sentinel, Bolt, Verifier, Janitor, Scribe |
| Mobile (Android/iOS) | All 8 |
| Library/SDK | Architect, Sentinel, Verifier, Scribe |

To customize prompts: copy `assets/prompts/<agent>.md` to your repo's `.prompts/jules/` directory and edit.
