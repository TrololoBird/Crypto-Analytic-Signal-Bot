"""MTF snapshot and confirm vetoes (Phase 9 split)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hunt_core.params.store import basis_thresholds, confirm_thresholds
from hunt_core.toolkit.adx_thresholds import ADX_MEME_RANGE_MAX, ADX_MEME_TREND_MIN
from hunt_core.toolkit.trend import normalize_rsi14, trend_1h_bias
from hunt_core.scanner.gate._policy_regime import (
    BASIS_AP_OVERHEAT_BPS,
    BASIS_AP_UNDERHEAT_BPS,
    EnsembleRegime,
    FUNDING_SHORT_CONFIRM_MIN,
    FUNDING_SQUEEZE_BLOCK,
    FUNDING_SQUEEZE_WARN,
    _frame,
    classify,
    resolve_market_funding_rate,
)


@dataclass(frozen=True, slots=True)
class MtfFacts:
    trend_1h: str
    adx_regime: str
    adx_1h: float
    closed_5m_available: bool
    closed_15m_available: bool
    funding_rate: float | None
    funding_extreme_long: bool
    funding_squeeze_short: bool
    funding_squeeze_caution: bool
    ensemble: EnsembleRegime
    basis_ap_bps: float | None
    mark_ap_spread_bps: float | None


def _trend_1h(tf: dict[str, Any]) -> str:
    r1h = _frame(tf, "1h_closed") or _frame(tf, "1h")
    return trend_1h_bias(r1h)


def _adx_regime(adx: float) -> str:
    if adx >= ADX_MEME_TREND_MIN:
        return "trending"
    if adx > 0 and adx < ADX_MEME_RANGE_MAX:
        return "ranging"
    return "neutral"


def _closed_bar_close(tf: dict[str, Any], interval: str) -> float:
    block = _frame(tf, interval if interval.endswith("_closed") else f"{interval}_closed")
    if not block.get("closed_bar"):
        return 0.0
    candle = block.get("candle") if isinstance(block.get("candle"), dict) else {}
    try:
        if candle.get("close") is not None:
            return float(candle.get("close"))
        return float(block.get("close") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def snapshot(
    tf: dict[str, Any],
    *,
    market: dict[str, Any] | None = None,
) -> MtfFacts:
    r1h = _frame(tf, "1h_closed") or _frame(tf, "1h")
    adx = float(r1h.get("adx14") or 0.0)
    mkt = market or {}
    fr = resolve_market_funding_rate(mkt)
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
        closed_5m_available=bool(_frame(tf, "5m_closed").get("closed_bar")),
        closed_15m_available=bool(_frame(tf, "15m_closed").get("closed_bar")),
        funding_rate=fr,
        funding_extreme_long=fr is not None and fr >= FUNDING_SHORT_CONFIRM_MIN,
        funding_squeeze_short=fr is not None and fr <= FUNDING_SQUEEZE_BLOCK,
        funding_squeeze_caution=fr is not None and fr <= FUNDING_SQUEEZE_WARN,
        ensemble=classify(tf, trend_1h=trend),
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
    d = direction.lower().strip()
    phase = str(lifecycle_phase or "").strip()

    if d == "short" and phase == "post_dump_bounce":
        return True, "mtf_post_dump_bounce_short"

    facts = snapshot(tf, market=market)
    bt = basis_thresholds()
    overheat = float(bt.get("ap_overheat_bps", BASIS_AP_OVERHEAT_BPS))
    underheat = float(bt.get("ap_underheat_bps", BASIS_AP_UNDERHEAT_BPS))

    if d == "short" and facts.trend_1h == "bull":
        peak_fade = phase == "exhaustion_at_high"
        distribution_fade = phase == "distribution" and fall_from_high_pct >= 15.0
        if not (peak_fade or distribution_fade):
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

    if (
        d == "long"
        and facts.basis_ap_bps is not None
        and facts.basis_ap_bps >= overheat
        and phase not in {"post_dump_bounce", "recovery", "accumulation"}
    ):
        if phase == "breakout_arming":
            from hunt_core.scanner.gate._delivery_helpers import maps_accumulation_confirms

            try:
                map_acc = float((market or {}).get("map_vp_accumulation") or 0)
            except (TypeError, ValueError):
                map_acc = 0.0
            if not (maps_accumulation_confirms(market or {}, direction="long") and map_acc >= 0.50):
                return True, "mtf_basis_ap_overheat_long"
        else:
            return True, "mtf_basis_ap_overheat_long"

    if (
        d == "short"
        and facts.basis_ap_bps is not None
        and facts.basis_ap_bps <= underheat
        and phase in {"post_dump_bounce", "recovery"}
    ):
        return True, "mtf_basis_ap_underheat_short"

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

    if (
        d == "long"
        and facts.basis_ap_bps is None
        and facts.mark_ap_spread_bps is not None
        and facts.mark_ap_spread_bps >= overheat
        and phase not in {"post_dump_bounce", "recovery", "accumulation"}
    ):
        return True, "mtf_basis_ap_overheat_long"

    if (
        d == "long"
        and facts.ensemble.label == "volatile_chop"
        and facts.trend_1h == "bear"
        and phase not in {"post_dump_bounce", "recovery", "accumulation"}
    ):
        return True, "mtf_volatile_chop_vs_long"

    r1h = _frame(tf, "1h_closed") or _frame(tf, "1h")
    r1h_rsi = normalize_rsi14(float(r1h.get("rsi14") or 50.0))
    if (
        d == "long"
        and phase == "accumulation"
        and facts.trend_1h == "bear"
        and r1h_rsi < 45.0
        and fall_from_high_pct < 8.0
    ):
        from hunt_core.scanner.gate._delivery_helpers import maps_accumulation_confirms

        if maps_accumulation_confirms(market or {}, direction="long"):
            return False, ""
        return True, "mtf_bear_1h_blocks_accumulation_long"

    c5 = _closed_bar_close(tf, "5m_closed")
    c15 = _closed_bar_close(tf, "15m_closed")
    if c5 <= 0 or c15 <= 0:
        return True, "mtf_missing_closed_bars"

    return False, ""


def check_mtf_structure_break(
    direction: str,
    tf: dict[str, Any],
    *,
    level_expired: bool = False,
) -> tuple[bool, str]:
    if not level_expired:
        return True, ""
    d = direction.lower().strip()
    for interval in ("15m_closed", "1h_closed"):
        block = _frame(tf, interval)
        if not block.get("closed_bar"):
            continue
        candle = block.get("candle") if isinstance(block.get("candle"), dict) else {}
        close = float(block.get("close") or candle.get("close") or 0.0)
        if close <= 0:
            continue
        if d == "long":
            prev_hi = float(block.get("prev_high") or 0.0)
            if prev_hi > 0 and close > prev_hi:
                return True, f"mtf_structure_break_{interval}_long"
        elif d == "short":
            prev_lo = float(block.get("prev_low") or 0.0)
            if prev_lo > 0 and close < prev_lo:
                return True, f"mtf_structure_break_{interval}_short"
    return False, "mtf_structure_break_required"


def closed_rsi(tf: dict[str, Any], interval: str, default: float = 50.0) -> float:
    key = f"{interval}_closed" if not interval.endswith("_closed") else interval
    row = _frame(tf, key)
    return normalize_rsi14(float(row.get("rsi14") or default), default=default)
