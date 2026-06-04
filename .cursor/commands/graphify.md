# Graphify architecture query

Answer a codebase question using the knowledge graph first (before broad grep).

## Steps

1. If missing: `graphify update .` (from repo root, `.venv` active)
2. Run: `graphify query "<user question>"`  
   Or: `graphify path "<A>" "<B>"` / `graphify explain "<concept>"`
3. Use `graphify-out/wiki/index.md` for navigation if present
4. Only read `graphify-out/GRAPH_REPORT.md` if query/path insufficient

## After code changes in session

`make graphify-update`

Skill: `graphify-navigate`

Return: subgraph summary + recommended files to edit.
