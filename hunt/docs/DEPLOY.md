# Hunt Watch — Deploy & Operations

Standalone **crypto-hunter** package (`hunt/`). Public Binance USDⓈ-M only. No auto-trading.

## Prerequisites

- Python **3.14.5** (repo `.venv`)
- `config.toml` + `.env` with `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (optional for `--no-telegram` smoke)
- Network: Binance REST/WS reachable (proxy via `[bot.network]` if geo-blocked)

## Install

```bash
source .venv/bin/activate
pip install -e ".[live,dev,test]"
pip install -e "./hunt"
python scripts/validate_config.py --config config.toml
python scripts/clean_session_data.py --mode smoke --config config.toml
```

## Run modes

| Command | Purpose |
|---------|---------|
| `python -m hunt_core watch --interval 60` | Production minute loop + Telegram |
| `python -m hunt_core watch --once --no-telegram` | Single tick smoke |
| `python -m hunt_core._dev.check_logic` | Offline logic self-check (replaces removed `verify`) |

## Data paths (canonical: `hunt_core.paths`)

| Path | Content |
|------|---------|
| `hunt/data/dump_minute_watch.jsonl` | Tick archive |
| `hunt/data/hunt_signal_state.json` | Active tracker |
| `hunt/data/hunt_calibration.json` | Calibrated thresholds |
| `hunt/data/lake/hunt_lake.sqlite` | SQLite lake |
| `hunt/data/watch.pid` | Single-instance lock |

## Verification gates

| Gate | Command | Pass |
|------|---------|------|
| Logic | `python -m hunt_core._dev.check_logic` + `check_scenarios` | exit 0 |
| LOC budget | `python -m hunt_core._dev.budget` | ≤44k hot LOC, no engine/bot imports |
| Compile | `python -m compileall -q hunt/hunt_core` | exit 0 |

## Architecture

See [HUNT_ARCHITECTURE.md](HUNT_ARCHITECTURE.md).

**Entry:** `python -m hunt_core watch` → `hunt_core.runtime._impl` → `cycle.run_loop`

**Market plane:** unified `ccxt.binance` + `defaultType: future` for REST **and** Pro WS.
See [CCXT.md](CCXT.md).

## Multi-exchange intel (default on)

| Env | Default | Role |
|-----|---------|------|
| `HUNT_MULTI_EXCHANGE` | `1` | REST funding/OI/mark from Bybit, OKX, Bitget |
| `HUNT_CROSS_WS` | `1` | Pro WS funding on secondary venues |
| `HUNT_CROSS_REFRESH_S` | `300` | REST refresh interval |
| `HUNT_CROSS_MAX_SYMBOLS` | `24` | Symbols per cross refresh |
