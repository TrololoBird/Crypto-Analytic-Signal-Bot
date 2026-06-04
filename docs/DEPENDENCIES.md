# Dependencies (v9)

**Canonical:** [pyproject.toml](../pyproject.toml)  
**Pins (3.14.5):** [requirements-lock.txt](../requirements-lock.txt)  
**Install guide:** [requirements.txt](../requirements.txt)

## Core runtime → modules

| Package | Version (lock) | Used by |
|---------|------------------|---------|
| python-dotenv | 1.2.2 | `bot/secrets.py` |
| polars | 1.41.2 | features, market, strategies, persistence |
| aiohttp | 3.13.5 | `bot/market/rest_*`, messaging (3.14 blocked by aiogram 3.28) |
| aiohttp-socks | 0.10.1 | `bot/market/network_proxy.py` (SOCKS REST) |
| python-socks[asyncio] | 2.7.x | `network_proxy` + `websockets` SOCKS |
| numpy | 2.4.6 | features, regime, setups/smc |
| aiogram | 3.28.2 | `bot/delivery/telegram.py` |
| websockets | 16.0 | `bot/market/ws*.py` |
| aiosqlite | 0.22.1 | repository, migrations, diary |
| msgspec | 0.21.1 | `bot/domain/events.py`, tracking |
| structlog | 25.5.0 | logging, diagnostics |
| pydantic | 2.13.4 | `bot/domain/config.py` |
| tenacity | 9.1.4 | Telegram retries |

## Live extra

| Package | Optional | Used by |
|---------|----------|---------|
| fastapi, uvicorn | yes | `bot/dashboard/` |
| prometheus-client | yes | metrics endpoint |
| orjson | yes | WS JSON fast path |
| polars_ta, polars-ols | yes | `bot/features/prepare_frame.py` |

## Dev / test

| Package | Role |
|---------|------|
| ruff 0.15.15 | lint + format (run `fix_py314_except.py` after format on 3.14) |
| mypy 2.1.0 + pydantic plugin | `scripts/run_mypy_critical.py` |
| pytest 9 + pytest-asyncio 1.4 | `tests/` |

## Not on live path

- `[regime]` — hmmlearn, sklearn, statsmodels (`bot/regime/`, ImportError fallback)

Hot path is **Polars-only** (`prepare_frame.py`); no TA-Lib, no pandas. `prepare.py` rejects pandas DataFrames explicitly.

## Verification commands

```powershell
pip install -e ".[live,dev,test]"
python scripts/verify_dependencies.py
pip install aiohttp==3.14.0  # when bumping; re-run pytest
```

Last audited: **2026-06-02** (Python 3.14.5). aiohttp capped at 3.13.x until aiogram allows 3.14.
