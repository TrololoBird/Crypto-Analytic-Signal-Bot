# graphify — knowledge graph для Cursor и Claude Code

> **Пакет PyPI:** `graphifyy` (два «y»). CLI-команда: `graphify`.  
> **Официальный репозиторий:** [safishamsi/graphify](https://github.com/safishamsi/graphify)

graphify строит AST-граф кодовой базы в `graphify-out/` и позволяет отвечать на архитектурные вопросы через `graphify query` вместо broad grep или чтения монолитов (memory.py, ws.py, …).

---

## Быстрый старт (агент или оператор один раз)

```bash
cd /path/to/Crypto-Analytic-Signal-Bot
./scripts/setup_graphify.sh
```

Скрипт: `uv tool install graphifyy` → интеграция Cursor + Claude Code → `graphify update .` → git hooks.

---

## Ручная установка

### 1. CLI

```bash
# Рекомендуется — PATH настраивается автоматически
export PATH="$HOME/.local/bin:$PATH"
uv tool install graphifyy
graphify --version   # ожидается 0.8.x
```

Альтернативы: `pipx install graphifyy` или `pip install graphifyy` (на macOS может понадобиться `~/Library/Python/3.x/bin` в PATH).

> **Не путать** с другими пакетами `graphify*` на PyPI — они не связаны с этим проектом.

### 2. Интеграция Cursor (project-scoped)

```bash
graphify cursor install --project
```

Создаёт/обновляет:

| Файл | Назначение |
|------|------------|
| `.cursor/rules/graphify.mdc` | `alwaysApply: true` — query-first перед grep |
| Slash `/graphify` | `.cursor/commands/graphify.md` (уже в репо) |
| Skill | `.cursor/skills/graphify-navigate/SKILL.md` |

### 3. Интеграция Claude Code (project-scoped)

```bash
graphify install --project
```

Создаёт/обновляет:

| Файл | Назначение |
|------|------------|
| `.claude/skills/graphify/SKILL.md` | Skill `/graphify` |
| `.claude/skills/graphify/references/` | query, update, hooks, exports |
| `.claude/CLAUDE.md` | Указатель на skill |
| `.claude/settings.json` | PreToolUse hooks (Bash grep + Read/Glob → подсказка graphify) |

### 4. Построить / обновить граф

```bash
graphify update .          # AST-only, без LLM, ~20–60 с на этом репо
make graphify-update       # то же, если CLI в PATH
```

Выход:

```
graphify-out/
├── graph.json          # полный граф (~11k nodes) — query/path/explain
├── GRAPH_REPORT.md     # обзор архитектуры
├── manifest.json       # индекс файлов
└── cache/              # ast/ в .gitignore; stat-index коммитится
```

`graph.html` не генерируется при >5000 nodes — используйте CLI query.

### 5. Git hooks (auto-rebuild после commit)

```bash
graphify hook install
```

Устанавливает `.git/hooks/post-commit` и `post-checkout` — пересборка AST-графа без LLM. После `uv tool upgrade graphifyy` перезапустите `graphify hook install`.

---

## Использование в сессии

### Команды CLI

```bash
graphify query "delivery path validate_signal_contract confluence"
graphify path "SymbolAnalyzer" "deliver"
graphify explain "DeliveryOrchestrator"
graphify update .                    # после правок bot/
graphify export callflow-html        # Mermaid call-flow (опционально)
```

### Cursor

- Rule **graphify** включён always-on.
- Slash: `/graphify` или `/graphify .` для полной пересборки.
- Агент: `graphify query "…"` **до** `grep` по `bot/`.

### Claude Code

- Slash: `/graphify .` или skill `graphify`.
- Hooks в `.claude/settings.json` подсказывают graph при grep/Read исходников.

### Политика токенов

См. [AGENT_TOKEN_POLICY.md](AGENT_TOKEN_POLICY.md):

1. `graphify query "<вопрос>"` — scoped subgraph
2. `graphify-out/wiki/index.md` — если есть
3. `GRAPH_REPORT.md` — только для broad review
4. Raw grep / чтение memory.py целиком — последний resort

---

## Makefile

| Target | Действие |
|--------|----------|
| `make graphify-update` | `graphify update .` (skip если CLI нет) |
| `make graphify-install` | `./scripts/setup_graphify.sh` |

---

## Что коммитить

| Коммитить | Не коммитить |
|-----------|--------------|
| `.claude/skills/graphify/` | `graphify-out/cache/ast/` |
| `.cursor/rules/graphify.mdc` | `.claude/settings.local.json` |
| `graphify-out/graph.json`, `GRAPH_REPORT.md`, `manifest.json` | |
| `.claude/settings.json` (graphify hooks) | |

`.cursorignore` исключает `graphify-out/` из AI index Cursor — агенты используют CLI, не читают 15 MB JSON.

---

## Troubleshooting

| Проблема | Решение |
|----------|---------|
| `graphify: command not found` | `uv tool install graphifyy`; `export PATH="$HOME/.local/bin:$PATH"` |
| `ModuleNotFoundError: graphify` | Не использовать plain `pip` в `.venv` — только `uv tool` / `pipx` |
| Граф устарел | `graphify update .` или post-commit hook |
| CI не нужен graphify | Локальный dev tool; CI не вызывает graphify |
| Semantic nodes (docs/PDF) | `/graphify --update` в assistant + `GEMINI_API_KEY` (опционально) |

---

## Ссылки

- [CURSOR_CLAUDE_DEV_SETUP.md](CURSOR_CLAUDE_DEV_SETUP.md) §3.3
- [CURSOR_SETUP.md](CURSOR_SETUP.md)
- [AGENT_TOKEN_POLICY.md](AGENT_TOKEN_POLICY.md)
- [graphify docs](https://github.com/safishamsi/graphify)
