"""Live Binance session hooks — skip when the runner region is geo-blocked."""

from __future__ import annotations

import asyncio
import os

import pytest

from bot.market.data import BinanceFuturesMarketData, MarketDataUnavailable

_GEO_SKIP_REASON: str | None = None


def _restricted_location_reason(exc: MarketDataUnavailable) -> str | None:
    text = f"{exc.operation} {exc.detail}".lower()
    if "restricted location" in text or "service unavailable from a restricted" in text:
        return "Binance public API blocked from this network (geo-restricted); live tests skipped"
    return None


async def _probe_binance_access() -> str | None:
    from bot.domain.config import load_settings
    from bot.market.rest import BinanceClientImpl

    settings = load_settings()
    binance_client = BinanceClientImpl(
        rest_timeout_seconds=30.0,
        futures_data_request_limit_per_5m=1200,
        proxy_url=settings.network.proxy_url,
        trust_env=settings.network.trust_env,
    )
    market = BinanceFuturesMarketData(binance_client=binance_client)
    try:
        await market.fetch_exchange_symbols()
    except MarketDataUnavailable as exc:
        return _restricted_location_reason(exc)
    finally:
        await market.close()
    return None


def _live_tests_requested(session: pytest.Session) -> bool:
    if os.environ.get("PYTEST_LIVE") != "1":
        return False
    args = [str(a) for a in session.config.args]
    if not args:
        return True
    return any("live" in arg for arg in args)


def pytest_sessionstart(session: pytest.Session) -> None:
    global _GEO_SKIP_REASON
    if not _live_tests_requested(session):
        return
    try:
        _GEO_SKIP_REASON = asyncio.run(_probe_binance_access())
    except Exception:
        _GEO_SKIP_REASON = None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if _GEO_SKIP_REASON is None:
        return
    skip = pytest.mark.skip(reason=_GEO_SKIP_REASON)
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
