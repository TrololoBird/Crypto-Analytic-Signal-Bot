# Signal Bot OSS Landscape

External research for a **public-only** Binance USD-M **signal-only** bot: posts manual trade plans to Telegram. **No API keys, no order placement, no auto-trading in our product.** Rows below labeled «auto-trade» describe **OSS to avoid**, not our architecture.

## Categories

| Category | Examples | Lesson |
|----------|----------|--------|
| Auto-trade from Telegram | [binance-futures-bot](https://github.com/shivpatel-dev/binance-futures-bot), [Binance-Futures-Signal-Bot](https://github.com/Whit1985/Binance-Futures-Signal-Bot) | **Anti-pattern:** keys + execution risk |
| Read-only signal engine | [binance-signal-engine](https://github.com/eplt/binance-signal-engine) | MTF score → trade plan; public OHLCV |
| TA alert bots | [Crypto-Signal](https://github.com/CryptoSignal/Crypto-Signal) (~5.6k★) | Indicator scanner, **not** trade plans — см. [CRYPTO_SIGNAL_COMPARISON.md](CRYPTO_SIGNAL_COMPARISON.md); high volume = anti-pattern for manual TG |
| Futures signal + charts | [trading_signal_bot](https://github.com/rizesky/trading_signal_bot) | MTF, regime, cooldown — often pulls keys |
| Frameworks | [jesse](https://github.com/jesse-ai/jesse), [freqtrade](https://github.com/freqtrade/freqtrade) | Backtest strong; live = ccxt/keys |
| Polars pipelines | [PolarBT](https://github.com/nikkisora/PolarBT) | Vector features; trading separate |
| SMC libraries | [smart-money-concepts](https://github.com/joshyattridge/smart-money-concepts) | OB, FVG, BOS, liquidity semantics |
| Signal parsers | [telegram-crypto-signal-parser](https://github.com/joostmbakker/telegram-crypto-signal-parser) | Target JSON shape for outbound messages |
| HFT / replay | [high-perf-trading](https://github.com/fredski02/high-perf-trading) | Event log + per-symbol engine ideas |
| Price alerts firehose | [kairos-quantum](https://github.com/Enmilo-dev/kairos-quantum) | 2000+ pairs one WS; Redis dedup — radar only |
| TG auto-execution | [gary-bot](https://github.com/alhaannn/gary-bot) | Stale-msg reject, per-channel state |
| Risk / dedup trading | [algo-trading-platform](https://github.com/yakub268/algo-trading-platform) | Contradictory positions blocker, cooldown |
| Liq listener | [binanceliquidationlistener](https://github.com/xiaoshulittletree/binanceliquidationlistener) | `!forceOrder@arr` → CSV |
| Market making / data | [hummingbot](https://github.com/hummingbot/hummingbot) | OB + trades recording; not signal TG |

## Architectural lessons

1. **Permanent separation:** Data plane (public) | Signal engine | Delivery | Tracking | Ops dashboard.
2. **Do not use** python-binance/ccxt on the live signal hot path — scope creep to private API ([freqtrade #4136](https://github.com/freqtrade/freqtrade/issues/4136): signal mode ≈ dry-run, not a distinct production architecture).
3. **Best references:** binance-signal-engine (trade plan) + smartmoneyconcepts (SMC) + Polars. **Crypto-Signal:** borrow modularity/`alert_frequency: once`, reject 500×always alerts ([comparison](CRYPTO_SIGNAL_COMPARISON.md)).
4. **Connector:** thin public aiohttp, not CCXT on hot path — [CONNECTOR_DECISION.md](CONNECTOR_DECISION.md).

## Subscriber expectations (media)

Sources: [Bitrates Telegram guide](https://www.bitrates.com/news/p/the-best-telegram-channels-for-crypto-signals-a-guide-to-finding-reliable-groups/), [Markets Herald scams](https://marketsherald.com/how-to-find-legit-crypto-telegram-influencers-and-avoid-scams/), [Fat Pig verification 2026](https://www.fatpigsignals.com/blog/how-to-verify-crypto-signal-claims-in-2026/), [Coinmonks 6-month test](https://medium.com/coinmonks/i-tested-crypto-signal-groups-for-6-months-heres-what-i-learned-b5d4b42cb526).

### Required for trust

- Entry + SL + ≥1 TP (ideally TP1–TP3) on every ACTION.
- Explain **why** (pattern, TF, R:R).
- Public history including **losses**.
- Paper track 4–8 weeks before paid VIP.
- Disclaimer: education only, no auto-trading.

### Anti-patterns

- Guaranteed 90%+ win rate, no SL.
- Chase signals after the move; deleted losing posts.
- 100+ full ACTION posts/day without WATCH/ACTION tiers.
- Paid VIP with zero free preview.

## Channel model (chosen)

**Tiered:** many WATCH (silent) + moderate ACTION (~15–40/day active market) + burst cap per 15m window.
