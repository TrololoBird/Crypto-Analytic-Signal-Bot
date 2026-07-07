# Hunt (crypto-hunter)

Standalone crypto-futures signal-analytics package in the monorepo — **two independent modules**:

- **Deep** (`hunt_core/deep/`) — 5-module gating pipeline (macro/trend/structure/positioning/risk) for pinned majors and `/signal SYM`
- **Scanner** (`hunt_core/scanner/`) — universe-wide pre-pump/pre-dump detection (`run_scan()`, `PrescanEngine`)

Both share only via `hunt_core/signals/`, `data/`, `market/`, `track/` — they never import each other.

- Public **Binance USDⓈ-M** via **CCXT** — 100% CCXT market plane, no raw Binance HTTP, no CoinMarketCap/CoinGecko (`hunt_core/deep/pipeline/macro_data.py` computes BTC.D/TOTAL3 as a CCXT `fetchTickers()` quoteVolume proxy)
- **Telegram** manual signals only — signal-analytics, no auto-trading, no private Binance auth
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

Secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` in `.env` (repo root).

Data: `hunt/data/` — runtime state, watchlist, calibration cache.

## Package layout

```
hunt/
├── hunt_core/
│   ├── deep/            # Deep module: 5-module gating pipeline + config
│   │   └── pipeline/    # macro/trend/structure/positioning/risk + config.py (reads config.defaults.toml [deep])
│   ├── scanner/         # Scanner module: universe pre-pump/pre-dump (prescan, gate, detect)
│   ├── toolkit/         # Shared analytical primitives (manipulation fusion, order flow, robust stats)
│   ├── market/          # CCXT client, rate limiting, WS/REST transport (shared kernel)
│   ├── signals/         # Shared spine: Signal, setup_id dedup, lifecycle states
│   ├── data/, track/, deliver/, domain/, features/, runtime/, ...
├── docs/                # SPEC_v5.1.md (Deep pipeline target spec)
├── config.toml / config.defaults.toml   # includes [deep.*] sections for pipeline thresholds
└── data/                # Runtime state + baseline/
```

## vs main bot

| | Main bot (`bot/`) | Hunt |
|---|-------------------|------|
| Trigger | WS kline close | CCXT REST poll (Deep: every `HUNT_DEEP_PINNED_INTERVAL`s, default 300s) + Scanner tick |
| Delivery | contract → confluence 3/5 | Deep 5-module gating / Scanner prescan → TG |
| Universe | shortlist | Deep: pinned majors + `/signal SYM`; Scanner: full USDⓈ-M universe |

## Configuration

`PipelineConfig.load()` (`hunt_core/deep/pipeline/config.py`) reads `[deep]`/`[deep.macro]`/`[deep.trend]`/`[deep.positioning]`/`[deep.positioning.vp_ofi]`/`[deep.risk]`/`[deep.new_coin]`/`[deep.regime]` from `config.defaults.toml`, merging overrides onto the dataclass defaults — same pattern as `hunt_core/scanner/detect/config.py::fusion_params()`.

## Verification

After `pip install -e "./hunt"` (repo root, venv active):

```bash
python -m compileall -q hunt/hunt_core
```

There is no `_dev` diagnostics package or `verify` subcommand in the current tree — verify via `compileall` plus a live smoke run:

```bash
python -m hunt_core watch --once --no-telegram
```

## Docs

- [SPEC_v5.1.md](docs/SPEC_v5.1.md) — Deep 5-module pipeline target specification
