from __future__ import annotations

from bot.runtime.analyzer.common import *  # noqa: F403
from bot.runtime.analyzer.common import _history_fetch_limit


class AnalyzerFramesMixin:
    async def fetch_frames(self, item: UniverseSymbol) -> SymbolFrames | None:
        symbol = item.symbol
        minimums = self._minimums()
        limit_5m = _history_fetch_limit(minimums, "5m")
        limit_15m = _history_fetch_limit(minimums, "15m")
        limit_1h = _history_fetch_limit(minimums, "1h")
        limit_4h = _history_fetch_limit(minimums, "4h")

        ws_5m = ws_15m = ws_1h = None
        ws_bid = ws_ask = None
        if self._bot._ws_manager is not None:
            ws_frames = await self._bot._ws_manager.get_symbol_frames(symbol)
            if ws_frames is not None:
                ws_5m = ws_frames.df_5m
                ws_15m = ws_frames.df_15m
                ws_1h = ws_frames.df_1h
                ws_bid = ws_frames.bid_price
                ws_ask = ws_frames.ask_price

        try:
            if isinstance(self._bot.client, BinanceFuturesMarketData):
                df_4h = await self._bot.client.fetch_klines_cached(symbol, "4h", limit=limit_4h)
                df_1h = (
                    ws_1h
                    if ws_1h is not None and ws_1h.height >= minimums["1h"]
                    else await self._bot.client.fetch_klines_cached(symbol, "1h", limit=limit_1h)
                )
                df_15m = (
                    ws_15m
                    if ws_15m is not None and ws_15m.height >= minimums["15m"]
                    else await self._bot.client.fetch_klines_cached(symbol, "15m", limit=limit_15m)
                )
                df_5m = (
                    ws_5m
                    if ws_5m is not None and ws_5m.height >= minimums["5m"]
                    else await self._bot.client.fetch_klines_cached(symbol, "5m", limit=limit_5m)
                )

                bid, ask = ws_bid, ws_ask
                bid_qty = ask_qty = None
                if self._bot._ws_manager is not None:
                    qty = getattr(self._bot._ws_manager, "_book_qty", {}).get(symbol)
                    if isinstance(qty, tuple):
                        bid_qty, ask_qty = qty[:2]
                depth_context_needed = (
                    bid is None or ask is None or bid_qty is None or ask_qty is None
                )
                if self._bot._ws_manager is not None:
                    depth_age_getter = getattr(
                        self._bot._ws_manager,
                        "get_depth_book_age_seconds",
                        None,
                    )
                    if callable(depth_age_getter):
                        depth_age = depth_age_getter(symbol)
                        max_age = self._bot.settings.ws.market_ticker_freshness_seconds
                        if depth_age is None or depth_age > max_age:
                            depth_context_needed = True
                if depth_context_needed:
                    try:
                        book_context = await self._bot.client.fetch_order_book_depth_snapshot(
                            symbol,
                            limit=20,
                        )
                    except MarketDataUnavailable as exc:
                        LOG.info(
                            "order book depth fallback unavailable | symbol=%s detail=%s",
                            symbol,
                            exc.detail,
                        )
                        book_context = await self._bot.client._fetch_book_ticker_rest_detail(
                            symbol
                        )
                    bid = bid if bid is not None else book_context.get("bid_price")
                    ask = ask if ask is not None else book_context.get("ask_price")
                    bid_qty = book_context.get("bid_qty")
                    ask_qty = book_context.get("ask_qty")

                return SymbolFrames(
                    symbol=symbol,
                    df_1h=df_1h,
                    df_15m=df_15m,
                    bid_price=bid,
                    ask_price=ask,
                    df_5m=df_5m,
                    df_4h=df_4h,
                    bid_qty=bid_qty,
                    ask_qty=ask_qty,
                )

            return await cast(Any, self._bot.client.fetch_symbol_frames(symbol))
        except MarketDataUnavailable as exc:
            LOG.info("frame fetch failed for %s: %s", symbol, exc)
            return None
        except Exception:
            LOG.exception("unexpected frame fetch failure for %s", symbol)
            raise

    async def preload_shortlist_frames(self) -> None:
        await asyncio.sleep(1.0)
        if not isinstance(self._bot.client, BinanceFuturesMarketData):
            return
        minimums = self._minimums()
        async with self._bot._shortlist_lock:
            shortlist = list(self._bot._shortlist)
        if not shortlist:
            return

        batch_size = int(self._bot.settings.runtime.startup_batch_size)
        batch_delay = float(self._bot.settings.runtime.startup_batch_delay_seconds)
        sem = asyncio.Semaphore(int(self._bot.settings.runtime.max_concurrent_rest_requests))

        async def _preload_one(symbol: str) -> None:
            async with sem:
                try:
                    await self._bot.client.fetch_klines_cached(
                        symbol, "5m", limit=_history_fetch_limit(minimums, "5m")
                    )
                    await self._bot.client.fetch_klines_cached(
                        symbol, "1h", limit=_history_fetch_limit(minimums, "1h")
                    )
                    await self._bot.client.fetch_klines_cached(
                        symbol, "15m", limit=_history_fetch_limit(minimums, "15m")
                    )
                    await self._bot.client.fetch_klines_cached(
                        symbol, "4h", limit=_history_fetch_limit(minimums, "4h")
                    )
                except (
                    MarketDataUnavailable,
                    RuntimeError,
                    ValueError,
                    TypeError,
                ) as exc:
                    self._log_degradation(
                        level=logging.INFO,
                        symbol=symbol,
                        stage="shortlist_preload",
                        source="rest",
                        reason=str(exc),
                        fallback_used="skip_symbol_preload",
                        exception_type=type(exc).__name__,
                    )

        for i in range(0, len(shortlist), batch_size):
            batch = shortlist[i : i + batch_size]
            await asyncio.gather(
                *[_preload_one(item.symbol) for item in batch], return_exceptions=True
            )
            if i + batch_size < len(shortlist):
                await asyncio.sleep(batch_delay)

    def ws_cache_enrichments(self, symbol: str) -> dict[str, Any]:
        enrichments: dict[str, Any] = {}
        context_ages: list[float] = []
        freshness_flags: set[str] = set()
        degradation_events: list[dict[str, Any]] = []
        if self._bot._ws_manager is not None:
            max_age = self._bot.settings.ws.market_ticker_freshness_seconds

            ticker = self._safe_ws_get(symbol, "get_ticker_snapshot")
            ticker_age = self._safe_ws_get(symbol, "get_ticker_age_seconds")
            if ticker:
                ticker_price = float(ticker.get("last_price") or 0.0)
                if ticker_price > 0.0:
                    enrichments["ticker_price"] = ticker_price
                if ticker_age is not None:
                    ticker_age = float(ticker_age)
                    enrichments["ticker_price_age_seconds"] = ticker_age
                    context_ages.append(ticker_age)
                    if ticker_age > max_age:
                        freshness_flags.add("ticker_price_stale")
            else:
                freshness_flags.add("ticker_price_missing")

            mark = self._safe_ws_get(symbol, "get_mark_price_snapshot")
            mark_age = self._safe_ws_get(symbol, "get_mark_price_age_seconds")
            if mark:
                mark_price = float(mark.get("mark_price") or 0.0)
                index_price = float(mark.get("index_price") or 0.0)
                if mark_price > 0.0:
                    enrichments["mark_price"] = mark_price
                if "funding_rate" in mark:
                    enrichments["funding_rate"] = float(mark.get("funding_rate") or 0.0)
                if mark_price > 0.0 and index_price > 0.0:
                    basis_pct = (mark_price - index_price) / index_price * 100.0
                    enrichments["basis_pct"] = basis_pct
                    enrichments["mark_index_spread_bps"] = basis_pct * 100.0
            elif mark_age is None:
                freshness_flags.add("mark_price_missing")
            if mark_age is not None:
                mark_age = float(mark_age)
                enrichments["mark_price_age_seconds"] = mark_age
                context_ages.append(mark_age)
                if mark_age > max_age:
                    freshness_flags.add("mark_price_stale")
                    degrade_reason = "mark_price_stale"
                    if mark is not None and "funding_rate" not in mark:
                        degrade_reason = "mark_price_stale_funding_missing"
                    degradation_events.append(
                        self._degrade_event(
                            symbol=symbol,
                            stage="mark_snapshot",
                            source="ws",
                            reason=degrade_reason,
                            fallback_used="rest_cached_funding",
                            exception_type="stale_cache",
                        )
                    )
                    self._log_degradation(
                        level=logging.INFO,
                        symbol=symbol,
                        stage="mark_snapshot",
                        source="ws",
                        reason=degrade_reason,
                        fallback_used="rest_cached_funding",
                        exception_type="stale_cache",
                    )

            depth_imbalance = self._safe_ws_get(symbol, "get_depth_imbalance")
            if depth_imbalance is not None:
                enrichments["depth_imbalance"] = float(depth_imbalance)
                depth_source = self._safe_ws_get(symbol, "get_depth_imbalance_source")
                if depth_source:
                    enrichments["depth_imbalance_source"] = str(depth_source)
            microprice_bias = self._safe_ws_get(symbol, "get_microprice_bias")
            if microprice_bias is not None:
                enrichments["microprice_bias"] = float(microprice_bias)
                micro_source = self._safe_ws_get(symbol, "get_microprice_bias_source")
                if micro_source:
                    enrichments["microprice_bias_source"] = str(micro_source)
            depth_age = self._safe_ws_get(symbol, "get_depth_book_age_seconds")
            if depth_age is not None:
                depth_age = float(depth_age)
                enrichments["depth_book_age_seconds"] = depth_age
                context_ages.append(depth_age)
                if depth_age > max_age:
                    freshness_flags.add("depth_book_stale")
            wall_pressure = self._safe_ws_get(symbol, "get_depth_wall_pressure")
            if wall_pressure is not None:
                enrichments["depth_wall_pressure"] = float(wall_pressure)

            short_flow = self._safe_ws_get(symbol, "get_agg_trade_snapshot", window_seconds=30)
            long_flow = self._safe_ws_get(symbol, "get_agg_trade_snapshot", window_seconds=300)
            if short_flow is not None and short_flow.delta_ratio is not None:
                enrichments["agg_trade_delta_30s"] = float(short_flow.delta_ratio)
                enrichments["orderflow_source"] = "agg_trade"
            if (
                short_flow is not None
                and long_flow is not None
                and short_flow.delta_ratio is not None
                and long_flow.delta_ratio is not None
            ):
                enrichments["aggression_shift"] = float(
                    short_flow.delta_ratio - long_flow.delta_ratio
                )
                enrichments["orderflow_source"] = "agg_trade"

            liquidation = self._safe_ws_get(
                symbol,
                "get_liquidation_sentiment",
                window_seconds=900,
            )
            if liquidation is not None:
                enrichments["liquidation_score"] = float(liquidation)
                enrichments["liquidation_score_source"] = "force_order"
                liq_age = self._safe_ws_get(
                    symbol,
                    "get_liquidation_age_seconds",
                    window_seconds=900,
                )
                if liq_age is not None:
                    liq_age = float(liq_age)
                    enrichments["liquidation_score_age_seconds"] = liq_age
                    context_ages.append(liq_age)

        if isinstance(self._bot.client, BinanceFuturesMarketData):
            premium = self._bot.client.get_cached_premium_index(symbol)
            if premium is not None:
                mark_price = float(premium.get("mark_price") or 0.0)
                index_price = float(premium.get("index_price") or 0.0)
                if "funding_rate" not in enrichments:
                    enrichments["funding_rate"] = float(premium.get("funding_rate") or 0.0)
                if "mark_price" not in enrichments and mark_price > 0.0:
                    enrichments["mark_price"] = mark_price
                if "basis_pct" not in enrichments and mark_price > 0.0 and index_price > 0.0:
                    basis_pct = (mark_price - index_price) / index_price * 100.0
                    enrichments["basis_pct"] = basis_pct
                    enrichments.setdefault("mark_index_spread_bps", basis_pct * 100.0)
            elif "funding_rate" not in enrichments:
                funding_rate = self._bot.client.get_cached_funding_rate(symbol)
                if funding_rate is not None:
                    enrichments["funding_rate"] = funding_rate

            oi_current = self._bot.client.get_cached_open_interest(symbol)
            if oi_current is not None:
                enrichments["oi_current"] = oi_current

            oi_chg = self._bot.client.get_cached_oi_change(symbol)
            if oi_chg is not None:
                enrichments["oi_change_pct"] = oi_chg
            else:
                freshness_flags.add("oi_change_missing")
            ls = self._bot.client.get_cached_ls_ratio(symbol)
            if ls is not None:
                enrichments["ls_ratio"] = ls
                enrichments["top_account_ls_ratio"] = ls
            else:
                freshness_flags.add("ls_ratio_missing")
            top_position_ls = self._bot.client.get_cached_top_position_ls_ratio(symbol)
            if top_position_ls is not None:
                enrichments["top_position_ls_ratio"] = top_position_ls
                enrichments["top_trader_position_ratio"] = top_position_ls
            taker = self._bot.client.get_cached_taker_ratio(symbol)
            if taker is not None:
                enrichments["taker_ratio"] = taker
            global_ls = self._bot.client.get_cached_global_ls_ratio(symbol)
            if global_ls is not None:
                enrichments["global_ls_ratio"] = global_ls
                enrichments["global_account_ls_ratio"] = global_ls
            funding_trend = self._bot.client.get_cached_funding_trend(symbol)
            if funding_trend is not None:
                enrichments["funding_trend"] = funding_trend
                recent_extreme = self._bot.client.get_cached_funding_recent_extreme(symbol)
                if recent_extreme is not None:
                    extreme_rate, extreme_age_hours = recent_extreme
                    enrichments["funding_recent_extreme_rate"] = extreme_rate
                    enrichments["funding_recent_extreme_age_hours"] = extreme_age_hours
            else:
                freshness_flags.add("funding_trend_missing")
            cached_basis_pct = self._bot.client.get_cached_basis(symbol, period="1h")
            if cached_basis_pct is not None:
                enrichments["basis_pct"] = cached_basis_pct
            basis_stats = self._bot.client.get_cached_basis_stats(symbol, period="5m")
            if basis_stats is not None:
                premium_slope = basis_stats.get("premium_slope_5m")
                if premium_slope is not None:
                    enrichments["premium_slope_5m"] = float(cast(Any, premium_slope))
                premium_zscore = basis_stats.get("premium_zscore_5m")
                if premium_zscore is not None:
                    enrichments["premium_zscore_5m"] = float(cast(Any, premium_zscore))
                mark_spread = basis_stats.get("mark_index_spread_bps")
                if mark_spread is not None:
                    enrichments["mark_index_spread_bps"] = float(cast(Any, mark_spread))
            top = enrichments.get("ls_ratio")
            global_ls = enrichments.get("global_ls_ratio")
            if isinstance(top, (int, float)) and isinstance(global_ls, (int, float)):
                enrichments["top_vs_global_ls_gap"] = float(top) - float(global_ls)
            else:
                freshness_flags.add("crowding_context_missing")

        if context_ages:
            enrichments["context_snapshot_age_seconds"] = max(context_ages)
        if freshness_flags:
            enrichments["data_freshness_flags"] = tuple(sorted(freshness_flags))
        if degradation_events:
            primary = degradation_events[0]
            enrichments["degraded"] = True
            enrichments["degrade_reason"] = primary["degrade_reason"]
            enrichments["fallback_used"] = primary["fallback_used"]
            enrichments["degradation_events"] = tuple(degradation_events)
        enrichments.setdefault("data_source_mix", "futures_only")
        return enrichments

    def refresh_universe_symbol_from_ws(self, item: UniverseSymbol) -> UniverseSymbol:
        if self._bot._ws_manager is None:
            return item
        ticker = self._bot._ws_manager.get_ticker_snapshot(item.symbol)
        ticker_age = self._bot._ws_manager.get_ticker_age_seconds(item.symbol)
        if (
            not ticker
            or ticker_age is None
            or ticker_age > self._bot.settings.ws.market_ticker_freshness_seconds
        ):
            return item

        next_last_price = item.last_price
        try:
            ticker_last_price = float(ticker.get("last_price") or 0.0)
        except (TypeError, ValueError):
            return item
        if ticker_last_price > 0:
            next_last_price = ticker_last_price

        if next_last_price == item.last_price:
            return item
        return replace(item, last_price=next_last_price)
