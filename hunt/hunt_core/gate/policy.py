"""Edge, MTF, and regime ensemble policy gates."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hunt_core.analysis.adx_thresholds import (
    ADX_MEME_RANGE_MAX,
    ADX_MEME_TREND_MIN,
    ADX_RANGE_MAX,
    ADX_TREND_MIN,
)
from hunt_core.analysis.trend_engine import normalize_rsi14, trend_1h_bias
from hunt_core.params.store import basis_thresholds, confirm_thresholds
from hunt_core.paths import GATE_EDGE_OUTCOMES

LONG_SL_GATE = 0.35
LONG_TP1_GATE = 0.25
LONG_MIN_N = 30
SHORT_SL_BASELINE = 0.30


@dataclass(frozen=True, slots=True)
class EdgePolicyConfig:
    wide_hunter: bool = True
    long_tg_enabled: bool = False
    long_sl_max: float = LONG_SL_GATE
    long_tp1_min: float = LONG_TP1_GATE
    long_min_n: int = LONG_MIN_N

    @classmethod
    def from_env(cls) -> EdgePolicyConfig:
        wide = os.environ.get("HUNT_WIDE_MODE", "1") not in {"0", "false", "False"}
        long_on = os.environ.get("HUNT_LONG_TG", "0") in {"1", "true", "True"}
        return cls(wide_hunter=wide, long_tg_enabled=long_on)


def _load_gate_edge_long_stats(path: Path | None = None) -> dict[str, Any]:
    p = path or GATE_EDGE_OUTCOMES
    if not p.exists():
        return {"n": 0, "sl_rate": None, "tp1_plus_rate": None}
    rows: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("direction") == "long":
            rows.append(row)
    n = len(rows)
    if n == 0:
        return {"n": 0, "sl_rate": None, "tp1_plus_rate": None}
    sl = sum(1 for r in rows if r.get("bt_outcome") == "sl_hit")
    tp1p = sum(1 for r in rows if r.get("bt_outcome") in ("tp1_hit", "tp2_hit"))
    return {"n": n, "sl_rate": sl / n, "tp1_plus_rate": tp1p / n}


def long_tg_allowed(config: EdgePolicyConfig | None = None) -> tuple[bool, str]:
    """Return (allowed, reason) for long Telegram delivery."""
    cfg = config or EdgePolicyConfig.from_env()
    if not cfg.wide_hunter:
        return False, "wide_mode_off"
    if cfg.long_tg_enabled:
        return True, "env_override"
    stats = _load_gate_edge_long_stats()
    n = int(stats["n"])
    if n < cfg.long_min_n:
        return False, f"long_n_below_{cfg.long_min_n}"
    sl = stats.get("sl_rate")
    tp1p = stats.get("tp1_plus_rate")
    if sl is None or sl > cfg.long_sl_max:
        return False, f"long_sl_{sl:.2f}" if sl is not None else "long_sl_unknown"
    if tp1p is None or tp1p < cfg.long_tp1_min:
        return False, f"long_tp1_{tp1p:.2f}" if tp1p is not None else "long_tp1_unknown"
    return True, "edge_gate_pass"


def direction_block_reason(
    direction: str,
    *,
    config: EdgePolicyConfig | None = None,
) -> str | None:
    """Machine block code if direction vetoed by H-B edge policy."""
    cfg = config or EdgePolicyConfig.from_env()
    if direction == "long":
        ok, reason = long_tg_allowed(cfg)
        if not ok:
            return f"hb_long_{reason}"
    return None


# Canonical ADX thresholds (analysis/adx_thresholds.py)
ADX_TREND = ADX_TREND_MIN
ADX_RANGE = ADX_RANGE_MAX
ATR_PCT_HIGH = 5.0
ATR_PCT_LOW = 2.0
BB_SQUEEZE_PCTILE = 0.25


@dataclass(frozen=True, slots=True)
class EnsembleRegime:
    """Composite label from three independent votes."""

    label: str  # trend_up | trend_down | range | squeeze | volatile_chop
    adx_vote: str  # trending | ranging | neutral
    vol_vote: str  # high | normal | low
    chop_vote: str  # squeeze | choppy | clean
    votes_agree: int  # 0–3


def _frame(tf: dict[str, Any], key: str) -> dict[str, Any]:
    row = tf.get(key)
    return row if isinstance(row, dict) else {}


def classify(tf: dict[str, Any], *, trend_1h: str = "neutral") -> EnsembleRegime:
    """Classify structure from closed 1h frame (fallback: live 1h)."""
    r1h = _frame(tf, "1h_closed") or _frame(tf, "1h")
    adx = float(r1h.get("adx14") or 0.0)
    atr_pct = float(r1h.get("atr_pct") or 0.0)
    squeeze_on = bool(r1h.get("squeeze_on"))
    bb_pctile = r1h.get("bb_width_pctile")
    bb_low = bb_pctile is not None and float(bb_pctile) <= BB_SQUEEZE_PCTILE

    if adx >= ADX_TREND:
        adx_vote = "trending"
    elif 0 < adx < ADX_RANGE:
        adx_vote = "ranging"
    else:
        adx_vote = "neutral"

    if atr_pct >= ATR_PCT_HIGH:
        vol_vote = "high"
    elif 0 < atr_pct < ATR_PCT_LOW:
        vol_vote = "low"
    else:
        vol_vote = "normal"

    if squeeze_on or bb_low:
        chop_vote = "squeeze"
    elif adx_vote == "ranging" and vol_vote == "high":
        chop_vote = "choppy"
    else:
        chop_vote = "clean"

    votes = {adx_vote, vol_vote, chop_vote}
    agree = 3 - len(votes) + 1 if len(votes) < 3 else 1

    if chop_vote == "squeeze":
        label = "squeeze"
    elif chop_vote == "choppy" and adx_vote != "trending":
        label = "volatile_chop"
    elif adx_vote == "trending":
        label = "trend_up" if trend_1h == "bull" else ("trend_down" if trend_1h == "bear" else "range")
    else:
        label = "range"

    return EnsembleRegime(
        label=label,
        adx_vote=adx_vote,
        vol_vote=vol_vote,
        chop_vote=chop_vote,
        votes_agree=agree,
    )


# Meme perp ADX thresholds — see hunt_core.analysis.adx_thresholds

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
    """Return close from a confirmed closed bar block, else 0."""
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
        closed_5m_available=bool(_frame(tf, "5m_closed").get("closed_bar")),
        closed_15m_available=bool(_frame(tf, "15m_closed").get("closed_bar")),
        funding_rate=fr,
        funding_extreme_long=fr is not None and fr >= FUNDING_SHORT_CONFIRM_MIN,
        funding_squeeze_short=fr is not None and fr <= FUNDING_SQUEEZE_MAX,
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
        peak_fade = phase == "exhaustion_at_high"
        dump_cont = phase in {"dump_active", "distribution"} and fall_from_high_pct >= 15.0
        if not (peak_fade or dump_cont):
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
    """Re-entry permission after level test expiry (Phase 4A)."""
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
    """RSI from closed frame only — no live-bar fallback."""
    key = f"{interval}_closed" if not interval.endswith("_closed") else interval
    row = _frame(tf, key)
    return normalize_rsi14(float(row.get("rsi14") or default), default=default)
