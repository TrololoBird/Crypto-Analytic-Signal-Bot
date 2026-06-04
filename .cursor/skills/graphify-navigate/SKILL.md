---
name: graphify-navigate
description: Navigates the graphify knowledge graph for architecture questions before broad grep. Use when graphify-out/graph.json exists, for cross-module flows, or when the user types /graphify.
---

# Graphify Navigate

## Order

1. `graphify query "<question>"`
2. `graphify path "<from>" "<to>"` for call chains
3. `graphify explain "<concept>"` for focused subgraphs
4. `graphify-out/wiki/index.md` for broad nav
5. `graphify-out/GRAPH_REPORT.md` only if 1–4 insufficient

## After code edits

```bash
make graphify-update
```

Requires graphify CLI on PATH; skip gracefully if not installed.

## Dirty graph

Dirty `graphify-out/` after hooks is expected — still run query; `graphify update .` refreshes AST.
