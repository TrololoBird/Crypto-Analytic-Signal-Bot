"""Hunter live streams — CCXT Pro watch* (liquidations, trades, OHLCV, mark)."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from hunt_core.errors import defensive_exc_types
from hunt_core.market.client import HuntCcxtClient
from hunt_core.market.symbols import from_ccxt_symbol, to_binance_symbol, to_ccxt_symbol
from hunt_watch.param_store import orderflow_use_nq, ws_thresholds

LOG = logging.getLogger("hunt_core.market.streams")

_MAX_SYMBOL_STREAMS = 24
_LIQ_BUFFER_MAX = 8_000
_AGG_BUFFER_MAX = 2_000
_KLINE_INTERVAL = "1m"


def _kline_grace_sec() -> float:
    return float(ws_thresholds().get("kline_grace_sec", 1.5))


def _liquidation_event_count(
    buffer: collections.deque[tuple[int, str, str, float, float]],
    *,
    symbol: str | None,
    window_seconds: int,
) -> int:
    cutoff_ms = int(time.time() * 1000) - window_seconds * 1000
    count = 0
    for ts_ms, sym, _side, qty, _price in buffer:
        if ts_ms < cutoff_ms or qty <= 0.0:
            continue
        if symbol is not None and sym != symbol:
            continue
        count += 1
    return count


def _liquidation_rollups(
    buffer: collections.deque[tuple[int, str, str, float, float]],
    *,
    symbol: str | None,
    window_seconds: int,
) -> dict[str, float] | None:
    cutoff_ms = int(time.time() * 1000) - window_seconds * 1000
    long_notional = short_notional = 0.0
    for ts_ms, sym, side, qty, price in buffer:
        if ts_ms < cutoff_ms:
            continue
        if symbol is not None and sym != symbol:
            continue
        try:
            qty_val = float(qty)
            price_val = float(price)
        except (TypeError, ValueError):
            continue
        if qty_val <= 0.0:
            continue
        notional = qty_val * price_val if price_val > 0.0 else qty_val
        if side == "BUY":
            short_notional += notional
        else:
            long_notional += notional
    total = long_notional + short_notional
    if total <= 0.0:
        return None
    return {
        "liquidation_long_notional": long_notional,
        "liquidation_short_notional": short_notional,
        "liquidation_total_notional": total,
        "liquidation_score": round(short_notional / total, 4),
    }


@dataclass
class _ClosedKlineBar:
    open_ms: int
    o: float
    h: float
    l: float
    c: float
    v: float
    received_ms: int


@dataclass
class _AggPoint:
    ts_ms: int
    qty: float
    qty_full: float
    is_buy: bool


@dataclass
class HuntCcxtStreams:
    """CCXT watch* background tasks — multiplexed streams via ccxt.pro watch_*_for_symbols."""

    client: HuntCcxtClient
    _symbols: set[str] = field(default_factory=set)
    _force_order_buffer: collections.deque[tuple[int, str, str, float, float]] = field(
        default_factory=lambda: collections.deque(maxlen=_LIQ_BUFFER_MAX)
    )
    _agg_points: dict[str, collections.deque[_AggPoint]] = field(default_factory=dict)
    _tasks: list[asyncio.Task[None]] = field(default_factory=list)
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _connected: bool = False
    _symbols_dirty: bool = False
    _kline_closed_open_ms: dict[str, int] = field(default_factory=dict)
    _kline_waiting: dict[str, _ClosedKlineBar] = field(default_factory=dict)
    _kline_ready: dict[str, _ClosedKlineBar] = field(default_factory=dict)
    _last_kline_open_ms: dict[str, int] = field(default_factory=dict)
    kline_ws_enabled: bool = True
    mark_price_enabled: bool = True
    _mark_state: dict[str, tuple[int, float, float, float, float]] = field(default_factory=dict)
    _last_msg_ms: int = 0
    # live book/ticker/funding data from new multiplexed streams
    _live_books: dict[str, dict[str, Any]] = field(default_factory=dict)
    _live_tickers: dict[str, dict[str, float]] = field(default_factory=dict)
    _live_funding: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def kline_5m_enabled(self) -> bool:
        return self.kline_ws_enabled

    def set_symbols(self, symbols: list[str]) -> None:
        trimmed = [to_binance_symbol(s) for s in symbols if s][:_MAX_SYMBOL_STREAMS]
        new_set = set(trimmed)
        if new_set != self._symbols:
            self._symbols = new_set
            self._symbols_dirty = True

    def liquidation_rollups(self, symbol: str, *, window_seconds: int = 300) -> dict[str, float] | None:
        return _liquidation_rollups(
            self._force_order_buffer,
            symbol=to_binance_symbol(symbol),
            window_seconds=window_seconds,
        )

    def liquidation_events(self, symbol: str, *, window_seconds: int = 300) -> int:
        return _liquidation_event_count(
            self._force_order_buffer,
            symbol=to_binance_symbol(symbol),
            window_seconds=window_seconds,
        )

    def agg_trade_delta(
        self,
        symbol: str,
        *,
        window_seconds: int = 60,
        use_nq: bool | None = None,
    ) -> float | None:
        sym = to_binance_symbol(symbol)
        if use_nq is None:
            use_nq = orderflow_use_nq(sym)
        buf = self._agg_points.get(sym)
        if not buf:
            return None
        cutoff = int(time.time() * 1000) - window_seconds * 1000
        buy = sell = 0.0
        for pt in buf:
            if pt.ts_ms < cutoff:
                continue
            vol = pt.qty if use_nq else pt.qty_full
            if pt.is_buy:
                buy += vol
            else:
                sell += vol
        total = buy + sell
        if total <= 0:
            return None
        return round(buy / total, 3)

    def agg_rpi_skew(self, symbol: str, *, window_seconds: int = 60) -> float | None:
        sym = to_binance_symbol(symbol)
        buf = self._agg_points.get(sym)
        if not buf:
            return None
        cutoff = int(time.time() * 1000) - window_seconds * 1000
        nq_sum = q_sum = 0.0
        for pt in buf:
            if pt.ts_ms < cutoff:
                continue
            nq_sum += pt.qty
            q_sum += pt.qty_full
        if q_sum <= 0:
            return None
        return round(max(0.0, (q_sum - nq_sum) / q_sum), 4)

    def live_book(self, symbol: str) -> dict[str, Any] | None:
        """Latest L2 order book snapshot from watch_order_book_for_symbols."""
        return self._live_books.get(to_binance_symbol(symbol))

    def live_ticker(self, symbol: str) -> dict[str, float] | None:
        """Latest 24h ticker from watch_tickers."""
        return self._live_tickers.get(to_binance_symbol(symbol))

    def live_funding(self, symbol: str) -> dict[str, float] | None:
        """Latest funding rate from watch_funding_rates."""
        return self._live_funding.get(to_binance_symbol(symbol))

    def snapshot(self, symbol: str) -> dict[str, Any]:
        sym = to_binance_symbol(symbol)
        liq = self.liquidation_rollups(sym, window_seconds=300)
        liq_60 = self.liquidation_rollups(sym, window_seconds=60)
        fresh = self._last_msg_ms > 0 and time.time() * 1000 - self._last_msg_ms < 60_000
        book = self._live_books.get(sym) or {}
        ticker = self._live_tickers.get(sym) or {}
        funding = self._live_funding.get(sym) or {}
        return {
            "ws_routed_market": True,
            "ws_base_url": "ccxt.pro",
            "ws_connected": self._connected and fresh,
            "ws_socket_open": self._connected,
            "ws_last_msg_age_s": (
                round((time.time() * 1000 - self._last_msg_ms) / 1000.0, 1)
                if self._last_msg_ms
                else None
            ),
            "liq_events_5m": self.liquidation_events(sym, window_seconds=300),
            "liq_events_1m": self.liquidation_events(sym, window_seconds=60),
            "liquidation_score_5m": (liq or {}).get("liquidation_score"),
            "liquidation_score_1m": (liq_60 or {}).get("liquidation_score"),
            "liquidation_long_notional_5m": (liq or {}).get("liquidation_long_notional"),
            "liquidation_short_notional_5m": (liq or {}).get("liquidation_short_notional"),
            "agg_trade_delta_60s": self.agg_trade_delta(sym, window_seconds=60),
            "agg_trade_delta_30s": self.agg_trade_delta(sym, window_seconds=30),
            "agg_trade_source": "ccxt_watch_trades",
            "agg_rpi_skew_60s": self.agg_rpi_skew(sym, window_seconds=60),
            f"kline_{_KLINE_INTERVAL}_last_close_ms": self._kline_closed_open_ms.get(sym),
            "kline_ws_interval": _KLINE_INTERVAL,
            # live book microstructure
            "live_bid": book.get("bid"),
            "live_ask": book.get("ask"),
            "live_depth_imbalance": book.get("depth_imbalance"),
            "live_microprice_bias": book.get("microprice_bias"),
            # live ticker
            "live_quote_volume": ticker.get("quoteVolume"),
            "live_price_change_pct": ticker.get("percentage"),
            # live funding
            "live_funding_rate": funding.get("fundingRate"),
            "live_mark_price": funding.get("markPrice"),
            "live_index_price": funding.get("indexPrice"),
            **(self.mark_snapshot(sym) or {}),
        }

    def _promote_kline_grace(self) -> None:
        now_ms = int(time.time() * 1000)
        grace_ms = int(_kline_grace_sec() * 1000)
        for sym, bar in list(self._kline_waiting.items()):
            if now_ms - bar.received_ms < grace_ms:
                continue
            prev = self._kline_closed_open_ms.get(sym)
            if prev != bar.open_ms:
                self._kline_closed_open_ms[sym] = bar.open_ms
            self._kline_ready[sym] = bar
            self._kline_waiting.pop(sym, None)

    def pop_kline_close_triggers(self) -> set[str]:
        self._promote_kline_grace()
        return set(self._kline_ready)

    def consume_kline_close_triggers(self, symbols: set[str] | frozenset[str]) -> None:
        for sym in symbols:
            self._kline_ready.pop(to_binance_symbol(str(sym)), None)

    def closed_kline_overlay(self, symbol: str) -> dict[str, Any] | None:
        self._promote_kline_grace()
        sym = to_binance_symbol(symbol)
        bar = self._kline_ready.get(sym)
        if bar is None:
            return None
        body = abs(bar.c - bar.o)
        full = max(bar.h - bar.l, 1e-12)
        upper_wick = bar.h - max(bar.o, bar.c)
        lower_wick = min(bar.o, bar.c) - bar.l
        return {
            "close": round(bar.c, 6),
            "closed_bar": True,
            "ws_open_ms": bar.open_ms,
            "ws_grace_s": _kline_grace_sec(),
            "candle": {
                "open": round(bar.o, 6),
                "high": round(bar.h, 6),
                "low": round(bar.l, 6),
                "close": round(bar.c, 6),
                "upper_wick_ratio": round(upper_wick / full, 3),
                "lower_wick_ratio": round(lower_wick / full, 3),
                "body_ratio": round(body / full, 3),
                "bearish": bar.c < bar.o,
                "bullish": bar.c > bar.o,
            },
        }

    def closed_5m_overlay(self, symbol: str) -> dict[str, Any] | None:
        return self.closed_kline_overlay(symbol)

    def mark_snapshot(self, symbol: str, *, max_age_s: float = 10.0) -> dict[str, float] | None:
        rec = self._mark_state.get(to_binance_symbol(symbol))
        if rec is None:
            return None
        ts_ms, mark, index, funding, ap = rec
        if time.time() * 1000 - ts_ms > max_age_s * 1000:
            return None
        basis_bps = (mark - index) / index * 10_000 if index > 0 else 0.0
        out: dict[str, float] = {
            "mark_live": mark,
            "index_live": index,
            "funding_live": funding,
            "basis_bps_live": round(basis_bps, 2),
        }
        if ap > 0:
            out["mark_ap_live"] = ap
            out["mark_ap_spread_bps"] = round((mark - ap) / ap * 10_000, 2)
            if index > 0:
                out["basis_ap_bps"] = round((ap - index) / index * 10_000, 2)
        return out

    def _touch(self) -> None:
        self._last_msg_ms = int(time.time() * 1000)

    def _record_liquidation(self, item: dict[str, Any]) -> None:
        info = item.get("info") if isinstance(item.get("info"), dict) else item
        sym = to_binance_symbol(str(item.get("symbol") or info.get("s") or ""))
        side = str(item.get("side") or info.get("S") or "").upper()
        qty = float(item.get("amount") or info.get("q") or 0)
        price = float(item.get("price") or info.get("p") or 0)
        ts_ms = int(item.get("timestamp") or info.get("T") or time.time() * 1000)
        if sym and qty > 0:
            self._force_order_buffer.append((ts_ms, sym, side, qty, price))

    def _record_trade(self, sym: str, trade: dict[str, Any]) -> None:
        info = trade.get("info") if isinstance(trade.get("info"), dict) else trade
        qty_full = float(trade.get("amount") or info.get("q") or 0)
        nq_raw = info.get("nq")
        qty_nq = float(nq_raw) if nq_raw is not None else qty_full
        qty = qty_nq if qty_nq > 0 else qty_full
        ts_ms = int(trade.get("timestamp") or info.get("T") or time.time() * 1000)
        is_buy = str(trade.get("side") or "").lower() == "buy"
        if qty <= 0 and qty_full <= 0:
            return
        buf = self._agg_points.setdefault(sym, collections.deque(maxlen=_AGG_BUFFER_MAX))
        buf.append(
            _AggPoint(
                ts_ms=ts_ms,
                qty=qty,
                qty_full=qty_full if qty_full > 0 else qty,
                is_buy=is_buy,
            )
        )

    def _on_closed_kline(self, sym: str, candle: list[Any]) -> None:
        try:
            open_ms = int(candle[0])
            o, h, low, c, v = (
                float(candle[1]),
                float(candle[2]),
                float(candle[3]),
                float(candle[4]),
                float(candle[5]),
            )
        except (TypeError, ValueError, IndexError):
            return
        if open_ms <= 0 or c <= 0:
            return
        self._kline_waiting[sym] = _ClosedKlineBar(
            open_ms=open_ms,
            o=o,
            h=h,
            l=low,
            c=c,
            v=v,
            received_ms=int(time.time() * 1000),
        )

    def _on_ohlcv_update(self, sym: str, ohlcv: list[list[Any]]) -> None:
        if not ohlcv:
            return
        latest_open = int(ohlcv[-1][0])
        prev_open = self._last_kline_open_ms.get(sym)
        if prev_open is not None and latest_open != prev_open and len(ohlcv) >= 2:
            self._on_closed_kline(sym, ohlcv[-2])
        self._last_kline_open_ms[sym] = latest_open

    async def start(self) -> None:
        if self._tasks:
            return
        self._stop.clear()
        await self.client.load_markets()
        self._tasks = [
            asyncio.create_task(self._watch_liquidations(), name="hunt_ccxt_liq"),
            asyncio.create_task(self._watch_mark_prices(), name="hunt_ccxt_mark"),
            asyncio.create_task(self._watch_trades_mux(), name="hunt_ccxt_trades"),
            asyncio.create_task(self._watch_ohlcv_mux(), name="hunt_ccxt_ohlcv"),
            asyncio.create_task(self._watch_order_book_mux(), name="hunt_ccxt_book"),
            asyncio.create_task(self._watch_tickers_mux(), name="hunt_ccxt_tickers"),
            asyncio.create_task(self._watch_funding_rates_mux(), name="hunt_ccxt_funding"),
        ]
        self._connected = True
        LOG.info("hunt_ccxt_streams_started")

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        self._connected = False

    async def _watch_liquidations(self) -> None:
        ex = self.client.exchange
        while not self._stop.is_set():
            try:
                items = await ex.watch_liquidations()
                self._touch()
                batch = items if isinstance(items, list) else [items]
                for item in batch:
                    if isinstance(item, dict):
                        self._record_liquidation(item)
            except asyncio.CancelledError:
                break
            except defensive_exc_types(Exception) as exc:
                LOG.warning("hunt_ccxt_liq_error", error=repr(exc))
                await asyncio.sleep(1.0)

    async def _watch_mark_prices(self) -> None:
        if not self.mark_price_enabled:
            return
        ex = self.client.exchange
        while not self._stop.is_set():
            try:
                prices = await ex.watch_mark_prices()
                self._touch()
                now_ms = int(time.time() * 1000)
                items = prices.values() if isinstance(prices, dict) else prices
                for item in items if isinstance(items, list) else []:
                    if not isinstance(item, dict):
                        continue
                    sym = to_binance_symbol(from_ccxt_symbol(str(item.get("symbol") or "")))
                    if sym not in self._symbols:
                        continue
                    mark = float(item.get("markPrice") or item.get("last") or 0)
                    index = float(item.get("indexPrice") or 0)
                    funding = float(item.get("fundingRate") or 0)
                    info = item.get("info") if isinstance(item.get("info"), dict) else {}
                    ap_raw = info.get("ap")
                    ap = float(ap_raw) if ap_raw is not None else 0.0
                    if mark > 0:
                        self._mark_state[sym] = (now_ms, mark, index, funding, ap)
                        self.client.update_basis_from_websocket(sym, mark, index if index > 0 else None)
            except asyncio.CancelledError:
                break
            except defensive_exc_types(Exception) as exc:
                LOG.warning("hunt_ccxt_mark_error", error=repr(exc))
                await asyncio.sleep(1.0)

    def _ccxt_symbols(self) -> list[str]:
        ex = self.client.exchange
        return [to_ccxt_symbol(s, markets=ex.markets) for s in sorted(self._symbols)]

    async def _watch_trades_mux(self) -> None:
        """Single multiplexed trades stream for all symbols via watch_trades_for_symbols."""
        ex = self.client.exchange
        while not self._stop.is_set():
            syms = self._ccxt_symbols()
            if not syms:
                await asyncio.sleep(0.5)
                continue
            try:
                trades = await ex.watch_trades_for_symbols(syms)
                self._touch()
                for trade in trades if isinstance(trades, list) else [trades]:
                    if not isinstance(trade, dict):
                        continue
                    sym = to_binance_symbol(from_ccxt_symbol(str(trade.get("symbol") or "")))
                    if sym:
                        self._record_trade(sym, trade)
            except asyncio.CancelledError:
                break
            except defensive_exc_types(Exception) as exc:
                LOG.warning("hunt_ccxt_trades_error", error=repr(exc))
                await asyncio.sleep(1.0)

    async def _watch_ohlcv_mux(self) -> None:
        """Single multiplexed OHLCV stream for all symbols via watch_ohlcv_for_symbols."""
        if not self.kline_ws_enabled:
            return
        ex = self.client.exchange
        while not self._stop.is_set():
            syms = self._ccxt_symbols()
            if not syms:
                await asyncio.sleep(0.5)
                continue
            pairs = [(s, _KLINE_INTERVAL) for s in syms]
            try:
                result = await ex.watch_ohlcv_for_symbols(pairs)
                self._touch()
                # result: {ccxt_sym: {timeframe: [[ts, o, h, l, c, v], ...]}}
                if isinstance(result, dict):
                    for ccxt_sym, tf_map in result.items():
                        sym = to_binance_symbol(from_ccxt_symbol(str(ccxt_sym)))
                        if not sym or not isinstance(tf_map, dict):
                            continue
                        ohlcv = tf_map.get(_KLINE_INTERVAL)
                        if isinstance(ohlcv, list):
                            self._on_ohlcv_update(sym, ohlcv)
            except asyncio.CancelledError:
                break
            except defensive_exc_types(Exception) as exc:
                LOG.warning("hunt_ccxt_kline_error", error=repr(exc))
                await asyncio.sleep(1.0)

    async def _watch_order_book_mux(self) -> None:
        """Live L2 order book via watch_order_book_for_symbols → depth imbalance + microprice."""
        from hunt_core.market.book_parsers import depth_imbalance_from_book, microprice_bias_from_book

        ex = self.client.exchange
        while not self._stop.is_set():
            syms = self._ccxt_symbols()
            if not syms:
                await asyncio.sleep(0.5)
                continue
            try:
                book = await ex.watch_order_book_for_symbols(syms)
                self._touch()
                if not isinstance(book, dict):
                    continue
                sym = to_binance_symbol(from_ccxt_symbol(str(book.get("symbol") or "")))
                if not sym:
                    continue
                bids = book.get("bids") or []
                asks = book.get("asks") or []
                bid_p = float(bids[0][0]) if bids else None
                ask_p = float(asks[0][0]) if asks else None
                bid_q = float(bids[0][1]) if bids else None
                ask_q = float(asks[0][1]) if asks else None
                di = depth_imbalance_from_book(bid_qty=bid_q, ask_qty=ask_q, delta_ratio=None)
                mp = microprice_bias_from_book(bid=bid_p, ask=ask_p, bid_qty=bid_q, ask_qty=ask_q, delta_ratio=None)
                self._live_books[sym] = {
                    "bid": bid_p,
                    "ask": ask_p,
                    "bid_qty": bid_q,
                    "ask_qty": ask_q,
                    "depth_imbalance": di,
                    "microprice_bias": mp,
                }
            except asyncio.CancelledError:
                break
            except defensive_exc_types(Exception) as exc:
                LOG.warning("hunt_ccxt_book_error", error=repr(exc))
                await asyncio.sleep(1.0)

    async def _watch_tickers_mux(self) -> None:
        """Rolling 24h stats for all symbols via watch_tickers."""
        ex = self.client.exchange
        while not self._stop.is_set():
            syms = self._ccxt_symbols()
            if not syms:
                await asyncio.sleep(0.5)
                continue
            try:
                tickers = await ex.watch_tickers(syms)
                self._touch()
                items = tickers.values() if isinstance(tickers, dict) else []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    sym = to_binance_symbol(from_ccxt_symbol(str(item.get("symbol") or "")))
                    if not sym:
                        continue
                    self._live_tickers[sym] = {
                        "last": float(item.get("last") or 0),
                        "quoteVolume": float(item.get("quoteVolume") or 0),
                        "percentage": float(item.get("percentage") or 0),
                        "high": float(item.get("high") or 0),
                        "low": float(item.get("low") or 0),
                    }
            except asyncio.CancelledError:
                break
            except defensive_exc_types(Exception) as exc:
                LOG.warning("hunt_ccxt_tickers_error", error=repr(exc))
                await asyncio.sleep(1.0)

    async def _watch_funding_rates_mux(self) -> None:
        """Live funding/mark/index prices via watch_funding_rates."""
        ex = self.client.exchange
        while not self._stop.is_set():
            syms = self._ccxt_symbols()
            if not syms:
                await asyncio.sleep(0.5)
                continue
            try:
                rates = await ex.watch_funding_rates(syms)
                self._touch()
                items = rates.values() if isinstance(rates, dict) else []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    sym = to_binance_symbol(from_ccxt_symbol(str(item.get("symbol") or "")))
                    if not sym:
                        continue
                    mark = float(item.get("markPrice") or 0)
                    index = float(item.get("indexPrice") or 0)
                    funding = float(item.get("fundingRate") or 0)
                    self._live_funding[sym] = {
                        "markPrice": mark,
                        "indexPrice": index,
                        "fundingRate": funding,
                    }
                    if mark > 0 and index > 0:
                        self.client.update_basis_from_websocket(sym, mark, index)
            except asyncio.CancelledError:
                break
            except defensive_exc_types(Exception) as exc:
                LOG.warning("hunt_ccxt_funding_error", error=repr(exc))
                await asyncio.sleep(1.0)
