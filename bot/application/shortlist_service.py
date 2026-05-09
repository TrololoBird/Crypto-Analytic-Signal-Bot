"""Shortlist refresh helpers for SignalBot."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from ..market_data import BinanceFuturesMarketData
from ..models import UniverseSymbol
from ..universe import build_shortlist

UTC = timezone.utc
LOG = logging.getLogger("bot.application.shortlist_service")

FALLBACK_REASON_WS_CACHE_COLD = "ws_cache_cold"
FALLBACK_REASON_FULL_REFRESH_DUE = "full_refresh_due"
FALLBACK_REASON_REFRESH_EXCEPTION = "refresh_exception"
FALLBACK_REASON_LIVE_EMPTY = "live_empty"
FALLBACK_REASON_USING_CACHED = "using_cached"
FALLBACK_REASON_USING_PINNED = "using_pinned"
FALLBACK_REASON_UNKNOWN = "unknown"


def normalize_shortlist_fallback_reason(value: object) -> str | None:
    raw = str(value or "").strip().lower()
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
        "full_refresh_due": FALLBACK_REASON_FULL_REFRESH_DUE,
        "using_pinned": FALLBACK_REASON_USING_PINNED,
        "pinned": FALLBACK_REASON_USING_PINNED,
        "pinned_fallback": FALLBACK_REASON_USING_PINNED,
    }
    return aliases.get(raw, FALLBACK_REASON_UNKNOWN)


class ShortlistService:
    """Encapsulates shortlist build/refresh lifecycle for ``SignalBot``."""

    def __init__(self, bot: Any) -> None:
        self._bot = bot

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
        if bid is None or ask is None or bid <= 0.0 or ask <= 0.0 or ask < bid:
            return None
        mid = (bid + ask) / 2.0
        if mid <= 0.0:
            return None
        return ((ask - bid) / mid) * 10_000.0

    def _enrich_shortlist_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        bot = self._bot
        enriched: list[dict[str, Any]] = []
        ws = getattr(bot, "_ws_manager", None)
        client = bot.client
        open_interest_cache = getattr(client, "_open_interest_cache", {}) if isinstance(client, BinanceFuturesMarketData) else {}

        for raw in rows:
            row = dict(raw)
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol:
                continue

            if ws is not None:
                try:
                    ticker_age = ws.get_ticker_age_seconds(symbol)
                    if ticker_age is not None:
                        row["ticker_age_seconds"] = float(ticker_age)
                except Exception as exc:
                    LOG.debug("shortlist ticker age unavailable | symbol=%s error=%s", symbol, exc)
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
                except Exception as exc:
                    LOG.debug("shortlist mark price unavailable | symbol=%s error=%s", symbol, exc)
                try:
                    bid, ask = ws.get_book_snapshot(symbol)
                    spread_bps = self._spread_bps(bid, ask)
                    if spread_bps is not None:
                        row["spread_bps"] = spread_bps
                    book_age = ws.get_book_ticker_age_seconds(symbol)
                    if book_age is not None:
                        row["book_age_seconds"] = float(book_age)
                except Exception as exc:
                    LOG.debug("shortlist book snapshot unavailable | symbol=%s error=%s", symbol, exc)

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
                if "top_account_ls_ratio" in row and "global_account_ls_ratio" in row:
                    row["top_vs_global_ls_gap"] = float(row["top_account_ls_ratio"]) - float(row["global_account_ls_ratio"])
                basis_pct = client.get_cached_basis(symbol, period="1h")
                if basis_pct is not None:
                    row["basis_pct"] = float(basis_pct)
                cached_oi = open_interest_cache.get(symbol)
                if cached_oi is not None:
                    _ts, oi_value = cached_oi
                    row["oi_current"] = float(oi_value)
            enriched.append(row)
        return enriched

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
            merged.append(row)
        return merged

    async def fetch_symbols_with_retry(self, *, max_retries: int = 1) -> list[Any]:
        for attempt in range(max_retries + 1):
            try:
                return await asyncio.wait_for(
                    self._bot.client.fetch_exchange_symbols(),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                LOG.warning("fetch_exchange_symbols attempt %d/%d timed out", attempt + 1, max_retries + 1)
                if attempt < max_retries:
                    await asyncio.sleep(1.0)
                else:
                    raise
            except Exception as exc:
                LOG.warning("fetch_exchange_symbols attempt %d/%d failed: %s", attempt + 1, max_retries + 1, exc)
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
                cache_map = {
                    str(getattr(row, "symbol", "")).strip().upper(): row
                    for row in rows
                }
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
                LOG.warning(
                    "skipping pinned symbol due to unresolved base/quote assets | symbol=%s configured_quote_asset=%s",
                    symbol,
                    bot.settings.universe.quote_asset,
                )
                continue
            shortlist.append(
                UniverseSymbol(
                    symbol=symbol,
                    base_asset=base_asset,
                    quote_asset=quote_asset,
                    contract_type="PERPETUAL",
                    status="TRADING",
                    onboard_date_ms=0,
                    quote_volume=0.0,
                    price_change_pct=0.0,
                    last_price=0.0,
                    shortlist_bucket="pinned",
                    shortlist_score=1.0,
                    shortlist_reasons=("pinned_symbol",),
                    seed_source="pinned_fallback",
                )
            )
        return shortlist

    async def build_live_shortlist(self) -> tuple[list[UniverseSymbol], dict[str, Any]]:
        bot = self._bot
        timeout_s = max(10.0, float(bot.settings.ws.rest_timeout_seconds) * 2.0)
        symbol_meta_list, tickers_24h, premium_by_symbol = await asyncio.wait_for(
            asyncio.gather(
                self.fetch_symbols_with_retry(max_retries=1),
                bot.client.fetch_ticker_24h(),
                bot.client.fetch_premium_index_all(),
            ),
            timeout=timeout_s,
        )
        bot._symbol_meta_by_symbol = {
            str(getattr(row, "symbol", "")).strip().upper(): row for row in symbol_meta_list
        }
        shortlist, summary = build_shortlist(
            symbol_meta_list,
            self._enrich_shortlist_rows(
                self._merge_premium_index_rows(list(tickers_24h), premium_by_symbol)
            ),
            bot.settings,
            seed_source="rest_full",
        )
        return shortlist, summary

    async def build_light_shortlist(self) -> tuple[list[UniverseSymbol], dict[str, Any]]:
        bot = self._bot
        ws = getattr(bot, "_ws_manager", None)
        if ws is None or not bot._symbol_meta_by_symbol or not ws.is_ticker_cache_warm():
            return [], {"mode": "ws_light", "eligible": 0, "dynamic_pool": 0, "pinned": 0}

        tickers = self._enrich_shortlist_rows(ws.get_global_ticker_data())
        shortlist, summary = build_shortlist(
            list(bot._symbol_meta_by_symbol.values()),
            tickers,
            bot.settings,
            seed_source="ws_light",
        )
        return shortlist, summary

    async def do_refresh_shortlist(self) -> list[UniverseSymbol]:
        bot = self._bot
        LOG.info("refreshing shortlist...")

        source_before = str(getattr(bot, "_shortlist_source", "") or "")
        source = "pinned_fallback"
        summary: dict[str, Any] = {}
        shortlist = self.build_pinned_shortlist()
        now = datetime.now(UTC)
        full_interval = int(getattr(bot.settings.universe, "full_refresh_interval_seconds", bot.settings.runtime.shortlist_refresh_interval_seconds))
        last_full = getattr(bot, "_last_shortlist_full_refresh_at", None)
        full_refresh_due = last_full is None or (now - last_full).total_seconds() >= full_interval
        ws = getattr(bot, "_ws_manager", None)
        try:
            ws_cache_warm = bool(ws is not None and ws.is_ticker_cache_warm())
        except Exception:
            ws_cache_warm = False
        has_symbol_meta = bool(getattr(bot, "_symbol_meta_by_symbol", None))
        cached_shortlist = list(getattr(bot, "_last_live_shortlist", []) or [])
        cached_at = getattr(bot, "_last_live_shortlist_at", None)
        cached_shortlist_age_s = (
            max(0.0, (now - cached_at).total_seconds())
            if isinstance(cached_at, datetime)
            else None
        )
        fallback_reason: str | None = None

        try:
            live_shortlist: list[UniverseSymbol] = []
            live_summary: dict[str, Any] = {}
            if not full_refresh_due:
                live_shortlist, live_summary = await self.build_light_shortlist()
                if live_shortlist:
                    source = "ws_light"
                elif cached_shortlist:
                    live_shortlist = list(cached_shortlist)
                    live_summary = {"mode": "cached"}
                    source = "cached"
                    fallback_reason = FALLBACK_REASON_USING_CACHED
                elif not ws_cache_warm or not has_symbol_meta:
                    fallback_reason = FALLBACK_REASON_WS_CACHE_COLD
            if full_refresh_due or (not live_shortlist and not cached_shortlist):
                if full_refresh_due:
                    fallback_reason = FALLBACK_REASON_FULL_REFRESH_DUE
                live_shortlist, live_summary = await self.build_live_shortlist()
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
        except Exception as exc:
            if cached_shortlist:
                shortlist = list(cached_shortlist)
                source = "cached"
                fallback_reason = FALLBACK_REASON_REFRESH_EXCEPTION
                LOG.warning("shortlist refresh failed, using cached shortlist: %s", exc)
            else:
                fallback_reason = FALLBACK_REASON_REFRESH_EXCEPTION
                LOG.warning("shortlist refresh failed, using pinned fallback: %s", exc)

        async with bot._shortlist_lock:
            bot._shortlist = shortlist
        bot._shortlist_source = source
        if source == "pinned_fallback" and fallback_reason is None:
            fallback_reason = FALLBACK_REASON_USING_PINNED

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
                "eligible": summary.get("eligible"),
                "dynamic_pool": summary.get("dynamic_pool"),
                "pinned": summary.get("pinned"),
                "mode": summary.get("mode", source),
                "avg_score": summary.get("avg_score"),
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

        LOG.info(
            "shortlist refresh complete | source=%s mode=%s size=%d eligible=%s dynamic_pool=%s pinned=%s avg_score=%s",
            source,
            summary.get("mode", source),
            len(shortlist),
            summary.get("eligible"),
            summary.get("dynamic_pool"),
            summary.get("pinned"),
            summary.get("avg_score"),
        )
        self._schedule_context_preload()
        return shortlist

    async def refresh_shortlist_periodic(self) -> None:
        bot = self._bot
        await asyncio.sleep(5)
        while not bot._shutdown.is_set():
            await self.do_refresh_shortlist()
            try:
                await asyncio.wait_for(
                    bot._shutdown.wait(),
                    timeout=max(
                        15,
                        int(getattr(bot.settings.universe, "light_refresh_interval_seconds", 75)),
                    ),
                )
            except asyncio.TimeoutError:
                continue
