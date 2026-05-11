# File Ownership Matrix

This matrix defines which agent may edit which files. It prevents merge conflicts when agents run in parallel.

## Rules

- **Owner**: May read and edit these files
- **Read-only**: May read for context but must not edit
- **Blocked**: Must not touch under any circumstances

## Generic Web Project (React/Vue/Svelte)

| File / Area | Architect | Bolt | Navigator | Palette | Sentinel | Verifier | Janitor | Scribe |
|-------------|-----------|------|-----------|---------|----------|----------|---------|--------|
| Entry point (`App.tsx`) | Owner | - | - | - | - | - | - | - |
| `*/stores/` | Owner | Read | Read | - | Read | Read | Clean | - |
| `*/pages/` | Read | Read | Owner | Style | Read | Read | Clean | - |
| `*/components/` | Read | Read | Layout | Owner | Read | Read | Clean | - |
| `*/services/` | Read | Read | - | - | Owner | Read | Clean | - |
| `*/tests/` | Read | Read | Read | Read | Read | Owner | - | - |
| `*/types/` | Owner | Read | Read | Read | Read | Read | Clean | - |
| Shared lib (`lib/`) | Read | Read | - | - | Owner | Read | Clean | - |
| Theme/styling config | - | - | - | Owner | - | - | - | - |
| Build config (non-deps) | - | Owner | - | - | - | - | - | - |
| `docs/` | Read | - | - | - | - | - | - | Owner |
| `README.md` | Read | - | - | - | - | - | - | Owner |
| `package.json` | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked |
| Lock files | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked |
| CI/CD config | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked |
| `.env` / secrets | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked |

**Legend**: Owner = may edit | Read = read-only | Layout/Style/Clean = limited edit (see notes) | `-` = no access needed | Blocked = never touch

### Limited Edit Notes

- **Style**: Palette may edit CSS classes/Tailwind in page files but not page logic
- **Layout**: Navigator may adjust component layout but not visual styling
- **Clean**: Janitor may remove dead code but not refactor or change behavior

## Android Project (Kotlin/Compose)

| File / Area | Architect | Bolt | Navigator | Palette | Sentinel | Verifier | Janitor | Scribe |
|-------------|-----------|------|-----------|---------|----------|----------|---------|--------|
| `AppNavigation.kt` | Owner | - | - | - | - | - | - | - |
| `*/ui/Routes.kt` | Owner | - | Read | - | - | - | - | - |
| `*/ui/*Screen.kt` | Read | Read | Owner | Style | Read | Read | Clean | - |
| `*/ui/components/` | Read | Read | Layout | Owner | Read | Read | Clean | - |
| `*/ui/*ViewModel.kt` | Read | Read | Read | - | Read | Read | Clean | - |
| `*/domain/` | Owner | Read | Read | - | Read | Read | Clean | - |
| `*/data/` | Read | Read | - | - | Owner | Read | Clean | - |
| `test/` | Read | Read | Read | Read | Read | Owner | - | - |
| `res/values/strings.xml` | - | - | - | Owner | - | - | - | - |
| `res/values/themes.xml` | - | - | - | Owner | - | - | - | - |
| `build.gradle.kts` | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked |
| `google-services.json` | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked |
| `docs/` | Read | - | - | - | - | - | - | Owner |

## Backend Project (Python/Node API)

| File / Area | Architect | Bolt | Sentinel | Verifier | Janitor | Scribe |
|-------------|-----------|------|----------|----------|---------|--------|
| `*/router.py` / routes | Owner | Read | Read | Read | Clean | - |
| `*/services.py` / logic | Read | Read | Owner | Read | Clean | - |
| `*/schemas.py` / models | Owner | Read | Read | Read | Clean | - |
| `*/tests/` | Read | Read | Read | Owner | - | - |
| Middleware / auth | - | - | Owner | Read | - | - |
| DB migrations | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked |
| `requirements.txt` | Blocked | Blocked | Blocked | Blocked | Blocked | Blocked |
| `docs/` | Read | - | - | - | - | Owner |

## Resolving Ownership Disputes

If a file doesn't clearly belong to one agent:
1. The agent whose concern is most relevant claims it
2. If still ambiguous, Architect owns structure, Sentinel owns safety
3. Add the file to this matrix for future runs
