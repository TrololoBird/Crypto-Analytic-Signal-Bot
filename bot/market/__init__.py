"""Market data plane — REST, WebSocket, universe, enrichments (v9)."""

from __future__ import annotations

import importlib
from typing import Any

# Eager: no dependency on features
from bot.market.data import BinanceFuturesMarketData, MarketDataUnavailable
from bot.market.rest import BinanceClient, BinanceClientImpl
from bot.market.universe import build_shortlist, rerank_shortlist, strategy_fits_for_market_row

_LAZY: dict[str, tuple[str, str]] = {
    "FuturesWSManager": (".ws", "FuturesWSManager"),
    "MessageBuffer": (".ws", "MessageBuffer"),
    "RateLimiter": (".ws", "RateLimiter"),
    "PublicIntelligenceService": (".enrichment", "PublicIntelligenceService"),
}

__all__ = [
    "BinanceClient",
    "BinanceClientImpl",
    "BinanceFuturesMarketData",
    "MarketDataUnavailable",
    "build_shortlist",
    "rerank_shortlist",
    "strategy_fits_for_market_row",
    *sorted(_LAZY),
]


def __getattr__(name: str) -> Any:
    spec = _LAZY.get(name)
    if spec is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(spec[0], __name__)
    return getattr(module, spec[1])
