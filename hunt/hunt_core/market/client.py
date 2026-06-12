"""Hunter REST market client — CCXT binanceusdm (public only)."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any

import ccxt.async_support as ccxt
import polars as pl

from engine.domain.schemas import AggTradeSnapshot, SymbolMeta
from hunt_core.market.frames import ccxt_ohlcv_to_frame, finalize_kline_frame
from hunt_core.market.symbols import from_ccxt_symbol, to_binance_symbol, to_ccxt_symbol

LOG = logging.getLogger("hunt_core.market.client")

_CACHE_TTL: dict[str, int] = {
    "klines_1m": 50,
    "klines_3m": 180,
    "klines_5m": 300,
    "klines_15m": 900,
    "klines_1h": 3900,
    "klines_4h": 14400,
    "klines_1d": 3600,
    "open_interest": 600,
    "open_interest_change": 600,
    "metric_series": 240,
    "long_short_ratio": 600,
    "taker_ratio": 600,
    "global_ls_ratio": 600,
    "funding_rate": 300,
    "funding_history": 1800,
    "funding_info": 3600,
    "basis": 1800,
    "book_ticker": 5,
    "order_book_depth": 5,
    "ticker_24h": 300,
    "exchange_info": 3600,
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _parse_metric_series(payload: Any, *, keys: tuple[str, ...]) -> list[float]:
    if not payload:
        return []
    rows = [dict(item) if isinstance(item, dict) else {} for item in payload]
    rows.sort(key=lambda row: int(row.get("timestamp") or 0))
    series: list[float] = []
    for row in rows:
        raw = next((row[k] for k in keys if row.get(k) is not None), None)
        if raw is None:
            continue
        try:
            series.append(float(raw))
        except (TypeError, ValueError):
            continue
    return series


class HuntCcxtClient:
    """Drop-in for ``BinanceFuturesMarketData`` across hunt runtime/scripts."""

    def __init__(
        self,
        *,
        proxy_url: str | None = None,
        trust_env: bool = True,
        timeout_ms: int = 45_000,
    ) -> None:
        self._proxy_url = proxy_url
        self._trust_env = trust_env
        config: dict[str, Any] = {
            "enableRateLimit": True,
            "timeout": timeout_ms,
            "options": {"defaultType": "future", "adjustForTimeDifference": True},
        }
        if proxy_url:
            config["aiohttp_proxy"] = proxy_url
            config["proxies"] = {"http": proxy_url, "https": proxy_url}
        if trust_env:
            config["aiohttp_trust_env"] = True
        self._ex: ccxt.binanceusdm = ccxt.binanceusdm(config)
        self._markets_loaded = False
        self._klines_cache: dict[tuple[str, str, int], tuple[float, pl.DataFrame]] = {}
        self._klines_locks: dict[tuple[str, str, int], asyncio.Lock] = {}
        self._ticker_24h_cache: tuple[float, list[dict[str, float | str]]] | None = None
        self._exchange_info_cache: tuple[float, list[SymbolMeta]] | None = None
        self._open_interest_cache: dict[str, tuple[float, float]] = {}
        self._open_interest_change_cache: dict[tuple[str, str], tuple[float, float]] = {}
        self._long_short_ratio_cache: dict[tuple[str, str], tuple[float, float]] = {}
        self._top_position_ls_ratio_cache: dict[tuple[str, str], tuple[float, float]] = {}
        self._taker_ratio_cache: dict[tuple[str, str], tuple[float, float]] = {}
        self._global_ls_ratio_cache: dict[tuple[str, str], tuple[float, float]] = {}
        self._funding_rate_cache: dict[str, tuple[float, float]] = {}
        self._funding_history_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._funding_info_all_cache: tuple[float, dict[str, dict[str, float | int]]] | None = None
        self._premium_index_all_cache: tuple[float, dict[str, dict[str, float]]] | None = None
        self._basis_cache: dict[tuple[str, str], tuple[float, float]] = {}
        self._basis_stats_cache: dict[tuple[str, str], tuple[float, dict[str, float | None]]] = {}
        self._oi_series_cache: dict[tuple[str, str, int], tuple[float, list[float]]] = {}
        self._gls_series_cache: dict[tuple[str, str, int], tuple[float, list[float]]] = {}
        self._order_book_depth_cache: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}

    @classmethod
    def from_settings(cls, settings: Any) -> HuntCcxtClient:
        net = getattr(settings, "network", settings)
        return cls(
            proxy_url=getattr(net, "proxy_url", None),
            trust_env=getattr(net, "trust_env", True),
        )

    @property
    def exchange(self) -> ccxt.binanceusdm:
        return self._ex

    async def load_markets(self) -> None:
        if not self._markets_loaded:
            await self._ex.load_markets()
            self._markets_loaded = True

    async def close(self) -> None:
        await self._ex.close()

    def _ccxt_sym(self, symbol: str) -> str:
        return to_ccxt_symbol(symbol, markets=self._ex.markets if self._markets_loaded else None)

    def _bin_sym(self, symbol: str) -> str:
        return to_binance_symbol(symbol)

    @staticmethod
    def _cache_fresh(entry: tuple[float, Any] | None, ttl: float) -> bool:
        return entry is not None and (time.monotonic() - entry[0]) < ttl

    async def fetch_exchange_symbols(self) -> list[SymbolMeta]:
        now = time.monotonic()
        if self._cache_fresh(self._exchange_info_cache, _CACHE_TTL["exchange_info"]):
            assert self._exchange_info_cache is not None
            return self._exchange_info_cache[1]
        await self.load_markets()
        raw = await self._ex.fapiPublicGetExchangeInfo()
        symbols = raw.get("symbols") if isinstance(raw, dict) else []
        rows = [
            SymbolMeta(
                symbol=str(item.get("symbol") or ""),
                base_asset=str(item.get("baseAsset") or ""),
                quote_asset=str(item.get("quoteAsset") or ""),
                contract_type=str(item.get("contractType") or ""),
                status=str(item.get("status") or ""),
                onboard_date_ms=int(item.get("onboardDate") or 0),
            )
            for item in symbols
            if isinstance(item, dict)
        ]
        self._exchange_info_cache = (now, rows)
        return rows

    async def fetch_ticker_24h(self) -> list[dict[str, float | str]]:
        now = time.monotonic()
        if self._cache_fresh(self._ticker_24h_cache, _CACHE_TTL["ticker_24h"]):
            assert self._ticker_24h_cache is not None
            return self._ticker_24h_cache[1]
        await self.load_markets()
        tickers = await self._ex.fetch_tickers()
        rows: list[dict[str, float | str]] = []
        for ccxt_sym, item in tickers.items():
            sym = from_ccxt_symbol(ccxt_sym)
            last_price = _safe_float(item.get("last"))
            quote_volume = _safe_float(item.get("quoteVolume"))
            if not sym or last_price <= 0 or quote_volume <= 0:
                continue
            row: dict[str, float | str] = {
                "symbol": sym,
                "last_price": last_price,
                "price_change_percent": _safe_float(item.get("percentage")),
                "quote_volume": quote_volume,
                "trade_count": _safe_float(item.get("info", {}).get("count")),
            }
            high = _safe_float(item.get("high"))
            low = _safe_float(item.get("low"))
            if high > 0:
                row["high_price"] = high
            if low > 0:
                row["low_price"] = low
            rows.append(row)
        self._ticker_24h_cache = (now, rows)
        return rows

    async def fetch_klines(self, symbol: str, interval: str, *, limit: int) -> pl.DataFrame:
        await self.load_markets()
        ccxt_sym = self._ccxt_sym(symbol)
        rows = await self._ex.fetch_ohlcv(ccxt_sym, interval, limit=max(1, int(limit)))
        frame = finalize_kline_frame(ccxt_ohlcv_to_frame(rows, interval), interval)
        return frame

    async def fetch_klines_between(
        self,
        symbol: str,
        interval: str,
        *,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1500,
    ) -> pl.DataFrame:
        await self.load_markets()
        ccxt_sym = self._ccxt_sym(symbol)
        rows = await self._ex.fetch_ohlcv(
            ccxt_sym,
            interval,
            since=max(0, int(start_time_ms)),
            limit=min(1500, max(1, int(limit))),
        )
        end_ms = max(0, int(end_time_ms))
        trimmed = [r for r in rows if r and int(r[0]) <= end_ms]
        return ccxt_ohlcv_to_frame(trimmed, interval)

    async def fetch_klines_cached(self, symbol: str, interval: str, *, limit: int) -> pl.DataFrame:
        key = (self._bin_sym(symbol), interval, int(limit))
        ttl = float(_CACHE_TTL.get(f"klines_{interval}", 60))
        now = time.monotonic()
        cached = self._klines_cache.get(key)
        if cached is not None and now - cached[0] < ttl:
            return cached[1]
        lock = self._klines_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._klines_cache.get(key)
            if cached is not None and now - cached[0] < ttl:
                return cached[1]
            frame = await self.fetch_klines(symbol, interval, limit=limit)
            self._klines_cache[key] = (time.monotonic(), frame)
            return frame

    def get_cached_klines(
        self,
        symbol: str,
        interval: str,
        *,
        limit: int,
        max_age_s: float | None = None,
    ) -> pl.DataFrame | None:
        key = (self._bin_sym(symbol), interval, int(limit))
        cached = self._klines_cache.get(key)
        if cached is None:
            return None
        ttl = max_age_s if max_age_s is not None else float(_CACHE_TTL.get(f"klines_{interval}", 60))
        if time.monotonic() - cached[0] > ttl:
            return None
        return cached[1]

    async def fetch_order_book_depth_snapshot(
        self, symbol: str, *, limit: int = 20
    ) -> dict[str, float | None]:
        sym = self._bin_sym(symbol)
        key = (sym, int(limit))
        now = time.monotonic()
        cached = self._order_book_depth_cache.get(key)
        if self._cache_fresh(cached, _CACHE_TTL["order_book_depth"]):
            return dict(cached[1])  # type: ignore[index]
        await self.load_markets()
        ob = await self._ex.fetch_order_book(self._ccxt_sym(sym), limit=min(100, max(5, int(limit))))
        bids = ob.get("bids") or []
        asks = ob.get("asks") or []
        if not bids or not asks:
            return {"bid_price": None, "ask_price": None, "bid_qty": None, "ask_qty": None}
        snapshot: dict[str, Any] = {
            "bid_price": float(bids[0][0]),
            "ask_price": float(asks[0][0]),
            "bid_qty": sum(float(q) for _p, q in bids),
            "ask_qty": sum(float(q) for _p, q in asks),
            "bid_levels": bids[:5],
            "ask_levels": asks[:5],
        }
        self._order_book_depth_cache[key] = (now, snapshot)
        return snapshot

    async def _fetch_book_ticker_rest_detail(self, symbol: str) -> dict[str, float | None]:
        depth = await self.fetch_order_book_depth_snapshot(symbol, limit=5)
        if depth.get("bid_price"):
            return depth
        return {"bid_price": None, "ask_price": None, "bid_qty": None, "ask_qty": None}

    async def fetch_open_interest(self, symbol: str) -> float | None:
        sym = self._bin_sym(symbol)
        now = time.monotonic()
        cached = self._open_interest_cache.get(sym)
        if self._cache_fresh(cached, _CACHE_TTL["open_interest"]):
            return cached[1]  # type: ignore[index]
        await self.load_markets()
        try:
            payload = await self._ex.fetch_open_interest(self._ccxt_sym(sym))
            value = _safe_float(payload.get("openInterestAmount") or payload.get("info", {}).get("openInterest"))
            if value > 0:
                self._open_interest_cache[sym] = (now, value)
                return value
        except Exception as exc:
            LOG.debug("fetch_open_interest failed | symbol=%s error=%s", sym, exc)
        return cached[1] if cached else None  # type: ignore[return-value]

    async def _fapi_ratio(
        self,
        method: str,
        symbol: str,
        *,
        period: str,
        limit: int,
        ratio_key: str,
    ) -> float | None:
        sym = self._bin_sym(symbol)
        await self.load_markets()
        fn = getattr(self._ex, method)
        payload = await fn({"symbol": sym, "period": period, "limit": limit})
        if not payload:
            return None
        item = payload[-1] if isinstance(payload, list) else payload
        raw = item.get(ratio_key) if isinstance(item, dict) else None
        return float(raw) if raw is not None else None

    async def fetch_open_interest_change(self, symbol: str, *, period: str = "1h") -> float | None:
        sym = self._bin_sym(symbol)
        cache_key = (sym, period)
        now = time.monotonic()
        cached = self._open_interest_change_cache.get(cache_key)
        if self._cache_fresh(cached, _CACHE_TTL["open_interest_change"]):
            return cached[1]  # type: ignore[index]
        try:
            payload = await self._ex.fapiDataGetOpenInterestHist(
                {"symbol": sym, "period": period, "limit": 2}
            )
            series = _parse_metric_series(payload, keys=("sumOpenInterest",))
            if len(series) < 2 or series[-2] <= 0:
                return None
            change = series[-1] / series[-2] - 1.0
            self._open_interest_change_cache[cache_key] = (now, change)
            return change
        except Exception as exc:
            LOG.debug("oi_change failed | symbol=%s period=%s error=%s", sym, period, exc)
            return cached[1] if cached else None  # type: ignore[return-value]

    async def fetch_long_short_ratio(self, symbol: str, *, period: str = "1h") -> float | None:
        cache_key = (self._bin_sym(symbol), period)
        cached = self._long_short_ratio_cache.get(cache_key)
        if self._cache_fresh(cached, _CACHE_TTL["long_short_ratio"]):
            return cached[1]  # type: ignore[index]
        try:
            value = await self._fapi_ratio(
                "fapiDataGetTopLongShortAccountRatio",
                symbol,
                period=period,
                limit=1,
                ratio_key="longShortRatio",
            )
            if value is not None:
                self._long_short_ratio_cache[cache_key] = (time.monotonic(), value)
            return value
        except Exception:
            return cached[1] if cached else None  # type: ignore[return-value]

    async def fetch_top_position_ls_ratio(self, symbol: str, *, period: str = "1h") -> float | None:
        cache_key = (self._bin_sym(symbol), period)
        cached = self._top_position_ls_ratio_cache.get(cache_key)
        if self._cache_fresh(cached, _CACHE_TTL["long_short_ratio"]):
            return cached[1]  # type: ignore[index]
        try:
            value = await self._fapi_ratio(
                "fapiDataGetTopLongShortPositionRatio",
                symbol,
                period=period,
                limit=1,
                ratio_key="longShortRatio",
            )
            if value is not None:
                self._top_position_ls_ratio_cache[cache_key] = (time.monotonic(), value)
            return value
        except Exception:
            return cached[1] if cached else None  # type: ignore[return-value]

    async def fetch_taker_ratio(self, symbol: str, *, period: str = "1h") -> float | None:
        cache_key = (self._bin_sym(symbol), period)
        cached = self._taker_ratio_cache.get(cache_key)
        if cached is not None and time.monotonic() - cached[0] < 1200:
            return cached[1]
        try:
            value = await self._fapi_ratio(
                "fapiDataGetTakerlongshortRatio",
                symbol,
                period=period,
                limit=1,
                ratio_key="buySellRatio",
            )
            if value is not None:
                self._taker_ratio_cache[cache_key] = (time.monotonic(), value)
            return value
        except Exception:
            return cached[1] if cached else None

    async def fetch_global_ls_ratio(self, symbol: str, *, period: str = "1h") -> float | None:
        cache_key = (self._bin_sym(symbol), period)
        cached = self._global_ls_ratio_cache.get(cache_key)
        if cached is not None and time.monotonic() - cached[0] < 1200:
            return cached[1]
        try:
            value = await self._fapi_ratio(
                "fapiDataGetGlobalLongShortAccountRatio",
                symbol,
                period=period,
                limit=1,
                ratio_key="longShortRatio",
            )
            if value is not None:
                self._global_ls_ratio_cache[cache_key] = (time.monotonic(), value)
            return value
        except Exception:
            return cached[1] if cached else None

    async def fetch_open_interest_series(
        self, symbol: str, *, period: str = "5m", limit: int = 48
    ) -> list[float]:
        sym = self._bin_sym(symbol)
        cache_key = (sym, period, int(limit))
        cached = self._oi_series_cache.get(cache_key)
        if self._cache_fresh(cached, _CACHE_TTL["metric_series"]):
            return cached[1]  # type: ignore[index]
        try:
            payload = await self._ex.fapiDataGetOpenInterestHist(
                {"symbol": sym, "period": period, "limit": int(limit)}
            )
            series = _parse_metric_series(payload, keys=("sumOpenInterest",))
            if series:
                self._oi_series_cache[cache_key] = (time.monotonic(), series)
            return series
        except Exception:
            return cached[1] if cached else []  # type: ignore[return-value]

    async def fetch_global_ls_series(
        self, symbol: str, *, period: str = "5m", limit: int = 48
    ) -> list[float]:
        sym = self._bin_sym(symbol)
        cache_key = (sym, period, int(limit))
        cached = self._gls_series_cache.get(cache_key)
        if self._cache_fresh(cached, _CACHE_TTL["metric_series"]):
            return cached[1]  # type: ignore[index]
        try:
            payload = await self._ex.fapiDataGetGlobalLongShortAccountRatio(
                {"symbol": sym, "period": period, "limit": int(limit)}
            )
            series = _parse_metric_series(payload, keys=("longShortRatio",))
            if series:
                self._gls_series_cache[cache_key] = (time.monotonic(), series)
            return series
        except Exception:
            return cached[1] if cached else []  # type: ignore[return-value]

    async def fetch_funding_rate(self, symbol: str) -> float | None:
        sym = self._bin_sym(symbol)
        now = time.monotonic()
        cached = self._funding_rate_cache.get(sym)
        if self._cache_fresh(cached, _CACHE_TTL["funding_rate"]):
            return cached[1]  # type: ignore[index]
        await self.load_markets()
        try:
            payload = await self._ex.fetch_funding_rate(self._ccxt_sym(sym))
            value = payload.get("fundingRate")
            if value is not None:
                rate = float(value)
                self._funding_rate_cache[sym] = (now, rate)
                return rate
        except Exception as exc:
            LOG.debug("fetch_funding_rate failed | symbol=%s error=%s", sym, exc)
        return cached[1] if cached else None  # type: ignore[return-value]

    async def fetch_funding_rate_history(
        self, symbol: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        sym = self._bin_sym(symbol)
        now = time.monotonic()
        cached = self._funding_history_cache.get(sym)
        if cached is not None and now - cached[0] < 900:
            return cached[1]
        await self.load_markets()
        try:
            payload = await self._ex.fetch_funding_rate_history(self._ccxt_sym(sym), limit=limit)
            rows: list[dict[str, Any]] = []
            for item in payload:
                rows.append(
                    {
                        "fundingTime": int(item.get("timestamp") or 0),
                        "fundingRate": float(item.get("fundingRate") or 0.0),
                        "markPrice": float(item.get("markPrice") or item.get("info", {}).get("markPrice") or 0.0),
                    }
                )
            rows.sort(key=lambda r: r["fundingTime"])
            self._funding_history_cache[sym] = (now, rows)
            return rows
        except Exception:
            return cached[1] if cached else []

    async def fetch_premium_index_all(self) -> dict[str, dict[str, float]]:
        now = time.monotonic()
        if self._cache_fresh(self._premium_index_all_cache, 30):
            assert self._premium_index_all_cache is not None
            return self._premium_index_all_cache[1]
        raw = await self._ex.fapiPublicGetPremiumIndex()
        rows: dict[str, dict[str, float]] = {}
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            sym = str(item.get("symbol") or "").upper()
            mark = _safe_float(item.get("markPrice"))
            index = _safe_float(item.get("indexPrice"))
            if not sym or mark <= 0:
                continue
            rows[sym] = {
                "mark_price": mark,
                "index_price": index,
                "last_funding_rate": _safe_float(item.get("lastFundingRate")),
            }
        self._premium_index_all_cache = (now, rows)
        return rows

    async def fetch_funding_info_all(self) -> dict[str, dict[str, float | int]]:
        now = time.monotonic()
        if self._cache_fresh(self._funding_info_all_cache, _CACHE_TTL["funding_info"]):
            assert self._funding_info_all_cache is not None
            return self._funding_info_all_cache[1]
        raw = await self._ex.fapiPublicGetFundingInfo()
        rows: dict[str, dict[str, float | int]] = {}
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            sym = str(item.get("symbol") or "").upper()
            if not sym:
                continue
            rows[sym] = {
                "funding_interval_hours": int(item.get("fundingIntervalHours") or 8),
                "cap": _safe_float(item.get("adjustedFundingRateCap")),
                "floor": _safe_float(item.get("adjustedFundingRateFloor")),
            }
        self._funding_info_all_cache = (now, rows)
        return rows

    async def fetch_basis(self, symbol: str, *, period: str = "1h", limit: int = 3) -> float | None:
        sym = self._bin_sym(symbol)
        cache_key = (sym, period)
        now = time.monotonic()
        cached = self._basis_cache.get(cache_key)
        if self._cache_fresh(cached, _CACHE_TTL["basis"]):
            return cached[1]  # type: ignore[index]
        try:
            payload = await self._ex.fapiDataGetBasis(
                {
                    "pair": sym,
                    "contractType": "PERPETUAL",
                    "period": period,
                    "limit": limit,
                }
            )
            basis_series: list[float] = []
            for row in payload if isinstance(payload, list) else []:
                futures_price = _safe_float(row.get("futuresPrice"))
                index_price = _safe_float(row.get("indexPrice"))
                if index_price <= 0:
                    continue
                basis_series.append((futures_price - index_price) / index_price * 100.0)
            if not basis_series:
                return cached[1] if cached else None  # type: ignore[return-value]
            basis_pct = basis_series[-1]
            premium_slope = basis_series[-1] - basis_series[-2] if len(basis_series) >= 2 else None
            premium_zscore = None
            if len(basis_series) >= 3:
                mean = sum(basis_series) / len(basis_series)
                variance = sum((v - mean) ** 2 for v in basis_series) / len(basis_series)
                std = math.sqrt(variance)
                if std > 0:
                    premium_zscore = (basis_series[-1] - mean) / std
            self._basis_cache[cache_key] = (now, basis_pct)
            self._basis_stats_cache[cache_key] = (
                now,
                {
                    "latest_basis_pct": basis_pct,
                    "premium_slope_5m": premium_slope,
                    "premium_zscore_5m": premium_zscore,
                    "mark_index_spread_bps": basis_pct * 100.0,
                },
            )
            return basis_pct
        except Exception:
            return cached[1] if cached else None  # type: ignore[return-value]

    async def fetch_agg_trade_snapshot(self, symbol: str, *, limit: int = 100) -> AggTradeSnapshot:
        sym = self._bin_sym(symbol)
        await self.load_markets()
        trades = await self._ex.fetch_trades(self._ccxt_sym(sym), limit=min(1000, max(1, limit)))
        buy_qty = sell_qty = 0.0
        for trade in trades:
            qty = _safe_float(trade.get("amount"))
            side = str(trade.get("side") or "").lower()
            if side == "buy":
                buy_qty += qty
            else:
                sell_qty += qty
        total = buy_qty + sell_qty
        delta_ratio = (buy_qty - sell_qty) / total if total > 0 else None
        return AggTradeSnapshot(
            symbol=sym,
            trade_count=len(trades),
            buy_qty=buy_qty,
            sell_qty=sell_qty,
            delta_ratio=delta_ratio,
        )

    def get_cached_open_interest(self, symbol: str, max_age_s: float = 1800.0) -> float | None:
        cached = self._open_interest_cache.get(self._bin_sym(symbol))
        if cached is None or time.monotonic() - cached[0] > max_age_s:
            return None
        return cached[1]

    def get_cached_oi_change(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        cached = self._open_interest_change_cache.get((self._bin_sym(symbol), period))
        if cached is None or time.monotonic() - cached[0] > max_age_s:
            return None
        return cached[1]

    def get_cached_ls_ratio(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        cached = self._long_short_ratio_cache.get((self._bin_sym(symbol), period))
        if cached is None or time.monotonic() - cached[0] > max_age_s:
            return None
        return cached[1]

    def get_cached_top_position_ls_ratio(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        cached = self._top_position_ls_ratio_cache.get((self._bin_sym(symbol), period))
        if cached is None or time.monotonic() - cached[0] > max_age_s:
            return None
        return cached[1]

    def get_cached_taker_ratio(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        cached = self._taker_ratio_cache.get((self._bin_sym(symbol), period))
        if cached is None or time.monotonic() - cached[0] > max_age_s:
            return None
        return cached[1]

    def get_cached_global_ls_ratio(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> float | None:
        cached = self._global_ls_ratio_cache.get((self._bin_sym(symbol), period))
        if cached is None or time.monotonic() - cached[0] > max_age_s:
            return None
        return cached[1]

    def get_cached_funding_rate(self, symbol: str, max_age_s: float = 1800.0) -> float | None:
        cached = self._funding_rate_cache.get(self._bin_sym(symbol))
        if cached is None or time.monotonic() - cached[0] > max_age_s:
            return None
        return cached[1]

    def get_cached_funding_trend(self, symbol: str, max_age_s: float = 1800.0) -> str | None:
        cached = self._funding_history_cache.get(self._bin_sym(symbol))
        if cached is None or time.monotonic() - cached[0] > max_age_s:
            return None
        rows = cached[1]
        if len(rows) < 3:
            return None
        recent = [float(r["fundingRate"]) for r in rows[-4:]]
        ups = sum(1 for i in range(1, len(recent)) if recent[i] > recent[i - 1])
        downs = sum(1 for i in range(1, len(recent)) if recent[i] < recent[i - 1])
        steps = len(recent) - 1
        if ups >= steps * 0.75:
            return "rising"
        if downs >= steps * 0.75:
            return "falling"
        return "flat"

    def get_cached_funding_rate_zscore(
        self, symbol: str, *, max_cache_age_s: float = 1800.0
    ) -> float | None:
        cached = self._funding_history_cache.get(self._bin_sym(symbol))
        if cached is None or time.monotonic() - cached[0] > max_cache_age_s:
            return None
        rates = [float(r["fundingRate"]) for r in cached[1] if math.isfinite(float(r["fundingRate"]))]
        if len(rates) < 6:
            return None
        mean = sum(rates) / len(rates)
        variance = sum((x - mean) ** 2 for x in rates) / len(rates)
        stdev = math.sqrt(variance) if variance > 0 else 0.0
        if stdev <= 1e-12:
            return 0.0
        return (rates[-1] - mean) / stdev

    def get_cached_funding_recent_extreme(
        self,
        symbol: str,
        *,
        max_age_hours: float = 48.0,
        max_cache_age_s: float = 1800.0,
    ) -> tuple[float, float] | None:
        cached = self._funding_history_cache.get(self._bin_sym(symbol))
        if cached is None or time.monotonic() - cached[0] > max_cache_age_s or not cached[1]:
            return None
        now_ms = int(time.time() * 1000)
        max_age_ms = max(0.0, float(max_age_hours)) * 3_600_000.0
        candidates: list[tuple[float, float]] = []
        for row in cached[1]:
            rate = float(row.get("fundingRate") or 0.0)
            funding_time = int(row.get("fundingTime") or 0)
            if funding_time <= 0:
                continue
            age_ms = max(0, now_ms - funding_time)
            if age_ms <= max_age_ms:
                candidates.append((rate, age_ms / 3_600_000.0))
        if not candidates:
            return None
        return max(candidates, key=lambda item: abs(item[0]))

    def get_cached_basis_stats(
        self, symbol: str, period: str = "1h", max_age_s: float = 1800.0
    ) -> dict[str, float | None] | None:
        cached = self._basis_stats_cache.get((self._bin_sym(symbol), period))
        if cached is None or time.monotonic() - cached[0] > max_age_s:
            return None
        return dict(cached[1])

    def update_basis_from_websocket(
        self,
        symbol: str,
        mark_price: float,
        index_price: float | None = None,
        period: str = "5m",
    ) -> dict[str, float | None] | None:
        if index_price is None or index_price <= 0 or mark_price <= 0:
            return None
        basis_pct = (mark_price - index_price) / index_price * 100.0
        now = time.monotonic()
        cache_key = (self._bin_sym(symbol), period)
        prev = self._basis_stats_cache.get(cache_key)
        premium_slope = None
        if prev is not None:
            prev_basis = prev[1].get("latest_basis_pct")
            if prev_basis is not None:
                premium_slope = basis_pct - float(prev_basis)
        stats = {
            "latest_basis_pct": basis_pct,
            "premium_slope_5m": premium_slope,
            "premium_zscore_5m": prev[1].get("premium_zscore_5m") if prev else None,
            "mark_index_spread_bps": basis_pct * 100.0,
        }
        self._basis_cache[cache_key] = (now, basis_pct)
        self._basis_stats_cache[cache_key] = (now, stats)
        return stats
