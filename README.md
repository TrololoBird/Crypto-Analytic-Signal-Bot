# Crypto Analytic Signal Bot

Event-driven cryptocurrency signal bot with public-only Binance USDⓈ-M market data, multi-timeframe analysis, and an integrated web dashboard.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Configure
cp config.toml.example config.toml
# Edit config.toml with your settings

# Start bot
python main.py
```

## Dashboard

The bot automatically opens a web dashboard at http://localhost:8080 with:
- Real-time bot status and market regime
- Active trading signals with entry/stop/TP levels
- Performance analytics and win rates
- Strategy configuration
- Keyboard shortcuts (1-4 for tabs, R to refresh, ? for help)

See [docs/OPERATIONS.md](docs/OPERATIONS.md) for detailed documentation.

## Architecture

- **Public-only Binance data plane**: only public USDⓈ-M REST market-data endpoints and market/public WebSocket streams; no auth, no account/trade endpoints, no user-data streams
- **Event-driven runtime**: WebSocket kline events → EventBus → Strategy analysis
- **Multi-timeframe analysis**: 5m, 15m, 1h, 4h timeframes with confluence scoring
- **Shortlist engine**: full REST rebalance plus light WS rerank with composite liquidity/freshness/OI/crowding scoring
- **Risk management**: Automated stop-loss, take-profit, and position sizing
- **ML integration**: Optional ML filtering for signal quality
- **SQLite persistence**: All data stored locally with no external dependencies

## Documentation

- [Operations Guide](docs/OPERATIONS.md) — Running and monitoring the bot
- [Architecture Overview](docs/ARCHITECTURE.md) — System design and components
- [Strategy Reference](docs/STRATEGIES.md) — Available trading strategies

## License

MIT
