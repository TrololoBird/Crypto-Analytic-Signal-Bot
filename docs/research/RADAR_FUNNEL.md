# Radar funnel (Tier 0–4)

> Implemented 2026-06-04. Signal-only: radar/WATCH does not bypass delivery gates.

## Architecture

| Tier | Data source | Symbols | Output |
|------|-------------|---------|--------|
| 0 | `!ticker@arr` WS | All liquid USDT perp | `MarketRadarStore` |
| 1 | Local aggregate + screener | warm_pool_limit (~200) | flags, prescore_boost |
| 2 | hot_pool_limit (~60) | impulse / vol / 24h change | tier HOT |
| 3 | shortlist_limit (~50) | build_shortlist + radar merge | WS kline subscribe |
| 4 | Deep path | shortlist | 38 setups → delivery |

## Modules

- `bot/market/radar_state.py` — ingest + tiers
- `bot/market/universe_screener.py` — light flags (RSI proxy, impulse, vol)
- `bot/market/promotion_engine.py` — tier cycle + shortlist merge
- `bot/runtime/shortlist_service.py` — wires funnel into refresh
- Config: `[bot.universe.radar]` in `config.toml.example`

## Telemetry

| Stream | Content |
|--------|---------|
| `shortlist_build.jsonl` | `radar`, `radar_tier_cycle`, `radar_watch` summary |
| `radar_health.jsonl` | tier counts, stale ingest ratio, status |
| `radar_watch.jsonl` | warm/hot candidates not on deep shortlist (WATCH-only) |

Operator DM for radar WATCH: `universe.radar.emit_watch_candidates=true` and
`notifiers.telegram_operator.send_radar_watch_candidate=true` (never bypasses delivery).
