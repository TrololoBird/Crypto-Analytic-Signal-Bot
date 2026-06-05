# GitHub + Cursor — токен, CI, MCP, расширения

> Solo-operator: агент выполняет `gh`/push; human только создаёт токен один раз.

## 1. Куда положить GitHub token

| Место | Назначение | Коммитить? |
|-------|------------|------------|
| **`.env`** → `GITHUB_TOKEN=...` | Agent: `scripts/gh_with_env_token.sh`, `scripts/verify_github_token.sh`, git push | **Нет** (в `.gitignore`) |
| **`~/.zshrc`** → `export GITHUB_TOKEN=...` | Cursor MCP (GitHub server), терминал вне проекта | Нет |
| **GitHub → Settings → Secrets** | Только CI secrets (Telegram, proxy) — не PAT для локальной разработки | N/A |

Скопируй из примера:

```bash
cp env.example .env
# Добавь строку GITHUB_TOKEN=... (см. §2)
```

Проверка (агент или ты после сохранения `.env`):

```bash
./scripts/verify_github_token.sh
```

## 2. Создание Fine-grained PAT (рекомендуется)

1. [GitHub → Settings → Developer settings → Fine-grained tokens](https://github.com/settings/personal-access-tokens/new)
2. **Repository access:** Only `TrololoBird/Crypto-Analytic-Signal-Bot`
3. **Expiration:** 90 days (поставь напоминание в календарь)
4. **Permissions:**

| Permission | Access | Зачем |
|------------|--------|-------|
| Contents | Read and write | push, PR |
| Pull requests | Read and write | `gh pr`, auto-fix |
| Actions | Read and write | push `.github/workflows/` (**обязательно**) |
| Metadata | Read | базовый API |
| Dependabot alerts | Read and write | dismiss/тriage alerts |
| Workflows | Read and write | fine-grained аналог `workflow` scope |

5. Сгенерируй token → вставь в `.env`:

```env
GITHUB_TOKEN=github_pat_xxxxxxxx
```

### Classic PAT (fallback)

[Classic tokens](https://github.com/settings/tokens/new) — scopes: `repo`, **`workflow`**, `read:org` (optional).

Fine-grained предпочтительнее: один репозиторий, минимальные права.

## 3. Как агент использует token

```bash
# Любая gh-команда с токеном из .env
./scripts/gh_with_env_token.sh pr list
./scripts/gh_with_env_token.sh run list --workflow=CI

# Push (если gh auth setup-git не настроен)
./scripts/github_push.sh main
```

Скрипты **не печатают** token. При отсутствии `GITHUB_TOKEN` — явная ошибка с ссылкой на этот doc.

## 4. Cursor MCP — GitHub (бесплатно, optional)

Официальный server: [github/github-mcp-server](https://github.com/github/github-mcp-server) (Docker).

1. Token в `~/.zshrc` или `.env` + Cursor читает env при старте:

```bash
export GITHUB_TOKEN="github_pat_..."
```

2. Скопируй пример конфига:

```bash
cp .cursor/mcp.json.example ~/.cursor/mcp.json
# или в проект: cp .cursor/mcp.json.example .cursor/mcp.json
```

3. **Cursor → Settings → MCP** — убедись что server `github` зелёный после Restart.

4. Toolsets по умолчанию ограничены (`issues,pull_requests,repos`) — без Actions deploy.

**Не коммить** `~/.cursor/mcp.json` с raw token. В репо только `mcp.json.example` с `${env:GITHUB_TOKEN}`.

## 5. Расширения Cursor / VS Code (бесплатные)

Рекомендации в `.vscode/extensions.json`:

| Extension ID | Цена | Auth | Назначение |
|--------------|------|------|------------|
| `ms-python.python` | Free | Нет | Python 3.14 |
| `charliermarsh.ruff` | Free | Нет | Lint/format |
| `github.vscode-github-actions` | Free | Нет* | Подсветка + validate workflows |
| `tamasfe.even-better-toml` | Free | Нет | config.toml |
| `usernamehw.errorlens` | Free | Нет | Inline errors |

\* GitHub Actions extension валидирует YAML локально без входа в GitHub.

`GitHub Pull Requests` (`GitHub.vscode-pull-request-github`) — бесплатен, но требует Sign in to GitHub в Cursor; для solo+agent достаточно `gh` CLI.

Установка (агент или Command Palette → **Extensions: Show Recommended Extensions**):

```bash
code --install-extension github.vscode-github-actions
```

В Cursor: те же ID через Extensions marketplace.

## 6. CI / Workflows (репозиторий)

| Workflow | Триггер | Назначение |
|----------|---------|------------|
| `ci.yml` | push, PR | ruff, pytest, mypy, live Binance |
| `dependency-review.yml` | PR | block critical CVE в diff |
| `codeql-analysis.yml` | push, PR, weekly | SAST Python + Actions |
| `supply-chain-audit.yml` | push, PR, weekly | pip-audit lockfile |
| `nightly-regression.yml` | cron | live pytest 03:00 UTC |
| `auto-fix.yml` | push bot/ | ruff PR |
| `quality-report.yml` | weekly Mon | ruff/vulture/jscpd artifacts |

Dependabot: `.github/dependabot.yml` — weekly pip + actions.

Security policy: [SECURITY.md](../SECURITY.md).

## 7. Dependabot PR — политика merge

1. Дождись зелёного **CI required checks**
2. **dependency-review** — no critical/high в diff
3. Pip group PR — `make check` локально если меняются major deps
4. Actions group PR — prefer наш SHA-pinned style; не merge blind `@v5` без pin

## 8. Branch protection (ручной шаг в GitHub UI)

Settings → Rules → Rulesets → New ruleset → `main`:

- Require PR before merge (optional для solo)
- Require status checks: **CI required checks**, **Lint**, **Unit tests**, **Type check**
- Block force push

API/rulesets требуют admin; агент не может включить без org admin.

## 9. Troubleshooting

| Ошибка | Решение |
|--------|---------|
| `refusing to allow OAuth App ... workflow scope` | Fine-grained **Actions+Workflows** или classic `workflow` scope; token в `.env` |
| `gh: To use GitHub CLI in automation...` | `./scripts/verify_github_token.sh` |
| CI: `dashboard.app is None` | Unit job ставит `[live,dev,test]` — нужен FastAPI |
| 6 Dependabot aiohttp alerts | Dismissed `tolerable_risk` — см. SECURITY.md |
| MCP github red | Docker running + `GITHUB_TOKEN` exported before Cursor start |

## 10. Ссылки

- [Fine-grained PAT](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)
- [GitHub MCP install Cursor](https://github.com/github/github-mcp-server/blob/main/docs/installation-guides/install-cursor.md)
- [Dependency review action](https://github.com/actions/dependency-review-action)
