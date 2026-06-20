"""ATR-scaled forecast scenarios — a statistical projection, not a prediction.

Each horizon is projected as a random-walk envelope: the per-bar ATR scales by √h to a
horizon band ``price ± σ_h``, and the fused directional lean (``tanh(z_dir)`` ∈ [-1, 1])
shifts a base path within that band. Fully deterministic and self-scaling — no fixed
price targets. The probability attached is the fused confidence on the lean side.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from hunt_core.detect.deep.panel import DeepPanel
from hunt_core.detect.windows import FeatureWindow

# (bars, label) — 15m bars to a human horizon.
_HORIZONS = ((4, "1h"), (16, "4h"), (32, "8h"))


@dataclass(frozen=True)
class Scenario:
    label: str
    horizon_bars: int
    base: float  # lean-shifted central path
    high: float  # +1σ envelope
    low: float  # -1σ envelope
    drift_pct: float  # base move vs current price
    prob: float  # fused confidence on the lean side


def _atr_abs(window: FeatureWindow, price: float) -> float | None:
    atr = window.last("atr14")
    if atr is not None and atr > 0:
        return atr
    atr_pct = window.last("atr_pct")
    if atr_pct is not None and atr_pct > 0:
        return atr_pct / 100.0 * price
    return None


def forecast_scenarios(window: FeatureWindow, panel: DeepPanel) -> list[Scenario]:
    """Random-walk ATR envelopes with a lean-shifted base path per horizon."""
    price = panel.price
    if price is None or price <= 0:
        return []
    atr = _atr_abs(window, price)
    if atr is None:
        return []
    lean = math.tanh(panel.fusion.z_dir)  # [-1, 1]
    out: list[Scenario] = []
    for bars, label in _HORIZONS:
        sigma_h = atr * math.sqrt(bars)
        base = price + lean * sigma_h
        out.append(
            Scenario(
                label=label,
                horizon_bars=bars,
                base=base,
                high=price + sigma_h,
                low=price - sigma_h,
                drift_pct=(base / price - 1.0) * 100.0,
                prob=panel.confidence,
            )
        )
    return out


__all__ = ["Scenario", "forecast_scenarios"]
