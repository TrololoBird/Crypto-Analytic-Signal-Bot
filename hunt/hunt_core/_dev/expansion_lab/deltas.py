"""Delta layer — block-score trajectory over the configured lookback.

A static ``compression = 0.9`` is weaker evidence than ``compression rose 0.4 → 0.9``.
Deltas are normalized to roughly -1..1 (clamped) and an aggregate ``momentum`` captures
how many tracked blocks are accelerating at once.
"""
from __future__ import annotations

from hunt_core._dev.expansion_lab._util import clamp01
from hunt_core._dev.expansion_lab.config import ExpansionConfig
from hunt_core._dev.expansion_lab.history import ExpansionHistory
from hunt_core._dev.expansion_lab.types import BlockDeltas, BlockScores

_TRACKED = (
    "compression",
    "oi",
    "funding",
    "liquidity",
    "structure",
    "fuel_imbalance",
    "supply_exhaustion",
)

# Map delta field -> source block score field (oi/funding read derived blocks).
_SOURCE = {
    "compression": "compression",
    "oi": "fuel",
    "funding": "funding",
    "liquidity": "liquidity",
    "structure": "structure",
    "fuel_imbalance": "fuel_imbalance",
    "supply_exhaustion": "supply_exhaustion",
}


def compute_deltas(
    symbol: str,
    scores: BlockScores,
    history: ExpansionHistory,
    cfg: ExpansionConfig,
) -> BlockDeltas:
    past = history.past_scores(symbol, lookback=cfg.delta_lookback_bars)
    if not past:
        return BlockDeltas()

    cur = scores.to_dict()
    out: dict[str, float] = {}
    accel_sum = 0.0
    accel_n = 0
    for field_name in _TRACKED:
        src = _SOURCE[field_name]
        now = float(cur.get(src, 0.0))
        prev = float(past.get(src, 0.0))
        d = max(-1.0, min(1.0, now - prev))
        out[field_name] = round(d, 4)
        accel_sum += d
        accel_n += 1
    momentum = clamp01((accel_sum / accel_n + 1.0) / 2.0) if accel_n else 0.5
    out["momentum"] = round(momentum, 4)
    return BlockDeltas(**out)


__all__ = ["compute_deltas"]
