# Crypto Analytic Signal Bot (v9)

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
| `ml` | `pip install -e ".[ml]"` | Offline experiments only |

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

- [docs/CURSOR_SETUP.md](docs/CURSOR_SETUP.md) — editor, venv, skills
- [docs/MARKET_DATA_PRINCIPLES.md](docs/MARKET_DATA_PRINCIPLES.md) — public data only
- [docs/DEPENDENCIES.md](docs/DEPENDENCIES.md) — package usage map
- [docs/research/README.md](docs/research/README.md) — strategy research spec
- [AGENTS.md](AGENTS.md) — agent / graphify rules

## CI

GitHub Actions (`.github/workflows/ci.yml`): Python **3.14.5**, ruff, offline pytest, mypy critical path, optional live Binance on `main`.

## License

MIT — see [LICENSE](LICENSE).
