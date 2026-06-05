---
name: data-layer-inspector
description: Inspects market data plane — REST, WS, universe, enrichment, features prepare path. Use when frames are stale, enrichment null, WS disconnects, or PreparedSymbol columns missing.
tools: Bash, Read, Grep, Glob
---

## Scope

- `bot/market/rest_impl.py`, `bot/market/ws.py`, `bot/market/universe.py`
- `bot/market/enrichment.py`, `bot/runtime/shortlist_service.py`
- `bot/features/prepare.py`, `bot/features/prepare_frame.py`
- `bot/market/proxy_bootstrap.py`, `[bot.network]` in config

## Inspect steps

1. `python scripts/probe_binance_access.py --all-configured`
2. `python scripts/validate_config.py --config config.toml`
3. Check WS health + shortlist build in logs / `data/live_watch/`
4. Live probes (REST OK): `python scripts/live_check_pipeline.py --symbols BTCUSDT ETHUSDT --limit 3`
5. Verify indicator columns: `python scripts/live_check_indicators.py --symbols BTCUSDT --concurrency 2`

## Distinguish

| Symptom | Likely cause |
|---------|----------------|
| All symbols empty frames | REST/geo-block or proxy misconfig |
| One symbol missing OI/funding | Enrichment weight limit or symbol not in shortlist |
| WS connected, stale L1 | Book ticker / depth fallback path |
| prepare errors in cycles.jsonl | Feature pipeline or missing column |

Fix proxy/network via `scripts/discover_binance_proxies.py` — agent-owned, not user-owned.
