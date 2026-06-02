"""Probe Binance public REST reachability (direct or via configured proxy)."""

from __future__ import annotations

import argparse
import asyncio
import sys

try:
    from scripts.common import bootstrap_repo_path, configure_script_logging
except ModuleNotFoundError:  # pragma: no cover
    from common import bootstrap_repo_path, configure_script_logging

bootstrap_repo_path()

from bot.domain.config import load_settings
from bot.market.data import BinanceFuturesMarketData, MarketDataUnavailable
from bot.market.network_proxy import mask_proxy_url, resolve_proxy_url
from bot.market.rest import BinanceClientImpl

LOG = configure_script_logging("scripts.probe_binance_access")


async def _probe(*, proxy_url: str | None, trust_env: bool) -> int:
    effective = resolve_proxy_url(config_url=proxy_url, trust_env=trust_env)
    if effective:
        LOG.info("using proxy | url=%s", mask_proxy_url(effective))
    else:
        LOG.warning("no proxy configured — direct connection only")

    settings = load_settings()
    client = BinanceClientImpl(
        rest_timeout_seconds=min(float(settings.ws.rest_timeout_seconds), 30.0),
        futures_data_request_limit_per_5m=settings.runtime.futures_data_request_limit_per_5m,
        proxy_url=effective,
        trust_env=trust_env,
    )
    market = BinanceFuturesMarketData(binance_client=client)
    try:
        symbols = await market.fetch_exchange_symbols()
        LOG.info("binance reachable | symbols=%d", len(symbols))
        return 0
    except MarketDataUnavailable as exc:
        LOG.error(
            "binance unreachable | operation=%s detail=%s",
            exc.operation,
            exc.detail,
        )
        return 2
    finally:
        await market.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe Binance futures public REST")
    parser.add_argument(
        "--proxy",
        default="",
        help="Override proxy URL (socks5h://127.0.0.1:7890 or http://host:port)",
    )
    parser.add_argument(
        "--no-trust-env",
        action="store_true",
        help="Ignore HTTPS_PROXY / BINANCE_PROXY_URL environment variables",
    )
    parser.add_argument(
        "--try-local-ports",
        action="store_true",
        help="Try common local proxy ports when direct access fails",
    )
    args = parser.parse_args()
    proxy = str(args.proxy or "").strip() or None
    trust_env = not args.no_trust_env

    if not proxy and not trust_env:
        LOG.error("provide --proxy or allow env via default trust_env")
        raise SystemExit(1)

    code = asyncio.run(_probe(proxy_url=proxy, trust_env=trust_env))
    if code == 0 or not args.try_local_ports:
        raise SystemExit(code)

    candidates = (
        "socks5h://127.0.0.1:7890",
        "socks5h://127.0.0.1:7891",
        "socks5h://127.0.0.1:10808",
        "socks5h://127.0.0.1:1080",
        "socks5h://127.0.0.1:9050",
        "http://127.0.0.1:7890",
    )
    for candidate in candidates:
        LOG.info("retry with local candidate | url=%s", mask_proxy_url(candidate))
        code = asyncio.run(_probe(proxy_url=candidate, trust_env=False))
        if code == 0:
            LOG.info("working proxy found — set BINANCE_PROXY_URL=%s", candidate)
            raise SystemExit(0)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
