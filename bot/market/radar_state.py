"""Market radar: aggregate !ticker@arr firehose into per-symbol tiers (cold→warm→hot→deep)."""

from __future__ import annotations

import contextlib
import math
import time
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..domain.config import UniverseRadarConfig

JsonDict = dict[str, Any]


class SymbolTier(StrEnum):
    COLD = "cold"
    WARM = "warm"
    HOT = "hot"
    DEEP = "deep"


@dataclass(slots=True)
class PriceSample:
    ts: float
    price: float


@dataclass(slots=True)
class SymbolRadarState:
    symbol: str
    tier: SymbolTier = SymbolTier.COLD
    last_price: float = 0.0
    quote_volume: float = 0.0
    price_change_pct_24h: float = 0.0
    funding_rate: float | None = None
    spread_bps: float | None = None
    prescore_boost: float = 0.0
    flags: tuple[str, ...] = ()
    promotion_reasons: tuple[str, ...] = ()
    last_update_ts: float = 0.0
    promoted_at: float = 0.0
    last_trigger_ts: float = 0.0
    _samples: deque[PriceSample] = field(default_factory=lambda: deque(maxlen=180))

    def ingest_price(self, price: float, *, now: float) -> None:
        if price > 0.0 and math.isfinite(price):
            self.last_price = price
            self._samples.append(PriceSample(ts=now, price=price))
            self.last_update_ts = now

    def change_pct_over(self, window_seconds: float, *, now: float) -> float | None:
        if self.last_price <= 0.0 or not self._samples:
            return None
        cutoff = now - window_seconds
        anchor: PriceSample | None = None
        for sample in self._samples:
            if sample.ts <= cutoff:
                anchor = sample
            else:
                break
        if anchor is None or anchor.price <= 0.0:
            return None
        return ((self.last_price - anchor.price) / anchor.price) * 100.0


class MarketRadarStore:
    """In-memory radar for all liquid symbols seen on global ticker streams."""

    def __init__(self, config: UniverseRadarConfig, *, quote_asset: str = "USDT") -> None:
        self._cfg = config
        self._quote = str(quote_asset or "USDT").strip().upper()
        self._states: dict[str, SymbolRadarState] = {}
        self._volume_stats: dict[str, float] = {}
        self._last_tier_cycle_ts: float = 0.0

    @property
    def symbol_count(self) -> int:
        return len(self._states)

    def get(self, symbol: str) -> SymbolRadarState | None:
        return self._states.get(str(symbol or "").strip().upper())

    def ingest_ticker(self, row: JsonDict, *, now: float | None = None) -> SymbolRadarState | None:
        if not self._cfg.enabled:
            return None
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol.endswith(self._quote):
            return None
        try:
            volume = float(row.get("quote_volume") or 0.0)
            price = float(row.get("last_price") or row.get("c") or 0.0)
            change_pct = float(
                row.get("price_change_percent") or row.get("price_change_pct") or 0.0
            )
        except (TypeError, ValueError):
            return None
        if volume < self._cfg.min_quote_volume_usd or price <= 0.0:
            return None

        ts = float(now if now is not None else time.monotonic())
        state = self._states.get(symbol)
        if state is None:
            state = SymbolRadarState(symbol=symbol)
            self._states[symbol] = state
        state.quote_volume = volume
        state.price_change_pct_24h = change_pct
        state.ingest_price(price, now=ts)
        funding = row.get("funding_rate")
        if funding is not None:
            with contextlib.suppress(TypeError, ValueError):
                state.funding_rate = float(funding)
        spread = row.get("spread_bps")
        if spread is not None:
            with contextlib.suppress(TypeError, ValueError):
                state.spread_bps = float(spread)
        self._volume_stats[symbol] = math.log10(max(volume, 1.0))
        return state

    def ingest_batch(self, rows: list[JsonDict], *, now: float | None = None) -> int:
        ts = float(now if now is not None else time.monotonic())
        count = 0
        for row in rows:
            if self.ingest_ticker(row, now=ts) is not None:
                count += 1
        return count

    def volume_zscore(self, symbol: str) -> float:
        log_vol = self._volume_stats.get(symbol)
        if log_vol is None or len(self._volume_stats) < 8:
            return 0.0
        values = list(self._volume_stats.values())
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        std = math.sqrt(variance) if variance > 0.0 else 1.0
        return (log_vol - mean) / std if std > 0.0 else 0.0

    def iter_states(self) -> list[SymbolRadarState]:
        """Snapshot of per-symbol radar state (for diagnostics / watch funnel)."""
        return list(self._states.values())

    def symbols_by_tier(self, tier: SymbolTier) -> list[str]:
        return sorted(symbol for symbol, state in self._states.items() if state.tier == tier)

    def snapshot_summary(self) -> dict[str, int]:
        counts = {tier.value: 0 for tier in SymbolTier}
        for state in self._states.values():
            counts[state.tier.value] = counts.get(state.tier.value, 0) + 1
        return counts
