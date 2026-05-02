# DIAG_2 WS audit

Timestamp: 2026-05-02T09:16:00Z

## Confirmed facts

- Static code path:
  - `bot.websocket.connection.build_stream_url(...)` builds `f"{base}/stream"`.
  - `bot.websocket.subscriptions.stream_endpoint_class(...)` routes `@bookTicker`/`@depth` to `public` and kline/ticker/mark/force-order streams to `market`.
- Current Binance USD-M docs require routed endpoints:
  - public: `wss://fstream.binance.com/public`
  - market: `wss://fstream.binance.com/market`
  - JSON `SUBSCRIBE` is supported.
- Pre-fix code normalized both endpoint classes back to root. That would make market streams drift to unrouted `wss://fstream.binance.com/stream`, which Binance says only receives public endpoint data after the routed endpoint migration.
- Runtime config also had `max_signals_per_cycle = 50`, while `BotSettings` allows `1..10`, so the current build could fail before WS startup.

## Code/config changes

- Restored routed WS endpoint preservation in `bot/config.py`.
- Kept `base_url` as root and resolved:
  - `public_base_url = wss://fstream.binance.com/public`
  - `market_base_url = wss://fstream.binance.com/market`
- Updated runtime validation to require `/public` and `/market` endpoint suffixes.
- Reduced `max_signals_per_cycle` to `10` in `config.toml` and `config.toml.example`.
- Restored missing `validate_runtime_public_rest_url(...)` import contract in `bot/market_data.py`; without it, `python main.py run` failed at import time.

## Live-run evidence

Run: `data/bot/logs/bot_20260502_091440_14552.log`

- `DOCTOR OK` showed:
  - `public_base_url=wss://fstream.binance.com/public`
  - `market_base_url=wss://fstream.binance.com/market`
- WS connected:
  - `endpoint=public url=wss://fstream.binance.com/public/stream streams=4`
  - `endpoint=market url=wss://fstream.binance.com/market/stream streams=8`
- Subscriptions sent:
  - public: 4 streams, 1 chunk
  - market: 8 streams, 1 chunk
- Kline data arrived and was published:
  - BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT 15m kline close events.
- Analysis cycles occurred:
  - cycle lines for BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT.
  - detector runs: 15 per analyzed symbol.
- Telegram evidence:
  - startup report message was sent before logging configured; stdout evidence shows a Telegram message with startup snapshot.
  - prior startup message id in zero-cycle run was 4741; this short run stdout did not expose the new message id because the structlog stdout line omitted it.

## Remaining issue discovered

Shortlist formation failed during the live-run:

```text
shortlist refresh failed, using pinned fallback: UniverseSymbol.__init__() got an unexpected keyword argument 'strategy_fits'
shortlist build failed, using pinned | error='UniverseSymbol' object has no attribute 'strategy_fits'
```

Inference: WS transport is now working, but DIAG_3 must fix the shortlist model/constructor mismatch so runtime uses the intended 45-symbol shortlist instead of pinned fallback.
