# ruff: noqa: E402
from __future__ import annotations

import argparse
import asyncio
from typing import Sequence

try:
    from scripts.common import bootstrap_repo_path, configure_script_logging
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from common import bootstrap_repo_path, configure_script_logging

bootstrap_repo_path()

from bot.domain.config import load_settings
from bot.market.data import BinanceFuturesMarketData, MarketDataUnavailable
from bot.market.ws import FuturesWSManager


LOG = configure_script_logging("scripts.live_check_binance_api")
LIVE_CHECK_HTTP_TIMEOUT_SECONDS = 30.0  # seconds: cap live REST smoke checks
PUBLIC_FAPI_PATHS = {
    "/fapi/v1/exchangeInfo",
    "/fapi/v1/ticker/24hr",
    "/fapi/v1/ticker/bookTicker",
    "/fapi/v1/klines",
}


async def _wait_for_ws_warmup(ws_manager: FuturesWSManager, timeout_seconds: float) -> dict:
    """Poll until global ticker/mark/book caches are warm or timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    last: dict = {}
    while loop.time() < deadline:
        last = ws_manager.state_snapshot()
        book_ready = int(last.get("fresh_book_tickers") or 0) > 0
        ticker_ready = ws_manager.is_ticker_cache_warm() or int(last.get("fresh_tickers") or 0) > 0
        mark_ready = int(last.get("fresh_mark_prices") or 0) > 0
        if book_ready and ticker_ready and mark_ready:
            return last
        await asyncio.sleep(1.0)
    return last


async def _wait_for_mark_prices(ws_manager: FuturesWSManager, timeout_seconds: float) -> dict:
    """Extra poll window when !markPrice@arr is slow after connect."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    last: dict = {}
    while loop.time() < deadline:
        last = ws_manager.state_snapshot()
        if int(last.get("fresh_mark_prices") or 0) > 0:
            return last
        await asyncio.sleep(1.0)
    return last


async def _wait_for_market_reconnect(
    ws_manager: FuturesWSManager,
    timeout_seconds: float,
    *,
    min_connect_count: int = 2,
) -> dict:
    """Poll until the market endpoint reconnects after a forced close."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    last: dict = {}
    while loop.time() < deadline:
        last = ws_manager.state_snapshot()
        if int(last.get("market_connect_count") or 0) >= min_connect_count:
            age = float(last.get("market_last_message_age_seconds") or 999.0)
            if age <= 10.0:
                return last
        await asyncio.sleep(1.0)
    return last


def _assert_public_endpoint(endpoint: str) -> None:
    if endpoint not in PUBLIC_FAPI_PATHS:
        raise RuntimeError(f"Non-public Binance endpoint in live check: {endpoint}")


async def _run(
    symbols: Sequence[str], warmup_seconds: float, reconnect_wait_seconds: float
) -> None:
    settings = load_settings()
    # Create BinanceClient implementation
    from bot.market.rest import BinanceClientImpl

    binance_client = BinanceClientImpl(
        rest_timeout_seconds=min(
            float(settings.ws.rest_timeout_seconds),
            LIVE_CHECK_HTTP_TIMEOUT_SECONDS,
        ),
        futures_data_request_limit_per_5m=settings.runtime.futures_data_request_limit_per_5m,
    )
    client = BinanceFuturesMarketData(binance_client=binance_client)
    ws_manager = FuturesWSManager(client, settings.ws)
    try:
        _assert_public_endpoint("/fapi/v1/exchangeInfo")
        exchange_symbols = await client.fetch_exchange_symbols()
        _assert_public_endpoint("/fapi/v1/ticker/24hr")
        ticker_rows = await client.fetch_ticker_24h()
        _assert_public_endpoint("/fapi/v1/ticker/bookTicker")
        book_bid, book_ask = await client.fetch_book_ticker(symbols[0])
        _assert_public_endpoint("/fapi/v1/klines")
        klines_15m = await client.fetch_klines_cached(symbols[0], "15m", limit=64)
        LOG.info(
            "rest_checks_ok",
            exchange_symbols=len(exchange_symbols),
            ticker_rows=len(ticker_rows),
            symbol=symbols[0],
            bid=book_bid,
            ask=book_ask,
            kline_rows=klines_15m.height,
        )

        await ws_manager.start(list(symbols))
        connected = await ws_manager.wait_until_connected(timeout=30.0)
        if not connected:
            raise RuntimeError("ws_manager failed to connect within 30 seconds")

        before = await _wait_for_ws_warmup(ws_manager, warmup_seconds)
        if int(before.get("fresh_mark_prices") or 0) <= 0:
            before = await _wait_for_mark_prices(ws_manager, min(20.0, warmup_seconds))
        LOG.info("ws_snapshot_before_reconnect", snapshot=before)
        if not ws_manager.is_ticker_cache_warm() and int(before.get("fresh_tickers") or 0) <= 0:
            raise RuntimeError(f"ticker cache did not warm up: {before}")
        if int(before.get("fresh_mark_prices") or 0) <= 0:
            raise RuntimeError(f"fresh_mark_prices did not warm up: {before}")
        if int(before.get("fresh_book_tickers") or 0) <= 0:
            raise RuntimeError(f"fresh_book_tickers did not warm up: {before}")

        market_ws = ws_manager._ws_conns.get("market")
        if market_ws is None:
            raise RuntimeError("market ws connection is missing before forced reconnect")
        await market_ws.close()
        after = await _wait_for_market_reconnect(ws_manager, reconnect_wait_seconds)
        LOG.info("ws_snapshot_after_reconnect", snapshot=after)
        if int(after.get("market_connect_count") or 0) < 2:
            raise RuntimeError(f"market reconnect was not observed: {after}")
        if not ws_manager.is_ticker_cache_warm() and int(after.get("fresh_tickers") or 0) <= 0:
            raise RuntimeError(f"ticker cache was not restored after reconnect: {after}")
        if int(after.get("fresh_mark_prices") or 0) <= 0:
            raise RuntimeError(f"fresh_mark_prices were not restored after reconnect: {after}")
    finally:
        await ws_manager.stop()
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Live REST/WS Binance API smoke check")
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    parser.add_argument("--warmup-seconds", type=float, default=20.0)
    parser.add_argument("--reconnect-wait-seconds", type=float, default=20.0)
    args = parser.parse_args()
    try:
        asyncio.run(_run(args.symbols, args.warmup_seconds, args.reconnect_wait_seconds))
    except MarketDataUnavailable as exc:
        LOG.error(
            "live_binance_api_unavailable",
            operation=exc.operation,
            detail=exc.detail,
            symbol=exc.symbol,
        )
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
