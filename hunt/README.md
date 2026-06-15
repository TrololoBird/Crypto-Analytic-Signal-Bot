# Hunt Watch (crypto-hunter)

**Memecoin pump/dump minute scanner** — independent package in the monorepo.

- Public **Binance USDⓈ-M** via **CCXT** REST + Pro WebSocket
- **Telegram** manual signals on closed-bar confirm
- **No auto-trading**, no private Binance auth
- Canonical package: **`hunt_core/`** only — `python -m hunt_core`

## Quick start

```bash
# repo root, venv active
pip install -e "./hunt"

# single tick (no Telegram)
python -m hunt_core watch --once --no-telegram

# production loop
python -m hunt_core watch --interval 60
```

Secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` in `.env`.

Data: `hunt/data/` — see [docs/DEPLOY.md](docs/DEPLOY.md).

## Package layout

```
hunt/
├── hunt_core/          # Canonical: market, data, features, scan, regime, gate, deliver
├── docs/               # HUNT_ARCHITECTURE, DEPLOY, LIBRARY_STACK
├── config.defaults.toml
└── data/               # Runtime state + baseline/
```

## vs main bot

| | Main bot (`bot/`) | Hunt |
|---|-------------------|------|
| Trigger | WS kline close | REST poll + WS enrich |
| Delivery | contract → confluence 3/5 | Hunt confirm → TG |
| Universe | shortlist | pinned + scanner watchlist |

## Verification

```bash
cd hunt && PYTHONPATH=.
python -m compileall -q hunt_core
python -m hunt_core._dev.budget
python -m hunt_core._dev.check_scenarios
python -m hunt_core._dev.smoke_signals --baseline data/baseline/hunt_baseline.json BTCUSDT
```

Offline logic checks replaced by `_dev/check_*` + CI live-smoke (no `verify` subcommand).

```bash
python -m hunt_core._dev.check_logic
```

## Docs

- [HUNT_ARCHITECTURE.md](docs/HUNT_ARCHITECTURE.md) — canonical architecture
- [DEPLOY.md](docs/DEPLOY.md) — install, run, ops
- [LIBRARY_STACK.md](docs/LIBRARY_STACK.md) — polars + ccxt deps
