"""Intra-bar PRE-pump/PRE-dump state — momentum z-score, trade burst, DOM imbalance.

Rolling per-symbol windows updated from WS callbacks. Exposes IntraBarSignal when
all three sub-signals agree above configurable thresholds.

Silence is default: 95%+ of ticks produce no signal. All signals are logged even
below confidence_threshold for calibration.
"""
from __future__ import annotations

import logging
import statistics
from collections import deque
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
    min_trades_for_burst: int = 5
    cooldown_seconds: int = 300


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
    side: str  # "long" | "short"
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
        "dom_imbalance",
        "last_price",
        "last_ts",
    )

    def __init__(self, momentum_window: int, trade_window: int) -> None:
        self.prices: deque[float] = deque(maxlen=momentum_window)
        # Each element is net delta of buy_vol - sell_vol for a snapshot
        self.trade_deltas: deque[float] = deque(maxlen=trade_window)
        self.dom_imbalance: float = 0.0
        self.last_price: float = 0.0
        self.last_ts: float = 0.0


# ---------------------------------------------------------------------------
# IntraBarState service
# ---------------------------------------------------------------------------


class IntraBarState:
    """Per-symbol rolling intra-bar state. NOT a singleton — instantiate once in runtime.

    Feed WS events into the three ``process_*`` methods, then poll ``signals()``.

    Thread-safe for asyncio (single event loop). No persistence.
    """

    def __init__(
        self,
        cfg: IntraBarConfig | None = None,
        initial_symbols: list[str] | None = None,
    ) -> None:
        self._cfg = cfg if cfg is not None else intra_bar_config()
        self._symbols: dict[str, _SymbolState] = {}
        if initial_symbols:
            for sym in initial_symbols:
                self._ensure(sym)

    def _ensure(self, symbol: str) -> _SymbolState:
        sym = symbol.upper().strip()
        if sym not in self._symbols:
            self._symbols[sym] = _SymbolState(
                self._cfg.momentum_window,
                self._cfg.trade_window,
            )
        return self._symbols[sym]

    def process_m1_close(self, symbol: str, price: float, ts: float) -> None:
        """Feed a 1m kline close price (or any tick-level price update)."""
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
        """Feed a raw trade event. ``side`` must be ``"buy"`` or ``"sell"``.
        Tracks net buy-volume delta (buy_vol - sell_vol) per trade event.
        """
        st = self._ensure(symbol)
        delta = qty if side == "buy" else -qty
        st.trade_deltas.append(delta)
        st.last_ts = ts

    def process_orderbook(
        self,
        symbol: str,
        bid_qty: float,
        ask_qty: float,
    ) -> None:
        """Feed a top-of-book depth snapshot.
        imbalance = (bid_qty - ask_qty) / (bid_qty + ask_qty).
        Positive = bid heavier (bullish), negative = ask heavier (bearish).
        """
        total = bid_qty + ask_qty
        if total <= 0:
            return
        st = self._ensure(symbol)
        st.dom_imbalance = (bid_qty - ask_qty) / total

    def compute(self, symbol: str) -> IntraBarSignal | None:
        """Run the three sub-signals for one symbol. Returns None if any window is cold."""
        st = self._symbols.get(symbol.upper().strip())
        if st is None:
            return None
        cfg = self._cfg
        prices = list(st.prices)
        deltas = list(st.trade_deltas)

        if len(prices) < 3 or st.last_price <= 0:
            return None

        # -- momentum z-score (standardised log-return from window mean) --
        mean_p = statistics.mean(prices)
        if mean_p <= 0:
            return None
        sd_p = statistics.stdev(prices) if len(prices) > 1 else 0.0
        momentum_z = ((st.last_price - mean_p) / sd_p) if sd_p > 0 else 0.0

        # -- trade burst (net buy-volume delta ratio, robust to uniform windows) --
        trade_burst = 0.0
        if len(deltas) >= cfg.min_trades_for_burst:
            total_abs = sum(abs(d) for d in deltas)
            if total_abs > 0:
                trade_burst = sum(deltas) / total_abs  # -1..1

        # -- DOM imbalance (positive = bid heavier = bullish) --
        dom_imb = st.dom_imbalance

        # -- consensus logic: all three must agree on direction --
        bull = momentum_z > 0 and trade_burst > cfg.dom_imbalance_min and dom_imb > 0.0
        bear = momentum_z < 0 and trade_burst < -cfg.dom_imbalance_min and dom_imb < 0.0

        if not bull and not bear:
            return None

        side = "long" if bull else "short"
        # direction-agnostic confidence: average of absolute signal strengths, scaled 0-1
        raw_conf = (
            abs(momentum_z) / 5.0  # z=5 → 1.0
            + abs(trade_burst) / 5.0
            + abs(dom_imb) / 0.67  # imbalance 0.67 → 1.0 (bid 5× ask = 0.67)
        ) / 3.0
        confidence = min(raw_conf, 1.0)

        signal = IntraBarSignal(
            symbol=symbol.upper().strip(),
            side=side,
            confidence=confidence,
            momentum_z=momentum_z,
            trade_burst=trade_burst,
            dom_imbalance=dom_imb,
            ts=st.last_ts,
            price=st.last_price,
        )
        return signal

    def signals(
        self,
        symbols: list[str] | None = None,
        *,
        min_confidence: float | None = None,
    ) -> list[IntraBarSignal]:
        """Compute signals for all tracked symbols (or a subset) and return those
        above the confidence threshold. All signals are logged regardless."""
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
