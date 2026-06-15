"""Setup catalog runner — all detectors with regime conflict suppression (C.1.2)."""
from __future__ import annotations



import math
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from hunt_core.domain.regime_classifier import Regime, RegimeResult, regime_conflicts_direction

Direction = Literal["short", "long"]

_SCORE_CAP = 0.95

# Regime → directional prior (logit offset).
_REGIME_PRIOR: dict[str, dict[str, float]] = {
    Regime.TREND_UP.value: {"long": 0.35, "short": -0.45},
    Regime.TREND_DOWN.value: {"long": -0.40, "short": 0.30},
    Regime.RANGE.value: {"long": 0.0, "short": 0.0},
    Regime.SQUEEZE.value: {"long": -0.15, "short": -0.05},
    Regime.EXPANSION.value: {"long": 0.10, "short": 0.10},
    Regime.CAPITULATION.value: {"long": 0.25, "short": -0.35},
    Regime.EUPHORIA.value: {"long": -0.40, "short": 0.20},
}

# Setup-specific calibration intercepts.
_SETUP_INTERCEPT: dict[str, float] = {
    "dump_initiation": -0.15,
    "squeeze_expansion": -0.10,
    "liquidity_sweep": -0.05,
    "bos_choch": -0.12,
    "value_accept_reject": -0.08,
    "oi_cascade": -0.18,
    "accumulation_breakout": -0.14,
}


@dataclass(frozen=True, slots=True)
class SetupEvidence:
    """Closed-bar confirmed setup candidate from the hunt setup catalog."""

    setup_id: str
    direction: Direction
    strength: float  # 0..1 detector-native strength
    confirmed: bool
    reasons: tuple[str, ...] = ()
    entry: float = 0.0
    stop_loss: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
        return v if v == v else default
    except (TypeError, ValueError):
        return default


def closed_tf(tf: dict[str, Any], key: str) -> dict[str, Any]:
    block = tf.get(key)
    return block if isinstance(block, dict) else {}


def closed_bar_close(tf_block: dict[str, Any]) -> float:
    if not tf_block.get("closed_bar"):
        return 0.0
    candle = tf_block.get("candle") or {}
    if isinstance(candle, dict) and candle.get("close") is not None:
        return _f(candle.get("close"))
    return _f(tf_block.get("close"))


def require_closed_bar(tf: dict[str, Any], key: str) -> tuple[dict[str, Any], float] | None:
    """Return (block, close) when the timeframe has a grace-closed bar."""
    block = closed_tf(tf, key)
    if not block.get("closed_bar"):
        return None
    close = closed_bar_close(block)
    if close <= 0:
        return None
    return block, close


def confirm_tf_chain(tf: dict[str, Any], *keys: str) -> tuple[dict[str, Any], float] | None:
    """Prefer first available closed bar from *keys* (confirm path order)."""
    for key in keys:
        hit = require_closed_bar(tf, key)
        if hit is not None:
            return hit
    return None


def atr_from_tf(tf: dict[str, Any], key: str = "15m_closed") -> float:
    block = closed_tf(tf, key) or closed_tf(tf, key.replace("_closed", ""))
    return _f(block.get("atr14"))


def _sigmoid(z: float) -> float:
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


def _regime_label(regime: RegimeResult | dict[str, Any] | str | None) -> str:
    if regime is None:
        return Regime.RANGE.value
    if isinstance(regime, RegimeResult):
        return regime.regime.value
    if isinstance(regime, dict):
        return str(
            regime.get("regime")
            or regime.get("label")
            or regime.get("market_regime")
            or Regime.RANGE.value
        )
    return str(regime)


def _lake_adjustment(lake_stats: dict[str, Any] | None, *, setup_id: str, direction: str) -> float:
    if not lake_stats:
        return 0.0
    sym_stats = lake_stats.get("by_setup") or lake_stats.get("setups") or {}
    row = sym_stats.get(setup_id) or sym_stats.get(f"{setup_id}:{direction}") or {}
    if not isinstance(row, dict):
        return 0.0
    n = int(row.get("n") or row.get("n_closed") or 0)
    if n < 8:
        return 0.0
    wr = float(row.get("wr_pct") or row.get("win_rate") or 50.0) / 100.0
    sl_rate = float(row.get("sl_rate") or row.get("sl_hit_rate") or 0.5)
    return (wr - 0.5) * 0.8 - max(0.0, sl_rate - 0.30) * 0.6


def score_setup_probability(
    evidence: SetupEvidence,
    regime: RegimeResult | dict[str, Any] | str | None,
    lake_stats: dict[str, Any] | None = None,
) -> float:
    """Return calibrated probability in [0, 0.95] for a closed-bar setup."""
    label = _regime_label(regime)
    direction = evidence.direction
    intercept = _SETUP_INTERCEPT.get(evidence.setup_id, -0.10)
    prior = (_REGIME_PRIOR.get(label) or {}).get(direction, 0.0)
    regime_conf = 0.0
    if isinstance(regime, RegimeResult):
        regime_conf = (regime.confidence - 0.5) * 0.6

    strength_logit = (evidence.strength - 0.5) * 3.2
    reason_bonus = min(0.25, len(evidence.reasons) * 0.04)
    lake_adj = _lake_adjustment(lake_stats, setup_id=evidence.setup_id, direction=direction)

    z = intercept + prior + strength_logit + reason_bonus + regime_conf + lake_adj
    if not evidence.confirmed:
        z -= 1.2

    p = _sigmoid(z)
    return round(min(_SCORE_CAP, max(0.0, p)), 4)


HUNT_SETUP_IDS: tuple[str, ...] = (
    "dump_initiation",
    "squeeze_expansion",
    "liquidity_sweep",
    "bos_choch",
    "value_accept_reject",
    "oi_cascade",
    "accumulation_breakout",
)


@dataclass(frozen=True, slots=True)
class HuntSetupMeta:
    setup_id: str
    trigger_tf: str
    pattern_tf: str
    order_type: str = "limit"
    ttl_minutes: int = 120


HUNT_SETUP_META: dict[str, HuntSetupMeta] = {
    "dump_initiation": HuntSetupMeta("dump_initiation", "5m", "15m", "limit", 90),
    "squeeze_expansion": HuntSetupMeta("squeeze_expansion", "15m", "15m", "limit", 120),
    "liquidity_sweep": HuntSetupMeta("liquidity_sweep", "15m", "15m", "limit", 120),
    "bos_choch": HuntSetupMeta("bos_choch", "15m", "15m", "limit", 120),
    "value_accept_reject": HuntSetupMeta("value_accept_reject", "15m", "15m", "limit", 120),
    "oi_cascade": HuntSetupMeta("oi_cascade", "5m", "15m", "limit", 90),
    "accumulation_breakout": HuntSetupMeta("accumulation_breakout", "15m", "1h", "limit", 180),
}


def resolve_setup_order_type(setup_id: str, *, default: str = "limit") -> str:
    meta = HUNT_SETUP_META.get(str(setup_id or "").strip())
    return meta.order_type if meta else default


def resolve_setup_ttl_minutes(setup_id: str, *, default: int = 120) -> int:
    meta = HUNT_SETUP_META.get(str(setup_id or "").strip())
    return meta.ttl_minutes if meta else default


def resolve_setup_trigger_tf(setup_id: str, *, default: str = "15m") -> str:
    meta = HUNT_SETUP_META.get(str(setup_id or "").strip())
    return meta.trigger_tf if meta else default


def resolve_setup_pattern_tf(setup_id: str, *, default: str = "15m") -> str:
    meta = HUNT_SETUP_META.get(str(setup_id or "").strip())
    return meta.pattern_tf if meta else default


DetectorFn = Callable[[dict[str, Any], dict[str, Any]], SetupEvidence | None]


def _catalog_detectors() -> tuple[DetectorFn, ...]:
    from hunt_core.scan.detectors import (
        detect_accumulation_breakout,
        detect_bos_choch,
        detect_dump_initiation,
        detect_liquidity_sweep,
        detect_oi_cascade,
        detect_squeeze_expansion,
        detect_value_accept_reject,
    )

    return (
        detect_dump_initiation,
        detect_squeeze_expansion,
        detect_liquidity_sweep,
        detect_bos_choch,
        detect_value_accept_reject,
        detect_oi_cascade,
        detect_accumulation_breakout,
    )


def resolve_catalog_regime(
    row: dict[str, Any],
    prepared: dict[str, Any] | None = None,
) -> str:
    """Structural regime for C.1.2 suppression — lifecycle.regime, not market snapshot."""
    prep = prepared if isinstance(prepared, dict) else row
    lc = row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else {}
    if not lc and isinstance(prep.get("lifecycle"), dict):
        lc = prep["lifecycle"]
    regime = lc.get("regime")
    if regime:
        return str(regime)
    market = row.get("regime") if isinstance(row.get("regime"), dict) else {}
    return _regime_label(market)


def run_setup_catalog(
    row: dict[str, Any],
    prepared: dict[str, Any],
    regime: RegimeResult | dict[str, Any] | str | None = None,
    *,
    include_forming: bool = False,
) -> list[SetupEvidence]:
    """Run all setup detectors; suppress counter-context hits per regime (C.1.2)."""
    label = _regime_label(regime) if regime is not None else resolve_catalog_regime(row, prepared)
    hits: list[SetupEvidence] = []
    for detect in _catalog_detectors():
        evidence = detect(row, prepared)
        if evidence is None:
            continue
        if not evidence.confirmed:
            if not include_forming or evidence.strength < 0.40:
                continue
        if regime_conflicts_direction(label, evidence.direction):
            continue
        prob = score_setup_probability(evidence, regime=label)
        hits.append(
            SetupEvidence(
                setup_id=evidence.setup_id,
                direction=evidence.direction,
                strength=evidence.strength,
                confirmed=evidence.confirmed,
                reasons=evidence.reasons,
                entry=evidence.entry,
                stop_loss=evidence.stop_loss,
                tp1=evidence.tp1,
                tp2=evidence.tp2,
                metadata={**evidence.metadata, "probability": round(prob, 4)},
            )
        )
    return hits


__all__ = [
    "HUNT_SETUP_IDS",
    "HUNT_SETUP_META",
    "HuntSetupMeta",
    "SetupEvidence",
    "atr_from_tf",
    "closed_bar_close",
    "closed_tf",
    "confirm_tf_chain",
    "require_closed_bar",
    "resolve_catalog_regime",
    "resolve_setup_order_type",
    "resolve_setup_pattern_tf",
    "resolve_setup_trigger_tf",
    "resolve_setup_ttl_minutes",
    "run_setup_catalog",
    "score_setup_probability",
]
