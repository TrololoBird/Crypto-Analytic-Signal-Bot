# Market data principles (post-refactor)

Aligned with [Binance USD-M docs](https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info).

## Correct interaction model

| Layer | Source | When |
|-------|--------|------|
| **Live prices** | WS `!ticker@arr`, `!markPrice@arr@1s`, `@bookTicker` / `!bookTicker` | Always while bot runs |
| **Klines (trigger TFs)** | WS `@kline_<interval>` per shortlist symbol | Primary; REST only if cache miss / backfill |
| **Universe meta** | REST `exchangeInfo` + `ticker/24hr` | Full refresh (~2h), not per analysis tick |
| **Shortlist refresh** | WS tickers (`ws_light`) between full REST rebuilds | ~75s light, REST full on interval |
| **Positioning / OI** | REST `/futures/data/*` | Scheduled batches; **1000 req / 5 min / IP** — never per intra-candle symbol |
| **Depth L2** | WS `@depth` top-N symbols only | Not REST per signal |

## What caused “degradation” in debug runs

1. **REST flood**: `analysis_concurrency=8` × multiple kline REST fallbacks × enrichments — exceeded 20s timeouts and opened circuit breakers.
2. **Semaphore mismatch**: global REST semaphore was 100 while config said `max_concurrent_rest_requests=3`.
3. **WS drops**: incoming message limit used `acquire()` + **drop** instead of **wait** → stale book/mark, `enrichment degraded`, high lag.
4. **DEBUG log volume**: masked real errors; use DEBUG only for short investigations.

## Implementation rules (code)

- `configure_rest_concurrency()` must run at container build from `settings.runtime.max_concurrent_rest_requests`.
- WS `_process_message_internal` must `wait_for_slot()` on the 10 msg/s limit.
- Hot path `fetch_frames`: use `get_cached_klines` for 4h before REST; prefer WS kline buffers for 5m/15m/1h.
- Do not call `fetch_priority_history_bundle` (4× kline types per interval) on the live analysis path — OI refresh / backtest only.
- `PublicIntelligenceService.collect` must respect futures-data budget (batch + interval), not run fully in parallel with 40+ symbols during analysis.

## Verification

```powershell
python scripts/verify_refactor_gate.py
$env:PYTEST_LIVE=1; pytest tests/live/test_binance_public_api.py -v
python -m scripts.live_check_pipeline --limit 10
# Production-like (INFO):
#   log_level = "INFO" in config.toml
python scripts/live_smoke_bot.py --runtime-seconds 600 --warmup-seconds 90
```
