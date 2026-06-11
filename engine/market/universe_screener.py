"""Light screener (Crypto-Signal-style flags) on radar state - WATCH/radar tier only."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .radar_state import MarketRadarStore, SymbolRadarState, SymbolTier

if TYPE_CHECKING:
    from ..domain.config import UniverseRadarConfig


@dataclass(frozen=True, slots=True)
class ScreenerHit:
    flags: tuple[str, ...]
    prescore_boost: float
    promotion_reasons: tuple[str, ...]
    suggested_tier: SymbolTier


def _rsi_proxy_from_change(change_24h_pct: float) -> float:
    """Map 24h change to 0-100 RSI-like proxy (not Wilder RSI - radar tier only)."""
    clamped = max(-15.0, min(15.0, change_24h_pct))
    return 50.0 + (clamped / 15.0) * 35.0


def screen_symbol(
    state: SymbolRadarState,
    *,
    config: UniverseRadarConfig,
    store: MarketRadarStore,
    now: float,
) -> ScreenerHit:
    flags: list[str] = []
    reasons: list[str] = []
    boost = 0.0
    suggested = SymbolTier.COLD

    change_5m = state.change_pct_over(300.0, now=now)
    vol_z = store.volume_zscore(state.symbol)

    change_24h = float(state.price_change_pct_24h)
    if abs(change_24h) >= config.change_24h_hot_pct:
        flags.append("change_24h_hot")
        reasons.append(f"change_24h={change_24h:.2f}%")
        boost = max(boost, config.prescore_boost_warm)
        suggested = SymbolTier.WARM

    if abs(change_24h) >= 15.0:
        flags.append("pump_extreme")
        reasons.append(f"pump_extreme={change_24h:.1f}%")
        boost = max(boost, config.prescore_boost_hot)
        suggested = SymbolTier.HOT

    if abs(change_24h) >= 8.0:
        flags.append("range_expansion")
        boost = max(boost, config.prescore_boost_warm * 0.7)

    if change_5m is not None and abs(change_5m) >= config.impulse_5m_pct:
        direction = "up" if change_5m > 0 else "down"
        flags.append(f"impulse_5m_{direction}")
        reasons.append(f"impulse_5m={change_5m:.2f}%")
        boost = max(boost, config.prescore_boost_hot)
        suggested = SymbolTier.HOT
        if change_5m > 0 and change_24h >= 10.0:
            flags.append("pos_near_high_proxy")
        elif change_5m < 0 and change_24h <= -10.0:
            flags.append("pos_near_low_proxy")

    if vol_z >= config.vol_spike_zscore:
        flags.append("vol_spike")
        reasons.append(f"vol_z={vol_z:.2f}")
        boost = max(boost, config.prescore_boost_warm)
        if suggested == SymbolTier.COLD:
            suggested = SymbolTier.WARM

    funding = state.funding_rate
    if funding is not None and abs(funding) >= config.funding_extreme_pct:
        flags.append("funding_extreme")
        reasons.append(f"funding={funding:.4f}")
        boost = max(boost, config.prescore_boost_warm * 0.8)

    rsi_proxy = _rsi_proxy_from_change(state.price_change_pct_24h)
    if rsi_proxy >= config.light_rsi_overbought:
        flags.append("rsi_proxy_overbought")
    elif rsi_proxy <= config.light_rsi_oversold:
        flags.append("rsi_proxy_oversold")
        boost = max(boost, config.prescore_boost_warm * 0.6)

    if not flags:
        return ScreenerHit((), 0.0, (), SymbolTier.COLD)

    if suggested == SymbolTier.HOT:
        boost = max(boost, config.prescore_boost_hot)
    elif suggested == SymbolTier.WARM:
        boost = max(boost, config.prescore_boost_warm)

    return ScreenerHit(
        tuple(flags),
        round(min(boost, 0.35), 4),
        tuple(reasons),
        suggested,
    )


def apply_screener_to_store(store: MarketRadarStore, *, now: float) -> int:
    """Update flags/boost on all symbols; return count with active flags."""
    cfg = store._cfg
    if not cfg.enabled:
        return 0
    hits = 0
    for state in store._states.values():
        hit = screen_symbol(state, config=cfg, store=store, now=now)
        state.flags = hit.flags
        state.prescore_boost = hit.prescore_boost
        state.promotion_reasons = hit.promotion_reasons
        if hit.flags:
            hits += 1
            if state.tier == SymbolTier.COLD and hit.suggested_tier != SymbolTier.COLD:
                state.tier = hit.suggested_tier
    return hits
