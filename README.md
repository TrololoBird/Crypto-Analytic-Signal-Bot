# Crypto Analytic Signal Bot (v9)

[![CI](https://github.com/TrololoBird/Crypto-Analytic-Signal-Bot/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/TrololoBird/Crypto-Analytic-Signal-Bot/actions/workflows/ci.yml)
[![Python 3.14.5](https://img.shields.io/badge/python-3.14.5-blue.svg)](https://www.python.org/downloads/)
[![Security policy](https://img.shields.io/badge/security-Security.md-green.svg)](SECURITY.md)

Event-driven **Binance USDⓈ-M public** futures signal bot. Detects 38 strategy setups on closed candles, applies delivery gates (contract → 3-of-5 confluence → Telegram), and tracks outcomes locally. **No auto-trading**, no private Binance keys.

| Requirement | Value |
|-------------|--------|
| Python | **3.14.5** (see `.python-version`; not 3.14 free-threading) |
| Config | `config.toml` (copy from `config.toml.example`) |
| Secrets | `.env` (see `env.example`) |

## Quick start

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[live,dev,test]"

copy config.toml.example config.toml
copy env.example .env
# Edit .env: TG_TOKEN, TARGET_CHAT_ID (or set provider=none in config for dry runs)

python scripts/validate_config.py --config config.toml
python main.py
```

Dashboard (when `[live]` installed): http://127.0.0.1:8080 — metrics: http://127.0.0.1:9090/metrics

### Binance from Russia / geo-blocked networks

Binance public API may be unreachable without a proxy. Configure a **local** SOCKS5/HTTP client (Clash, Mihomo, Tor) — see [docs/BINANCE_PROXY_RU.md](docs/BINANCE_PROXY_RU.md).

```powershell
$env:BINANCE_PROXY_URL = "socks5h://127.0.0.1:7890"
python scripts/probe_binance_access.py
```

## Architecture (v9)

```
bot/market/     REST + WebSocket, universe, enrichments
bot/features/   Polars pipeline (prepare_frame, indicators)
bot/strategies/ 38 canonical detectors (no bot/setups/detectors/)
bot/engine/     Lanes + strategy registry
bot/runtime/    Bot loop, analyzer, delivery orchestrator
bot/delivery/   Contract, confluence, Telegram
bot/persistence/ SQLite tracking, audit ledger
```

Full matrix: [docs/REFACTOR_PLAN.md](docs/REFACTOR_PLAN.md) · Canonical layout: [docs/research/ARCHITECTURE_CANONICAL.md](docs/research/ARCHITECTURE_CANONICAL.md)

## Dependencies

| File | Role |
|------|------|
| [pyproject.toml](pyproject.toml) | **Canonical** version ranges + extras |
| [requirements.txt](requirements.txt) | Install recipes + import map (comments) |
| [requirements-lock.txt](requirements-lock.txt) | Pinned versions for 3.14.5 audits |

Extras:

| Extra | Install | Purpose |
|-------|---------|---------|
| (core) | `pip install -e .` | Bot runtime |
| `live` | `pip install -e ".[live]"` | Dashboard, orjson, polars_ta |
| `dev` | `pip install -e ".[dev]"` | ruff, mypy, pre-commit |
| `test` | `pip install -e ".[test]"` | pytest |
| `regime` | `pip install -e ".[regime]"` | Optional HMM/GMM regime |

Improvement backlog (agent hours, not weeks): [docs/IMPROVEMENT_PLAN.md](docs/IMPROVEMENT_PLAN.md)

Verify imports: `python scripts/verify_dependencies.py`

## Verification

```powershell
python -m compileall -q bot
python scripts/validate_config.py --config config.toml.example
python scripts/verify_refactor_gate.py
python scripts/project_health_audit.py --stale-days 2 --full
python scripts/run_mypy_critical.py
pytest tests/ -q --ignore=tests/live
```

Live Binance tests (network, optional):

```powershell
$env:PYTEST_LIVE=1
pytest tests/live/ -v -m live
```

On geo-restricted networks (including some GitHub Actions regions), live tests **skip** automatically after a public API probe.

## Scripts

| Script | Description |
|--------|-------------|
| `scripts/validate_config.py` | Config + strategy wiring smoke |
| `scripts/verify_refactor_gate.py` | v9 layout / 38 strategies |
| `scripts/project_health_audit.py` | Stale files, forbidden paths, gates |
| `scripts/verify_dependencies.py` | Import all declared deps |
| `scripts/fix_py314_except.py` | After `ruff format` (Py3.14 except syntax) |
| `scripts/run_mypy_critical.py` | Mypy on delivery/config/merge/lanes |

## Documentation

- **[Project roadmap & status](docs/PROJECT_ROADMAP_AND_STATUS.md)** — completed waves (E1–F11), remaining work, live ops commands

- [docs/CURSOR_SETUP.md](docs/CURSOR_SETUP.md) — editor, venv, skills
- [docs/MARKET_DATA_PRINCIPLES.md](docs/MARKET_DATA_PRINCIPLES.md) — public data only
- [docs/DEPENDENCIES.md](docs/DEPENDENCIES.md) — package usage map
- [docs/BINANCE_PROXY_RU.md](docs/BINANCE_PROXY_RU.md) — proxy/VPN for geo-blocked regions
- [docs/research/README.md](docs/research/README.md) — strategy research spec
- [AGENTS.md](AGENTS.md) — agent / graphify rules

## CI & GitHub

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| [CI](.github/workflows/ci.yml) | push / PR | ruff, pytest, mypy critical, live Binance |
| [Dependency Review](.github/workflows/dependency-review.yml) | PR | block critical CVE in dependency diff |
| [Supply Chain Audit](.github/workflows/supply-chain-audit.yml) | push / PR / weekly | pip-audit lockfile (fail HIGH+) |
| [CodeQL](.github/workflows/codeql-analysis.yml) | push / PR / weekly | static analysis Python + Actions |
| [Nightly Regression](.github/workflows/nightly-regression.yml) | cron 03:00 UTC | live pytest + strategy smoke |
| [Auto Fix](.github/workflows/auto-fix.yml) | push `bot/` | ruff auto-format PR |
| [Quality Report](.github/workflows/quality-report.yml) | weekly Mon | ruff/vulture/jscpd artifacts |

- **Dependabot**: weekly pip + GitHub Actions (`.github/dependabot.yml`)
- **Security**: [SECURITY.md](SECURITY.md) — reporting, known `aiohttp` constraint
- **GitHub + Cursor token/MCP**: [docs/GITHUB_CURSOR_SETUP.md](docs/GITHUB_CURSOR_SETUP.md)
- **CODEOWNERS**: delivery / market / CI paths

Python **3.14.5** on `ubuntu-latest`. Offline tests always; live Binance advisory on PRs, required on `main` pushes where network allows.

## License

MIT — see [LICENSE](LICENSE).
