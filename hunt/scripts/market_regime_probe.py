#!/usr/bin/env python3
"""One-shot market cross-section survey + calibrated hunt parameters."""

from __future__ import annotations

import asyncio
import json
import sys

from hunt_watch.bootstrap import bootstrap

bootstrap()

from hunt_watch.market_regime import refresh_market_regime

from engine.domain.config import load_settings
from engine.market.data import BinanceFuturesMarketData
from engine.market.rest_impl import BinanceClientImpl


async def main() -> None:
    settings = load_settings()
    client = BinanceFuturesMarketData(
        binance_client=BinanceClientImpl(
            rest_timeout_seconds=45.0,
            futures_data_request_limit_per_5m=settings.runtime.futures_data_request_limit_per_5m,
            proxy_url=settings.network.proxy_url,
            trust_env=settings.network.trust_env,
        ),
    )
    try:
        snap = await refresh_market_regime(client)
        print(json.dumps(snap.to_dict(), indent=2, default=str))
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
