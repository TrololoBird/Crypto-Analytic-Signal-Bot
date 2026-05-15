# ruff: noqa: E402
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from scripts.common import (
        bootstrap_repo_path,
        configure_script_logging,
        load_symbols_from_run,
        resolve_symbols,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from common import (
        bootstrap_repo_path,
        configure_script_logging,
        load_symbols_from_run,
        resolve_symbols,
    )

bootstrap_repo_path()

from bot.domain.config import load_settings
from bot.core.engine import SignalEngine, StrategyRegistry
from bot.features import min_required_bars, prepare_symbol
from bot.market_data import BinanceFuturesMarketData, MarketDataUnavailable
from bot.domain.schemas import SymbolFrames, UniverseSymbol
from bot.setup_base import SetupParams
from bot.strategies import STRATEGY_CLASSES


LOG = configure_script_logging("scripts.live_check_strategies")


async def _build_prepared(
    client: BinanceFuturesMarketData,
    settings: Any,
    minimums: dict[str, int],
    meta_map: dict[str, Any],
    ticker_map: dict[str, dict[str, Any]],
    symbol: str,
):
    meta = meta_map.get(symbol)
    if meta is None:
        return None
    ticker = ticker_map.get(symbol, {})
    item = UniverseSymbol(
        symbol=symbol,
        base_asset=meta.base_asset,
        quote_asset=meta.quote_asset,
        contract_type=meta.contract_type,
        status=meta.status,
        onboard_date_ms=meta.onboard_date_ms,
        quote_volume=float(ticker.get("quote_volume") or 0.0),
        price_change_pct=float(ticker.get("price_change_percent") or 0.0),
        last_price=float(ticker.get("last_price") or 0.0),
        shortlist_bucket="",
    )
    frames = SymbolFrames(
        symbol=symbol,
        df_1h=await client.fetch_klines_cached(symbol, "1h", limit=300),
        df_15m=await client.fetch_klines_cached(symbol, "15m", limit=300),
        bid_price=None,
        ask_price=None,
        df_5m=await client.fetch_klines_cached(symbol, "5m", limit=300),
        df_4h=await client.fetch_klines_cached(symbol, "4h", limit=300),
    )
    try:
        book_context = await client._fetch_book_ticker_rest_detail(symbol)
        frames.bid_price = book_context.get("bid_price")
        frames.ask_price = book_context.get("ask_price")
        frames.bid_qty = book_context.get("bid_qty")
        frames.ask_qty = book_context.get("ask_qty")
    except Exception:
        pass
    for fetch in (
        lambda: client.fetch_open_interest(symbol),
        lambda: client.fetch_open_interest_change(symbol, period="1h"),
        lambda: client.fetch_long_short_ratio(symbol, period="1h"),
        lambda: client.fetch_top_position_ls_ratio(symbol, period="1h"),
        lambda: client.fetch_global_ls_ratio(symbol, period="1h"),
        lambda: client.fetch_taker_ratio(symbol, period="1h"),
        lambda: client.fetch_funding_rate(symbol),
    ):
        try:
            await fetch()
        except Exception:
            continue
    prepared = prepare_symbol(item, frames, minimums=minimums, settings=settings)
    if prepared is None:
        return None
    prepared.oi_current = client.get_cached_open_interest(symbol)
    prepared.oi_change_pct = client.get_cached_oi_change(symbol)
    prepared.ls_ratio = client.get_cached_ls_ratio(symbol)
    prepared.top_account_ls_ratio = prepared.ls_ratio
    prepared.top_position_ls_ratio = client.get_cached_top_position_ls_ratio(symbol)
    prepared.global_ls_ratio = client.get_cached_global_ls_ratio(symbol)
    prepared.taker_ratio = client.get_cached_taker_ratio(symbol)
    prepared.funding_rate = client.get_cached_funding_rate(symbol)
    prepared.funding_trend = client.get_cached_funding_trend(symbol)
    return prepared


def _top_volume_symbols(
    *,
    ticker_rows: list[dict[str, Any]],
    meta_map: dict[str, Any],
    limit: int,
) -> list[str]:
    rows: list[tuple[str, float]] = []
    for row in ticker_rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        meta = meta_map.get(symbol)
        if meta is None or getattr(meta, "status", "") != "TRADING":
            continue
        if getattr(meta, "quote_asset", "") != "USDT":
            continue
        try:
            quote_volume = float(row.get("quote_volume") or 0.0)
        except (TypeError, ValueError):
            quote_volume = 0.0
        if quote_volume > 0.0:
            rows.append((symbol, quote_volume))
    rows.sort(key=lambda item: item[1], reverse=True)
    return [symbol for symbol, _volume in rows[: max(1, int(limit or 30))]]


async def _run(symbols: list[str], concurrency: int, limit: int) -> None:
    settings = load_settings()
    minimums = min_required_bars(
        min_bars_15m=settings.filters.min_bars_15m,
        min_bars_1h=settings.filters.min_bars_1h,
        min_bars_4h=settings.filters.min_bars_4h,
    )
    registry = StrategyRegistry()
    for strategy_class in STRATEGY_CLASSES:
        registry.register(strategy_class(SetupParams(enabled=True), settings))
    engine = SignalEngine(registry, settings)
    client = BinanceFuturesMarketData(
        rest_timeout_seconds=settings.ws.rest_timeout_seconds,
        futures_data_request_limit_per_5m=settings.runtime.futures_data_request_limit_per_5m,
    )
    try:
        exchange_symbols = await client.fetch_exchange_symbols()
        ticker_rows = await client.fetch_ticker_24h()
        meta_map = {row.symbol: row for row in exchange_symbols}
        ticker_map = {
            str(row.get("symbol") or "").upper(): row
            for row in ticker_rows
            if isinstance(row, dict)
        }
        if not symbols:
            symbols = _top_volume_symbols(
                ticker_rows=ticker_rows,
                meta_map=meta_map,
                limit=limit,
            )
            LOG.info("symbols_top_volume_used", symbols=symbols)

        hits_by_setup: Counter[str] = Counter()
        errors_by_setup: Counter[str] = Counter()
        rejects_by_setup: Counter[str] = Counter()
        reject_reasons: Counter[str] = Counter()
        detector_runs = 0
        prepared_ok = 0
        failures: list[dict[str, Any]] = []
        semaphore = asyncio.Semaphore(concurrency)

        async def _analyze(symbol: str) -> None:
            nonlocal detector_runs, prepared_ok
            async with semaphore:
                prepared = await _build_prepared(
                    client,
                    settings,
                    minimums,
                    meta_map,
                    ticker_map,
                    symbol,
                )
                if prepared is None:
                    failures.append(
                        {
                            "symbol": symbol,
                            "stage": "prepare",
                            "error": "prepare_symbol returned None",
                        }
                    )
                    return
                prepared_ok += 1
                results = await engine.calculate_all(prepared)
                detector_runs += len(results)
                for result in results:
                    setup_id = str(
                        result.setup_id
                        or result.metadata.get("setup_id")
                        or getattr(result.signal, "setup_id", "unknown")
                    )
                    decision = result.decision
                    if decision is not None and decision.is_reject:
                        rejects_by_setup.update([setup_id])
                        reject_reasons.update([decision.reason_code])
                    if result.error:
                        errors_by_setup.update([setup_id])
                    elif result.signal is not None:
                        hits_by_setup.update([result.signal.setup_id])

        await asyncio.gather(*[asyncio.create_task(_analyze(symbol)) for symbol in symbols])
        LOG.info(
            "strategy_surface_summary",
            symbols=len(symbols),
            prepared_ok=prepared_ok,
            detector_runs=detector_runs,
            strategy_hits=hits_by_setup.most_common(),
            strategy_errors=errors_by_setup.most_common(),
            strategy_rejects=rejects_by_setup.most_common(20),
            strategy_reject_reasons=reject_reasons.most_common(15),
        )
        if prepared_ok <= 0:
            raise RuntimeError("no symbols prepared from live Binance data")
        if failures:
            LOG.warning("strategy_prepare_failures", failures=failures[:20])
        if errors_by_setup:
            raise RuntimeError(f"strategy errors detected: {errors_by_setup.most_common()}")
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Live strategy detector-surface review")
    parser.add_argument("--symbols", nargs="*", default=[])
    parser.add_argument("--symbols-from-run", default="20260421_215817_70948")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--concurrency", type=int, default=2)
    args = parser.parse_args()
    fallback_symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    symbols = resolve_symbols(
        args_symbols=args.symbols,
        symbols_from_run=load_symbols_from_run(
            args.symbols_from_run, Path("data") / "bot" / "telemetry"
        ),
        fallback_symbols=fallback_symbols,
    )
    explicit_symbols = bool(args.symbols)
    if symbols == fallback_symbols and not explicit_symbols and args.limit > len(fallback_symbols):
        LOG.info("symbols_fallback_used", symbols=symbols)
        symbols = []
    elif args.limit > 0:
        symbols = symbols[: args.limit]
    try:
        asyncio.run(_run(symbols, args.concurrency, args.limit))
    except MarketDataUnavailable as exc:
        LOG.error(
            "live_strategies_unavailable",
            operation=exc.operation,
            detail=exc.detail,
            symbol=exc.symbol,
        )
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
