# No-Bloat Rules

1. Edit existing files, don't create new ones, unless the module doesn't exist yet
2. No ABCs/Protocols/factories unless ≥2 real implementations exist today
3. No module splitting under 500 lines of actual code
4. No test file generation unless explicitly requested
5. No re-export `__init__.py` that adds nothing
6. When unsure: ask the architect, don't invent architecture

Source: MASTER_REFACTOR Phase 4E. Project freeze: `docs/DEFINITION_OF_DONE.md`.
