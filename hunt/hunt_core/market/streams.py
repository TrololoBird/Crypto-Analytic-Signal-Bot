"""Hunter live streams — CCXT Pro watch* (liquidations, trades, OHLCV, mark)."""
from __future__ import annotations



import asyncio
import collections
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any


from hunt_core.errors import defensive_exc_types
from hunt_core.market.factory import close_exchange_async, create_pro_secondary_swap
from hunt_core.market.client import HuntCcxtClient
from hunt_core.market.cross import configured_secondary_exchanges
from hunt_core.market.client import build_liquidation_heatmap, heatmap_to_market_dict
from hunt_core.market.symbols import (
    from_ccxt_symbol,
    to_binance_symbol,
    to_ccxt_symbol,
    try_resolve_linear_usdt_swap,
)
from hunt_core.params.store import orderflow_use_nq, ws_thresholds

LOG = logging.getLogger("hunt_core.market.streams")

_MAX_SYMBOL_STREAMS = 24
_LIQ_BUFFER_MAX = 8_000
_AGG_BUFFER_MAX = 2_000
_KLINE_INTERVAL = "1m"
_KLINE_5M_INTERVAL = "5m"
_KLINE_15M_INTERVAL = "15m"


def _kline_ws_5m_enabled() -> bool:
    return os.getenv("HUNT_KLINE_WS_5M", "1").strip().lower() in {"1", "true", "yes", "on"}


def _kline_ws_15m_enabled() -> bool:
    return os.getenv("HUNT_KLINE_WS_15M", "1").strip().lower() in {"1", "true", "yes", "on"}


def _kline_grace_sec() -> float:
    return float(ws_thresholds().get("kline_grace_sec", 1.5))


async def _join_cancelled_task(task: asyncio.Task[Any]) -> None:
    """Await a cancelled stream task; log unexpected shutdown errors."""
    try:
        await task
    except asyncio.CancelledError:
        return
    except Exception as exc:
        LOG.warning("stream_task_shutdown_error | task=%s error=%s", task.get_name(), exc)


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
    price: float = 0.0


_TOP_BOOK_DEPTH_LEVELS = 20


def _attach_task_guard(task: asyncio.Task[Any]) -> None:
    """Retrieve task exceptions so asyncio does not log 'Future exception was never retrieved'."""

    def _done(t: asyncio.Task[Any]) -> None:
        if t.cancelled():
            return
        try:
            exc = t.exception()
        except asyncio.CancelledError:
            return
        if exc is None:
            return
        if HuntCcxtStreams._ws_transport_fatal(exc):
            LOG.debug("hunt_ccxt_task_ws_drop | task=%s error=%s", t.get_name(), exc)
        else:
            LOG.warning("hunt_ccxt_task_failed | task=%s error=%s", t.get_name(), exc)

    task.add_done_callback(_done)


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
    _kline_closed_open_ms_5m: dict[str, int] = field(default_factory=dict)
    _kline_waiting_5m: dict[str, _ClosedKlineBar] = field(default_factory=dict)
    _kline_ready_5m: dict[str, _ClosedKlineBar] = field(default_factory=dict)
    _last_kline_open_ms_5m: dict[str, int] = field(default_factory=dict)
    _kline_closed_open_ms_15m: dict[str, int] = field(default_factory=dict)
    _kline_waiting_15m: dict[str, _ClosedKlineBar] = field(default_factory=dict)
    _kline_ready_15m: dict[str, _ClosedKlineBar] = field(default_factory=dict)
    _last_kline_open_ms_15m: dict[str, int] = field(default_factory=dict)
    kline_ws_enabled: bool = True
    mark_price_enabled: bool = True
    _mark_state: dict[str, tuple[int, float, float, float, float]] = field(default_factory=dict)
    _last_msg_ms: int = 0
    # live book/ticker/funding data from new multiplexed streams
    _live_books: dict[str, dict[str, Any]] = field(default_factory=dict)
    _live_tickers: dict[str, dict[str, float]] = field(default_factory=dict)
    _live_bbo: dict[str, dict[str, float]] = field(default_factory=dict)
    _live_funding: dict[str, dict[str, float]] = field(default_factory=dict)
    # cross-exchange funding: {exchange_name: {binance_symbol: {rate, mark, index}}}
    _live_funding_by_exchange: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)
    _secondary_pro_clients: dict[str, Any] = field(default_factory=dict)
    _secondary_funding_disabled: set[str] = field(default_factory=set)
    _pro_ex: Any | None = field(default=None, repr=False)
    _pro_specs: list[tuple[str, Any]] = field(default_factory=list)
    _reset_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _last_pro_reset: float = 0.0

    @property
    def kline_5m_enabled(self) -> bool:
        return self.kline_ws_enabled and _kline_ws_5m_enabled()

    @property
    def kline_15m_enabled(self) -> bool:
        return self.kline_ws_enabled and _kline_ws_15m_enabled()

    @property
    def cross_ws_connected(self) -> bool:
        """True when secondary-exchange funding WS tasks are active."""
        import os

        enabled = os.getenv("HUNT_CROSS_WS", "").strip().lower() in {"1", "true", "yes"}
        if not enabled or not self._connected:
            return False
        active = [
            name
            for name in self._secondary_pro_clients
            if name not in self._secondary_funding_disabled
        ]
        return bool(active)

    def set_symbols(self, symbols: list[str]) -> None:
        trimmed = [to_binance_symbol(s) for s in symbols if s][:_MAX_SYMBOL_STREAMS]
        new_set = set(trimmed)
        if new_set != self._symbols:
            self._symbols = new_set
            self._symbols_dirty = True

    @staticmethod
    def _ws_has(ex: Any, method: str) -> bool:
        return getattr(ex, "has", {}).get(method) is True

    @staticmethod
    def _ws_binance_id(ex: Any, raw: str) -> str | None:
        raw_sym = str(raw or "").strip()
        if not raw_sym:
            return None
        return from_ccxt_symbol(raw_sym, exchange=ex)

    def _ws_ex(self) -> Any:
        if self._pro_ex is None:
            msg = "HuntCcxtStreams.start() must be called before watch loops"
            raise RuntimeError(msg)
        return self._pro_ex

    @staticmethod
    def _ws_transport_fatal(exc: Exception) -> bool:
        text = repr(exc)
        name = type(exc).__name__
        return (
            "1006" in text
            or name in {"NetworkError", "RequestTimeout", "ExchangeNotAvailable"}
            or "ConnectionClosed" in name
        )

    def _spawn_pro_tasks(self, specs: list[tuple[str, Any]]) -> list[asyncio.Task[None]]:
        tasks: list[asyncio.Task[None]] = []
        for name, fn in specs:
            task = asyncio.create_task(fn(), name=name)
            _attach_task_guard(task)
            tasks.append(task)
        return tasks

    async def _reconnect_binance_pro(self) -> None:
        """Cancel all Binance Pro watch tasks, reset client, respawn (CCXT wiki pattern)."""
        async with self._reset_lock:
            now = time.monotonic()
            if now - self._last_pro_reset < 8.0:
                return
            self._last_pro_reset = now
            if not self._pro_specs:
                return
            LOG.info("hunt_ccxt_pro_reconnect_start")
            pro_tasks = [t for t in self._tasks if t.get_name() != "hunt_ccxt_funding_cross"]
            for task in pro_tasks:
                task.cancel()
            for task in pro_tasks:
                await _join_cancelled_task(task)
            self._tasks = [t for t in self._tasks if t.get_name() == "hunt_ccxt_funding_cross"]
            try:
                self._pro_ex = await self.client.reset_pro_exchange()
            except Exception as re_exc:
                LOG.warning("hunt_ccxt_pro_reconnect_failed | error=%s", re_exc)
                return
            self._tasks.extend(self._spawn_pro_tasks(self._pro_specs))
            LOG.info(
                "hunt_ccxt_pro_reconnected | tasks=%s",
                [t.get_name() for t in self._tasks if t.get_name() != "hunt_ccxt_funding_cross"],
            )

    async def _on_ws_loop_error(self, label: str, exc: Exception) -> None:
        LOG.warning("hunt_ccxt_%s_error | %s", label, repr(exc))
        if self._ws_transport_fatal(exc):
            await self._reconnect_binance_pro()
            return
        await asyncio.sleep(2.0)

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

    def agg_trade_buy_ratio(
        self,
        symbol: str,
        *,
        window_seconds: int = 60,
        use_nq: bool | None = None,
    ) -> float | None:
        """Taker buy share in window (0–1), not signed delta — §3 rename."""
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

    def agg_trade_delta(
        self,
        symbol: str,
        *,
        window_seconds: int = 60,
        use_nq: bool | None = None,
    ) -> float | None:
        """Deprecated alias — use agg_trade_buy_ratio (buy share, not delta)."""
        return self.agg_trade_buy_ratio(symbol, window_seconds=window_seconds, use_nq=use_nq)

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

    def ws_cvd(self, symbol: str, *, window_seconds: int = 60, use_nq: bool | None = None) -> float | None:
        """Rolling signed volume delta (buy qty − sell qty) from watch_trades."""
        sym = to_binance_symbol(symbol)
        buf = self._agg_points.get(sym)
        if not buf:
            return None
        if use_nq is None:
            use_nq = orderflow_use_nq(sym)
        cutoff = int(time.time() * 1000) - window_seconds * 1000
        cvd = 0.0
        for pt in buf:
            if pt.ts_ms < cutoff:
                continue
            vol = pt.qty if use_nq else pt.qty_full
            cvd += vol if pt.is_buy else -vol
        return round(cvd, 6)

    def ws_price_change_pct(self, symbol: str, *, window_seconds: int = 60) -> float | None:
        """Trade-price change across a rolling WS window (for CVD divergence)."""
        sym = to_binance_symbol(symbol)
        buf = self._agg_points.get(sym)
        if not buf:
            return None
        cutoff = int(time.time() * 1000) - window_seconds * 1000
        first_px = last_px = 0.0
        for pt in buf:
            if pt.ts_ms < cutoff or pt.price <= 0:
                continue
            if first_px <= 0:
                first_px = pt.price
            last_px = pt.price
        if first_px <= 0 or last_px <= 0:
            return None
        return round((last_px - first_px) / first_px * 100.0, 4)

    def live_book(self, symbol: str) -> dict[str, Any] | None:
        """Latest L2 order book snapshot from watch_order_book_for_symbols."""
        return self._live_books.get(to_binance_symbol(symbol))

    def live_ticker(self, symbol: str) -> dict[str, float] | None:
        """Latest 24h ticker from watch_tickers."""
        return self._live_tickers.get(to_binance_symbol(symbol))

    def live_bbo(self, symbol: str) -> dict[str, float] | None:
        """Top-of-book bid/ask from watch_bids_asks (lower CPU than full tickers)."""
        return self._live_bbo.get(to_binance_symbol(symbol))

    def live_funding(self, symbol: str) -> dict[str, float] | None:
        """Latest funding rate from watch_funding_rates."""
        return self._live_funding.get(to_binance_symbol(symbol))

    def live_funding_cross(self, symbol: str) -> dict[str, dict[str, float]]:
        """Latest funding rates from secondary exchanges keyed by exchange name."""
        sym = to_binance_symbol(symbol)
        return {
            ex: data[sym]
            for ex, data in self._live_funding_by_exchange.items()
            if sym in data
        }

    def snapshot(self, symbol: str) -> dict[str, Any]:
        sym = to_binance_symbol(symbol)
        liq = self.liquidation_rollups(sym, window_seconds=300)
        liq_60 = self.liquidation_rollups(sym, window_seconds=60)
        fresh = self._last_msg_ms > 0 and time.time() * 1000 - self._last_msg_ms < 60_000
        book = self._live_books.get(sym) or {}
        ticker = self._live_tickers.get(sym) or {}
        funding = self._live_funding.get(sym) or {}
        mark_px = funding.get("markPrice") or ticker.get("last")
        if mark_px is None and book.get("bid") and book.get("ask"):
            mark_px = (float(book["bid"]) + float(book["ask"])) / 2.0
        heatmap = None
        bracket_tiers = None
        if mark_px is not None:
            try:
                bracket_tiers = self.client.get_cached_leverage_tiers(sym)
                heatmap = build_liquidation_heatmap(
                    self._force_order_buffer,
                    symbol=sym,
                    current_price=float(mark_px),
                    bracket_tiers=bracket_tiers,
                )
            except (TypeError, ValueError):
                heatmap = None
                bracket_tiers = None
        liq_source = "binance_brackets" if bracket_tiers else "default"
        ratio_60 = self.agg_trade_buy_ratio(sym, window_seconds=60)
        ratio_30 = self.agg_trade_buy_ratio(sym, window_seconds=30)
        return {
            "ws_routed_market": True,
            "ws_base_url": "ccxt.pro",
            "ws_connected": self._connected and fresh,
            "cross_ws_connected": self.cross_ws_connected,
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
            "agg_trade_buy_ratio_60s": ratio_60,
            "agg_trade_buy_ratio_30s": ratio_30,
            # Legacy keys (buy share, not signed delta)
            "agg_trade_delta_60s": ratio_60,
            "agg_trade_delta_30s": ratio_30,
            "agg_trade_source": "ccxt_watch_trades",
            "agg_rpi_skew_60s": self.agg_rpi_skew(sym, window_seconds=60),
            "ws_cvd_1m": self.ws_cvd(sym, window_seconds=60),
            "ws_cvd_5m": self.ws_cvd(sym, window_seconds=300),
            "ws_price_chg_1m": self.ws_price_change_pct(sym, window_seconds=60),
            "ws_price_chg_5m": self.ws_price_change_pct(sym, window_seconds=300),
            f"kline_{_KLINE_INTERVAL}_last_close_ms": self._kline_closed_open_ms.get(sym),
            "kline_ws_interval": _KLINE_INTERVAL,
            # live book microstructure
            "live_bid": book.get("bid"),
            "live_ask": book.get("ask"),
            "live_depth_imbalance": book.get("depth_imbalance"),
            "ws_depth_imbalance": book.get("ws_depth_imbalance"),
            "live_microprice_bias": book.get("microprice_bias"),
            # live ticker
            "live_quote_volume": ticker.get("quoteVolume"),
            "live_price_change_pct": ticker.get("percentage"),
            # live funding
            "live_funding_rate": funding.get("fundingRate"),
            "live_mark_price": funding.get("markPrice"),
            "live_index_price": funding.get("indexPrice"),
            **(self.mark_snapshot(sym) or {}),
            **heatmap_to_market_dict(heatmap, prospective_source=liq_source),
        }

    def _promote_kline_grace(self, *, interval: str = _KLINE_INTERVAL) -> None:
        now_ms = int(time.time() * 1000)
        grace_ms = int(_kline_grace_sec() * 1000)
        if interval == _KLINE_5M_INTERVAL:
            waiting, ready, closed_ms = (
                self._kline_waiting_5m,
                self._kline_ready_5m,
                self._kline_closed_open_ms_5m,
            )
        elif interval == _KLINE_15M_INTERVAL:
            waiting, ready, closed_ms = (
                self._kline_waiting_15m,
                self._kline_ready_15m,
                self._kline_closed_open_ms_15m,
            )
        else:
            waiting, ready, closed_ms = (
                self._kline_waiting,
                self._kline_ready,
                self._kline_closed_open_ms,
            )
        for sym, bar in list(waiting.items()):
            if now_ms - bar.received_ms < grace_ms:
                continue
            prev = closed_ms.get(sym)
            if prev != bar.open_ms:
                closed_ms[sym] = bar.open_ms
            ready[sym] = bar
            waiting.pop(sym, None)

    def pop_kline_close_triggers(self) -> set[str]:
        self._promote_kline_grace()
        if self.kline_5m_enabled:
            self._promote_kline_grace(interval=_KLINE_5M_INTERVAL)
        if self.kline_15m_enabled:
            self._promote_kline_grace(interval=_KLINE_15M_INTERVAL)
        return set(self._kline_ready)

    def consume_kline_close_triggers(self, symbols: set[str] | frozenset[str]) -> None:
        for sym in symbols:
            sym_n = to_binance_symbol(str(sym))
            self._kline_ready.pop(sym_n, None)
            if self.kline_5m_enabled:
                self._kline_ready_5m.pop(sym_n, None)
            if self.kline_15m_enabled:
                self._kline_ready_15m.pop(sym_n, None)

    def _bar_overlay(self, bar: _ClosedKlineBar, *, interval: str) -> dict[str, Any]:
        body = abs(bar.c - bar.o)
        full = max(bar.h - bar.l, 1e-12)
        upper_wick = bar.h - max(bar.o, bar.c)
        lower_wick = min(bar.o, bar.c) - bar.l
        return {
            "close": round(bar.c, 6),
            "closed_bar": True,
            "ws_open_ms": bar.open_ms,
            "ws_grace_s": _kline_grace_sec(),
            "ws_interval": interval,
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

    def closed_kline_overlay(
        self,
        symbol: str,
        *,
        interval: str = _KLINE_INTERVAL,
    ) -> dict[str, Any] | None:
        if interval == _KLINE_5M_INTERVAL and not self.kline_5m_enabled:
            return None
        if interval == _KLINE_15M_INTERVAL and not self.kline_15m_enabled:
            return None
        self._promote_kline_grace(interval=interval)
        sym = to_binance_symbol(symbol)
        if interval == _KLINE_5M_INTERVAL:
            ready = self._kline_ready_5m
        elif interval == _KLINE_15M_INTERVAL:
            ready = self._kline_ready_15m
        else:
            ready = self._kline_ready
        bar = ready.get(sym)
        if bar is None:
            return None
        return self._bar_overlay(bar, interval=interval)

    def closed_5m_overlay(self, symbol: str) -> dict[str, Any] | None:
        return self.closed_kline_overlay(symbol, interval=_KLINE_5M_INTERVAL)

    def closed_15m_overlay(self, symbol: str) -> dict[str, Any] | None:
        return self.closed_kline_overlay(symbol, interval=_KLINE_15M_INTERVAL)

    def closed_1m_kline_overlay(self, symbol: str) -> dict[str, Any] | None:
        return self.closed_kline_overlay(symbol, interval=_KLINE_INTERVAL)

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

    def _record_liquidation(self, item: dict[str, Any], *, exchange: Any) -> None:
        info = item.get("info") if isinstance(item.get("info"), dict) else item
        sym = self._ws_binance_id(exchange, str(item.get("symbol") or info.get("s") or ""))
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
        px = float(trade.get("price") or info.get("p") or 0)
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
                price=px,
            )
        )

    def _on_closed_kline(self, sym: str, candle: list[Any], *, interval: str = _KLINE_INTERVAL) -> None:
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
        bar = _ClosedKlineBar(
            open_ms=open_ms,
            o=o,
            h=h,
            l=low,
            c=c,
            v=v,
            received_ms=int(time.time() * 1000),
        )
        if interval == _KLINE_5M_INTERVAL:
            self._kline_waiting_5m[sym] = bar
        elif interval == _KLINE_15M_INTERVAL:
            self._kline_waiting_15m[sym] = bar
        else:
            self._kline_waiting[sym] = bar

    def _on_ohlcv_update(
        self,
        sym: str,
        ohlcv: list[list[Any]],
        *,
        interval: str = _KLINE_INTERVAL,
    ) -> None:
        if not ohlcv:
            return
        latest_open = int(ohlcv[-1][0])
        if interval == _KLINE_5M_INTERVAL:
            last_open = self._last_kline_open_ms_5m
        elif interval == _KLINE_15M_INTERVAL:
            last_open = self._last_kline_open_ms_15m
        else:
            last_open = self._last_kline_open_ms
        prev_open = last_open.get(sym)
        if prev_open is not None and latest_open != prev_open and len(ohlcv) >= 2:
            self._on_closed_kline(sym, ohlcv[-2], interval=interval)
        last_open[sym] = latest_open

    async def start(self) -> None:
        if self._tasks:
            return
        self._stop.clear()
        await self.client.load_markets()
        self._pro_ex = await self.client.acquire_pro_exchange()
        ex = self._pro_ex
        specs: list[tuple[str, Any]] = []
        if self._ws_has(ex, "watchLiquidationsForSymbols"):
            specs.append(("hunt_ccxt_liq", self._watch_liquidations_mux))
        elif self._ws_has(ex, "watchLiquidations"):
            specs.append(("hunt_ccxt_liq", self._watch_liquidations_symbol))
        if self.mark_price_enabled and self._ws_has(ex, "watchMarkPrices"):
            specs.append(("hunt_ccxt_mark", self._watch_mark_prices))
        if self._ws_has(ex, "watchTradesForSymbols"):
            specs.append(("hunt_ccxt_trades", self._watch_trades_mux))
        if self.kline_ws_enabled and (
            self._ws_has(ex, "watchOHLCVForSymbols") or self._ws_has(ex, "watchOHLCV")
        ):
            specs.append(("hunt_ccxt_kline", self._watch_ohlcv_mux))
            if self.kline_5m_enabled:
                specs.append(("hunt_ccxt_kline_5m", self._watch_ohlcv_5m_mux))
            if self.kline_15m_enabled:
                specs.append(("hunt_ccxt_kline_15m", self._watch_ohlcv_15m_mux))
        elif self.kline_ws_enabled:
            self.kline_ws_enabled = False
            LOG.info("hunt_ccxt_kline_disabled | reason=exchange_has_no_watch_ohlcv")
        if self._ws_has(ex, "watchOrderBookForSymbols"):
            specs.append(("hunt_ccxt_book", self._watch_order_book_mux))
        if self._ws_has(ex, "watchTickers"):
            specs.append(("hunt_ccxt_tickers", self._watch_tickers_mux))
        if self._ws_has(ex, "watchBidsAsks"):
            specs.append(("hunt_ccxt_bbo", self._watch_bids_asks_mux))
        if self._ws_has(ex, "watchFundingRates"):
            specs.append(("hunt_ccxt_funding", self._watch_funding_rates_mux))
        cross_ws = os.getenv("HUNT_CROSS_WS", "").strip().lower() in {"1", "true", "yes"}
        if cross_ws:
            specs.append(("hunt_ccxt_funding_cross", self._watch_secondary_funding_mux))
        else:
            LOG.info("hunt_cross_ws_disabled | hint=set HUNT_CROSS_WS=1 for Bybit/OKX WS")
        if not specs:
            LOG.warning("hunt_ccxt_streams_no_capabilities | ws_plane=binance_future")
        self._pro_specs = [(n, fn) for n, fn in specs if n != "hunt_ccxt_funding_cross"]
        self._tasks = self._spawn_pro_tasks(self._pro_specs)
        cross = [(n, fn) for n, fn in specs if n == "hunt_ccxt_funding_cross"]
        self._tasks.extend(self._spawn_pro_tasks(cross))
        self._connected = True
        LOG.info("hunt_ccxt_streams_started | tasks=%s", [n for n, _ in specs])

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            await _join_cancelled_task(task)
        self._tasks.clear()
        for name, ex in self._secondary_pro_clients.items():
            await close_exchange_async(ex, label=f"secondary_pro:{name}")
        self._secondary_pro_clients.clear()
        self._pro_ex = None
        self._connected = False

    async def _watch_liquidations_mux(self) -> None:
        """All-symbol liquidations via watch_liquidations_for_symbols."""
        ex = self._ws_ex()
        while not self._stop.is_set():
            syms = self._ccxt_symbols()
            if not syms:
                await asyncio.sleep(0.5)
                continue
            try:
                items = await ex.watch_liquidations_for_symbols(syms)
                self._touch()
                batch = items if isinstance(items, list) else [items]
                for item in batch:
                    if isinstance(item, dict):
                        self._record_liquidation(item, exchange=ex)
            except asyncio.CancelledError:
                break
            except defensive_exc_types(Exception) as exc:
                await self._on_ws_loop_error("liq_mux", exc)

    async def _watch_liquidations_symbol(self) -> None:
        """Fallback: round-robin watch_liquidations(symbol) per subscribed symbol."""
        ex = self._ws_ex()
        idx = 0
        while not self._stop.is_set():
            syms = self._ccxt_symbols()
            if not syms:
                await asyncio.sleep(0.5)
                continue
            ccxt_sym = syms[idx % len(syms)]
            idx += 1
            try:
                items = await ex.watch_liquidations(ccxt_sym)
                self._touch()
                batch = items if isinstance(items, list) else [items]
                for item in batch:
                    if isinstance(item, dict):
                        self._record_liquidation(item, exchange=ex)
            except asyncio.CancelledError:
                break
            except defensive_exc_types(Exception) as exc:
                await self._on_ws_loop_error("liq_symbol", exc)

    async def _watch_mark_prices(self) -> None:
        if not self.mark_price_enabled:
            return
        ex = self._ws_ex()
        while not self._stop.is_set():
            try:
                prices = await ex.watch_mark_prices()
                self._touch()
                now_ms = int(time.time() * 1000)
                items = prices.values() if isinstance(prices, dict) else prices
                for item in items if isinstance(items, list) else []:
                    if not isinstance(item, dict):
                        continue
                    sym = self._ws_binance_id(ex, str(item.get("symbol") or ""))
                    if not sym or sym not in self._symbols:
                        continue
                    mark = float(item.get("markPrice") or item.get("last") or 0)
                    index = float(item.get("indexPrice") or 0)
                    funding = float(item.get("fundingRate") or 0)
                    info = item.get("info") if isinstance(item.get("info"), dict) else {}
                    ap_raw = info.get("ap")
                    ap = float(ap_raw) if ap_raw is not None else 0.0
                    if mark > 0:
                        self._mark_state[sym] = (now_ms, mark, index, funding, ap)
                        self._live_funding[sym] = {
                            "markPrice": mark,
                            "indexPrice": index,
                            "fundingRate": funding,
                        }
                        self.client.update_basis_from_websocket(sym, mark, index if index > 0 else None)
            except asyncio.CancelledError:
                break
            except defensive_exc_types(Exception) as exc:
                await self._on_ws_loop_error("mark", exc)

    def _ccxt_symbols(self) -> list[str]:
        ex = self._ws_ex()
        return [to_ccxt_symbol(s, exchange=ex) for s in sorted(self._symbols)]

    async def _watch_trades_mux(self) -> None:
        """Single multiplexed trades stream for all symbols via watch_trades_for_symbols."""
        ex = self._ws_ex()
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
                    sym = self._ws_binance_id(ex, str(trade.get("symbol") or ""))
                    if sym:
                        self._record_trade(sym, trade)
            except asyncio.CancelledError:
                break
            except defensive_exc_types(Exception) as exc:
                await self._on_ws_loop_error("trades", exc)

    async def _watch_ohlcv_mux(self) -> None:
        """Single multiplexed OHLCV stream for all symbols via watch_ohlcv_for_symbols."""
        if not self.kline_ws_enabled:
            return
        ex = self._ws_ex()
        while not self._stop.is_set():
            syms = self._ccxt_symbols()
            if not syms:
                await asyncio.sleep(0.5)
                continue
            try:
                if self._ws_has(ex, "watchOHLCVForSymbols"):
                    pairs = [(s, _KLINE_INTERVAL) for s in syms]
                    result = await ex.watch_ohlcv_for_symbols(pairs)
                    self._touch()
                    if isinstance(result, dict):
                        for ccxt_sym, tf_map in result.items():
                            sym = self._ws_binance_id(ex, str(ccxt_sym))
                            if not sym or not isinstance(tf_map, dict):
                                continue
                            ohlcv = tf_map.get(_KLINE_INTERVAL)
                            if isinstance(ohlcv, list):
                                self._on_ohlcv_update(sym, ohlcv)
                else:
                    sym = syms[0]
                    ohlcv = await ex.watch_ohlcv(sym, _KLINE_INTERVAL)
                    self._touch()
                    bin_sym = self._ws_binance_id(ex, sym)
                    if isinstance(ohlcv, list) and bin_sym:
                        self._on_ohlcv_update(bin_sym, ohlcv)
            except asyncio.CancelledError:
                break
            except defensive_exc_types(Exception) as exc:
                await self._on_ws_loop_error("kline_ws", exc)

    async def _watch_ohlcv_5m_mux(self) -> None:
        """Multiplexed 5m OHLCV — overlays REST ``5m_closed`` on confirm path."""
        if not self.kline_5m_enabled:
            return
        ex = self._ws_ex()
        while not self._stop.is_set():
            syms = self._ccxt_symbols()
            if not syms:
                await asyncio.sleep(0.5)
                continue
            try:
                if self._ws_has(ex, "watchOHLCVForSymbols"):
                    pairs = [(s, _KLINE_5M_INTERVAL) for s in syms]
                    result = await ex.watch_ohlcv_for_symbols(pairs)
                    self._touch()
                    if isinstance(result, dict):
                        for ccxt_sym, tf_map in result.items():
                            sym = self._ws_binance_id(ex, str(ccxt_sym))
                            if not sym or not isinstance(tf_map, dict):
                                continue
                            ohlcv = tf_map.get(_KLINE_5M_INTERVAL)
                            if isinstance(ohlcv, list):
                                self._on_ohlcv_update(sym, ohlcv, interval=_KLINE_5M_INTERVAL)
                else:
                    sym = syms[0]
                    ohlcv = await ex.watch_ohlcv(sym, _KLINE_5M_INTERVAL)
                    self._touch()
                    bin_sym = self._ws_binance_id(ex, sym)
                    if isinstance(ohlcv, list) and bin_sym:
                        self._on_ohlcv_update(bin_sym, ohlcv, interval=_KLINE_5M_INTERVAL)
            except asyncio.CancelledError:
                break
            except defensive_exc_types(Exception) as exc:
                await self._on_ws_loop_error("kline_ws_5m", exc)

    async def _watch_ohlcv_15m_mux(self) -> None:
        """Multiplexed 15m OHLCV — overlays REST ``15m_closed`` on confirm path."""
        if not self.kline_15m_enabled:
            return
        ex = self._ws_ex()
        while not self._stop.is_set():
            syms = self._ccxt_symbols()
            if not syms:
                await asyncio.sleep(0.5)
                continue
            try:
                if self._ws_has(ex, "watchOHLCVForSymbols"):
                    pairs = [(s, _KLINE_15M_INTERVAL) for s in syms]
                    result = await ex.watch_ohlcv_for_symbols(pairs)
                    self._touch()
                    if isinstance(result, dict):
                        for ccxt_sym, tf_map in result.items():
                            sym = self._ws_binance_id(ex, str(ccxt_sym))
                            if not sym or not isinstance(tf_map, dict):
                                continue
                            ohlcv = tf_map.get(_KLINE_15M_INTERVAL)
                            if isinstance(ohlcv, list):
                                self._on_ohlcv_update(sym, ohlcv, interval=_KLINE_15M_INTERVAL)
                else:
                    sym = syms[0]
                    ohlcv = await ex.watch_ohlcv(sym, _KLINE_15M_INTERVAL)
                    self._touch()
                    bin_sym = self._ws_binance_id(ex, sym)
                    if isinstance(ohlcv, list) and bin_sym:
                        self._on_ohlcv_update(bin_sym, ohlcv, interval=_KLINE_15M_INTERVAL)
            except asyncio.CancelledError:
                break
            except defensive_exc_types(Exception) as exc:
                await self._on_ws_loop_error("kline_ws_15m", exc)

    async def _watch_order_book_mux(self) -> None:
        """Live L2 order book via watch_order_book_for_symbols → depth imbalance + microprice."""
        from hunt_core.market.client import (
            depth_imbalance_from_book,
            depth_imbalance_from_levels,
            microprice_bias_from_book,
        )

        ex = self._ws_ex()
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
                sym = self._ws_binance_id(ex, str(book.get("symbol") or ""))
                if not sym:
                    continue
                bids = book.get("bids") or []
                asks = book.get("asks") or []
                bid_p = float(bids[0][0]) if bids else None
                ask_p = float(asks[0][0]) if asks else None
                bid_q = float(bids[0][1]) if bids else None
                ask_q = float(asks[0][1]) if asks else None
                di_l1 = depth_imbalance_from_book(bid_qty=bid_q, ask_qty=ask_q, delta_ratio=None)
                di_top20 = depth_imbalance_from_levels(bids, asks, top_n=_TOP_BOOK_DEPTH_LEVELS)
                mp = microprice_bias_from_book(bid=bid_p, ask=ask_p, bid_qty=bid_q, ask_qty=ask_q, delta_ratio=None)
                self._live_books[sym] = {
                    "bid": bid_p,
                    "ask": ask_p,
                    "bid_qty": bid_q,
                    "ask_qty": ask_q,
                    "depth_imbalance": di_l1,
                    "ws_depth_imbalance": di_top20,
                    "microprice_bias": mp,
                }
            except asyncio.CancelledError:
                break
            except defensive_exc_types(Exception) as exc:
                await self._on_ws_loop_error("book", exc)

    async def _watch_bids_asks_mux(self) -> None:
        """BBO spread via watch_bids_asks (lighter than full watch_tickers)."""
        ex = self._ws_ex()
        if not getattr(ex, "has", {}).get("watchBidsAsks"):
            return
        while not self._stop.is_set():
            syms = self._ccxt_symbols()
            if not syms:
                await asyncio.sleep(0.5)
                continue
            try:
                items = await ex.watch_bids_asks(syms)
                self._touch()
                batch = items.values() if isinstance(items, dict) else [items]
                for item in batch:
                    if not isinstance(item, dict):
                        continue
                    sym = self._ws_binance_id(ex, str(item.get("symbol") or ""))
                    bid = float(item.get("bid") or 0)
                    ask = float(item.get("ask") or 0)
                    if not sym or bid <= 0 or ask <= 0:
                        continue
                    spread_pct = (ask - bid) / bid * 100.0 if bid > 0 else 0.0
                    self._live_bbo[sym] = {
                        "bid": bid,
                        "ask": ask,
                        "spread_pct": round(spread_pct, 5),
                    }
            except asyncio.CancelledError:
                break
            except defensive_exc_types(Exception) as exc:
                await self._on_ws_loop_error("bbo", exc)

    async def _watch_tickers_mux(self) -> None:
        """Rolling 24h stats for all symbols via watch_tickers."""
        ex = self._ws_ex()
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
                    sym = self._ws_binance_id(ex, str(item.get("symbol") or ""))
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
                await self._on_ws_loop_error("tickers", exc)

    async def _watch_funding_rates_mux(self) -> None:
        """Live funding/mark/index prices via watch_funding_rates."""
        ex = self._ws_ex()
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
                    sym = self._ws_binance_id(ex, str(item.get("symbol") or ""))
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
                await self._on_ws_loop_error("funding", exc)

    @staticmethod
    def _funding_ws_permanent(exc: Exception) -> bool:
        text = str(exc).lower()
        name = type(exc).__name__
        return name in {"NotSupported", "NotImplemented"} or "not supported" in text

    async def _reset_secondary_pro(self, name: str) -> None:
        ex = self._secondary_pro_clients.pop(name, None)
        if ex is not None:
            await close_exchange_async(ex, label=f"secondary_pro_reset:{name}")
        try:
            fresh = create_pro_secondary_swap(
                name,
                proxy_url=self.client._proxy_url,
                trust_env=self.client._trust_env,
                timeout_ms=self.client._timeout_ms,
            )
            await fresh.load_markets()
            self._secondary_pro_clients[name] = fresh
            LOG.info("secondary_funding_ws_reset | exchange=%s", name)
        except Exception as exc:
            LOG.warning("secondary_funding_ws_reset_failed | exchange=%s error=%s", name, exc)

    async def _watch_one_secondary_funding(self, name: str) -> None:
        """Continuous watch_funding_rates loop for one secondary exchange."""
        ex = self._secondary_pro_clients.get(name)
        if ex is None or not self._ws_has(ex, "watchFundingRates"):
            LOG.info(
                "secondary_funding_ws_skipped | exchange=%s reason=watchFundingRates_unsupported",
                name,
            )
            return
        backoff_s = 5.0
        while not self._stop.is_set():
            if name in self._secondary_funding_disabled:
                return
            ex = self._secondary_pro_clients.get(name)
            if ex is None:
                return
            syms_bin = list(self._symbols)
            if not syms_bin:
                await asyncio.sleep(2.0)
                continue
            ccxt_syms = [
                resolved
                for s in syms_bin
                if (resolved := try_resolve_linear_usdt_swap(s, exchange=ex))
            ]
            if not ccxt_syms:
                await asyncio.sleep(2.0)
                continue
            try:
                rates = await ex.watch_funding_rates(ccxt_syms)
                items = rates.values() if isinstance(rates, dict) else []
                bucket = self._live_funding_by_exchange.setdefault(name, {})
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    sym = self._ws_binance_id(ex, str(item.get("symbol") or ""))
                    if not sym:
                        continue
                    mark = float(item.get("markPrice") or 0)
                    index = float(item.get("indexPrice") or 0)
                    funding = float(item.get("fundingRate") or 0)
                    bucket[sym] = {"markPrice": mark, "indexPrice": index, "fundingRate": funding}
                backoff_s = 5.0
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if self._funding_ws_permanent(exc):
                    self._secondary_funding_disabled.add(name)
                    LOG.warning(
                        "secondary_funding_ws_disabled | exchange=%s error=%s",
                        name,
                        exc,
                    )
                    return
                LOG.warning("secondary_funding_ws_error | exchange=%s error=%s", name, exc)
                if self._ws_transport_fatal(exc):
                    await self._reset_secondary_pro(name)
                    backoff_s = 5.0
                    await asyncio.sleep(2.0)
                    continue
                await asyncio.sleep(min(60.0, backoff_s))
                backoff_s = min(60.0, backoff_s * 1.5)

    async def _watch_secondary_funding_mux(self) -> None:
        """Spawn per-exchange WS funding tasks for Bybit / OKX / Bitget."""
        tasks: list[asyncio.Task[None]] = []
        for name in configured_secondary_exchanges():
            ex_id = name
            ex: Any | None = None
            try:
                await asyncio.sleep(1.5)
                ex = create_pro_secondary_swap(
                    ex_id,
                    proxy_url=self.client._proxy_url,
                    trust_env=self.client._trust_env,
                    timeout_ms=self.client._timeout_ms,
                )
                await ex.load_markets()
                usdt_swap = sum(
                    1
                    for m in ex.markets.values()
                    if isinstance(m, dict)
                    and str(m.get("settle") or "").upper() == "USDT"
                    and str(m.get("type") or "") in {"swap", "future"}
                )
                if usdt_swap <= 0:
                    LOG.warning("secondary_no_usdt_swap_markets | exchange=%s", name)
                    await ex.close()
                    continue
                if not self._ws_has(ex, "watchFundingRates"):
                    LOG.info(
                        "secondary_funding_ws_skipped | exchange=%s reason=watchFundingRates_unsupported",
                        name,
                    )
                    await ex.close()
                    continue
                self._secondary_pro_clients[name] = ex
                task = asyncio.create_task(
                    self._watch_one_secondary_funding(name),
                    name=f"hunt_ccxt_funding_{name}",
                )
                _attach_task_guard(task)
                tasks.append(task)
                LOG.info("secondary_funding_ws_started | exchange=%s", name)
            except Exception as exc:
                LOG.warning("secondary_funding_ws_init_failed | exchange=%s error=%s", name, exc)
                if ex is not None:
                    await close_exchange_async(ex, label=f"secondary_pro_init:{name}")
        if not tasks:
            return
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            raise
