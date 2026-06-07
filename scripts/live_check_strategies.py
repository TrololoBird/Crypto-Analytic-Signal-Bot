from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

try:
    from scripts.common import (
        configure_script_logging,
        load_symbols_from_run,
        resolve_symbols,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from common import (
        configure_script_logging,
        load_symbols_from_run,
        resolve_symbols,
    )

from bot.delivery.confluence import ConfluenceEngine
from bot.delivery.contract import signal_contract_row, validate_signal_contract
from bot.domain.config import load_settings
from bot.domain.schemas import SymbolFrames, UniverseSymbol
from bot.engine import SignalEngine, StrategyRegistry
from bot.features.prepare import min_required_bars, prepare_symbol
from bot.market.data import BinanceFuturesMarketData, MarketDataUnavailable
from bot.market.rest_impl import BinanceClientImpl
from bot.market.universe import strategy_fits_for_market_row
from bot.runtime.errors import DEFENSIVE_EXC
from bot.setups.base import SetupParams
from bot.strategies import STRATEGY_CLASSES

LOG = configure_script_logging("scripts.live_check_strategies")

LIVE_CHECK_HTTP_TIMEOUT_SECONDS = 30.0  # seconds: cap live REST smoke checks


async def _build_prepared(
    client: BinanceFuturesMarketData,
    settings: Any,
    minimums: dict[str, int],
    meta_map: dict[str, Any],
    ticker_map: dict[str, dict[str, Any]],
    market_context: dict[str, Any],
    symbol: str,
):
    meta = meta_map.get(symbol)
    if meta is None:
        return None
    ticker = ticker_map.get(symbol, {})
    liquidity_rank = _liquidity_rank(symbol, ticker_rows=list(ticker_map.values()))
    market_row = {
        "symbol": symbol,
        "base_asset": meta.base_asset,
        "quote_asset": meta.quote_asset,
        "contract_type": meta.contract_type,
        "status": meta.status,
        "onboard_date_ms": meta.onboard_date_ms,
        "quote_volume": float(ticker.get("quote_volume") or 0.0),
        "price_change_percent": float(ticker.get("price_change_percent") or 0.0),
        "price_change_pct": float(ticker.get("price_change_percent") or 0.0),
        "last_price": float(ticker.get("last_price") or 0.0),
        "trade_count": int(float(ticker.get("trade_count") or 0.0)),
    }
    item = UniverseSymbol(
        symbol=symbol,
        base_asset=meta.base_asset,
        quote_asset=meta.quote_asset,
        contract_type=meta.contract_type,
        status=meta.status,
        onboard_date_ms=meta.onboard_date_ms,
        quote_volume=float(market_row["quote_volume"]),
        price_change_pct=float(market_row["price_change_pct"]),
        last_price=float(market_row["last_price"]),
        shortlist_bucket="",
        seed_source="live_check_strategies",
        liquidity_rank=liquidity_rank,
        strategy_fits=strategy_fits_for_market_row(
            market_row,
            settings=settings,
            liquidity_rank=liquidity_rank,
        ),
    )
    frames = SymbolFrames(
        symbol=symbol,
        df_1h=await client.fetch_klines_cached(symbol, "1h", limit=500),
        df_15m=await client.fetch_klines_cached(symbol, "15m", limit=500),
        bid_price=None,
        ask_price=None,
        df_5m=await client.fetch_klines_cached(symbol, "5m", limit=300),
        df_4h=await client.fetch_klines_cached(symbol, "4h", limit=500),
    )
    try:
        book_context = await client._fetch_book_ticker_rest_detail(symbol)
        frames.bid_price = book_context.get("bid_price")
        frames.ask_price = book_context.get("ask_price")
        frames.bid_qty = book_context.get("bid_qty")
        frames.ask_qty = book_context.get("ask_qty")
    except DEFENSIVE_EXC as exc:
        LOG.debug("book ticker enrichment failed", symbol=symbol, error=repr(exc))
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
            LOG.debug("microstructure enrichment failed", symbol=symbol, error=repr(exc))
            continue
    prepared = prepare_symbol(item, frames, minimums=minimums, settings=settings)
    if prepared is None:
        return None
    try:
        premium_rows = await client.fetch_premium_index_all()
        premium = premium_rows.get(symbol, {})
    except DEFENSIVE_EXC as exc:
        LOG.debug("premium index enrichment failed", symbol=symbol, error=repr(exc))
        premium = {}
    mark_price = premium.get("mark_price")
    basis_pct = premium.get("basis_pct")
    if isinstance(mark_price, int | float) and float(mark_price) > 0.0:
        prepared.mark_price = float(mark_price)
    if isinstance(basis_pct, int | float):
        prepared.basis_pct = float(basis_pct)
    prepared.oi_current = client.get_cached_open_interest(symbol)
    prepared.oi_change_pct = client.get_cached_oi_change(symbol)
    prepared.ls_ratio = client.get_cached_ls_ratio(symbol)
    prepared.top_account_ls_ratio = prepared.ls_ratio
    prepared.top_position_ls_ratio = client.get_cached_top_position_ls_ratio(symbol)
    prepared.global_ls_ratio = client.get_cached_global_ls_ratio(symbol)
    prepared.taker_ratio = client.get_cached_taker_ratio(symbol)
    prepared.funding_rate = client.get_cached_funding_rate(symbol)
    prepared.funding_trend = client.get_cached_funding_trend(symbol)
    funding_recent_extreme = client.get_cached_funding_recent_extreme(symbol)
    if funding_recent_extreme is not None:
        prepared.funding_recent_extreme_rate = funding_recent_extreme[0]
        prepared.funding_recent_extreme_age_hours = funding_recent_extreme[1]
    for key in ("btc_bias", "eth_bias", "altcoin_season_index", "btc_phase"):
        if key in market_context:
            setattr(prepared, key, market_context[key])
    return prepared


def _liquidity_rank(symbol: str, *, ticker_rows: list[dict[str, Any]]) -> int | None:
    ranked: list[tuple[str, float]] = []
    for row in ticker_rows:
        candidate = str(row.get("symbol") or "").upper()
        if not candidate.endswith("USDT"):
            continue
        try:
            volume = float(row.get("quote_volume") or 0.0)
        except TypeError, ValueError:
            volume = 0.0
        ranked.append((candidate, volume))
    ranked.sort(key=lambda item: item[1], reverse=True)
    for index, (candidate, _volume) in enumerate(ranked, start=1):
        if candidate == symbol:
            return index
    return None


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
        except TypeError, ValueError:
            quote_volume = 0.0
        if quote_volume > 0.0:
            rows.append((symbol, quote_volume))
    rows.sort(key=lambda item: item[1], reverse=True)
    return [symbol for symbol, _volume in rows[: max(1, int(limit or 30))]]


def _market_context_from_tickers(ticker_map: dict[str, dict[str, Any]]) -> dict[str, Any]:
    def change_pct(symbol: str) -> float:
        row = ticker_map.get(symbol, {})
        try:
            return float(row.get("price_change_percent") or 0.0)
        except TypeError, ValueError:
            return 0.0

    def bias(symbol: str) -> str:
        change = change_pct(symbol)
        if change > 2.0:
            return "uptrend"
        if change < -2.0:
            return "downtrend"
        return "neutral"

    btc_change = change_pct("BTCUSDT")
    alt_changes = [
        change_pct(symbol)
        for symbol in ticker_map
        if symbol.endswith("USDT") and symbol not in {"BTCUSDT", "ETHUSDT"}
    ]
    if alt_changes:
        avg_alt_change = sum(alt_changes) / len(alt_changes)
        alt_index = max(0.0, min(100.0, 50.0 + (avg_alt_change - btc_change) * 5.0))
    else:
        alt_index = 50.0
    eth_change = change_pct("ETHUSDT")
    dominance_24h = btc_change - eth_change
    if btc_change > 1.5 and dominance_24h >= 0:
        btc_phase = "markup"
    elif btc_change < -1.5 and dominance_24h >= 0:
        btc_phase = "decline"
    elif dominance_24h < 0 and btc_change <= 0.5:
        btc_phase = "accumulation"
    elif dominance_24h < 0 and btc_change > 0.5:
        btc_phase = "distribution"
    else:
        btc_phase = "sideways"
    return {
        "btc_bias": bias("BTCUSDT"),
        "eth_bias": bias("ETHUSDT"),
        "altcoin_season_index": alt_index,
        "btc_phase": btc_phase,
    }


def _parse_strategy_filter(raw: list[str] | None) -> tuple[str, ...]:
    if not raw:
        return ()
    values: list[str] = []
    for item in raw:
        values.extend(part.strip() for part in str(item).split(",") if part.strip())
    return tuple(dict.fromkeys(values))


def _score_summary(scores: list[float]) -> dict[str, Any]:
    if not scores:
        return {"n": 0}
    buckets = {
        "<0.4": sum(1 for score in scores if score < 0.4),
        "0.4-0.55": sum(1 for score in scores if 0.4 <= score < 0.55),
        "0.55-0.65": sum(1 for score in scores if 0.55 <= score < 0.65),
        "0.65-0.75": sum(1 for score in scores if 0.65 <= score < 0.75),
        ">=0.75": sum(1 for score in scores if score >= 0.75),
    }
    return {
        "n": len(scores),
        "min": round(min(scores), 4),
        "mean": round(statistics.mean(scores), 4),
        "max": round(max(scores), 4),
        "stdev": round(statistics.stdev(scores), 4) if len(scores) > 1 else 0.0,
        "high_confidence": buckets[">=0.75"],
        "buckets": buckets,
    }


def _component_summary(values_by_name: dict[str, list[float]]) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for name, values in sorted(values_by_name.items()):
        if not values:
            continue
        summary[name] = {
            "min": round(min(values), 4),
            "mean": round(statistics.mean(values), 4),
            "max": round(max(values), 4),
            "unique": len({round(value, 3) for value in values}),
        }
    return summary


def _empty_contract_summary() -> dict[str, Any]:
    return {
        "checked": 0,
        "ok": 0,
        "failed": 0,
        "failure_rate": 0.0,
        "issues": [],
        "issue_counts": {},
        "fields": {
            "entry_zone": 0,
            "stop_loss": 0,
            "tp1": 0,
            "tp2": 0,
            "tp3": 0,
            "valid_until": 0,
            "scale_weights": 0,
        },
    }


def _contract_field_presence(signal: Any) -> dict[str, bool]:
    valid_until = getattr(signal, "valid_until", None)
    scale_weights = getattr(signal, "scale_weights", None)
    return {
        "entry_zone": getattr(signal, "entry_low", None) is not None
        and getattr(signal, "entry_high", None) is not None,
        "stop_loss": getattr(signal, "stop_loss", getattr(signal, "stop", None)) is not None,
        "tp1": getattr(signal, "tp1", getattr(signal, "take_profit_1", None)) is not None,
        "tp2": getattr(signal, "tp2", getattr(signal, "take_profit_2", None)) is not None,
        "tp3": getattr(signal, "tp3", getattr(signal, "take_profit_3", None)) is not None,
        "valid_until": valid_until is not None,
        "scale_weights": bool(scale_weights) and len(tuple(scale_weights)) == 3,
    }


def _contract_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return _empty_contract_summary()
    issue_counter: Counter[str] = Counter()
    field_counter: Counter[str] = Counter()
    failed_rows: list[dict[str, Any]] = []
    for row in rows:
        for field, present in row.get("field_presence", {}).items():
            if present:
                field_counter.update([field])
        issues = row.get("issues", [])
        if issues:
            failed_rows.append(row)
            for issue in issues:
                field = str(issue.get("field") or "unknown")
                reason = str(issue.get("reason") or "unknown")
                issue_counter.update([f"{field}:{reason}"])
    checked = len(rows)
    failed = len(failed_rows)
    return {
        "checked": checked,
        "ok": checked - failed,
        "failed": failed,
        "failure_rate": round(failed / checked, 4) if checked else 0.0,
        "issues": failed_rows[:20],
        "issue_counts": _counter_map(issue_counter),
        "fields": {
            field: int(field_counter.get(field, 0)) for field in _empty_contract_summary()["fields"]
        },
    }


def _strategy_ids() -> tuple[str, ...]:
    return tuple(strategy_class.setup_id for strategy_class in STRATEGY_CLASSES)


def _counter_items(counter: Counter[str], limit: int | None = None) -> list[dict[str, Any]]:
    items = counter.most_common(limit) if limit is not None else counter.most_common()
    return [{"name": name, "count": count} for name, count in items]


def _counter_map(counter: Counter[str]) -> dict[str, int]:
    return {name: int(count) for name, count in counter.items()}


def _hit_rate_summary(
    *,
    setup_ids: tuple[str, ...],
    hits_by_setup: Counter[str],
    rejects_by_setup: Counter[str],
    skips_by_setup: Counter[str],
    errors_by_setup: Counter[str],
) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for setup_id in setup_ids:
        hits = int(hits_by_setup.get(setup_id, 0))
        rejects = int(rejects_by_setup.get(setup_id, 0))
        skips = int(skips_by_setup.get(setup_id, 0))
        errors = int(errors_by_setup.get(setup_id, 0))
        runs = hits + rejects + skips + errors
        summary[setup_id] = {
            "hits": hits,
            "rejects": rejects,
            "skips": skips,
            "errors": errors,
            "observed_results": runs,
            "hit_rate": round(hits / runs, 4) if runs else 0.0,
            "reject_rate": round(rejects / runs, 4) if runs else 0.0,
            "skip_rate": round(skips / runs, 4) if runs else 0.0,
        }
    return summary


def _build_surface_summary(
    *,
    symbols: list[str],
    selected_ids: set[str],
    registered_ids: tuple[str, ...],
    prepared_ok: int,
    detector_runs: int,
    failures: list[dict[str, Any]],
    hits_by_setup: Counter[str],
    errors_by_setup: Counter[str],
    rejects_by_setup: Counter[str],
    skips_by_setup: Counter[str],
    reject_reasons: Counter[str],
    skip_reasons: Counter[str],
    scoring_summary: dict[str, Any],
    component_summary: dict[str, dict[str, Any]],
    signal_contract_summary: dict[str, Any],
) -> dict[str, Any]:
    evaluated_ids = tuple(id_ for id_ in registered_ids if not selected_ids or id_ in selected_ids)
    hit_ids = tuple(sorted(set(hits_by_setup)))
    missing_hit_ids = tuple(id_ for id_ in evaluated_ids if hits_by_setup.get(id_, 0) <= 0)
    error_ids = tuple(id_ for id_ in evaluated_ids if errors_by_setup.get(id_, 0) > 0)
    return {
        "symbols_requested": len(symbols),
        "prepared_ok": prepared_ok,
        "detector_runs": detector_runs,
        "requested_strategies": sorted(selected_ids),
        "registered_strategies": list(registered_ids),
        "evaluated_strategies": list(evaluated_ids),
        "hit_strategy_count": len(hit_ids),
        "hit_strategies": list(hit_ids),
        "missing_hit_strategies": list(missing_hit_ids),
        "error_strategies": list(error_ids),
        "confluence_score": scoring_summary,
        "confluence_components": component_summary,
        "signal_contract": signal_contract_summary,
        "strategy_hits": _counter_items(hits_by_setup),
        "strategy_errors": _counter_items(errors_by_setup),
        "strategy_rejects": _counter_items(rejects_by_setup),
        "strategy_reject_reasons": _counter_items(reject_reasons),
        "strategy_skips": _counter_items(skips_by_setup),
        "strategy_skip_reasons": _counter_items(skip_reasons),
        "strategy_counts": _hit_rate_summary(
            setup_ids=evaluated_ids,
            hits_by_setup=hits_by_setup,
            rejects_by_setup=rejects_by_setup,
            skips_by_setup=skips_by_setup,
            errors_by_setup=errors_by_setup,
        ),
        "prepare_failures": failures,
        "hit_counts": _counter_map(hits_by_setup),
        "reject_counts": _counter_map(rejects_by_setup),
        "skip_counts": _counter_map(skips_by_setup),
        "error_counts": _counter_map(errors_by_setup),
    }


def _write_summary_json(path: str, summary: dict[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")


def _resolve_required_hit_ids(
    raw: list[str] | None,
    *,
    selected_ids: set[str],
    available_ids: set[str],
) -> tuple[str, ...]:
    parsed = _parse_strategy_filter(raw)
    if not parsed:
        return ()
    if "all" in parsed:
        base = selected_ids or available_ids
        return tuple(sorted(base))
    unknown = sorted(set(parsed) - available_ids)
    if unknown:
        msg = f"unknown strategies required for hits: {unknown}"
        raise ValueError(msg)
    return tuple(dict.fromkeys(parsed))


def _resolve_allowed_missing_hit_ids(
    raw: list[str] | None,
    *,
    available_ids: set[str],
) -> set[str]:
    parsed = set(_parse_strategy_filter(raw))
    unknown = sorted(parsed - available_ids)
    if unknown:
        msg = f"unknown strategies allowed missing hits: {unknown}"
        raise ValueError(msg)
    return parsed


def _validate_surface_requirements(
    summary: dict[str, Any],
    *,
    required_hit_ids: tuple[str, ...],
    allowed_missing_hit_ids: set[str],
    min_hit_strategies: int,
    min_prepared: int,
    max_prepare_failures: int | None,
    min_score_max: float | None,
    min_score_stdev: float | None,
    require_signal_contract: bool,
) -> None:
    failures: list[str] = []
    if summary["prepared_ok"] < min_prepared:
        failures.append(
            f"prepared symbol count below requirement: {summary['prepared_ok']} < {min_prepared}"
        )
    if max_prepare_failures is not None:
        prepare_failures = len(summary.get("prepare_failures", []))
        if prepare_failures > max_prepare_failures:
            failures.append(
                f"prepare failures above requirement: {prepare_failures} > {max_prepare_failures}"
            )
    if summary["error_strategies"]:
        failures.append(f"strategy errors detected: {summary['error_strategies']}")
    if required_hit_ids:
        hit_counts = summary.get("hit_counts", {})
        missing = [
            setup_id
            for setup_id in required_hit_ids
            if setup_id not in allowed_missing_hit_ids and hit_counts.get(setup_id, 0) <= 0
        ]
        if missing:
            failures.append(f"required strategies without hits: {missing}")
    if min_hit_strategies > 0 and summary["hit_strategy_count"] < min_hit_strategies:
        failures.append(
            "hit strategy count below requirement: "
            f"{summary['hit_strategy_count']} < {min_hit_strategies}"
        )
    score_summary = summary.get("confluence_score", {})
    if min_score_max is not None and score_summary.get("n", 0) > 0:
        score_max = float(score_summary.get("max", 0.0))
        if score_max < min_score_max:
            failures.append(f"score max below requirement: {score_max:.4f} < {min_score_max:.4f}")
    if min_score_stdev is not None and score_summary.get("n", 0) > 1:
        score_stdev = float(score_summary.get("stdev", 0.0))
        if score_stdev < min_score_stdev:
            failures.append(
                f"score stdev below requirement: {score_stdev:.4f} < {min_score_stdev:.4f}"
            )
    if require_signal_contract:
        contract = summary.get("signal_contract", {})
        checked = int(contract.get("checked", 0) or 0)
        failed = int(contract.get("failed", 0) or 0)
        if checked <= 0:
            failures.append("signal contract requirement had no signals to inspect")
        if failed:
            failures.append(
                f"signal contract failures detected: {failed}/{checked} "
                f"{contract.get('issue_counts', {})}"
            )
    if failures:
        raise RuntimeError("; ".join(failures))


async def _run(
    symbols: list[str],
    concurrency: int,
    limit: int,
    strategy_filter: tuple[str, ...] = (),
) -> dict[str, Any]:
    settings = load_settings()
    minimums = min_required_bars(
        min_bars_15m=settings.filters.min_bars_15m,
        min_bars_1h=settings.filters.min_bars_1h,
        min_bars_4h=settings.filters.min_bars_4h,
    )
    registry = StrategyRegistry()
    selected_ids = set(strategy_filter)
    registered_ids = _strategy_ids()
    available_ids = set(registered_ids)
    unknown_ids = sorted(selected_ids - available_ids)
    if unknown_ids:
        msg = f"unknown strategies requested: {unknown_ids}"
        raise ValueError(msg)
    for strategy_class in STRATEGY_CLASSES:
        if selected_ids and strategy_class.setup_id not in selected_ids:
            continue
        registry.register(strategy_class(SetupParams(enabled=True), settings))
    if selected_ids:
        LOG.info("strategy_filter_applied", strategies=sorted(selected_ids))
    engine = SignalEngine(registry, settings)
    confluence = ConfluenceEngine(settings)
    client = BinanceFuturesMarketData(
        binance_client=BinanceClientImpl(
            rest_timeout_seconds=min(
                float(settings.ws.rest_timeout_seconds),
                LIVE_CHECK_HTTP_TIMEOUT_SECONDS,
            ),
            futures_data_request_limit_per_5m=settings.runtime.futures_data_request_limit_per_5m,
            proxy_url=settings.network.proxy_url,
            trust_env=settings.network.trust_env,
        ),
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
        market_context = _market_context_from_tickers(ticker_map)
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
        skips_by_setup: Counter[str] = Counter()
        reject_reasons: Counter[str] = Counter()
        skip_reasons: Counter[str] = Counter()
        confluence_scores: list[float] = []
        confluence_components: defaultdict[str, list[float]] = defaultdict(list)
        signal_contract_rows: list[dict[str, Any]] = []
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
                    market_context,
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
                results = await engine.calculate_all(prepared, event_interval="15m")
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
                    if decision is not None and decision.is_skip:
                        skips_by_setup.update([setup_id])
                        skip_reasons.update([decision.reason_code])
                    if decision is not None and decision.is_error:
                        errors_by_setup.update([setup_id])
                    elif result.signal is not None:
                        hits_by_setup.update([result.signal.setup_id])
                        contract_issues = validate_signal_contract(result.signal)
                        contract_row = signal_contract_row(result.signal)
                        contract_row["symbol"] = prepared.symbol
                        contract_row["field_presence"] = _contract_field_presence(result.signal)
                        if contract_issues:
                            contract_row["strategy_status"] = (
                                decision.status if decision is not None else "unknown"
                            )
                        signal_contract_rows.append(contract_row)
                        confluence_result = confluence.score(result.signal, prepared)
                        confluence_scores.append(confluence_result.final_score)
                        for component in confluence_result.components:
                            if component.available:
                                confluence_components[component.name].append(component.raw)

        await asyncio.gather(*[asyncio.create_task(_analyze(symbol)) for symbol in symbols])
        scoring_summary = _score_summary(confluence_scores)
        component_summary = _component_summary(confluence_components)
        contract_summary = _contract_summary(signal_contract_rows)
        summary = _build_surface_summary(
            symbols=symbols,
            selected_ids=selected_ids,
            registered_ids=registered_ids,
            prepared_ok=prepared_ok,
            detector_runs=detector_runs,
            failures=failures,
            hits_by_setup=hits_by_setup,
            errors_by_setup=errors_by_setup,
            rejects_by_setup=rejects_by_setup,
            skips_by_setup=skips_by_setup,
            reject_reasons=reject_reasons,
            skip_reasons=skip_reasons,
            scoring_summary=scoring_summary,
            component_summary=component_summary,
            signal_contract_summary=contract_summary,
        )
        LOG.info(
            "strategy_surface_summary",
            symbols=len(symbols),
            prepared_ok=prepared_ok,
            detector_runs=detector_runs,
            requested_strategies=sorted(selected_ids),
            confluence_score=scoring_summary,
            confluence_components=component_summary,
            signal_contract=contract_summary,
            strategy_hits=hits_by_setup.most_common(),
            strategy_errors=errors_by_setup.most_common(),
            strategy_rejects=rejects_by_setup.most_common(20),
            strategy_reject_reasons=reject_reasons.most_common(15),
            strategy_skips=skips_by_setup.most_common(20),
            strategy_skip_reasons=skip_reasons.most_common(15),
            missing_hit_strategies=summary["missing_hit_strategies"],
        )
        if failures:
            LOG.info("strategy_prepare_failures", failures=failures[:20])
        return summary
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Live strategy detector-surface review")
    parser.add_argument("--symbols", nargs="*", default=[])
    parser.add_argument("--symbols-from-run", default="")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument(
        "--strategies",
        nargs="*",
        default=[],
        help="Optional setup ids to run; accepts space- or comma-separated ids.",
    )
    parser.add_argument(
        "--summary-json",
        default="",
        help="Optional path for a machine-readable live strategy surface summary.",
    )
    parser.add_argument(
        "--print-summary-json",
        action="store_true",
        help="Print the machine-readable summary to stdout after the log summary.",
    )
    parser.add_argument(
        "--require-hit-strategies",
        nargs="*",
        default=[],
        help=(
            "Fail unless these setup ids produce at least one signal. "
            "Use 'all' to require every selected strategy, or every registered strategy "
            "when no --strategies filter is set."
        ),
    )
    parser.add_argument(
        "--allow-missing-hit-strategies",
        nargs="*",
        default=[],
        help=(
            "Setup ids exempted from --require-hit-strategies. "
            "Use this for deliberate schedule gates during outside-window checks."
        ),
    )
    parser.add_argument(
        "--min-hit-strategies",
        type=int,
        default=0,
        help="Fail unless at least this many strategies produce detector hits.",
    )
    parser.add_argument(
        "--min-prepared",
        type=int,
        default=1,
        help="Fail unless at least this many symbols are prepared successfully.",
    )
    parser.add_argument(
        "--max-prepare-failures",
        type=int,
        default=None,
        help="Fail when more than this many requested symbols fail prepare_symbol().",
    )
    parser.add_argument(
        "--min-score-max",
        type=float,
        default=None,
        help="Fail when confluence score max is below this value.",
    )
    parser.add_argument(
        "--min-score-stdev",
        type=float,
        default=None,
        help="Fail when confluence score standard deviation is below this value.",
    )
    parser.add_argument(
        "--require-signal-contract",
        action="store_true",
        help=(
            "Fail when any emitted signal misses entry zone, SL, TP1/TP2/TP3, TTL or scale weights."
        ),
    )
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
    strategy_filter = _parse_strategy_filter(args.strategies)
    required_hit_ids = _resolve_required_hit_ids(
        args.require_hit_strategies,
        selected_ids=set(strategy_filter),
        available_ids=set(_strategy_ids()),
    )
    allowed_missing_hit_ids = _resolve_allowed_missing_hit_ids(
        args.allow_missing_hit_strategies,
        available_ids=set(_strategy_ids()),
    )
    try:
        summary = asyncio.run(
            _run(
                symbols,
                args.concurrency,
                args.limit,
                strategy_filter,
            )
        )
        _validate_surface_requirements(
            summary,
            required_hit_ids=required_hit_ids,
            allowed_missing_hit_ids=allowed_missing_hit_ids,
            min_hit_strategies=max(0, int(args.min_hit_strategies)),
            min_prepared=max(1, int(args.min_prepared)),
            max_prepare_failures=args.max_prepare_failures,
            min_score_max=args.min_score_max,
            min_score_stdev=args.min_score_stdev,
            require_signal_contract=bool(args.require_signal_contract),
        )
        if args.summary_json:
            _write_summary_json(args.summary_json, summary)
            LOG.info("strategy_surface_summary_json_written", path=args.summary_json)
        if args.print_summary_json:
            print(json.dumps(summary, indent=2, sort_keys=True))
    except MarketDataUnavailable as exc:
        LOG.exception(
            "live_strategies_unavailable",
            operation=exc.operation,
            detail=exc.detail,
            symbol=exc.symbol,
        )
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
