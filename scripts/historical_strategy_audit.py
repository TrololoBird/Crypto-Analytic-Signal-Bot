from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import polars as pl

try:
    from scripts.common import bootstrap_repo_path, configure_script_logging
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from common import bootstrap_repo_path, configure_script_logging

from bot.runtime.errors import DEFENSIVE_EXC
from bot.delivery.contract import validate_signal_contract
from bot.domain.config import load_settings
from bot.domain.schemas import Signal, SymbolFrames, SymbolMeta, UniverseSymbol
from bot.engine import SignalEngine, StrategyRegistry
from bot.features.prepare import min_required_bars, prepare_symbol
from bot.market.data import (
    BinanceFuturesMarketData,
    _drop_incomplete_ohlcv_tail,
    _klines_to_frame,
)
from bot.runtime.delivery_orchestrator import DeliveryOrchestrator
from bot.setups.base import SetupParams
from bot.strategies import STRATEGY_CLASSES

LOG = configure_script_logging("scripts.historical_strategy_audit")

BINANCE_FAPI = "https://fapi.binance.com"


def _interval_ms(interval: str) -> int:
    mapping = {
        "1m": 60_000,
        "3m": 180_000,
        "5m": 300_000,
        "15m": 900_000,
        "30m": 1_800_000,
        "1h": 3_600_000,
        "2h": 7_200_000,
        "4h": 14_400_000,
        "6h": 21_600_000,
        "8h": 28_800_000,
        "12h": 43_200_000,
        "1d": 86_400_000,
    }
    try:
        return mapping[interval]
    except KeyError as exc:
        msg = f"unsupported interval: {interval}"
        raise ValueError(msg) from exc


async def _fetch_json(
    session: aiohttp.ClientSession,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    attempts: int = 4,
) -> Any:
    url = f"{BINANCE_FAPI}{path}"
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            async with session.get(url, params=params) as response:
                text = await response.text()
                if response.status >= 400:
                    msg = f"HTTP {response.status}: {text[:240]}"
                    raise RuntimeError(msg)
                return json.loads(text)
        except DEFENSIVE_EXC as exc:
            last_error = exc
            await asyncio.sleep(min(8.0, 0.75 * (2**attempt)))
    msg = f"Binance request failed: {path} {params}: {last_error!r}"
    raise RuntimeError(msg)


async def _fetch_klines_range(
    session: aiohttp.ClientSession,
    symbol: str,
    interval: str,
    *,
    start_ms: int,
    end_ms: int,
) -> pl.DataFrame:
    step = _interval_ms(interval)
    cursor = int(start_ms)
    rows: list[list[Any]] = []
    while cursor < end_ms:
        payload = await _fetch_json(
            session,
            "/fapi/v1/klines",
            params={
                "symbol": symbol,
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 1500,
            },
        )
        if not isinstance(payload, list) or not payload:
            break
        page_rows = [row for row in payload if isinstance(row, list) and len(row) >= 11]
        if not page_rows:
            break
        rows.extend(page_rows)
        last_open = int(page_rows[-1][0])
        next_cursor = last_open + step
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        await asyncio.sleep(0.04)
        if len(page_rows) < 1500:
            break
    if not rows:
        return pl.DataFrame()
    dedup: dict[int, list[Any]] = {}
    for row in rows:
        try:
            dedup[int(row[0])] = row
        except (TypeError, ValueError):
            continue
    ordered = [dedup[key] for key in sorted(dedup)]
    return _drop_incomplete_ohlcv_tail(_klines_to_frame(ordered), interval)


async def _exchange_meta_and_tickers(
    session: aiohttp.ClientSession,
) -> tuple[dict[str, SymbolMeta], dict[str, dict[str, Any]]]:
    exchange_info, tickers = await asyncio.gather(
        _fetch_json(session, "/fapi/v1/exchangeInfo"),
        _fetch_json(session, "/fapi/v1/ticker/24hr"),
    )
    meta: dict[str, SymbolMeta] = {}
    for item in exchange_info.get("symbols", []) if isinstance(exchange_info, dict) else []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").upper()
        if not symbol:
            continue
        meta[symbol] = SymbolMeta(
            symbol=symbol,
            base_asset=str(item.get("baseAsset") or ""),
            quote_asset=str(item.get("quoteAsset") or ""),
            contract_type=str(item.get("contractType") or ""),
            status=str(item.get("status") or ""),
            onboard_date_ms=int(item.get("onboardDate") or 0),
        )
    ticker_map: dict[str, dict[str, Any]] = {}
    for item in tickers if isinstance(tickers, list) else []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").upper()
        if not symbol:
            continue
        ticker_map[symbol] = {
            "symbol": symbol,
            "last_price": _safe_float(item.get("lastPrice")),
            "price_change_percent": _safe_float(item.get("priceChangePercent")),
            "quote_volume": _safe_float(item.get("quoteVolume")),
            "trade_count": int(_safe_float(item.get("count"))),
        }
    return meta, ticker_map


async def _fetch_history_bundle(
    session: aiohttp.ClientSession,
    symbol: str,
    *,
    days: int,
    warmup_days: int,
) -> dict[str, pl.DataFrame]:
    end_ms = int(time.time() * 1000)
    start_ms = int((datetime.now(UTC) - timedelta(days=days + warmup_days)).timestamp() * 1000)
    frames = await asyncio.gather(
        _fetch_klines_range(session, symbol, "15m", start_ms=start_ms, end_ms=end_ms),
        _fetch_klines_range(session, symbol, "1h", start_ms=start_ms, end_ms=end_ms),
        _fetch_klines_range(session, symbol, "4h", start_ms=start_ms, end_ms=end_ms),
        _fetch_klines_range(session, symbol, "5m", start_ms=start_ms, end_ms=end_ms),
    )
    return {"15m": frames[0], "1h": frames[1], "4h": frames[2], "5m": frames[3]}


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        numeric = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return numeric if math.isfinite(numeric) else default


def _liquidity_rank(symbol: str, ticker_map: dict[str, dict[str, Any]]) -> int | None:
    rows = [
        (candidate, _safe_float(row.get("quote_volume")))
        for candidate, row in ticker_map.items()
        if candidate.endswith("USDT")
    ]
    rows.sort(key=lambda item: item[1], reverse=True)
    for index, (candidate, _volume) in enumerate(rows, start=1):
        if candidate == symbol:
            return index
    return None


def _market_context_from_tickers(ticker_map: dict[str, dict[str, Any]]) -> dict[str, Any]:
    def change_pct(symbol: str) -> float:
        return _safe_float(ticker_map.get(symbol, {}).get("price_change_percent"))

    def bias(symbol: str) -> str:
        change = change_pct(symbol)
        if change > 2.0:
            return "uptrend"
        if change < -2.0:
            return "downtrend"
        return "neutral"

    btc_change = change_pct("BTCUSDT")
    eth_change = change_pct("ETHUSDT")
    alt_changes = [
        change_pct(symbol)
        for symbol in ticker_map
        if symbol.endswith("USDT") and symbol not in {"BTCUSDT", "ETHUSDT"}
    ]
    avg_alt_change = sum(alt_changes) / len(alt_changes) if alt_changes else 0.0
    dominance_24h = btc_change - eth_change
    if btc_change > 1.5 and dominance_24h >= 0.0:
        btc_phase = "markup"
    elif btc_change < -1.5 and dominance_24h >= 0.0:
        btc_phase = "decline"
    elif dominance_24h < 0.0 and btc_change <= 0.5:
        btc_phase = "accumulation"
    elif dominance_24h < 0.0 and btc_change > 0.5:
        btc_phase = "distribution"
    else:
        btc_phase = "sideways"
    return {
        "btc_bias": bias("BTCUSDT"),
        "eth_bias": bias("ETHUSDT"),
        "altcoin_season_index": max(0.0, min(100.0, 50.0 + (avg_alt_change - btc_change) * 5.0)),
        "btc_phase": btc_phase,
    }


def _universe_symbol(
    symbol: str,
    *,
    meta_map: dict[str, SymbolMeta],
    ticker_map: dict[str, dict[str, Any]],
    strategy_ids: tuple[str, ...],
) -> UniverseSymbol:
    meta = meta_map[symbol]
    ticker = ticker_map.get(symbol, {})
    return UniverseSymbol(
        symbol=symbol,
        base_asset=meta.base_asset,
        quote_asset=meta.quote_asset,
        contract_type=meta.contract_type,
        status=meta.status,
        onboard_date_ms=meta.onboard_date_ms,
        quote_volume=_safe_float(ticker.get("quote_volume")),
        price_change_pct=_safe_float(ticker.get("price_change_percent")),
        last_price=_safe_float(ticker.get("last_price")),
        trade_count_24h=int(_safe_float(ticker.get("trade_count"))),
        shortlist_bucket="historical_audit",
        shortlist_score=1.0,
        shortlist_reasons=("historical_surface_audit",),
        seed_source="historical_strategy_audit",
        liquidity_rank=_liquidity_rank(symbol, ticker_map),
        strategy_fits=strategy_ids,
    )


def _frame_to_time(frame: pl.DataFrame, anchor: datetime) -> pl.DataFrame:
    if frame.is_empty() or "time" not in frame.columns:
        return frame
    return frame.filter(pl.col("time") <= pl.lit(anchor))


def _anchor_indices(
    df_15m: pl.DataFrame,
    *,
    days: int,
    step_bars: int,
    max_windows: int,
) -> list[int]:
    if df_15m.is_empty() or "time" not in df_15m.columns:
        return []
    cutoff = datetime.now(UTC) - timedelta(days=days)
    eligible = df_15m.with_row_index("_hist_idx").filter(pl.col("time") >= pl.lit(cutoff))
    if eligible.is_empty():
        return []
    indices = [int(value) for value in eligible["_hist_idx"].to_list()]
    sampled = indices[:: max(1, int(step_bars))]
    if not sampled or sampled[-1] != indices[-1]:
        sampled.append(indices[-1])
    if max_windows > 0 and len(sampled) > max_windows:
        if max_windows == 1:
            return [sampled[-1]]
        stride = max(1, math.ceil(len(sampled) / max_windows))
        sampled = sampled[::stride]
        if sampled[-1] != indices[-1]:
            sampled.append(indices[-1])
        sampled = sampled[-max_windows:]
    return sorted(set(sampled))


def _attach_current_microstructure(
    prepared: Any, client: BinanceFuturesMarketData, symbol: str
) -> None:
    prepared.oi_current = client.get_cached_open_interest(symbol)
    prepared.oi_change_pct = client.get_cached_oi_change(symbol)
    prepared.ls_ratio = client.get_cached_ls_ratio(symbol)
    prepared.top_account_ls_ratio = prepared.ls_ratio
    prepared.top_position_ls_ratio = client.get_cached_top_position_ls_ratio(symbol)
    prepared.global_ls_ratio = client.get_cached_global_ls_ratio(symbol)
    prepared.taker_ratio = client.get_cached_taker_ratio(symbol)
    prepared.funding_rate = client.get_cached_funding_rate(symbol)
    prepared.funding_trend = client.get_cached_funding_trend(symbol)
    recent_extreme = client.get_cached_funding_recent_extreme(symbol)
    if recent_extreme is not None:
        prepared.funding_recent_extreme_rate = recent_extreme[0]
        prepared.funding_recent_extreme_age_hours = recent_extreme[1]


async def _current_book_context(
    client: BinanceFuturesMarketData,
    symbol: str,
) -> dict[str, float | None]:
    try:
        return await client._fetch_book_ticker_rest_detail(symbol)
    except DEFENSIVE_EXC as exc:
        LOG.debug("book context fetch failed", symbol=symbol, error=repr(exc))
        return {}


async def _warm_microstructure(client: BinanceFuturesMarketData, symbol: str) -> None:
    for fetch in (
        lambda: client.fetch_open_interest(symbol),
        lambda: client.fetch_open_interest_change(symbol, period="1h"),
        lambda: client.fetch_long_short_ratio(symbol, period="1h"),
        lambda: client.fetch_top_position_ls_ratio(symbol, period="1h"),
        lambda: client.fetch_global_ls_ratio(symbol, period="1h"),
        lambda: client.fetch_taker_ratio(symbol, period="1h"),
        lambda: client.fetch_funding_rate(symbol),
        lambda: client.fetch_funding_rate_history(symbol),
    ):
        try:
            await fetch()
        except DEFENSIVE_EXC as exc:
            LOG.debug("microstructure warmup failed", symbol=symbol, error=repr(exc))


def _contract_errors(signal: Signal) -> list[str]:
    return [f"{issue.field}.{issue.reason}" for issue in validate_signal_contract(signal)]


def _outcome_for_signal(
    signal: Signal,
    future_15m: pl.DataFrame,
    *,
    horizon_bars: int,
) -> str:
    if future_15m.is_empty():
        return "no_future"
    rows = future_15m.head(max(1, int(horizon_bars))).to_dicts()
    if not rows:
        return "no_future"
    direction = signal.direction.lower()
    tp1 = float(signal.take_profit_1)
    stop = float(signal.stop)
    entry_low = min(float(signal.entry_low), float(signal.entry_high))
    entry_high = max(float(signal.entry_low), float(signal.entry_high))
    entry_seen = False
    for row in rows:
        high = _safe_float(row.get("high"))
        low = _safe_float(row.get("low"))
        if high <= 0.0 or low <= 0.0:
            continue
        if not entry_seen:
            entry_seen = low <= entry_high and high >= entry_low
            if not entry_seen:
                continue
        if direction == "long":
            if low <= stop:
                return "sl_hit"
            if high >= tp1:
                return "tp1_hit"
        else:
            if high >= stop:
                return "sl_hit"
            if low <= tp1:
                return "tp1_hit"
    return "entry_no_exit" if entry_seen else "entry_not_touched"


def _summarize_strategy_counts(
    strategy_ids: tuple[str, ...],
    hits: Counter[str],
    rejects: Counter[str],
    skips: Counter[str],
    errors: Counter[str],
    outcomes: dict[str, Counter[str]],
) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for setup_id in strategy_ids:
        outcome_counts = outcomes.get(setup_id, Counter())
        closed = outcome_counts["tp1_hit"] + outcome_counts["sl_hit"]
        winrate = (outcome_counts["tp1_hit"] / closed) if closed else 0.0
        summary[setup_id] = {
            "hits": int(hits[setup_id]),
            "rejects": int(rejects[setup_id]),
            "skips": int(skips[setup_id]),
            "errors": int(errors[setup_id]),
            "outcomes": dict(outcome_counts),
            "closed_outcomes": int(closed),
            "winrate_tp1_vs_sl": round(winrate, 6),
        }
    return summary


async def run_audit(args: argparse.Namespace) -> dict[str, Any]:
    settings = load_settings()
    strategy_ids = tuple(strategy_class.setup_id for strategy_class in STRATEGY_CLASSES)
    registry = StrategyRegistry()
    for strategy_class in STRATEGY_CLASSES:
        registry.register(strategy_class(SetupParams(enabled=True), settings))
    engine = SignalEngine(registry=registry, settings=settings)
    minimums = min_required_bars(
        min_bars_15m=settings.filters.min_bars_15m,
        min_bars_1h=settings.filters.min_bars_1h,
        min_bars_4h=settings.filters.min_bars_4h,
    )

    timeout = aiohttp.ClientTimeout(total=45)
    connector = aiohttp.TCPConnector(limit=max(4, args.concurrency * 4))
    client = BinanceFuturesMarketData()
    try:
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            meta_map, ticker_map = await _exchange_meta_and_tickers(session)
            symbols = [symbol.upper() for symbol in args.symbols if symbol.upper() in meta_map]
            if not symbols:
                msg = "no requested symbols are available on Binance futures"
                raise RuntimeError(msg)

            hits: Counter[str] = Counter()
            rejects: Counter[str] = Counter()
            skips: Counter[str] = Counter()
            errors: Counter[str] = Counter()
            reject_reasons: Counter[str] = Counter()
            skip_reasons: Counter[str] = Counter()
            error_reasons: Counter[str] = Counter()
            reject_reasons_by_strategy: dict[str, Counter[str]] = defaultdict(Counter)
            skip_reasons_by_strategy: dict[str, Counter[str]] = defaultdict(Counter)
            error_reasons_by_strategy: dict[str, Counter[str]] = defaultdict(Counter)
            contract_failures: Counter[str] = Counter()
            contract_failure_examples: list[dict[str, Any]] = []
            outcomes: dict[str, Counter[str]] = defaultdict(Counter)
            gate_checked: Counter[str] = Counter()
            gate_passed: Counter[str] = Counter()
            gate_rejected: Counter[str] = Counter()
            gate_reasons: Counter[str] = Counter()
            gate_outcomes: dict[str, Counter[str]] = defaultdict(Counter)
            windows_total = 0
            prepared_ok = 0
            prepared_failed = 0
            symbol_windows: dict[str, int] = {}
            symbol_failures: list[dict[str, Any]] = []

            semaphore = asyncio.Semaphore(max(1, int(args.concurrency)))

            async def analyze_symbol(symbol: str) -> None:
                nonlocal windows_total, prepared_ok, prepared_failed
                async with semaphore:
                    await _warm_microstructure(client, symbol)
                    book_context = await _current_book_context(client, symbol)
                    history = await _fetch_history_bundle(
                        session,
                        symbol,
                        days=int(args.days),
                        warmup_days=int(args.warmup_days),
                    )
                    df_15m = history["15m"]
                    if df_15m.is_empty():
                        symbol_failures.append({"symbol": symbol, "reason": "empty_15m_history"})
                        return
                    indices = _anchor_indices(
                        df_15m,
                        days=int(args.days),
                        step_bars=int(args.window_step_bars),
                        max_windows=int(args.max_windows_per_symbol),
                    )
                    symbol_windows[symbol] = len(indices)
                    universe = _universe_symbol(
                        symbol,
                        meta_map=meta_map,
                        ticker_map=ticker_map,
                        strategy_ids=strategy_ids,
                    )
                    for anchor_idx in indices:
                        windows_total += 1
                        anchor_time = df_15m.item(anchor_idx, "time")
                        if not isinstance(anchor_time, datetime):
                            prepared_failed += 1
                            continue
                        frame_15m = df_15m.head(anchor_idx + 1).tail(600)
                        frame_1h = _frame_to_time(history["1h"], anchor_time).tail(600)
                        frame_4h = _frame_to_time(history["4h"], anchor_time).tail(600)
                        frame_5m = _frame_to_time(history["5m"], anchor_time).tail(400)
                        frames = SymbolFrames(
                            symbol=symbol,
                            df_1h=frame_1h,
                            df_15m=frame_15m,
                            df_4h=frame_4h,
                            df_5m=frame_5m,
                            bid_price=book_context.get("bid_price"),
                            ask_price=book_context.get("ask_price"),
                            bid_qty=book_context.get("bid_qty"),
                            ask_qty=book_context.get("ask_qty"),
                        )
                        prepared = prepare_symbol(
                            universe,
                            frames,
                            minimums=minimums,
                            settings=settings,
                        )
                        if prepared is None:
                            prepared_failed += 1
                            continue
                        prepared_ok += 1
                        _attach_current_microstructure(prepared, client, symbol)
                        for key, value in _market_context_from_tickers(ticker_map).items():
                            setattr(prepared, key, value)
                        results = await engine.calculate_all(prepared)
                        future = df_15m.slice(anchor_idx + 1, int(args.outcome_horizon_bars))
                        for result in results:
                            setup_id = result.setup_id
                            decision = result.decision
                            if result.signal is not None:
                                hits[setup_id] += 1
                                outcome = _outcome_for_signal(
                                    result.signal,
                                    future,
                                    horizon_bars=int(args.outcome_horizon_bars),
                                )
                                contract_errors = _contract_errors(result.signal)
                                if contract_errors:
                                    contract_failures[setup_id] += 1
                                    if len(contract_failure_examples) < 20:
                                        contract_failure_examples.append(
                                            {
                                                "symbol": symbol,
                                                "anchor_time": anchor_time.isoformat(),
                                                "setup_id": setup_id,
                                                "direction": result.signal.direction,
                                                "entry_low": result.signal.entry_low,
                                                "entry_high": result.signal.entry_high,
                                                "stop": result.signal.stop,
                                                "tp1": result.signal.take_profit_1,
                                                "tp2": result.signal.take_profit_2,
                                                "tp3": result.signal.take_profit_3,
                                                "errors": contract_errors,
                                            }
                                        )
                                else:
                                    gate_checked[setup_id] += 1
                                    gate_ok, confirmations, gate_details = (
                                        DeliveryOrchestrator._hard_confluence_gate(
                                            result.signal,
                                            prepared,
                                        )
                                    )
                                    if gate_ok:
                                        gate_passed[setup_id] += 1
                                        gate_outcomes[setup_id][outcome] += 1
                                    else:
                                        gate_rejected[setup_id] += 1
                                        confirmed = int(gate_details.get("confirmed") or 0)
                                        gate_reasons[
                                            f"{confirmed}_of_{gate_details.get('required', 3)}"
                                        ] += 1
                                        for name, value in confirmations.items():
                                            if not value:
                                                gate_reasons[f"missing_{name}"] += 1
                                outcomes[setup_id][outcome] += 1
                            elif decision is not None and decision.is_skip:
                                skips[setup_id] += 1
                                skip_reasons[decision.reason_code] += 1
                                skip_reasons_by_strategy[setup_id][decision.reason_code] += 1
                            elif decision is not None and decision.is_error:
                                errors[setup_id] += 1
                                error_reasons[decision.reason_code] += 1
                                error_reasons_by_strategy[setup_id][decision.reason_code] += 1
                            else:
                                rejects[setup_id] += 1
                                reason = (
                                    decision.reason_code
                                    if decision is not None
                                    else result.metadata.get("reason", "pattern.no_raw_hit")
                                )
                                reject_reasons[str(reason)] += 1
                                reject_reasons_by_strategy[setup_id][str(reason)] += 1

            await asyncio.gather(*(analyze_symbol(symbol) for symbol in symbols))
    finally:
        await client.close()

    zero_signal = [setup_id for setup_id in strategy_ids if hits[setup_id] == 0]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "days": int(args.days),
        "warmup_days": int(args.warmup_days),
        "symbols": symbols,
        "registered_strategies": list(strategy_ids),
        "registered_count": len(strategy_ids),
        "windows_total": windows_total,
        "prepared_ok": prepared_ok,
        "prepared_failed": prepared_failed,
        "symbol_windows": symbol_windows,
        "symbol_failures": symbol_failures,
        "detector_runs_estimated": prepared_ok * len(strategy_ids),
        "hit_strategy_count": len([setup_id for setup_id in strategy_ids if hits[setup_id] > 0]),
        "zero_signal_strategies": zero_signal,
        "hit_counts": dict(hits),
        "reject_counts": dict(rejects),
        "skip_counts": dict(skips),
        "error_counts": dict(errors),
        "top_reject_reasons": reject_reasons.most_common(30),
        "top_skip_reasons": skip_reasons.most_common(20),
        "top_error_reasons": error_reasons.most_common(20),
        "reject_reasons_by_strategy": {
            setup_id: counter.most_common(10)
            for setup_id, counter in sorted(reject_reasons_by_strategy.items())
        },
        "skip_reasons_by_strategy": {
            setup_id: counter.most_common(10)
            for setup_id, counter in sorted(skip_reasons_by_strategy.items())
        },
        "error_reasons_by_strategy": {
            setup_id: counter.most_common(10)
            for setup_id, counter in sorted(error_reasons_by_strategy.items())
        },
        "contract_failures": dict(contract_failures),
        "contract_failure_examples": contract_failure_examples,
        "delivery_gate": {
            "checked": int(sum(gate_checked.values())),
            "passed": int(sum(gate_passed.values())),
            "rejected": int(sum(gate_rejected.values())),
            "reject_reasons": gate_reasons.most_common(20),
            "pass_counts": dict(gate_passed),
            "reject_counts": dict(gate_rejected),
            "outcomes": {
                setup_id: dict(counter) for setup_id, counter in sorted(gate_outcomes.items())
            },
            "summary": _summarize_strategy_counts(
                strategy_ids,
                gate_passed,
                gate_rejected,
                Counter(),
                Counter(),
                gate_outcomes,
            ),
        },
        "strategy_summary": _summarize_strategy_counts(
            strategy_ids,
            hits,
            rejects,
            skips,
            errors,
            outcomes,
        ),
        "notes": [
            "Historical audit replays closed Binance Futures klines in rolling windows.",
            (
                "Current microstructure snapshot is attached only to exercise "
                "OI/funding-aware detectors; live delivery gate still uses current values."
            ),
            (
                "Outcome model is conservative: if TP1 and SL are touched in the same "
                "future candle, SL is counted first."
            ),
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay all registered strategies over historical Binance futures klines."
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"],
    )
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--warmup-days", type=int, default=45)
    parser.add_argument("--window-step-bars", type=int, default=24)
    parser.add_argument("--max-windows-per-symbol", type=int, default=48)
    parser.add_argument("--outcome-horizon-bars", type=int, default=96)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--write-json", type=Path, default=None)
    parser.add_argument("--require-registered", type=int, default=38)
    parser.add_argument("--require-no-zero-signals", action="store_true")
    parser.add_argument("--require-contract-clean", action="store_true")
    return parser.parse_args()


async def main_async() -> int:
    args = parse_args()
    summary = await run_audit(args)
    if args.write_json is not None:
        args.write_json.parent.mkdir(parents=True, exist_ok=True)
        args.write_json.write_text(
            json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )
        LOG.info("historical_strategy_audit_json_written", path=str(args.write_json))

    print(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False))

    failures: list[str] = []
    if int(summary["registered_count"]) != int(args.require_registered):
        failures.append(
            f"registered_count={summary['registered_count']} expected={args.require_registered}"
        )
    if args.require_no_zero_signals and summary["zero_signal_strategies"]:
        failures.append("zero_signal_strategies=" + ",".join(summary["zero_signal_strategies"]))
    if args.require_contract_clean and summary["contract_failures"]:
        failures.append(
            "contract_failures=" + json.dumps(summary["contract_failures"], sort_keys=True)
        )
    if failures:
        for failure in failures:
            LOG.error("historical_strategy_audit_gate_failed", failure=failure)
        return 1
    return 0


def main() -> int:
    bootstrap_repo_path()
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
