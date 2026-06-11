"""Lightweight Binance USD-M public WS for hunt — liquidations + aggTrade CVD.

Hunt needs sub-minute fuel (forceOrder cascades, live orderflow) that REST snapshots
miss. This module is intentionally small: no bot runtime, no event bus.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets import exceptions as ws_exceptions

from engine.errors import defensive_exc_types
from engine.market._ws_parsers import handle_force_order
from hunt_watch.param_store import orderflow_use_nq, ws_thresholds
from hunt_watch.scriptutil import configure_script_logging
from engine.market.network_proxy import websockets_connect_kwargs
from engine.market.ws import get_liquidation_event_count, get_liquidation_rollups

LOG = configure_script_logging("hunt_watch.ws_feed")

# Routed /market endpoint — legacy /stream decommissioned 2026-04-23 (Binance WS split).
_FSTREAM_WS = "wss://fstream.binance.com/market/stream"
# Binance allows 1024 streams/conn (2025-07-02); URL length is the practical cap here.
_MAX_SYMBOL_STREAMS = 24
_LIQ_BUFFER_MAX = 8_000
_AGG_BUFFER_MAX = 2_000
_KLINE_INTERVAL = "5m"
# Binance kline x=true may lag 0.2–2s (p95); grace before promote (report Q01 / B.16).
def _kline_grace_sec() -> float:
    return float(ws_thresholds().get("kline_grace_sec", 2.5))


@dataclass
class _ClosedKline5m:
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
    qty: float  # nq-preferred (RPI excluded, Binance 2025-12-31)
    qty_full: float  # q — full aggregate incl. RPI when present
    is_buy: bool  # taker buy when buyer is NOT maker


@dataclass
class HuntWsFeed:
    """Background WS: !forceOrder@arr + per-symbol aggTrade for watch universe."""

    proxy_url: str | None = None
    trust_env: bool = True
    _symbols: set[str] = field(default_factory=set)
    _force_order_buffer: collections.deque[tuple[int, str, str, float, float]] = field(
        default_factory=lambda: collections.deque(maxlen=_LIQ_BUFFER_MAX)
    )
    _agg_points: dict[str, collections.deque[_AggPoint]] = field(default_factory=dict)
    _task: asyncio.Task[None] | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _connected: bool = False
    _symbols_dirty: bool = False
    _active_url: str = ""
    _proxy_fail_streak: int = 0
    _kline_closed_open_ms: dict[str, int] = field(default_factory=dict)
    _kline_waiting: dict[str, _ClosedKline5m] = field(default_factory=dict)
    _kline_ready: dict[str, _ClosedKline5m] = field(default_factory=dict)
    kline_5m_enabled: bool = True
    # !markPrice@arr — one stream covers every symbol (no per-symbol budget):
    # live funding/basis between 60s REST polls for confirm/fuel inputs.
    # ts_ms, mark, index, funding, ap (mark MA — 2026-03-16)
    _mark_state: dict[str, tuple[int, float, float, float, float]] = field(default_factory=dict)
    mark_price_enabled: bool = True
    _last_msg_ms: int = 0

    def set_symbols(self, symbols: list[str]) -> None:
        trimmed = [s.upper() for s in symbols if s][: _MAX_SYMBOL_STREAMS]
        new_set = set(trimmed)
        if new_set != self._symbols:
            self._symbols = new_set
            self._symbols_dirty = True

    def liquidation_rollups(self, symbol: str, *, window_seconds: int = 300) -> dict[str, float] | None:
        return get_liquidation_rollups(
            self, symbol=symbol.upper(), window_seconds=window_seconds
        )

    def liquidation_events(self, symbol: str, *, window_seconds: int = 300) -> int:
        return get_liquidation_event_count(
            self, symbol=symbol.upper(), window_seconds=window_seconds
        )

    def agg_trade_delta(
        self,
        symbol: str,
        *,
        window_seconds: int = 60,
        use_nq: bool | None = None,
    ) -> float | None:
        """Taker buy ratio in rolling window (0=all sell, 1=all buy).

        Default uses ``nq`` (normal quantity, RPI excluded) per Binance Q03/D.35.
        """
        sym = symbol.upper()
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
        """(q - nq) / q — share of RPI volume in agg window; diagnostic only."""
        sym = symbol.upper()
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

    def snapshot(self, symbol: str) -> dict[str, Any]:
        sym = symbol.upper()
        liq = self.liquidation_rollups(sym, window_seconds=300)
        liq_60 = self.liquidation_rollups(sym, window_seconds=60)
        # connected == socket open AND frames actually flowing: proxy stalls
        # leave the socket "open" with zero traffic (observed 2026-06-10).
        fresh = (
            self._last_msg_ms > 0
            and time.time() * 1000 - self._last_msg_ms < 60_000
        )
        return {
            "ws_routed_market": True,
            "ws_base_url": _FSTREAM_WS,
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
            "agg_trade_source": "ws_nq",
            "agg_rpi_skew_60s": self.agg_rpi_skew(sym, window_seconds=60),
            "kline_5m_last_close_ms": self._kline_closed_open_ms.get(sym),
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
        """Symbols whose 5m bar passed grace since last consume — fast confirm tick."""
        self._promote_kline_grace()
        pending = set(self._kline_ready)
        return pending

    def closed_5m_overlay(self, symbol: str) -> dict[str, Any] | None:
        """WS-closed 5m OHLC for merging into REST 5m_closed (anti-stale)."""
        self._promote_kline_grace()
        sym = symbol.upper()
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

    def mark_snapshot(self, symbol: str, *, max_age_s: float = 10.0) -> dict[str, float] | None:
        """Live mark/index/funding/ap from WS — None when stale or absent."""
        rec = self._mark_state.get(symbol.upper())
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

    def _handle_mark_array(self, data: Any) -> None:
        if not isinstance(data, list):
            return
        now_ms = int(time.time() * 1000)
        for item in data:
            if not isinstance(item, dict):
                continue
            sym = str(item.get("s") or "").upper()
            # keep only watched symbols — the arr stream carries the full market
            if sym not in self._symbols:
                continue
            try:
                mark = float(item.get("p") or 0)
                index = float(item.get("i") or 0)
                funding = float(item.get("r") or 0)
                ap_raw = item.get("ap")
                ap = float(ap_raw) if ap_raw is not None else 0.0
            except (TypeError, ValueError):
                continue
            if mark > 0:
                self._mark_state[sym] = (now_ms, mark, index, funding, ap)

    def _stream_path(self) -> str:
        parts = ["!forceOrder@arr"]
        if self.mark_price_enabled:
            parts.append("!markPrice@arr@1s")
        for sym in sorted(self._symbols):
            parts.append(f"{sym.lower()}@aggTrade")
            if self.kline_5m_enabled:
                parts.append(f"{sym.lower()}@kline_{_KLINE_INTERVAL}")
        return "/".join(parts)

    def _ws_url(self) -> str:
        return f"{_FSTREAM_WS}?streams={self._stream_path()}"

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_loop(), name="hunt_ws_feed")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None
        self._connected = False

    async def _run_loop(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            url = self._ws_url()
            proxy_url = self.proxy_url
            if proxy_url and self._proxy_fail_streak >= 3:
                LOG.warning(
                    "hunt_ws_proxy_fallback_direct",
                    streak=self._proxy_fail_streak,
                )
                proxy_url = None
            try:
                ws_kwargs = websockets_connect_kwargs(
                    proxy_url=proxy_url, trust_env=self.trust_env
                )
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    open_timeout=15.0,
                    close_timeout=5.0,
                    **ws_kwargs,
                ) as ws:
                    self._connected = True
                    self._proxy_fail_streak = 0
                    self._symbols_dirty = False
                    self._active_url = url
                    delay = 1.0
                    stream_n = len(self._symbols) + 1
                    if self.kline_5m_enabled:
                        stream_n += len(self._symbols)
                    LOG.info(
                        "hunt_ws_connected",
                        streams=stream_n,
                        kline_5m=self.kline_5m_enabled,
                        url_len=len(url),
                    )
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        if self._symbols_dirty and self._ws_url() != self._active_url:
                            break
                        self._dispatch(raw)
            except asyncio.CancelledError:
                break
            except defensive_exc_types(ws_exceptions.WebSocketException) as exc:
                self._connected = False
                if self.proxy_url and proxy_url is not None:
                    self._proxy_fail_streak += 1
                LOG.warning("hunt_ws_disconnect", error=repr(exc), retry_in=delay)
                await asyncio.sleep(delay)
                delay = min(60.0, delay * 1.5)
            finally:
                self._connected = False

    def _dispatch(self, raw: str | bytes) -> None:
        self._last_msg_ms = int(time.time() * 1000)
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        stream = str(msg.get("stream") or "")
        data = msg.get("data")
        if "markprice" in stream.lower():
            self._handle_mark_array(data)
            return
        if not isinstance(data, dict):
            return
        if "forceorder" in stream.lower():
            handle_force_order(self, data)
            return
        if "@kline" in stream.lower():
            sym = str(data.get("s") or "").upper()
            if sym not in self._symbols:
                return
            k = data.get("k")
            if not isinstance(k, dict) or not k.get("x"):
                return
            try:
                open_ms = int(k.get("t") or 0)
                o = float(k.get("o") or 0)
                h = float(k.get("h") or 0)
                low = float(k.get("l") or 0)
                c = float(k.get("c") or 0)
                v = float(k.get("v") or 0)
            except (TypeError, ValueError):
                return
            if open_ms <= 0 or c <= 0:
                return
            now_ms = int(time.time() * 1000)
            self._kline_waiting[sym] = _ClosedKline5m(
                open_ms=open_ms,
                o=o,
                h=h,
                l=low,
                c=c,
                v=v,
                received_ms=now_ms,
            )
            return
        if "@aggtrade" in stream.lower():
            sym = str(data.get("s") or "").upper()
            if sym not in self._symbols:
                return
            try:
                qty_full = float(data.get("q") or 0)
                nq_raw = data.get("nq")
                qty_nq = float(nq_raw) if nq_raw is not None else qty_full
                qty = qty_nq if qty_nq > 0 else qty_full
                ts_ms = int(data.get("T") or time.time() * 1000)
                is_buy = not bool(data.get("m"))  # m=true => seller is taker
            except (TypeError, ValueError):
                return
            if qty <= 0 and qty_full <= 0:
                return
            buf = self._agg_points.setdefault(
                sym, collections.deque(maxlen=_AGG_BUFFER_MAX)
            )
            buf.append(
                _AggPoint(
                    ts_ms=ts_ms,
                    qty=qty,
                    qty_full=qty_full if qty_full > 0 else qty,
                    is_buy=is_buy,
                )
            )
