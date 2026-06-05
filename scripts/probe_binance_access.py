"""Probe Binance public REST reachability (direct or via configured proxy)."""

from __future__ import annotations

import argparse
import asyncio

try:
    from scripts.common import configure_script_logging
except ModuleNotFoundError:  # pragma: no cover
    from common import configure_script_logging

from bot.domain.config import NetworkConfig, load_settings
from bot.market.data import BinanceFuturesMarketData, MarketDataUnavailable
from bot.market.network_proxy import mask_proxy_url, resolve_proxy_url
from bot.market.proxy_bootstrap import probe_ws_handshake
from bot.market.rest_impl import BinanceClientImpl

LOG = configure_script_logging("scripts.probe_binance_access")


async def _probe_rest_network(net: NetworkConfig) -> int:
    settings = load_settings()
    client = BinanceClientImpl(
        rest_timeout_seconds=min(float(settings.ws.rest_timeout_seconds), 30.0),
        futures_data_request_limit_per_5m=settings.runtime.futures_data_request_limit_per_5m,
        network=net,
    )
    active = client._proxy_url
    if active:
        LOG.info("using proxy | url=%s", mask_proxy_url(active))
    else:
        LOG.warning("no proxy configured — direct connection only")

    market = BinanceFuturesMarketData(binance_client=client)
    try:
        symbols = await market.fetch_exchange_symbols()
        LOG.info("binance REST reachable | symbols=%d", len(symbols))
    except MarketDataUnavailable as exc:
        LOG.exception(
            "binance REST unreachable | operation=%s detail=%s",
            exc.operation,
            exc.detail,
        )
        return 2
    else:
        return 0
    finally:
        await market.close()


async def _probe_ws_network(net: NetworkConfig) -> int:
    active = net.proxy_url
    if active:
        LOG.info("using proxy | url=%s", mask_proxy_url(active))
    elif net.trust_env:
        env_proxy = resolve_proxy_url(trust_env=True)
        if env_proxy:
            LOG.info("using env proxy | url=%s", mask_proxy_url(env_proxy))
    else:
        LOG.warning("no proxy configured — direct connection only")

    if await probe_ws_handshake(proxy_url=net.proxy_url, trust_env=net.trust_env):
        LOG.info("binance WS reachable | url=wss://fstream.binance.com/ws")
        return 0
    LOG.error("binance WS unreachable | url=wss://fstream.binance.com/ws")
    return 2


async def _probe_network(net: NetworkConfig, *, mode: str) -> int:
    if mode == "ws":
        return await _probe_ws_network(net)
    if mode == "both":
        rest_code = await _probe_rest_network(net)
        ws_code = await _probe_ws_network(net)
        return 0 if rest_code == 0 and ws_code == 0 else max(rest_code, ws_code)
    return await _probe_rest_network(net)


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe Binance futures public REST/WS")
    parser.add_argument(
        "--proxy",
        default="",
        help="Override proxy URL (socks5://127.0.0.1:7890 or http://host:port)",
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
    parser.add_argument(
        "--all-configured",
        action="store_true",
        help="Probe every URL from config.toml / BINANCE_PROXY_URLS",
    )
    parser.add_argument(
        "--ws",
        action="store_true",
        help="Probe WebSocket handshake (wss://fstream.binance.com/ws) instead of REST",
    )
    parser.add_argument(
        "--both",
        action="store_true",
        help="Probe REST exchangeInfo and WS handshake (both must succeed)",
    )
    args = parser.parse_args()
    if args.ws and args.both:
        parser.error("use either --ws or --both, not both flags")
    probe_mode = "both" if args.both else ("ws" if args.ws else "rest")
    proxy = str(args.proxy or "").strip() or None
    trust_env = not args.no_trust_env
    settings = load_settings()

    if args.all_configured:
        urls = settings.network.effective_proxy_urls()
        if not urls and trust_env:
            env_only = resolve_proxy_url(config_url=None, trust_env=True)
            if env_only:
                urls = [env_only]
        if not urls:
            LOG.error("no proxy_urls in config — set [bot.network] or BINANCE_PROXY_URLS")
            raise SystemExit(1)
        any_ok = False
        last_code = 2
        for url in urls:
            LOG.info("probing configured endpoint | url=%s", mask_proxy_url(url))
            net = NetworkConfig(
                proxy_url=url,
                trust_env=False,
                failover_enabled=False,
            )
            last_code = asyncio.run(_probe_network(net, mode=probe_mode))
            any_ok = any_ok or last_code == 0
        raise SystemExit(0 if any_ok else last_code)

    if proxy:
        net = NetworkConfig(proxy_url=proxy, trust_env=False, failover_enabled=False)
    else:
        net = settings.network.model_copy(update={"trust_env": trust_env})

    if not proxy and not trust_env:
        LOG.error("provide --proxy or allow env via default trust_env")
        raise SystemExit(1)

    code = asyncio.run(_probe_network(net, mode=probe_mode))
    if code == 0 or not args.try_local_ports or probe_mode != "rest":
        raise SystemExit(code)

    candidates = (
        "socks5://127.0.0.1:7890",
        "socks5://127.0.0.1:7891",
        "socks5://127.0.0.1:10808",
        "socks5://127.0.0.1:1080",
        "socks5://127.0.0.1:9050",
        "http://127.0.0.1:7890",
    )
    for candidate in candidates:
        LOG.info("retry with local candidate | url=%s", mask_proxy_url(candidate))
        probe_net = NetworkConfig(proxy_url=candidate, trust_env=False, failover_enabled=False)
        code = asyncio.run(_probe_network(probe_net, mode=probe_mode))
        if code == 0:
            LOG.info("working proxy found — set BINANCE_PROXY_URL=%s", candidate)
            raise SystemExit(0)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
