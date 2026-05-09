# ruff: noqa: E402
from __future__ import annotations

import argparse
import asyncio
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

from common import (
    bootstrap_repo_path,
    configure_script_logging,
    load_symbols_from_run,
    resolve_symbols,
)

bootstrap_repo_path()

from bot.application.bot import SignalBot
from bot.config import load_settings
from bot.market_data import BinanceFuturesMarketData, MarketDataUnavailable
from bot.messaging import DeliveryResult
from bot.models import SymbolFrames, UniverseSymbol
from bot.telemetry import TelemetryStore


LOG = configure_script_logging("scripts.live_check_pipeline")


class FakeBroadcaster:
    async def preflight_check(self) -> None:
        return None

    async def send_html(
        self, text: str, *, reply_to_message_id: int | None = None
    ) -> DeliveryResult:
        return DeliveryResult(
            status="suppressed", message_id=None, reason="pipeline_check"
        )

    async def edit_html(self, message_id: int, text: str) -> None:
        return None

    async def close(self) -> None:
        return None


async def _safe_call(label: str, operation: Any, *, timeout: float) -> tuple[str, bool]:
    try:
        await asyncio.wait_for(operation(), timeout=timeout)
        return label, True
    except Exception as exc:
        LOG.warning("context_prefetch_failed", label=label, error=str(exc))
        return label, False


async def _wait_for(label: str, operation: Any, *, timeout: float) -> Any:
    try:
        return await asyncio.wait_for(operation(), timeout=timeout)
    except Exception as exc:
        raise RuntimeError(f"{label} failed or timed out: {exc}") from exc


async def _warm_public_context(
    client: BinanceFuturesMarketData,
    symbol: str,
    *,
    timeout: float,
    include_basis: bool,
) -> Counter[str]:
    operations = [
        _safe_call(
            "oi_change_1h",
            lambda: client.fetch_open_interest_change(symbol, period="1h"),
            timeout=timeout,
        ),
        _safe_call(
            "top_account_ls_ratio_1h",
            lambda: client.fetch_long_short_ratio(symbol, period="1h"),
            timeout=timeout,
        ),
        _safe_call(
            "top_position_ls_ratio_1h",
            lambda: client.fetch_top_position_ls_ratio(symbol, period="1h"),
            timeout=timeout,
        ),
        _safe_call(
            "global_ls_ratio_1h",
            lambda: client.fetch_global_ls_ratio(symbol, period="1h"),
            timeout=timeout,
        ),
        _safe_call(
            "taker_ratio_1h",
            lambda: client.fetch_taker_ratio(symbol, period="1h"),
            timeout=timeout,
        ),
        _safe_call(
            "funding_rate", lambda: client.fetch_funding_rate(symbol), timeout=timeout
        ),
        _safe_call(
            "funding_rate_history",
            lambda: client.fetch_funding_rate_history(symbol),
            timeout=timeout,
        ),
    ]
    if include_basis:
        operations.extend(
            [
                _safe_call(
                    "basis_1h",
                    lambda: client.fetch_basis(symbol, period="1h", limit=5),
                    timeout=timeout,
                ),
                _safe_call(
                    "basis_5m",
                    lambda: client.fetch_basis(symbol, period="5m", limit=5),
                    timeout=timeout,
                ),
            ]
        )
    results = await asyncio.gather(*operations, return_exceptions=False)
    return Counter(label if ok else f"{label}:failed" for label, ok in results)


def _build_universe_symbol(
    symbol: str,
    meta_map: dict[str, Any],
    ticker_map: dict[str, dict[str, Any]],
) -> UniverseSymbol | None:
    meta = meta_map.get(symbol)
    if meta is None:
        return None
    ticker = ticker_map.get(symbol, {})
    return UniverseSymbol(
        symbol=symbol,
        base_asset=meta.base_asset,
        quote_asset=meta.quote_asset,
        contract_type=meta.contract_type,
        status=meta.status,
        onboard_date_ms=meta.onboard_date_ms,
        quote_volume=float(ticker.get("quote_volume") or 0.0),
        price_change_pct=float(ticker.get("price_change_percent") or 0.0),
        last_price=float(ticker.get("last_price") or 0.0),
        shortlist_bucket="live_pipeline_check",
    )


async def _fetch_frames(
    client: BinanceFuturesMarketData,
    symbol: str,
    *,
    timeout: float,
) -> SymbolFrames:
    bid_price, ask_price = await _wait_for(
        "book_ticker",
        lambda: client.fetch_book_ticker(symbol),
        timeout=timeout,
    )
    df_1h, df_15m, df_5m, df_4h = await asyncio.gather(
        _wait_for(
            "klines_1h",
            lambda: client.fetch_klines_cached(symbol, "1h", limit=300),
            timeout=timeout,
        ),
        _wait_for(
            "klines_15m",
            lambda: client.fetch_klines_cached(symbol, "15m", limit=300),
            timeout=timeout,
        ),
        _wait_for(
            "klines_5m",
            lambda: client.fetch_klines_cached(symbol, "5m", limit=300),
            timeout=timeout,
        ),
        _wait_for(
            "klines_4h",
            lambda: client.fetch_klines_cached(symbol, "4h", limit=300),
            timeout=timeout,
        ),
    )
    return SymbolFrames(
        symbol=symbol,
        df_1h=df_1h,
        df_15m=df_15m,
        bid_price=bid_price,
        ask_price=ask_price,
        df_5m=df_5m,
        df_4h=df_4h,
    )


def _indicator_tail(result: Any) -> dict[str, float | None]:
    prepared = result.prepared
    if prepared is None:
        return {}

    def frame_value(frame: Any, column: str) -> float | None:
        try:
            if frame is None or frame.is_empty() or column not in frame.columns:
                return None
            value = frame.item(-1, column)
            return None if value is None else float(value)
        except Exception:
            return None

    return {
        "rsi15m": frame_value(prepared.work_15m, "rsi14"),
        "adx1h": frame_value(prepared.work_1h, "adx14"),
        "adx4h": frame_value(prepared.work_4h, "adx14"),
        "atr_pct": prepared.atr_pct,
        "spread_bps": prepared.spread_bps,
        "oi_change_pct": prepared.oi_change_pct,
        "ls_ratio": prepared.ls_ratio,
        "funding_rate": prepared.funding_rate,
    }


async def _run(
    symbols: Sequence[str],
    concurrency: int,
    warm_context: bool,
    include_basis: bool,
) -> None:
    os.environ.setdefault("BOT_DISABLE_HTTP_SERVERS", "1")
    settings = load_settings()
    client = BinanceFuturesMarketData(
        rest_timeout_seconds=settings.ws.rest_timeout_seconds,
        futures_data_request_limit_per_5m=settings.runtime.futures_data_request_limit_per_5m,
    )
    run_id = f"live_pipeline_check_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"
    telemetry = TelemetryStore(settings.telemetry_dir, run_id=run_id)
    bot = SignalBot(
        settings, market_data=client, broadcaster=FakeBroadcaster(), telemetry=telemetry
    )
    try:
        await bot._modern_repo.initialize()
        exchange_symbols = await client.fetch_exchange_symbols()
        ticker_rows = await client.fetch_ticker_24h()
        await client.fetch_premium_index_all()
        meta_map = {row.symbol: row for row in exchange_symbols}
        ticker_map = {
            str(row.get("symbol") or "").upper(): row
            for row in ticker_rows
            if isinstance(row, dict)
        }

        semaphore = asyncio.Semaphore(concurrency)
        all_candidates: dict[str, list[Any]] = {}
        rejection_reasons: Counter[str] = Counter()
        rejection_stages: Counter[str] = Counter()
        raw_hits_by_setup: Counter[str] = Counter()
        context_status: Counter[str] = Counter()
        indicator_samples: dict[str, dict[str, float | None]] = {}
        detector_runs = 0
        raw_hits = 0
        prepared_ok = 0
        failures: list[dict[str, Any]] = []

        async def analyze(symbol: str) -> None:
            nonlocal detector_runs, raw_hits, prepared_ok
            async with semaphore:
                item = _build_universe_symbol(symbol, meta_map, ticker_map)
                if item is None:
                    failures.append({"symbol": symbol, "stage": "metadata"})
                    return
                LOG.info("pipeline_symbol_start", symbol=symbol)
                if warm_context:
                    context_status.update(
                        await _warm_public_context(
                            client,
                            symbol,
                            timeout=max(3.0, float(settings.ws.rest_timeout_seconds)),
                            include_basis=include_basis,
                        )
                    )
                try:
                    frame_timeout = max(5.0, float(settings.ws.rest_timeout_seconds))
                    frames = await _fetch_frames(client, symbol, timeout=frame_timeout)
                    engine_timeout = max(
                        15.0,
                        float(
                            getattr(
                                bot._modern_engine,
                                "_strategy_timeout_seconds",
                                5.0,
                            )
                        )
                        * max(2, len(settings.setups.enabled_setup_ids())),
                    )
                    result = await asyncio.wait_for(
                        bot._run_modern_analysis(
                            item,
                            frames,
                            trigger="live_pipeline_check",
                            event_ts=datetime.now(UTC),
                            ws_enrichments=bot._ws_cache_enrichments(symbol),
                        ),
                        timeout=engine_timeout,
                    )
                except Exception as exc:
                    failures.append(
                        {
                            "symbol": symbol,
                            "stage": "analysis",
                            "error": str(exc),
                            "exception_type": type(exc).__name__,
                        }
                    )
                    return

                if result.prepared is not None:
                    prepared_ok += 1
                funnel = result.funnel or {}
                detector_runs += int(
                    funnel.get("detector_runs") or result.raw_setups or 0
                )
                raw_hits += int(funnel.get("raw_hits") or 0)
                raw_hits_by_setup.update(funnel.get("raw_hits_by_setup") or {})
                all_candidates[symbol] = list(result.candidates)
                indicator_samples[symbol] = _indicator_tail(result)
                LOG.info(
                    "pipeline_symbol_done",
                    symbol=symbol,
                    detector_runs=funnel.get("detector_runs"),
                    raw_hits=funnel.get("raw_hits"),
                    candidates=len(result.candidates),
                )
                for row in result.rejected:
                    rejection_stages.update([str(row.get("stage") or "unknown")])
                    rejection_reasons.update([str(row.get("reason") or "unknown")])

        await asyncio.gather(
            *(asyncio.create_task(analyze(symbol)) for symbol in symbols)
        )

        selected = bot._select_and_rank(
            all_candidates,
            max_signals=settings.runtime.max_signals_per_cycle,
        )
        candidate_count = sum(len(items) for items in all_candidates.values())
        LOG.info(
            "pipeline_surface_summary",
            symbols=len(symbols),
            prepared_ok=prepared_ok,
            detector_runs=detector_runs,
            raw_hits=raw_hits,
            candidates=candidate_count,
            dry_selected=len(selected),
            strategy_hits=raw_hits_by_setup.most_common(),
            rejection_stages=rejection_stages.most_common(12),
            rejection_reasons=rejection_reasons.most_common(20),
            context_status=context_status.most_common(20),
            telemetry_run_id=run_id,
        )
        if indicator_samples:
            LOG.info(
                "indicator_samples", samples=dict(list(indicator_samples.items())[:10])
            )
        if selected:
            LOG.info(
                "dry_selected_candidates",
                selected=[
                    {
                        "symbol": signal.symbol,
                        "setup_id": signal.setup_id,
                        "direction": signal.direction,
                        "score": signal.score,
                        "risk_reward": signal.risk_reward,
                        "spread_bps": signal.spread_bps,
                        "atr_pct": signal.atr_pct,
                    }
                    for signal in selected
                ],
            )
        if failures:
            LOG.warning("pipeline_failures", failures=failures[:20])
        if detector_runs <= 0 or prepared_ok <= 0:
            raise RuntimeError(
                "pipeline check did not execute any prepared detector runs"
            )
    finally:
        await bot.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Live prepare->strategy->confirmation->filters check without Telegram delivery"
    )
    parser.add_argument("--symbols", nargs="*", default=[])
    parser.add_argument("--symbols-from-run", default="20260502_110946_22548")
    parser.add_argument("--limit", type=int, default=24)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument(
        "--no-warm-context",
        action="store_true",
        help="Skip public REST context warmup for OI/L/S/funding/basis caches",
    )
    parser.add_argument(
        "--include-basis",
        action="store_true",
        help="Also warm /futures/data/basis. Disabled by default to avoid REST bursts.",
    )
    args = parser.parse_args()
    symbols = resolve_symbols(
        args_symbols=args.symbols,
        symbols_from_run=load_symbols_from_run(
            args.symbols_from_run, Path("data") / "bot" / "telemetry"
        ),
        fallback_symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
    )
    if args.limit > 0:
        symbols = symbols[: args.limit]
    try:
        asyncio.run(
            _run(
                symbols,
                args.concurrency,
                warm_context=not args.no_warm_context,
                include_basis=args.include_basis,
            )
        )
    except MarketDataUnavailable as exc:
        LOG.error(
            "live_pipeline_unavailable",
            operation=exc.operation,
            detail=exc.detail,
            symbol=exc.symbol,
        )
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
