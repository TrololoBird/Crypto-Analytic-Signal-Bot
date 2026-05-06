# Live Validation Report - 2026-05-06 02:33 Europe/Moscow

## Scope

Validation after runtime boundary changes for public REST URL allowlisting,
WebSocket stream endpoint routing, and disabled notifier support.

## Runs

- Run A: `C:\Users\undea\AppData\Local\Temp\bot2-live-20260506020018`, duration 931.912s, stopped externally after the 15-minute window.
- Run B: `C:\Users\undea\AppData\Local\Temp\bot2-live-mem-20260506021709`, duration 1000.312s, stopped externally after memory sampling.

Both runs used a temporary working directory, `config.toml.example`, `provider = "none"`, `BOT_DISABLE_HTTP_SERVERS=1`, and public Binance USD-M data only.

## Confirmed

- Runtime initialized without Telegram credentials using disabled notifier mode.
- WebSocket connected to routed endpoints:
  - `wss://fstream.binance.com/public/stream`
  - `wss://fstream.binance.com/market/stream`
- Run A processed 38 closed `15m` kline events and logged 39 local signal outputs.
- Run A log scan found 0 `ERROR`, 0 `CRITICAL`, 0 `Traceback`, and 0 `Unhandled exception` lines.
- Run B sampled process WorkingSet externally: first 452.488MB, last 352.961MB, min 154.562MB, max 532.805MB.

## Failed / Not Clean

- Run B logged 3 handled strategy timeout errors:
  - `vwap_trend timed out after 12.00s`
  - `price_velocity timed out after 12.00s`
  - `rsi_divergence_bottom timed out after 12.00s`
- Run B did not capture a logged `15m` kline close in its window.
- Memory flatness is not proven by current telemetry because runtime telemetry does not emit RSS; external Windows WorkingSet sampling was volatile.

## Status

FAIL for the strict `SYSTEM_PROMPT.md` live gate because the memory-sampled run was not error-clean.
PASS for the narrower endpoint/notifier changes: startup, public WS routing, local signal logging, and REST/WS boundary tests.
