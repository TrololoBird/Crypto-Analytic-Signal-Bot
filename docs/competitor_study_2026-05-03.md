# Competitor Study - 2026-05-03

Scope: crypto trading/signal bots and adjacent algorithmic trading frameworks.

Evidence level:

- Confirmed metadata: GitHub REST API results collected on 2026-05-03.
- Additional source snippets checked through web search for Freqtrade, Hummingbot,
  OctoBot, Jesse, Superalgos, and CryptoSignal.
- Limitation: GitHub unauthenticated API rate limiting blocked full README pulls
  for most repositories. Architecture notes below are therefore calibrated as
  metadata/README-summary findings, not deep code audits.

## Required Popular Projects

| Project | Stars | Updated | Status | Architecture / feature notes | Applicable insight |
|---|---:|---|---|---|---|
| [freqtrade/freqtrade](https://github.com/freqtrade/freqtrade) | 49717 | 2026-05-03 | active | Python bot with backtesting, dry-run, Telegram/WebUI, hyperopt, FreqAI. | Keep signal-only runtime separate from replay/backtest evidence; use outcome data before ML gating. |
| [hummingbot/hummingbot](https://github.com/hummingbot/hummingbot) | 18448 | 2026-05-03 | active | Connector-oriented framework standardizing REST/WS exchange access. | Keep Binance public USD-M I/O centralized; strategies should not call exchange APIs directly. |
| [Drakkar-Software/OctoBot](https://github.com/Drakkar-Software/OctoBot) | 5845 | 2026-05-03 | active | Visual UI, grid/DCA/TradingView/AI connectors, 15+ exchanges. | Strong operator UX and explicit strategy configuration matter, but this bot should avoid autotrading paths. |
| [jesse-ai/jesse](https://github.com/jesse-ai/jesse) | 7839 | 2026-05-03 | active | Python research/backtest/optimize/live framework; README advertises ML gather/train/deploy workflow. | Add replay fixtures and outcome labels before changing high-risk setup logic. |
| [Superalgos/Superalgos](https://github.com/Superalgos/Superalgos) | 5444 | 2026-05-03 | active | Visual strategy design, data mining, backtesting, paper/live sessions. | Telemetry should be inspectable and replayable; avoid opaque detector decisions. |
| [asavinov/intelligent-trading-bot](https://github.com/asavinov/intelligent-trading-bot) | 1675 | 2026-05-02 | active | ML signals and feature engineering. | The local bot needs larger labeled outcome history before enabling live ML. |
| [Haehnchen/crypto-trading-bot](https://github.com/Haehnchen/crypto-trading-bot) | 3467 | 2026-05-02 | active | Multi-exchange JavaScript bot with public edition and UI orientation. | Keep config/user-facing diagnostics clear; avoid hidden strategy state. |
| [wen82fastik/ai-crypto-cryptocurrency-trading-bot](https://github.com/wen82fastik/ai-crypto-cryptocurrency-trading-bot) | 12 | 2026-04-19 | active | Low-star AI/LLM multi-exchange bot per metadata. | Treat as weak evidence; no architecture adoption without code review. |
| [CryptoSignal/Crypto-Signal](https://github.com/CryptoSignal/Crypto-Signal) | 5543 | 2026-05-03 | active | Technical-analysis signal bot per repo description/search result. | Signal scanners should export full indicator/rejection context for later calibration. |
| [DeviaVir/zenbot](https://github.com/DeviaVir/zenbot) | 8268 | 2026-04-30 | archived | Historical Node/MongoDB command-line crypto trading bot. | Useful mainly as abandonment warning: avoid unmaintained dependencies and unclear runtime boundaries. |

## Additional Current / Topical Projects

These were selected from GitHub topic/search results for `trading-bot`,
`cryptocurrency`, and recent non-fork crypto trading bot repositories. Spam-like
low-quality search hits were excluded.

| Project | Stars | Updated | Status | Architecture / feature notes | Applicable insight |
|---|---:|---|---|---|---|
| [chrisleekr/binance-trading-bot](https://github.com/chrisleekr/binance-trading-bot) | 5500 | 2026-05-03 | active | Binance-focused grid/TradingView bot per metadata. | Orderbook/grid ideas are not directly reusable until the local bot has signal-only semantics for them. |
| [freqtrade/freqtrade-strategies](https://github.com/freqtrade/freqtrade-strategies) | 5094 | 2026-05-03 | active | Strategy library for Freqtrade. | Useful for comparing parameter surfaces and avoiding lookahead bias. |
| [michaelgrosner/tribeca](https://github.com/michaelgrosner/tribeca) | 4107 | 2026-05-02 | active | High-frequency market-making platform in Node.js. | Orderbook strategies need explicit spread/depth telemetry and should remain read-only here. |
| [ctubio/Krypto-trading-bot](https://github.com/ctubio/Krypto-trading-bot) | 3678 | 2026-05-02 | active | C++ self-hosted market-making bot. | Relevant for depth/market microstructure metrics, not for direct execution logic. |
| [thrasher-corp/gocryptotrader](https://github.com/thrasher-corp/gocryptotrader) | 3429 | 2026-04-30 | active | Go multi-exchange trading bot/framework. | Connector and exchange capability boundaries should stay explicit. |
| [blankly-finance/blankly](https://github.com/blankly-finance/blankly) | 2431 | 2026-05-03 | active | Backtest/deploy algo framework for stocks/crypto/forex. | Local backtest/replay APIs should mirror live signal contracts. |
| [Open-Trader/opentrader](https://github.com/Open-Trader/opentrader) | 2421 | 2026-05-02 | active | Open-source crypto bot with DCA/grid/UI per metadata. | UI/dashboard value depends on reliable rejection and outcome telemetry first. |
| [whittlem/pycryptobot](https://github.com/whittlem/pycryptobot) | 2054 | 2026-05-02 | active | Python crypto bot. | Compare indicator configuration style, but do not import trading/execution assumptions. |
| [Lumiwealth/lumibot](https://github.com/Lumiwealth/lumibot) | 1386 | 2026-05-03 | active | Backtesting/trading bot framework across assets, with AI strategy support per metadata. | AI features should be gated by labeled outcome quality and offline validation. |
| [TheFourGreatErrors/alpha-rptr](https://github.com/TheFourGreatErrors/alpha-rptr) | 654 | 2026-05-02 | active | Python algorithmic trading bot for futures venues per metadata. | Futures-specific strategy ideas need public-data-only adaptation before consideration. |

## Integrated Findings

- Confirmed common pattern: successful projects separate strategy definition,
  exchange I/O, backtesting/replay, and operator control surfaces.
- Inference: this repository should prioritize replayable signal records,
  rejection-funnel telemetry, and MAE/MFE outcomes before adding more setups.
- Confirmed risk: many competitor projects are autotrading systems. Their
  execution, account, and signed-endpoint code paths are out of scope for this
  analytical signal bot.
- Adoption priority:
  1. Strengthen replay/backtest parity and outcome labels.
  2. Keep public Binance REST/WS boundary centralized.
  3. Expand orderbook/order-flow strategies only after telemetry captures the
     needed depth, spread, and trade-flow fields.
  4. Treat live ML as blocked until outcome history is statistically meaningful.

