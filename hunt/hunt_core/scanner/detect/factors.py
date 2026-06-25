"""Fusion factors — each a distribution-relative, sign-explicit pre-move pressure.

Eleven production factors (see ``factor_registry.json``) read feature columns through the
trailing :class:`FeatureWindow` and emit a :class:`FactorScore` whose magnitude comes *only*
from ``calibrate`` (robust z / percentile vs the symbol's own recent window). There are no
decision thresholds in this module — a factor either produces a distribution-relative score
or **abstains** (``active=False``) when its inputs are missing (thin lake), stale, or below
the cold-start sample floor.

Two factor kinds:

- ``directional`` — sign carries the side: ``score > 0`` ⇒ pre-pump (long) pressure,
  ``score < 0`` ⇒ pre-dump (short) pressure. Magnitude is in robust-z units.
- ``amplifier`` — unsigned magnitude (``>= 0``, robust-z units) representing conviction
  behind whichever side the directional factors choose (OI behind the move, volatility
  coil). Fusion uses these to scale confidence, never to pick the side.

Sign conventions are domain facts, not tunables:
``depth_imbalance = (bid-ask)/(bid+ask)`` and ``microprice_bias`` are positive-bullish
(see ``features/microstructure.py``); funding is contrarian (crowded longs → short
pressure); price/RSI/BB stretch mean-reverts.
"""
from __future__ import annotations

from typing import Any

from dataclasses import dataclass, field

from hunt_core.scanner.detect import calibrate as C
from hunt_core.scanner.detect.windows import FeatureWindow
from hunt_core.scanner.gate._wash import fusion_window_wash_abstain

DIRECTIONAL = "directional"
AMPLIFIER = "amplifier"


@dataclass(frozen=True)
class FactorScore:
    """One factor's reading at the current bar."""

    name: str
    kind: str  # DIRECTIONAL | AMPLIFIER
    score: float  # directional: signed robust-z; amplifier: magnitude >= 0
    active: bool
    detail: str = ""
    parts: dict[str, float] = field(default_factory=dict)


def _abstain(name: str, kind: str, why: str) -> FactorScore:
    return FactorScore(name=name, kind=kind, score=0.0, active=False, detail=why)


def _mean_active(values: dict[str, float | None]) -> tuple[float | None, dict[str, float]]:
    """Mean of the non-None components, plus the surviving parts for diagnostics."""
    parts = {k: v for k, v in values.items() if v is not None}
    if not parts:
        return None, {}
    return sum(parts.values()) / len(parts), parts


# --- Directional factors -----------------------------------------------------

def factor_book(window: FeatureWindow) -> FactorScore:
    """Order-book imbalance: bid-heavy book / microprice above mid ⇒ long pressure."""
    z_di = C.robust_z(window.col("depth_imbalance"))
    z_mp = C.robust_z(window.col("microprice_bias"))
    score, parts = _mean_active({"depth_imbalance": z_di, "microprice_bias": z_mp})
    if score is None:
        return _abstain("book", DIRECTIONAL, "book_inputs_missing")
    return FactorScore("book", DIRECTIONAL, score, True, f"book_z={score:+.2f}", parts)


def factor_structure(window: FeatureWindow) -> FactorScore:
    """Price/RSI/BB stretch — trend-following: stretch ⇒ strength in stretch direction.

    Each stretch input is robust-z'd vs the symbol's own window, so "overbought" means
    *extended relative to this symbol's recent behaviour*, not a fixed RSI level.
    Was previously mean-reverting (score = -stretch) — adversarial for impulse detection.
    """
    z_rsi = C.robust_z(window.col("rsi14"))
    z_bb = C.robust_z(window.col("bb_pct_b"))
    z_pos = C.robust_z(window.col("zscore30"))
    stretch, parts = _mean_active({"rsi14": z_rsi, "bb_pct_b": z_bb, "zscore30": z_pos})
    if stretch is None:
        return _abstain("structure", DIRECTIONAL, "structure_inputs_missing")
    score = stretch  # trend-following: positive stretch ⇒ long pressure
    return FactorScore("structure", DIRECTIONAL, score, True, f"structure_z={score:+.2f}", parts)


def factor_funding(window: FeatureWindow) -> FactorScore:
    """Funding crowding (contrarian): unusually high funding ⇒ crowded longs ⇒ short."""
    from hunt_core.scanner.detect.config import fusion_params

    fp = fusion_params()
    z = C.robust_z(window.col("funding_rate"), min_n=fp.funding_min_n)
    if z is None:
        return _abstain("funding", DIRECTIONAL, "funding_unavailable")
    score = -z
    return FactorScore("funding", DIRECTIONAL, score, True, f"funding_z={score:+.2f}", {"funding_rate": z})


def factor_flow(window: FeatureWindow) -> FactorScore:
    """Order-flow / CVD: net buy pressure ⇒ long, net sell pressure ⇒ short.

    Uses live-only order-flow columns (CVD slope, taker buy-share); abstains on the
    thin parquet lake where these are absent (so replay runs on the other factors).
    """
    cvd = window.col("rolling_cvd_24h")
    if cvd is None:
        cvd = window.col("session_cvd")
    z_cvd = C.ols_slope(cvd) if cvd is not None else None
    # taker buy-share / delta_ratio is centred at 0.5: >0.5 ⇒ buy pressure.
    z_taker = C.robust_z(window.col("delta_ratio"))
    score, parts = _mean_active({"cvd_slope": z_cvd, "delta_ratio": z_taker})
    if score is None:
        return _abstain("flow", DIRECTIONAL, "flow_inputs_missing")
    return FactorScore("flow", DIRECTIONAL, score, True, f"flow_z={score:+.2f}", parts)


# --- Amplifier factors -------------------------------------------------------

def factor_oi_pressure(window: FeatureWindow) -> FactorScore:
    """Open-interest velocity magnitude — real positioning conviction behind the move.

    Unsigned: a fast OI build *or* flush both mean conviction; the directional factors
    decide the side. Magnitude is the robust-z of |OI change| vs the symbol's window.
    """
    z_chg = C.robust_z(window.col("oi_change_pct"))
    z_slope = C.robust_z(window.col("oi_slope_5m"))
    mags = [abs(v) for v in (z_chg, z_slope) if v is not None]
    if not mags:
        return _abstain("oi_pressure", AMPLIFIER, "oi_unavailable")
    score = max(mags)
    return FactorScore("oi_pressure", AMPLIFIER, score, True, f"oi_amp={score:.2f}")


def factor_compression(window: FeatureWindow) -> FactorScore:
    """Volatility coil — compression *level* and *velocity* both store energy.

    Level: ``max(0, -robust_z(bb_width))`` — band width unusually low vs the symbol's
    own history. Velocity: ``max(0, -ols_slope(bb_width))`` — band sharply contracting
    right now. A long-low band and a fast-collapsing band both coil; the factor takes
    whichever is stronger so a sharp squeeze is not flattened to the same percentile as a
    slow one (distribution-relative, no fixed threshold).
    """
    z_bb = C.robust_z(window.col("bb_width"))
    z_atr = C.robust_z(window.col("atr_pct"))
    comps = [v for v in (z_bb, z_atr) if v is not None]
    if not comps:
        return _abstain("compression", AMPLIFIER, "compression_inputs_missing")
    level_coil = max(0.0, -(sum(comps) / len(comps)))
    slope = C.ols_slope(window.col("bb_width"))
    vel_coil = max(0.0, -slope) if slope is not None else 0.0
    coil = max(level_coil, vel_coil)
    return FactorScore(
        "compression", AMPLIFIER, coil, True, f"coil={coil:.2f}(lvl={level_coil:.2f},vel={vel_coil:.2f})"
    )


_FACTORS = (
    factor_book,
    factor_structure,
    factor_funding,
    factor_flow,
    factor_oi_pressure,
    factor_compression,
)

_ROW_CONTEXT_FACTORS = frozenset({"market_maker_trap", "whale_activity"})


def _factor_registry() -> dict[str, Any]:
    from hunt_core.scanner.detect import factors_quarantine as Fq

    return {
        "book": factor_book,
        "structure": factor_structure,
        "funding": factor_funding,
        "flow": factor_flow,
        "oi_pressure": factor_oi_pressure,
        "compression": factor_compression,
        "oi_acceleration": Fq.factor_oi_acceleration,
        "funding_velocity": Fq.factor_funding_velocity,
        "poc_migration": Fq.factor_poc_migration,
        "va_contraction": Fq.factor_va_contraction,
        "liquidity_void_path": Fq.factor_liquidity_void_path,
        "market_maker_trap": Fq.factor_market_maker_trap,
        "whale_activity": Fq.factor_whale_activity,
    }


def _factor_kind(name: str) -> str:
    try:
        reg = __import__(
            "hunt_core.scanner.detect.factor_registry_loader",
            fromlist=["load_factor_registry"],
        ).load_factor_registry()
        meta = (reg.get("factors") or {}).get(name) or {}
        return str(meta.get("kind") or DIRECTIONAL)
    except Exception:
        return DIRECTIONAL


def _wash_abstain_all(names: frozenset[str], why: str) -> list[FactorScore]:
    out: list[FactorScore] = []
    for name in sorted(names):
        kind = _factor_kind(name)
        out.append(_abstain(name, kind, why))
    return out


def compute_factors(
    window: FeatureWindow,
    *,
    row: dict[str, Any] | None = None,
) -> list[FactorScore]:
    """Production factor readings for the current bar (registry status=production only)."""
    from hunt_core.scanner.detect.factor_registry_loader import production_factors

    allowed = production_factors()
    wash = fusion_window_wash_abstain(window)
    if wash:
        return _wash_abstain_all(allowed, wash)
    registry = _factor_registry()
    scores: list[FactorScore] = []
    for name in sorted(allowed):
        fn = registry.get(name)
        if fn is None:
            scores.append(_abstain(name, _factor_kind(name), "unknown_factor"))
            continue
        if name in _ROW_CONTEXT_FACTORS:
            scores.append(fn(window, row=row))
        else:
            scores.append(fn(window))
    return scores


__all__ = [
    "AMPLIFIER",
    "DIRECTIONAL",
    "FactorScore",
    "compute_factors",
    "factor_book",
    "factor_compression",
    "factor_flow",
    "factor_funding",
    "factor_oi_pressure",
    "factor_structure",
]
