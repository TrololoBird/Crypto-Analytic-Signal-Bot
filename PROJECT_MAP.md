# PROJECT MAP — Binance public signal-only bot

Updated: 2026-06-04

## Mission

Strictly analytical Binance public-data signal bot. Prepares market data, evaluates 38 strategy signals, validates contract, applies hard delivery gates, sends manual trade plans to Telegram. **No order placement.**

## Data flow (v9)

```text
Binance REST/WS (bot/market/rest_impl.py, bot/market/ws.py)
  → shortlist_service + universe (radar funnel, enrichment)
  → features/prepare_frame.py + prepare.py
  → engine + bot/strategies/*
  → symbol_analyzer + delivery/filters + confluence
  → delivery_orchestrator: validate_signal_contract → hard 3-of-5 gate
  → delivery.deliver + telegram.py
```

## Non-bypassable delivery path

```text
Signal → DeliveryOrchestrator → validate_signal_contract
  → _hard_confluence_gate → cooldown/tracking → delivery.deliver
```

Bypass raises `ValueError` in `DeliveryOrchestrator`.

## Core files

| Area | File | Role |
|------|------|------|
| Config | `bot/domain/config.py` | Pinned symbols, thresholds, runtime |
| Contract | `bot/delivery/contract.py` | Signal format / level validation |
| Delivery gate | `bot/runtime/delivery_orchestrator.py` | Contract, confluence, cooldown, send |
| Universe | `bot/market/universe.py` | Shortlist scoring, strategy fit routing |
| Shortlist | `bot/runtime/shortlist_service.py` | Funding, spread, OI, basis enrichment |
| Features | `bot/features/prepare_frame.py` | Polars indicators, swings, ATR/RSI/BB |
| Engine | `bot/engine/engine.py` | Strategy execution, lanes cap |
| Registry | `bot/strategies/__init__.py` | 38 `STRATEGY_CLASSES` |
| Shared detectors | `bot/strategies/_common.py`, `_roadmap.py` | OB, FVG, squeeze, divergences |
| Tracking | `bot/persistence/tracking.py` | Signal lifecycle, cooldowns |
| Cache | `bot/persistence/repository/cache.py` | Parquet time-series cache |

## Strategies

38 setup classes in `bot/strategies/` — catalog in `bot/domain/strategy_catalog.py` and `docs/research/STRATEGY_CATALOG.md`. Calibration via `scripts/strategy_shortlist_matrix.py` and `scripts/reconcile_strategy_defaults.py`.

## Guardrails

| Rule | Where |
|------|-------|
| No auto-trading | Delivery-only; hooks scan order-placement names |
| Contract first | `validate_signal_contract()` before selection |
| Hard confluence | 3 of 5: trend, momentum, volume, HTF, microstructure |
| ATR-based levels | Ordered entries, SL, TP1–3, RR gate |
| Cooldown | Setup + symbol-direction in orchestrator |
| Pinned anchors | BTCUSDT, ETHUSDT, SOLUSDT, XAUUSDT, XAGUSDT, PAXGUSDT |
| Indicators | Wilder ATR/RSI; BB ddof=1; no `shift(-N)` live |

## Verification (2026-06-04)

| Check | Command |
|-------|---------|
| Gate | `make check` |
| Config | `make validate-config` |
| Offline waves | `pytest tests/test_wave_f9_agent_*.py tests/test_wave_f10_agent_*.py tests/test_wave_i_calibration.py -q` |
| Live | `PYTEST_LIVE=1 pytest tests/live/ -v` |
| Smoke | `make live-smoke` |

Python **3.14.5** in `.venv`.

## Operational notes

- Public REST can exhaust weight during broad enrichment; use `[bot.network]` proxy bootstrap.
- Book-ticker/microprice may be null in REST-only checks without live WS.
- Session-bound strategies skip outside market window (explicit skip, not error).
- Live 6h sessions (2026-06-04): supervised runs with rollup + calibration loop documented in `docs/SOLO_OPERATOR_PLAYBOOK.md`.
