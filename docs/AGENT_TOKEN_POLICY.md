# Agent token policy (Cursor + Claude Code)

Цель: **не переизучать** репозиторий каждую сессию.

## Session start (обязательно)

1. Hook уже внедрил контекст — **не** дублировать чтение всех rules.
2. Если нужен контекст: **только** `CLAUDE.md` + `docs/DEFINITION_OF_DONE.md` (~2 файла).
3. Код: `graphify query "<вопрос>"` если есть `graphify-out/graph.json`; иначе grep по `bot/`, не весь репо. Setup: [GRAPHIFY_SETUP.md](GRAPHIFY_SETUP.md).

## Не читать без явного запроса

| Путь | Почему |
|------|--------|
| `graphify-out/GRAPH_REPORT.md` целиком | Используй `graphify query` |
| `graphify-out/GRAPH_REPORT.md` | Используй `graphify query` |
| `bot/persistence/repository/memory.py` целиком | >1500 LOC — graphify/grep |
| `data/`, `telemetry/`, `.env`, `config.toml` | Секреты/шум; hooks блокируют read |
| Старые agent transcripts | Устаревший контекст |

## Читать по теме (максимум 1–2 файла)

| Тема | Файл |
|------|------|
| Архитектура | `graphify query "<вопрос>"` → при необходимости `ARCHITECTURE.md` |
| Стратегия | `bot/domain/strategy_catalog.py` + один `bot/strategies/<id>.py` |
| Delivery | `bot/delivery/contract.py` + `bot/delivery/confluence.py` |
| Live ops | `docs/SOLO_OPERATOR_PLAYBOOK.md` |
| Статус / backlog | `docs/DEFINITION_OF_DONE.md` |

## Правила работы

- **Минимальный diff** — не рефакторить «заодно».
- **Verify** — `make check` + wave pytest; live только если меняли market/features.
- **Не** генерировать списки из 50 пунктов — обновлять таблицу backlog в `DEFINITION_OF_DONE.md`.
- **Subagent** для 6h live / de-bloat / verify — не тянуть весь лог в родительский чат.
- **Handoff** — `/handoff` → 5 строк: сделано, backlog ID, следующий ID, verify result.

## Commands (коротко)

| Cmd | Когда |
|-----|-------|
| `/prime-context` | Старт сессии (2 файла + graphify) |
| `/verify` | После правок `bot/` |
| `/handoff` | Конец сессии |

Полный список: `.claude/rules/` — не перечислять в ответе пользователю.
