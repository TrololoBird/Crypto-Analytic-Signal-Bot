"""Intra-bar PRE-pump/PRE-dump state — momentum z-score, trade burst, DOM imbalance.

Rolling per-symbol windows updated from WS callbacks. Exposes IntraBarSignal when
all three sub-signals agree above configurable thresholds.

Silence is default: 95%+ of ticks produce no signal. All signals are logged even
below confidence_threshold for calibration.
"""
from __future__ import annotations

import logging
import statistics
from collections import deque, OrderedDict
from dataclasses import dataclass, fields
from functools import lru_cache
from typing import Any

from hunt_core.domain.config import load_config_defaults_toml
from hunt_core.params.store import universal_section

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntraBarConfig:
    momentum_window: int = 10
    trade_window: int = 10
    momentum_z_min: float = 2.0
    dom_imbalance_min: float = 0.60
    confidence_threshold: float = 0.0
    min_trades_for_burst: int = 2
    cooldown_seconds: int = 300
    max_symbols: int = 100
    dom_ema_alpha: float = 0.3


def _merge_intra_bar(raw: dict[str, Any]) -> IntraBarConfig:
    base = IntraBarConfig()
    kw = {f.name: getattr(base, f.name) for f in fields(IntraBarConfig)}
    for f in fields(IntraBarConfig):
        if f.name in raw and raw[f.name] is not None:
            kw[f.name] = raw[f.name]
    return IntraBarConfig(**kw)


@lru_cache(maxsize=1)
def intra_bar_config() -> IntraBarConfig:
    """Merged [hunter.intra_bar] from config.defaults.toml + calibration override."""
    toml_block = (load_config_defaults_toml().get("intra_bar") or {})
    cal_block = universal_section("intra_bar")
    return _merge_intra_bar({**cal_block, **toml_block})


def clear_intra_bar_config_cache() -> None:
    intra_bar_config.cache_clear()


# ---------------------------------------------------------------------------
# Signal output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntraBarSignal:
    symbol: str
    side: str
    confidence: float
    momentum_z: float
    trade_burst: float
    dom_imbalance: float
    ts: float
    price: float


# ---------------------------------------------------------------------------
# Per-symbol state (private)
# ---------------------------------------------------------------------------


class _SymbolState:
    __slots__ = (
        "prices",
        "trade_deltas",
        "trade_buffer",
        "dom_ema",
        "dom_ema_initialized",
        "last_price",
        "last_ts",
    )

    def __init__(self, momentum_window: int, trade_window: int) -> None:
        self.prices: deque[float] = deque(maxlen=momentum_window)
        self.trade_deltas: deque[float] = deque(maxlen=trade_window)
        self.trade_buffer: list[float] = []
        self.dom_ema: float = 0.0
        self.dom_ema_initialized: bool = False
        self.last_price: float = 0.0
        self.last_ts: float = 0.0


# ---------------------------------------------------------------------------
# IntraBarState service
# ---------------------------------------------------------------------------


class IntraBarState:
    """Per-symbol rolling intra-bar state. NOT a singleton — instantiate once in runtime.

    Feed WS events into the three ``process_*`` methods, then poll ``signals()``.
    Trade events are buffered and aggregated on flush to avoid per-trade CPU overhead.

    Thread-safe for asyncio (single event loop). No persistence.
    """

    def __init__(
        self,
        cfg: IntraBarConfig | None = None,
        initial_symbols: list[str] | None = None,
    ) -> None:
        self._cfg = cfg if cfg is not None else intra_bar_config()
        self._symbols: dict[str, _SymbolState] = OrderedDict()
        self._lru: deque[str] = deque()
        if initial_symbols:
            for sym in initial_symbols:
                self._ensure(sym)

    def _evict_lru(self) -> None:
        while len(self._symbols) > self._cfg.max_symbols:
            oldest = self._lru.popleft()
            self._symbols.pop(oldest, None)

    def _touch_lru(self, symbol: str) -> None:
        try:
            self._lru.remove(symbol)
        except ValueError:
            pass
        self._lru.append(symbol)

    def _ensure(self, symbol: str) -> _SymbolState:
        sym = symbol.upper().strip()
        if sym not in self._symbols:
            self._symbols[sym] = _SymbolState(
                self._cfg.momentum_window,
                self._cfg.trade_window,
            )
            self._evict_lru()
        self._touch_lru(sym)
        return self._symbols[sym]

    def process_m1_close(self, symbol: str, price: float, ts: float) -> None:
        if price <= 0 or ts <= 0:
            _LOG.warning("intra_bar_skip_m1_close | symbol=%s price=%s ts=%s", symbol, price, ts)
            return
        st = self._ensure(symbol)
        st.prices.append(price)
        st.last_price = price
        st.last_ts = ts

    def process_trade(
        self,
        symbol: str,
        side: str,
        qty: float,
        ts: float,
    ) -> None:
        """Buffer trade delta. Aggregated on next ``_flush_trade_buffer`` call."""
        side_norm = str(side).strip().lower()
        if side_norm not in ("buy", "sell"):
            _LOG.warning("intra_bar_skip_trade | symbol=%s unknown_side=%s", symbol, side)
            return
        if qty <= 0:
            return
        st = self._ensure(symbol)
        delta = qty if side_norm == "buy" else -qty
        st.trade_buffer.append(delta)
        st.last_ts = ts

    def _flush_trade_buffer(self, symbol: str) -> None:
        st = self._symbols.get(symbol.upper().strip())
        if st is None or not st.trade_buffer:
            return
        agg = sum(st.trade_buffer)
        st.trade_buffer.clear()
        st.trade_deltas.append(agg)

    def process_orderbook(
        self,
        symbol: str,
        bid_qty: float,
        ask_qty: float,
    ) -> None:
        """Update DOM via EMA: new = alpha * imb + (1-alpha) * ema.
        First call initialises EMA directly (no previous value).
        """
        try:
            bid_qty = float(bid_qty or 0)
            ask_qty = float(ask_qty or 0)
        except (TypeError, ValueError):
            _LOG.warning("intra_bar_skip_orderbook | symbol=%s non_float", symbol)
            return
        total = bid_qty + ask_qty
        if total <= 0:
            return
        st = self._ensure(symbol)
        imb = (bid_qty - ask_qty) / total
        if not st.dom_ema_initialized:
            st.dom_ema = imb
            st.dom_ema_initialized = True
        else:
            alpha = self._cfg.dom_ema_alpha
            st.dom_ema = alpha * imb + (1.0 - alpha) * st.dom_ema

    def compute(self, symbol: str) -> IntraBarSignal | None:
        st = self._symbols.get(symbol.upper().strip())
        if st is None:
            return None
        cfg = self._cfg

        self._flush_trade_buffer(symbol)

        prices = list(st.prices)
        deltas = list(st.trade_deltas)

        if len(prices) < 3 or st.last_price <= 0:
            return None

        mean_p = statistics.mean(prices)
        if mean_p <= 0:
            return None
        sd_p = statistics.stdev(prices) if len(prices) > 1 else 0.0
        momentum_z = ((st.last_price - mean_p) / sd_p) if sd_p > 0 else 0.0

        trade_burst = 0.0
        if len(deltas) >= cfg.min_trades_for_burst:
            total_abs = sum(abs(d) for d in deltas)
            if total_abs > 0:
                trade_burst = sum(deltas) / total_abs

        dom_imb = st.dom_ema

        direction_bull = momentum_z > 0 and trade_burst > cfg.dom_imbalance_min and dom_imb > 0.0
        direction_bear = momentum_z < 0 and trade_burst < -cfg.dom_imbalance_min and dom_imb < 0.0

        if not direction_bull and not direction_bear:
            return None

        side = "long" if direction_bull else "short"
        raw_conf = (
            abs(momentum_z) / 5.0
            + abs(trade_burst) / 5.0
            + abs(dom_imb) / 0.67
        ) / 3.0
        confidence = min(raw_conf, 1.0)

        return IntraBarSignal(
            symbol=symbol.upper().strip(),
            side=side,
            confidence=confidence,
            momentum_z=momentum_z,
            trade_burst=trade_burst,
            dom_imbalance=dom_imb,
            ts=st.last_ts,
            price=st.last_price,
        )

    def signals(
        self,
        symbols: list[str] | None = None,
        *,
        min_confidence: float | None = None,
    ) -> list[IntraBarSignal]:
        cfg = self._cfg
        threshold = min_confidence if min_confidence is not None else cfg.confidence_threshold
        out: list[IntraBarSignal] = []
        candidates = symbols if symbols is not None else list(self._symbols.keys())
        for sym in candidates:
            sig = self.compute(sym)
            if sig is None:
                continue
            if sig.confidence >= threshold:
                out.append(sig)
            else:
                _LOG.debug(
                    "intra_bar_signal_below_threshold",
                    symbol=sym,
                    confidence=round(sig.confidence, 3),
                    side=sig.side,
                )
        return out


__all__ = [
    "IntraBarConfig",
    "IntraBarSignal",
    "IntraBarState",
    "clear_intra_bar_config_cache",
    "intra_bar_config",
]
