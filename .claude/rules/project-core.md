# Crypto Signal Bot v9 — core guardrails

Event-driven Binance USDⓈ-M **public** signal bot. Telegram manual signals only. Python **3.14.5**, Polars pipeline, async runtime.

## Non-negotiable

- NO auto-trading, order placement, or private/auth Binance endpoints
- Delivery path: `validate_signal_contract` → `hard_confluence_gate` (3-of-5) → `delivery.deliver` — never bypass
- Strategy logic in `bot/strategies/` only (no `setups/detectors/` duplicate tree)
- Orchestration in `bot/runtime/`; data plane `bot/market/`; features `bot/features/`
- Wilder ATR/RSI; BB std ddof=1; no `shift(-N)` on live path
- Rewrite broken modules; do not disable strategies silently

## Sole executor

Human: direction + acceptance only. Agent: all commands, config, probes, terminals.

- Before live/smoke: `python scripts/clean_session_data.py --mode smoke --config config.toml`
- Proxy: `scripts/discover_binance_proxies.py` + `bot.market.proxy_bootstrap`
- Never end with “run/configure/close terminal yourself” — report outcomes

## Token economy

- Session start: `CLAUDE.md` + `HANDOFF_REPORT.md` + `docs/DEFINITION_OF_DONE.md` — no full-repo reads
- No new «50 improvements» lists; backlog table only
- `graphify query` before broad grep when `graphify-out/graph.json` exists

## Verify after `bot/` edits

```bash
make check
PYTEST_LIVE=1 pytest tests/live/ -v
```
