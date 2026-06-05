# План развития проекта и настройка Cursor + Claude Code

> **Дата:** 2026-06-04  
> **v1 готов:** [DEFINITION_OF_DONE.md](DEFINITION_OF_DONE.md)  
> **Экономия токенов:** [AGENT_TOKEN_POLICY.md](AGENT_TOKEN_POLICY.md) — агенты не читают весь research pack  
> **Solo:** [SOLO_OPERATOR_PLAYBOOK.md](SOLO_OPERATOR_PLAYBOOK.md)  
> **GitHub token / CI / MCP:** [GITHUB_CURSOR_SETUP.md](GITHUB_CURSOR_SETUP.md)  
> **Roadmap:** [PROJECT_ROADMAP_AND_STATUS.md](PROJECT_ROADMAP_AND_STATUS.md)

---

## Token economy (обязательно для агентов)

| Действие | Вместо |
|----------|--------|
| Старт сессии | Hook + `DEFINITION_OF_DONE.md` |
| «50 улучшений» | Таблица backlog в DoD (7 строк) |
| Архитектура | `graphify query` |
| Код | grep / 1 файл |

Команда: `/prime-context` (2 файла max).

---

## Solo operator (главный режим)

Один человек не выполняет команды — только Cursor Agent и Claude Code.

| Слой | Назначение |
|------|------------|
| **Rules (3× always)** | `project-core`, `agent-sole-executor`, `solo-operator` |
| **Rules (on-demand)** | `architecture-v9`, `graphify`, `strategies`, `delivery`, `features` |
| **Commands** | `/plan-task` → `/implement-plan` → `/verify` → `/handoff` |
| **Subagents** | `orchestrator` маршрутизирует; `verifier` после кода |
| **Hooks** | secrets read block, shell guard, ruff, stop verify |
| **Claude skills** | `.claude/skills/` auto-load (verify, live, calibration) |

См. [SOLO_OPERATOR_PLAYBOOK.md](SOLO_OPERATOR_PLAYBOOK.md).

---

## 1. Где мы сейчас (кратко)

| Область | Статус |
|---------|--------|
| v9 layout (`market/`, `features/`, `runtime/`, `delivery/`, …) | ✅ Готово |
| Волны E1–F10 (gates, WS, dashboard, calibration, journal) | ✅ Готово |
| F11 (live_watch ↔ matrix/calibration) | ⏳ Частично |
| Phase 3 slim analyzer | ⏸ Отложено (`pipeline.py` ~1554 LOC) |
| F12 de-bloat (memory, ws, tracking) | 📋 Следующая структурная волна |
| Live 6h supervised | ✅ Сессии 2026-06-04, 124 delivered |

**Инвариант доставки (никогда не обходить):**

`validate_signal_contract` → `hard_confluence_gate` (3-of-5) → `delivery.deliver`

---

## 2. Дорожная карта развития (приоритеты)

### P0 — Качество сигналов в проде (1–2 недели агента)

1. **Weighted confluence** — включить `use_weighted_confluence` в `config.toml` после разбора telemetry confluence (F9-S8).
2. **Post-live calibration loop** на каждую 6h-сессию:
   - `python scripts/live_watch_rollup_report.py`
   - `python scripts/calibration_pipeline.py --run-id <RUN_ID>`
   - `python scripts/strategy_shortlist_matrix.py --run-id <RUN_ID> --live-shortlist` (при доступном REST)
3. **Сеть** — `[bot.network]` через `scripts/discover_binance_proxies.py` (RU/geo-block).

### P1 — F12 structural de-bloat

| Модуль | ~LOC | Действие |
|--------|------|----------|
| `persistence/repository/memory.py` | 2073 | Вынос query-групп в `persistence/queries/` |
| `runtime/analyzer/pipeline.py` | 1554 | Cycle dispatch + conflict merge |
| `market/ws.py` | 1937 | Продолжить split `ws_connection` |
| `persistence/tracking.py` | 1935 | Lifecycle + channel tests |

**Правило:** не плодить файлы без функционального выигрыша (см. REFACTOR_PLAN §7).

### P2 — Analyzer & parity

- Backtest vs live: Wilder ATR/RSI, без `shift(-N)` на live path.
- `order_block.py`: унификация с `is_clean_fvg` / `sweep_tolerance` из F10.

### P3 — Observability

- F11: парсинг `strategy_decisions` из `bot_stdout.log` при отсутствии JSONL.
- Dashboard `/api/health` в supervised runs.
- Prometheus funnel → Grafana (опционально).

### P4 — Ops calibration (волны)

- Zero-hit triage по стратегиям (skill `zero-hit-strategy-triage`).
- Nightly: `make nightly-calibration`, shortlist matrix live mode.
- Outcomes derank из SQLite.

---

## 3. Настройка окружения (macOS / darwin)

### 3.1 Python 3.14.5

```bash
cd /path/to/Crypto-Analytic-Signal-Bot
# uv (рекомендуется на Cloud/macOS без py 3.14)
export PATH="$HOME/.local/bin:$PATH"
uv python install 3.14
uv venv .venv --python 3.14
source .venv/bin/activate
uv pip install -e ".[live,dev,test]"
```

Альтернатива: `python3.14 -m venv .venv` + `pip install -e ".[live,dev,test]"`.

### 3.2 Конфиг (один раз на workspace)

```bash
cp config.toml.example config.toml
cp env.example .env   # токены Telegram — не коммитить
python scripts/validate_config.py --config config.toml
python scripts/project_health_audit.py --stale-days 2 --full
```

Smoke без Telegram: `provider = "none"` или `BOT_NOTIFIER_PROVIDER=none`.

### 3.3 graphify (опционально, сильно рекомендуется)

```bash
# установка CLI — см. graphify docs проекта
graphify update .
graphify query "delivery path confluence"
```

### 3.4 Binance / proxy

```bash
python scripts/discover_binance_proxies.py
python scripts/probe_binance_access.py --all-configured
```

См. [BINANCE_PROXY_RU.md](BINANCE_PROXY_RU.md).

### 3.5 Cursor IDE

| Артефакт | Путь | Назначение |
|----------|------|------------|
| Rules (always) | `.cursor/rules/*.mdc` | Guardrails v9, sole executor |
| Skills | `.cursor/skills/*/SKILL.md` | Live verify, triage, delivery audit |
| Commands | `.cursor/commands/*.md` | Slash `/verify`, `/live-smoke`, … |
| Hooks | `.cursor/hooks.json` + `.cursor/hooks/*` | Session, shell guard, post-edit hint |

**Проверка hooks:** Cursor → Settings → Hooks, или канал Hooks в Output.

### 3.6 Claude Code (CLI)

| Артефакт | Путь | Назначение |
|----------|------|------------|
| Project memory | `CLAUDE.md` | Краткий контекст + ссылки |
| Settings | `.claude/settings.json` | Hooks, permissions |
| Local overrides | `.claude/settings.local.json` | Личное (gitignored) |
| Subagents | `.claude/agents/*.md` | live-ops, de-bloat, strategy-cal |

Запуск: `claude` из корня репо. Команда `/hooks` — список активных hooks.

### 3.7 Роли: вы vs агент

- **Вы:** архитектура, приоритеты, acceptance.
- **Агент (Cursor/Claude):** все команды, config, proxy, терминалы, verification.

См. `.cursor/rules/agent-sole-executor.mdc`.

---

## 4. Аудит первой версии (2026-06-04) — что было упущено

| Пробел | Исправление |
|--------|-------------|
| Нет `.cursor/agents/` | Добавлены 5 subagents (Cursor Task tool); `.claude/agents/` оставлены для CLI |
| `afterFileEdit` с `additional_context` | Неверный формат — заменён на `ruff format` + hints через `postToolUse` |
| Matcher `bot/` на `afterFileEdit` | По [Cursor docs](https://cursor.com/docs/hooks) matcher там — tool type, не путь |
| Нет `beforeReadFile` | Блок чтения `.env`, `config.toml`, `data/` (паттерн [cursor-hooks](https://github.com/hamzafer/cursor-hooks)) |
| `guard_shell.py` без Claude stdin | Парсинг `tool_name` + `tool_input.command` |
| Claude Write guard | `protect-files.sh` + `permissions.deny` |
| Дублирование `docs/CURSOR_SETUP.md` | Обновлён и связан с этим документом |
| `.vscode/` в `.gitignore` | Снят ignore; добавлены `settings.json` + `extensions.json` |
| Cloud agents без `sessionStart` | В commands: использовать `/prime-context` |
| Нет `delivery-guardian` readonly | Subagent только для аудита delivery |
| Нет `.claude/rules/` | `delivery-invariant.md` для Claude Code |

## 5. Матрица артефактов AI-окружения

### Cursor Commands (`/` в чате)

| Команда | Файл | Когда |
|---------|------|-------|
| `/prime` | `prime-context.md` | Старт сессии — загрузить контекст v9 |
| `/verify` | `verify.md` | После изменений кода |
| `/live-smoke` | `live-smoke.md` | Короткий live smoke |
| `/supervised-6h` | `supervised-6h.md` | Длинная supervised сессия |
| `/calibrate-run` | `calibrate-run.md` | Калибровка по run_id |
| `/zero-hit` | `zero-hit.md` | Триаж стратегии без хитов |
| `/delivery-audit` | `delivery-audit.md` | Аудит delivery path |
| `/graphify` | `graphify.md` | Запрос к knowledge graph |
| `/de-bloat` | `de-bloat.md` | F12 split одного модуля |

### Cursor Skills (автовыбор по description)

| Skill | Триггер |
|-------|---------|
| `live-binance-verify` | REST/WS/pipeline после изменений market/features |
| `validate-delivery-path` | Изменения `bot/delivery/` |
| `zero-hit-strategy-triage` | Стратегия без сигналов |
| `refactor-module` | Удаление/слияние модулей |
| `supervised-live-session` | 6h live_watch |
| `calibration-wave` | calibration_pipeline + matrix |
| `graphify-navigate` | Архитектурные вопросы |

### Claude Code agents (`/agent-name`)

| Agent | Фокус |
|-------|-------|
| `live-ops` | Supervised sessions, rollup, proxy |
| `de-bloat` | F12 splits с health audit |
| `strategy-calibration` | Thresholds, zero-hit, matrix |

### Hooks (детерминированные)

| Event | Скрипт | Эффект |
|-------|--------|--------|
| `sessionStart` | `session-context.sh` | Напоминание v9 + sole executor |
| `beforeShellExecution` | `guard-shell.sh` | Блок опасных команд |
| `afterFileEdit` | `post-python-edit.sh` | Hint: compileall/graphify |

---

## 6. Verification checklist (агент после каждой волны)

```bash
source .venv/bin/activate
python scripts/clean_session_data.py --mode smoke --config config.toml
python -m compileall -q bot
python scripts/validate_config.py --config config.toml
python scripts/verify_refactor_gate.py
pytest tests/test_wave_f9_agent_*.py tests/test_wave_f10_agent_*.py tests/test_wave_i_calibration.py -q
# При доступном Binance REST:
PYTEST_LIVE=1 pytest tests/live/ -v
python scripts/live_check_pipeline.py --symbols BTCUSDT --limit 1
make graphify-update
```

---

## 7. Что не коммитить

`config.toml`, `.env`, `data/`, `logs/`, `*.db`, `telemetry/`, `.claude/settings.local.json`

---

## 8. Следующие шаги (рекомендуемый порядок сессий)

1. **Сессия A:** P0 — weighted confluence + calibration на `20260604T014627Z`.
2. **Сессия B:** F11 finish — stdout parser для strategy_decisions.
3. **Сессия C:** F12 — `memory.py` query split (subagent `de-bloat`).
4. **Сессия D:** P2 — order_block + `is_clean_fvg` dedup.
5. **Регулярно:** `make nightly-calibration` + health audit.

---

## Ссылки

- [AGENTS.md](../AGENTS.md) — entry для Cursor Cloud
- [AGENT_QUICK_START.md](../AGENT_QUICK_START.md) — hot files
- [ARCHITECTURE_CANONICAL.md](research/ARCHITECTURE_CANONICAL.md)
- [Cursor Commands docs](https://cursor.com/docs/agent/chat/commands)
- [Claude Code Hooks](https://code.claude.com/docs/en/hooks-guide)
