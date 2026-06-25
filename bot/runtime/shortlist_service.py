"""Shortlist refresh helpers for SignalBot."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import UTC, datetime
from typing import Any

from bot.diagnostics.facade import assess_radar_store
from bot.runtime.watch_escalation import emit_radar_watch_candidates
from engine.domain.config import _ALL_SETUP_IDS
from engine.domain.events import ShortlistUpdatedEvent
from engine.domain.schemas import UniverseSymbol
from engine.errors import DEFENSIVE_EXC
from engine.market.data import BinanceFuturesMarketData, MarketDataUnavailable
from engine.market.outcome_derank import penalties_from_sl_counts
from engine.market.promotion_engine import PromotionEngine
from engine.market.proxy_bootstrap import retry_network_after_failure
from engine.market.universe import (
    DEFAULT_PRESCORE_BASIS_WARM_LIMIT,
    build_shortlist,
    rerank_shortlist,
    warm_prescore_basis_rest,
)

LOG = logging.getLogger("bot.runtime.shortlist_service")
_LOG_MISSING_VALUE = "not_available"

FALLBACK_REASON_WS_CACHE_COLD = "ws_cache_cold"
FALLBACK_REASON_FULL_REFRESH_DUE = "full_refresh_due"
FALLBACK_REASON_REFRESH_EXCEPTION = "refresh_exception"
FALLBACK_REASON_LIVE_EMPTY = "live_empty"
FALLBACK_REASON_USING_CACHED = "using_cached"
FALLBACK_REASON_USING_PINNED = "using_pinned"
FALLBACK_REASON_UNKNOWN = "unknown"


def _market_regime_hint(bot: Any) -> str | None:
    analyzer = getattr(bot, "market_regime", None)
    last = getattr(analyzer, "_last_result", None) if analyzer is not None else None
    if last is None:
        updater = getattr(bot, "_market_context_updater", None)
        last_regime = getattr(updater, "_last_regime", None) if updater is not None else None
        return str(last_regime).strip() if last_regime else None
    regime = str(getattr(last, "regime", "") or "").strip()
    return regime or None


def _apply_shortlist_tenure(
    bot: Any,
    shortlist: list[UniverseSymbol],
    *,
    now: datetime,
) -> list[UniverseSymbol]:
    min_tenure_s = int(getattr(bot.settings.universe, "shortlist_min_tenure_seconds", 0) or 0)
    if min_tenure_s <= 0 or not shortlist:
        for item in shortlist:
            bot._shortlist_symbol_since.setdefault(item.symbol, now)
        return shortlist

    since: dict[str, datetime] = getattr(bot, "_shortlist_symbol_since", {}) or {}
    incoming = {item.symbol: item for item in shortlist}
    protected: list[UniverseSymbol] = []
    for item in list(getattr(bot, "_shortlist", []) or []):
        joined = since.get(item.symbol)
        if joined is None:
            continue
        age_s = (now - joined.astimezone(UTC)).total_seconds()
        if age_s < min_tenure_s and item.symbol not in incoming:
            protected.append(item)

    merged: dict[str, UniverseSymbol] = dict(incoming)
    for item in protected:
        merged.setdefault(item.symbol, item)
    ordered = list(shortlist)
    for item in protected:
        if item.symbol not in {row.symbol for row in ordered}:
            ordered.append(item)
    for item in ordered:
        since.setdefault(item.symbol, now)
    bot._shortlist_symbol_since = since
    return ordered


async def _outcome_derank_penalties(bot: Any) -> dict[str, float]:
    repo = getattr(bot, "_modern_repo", None)
    if repo is None or not hasattr(repo, "get_symbol_sl_counts"):
        return {}
    try:
        sl_counts = await repo.get_symbol_sl_counts(last_days=7)
        sl_ages: dict[str, list[float]] = {}
        if hasattr(repo, "get_symbol_sl_event_ages"):
            sl_ages = await repo.get_symbol_sl_event_ages(last_days=7)
    except DEFENSIVE_EXC:
        LOG.debug("outcome_derank skipped: repository unavailable")
        return {}
    return penalties_from_sl_counts(
        sl_counts,
        sl_event_ages_days=sl_ages or None,
    )


def normalize_shortlist_fallback_reason(value: object) -> str | None:
    raw = str(value or "").strip().replace(" ", "_").lower()
    if not raw:
        return None
    aliases = {
        "cached": FALLBACK_REASON_USING_CACHED,
        "using_cache": FALLBACK_REASON_USING_CACHED,
        "using_cached": FALLBACK_REASON_USING_CACHED,
        "refresh_failed": FALLBACK_REASON_REFRESH_EXCEPTION,
        "refresh_exception": FALLBACK_REASON_REFRESH_EXCEPTION,
        "exception": FALLBACK_REASON_REFRESH_EXCEPTION,
        "live_empty": FALLBACK_REASON_LIVE_EMPTY,
        "empty_live": FALLBACK_REASON_LIVE_EMPTY,
        "ws_cache_cold": FALLBACK_REASON_WS_CACHE_COLD,
        "ws_light_skipped": FALLBACK_REASON_WS_CACHE_COLD,
        "ws_light_no_meta": FALLBACK_REASON_WS_CACHE_COLD,
        "ws_light_partial_cache": FALLBACK_REASON_WS_CACHE_COLD,
        "ws_light_filtered_small": FALLBACK_REASON_LIVE_EMPTY,
        "full_refresh_due": FALLBACK_REASON_FULL_REFRESH_DUE,
        "using_pinned": FALLBACK_REASON_USING_PINNED,
        "pinned": FALLBACK_REASON_USING_PINNED,
        "pinned_fallback": FALLBACK_REASON_USING_PINNED,
    }
    return aliases.get(raw, FALLBACK_REASON_UNKNOWN)


def _log_value(value: object) -> object:
    return _LOG_MISSING_VALUE if value is None else value


def _radar_store(bot: Any) -> Any | None:
    ws = getattr(bot, "_ws_manager", None)
    if ws is None:
        return None
    return getattr(ws, "_radar_store", None)


class ShortlistService:
    """Encapsulates shortlist build/refresh lifecycle for ``SignalBot``."""

    def __init__(self, bot: Any) -> None:
        self._bot = bot

    def _prepare_tickers_with_radar(
        self,
        tickers: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        bot = self._bot
        if not bot.settings.universe.radar.enabled:
            return tickers, None
        store = _radar_store(bot)
        if store is None:
            return tickers, None
        engine = PromotionEngine(bot.settings)
        store.ingest_batch(tickers)
        tier_summary = engine.run_tier_cycle(store)
        enriched = engine.enrich_ticker_rows(tickers, store)
        return enriched, tier_summary

    def _merge_shortlist_with_radar(
        self,
        shortlist: list[UniverseSymbol],
        summary: dict[str, Any],
        *,
        seed_source: str,
    ) -> tuple[list[UniverseSymbol], dict[str, Any]]:
        bot = self._bot
        if not bot.settings.universe.radar.enabled:
            return shortlist, summary
        store = _radar_store(bot)
        if store is None:
            return shortlist, summary
        engine = PromotionEngine(bot.settings)
        merged, radar_summary = engine.merge_shortlist(
            shortlist,
            store,
            meta_by_symbol=bot._symbol_meta_by_symbol,
            seed_source=seed_source,
        )
        return merged, {**summary, "radar": radar_summary}

    def _schedule_context_preload(self) -> None:
        bot = self._bot
        if not isinstance(getattr(bot, "client", None), BinanceFuturesMarketData):
            return
        preload = getattr(bot, "_preload_shortlist_frames", None)
        if not callable(preload):
            return
        current_task = getattr(bot, "_context_preload_task", None)
        if current_task is not None and not current_task.done():
            return
        try:
            task = asyncio.create_task(preload(), name="preload_frames:shortlist_refresh")
        except RuntimeError:
            return
        bot._context_preload_task = task
        background_tasks = getattr(bot, "_background_tasks", None)
        if isinstance(background_tasks, set):
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)

    @staticmethod
    def _spread_bps(bid: float | None, ask: float | None) -> float | None:
        try:
            bid_value = float(bid) if bid is not None else None
            ask_value = float(ask) if ask is not None else None
        except (TypeError, ValueError):
            return None
        if (
            bid_value is None
            or ask_value is None
            or not math.isfinite(bid_value)
            or not math.isfinite(ask_value)
            or bid_value <= 0.0
            or ask_value <= 0.0
            or ask_value < bid_value
        ):
            return None
        mid = (bid_value + ask_value) / 2.0
        if mid <= 0.0:
            return None
        return ((ask_value - bid_value) / mid) * 10_000.0

    def _enrich_shortlist_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        bot = self._bot
        enriched: list[dict[str, Any]] = []
        ws = getattr(bot, "_ws_manager", None)
        client = bot.client
        open_interest_cache = (
            getattr(client, "_open_interest_cache", {})
            if isinstance(client, BinanceFuturesMarketData)
            else {}
        )

        for raw in rows:
            row = dict(raw)
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol:
                continue

            rest_aux = getattr(bot, "_rest_ticker_aux_by_symbol", {}).get(symbol, {})
            if int(float(row.get("trade_count") or 0)) <= 0:
                rest_trade_count = int(rest_aux.get("trade_count") or 0)
                if rest_trade_count > 0:
                    row["trade_count"] = rest_trade_count

            if ws is not None:
                try:
                    ticker_age = ws.get_ticker_age_seconds(symbol)
                    if ticker_age is not None:
                        row["ticker_age_seconds"] = float(ticker_age)
                except DEFENSIVE_EXC as exc:
                    LOG.debug(
                        "shortlist ticker age unavailable | symbol=%s error=%s",
                        symbol,
                        exc,
                    )
                try:
                    mark = ws.get_mark_price_snapshot(symbol)
                    mark_age = ws.get_mark_price_age_seconds(symbol)
                    if mark_age is not None:
                        row["mark_price_age_seconds"] = float(mark_age)
                    if mark:
                        funding_rate = mark.get("funding_rate")
                        if funding_rate is not None:
                            row["funding_rate"] = float(funding_rate)
                        mark_price = float(mark.get("mark_price") or 0.0)
                        index_price = float(mark.get("index_price") or 0.0)
                        if mark_price > 0.0 and index_price > 0.0:
                            row["basis_pct"] = ((mark_price - index_price) / index_price) * 100.0
                except DEFENSIVE_EXC as exc:
                    LOG.debug(
                        "shortlist mark price unavailable | symbol=%s error=%s",
                        symbol,
                        exc,
                    )
                try:
                    bid, ask = ws.get_book_snapshot(symbol)
                    spread_bps = self._spread_bps(bid, ask)
                    if spread_bps is not None:
                        row["spread_bps"] = spread_bps
                    book_age = ws.get_book_ticker_age_seconds(symbol)
                    if book_age is not None:
                        row["book_age_seconds"] = float(book_age)
                except DEFENSIVE_EXC as exc:
                    LOG.debug(
                        "shortlist book snapshot unavailable | symbol=%s error=%s",
                        symbol,
                        exc,
                    )
                try:
                    liquidation = ws.get_liquidation_sentiment(symbol, window_seconds=900)
                    if liquidation is not None:
                        row["liquidation_score"] = float(liquidation)
                    rollups = ws.get_liquidation_rollups(symbol, window_seconds=900)
                    if rollups is not None:
                        if rollups.get("liquidation_long_notional") is not None:
                            row["liquidation_long_notional"] = float(
                                rollups["liquidation_long_notional"]
                            )
                        if rollups.get("liquidation_short_notional") is not None:
                            row["liquidation_short_notional"] = float(
                                rollups["liquidation_short_notional"]
                            )
                except DEFENSIVE_EXC as exc:
                    LOG.debug(
                        "shortlist liquidation sentiment unavailable | symbol=%s error=%s",
                        symbol,
                        exc,
                    )
                try:
                    agg_snapshot = ws.get_agg_trade_snapshot(symbol, window_seconds=30)
                    if agg_snapshot is not None and agg_snapshot.delta_ratio is not None:
                        row["agg_trade_delta_30s"] = float(agg_snapshot.delta_ratio)
                except DEFENSIVE_EXC as exc:
                    LOG.debug(
                        "shortlist agg trade delta unavailable | symbol=%s error=%s",
                        symbol,
                        exc,
                    )
                try:
                    depth_imbalance = ws.get_depth_imbalance(symbol)
                    if depth_imbalance is not None:
                        row["depth_imbalance"] = float(depth_imbalance)
                except DEFENSIVE_EXC as exc:
                    LOG.debug(
                        "shortlist depth imbalance unavailable | symbol=%s error=%s",
                        symbol,
                        exc,
                    )
                try:
                    microprice_bias = ws.get_microprice_bias(symbol)
                    if microprice_bias is not None:
                        row["microprice_bias"] = float(microprice_bias)
                except DEFENSIVE_EXC as exc:
                    LOG.debug(
                        "shortlist microprice bias unavailable | symbol=%s error=%s",
                        symbol,
                        exc,
                    )

            if isinstance(client, BinanceFuturesMarketData):
                oi_change = client.get_cached_oi_change(symbol)
                if oi_change is not None:
                    row["oi_change_pct"] = float(oi_change)
                ls_ratio = client.get_cached_ls_ratio(symbol)
                if ls_ratio is not None:
                    row["top_account_ls_ratio"] = float(ls_ratio)
                top_position = client.get_cached_top_position_ls_ratio(symbol)
                if top_position is not None:
                    row["top_position_ls_ratio"] = float(top_position)
                global_ratio = client.get_cached_global_ls_ratio(symbol)
                if global_ratio is not None:
                    row["global_account_ls_ratio"] = float(global_ratio)
                taker_ratio = client.get_cached_taker_ratio(symbol)
                if taker_ratio is not None:
                    row["taker_ratio"] = float(taker_ratio)
                funding_trend = client.get_cached_funding_trend(symbol)
                if funding_trend is not None:
                    row["funding_trend"] = str(funding_trend)
                if "top_account_ls_ratio" in row and "global_account_ls_ratio" in row:
                    row["top_vs_global_ls_gap"] = float(row["top_account_ls_ratio"]) - float(
                        row["global_account_ls_ratio"]
                    )
                basis_pct = client.get_cached_basis(symbol, period="1h")
                if basis_pct is not None:
                    row["basis_pct"] = float(basis_pct)
                basis_stats = client.get_cached_basis_stats(symbol, period="5m")
                if basis_stats is not None:
                    premium_slope = basis_stats.get("premium_slope_5m")
                    premium_zscore = basis_stats.get("premium_zscore_5m")
                    if premium_slope is not None:
                        row["premium_slope_5m"] = float(premium_slope)
                    if premium_zscore is not None:
                        row["premium_zscore_5m"] = float(premium_zscore)
                cached_oi = open_interest_cache.get(symbol)
                if cached_oi is not None:
                    _ts, oi_value = cached_oi
                    row["oi_current"] = float(oi_value)
                stale_flags_fn = getattr(client, "get_rest_enrichment_stale_flags", None)
                if callable(stale_flags_fn):
                    stale_fields = stale_flags_fn(symbol)
                    if stale_fields:
                        row["enrichment_stale_fields"] = list(stale_fields)
            enriched.append(row)
        return enriched

    @staticmethod
    def _merge_symbol_prices_rows(
        rows: list[dict[str, Any]],
        prices_by_symbol: dict[str, float],
    ) -> list[dict[str, Any]]:
        if not prices_by_symbol:
            return rows
        merged: list[dict[str, Any]] = []
        for raw in rows:
            row = dict(raw)
            symbol = str(row.get("symbol") or "").strip().upper()
            price = prices_by_symbol.get(symbol)
            if price is not None and price > 0.0:
                row["last_price"] = price
            merged.append(row)
        return merged

    @staticmethod
    def _merge_premium_index_rows(
        rows: list[dict[str, Any]],
        premium_by_symbol: dict[str, dict[str, float]],
    ) -> list[dict[str, Any]]:
        if not premium_by_symbol:
            return rows
        merged: list[dict[str, Any]] = []
        for raw in rows:
            row = dict(raw)
            symbol = str(row.get("symbol") or "").strip().upper()
            premium = premium_by_symbol.get(symbol)
            if premium:
                if row.get("funding_rate") is None:
                    row["funding_rate"] = premium.get("funding_rate")
                if row.get("basis_pct") is None:
                    row["basis_pct"] = premium.get("basis_pct")
                if row.get("estimated_settle_price") is None:
                    row["estimated_settle_price"] = premium.get("estimated_settle_price")
                if row.get("interest_rate") is None:
                    row["interest_rate"] = premium.get("interest_rate")
                if row.get("next_funding_time_ms") is None:
                    row["next_funding_time_ms"] = premium.get("next_funding_time_ms")
            merged.append(row)
        return merged

    def _basis_warm_kwargs(self) -> dict[str, Any]:
        bot = self._bot
        ws = getattr(bot, "_ws_manager", None)
        client = bot.client

        def get_mark_basis(symbol: str) -> float | None:
            if ws is None:
                return None
            try:
                mark = ws.get_mark_price_snapshot(symbol)
            except DEFENSIVE_EXC:
                return None
            if not mark:
                return None
            try:
                mark_price = float(mark.get("mark_price") or 0.0)
                index_price = float(mark.get("index_price") or 0.0)
            except (TypeError, ValueError):
                return None
            if mark_price <= 0.0 or index_price <= 0.0:
                return None
            return ((mark_price - index_price) / index_price) * 100.0

        def get_cached_basis(symbol: str) -> float | None:
            if not isinstance(client, BinanceFuturesMarketData):
                return None
            return client.get_cached_basis(symbol, period="1h")

        return {
            "get_cached_basis": get_cached_basis,
            "get_mark_basis": get_mark_basis,
        }

    async def _warm_prescore_basis_for_rows(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        bot = self._bot
        client = bot.client
        if not isinstance(client, BinanceFuturesMarketData):
            return {
                "basis_warm_attempted": 0,
                "basis_warm_ok": 0,
                "basis_warm_failed": 0,
            }
        warm_limit = int(
            getattr(
                bot.settings.universe,
                "prescore_basis_warm_limit",
                DEFAULT_PRESCORE_BASIS_WARM_LIMIT,
            )
            or DEFAULT_PRESCORE_BASIS_WARM_LIMIT
        )

        async def _fetch(symbol: str) -> float | None:
            return await client.fetch_basis(symbol, period="1h", limit=5)

        return await warm_prescore_basis_rest(
            rows,
            _fetch,
            settings=bot.settings,
            limit=warm_limit,
        )

    async def fetch_symbols_with_retry(self, *, max_retries: int = 1) -> list[Any]:
        for attempt in range(max_retries + 1):
            try:
                return await asyncio.wait_for(
                    self._bot.client.fetch_exchange_symbols(),
                    timeout=10.0,
                )
            except TimeoutError:
                LOG.info(
                    "fetch_exchange_symbols attempt %d/%d timed out",
                    attempt + 1,
                    max_retries + 1,
                )
                if attempt < max_retries:
                    await asyncio.sleep(1.0)
                else:
                    raise
            except DEFENSIVE_EXC:
                LOG.exception(
                    "fetch_exchange_symbols attempt %d/%d failed",
                    attempt + 1,
                    max_retries + 1,
                )
                if attempt < max_retries:
                    await asyncio.sleep(1.0)
                else:
                    raise
        return []

    def extract_symbol_assets(self, symbol: str) -> tuple[str | None, str | None]:
        bot = self._bot
        sym = str(symbol).strip().upper()
        meta = bot._symbol_meta_by_symbol.get(sym)
        if meta is None:
            exchange_cache = getattr(bot.client, "_exchange_info_cache", None)
            if exchange_cache is not None:
                _cached_at, rows = exchange_cache
                cache_map = {str(getattr(row, "symbol", "")).strip().upper(): row for row in rows}
                bot._symbol_meta_by_symbol.update(cache_map)
                meta = bot._symbol_meta_by_symbol.get(sym)
        if meta is not None:
            base = str(getattr(meta, "base_asset", "")).strip().upper()
            quote = str(getattr(meta, "quote_asset", "")).strip().upper()
            if base and quote:
                return base, quote

        configured_quote = str(bot.settings.universe.quote_asset).strip().upper()
        if configured_quote and sym.endswith(configured_quote):
            base = sym[: -len(configured_quote)]
            if base:
                return base, configured_quote
        return None, None

    def build_pinned_shortlist(self) -> list[UniverseSymbol]:
        bot = self._bot
        shortlist: list[UniverseSymbol] = []
        for raw_symbol in bot.settings.universe.pinned_symbols:
            symbol = str(raw_symbol).strip().upper()
            base_asset, quote_asset = self.extract_symbol_assets(symbol)
            if not base_asset or not quote_asset:
                LOG.error(
                    (
                        "skipping pinned symbol due to unresolved base/quote assets | "
                        "symbol=%s configured_quote_asset=%s"
                    ),
                    symbol,
                    bot.settings.universe.quote_asset,
                )
                continue
            meta = bot._symbol_meta_by_symbol.get(symbol)
            contract_type = str(getattr(meta, "contract_type", "") or "PERPETUAL").upper()
            onboard_date_ms = int(getattr(meta, "onboard_date_ms", 0) or 0)
            shortlist.append(
                UniverseSymbol(
                    symbol=symbol,
                    base_asset=base_asset,
                    quote_asset=quote_asset,
                    contract_type=contract_type,
                    status="TRADING",
                    onboard_date_ms=onboard_date_ms,
                    quote_volume=0.0,
                    price_change_pct=0.0,
                    last_price=0.0,
                    shortlist_bucket="pinned",
                    shortlist_score=1.0,
                    shortlist_reasons=("pinned_symbol",),
                    seed_source="pinned_fallback",
                    strategy_fits=tuple(_ALL_SETUP_IDS),
                )
            )
        return shortlist

    async def build_live_shortlist(self) -> tuple[list[UniverseSymbol], dict[str, Any]]:
        bot = self._bot
        settings = bot.settings
        timeout_s = max(10.0, float(bot.settings.ws.rest_timeout_seconds) * 2.0)
        results = await asyncio.wait_for(
            asyncio.gather(
                self.fetch_symbols_with_retry(max_retries=1),
                bot.client.fetch_ticker_24h(),
                bot.client.fetch_premium_index_all(),
                bot.client.fetch_symbol_prices_all(),
                return_exceptions=True,
            ),
            timeout=timeout_s,
        )
        symbol_meta_result, tickers_result, premium_result, prices_result = results
        if isinstance(symbol_meta_result, Exception):
            cached_meta = list(getattr(bot, "_symbol_meta_by_symbol", {}).values())
            if not cached_meta:
                raise symbol_meta_result
            LOG.info(
                (
                    "shortlist symbol metadata refresh failed; using cached metadata | "
                    "count=%d error=%s"
                ),
                len(cached_meta),
                symbol_meta_result,
            )
            symbol_meta_list = cached_meta
        else:
            symbol_meta_list = list(symbol_meta_result)
        if isinstance(tickers_result, Exception):
            if isinstance(tickers_result, MarketDataUnavailable):
                try:
                    refreshed = await retry_network_after_failure(bot.settings)
                    if (
                        refreshed.network.effective_proxy_urls()
                        != bot.settings.network.effective_proxy_urls()
                    ):
                        bot.settings = refreshed
                        _inner = getattr(bot, "client", None)
                        _inner = getattr(_inner, "_binance_client", None) if _inner else None
                        if _inner is not None and hasattr(_inner, "_apply_active_proxy"):
                            new_url = refreshed.network.proxy_url or (
                                refreshed.network.effective_proxy_urls()[0]
                                if refreshed.network.effective_proxy_urls()
                                else None
                            )
                            await _inner._apply_active_proxy(new_url)
                            LOG.warning(
                                "proxy pool refreshed after REST failure — "
                                "hot-swapped egress | url=%s",
                                new_url,
                            )
                except DEFENSIVE_EXC:
                    LOG.debug("network rediscovery after ticker failure skipped", exc_info=True)
            raise tickers_result
        tickers_24h = list(tickers_result)
        if isinstance(premium_result, Exception):
            premium_by_symbol = {}
            LOG.info(
                (
                    "shortlist premium-index refresh failed; "
                    "continuing without premium context | error=%s"
                ),
                premium_result,
            )
        else:
            premium_by_symbol = dict(premium_result)
        if isinstance(prices_result, Exception):
            prices_by_symbol: dict[str, float] = {}
            LOG.info(
                "shortlist symbol-price refresh failed; using 24h ticker prices | error=%s",
                prices_result,
            )
        else:
            prices_by_symbol = dict(prices_result)
        bot._symbol_meta_by_symbol = {
            str(getattr(row, "symbol", "")).strip().upper(): row for row in symbol_meta_list
        }
        bot._rest_ticker_aux_by_symbol = {
            str(t.get("symbol", "")).strip().upper(): {
                "trade_count": int(float(t.get("trade_count") or 0)),
            }
            for t in tickers_24h
            if t.get("symbol")
        }
        outcome_penalties = await _outcome_derank_penalties(bot)
        enriched_tickers = self._enrich_shortlist_rows(
            self._merge_symbol_prices_rows(
                self._merge_premium_index_rows(list(tickers_24h), premium_by_symbol),
                prices_by_symbol,
            )
        )
        await self._warm_prescore_basis_for_rows(enriched_tickers)
        enriched_tickers, radar_pre = self._prepare_tickers_with_radar(enriched_tickers)
        shortlist, summary = build_shortlist(
            symbol_meta_list,
            enriched_tickers,
            bot.settings,
            seed_source="rest_full",
            market_regime=_market_regime_hint(bot),
            outcome_penalties=outcome_penalties,
            **self._basis_warm_kwargs(),
        )
        if radar_pre is not None:
            summary = {**summary, "radar_tier_cycle": radar_pre}
        shortlist, summary = self._merge_shortlist_with_radar(
            shortlist, summary, seed_source="rest_full"
        )
        LOG.info(
            "universe filter result | raw_tickers=%d gate_passed=%d light_pool=%d eligible=%d "
            "passed_volume=%d passed_change=%d min_volume=%.0f min_change=%.2f",
            len(tickers_24h),
            summary.get("gate_passed", summary.get("eligible", 0)),
            summary.get("light_pool", summary.get("eligible", 0)),
            summary.get("eligible", 0),
            sum(
                1
                for t in tickers_24h
                if float(t.get("quote_volume") or 0.0) >= settings.universe.min_quote_volume_usd
            ),
            sum(
                1
                for t in tickers_24h
                if abs(float(t.get("price_change_percent") or 0.0))
                >= settings.universe.min_price_change_pct
            ),
            settings.universe.min_quote_volume_usd,
            settings.universe.min_price_change_pct,
        )
        return shortlist, summary

    async def build_light_shortlist(
        self,
    ) -> tuple[list[UniverseSymbol], dict[str, Any]]:
        bot = self._bot
        ws = getattr(bot, "_ws_manager", None)
        if ws is None or not ws.is_ticker_cache_warm():
            LOG.debug("light shortlist skipped: ws cache not warm")
            return [], {
                "mode": "ws_light_skipped",
                "eligible": 0,
                "dynamic_pool": 0,
                "pinned": 0,
            }
        if not bot._symbol_meta_by_symbol:
            LOG.info("light shortlist skipped: symbol_meta not loaded yet, triggering REST fetch")
            return [], {
                "mode": "ws_light_no_meta",
                "eligible": 0,
                "dynamic_pool": 0,
                "pinned": 0,
            }

        raw_tickers = ws.get_global_ticker_data()
        cached_shortlist = list(getattr(bot, "_last_live_shortlist", []) or [])
        pinned_count = len(getattr(bot.settings.universe, "pinned_symbols", ()) or ())
        shortlist_limit = int(getattr(bot.settings.universe, "shortlist_limit", 50))
        minimum_light_tickers = max(pinned_count + 3, min(shortlist_limit, len(cached_shortlist)))
        if len(raw_tickers) < minimum_light_tickers:
            LOG.info(
                (
                    "light shortlist skipped: partial ws ticker cache | tickers=%d "
                    "required=%d cached_shortlist=%d"
                ),
                len(raw_tickers),
                minimum_light_tickers,
                len(cached_shortlist),
            )
            return [], {
                "mode": "ws_light_partial_cache",
                "eligible": 0,
                "dynamic_pool": 0,
                "pinned": 0,
                "raw_tickers": len(raw_tickers),
            }

        tickers = self._enrich_shortlist_rows(raw_tickers)
        await self._warm_prescore_basis_for_rows(tickers)
        tickers, radar_pre = self._prepare_tickers_with_radar(tickers)
        outcome_penalties = await _outcome_derank_penalties(bot)
        shortlist, summary = build_shortlist(
            list(bot._symbol_meta_by_symbol.values()),
            tickers,
            bot.settings,
            seed_source="ws_light",
            market_regime=_market_regime_hint(bot),
            outcome_penalties=outcome_penalties,
            **self._basis_warm_kwargs(),
        )
        if radar_pre is not None:
            summary = {**summary, "radar_tier_cycle": radar_pre}
        shortlist, summary = self._merge_shortlist_with_radar(
            shortlist, summary, seed_source="ws_light"
        )
        if (
            cached_shortlist
            and len(shortlist) < max(pinned_count + 3, int(shortlist_limit * 0.4))
            and len(cached_shortlist) > len(shortlist)
        ):
            LOG.info(
                (
                    "light shortlist skipped: filtered ws shortlist too small | "
                    "size=%d required=%d cached_shortlist=%d eligible=%s dynamic_pool=%s"
                ),
                len(shortlist),
                minimum_light_tickers,
                len(cached_shortlist),
                summary.get("eligible"),
                summary.get("dynamic_pool"),
            )
            return [], {
                **summary,
                "mode": "ws_light_filtered_small",
                "raw_tickers": len(raw_tickers),
            }
        return shortlist, summary

    @staticmethod
    def _union_shortlist_rows(
        primary: list[UniverseSymbol],
        secondary: list[UniverseSymbol],
    ) -> list[UniverseSymbol]:
        """Merge shortlist snapshots, keeping the higher-scored row per symbol."""
        merged: dict[str, UniverseSymbol] = {}
        for row in (*secondary, *primary):
            existing = merged.get(row.symbol)
            if existing is None or (row.shortlist_score or 0.0) >= (
                existing.shortlist_score or 0.0
            ):
                merged[row.symbol] = row
        return sorted(
            merged.values(),
            key=lambda item: (-(item.shortlist_score or 0.0), item.symbol),
        )

    async def build_medium_shortlist(
        self,
    ) -> tuple[list[UniverseSymbol], dict[str, Any]]:
        """REST ticker refresh using cached symbol meta (Phase 4 medium refresh)."""
        bot = self._bot
        if not bot._symbol_meta_by_symbol:
            return [], {
                "mode": "rest_medium_skipped",
                "eligible": 0,
                "dynamic_pool": 0,
                "pinned": 0,
            }
        timeout_s = max(10.0, float(bot.settings.ws.rest_timeout_seconds) * 2.0)
        try:
            tickers_result = await asyncio.wait_for(
                bot.client.fetch_ticker_24h(),
                timeout=timeout_s,
            )
        except DEFENSIVE_EXC as exc:
            LOG.info("medium shortlist ticker refresh failed | error=%s", exc)
            return [], {
                "mode": "rest_medium_failed",
                "eligible": 0,
                "dynamic_pool": 0,
                "pinned": 0,
            }
        tickers_24h = list(tickers_result)
        bot._rest_ticker_aux_by_symbol = {
            str(t.get("symbol", "")).strip().upper(): {
                "trade_count": int(float(t.get("trade_count") or 0)),
            }
            for t in tickers_24h
            if t.get("symbol")
        }
        outcome_penalties = await _outcome_derank_penalties(bot)
        enriched_tickers = self._enrich_shortlist_rows(list(tickers_24h))
        await self._warm_prescore_basis_for_rows(enriched_tickers)
        enriched_tickers, radar_pre = self._prepare_tickers_with_radar(enriched_tickers)
        shortlist, summary = build_shortlist(
            list(bot._symbol_meta_by_symbol.values()),
            enriched_tickers,
            bot.settings,
            seed_source="rest_medium",
            market_regime=_market_regime_hint(bot),
            outcome_penalties=outcome_penalties,
            **self._basis_warm_kwargs(),
        )
        if radar_pre is not None:
            summary = {**summary, "radar_tier_cycle": radar_pre}
        shortlist, summary = self._merge_shortlist_with_radar(
            shortlist, summary, seed_source="rest_medium"
        )
        cached_shortlist = list(getattr(bot, "_last_live_shortlist", []) or [])
        if cached_shortlist:
            shortlist = self._union_shortlist_rows(shortlist, cached_shortlist)
            summary = {
                **summary,
                "mode": "rest_medium_union",
                "union_with_cached": len(cached_shortlist),
            }
        else:
            summary = {**summary, "mode": "rest_medium"}
        return shortlist, summary

    async def do_refresh_shortlist(self) -> list[UniverseSymbol]:
        bot = self._bot
        LOG.info("refreshing shortlist...")
        if not hasattr(bot, "_last_shortlist_full_refresh_at"):
            bot._last_shortlist_full_refresh_at = None
        if not hasattr(bot, "_last_shortlist_medium_refresh_at"):
            bot._last_shortlist_medium_refresh_at = None

        source_before = str(getattr(bot, "_shortlist_source", "") or "")
        source = "pinned_fallback"
        summary: dict[str, Any] = {}
        shortlist = self.build_pinned_shortlist()
        now = datetime.now(UTC)
        full_interval = int(
            getattr(
                bot.settings.universe,
                "full_refresh_interval_seconds",
                bot.settings.runtime.shortlist_refresh_interval_seconds,
            )
        )
        medium_interval = int(
            getattr(bot.settings.universe, "medium_refresh_interval_seconds", 900)
        )
        last_full = getattr(bot, "_last_shortlist_full_refresh_at", None)
        last_medium = getattr(bot, "_last_shortlist_medium_refresh_at", None)
        full_refresh_due = last_full is None or (now - last_full).total_seconds() >= full_interval
        medium_refresh_due = (
            last_medium is None or (now - last_medium).total_seconds() >= medium_interval
        )
        ws = getattr(bot, "_ws_manager", None)
        try:
            ws_cache_warm = bool(ws is not None and ws.is_ticker_cache_warm())
        except DEFENSIVE_EXC as exc:
            LOG.debug("shortlist ws cache warm check failed: %s", exc)
            ws_cache_warm = False
        has_symbol_meta = bool(getattr(bot, "_symbol_meta_by_symbol", None))
        cached_shortlist = list(getattr(bot, "_last_live_shortlist", []) or [])
        cached_at = getattr(bot, "_last_live_shortlist_at", None)
        cached_shortlist_age_s = (
            max(0.0, (now - cached_at).total_seconds()) if isinstance(cached_at, datetime) else None
        )
        fallback_reason: str | None = None

        try:
            live_shortlist: list[UniverseSymbol] = []
            live_summary: dict[str, Any] = {}
            if not full_refresh_due:
                live_shortlist, live_summary = await self.build_light_shortlist()
                if live_shortlist:
                    source = "ws_light"
                elif medium_refresh_due:
                    medium_shortlist, medium_summary = await self.build_medium_shortlist()
                    if medium_shortlist:
                        live_shortlist = medium_shortlist
                        live_summary = medium_summary
                        source = str(medium_summary.get("mode") or "rest_medium")
                        bot._last_shortlist_medium_refresh_at = now
                elif cached_shortlist:
                    max_cache_age = float(
                        getattr(
                            bot.settings.universe,
                            "shortlist_cache_max_age_seconds",
                            3600,
                        )
                        or 3600
                    )
                    cache_fresh = (
                        cached_shortlist_age_s is not None
                        and cached_shortlist_age_s <= max_cache_age
                    )
                    if cache_fresh:
                        live_shortlist = list(cached_shortlist)
                        live_summary = {"mode": "cached"}
                        source = "cached"
                        fallback_reason = FALLBACK_REASON_USING_CACHED
                    elif not ws_cache_warm or not has_symbol_meta:
                        fallback_reason = FALLBACK_REASON_WS_CACHE_COLD
                elif not ws_cache_warm or not has_symbol_meta:
                    fallback_reason = FALLBACK_REASON_WS_CACHE_COLD
            if full_refresh_due or (not live_shortlist and not cached_shortlist):
                if full_refresh_due:
                    fallback_reason = FALLBACK_REASON_FULL_REFRESH_DUE
                live_shortlist, live_summary = await self.build_live_shortlist()
                fit_counts = [len(item.strategy_fits) for item in live_shortlist]
                zero_fit = sum(1 for count in fit_counts if count == 0)
                LOG.info(
                    "shortlist build result | source=%s gate_passed=%s light_pool=%s "
                    "eligible=%s dynamic_pool=%s pinned=%s total=%d "
                    "strategy_fits_total=%d zero_strategy_fit=%d",
                    "rest_full",
                    live_summary.get("gate_passed"),
                    live_summary.get("light_pool"),
                    live_summary.get("eligible"),
                    live_summary.get("dynamic_pool"),
                    live_summary.get("pinned"),
                    len(live_shortlist),
                    sum(fit_counts),
                    zero_fit,
                )
                if live_shortlist and zero_fit > len(live_shortlist) * 0.5:
                    LOG.warning(
                        "shortlist DEGRADED (immediate): >50%% symbols have zero strategy_fits "
                        "(%d/%d) after refresh",
                        zero_fit,
                        len(live_shortlist),
                    )
                if live_shortlist:
                    bot._last_shortlist_full_refresh_at = now
                    source = "rest_full"
                elif fallback_reason is None:
                    fallback_reason = FALLBACK_REASON_LIVE_EMPTY
            if live_shortlist:
                shortlist = live_shortlist
                summary = live_summary
                bot._last_live_shortlist = list(live_shortlist)
                if source in {"rest_full", "ws_light"}:
                    bot._last_live_shortlist_at = now
                    fallback_reason = None
            elif cached_shortlist:
                shortlist = list(cached_shortlist)
                source = "cached"
                fallback_reason = FALLBACK_REASON_USING_CACHED
        except DEFENSIVE_EXC:
            if cached_shortlist:
                shortlist = list(cached_shortlist)
                source = "cached"
                fallback_reason = FALLBACK_REASON_REFRESH_EXCEPTION
                LOG.exception("shortlist refresh failed, using cached shortlist")
            else:
                fallback_reason = FALLBACK_REASON_REFRESH_EXCEPTION
                LOG.exception("shortlist refresh failed, using pinned fallback")

        shortlist = _apply_shortlist_tenure(bot, shortlist, now=now)
        async with bot._shortlist_lock:
            bot._shortlist = shortlist
        bot._shortlist_source = source
        new_symbols = [item.symbol for item in shortlist]
        previous_symbols = list(getattr(bot, "_ws_subscribed_symbols", []) or [])
        if bot._ws_manager is not None and set(new_symbols) != set(previous_symbols):
            try:
                await bot._ws_manager.subscribe(new_symbols)
                bot._ws_subscribed_symbols = list(new_symbols)
                await bot._bus.publish(ShortlistUpdatedEvent(symbols=tuple(new_symbols)))
                LOG.info(
                    "ws resubscribed after shortlist refresh | symbols=%d added=%d removed=%d",
                    len(new_symbols),
                    len(set(new_symbols) - set(previous_symbols)),
                    len(set(previous_symbols) - set(new_symbols)),
                )
            except DEFENSIVE_EXC:
                LOG.exception("ws resubscribe failed after shortlist refresh")
        if bot._ws_manager is not None:
            try:
                await bot._sync_ws_tracked_symbols()
            except DEFENSIVE_EXC:
                LOG.debug("tracked-symbol sync after shortlist refresh failed", exc_info=True)
        if source == "pinned_fallback" and fallback_reason is None:
            fallback_reason = FALLBACK_REASON_USING_PINNED
        if (
            len(shortlist) < len(bot.settings.universe.pinned_symbols) + 3
            and source != "pinned_fallback"
        ):
            LOG.warning(
                "shortlist suspiciously small | size=%d source=%s "
                "eligible=%s - check universe filter thresholds in config.toml",
                len(shortlist),
                source,
                summary.get("eligible"),
            )

        bot.telemetry.append_jsonl(
            "shortlist.jsonl",
            {
                "ts": datetime.now(UTC).isoformat(),
                "source": source,
                "source_before": source_before,
                "source_after": source,
                "fallback_reason": normalize_shortlist_fallback_reason(fallback_reason),
                "full_refresh_due": full_refresh_due,
                "ws_cache_warm": ws_cache_warm,
                "has_symbol_meta": has_symbol_meta,
                "cached_shortlist_age_s": cached_shortlist_age_s,
                "cached_shortlist_size": len(cached_shortlist),
                "size": len(shortlist),
                "symbols": [item.symbol for item in shortlist[:20]],
                "gate_passed": summary.get("gate_passed"),
                "light_pool": summary.get("light_pool"),
                "light_pool_limit": summary.get("light_pool_limit"),
                "eligible": summary.get("eligible"),
                "dynamic_pool": summary.get("dynamic_pool"),
                "pinned": summary.get("pinned"),
                "mode": summary.get("mode", source),
                "avg_score": summary.get("avg_score"),
                "score_p25": summary.get("score_p25"),
                "score_p50": summary.get("score_p50"),
                "score_p75": summary.get("score_p75"),
                "score_p90": summary.get("score_p90"),
                "strategy_fit_density": summary.get("strategy_fit_density"),
                "strategy_seed": summary.get("strategy_seed"),
                "strategy_fit_counts": summary.get("strategy_fit_counts"),
                "top_scores": [
                    {
                        "symbol": item.symbol,
                        "score": item.shortlist_score,
                        "reasons": list(item.shortlist_reasons[:3]),
                        "strategy_fits": list(item.strategy_fits),
                        "seed_source": item.seed_source,
                    }
                    for item in shortlist[:5]
                ],
            },
        )
        bot.telemetry.append_jsonl(
            "shortlist_build.jsonl",
            {
                "ts": datetime.now(UTC).isoformat(),
                "stage": "refresh_complete",
                "source": source,
                "seed_source": summary.get("mode", source),
                "fallback_reason": normalize_shortlist_fallback_reason(fallback_reason),
                "gate_passed": summary.get("gate_passed"),
                "light_pool": summary.get("light_pool"),
                "light_pool_limit": summary.get("light_pool_limit"),
                "eligible": summary.get("eligible"),
                "dynamic_pool": summary.get("dynamic_pool"),
                "shortlist_size": len(shortlist),
                "pinned": summary.get("pinned"),
                "trend": summary.get("trend"),
                "breakout": summary.get("breakout"),
                "reversal": summary.get("reversal"),
                "strategy_seed": summary.get("strategy_seed"),
                "fill": summary.get("fill"),
                "avg_score": summary.get("avg_score"),
                "strategy_fit_density": summary.get("strategy_fit_density"),
                "radar": summary.get("radar"),
                "radar_tier_cycle": summary.get("radar_tier_cycle"),
            },
        )

        store = _radar_store(bot)
        if store is not None and bot.settings.universe.radar.enabled:
            radar_health = assess_radar_store(store, config=bot.settings.universe.radar)
            bot.telemetry.append_jsonl(
                "radar_health.jsonl",
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "source": source,
                    **radar_health,
                },
            )
            try:
                watch_summary = await emit_radar_watch_candidates(bot, store)
                summary = {**summary, "radar_watch": watch_summary}
            except DEFENSIVE_EXC:
                LOG.debug("radar watch emit failed", exc_info=True)

        LOG.info(
            (
                "shortlist refresh complete | source=%s mode=%s size=%d gate_passed=%s "
                "light_pool=%s eligible=%s dynamic_pool=%s pinned=%s avg_score=%s p25=%s "
                "p50=%s p75=%s p90=%s fit_density=%s"
            ),
            source,
            summary.get("mode", source),
            len(shortlist),
            _log_value(summary.get("gate_passed")),
            _log_value(summary.get("light_pool")),
            _log_value(summary.get("eligible")),
            _log_value(summary.get("dynamic_pool")),
            _log_value(summary.get("pinned")),
            _log_value(summary.get("avg_score")),
            _log_value(summary.get("score_p25")),
            _log_value(summary.get("score_p50")),
            _log_value(summary.get("score_p75")),
            _log_value(summary.get("score_p90")),
            _log_value(summary.get("strategy_fit_density")),
        )
        self._schedule_context_preload()
        return shortlist

    async def refresh_shortlist_periodic(self) -> None:
        bot = self._bot
        await asyncio.sleep(5)
        last_rerank_ts = 0.0
        rerank_interval = 30.0  # Rerank every 30s for better real-time relevance

        while not bot._shutdown.is_set():
            now = asyncio.get_event_loop().time()
            if now - last_rerank_ts >= rerank_interval:
                await self.do_rerank_shortlist()
                last_rerank_ts = now

            await self.do_refresh_shortlist()

            # Wait for either the rerank interval or the full light refresh interval
            refresh_interval = int(
                getattr(
                    bot.settings.universe,
                    "light_refresh_interval_seconds",
                    75,
                )
            )
            wait_timeout = max(5, min(rerank_interval, refresh_interval))

            try:
                await asyncio.wait_for(
                    bot._shutdown.wait(),
                    timeout=wait_timeout,
                )
            except TimeoutError:
                continue

    async def do_rerank_shortlist(self) -> None:
        """Lightweight reranking using WS data without full rebuild."""
        bot = self._bot
        ws = getattr(bot, "_ws_manager", None)
        if ws is None or not ws.is_ticker_cache_warm():
            return

        async with bot._shortlist_lock:
            current_shortlist = list(bot._shortlist)
        if not current_shortlist:
            return

        current_symbols = {item.symbol for item in current_shortlist}
        raw_tickers = [
            row
            for row in ws.get_global_ticker_data()
            if str(row.get("symbol") or "").strip().upper() in current_symbols
        ]
        if not raw_tickers:
            return

        tickers = self._enrich_shortlist_rows(raw_tickers)
        original_top = [s.symbol for s in current_shortlist[:5]]
        outcome_penalties = await _outcome_derank_penalties(bot)
        reranked = rerank_shortlist(
            current_shortlist,
            tickers,
            bot.settings,
            outcome_penalties=outcome_penalties,
        )
        new_top = [s.symbol for s in reranked[:5]]

        async with bot._shortlist_lock:
            latest_symbols = [s.symbol for s in bot._shortlist]
            if latest_symbols == [s.symbol for s in current_shortlist]:
                bot._shortlist = reranked
            else:
                bot._shortlist = rerank_shortlist(
                    list(bot._shortlist),
                    tickers,
                    bot.settings,
                    outcome_penalties=outcome_penalties,
                )

        if original_top != new_top:
            LOG.debug(
                "shortlist reranked | top_before=%s top_after=%s",
                original_top,
                new_top,
            )


async def run_shortlist_refresh_loop(service: ShortlistService) -> None:
    """Background shortlist rerank/refresh loop (started from SignalBot.run_forever)."""
    await service.refresh_shortlist_periodic()
