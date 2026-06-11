"""Multi-timeframe policy — single source for confirm vetoes (engine + gates).

Hierarchy (research-backed): 1H bias → 15m structure → 5m entry. Closed bars only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hunt_watch.regime_ensemble import EnsembleRegime, classify as classify_ensemble
from hunt_watch.param_store import basis_thresholds

# Meme perp ADX thresholds (Panoptic 2026 / plan consensus)
ADX_MEME_TREND_MIN = 30.0
ADX_MEME_RANGE_MAX = 15.0
DI_DOMINANCE = 1.15

# Funding extremes (8h rate, decimal)
FUNDING_SHORT_CONFIRM_MIN = 0.001  # +0.1%/8h overcrowded longs
FUNDING_SQUEEZE_MAX = -0.001  # -0.1%/8h short squeeze risk
# Smoothed basis (ap − index) / index — report Q02/A.8; gate not raw mark−index.
BASIS_AP_OVERHEAT_BPS = 120.0
BASIS_AP_UNDERHEAT_BPS = -120.0


@dataclass(frozen=True, slots=True)
class MtfFacts:
    trend_1h: str  # bull | bear | neutral
    adx_regime: str  # trending | ranging | neutral
    adx_1h: float
    closed_5m_available: bool
    closed_15m_available: bool
    funding_rate: float | None
    funding_extreme_long: bool
    funding_squeeze_short: bool
    ensemble: EnsembleRegime
    basis_ap_bps: float | None
    mark_ap_spread_bps: float | None  # diagnostic: (mark − ap) / ap


def _frame(tf: dict[str, Any], key: str) -> dict[str, Any]:
    row = tf.get(key)
    return row if isinstance(row, dict) else {}


def _trend_1h(tf: dict[str, Any]) -> str:
    r1h = _frame(tf, "1h_closed") or _frame(tf, "1h")
    plus = float(r1h.get("plus_di") or 0.0)
    minus = float(r1h.get("minus_di") or 0.0)
    if plus > 0 and minus > 0:
        if plus > minus * DI_DOMINANCE:
            return "bull"
        if minus > plus * DI_DOMINANCE:
            return "bear"
    ema9 = float(r1h.get("ema9") or 0.0)
    ema21 = float(r1h.get("ema21") or 0.0)
    if ema9 > 0 and ema21 > 0:
        if ema9 > ema21 * 1.002:
            return "bull"
        if ema9 < ema21 * 0.998:
            return "bear"
    return "neutral"


def _adx_regime(adx: float) -> str:
    if adx >= ADX_MEME_TREND_MIN:
        return "trending"
    if adx > 0 and adx < ADX_MEME_RANGE_MAX:
        return "ranging"
    return "neutral"


def snapshot(
    tf: dict[str, Any],
    *,
    market: dict[str, Any] | None = None,
) -> MtfFacts:
    r1h = _frame(tf, "1h_closed") or _frame(tf, "1h")
    adx = float(r1h.get("adx14") or 0.0)
    mkt = market or {}
    funding = mkt.get("funding_live")
    if funding is None:
        funding = mkt.get("funding_rate")
    fr = float(funding) if funding is not None else None
    trend = _trend_1h(tf)
    map_spread = mkt.get("mark_ap_spread_bps")
    try:
        map_bps = float(map_spread) if map_spread is not None else None
    except (TypeError, ValueError):
        map_bps = None
    basis_ap_raw = mkt.get("basis_ap_bps")
    try:
        basis_ap = float(basis_ap_raw) if basis_ap_raw is not None else None
    except (TypeError, ValueError):
        basis_ap = None
    return MtfFacts(
        trend_1h=trend,
        adx_regime=_adx_regime(adx),
        adx_1h=adx,
        closed_5m_available=float(_frame(tf, "5m_closed").get("close") or 0) > 0,
        closed_15m_available=float(_frame(tf, "15m_closed").get("close") or 0) > 0,
        funding_rate=fr,
        funding_extreme_long=fr is not None and fr >= FUNDING_SHORT_CONFIRM_MIN,
        funding_squeeze_short=fr is not None and fr <= FUNDING_SQUEEZE_MAX,
        ensemble=classify_ensemble(tf, trend_1h=trend),
        basis_ap_bps=basis_ap,
        mark_ap_spread_bps=map_bps,
    )


def mtf_confirm_veto(
    direction: str,
    tf: dict[str, Any],
    lifecycle_phase: str,
    *,
    market: dict[str, Any] | None = None,
    fall_from_high_pct: float = 0.0,
    bounce_from_low_pct: float = 0.0,
) -> tuple[bool, str]:
    """Return (blocked, reason). Hard vetoes only — soft scoring stays in engine."""
    d = direction.lower().strip()
    phase = str(lifecycle_phase or "").strip()

    if d == "short" and phase == "post_dump_bounce":
        return True, "mtf_post_dump_bounce_short"

    facts = snapshot(tf, market=market)
    bt = basis_thresholds()
    overheat = float(bt.get("ap_overheat_bps", BASIS_AP_OVERHEAT_BPS))
    underheat = float(bt.get("ap_underheat_bps", BASIS_AP_UNDERHEAT_BPS))

    if d == "short" and facts.trend_1h == "bull":
        # Allow mid-dump continuation shorts when structural fall is large
        if not (phase in {"dump_active", "distribution"} and fall_from_high_pct >= 15.0):
            return True, "mtf_1h_bull_vs_short"

    if d == "long" and facts.trend_1h == "bear":
        if phase not in {
            "post_dump_bounce",
            "impulse_initiating",
            "breakout_arming",
            "accumulation",
            "recovery",
        }:
            return True, "mtf_1h_bear_vs_long"

    if facts.funding_squeeze_short and d == "short":
        return True, "mtf_funding_squeeze_short"

    # Basis gate: smoothed (ap − index) / index — report Q02 (not raw mark−index).
    if (
        d == "long"
        and facts.basis_ap_bps is not None
        and facts.basis_ap_bps >= overheat
        and phase not in {"post_dump_bounce", "recovery", "accumulation"}
    ):
        return True, "mtf_basis_ap_overheat_long"

    if (
        d == "short"
        and facts.basis_ap_bps is not None
        and facts.basis_ap_bps <= underheat
        and phase in {"post_dump_bounce", "recovery"}
    ):
        return True, "mtf_basis_ap_underheat_short"

    # Q08: bounce recovery — avoid fade shorts while price is still bouncing.
    from hunt_watch.param_store import confirm_thresholds

    ct = confirm_thresholds()
    bounce_min = float(ct.get("short_bounce_recovery_bounce_min_pct", 8.0))
    fall_max = float(ct.get("short_bounce_recovery_fall_max_pct", 15.0))
    if (
        d == "short"
        and phase in {"accumulation", "recovery"}
        and bounce_from_low_pct >= bounce_min
        and fall_from_high_pct < fall_max
    ):
        return True, "mtf_bounce_recovery_short"

    # Legacy mark−ap spread kept for telemetry only (not gated here).
    if (
        d == "long"
        and facts.basis_ap_bps is None
        and facts.mark_ap_spread_bps is not None
        and facts.mark_ap_spread_bps >= overheat
        and phase not in {"post_dump_bounce", "recovery", "accumulation"}
    ):
        return True, "mtf_basis_ap_overheat_long"

    # Ensemble: volatile chop — block counter-trend longs outside bounce phases.
    if (
        d == "long"
        and facts.ensemble.label == "volatile_chop"
        and facts.trend_1h == "bear"
        and phase not in {"post_dump_bounce", "recovery", "accumulation"}
    ):
        return True, "mtf_volatile_chop_vs_long"

    # Accumulation long in bear 1H without HTF support (WLD: rsi39, chg24 -10%).
    r1h = _frame(tf, "1h_closed") or _frame(tf, "1h")
    r1h_rsi = float(r1h.get("rsi14") or 50.0)
    if (
        d == "long"
        and phase == "accumulation"
        and facts.trend_1h == "bear"
        and r1h_rsi < 45.0
        and fall_from_high_pct < 8.0
    ):
        return True, "mtf_bear_1h_blocks_accumulation_long"

    c5 = float(_frame(tf, "5m_closed").get("close") or 0.0)
    c15 = float(_frame(tf, "15m_closed").get("close") or 0.0)
    if c5 <= 0 or c15 <= 0:
        return True, "mtf_missing_closed_bars"

    return False, ""


def closed_rsi(tf: dict[str, Any], interval: str, default: float = 50.0) -> float:
    """RSI from closed frame only — no live-bar fallback."""
    key = f"{interval}_closed" if not interval.endswith("_closed") else interval
    row = _frame(tf, key)
    return float(row.get("rsi14") or default)
