"""Hunter REST market client — CCXT binanceusdm (public only)."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any

import ccxt.async_support as ccxt
import polars as pl

from hunt_core.domain.schemas import AggTradeSnapshot, SymbolMeta
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
        rows: list[SymbolMeta] = []
        for market in self._ex.markets.values():
            info = market.get("info") if isinstance(market, dict) else None
            info = info if isinstance(info, dict) else {}
            rows.append(
                SymbolMeta(
                    symbol=str(market.get("id") or info.get("symbol") or ""),
                    base_asset=str(market.get("base") or info.get("baseAsset") or ""),
                    quote_asset=str(market.get("quote") or info.get("quoteAsset") or ""),
                    contract_type=str(info.get("contractType") or ""),
                    status=str(info.get("status") or ""),
                    onboard_date_ms=int(info.get("onboardDate") or 0),
                )
            )
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
            "bid_qty": float(pl.Series([float(q) for _p, q in bids]).sum()),
            "ask_qty": float(pl.Series([float(q) for _p, q in asks]).sum()),
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

    async def fetch_open_interest_change(self, symbol: str, *, period: str = "1h") -> float | None:
        sym = self._bin_sym(symbol)
        cache_key = (sym, period)
        now = time.monotonic()
        cached = self._open_interest_change_cache.get(cache_key)
        if self._cache_fresh(cached, _CACHE_TTL["open_interest_change"]):
            return cached[1]  # type: ignore[index]
        try:
            await self.load_markets()
            payload = await self._ex.fetch_open_interest_history(
                self._ccxt_sym(sym), timeframe=period, limit=2
            )
            series = [float(item["openInterestAmount"]) for item in payload
                      if item.get("openInterestAmount") is not None]
            if len(series) < 2 or series[-2] <= 0:
                return None
            change = series[-1] / series[-2] - 1.0
            self._open_interest_change_cache[cache_key] = (now, change)
            return change
        except Exception as exc:
            LOG.debug("oi_change failed | symbol=%s period=%s error=%s", sym, period, exc)
            return cached[1] if cached else None  # type: ignore[return-value]

    async def fetch_long_short_ratio(self, symbol: str, *, period: str = "1h") -> float | None:
        """Global long/short account ratio via ccxt unified fetch_long_short_ratio_history."""
        cache_key = (self._bin_sym(symbol), period)
        cached = self._long_short_ratio_cache.get(cache_key)
        if self._cache_fresh(cached, _CACHE_TTL["long_short_ratio"]):
            return cached[1]  # type: ignore[index]
        try:
            await self.load_markets()
            payload = await self._ex.fetch_long_short_ratio_history(
                self._ccxt_sym(symbol), timeframe=period, limit=1
            )
            if payload:
                value = _safe_float(payload[-1].get("longShortRatio"))
                if value > 0:
                    self._long_short_ratio_cache[cache_key] = (time.monotonic(), value)
                    return value
        except Exception:
            pass
        return cached[1] if cached else None  # type: ignore[return-value]

    async def fetch_open_interest_series(
        self, symbol: str, *, period: str = "5m", limit: int = 48
    ) -> list[float]:
        sym = self._bin_sym(symbol)
        cache_key = (sym, period, int(limit))
        cached = self._oi_series_cache.get(cache_key)
        if self._cache_fresh(cached, _CACHE_TTL["metric_series"]):
            return cached[1]  # type: ignore[index]
        try:
            await self.load_markets()
            payload = await self._ex.fetch_open_interest_history(
                self._ccxt_sym(sym), timeframe=period, limit=int(limit)
            )
            series = [float(item["openInterestAmount"]) for item in payload
                      if item.get("openInterestAmount") is not None]
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
            await self.load_markets()
            payload = await self._ex.fetch_long_short_ratio_history(
                self._ccxt_sym(sym), timeframe=period, limit=int(limit)
            )
            series = [float(item["longShortRatio"]) for item in payload
                      if item.get("longShortRatio") is not None]
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
        await self.load_markets()
        funding = await self._ex.fetch_funding_rates()
        rows: dict[str, dict[str, float]] = {}
        for ccxt_sym, item in funding.items():
            sym = self._bin_sym(from_ccxt_symbol(ccxt_sym))
            mark = _safe_float(item.get("markPrice"))
            index = _safe_float(item.get("indexPrice"))
            if not sym or mark <= 0:
                continue
            rows[sym] = {
                "mark_price": mark,
                "index_price": index,
                "last_funding_rate": _safe_float(item.get("fundingRate")),
            }
        self._premium_index_all_cache = (now, rows)
        return rows

    async def fetch_funding_info_all(self) -> dict[str, dict[str, float | int]]:
        now = time.monotonic()
        if self._cache_fresh(self._funding_info_all_cache, _CACHE_TTL["funding_info"]):
            assert self._funding_info_all_cache is not None
            return self._funding_info_all_cache[1]
        await self.load_markets()
        intervals = await self._ex.fetch_funding_intervals()
        rows: dict[str, dict[str, float | int]] = {}
        for ccxt_sym, item in intervals.items():
            info = item.get("info") if isinstance(item, dict) else None
            info = info if isinstance(info, dict) else {}
            sym = self._bin_sym(from_ccxt_symbol(ccxt_sym))
            if not sym:
                continue
            rows[sym] = {
                "funding_interval_hours": int(info.get("fundingIntervalHours") or 8),
                "cap": _safe_float(info.get("adjustedFundingRateCap")),
                "floor": _safe_float(info.get("adjustedFundingRateFloor")),
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
            s = pl.Series("basis", basis_series)
            basis_pct = float(s[-1])
            premium_slope = float(s[-1] - s[-2]) if len(basis_series) >= 2 else None
            premium_zscore = None
            if len(basis_series) >= 3:
                std = float(s.std(ddof=0) or 0.0)
                if std > 0:
                    premium_zscore = float((s[-1] - s.mean()) / std)
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
        s = pl.Series("r", [float(r["fundingRate"]) for r in rows[-4:]])
        diffs = s.diff().drop_nulls()
        ups = int((diffs > 0).sum())
        downs = int((diffs < 0).sum())
        steps = diffs.len()
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
        s = pl.Series("rates", [float(r["fundingRate"]) for r in cached[1]]).drop_nans()
        if s.len() < 6:
            return None
        stdev = float(s.std(ddof=1) or 0.0)
        if stdev <= 1e-12:
            return 0.0
        return float((s[-1] - s.mean()) / stdev)

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

    async def fetch_mark_ohlcv(
        self, symbol: str, interval: str = "1h", *, limit: int = 96
    ) -> pl.DataFrame:
        """Mark price OHLCV via ccxt fetch_mark_ohlcv."""
        await self.load_markets()
        from hunt_core.market.frames import ccxt_ohlcv_to_frame, finalize_kline_frame
        rows = await self._ex.fetch_mark_ohlcv(self._ccxt_sym(symbol), interval, limit=limit)
        return finalize_kline_frame(ccxt_ohlcv_to_frame(rows, interval), interval)

    async def fetch_index_ohlcv(
        self, symbol: str, interval: str = "1h", *, limit: int = 96
    ) -> pl.DataFrame:
        """Index price OHLCV via ccxt fetch_index_ohlcv."""
        await self.load_markets()
        from hunt_core.market.frames import ccxt_ohlcv_to_frame, finalize_kline_frame
        rows = await self._ex.fetch_index_ohlcv(self._ccxt_sym(symbol), interval, limit=limit)
        return finalize_kline_frame(ccxt_ohlcv_to_frame(rows, interval), interval)

    async def fetch_premium_index_ohlcv(
        self, symbol: str, interval: str = "1h", *, limit: int = 96
    ) -> pl.DataFrame:
        """Premium index (basis %) OHLCV via ccxt fetch_premium_index_ohlcv."""
        await self.load_markets()
        from hunt_core.market.frames import ccxt_ohlcv_to_frame, finalize_kline_frame
        rows = await self._ex.fetch_premium_index_ohlcv(self._ccxt_sym(symbol), interval, limit=limit)
        return finalize_kline_frame(ccxt_ohlcv_to_frame(rows, interval), interval)

    async def fetch_basis_from_ohlcv(
        self, symbol: str, interval: str = "1h", *, limit: int = 48
    ) -> dict[str, float | None]:
        """Basis stats computed from mark vs index OHLCV frames via polars."""
        try:
            mark_df, index_df = await asyncio.gather(
                self.fetch_mark_ohlcv(symbol, interval, limit=limit),
                self.fetch_index_ohlcv(symbol, interval, limit=limit),
            )
            if mark_df.is_empty() or index_df.is_empty():
                return {}
            joined = mark_df.select(["time", pl.col("close").alias("mark_close")]).join(
                index_df.select(["time", pl.col("close").alias("index_close")]),
                on="time",
                how="inner",
            )
            if joined.is_empty():
                return {}
            basis = (
                (pl.col("mark_close") - pl.col("index_close")) / pl.col("index_close") * 100.0
            )
            joined = joined.with_columns(basis.alias("basis_pct"))
            s = joined["basis_pct"]
            latest = float(s[-1])
            slope = float(s[-1] - s[-2]) if s.len() >= 2 else None
            std = float(s.std(ddof=0) or 0.0)
            zscore = float((s[-1] - s.mean()) / std) if std > 0 else None
            cache_key = (self._bin_sym(symbol), interval)
            now = time.monotonic()
            stats = {
                "latest_basis_pct": latest,
                "premium_slope_5m": slope,
                "premium_zscore_5m": zscore,
                "mark_index_spread_bps": latest * 100.0,
            }
            self._basis_cache[cache_key] = (now, latest)
            self._basis_stats_cache[cache_key] = (now, stats)
            return stats
        except Exception as exc:
            LOG.debug("fetch_basis_from_ohlcv failed | symbol=%s error=%s", symbol, exc)
            return {}

    # ── Secondary exchange REST (Bybit / OKX / Bitget) ──────────────────────

    _SECONDARY_EXCHANGE_IDS: dict[str, str] = {
        "bybit": "bybit",
        "okx": "okx",
        "bitget": "bitget",
    }

    async def _get_secondary(self, name: str) -> ccxt.Exchange:
        """Return (and lazily create) a cached secondary ccxt REST client."""
        if not hasattr(self, "_secondary_clients"):
            self._secondary_clients: dict[str, ccxt.Exchange] = {}
        if name not in self._secondary_clients:
            ex_id = self._SECONDARY_EXCHANGE_IDS[name]
            cls = getattr(ccxt, ex_id)
            ex: ccxt.Exchange = cls(
                {
                    "enableRateLimit": True,
                    "options": {"defaultType": "swap"},
                }
            )
            try:
                await ex.load_markets()
            except Exception as exc:
                LOG.warning("secondary_load_markets_failed | exchange=%s error=%s", name, exc)
            self._secondary_clients[name] = ex
        return self._secondary_clients[name]

    async def _fetch_secondary_funding(
        self, name: str, ccxt_sym: str
    ) -> dict[str, float | None]:
        try:
            ex = await self._get_secondary(name)
            r = await ex.fetch_funding_rate(ccxt_sym)
            return {"fundingRate": float(r.get("fundingRate") or 0)}
        except Exception as exc:
            LOG.debug("secondary_funding_failed | exchange=%s sym=%s error=%s", name, ccxt_sym, exc)
            return {"fundingRate": None}

    async def _fetch_secondary_oi(
        self, name: str, ccxt_sym: str
    ) -> dict[str, float | None]:
        try:
            ex = await self._get_secondary(name)
            r = await ex.fetch_open_interest(ccxt_sym)
            oi_val = (
                float(r.get("openInterestValue") or r.get("openInterest") or 0) or None
            )
            return {"oi_usd": oi_val}
        except Exception as exc:
            LOG.debug("secondary_oi_failed | exchange=%s sym=%s error=%s", name, ccxt_sym, exc)
            return {"oi_usd": None}

    async def _fetch_secondary_ticker(
        self, name: str, ccxt_sym: str
    ) -> dict[str, float | None]:
        try:
            ex = await self._get_secondary(name)
            t = await ex.fetch_ticker(ccxt_sym)
            mark = float(t.get("mark") or t.get("last") or 0) or None
            return {"mark_price": mark}
        except Exception as exc:
            LOG.debug("secondary_ticker_failed | exchange=%s sym=%s error=%s", name, ccxt_sym, exc)
            return {"mark_price": None}

    async def fetch_cross_exchange_snapshot(self, symbol: str) -> dict[str, Any]:
        """
        Fetch funding / OI / mark-price for symbol from Bybit + OKX + Bitget in parallel.

        Returns a dict with:
          funding:   {exchange: rate|None}
          oi_usd:    {exchange: value|None}
          mark_price:{exchange: price|None}
          funding_spread: abs(max−min) across exchanges with data
          funding_consensus: "bull"|"bear"|"neutral"|"divergent"
          oi_total:  sum of OI across exchanges with data
          price_divergence_pct: max price spread / mean (%)
        """
        # Use the base ccxt symbol (Binance mark price as reference)
        ref_funding = 0.0
        ref_mark = 0.0
        try:
            pr = (self._premium_index_all_cache or (None, {}))[1].get(symbol) or {}
            ref_funding = float(pr.get("last_funding_rate") or 0)
            ref_mark = float(pr.get("mark_price") or 0)
        except Exception:
            pass

        # ccxt unified sym works on all three secondary exchanges
        ccxt_sym = self._ccxt_sym(symbol)

        results = await asyncio.gather(
            *[
                asyncio.gather(
                    self._fetch_secondary_funding(name, ccxt_sym),
                    self._fetch_secondary_oi(name, ccxt_sym),
                    self._fetch_secondary_ticker(name, ccxt_sym),
                )
                for name in self._SECONDARY_EXCHANGE_IDS
            ],
            return_exceptions=True,
        )

        funding: dict[str, float | None] = {"binance": ref_funding or None}
        oi_usd: dict[str, float | None] = {}
        mark_price: dict[str, float | None] = {"binance": ref_mark or None}

        for name, res in zip(self._SECONDARY_EXCHANGE_IDS, results):
            if isinstance(res, Exception):
                funding[name] = None
                oi_usd[name] = None
                mark_price[name] = None
                continue
            f_r, oi_r, t_r = res
            funding[name] = f_r.get("fundingRate")
            oi_usd[name] = oi_r.get("oi_usd")
            mark_price[name] = t_r.get("mark_price")

        # Aggregate
        rates = [v for v in funding.values() if v is not None]
        funding_spread = round(max(rates) - min(rates), 6) if len(rates) >= 2 else 0.0

        consensus: str
        if len(rates) < 2:
            consensus = "neutral"
        elif all(r > 0.0001 for r in rates):
            consensus = "bull"
        elif all(r < -0.0001 for r in rates):
            consensus = "bear"
        elif funding_spread > 0.0005:
            consensus = "divergent"
        else:
            consensus = "neutral"

        oi_values = [v for v in oi_usd.values() if v is not None]
        oi_total = round(sum(oi_values), 0) if oi_values else 0.0

        prices = [v for v in mark_price.values() if v and v > 0]
        price_div = 0.0
        if len(prices) >= 2:
            mean_p = sum(prices) / len(prices)
            price_div = round((max(prices) - min(prices)) / mean_p * 100, 4) if mean_p > 0 else 0.0

        return {
            "funding": funding,
            "oi_usd": oi_usd,
            "mark_price": mark_price,
            "funding_spread": funding_spread,
            "funding_consensus": consensus,
            "oi_total": oi_total,
            "price_divergence_pct": price_div,
        }
