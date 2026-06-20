"""Edge, MTF, and regime ensemble policy gates."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

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
    wide_hunter: bool = False
    long_tg_enabled: bool = False
    long_sl_max: float = LONG_SL_GATE
    long_tp1_min: float = LONG_TP1_GATE
    long_min_n: int = LONG_MIN_N

    @classmethod
    def from_env(cls) -> EdgePolicyConfig:
        wide = os.environ.get("HUNT_WIDE_MODE", "0") not in {"0", "false", "False"}
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

# Funding extremes (decimal rate per funding interval, e.g. 0.0001 ≈ 0.01%)
FUNDING_SHORT_CONFIRM_MIN = 0.001  # +0.1% overcrowded longs
FUNDING_SQUEEZE_WARN = -0.0015  # −0.15% cautious short (FMZ-style tier)
FUNDING_SQUEEZE_BLOCK = -0.002  # −0.20% hard block new shorts
FUNDING_SQUEEZE_MAX = FUNDING_SQUEEZE_BLOCK  # legacy alias
# Smoothed basis (ap − index) / index — report Q02/A.8; gate not raw mark−index.
BASIS_AP_OVERHEAT_BPS = 120.0
BASIS_AP_UNDERHEAT_BPS = -120.0


def resolve_market_funding_rate(mkt: dict[str, Any] | None) -> float | None:
    """Normalize funding to decimal rate per interval (Binance funding_rate scale)."""
    market = mkt if isinstance(mkt, dict) else {}
    funding = market.get("funding_live")
    if funding is None:
        funding = market.get("funding_rate")
    if funding is None and market.get("funding_pct") is not None:
        try:
            funding = float(market["funding_pct"]) / 100.0
        except (TypeError, ValueError):
            funding = None
    if funding is None:
        return None
    try:
        return float(funding)
    except (TypeError, ValueError):
        return None


def funding_short_risk_tier(fr: float | None) -> str:
    """ok | caution | block — tiered crowded-short gate (FMZ / quant practice)."""
    if fr is None:
        return "ok"
    if fr <= FUNDING_SQUEEZE_BLOCK:
        return "block"
    if fr <= FUNDING_SQUEEZE_WARN:
        return "caution"
    return "ok"


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
    funding_squeeze_caution: bool
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
            from hunt_core.gate._delivery_helpers import maps_accumulation_confirms

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
        from hunt_core.gate._delivery_helpers import maps_accumulation_confirms

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


# ── Declarative delivery gates (Phase 6 — rules in gate/_rules_table.py) ─────

from hunt_core.gate._rules_table import (  # noqa: E402
    DELIVERY_GATE_RULES,
    DeliveryGateTier,
)


def _decl_tier_matches(rule_tier: DeliveryGateTier, delivery_tier: str) -> bool:
    if rule_tier == "both":
        return True
    return rule_tier == delivery_tier


def _decl_snapshot_tier(row: dict[str, Any], setup: dict[str, Any]) -> str:
    from hunt_core.gate.delivery import _snapshot_tier_from_row  # noqa: PLC0415

    return _snapshot_tier_from_row(row, setup)


def _decl_check_data_complete(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.data.completeness import delivery_derivatives_complete
    from hunt_core.gate.delivery import GateResult  # noqa: PLC0415

    _ = direction, lifecycle, delivery_tier
    tier = _decl_snapshot_tier(row, setup)
    ok, missing = delivery_derivatives_complete(row, tier=tier)
    if ok:
        return None
    detail = ", ".join(missing[:8])
    if len(missing) > 8:
        detail += f" (+{len(missing) - 8})"
    return GateResult(
        False,
        "data_incomplete",
        f"Деривативы неполные ({tier}): {detail}",
    )


def _decl_check_data_stale(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.data.completeness import DELIVERY_MARKET_KEYS_FAST, DELIVERY_MARKET_KEYS_FULL
    from hunt_core.gate.delivery import GateResult  # noqa: PLC0415

    _ = direction, lifecycle, delivery_tier, symbol
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    max_age = float(os.getenv("HUNT_MAX_DERIVATIVE_AGE_S", "300") or 300)
    tier = _decl_snapshot_tier(row, setup)
    keys = DELIVERY_MARKET_KEYS_FAST if tier in {"fast", "hot"} else DELIVERY_MARKET_KEYS_FULL
    stale: list[str] = []
    for key in keys:
        age_raw = market.get(f"{key}_age_seconds")
        if age_raw is None:
            continue
        try:
            age_s = float(age_raw)
        except (TypeError, ValueError):
            continue
        if age_s > max_age:
            stale.append(f"{key}={age_s:.0f}s")
    if not stale:
        return None
    detail = ", ".join(stale[:6])
    if len(stale) > 6:
        detail += f" (+{len(stale) - 6})"
    return GateResult(
        False,
        "data_stale",
        f"Деривативы устарели (>{max_age:.0f}s): {detail}",
    )


def _structure_opposes_direction(bias: str, direction: str) -> bool:
    b = bias.lower().strip()
    d = direction.lower().strip()
    bearish = b in {"bear", "short", "down", "downtrend", "bearish"}
    bullish = b in {"bull", "long", "up", "uptrend", "bullish"}
    if d == "long" and bearish:
        return True
    if d == "short" and bullish:
        return True
    return False


def _structure_is_choch(struct: dict[str, Any]) -> bool:
    if bool(struct.get("choch")):
        return True
    event = str(struct.get("event") or struct.get("bos_choch") or "").lower()
    return "choch" in event


def _decl_check_structure_aligned(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.gate.delivery import GateResult  # noqa: PLC0415

    _ = setup, lifecycle, delivery_tier, symbol
    struct = row.get("structure")
    if not isinstance(struct, dict) or not struct:
        return None
    bias = str(
        struct.get("structure_bias") or struct.get("bias") or struct.get("htf_trend") or ""
    ).strip()
    if not bias or bias.lower() in {"neutral", "ranging", "range", "—", "wait"}:
        return None
    if _structure_is_choch(struct):
        return None
    if not _structure_opposes_direction(bias, direction):
        return None
    return GateResult(
        False,
        "structure_bias_conflict",
        f"Structure bias {bias} против {direction}",
    )


def _decl_check_lifecycle_context(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    """MLIVE-8: block direction vs 24h tape contradictions without CHoCH."""
    from hunt_core.gate.delivery import GateResult  # noqa: PLC0415

    _ = setup, delivery_tier, symbol
    phase = str(lifecycle.get("phase") or "")
    try:
        chg = abs(float(row.get("chg_24h_pct") or row.get("change_24h_pct") or 0))
    except (TypeError, ValueError):
        chg = 0.0
    struct = row.get("structure") if isinstance(row.get("structure"), dict) else {}
    choch = _structure_is_choch(struct)
    d = direction.lower().strip()
    if d == "long" and phase == "impulse_initiating" and chg >= 15.0 and not choch:
        return GateResult(
            False,
            "lifecycle_context_veto",
            f"Long impulse при −{chg:.1f}%/24h без CHoCH — knife-catch",
        )
    if d == "short" and phase in {"dump_initiating", "dump_active"} and chg < 3.0:
        return GateResult(
            False,
            "lifecycle_context_veto",
            f"Short dump при flat 24h ({chg:.1f}%) — нет импульса",
        )
    bias = str(lifecycle.get("recommended_bias") or "")
    if (
        d == "short"
        and phase == "dump_active"
        and bias == "wait"
        and not choch
        and float(setup.get("ignition_score") or 0) < float(
            os.getenv("HUNT_IGNITION_OVERRIDE", "55") or 55
        )
    ):
        return GateResult(
            False,
            "bias_wait_mid_dump",
            "Bias wait + mid-dump без CHoCH/ignition — только monitor",
        )
    return None


def _decl_check_at_level(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.gate.delivery import GateResult, price_in_entry_zone  # noqa: PLC0415

    _ = lifecycle, delivery_tier, symbol
    struct = row.get("structure")
    if isinstance(struct, dict) and bool(struct.get("at_level")):
        return None
    price = float(row.get("price") or 0)
    if price > 0 and price_in_entry_zone(setup, price, direction=direction):
        return None
    return GateResult(
        False,
        "not_at_level",
        "Цена вне entry zone и structure.at_level не выставлен",
    )


def _decl_check_rr_floor(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.contract import compute_setup_risk_reward
    from hunt_core.gate.delivery import (  # noqa: PLC0415
        GateResult,
        _effective_min_rr,
    )
    from hunt_core.params.store import effective_hunt_params

    _ = row, delivery_tier
    sym = symbol.upper()
    cal = effective_hunt_params(sym)
    min_rr = _effective_min_rr(
        setup,
        direction=direction,
        symbol=sym,
        lc=lifecycle,
        cal=cal,
    )
    rr = compute_setup_risk_reward(setup, direction=direction)
    if rr is not None:
        setup["risk_reward"] = rr
    if rr is None:
        return GateResult(False, "rr_missing", "R:R не вычислен — нет entry/SL/TP1")
    if float(rr) < min_rr:
        return GateResult(False, "rr_below_min", f"R:R {float(rr):.2f} < min {min_rr:.2f}")
    return None


def _decl_check_playbook(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.analysis.playbook_eval import setup_meets_playbook
    from hunt_core.gate._ev import legacy_fuel_delivery_enabled
    from hunt_core.gate.delivery import GateResult  # noqa: PLC0415

    _ = lifecycle, delivery_tier, symbol
    if legacy_fuel_delivery_enabled():
        return None
    dir_lit = "short" if direction == "short" else "long"
    if setup_meets_playbook(setup, row=row, direction=dir_lit):  # type: ignore[arg-type]
        return None
    fusion = row.get("manipulation_fusion") if isinstance(row.get("manipulation_fusion"), dict) else {}
    pc = fusion.get("pass_count", 0)
    req = fusion.get("required_n", 0)
    arch = fusion.get("archetype") or "none"
    return GateResult(
        False,
        "playbook_fail",
        f"Playbook {pc}/{req} для {arch} — N-of-M не пройден",
    )


def _decl_check_ev_delivery(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.gate._ev import (
        delivery_ev_floors,
        legacy_fuel_delivery_enabled,
        pwin_gate_enabled,
        resolve_delivery_ev,
    )
    from hunt_core.gate.delivery import GateResult  # noqa: PLC0415
    from hunt_core.gate._delivery_helpers import (
        count_fuel_evidence,
        evidence_coverage_ratio,
    )

    _ = lifecycle, delivery_tier
    sym = symbol.upper()
    dir_lit = "short" if direction == "short" else "long"
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    present, total = count_fuel_evidence(market, direction=dir_lit)
    coverage = evidence_coverage_ratio(market, direction=dir_lit)
    setup["fuel_evidence_present"] = present
    setup["fuel_evidence_total"] = total
    setup["fuel_evidence_coverage"] = round(coverage, 3)

    if legacy_fuel_delivery_enabled():
        from hunt_core.gate._delivery_helpers import evidence_adjusted_min_fuel
        from hunt_core.gate.delivery import _setup_fuel
        from hunt_core.params.store import delivery_thresholds

        dl = delivery_thresholds(sym)
        base_min_fuel = float(dl.get("min_fuel", 72.0))
        evidence_floor = evidence_adjusted_min_fuel(base_min_fuel, coverage)
        setup["_declarative_evidence_floor"] = evidence_floor
        if evidence_floor is None:
            return GateResult(
                False,
                "fuel_evidence_sparse",
                f"Недостаточно evidence для fuel ({present}/{total}, coverage {coverage:.0%})",
            )
        fuel = _setup_fuel(setup, direction)
        if fuel < float(evidence_floor):
            return GateResult(
                False,
                "below_min_fuel",
                f"Fuel {fuel:.0f} < evidence floor {float(evidence_floor):.0f} "
                f"(coverage {coverage:.0%}, {present}/{total})",
            )
        return None

    struct = row.get("structure") if isinstance(row.get("structure"), dict) else {}
    resolved = resolve_delivery_ev(setup, direction=dir_lit, row=row, structure=struct)
    ev = resolved.get("ev")
    p_win = resolved.get("p_win")
    confirmed = bool(setup.get("confirmed") or setup.get("intrabar_confirmed"))
    min_ev, min_p = delivery_ev_floors(sym, confirmed=confirmed)

    if ev is not None:
        setup["delivery_ev"] = ev
    if p_win is not None:
        setup["delivery_p_win"] = p_win
    setup["delivery_ev_source"] = resolved.get("source")

    if ev is None:
        reason = resolved.get("reason") or "incomplete_levels"
        return GateResult(
            False,
            "ev_incomplete",
            f"EV не вычислен ({reason}) — нет entry/SL/TP1 или P",
        )
    try:
        ev_f = float(ev)
    except (TypeError, ValueError):
        return GateResult(False, "ev_incomplete", "EV не числовой")
    if ev_f <= min_ev:
        return GateResult(
            False,
            "ev_below_floor",
            f"EV {ev_f:.4f} ≤ floor {min_ev:.4f}",
        )
    if not pwin_gate_enabled():
        shadow = setup.get("ev_shadow")
        if not isinstance(shadow, dict):
            shadow = {}
            setup["ev_shadow"] = shadow
        if p_win is not None:
            shadow["p_win"] = p_win
            shadow["p_win_shadow_only"] = True
        return None
    if p_win is None:
        return GateResult(False, "p_win_missing", "P(win) не вычислен для delivery")
    try:
        p_f = float(p_win)
    except (TypeError, ValueError):
        return GateResult(False, "p_win_missing", "P(win) не числовой")
    if p_f < min_p:
        return GateResult(
            False,
            "p_win_below_floor",
            f"P(win) {p_f:.2f} < floor {min_p:.2f}",
        )
    return None


def _decl_check_structural_trigger(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.gate.delivery import GateResult, _structural_hard_count  # noqa: PLC0415

    _ = row, lifecycle, symbol
    if delivery_tier != "triggered":
        return None
    if setup.get("intrabar_confirmed"):
        return None
    hard = setup.get("confirm_hard") or []
    struct_n = _structural_hard_count(hard, direction=direction)
    if struct_n >= 1:
        return None
    return GateResult(
        False,
        "no_structural_trigger",
        f"Нет structural trigger (hard={struct_n}, нужен ≥1)",
    )


def _decl_check_ignition_floor(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.gate.delivery import GateResult  # noqa: PLC0415
    from hunt_core.gate._delivery_helpers import EARLY_ADVISORY_MIN_IGNITION

    _ = row, direction, lifecycle, symbol
    if delivery_tier != "armed":
        return None
    if not (
        setup.get("anticipation")
        or setup.get("early_tier") == "armed"
        or setup.get("intrabar_armed")
    ):
        return None
    ign = setup.get("ignition_score")
    if ign is None:
        return None
    try:
        ign_f = float(ign)
    except (TypeError, ValueError):
        return GateResult(False, "ignition_low", "Ignition score invalid")
    min_ign = float(os.getenv("HUNT_MIN_IGNITION_ARMED", str(EARLY_ADVISORY_MIN_IGNITION)) or EARLY_ADVISORY_MIN_IGNITION)
    if ign_f < min_ign:
        return GateResult(
            False,
            "ignition_low",
            f"Ignition {ign_f:.0f} < min {min_ign:.0f} для ARMED",
        )
    return None


def _decl_check_orderflow_present(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    """Soft optional — annotate only; never blocks delivery."""
    from hunt_core.gate._delivery_helpers import _orderflow_confirm_aligned

    _ = lifecycle, delivery_tier
    sym = symbol.upper()
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    aligned, reason = _orderflow_confirm_aligned(direction, market, symbol=sym)
    if aligned:
        setup.pop("orderflow_soft_note", None)
        return None
    if reason and market.get("agg_trade_delta_60s") is not None:
        setup["orderflow_soft_note"] = reason
    return None


def _decl_check_setup_type(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.gate.delivery import GateResult  # noqa: PLC0415

    _ = row, direction, lifecycle, symbol
    if delivery_tier != "triggered":
        return None
    if not bool(setup.get("confirmed") or setup.get("intrabar_confirmed")):
        return None
    st = setup.get("setup_type") or row.get("setup_type")
    if st is None and setup.get("ev_primary") and setup.get("catalog_setup"):
        from hunt_core.setups.catalog import catalog_struct_setup_type

        st = catalog_struct_setup_type(str(setup.get("catalog_setup")))
        if st:
            setup["setup_type"] = st
    if st is None:
        return GateResult(
            False,
            "no_setup_type",
            "Нет структурного setup_type — только monitor",
        )
    return None


def _decl_check_meme_pump_volume(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.gate._quality import check_meme_pump_volume_ratio  # noqa: PLC0415

    _ = delivery_tier
    return check_meme_pump_volume_ratio(
        setup,
        direction=direction,
        lifecycle=lifecycle if isinstance(lifecycle, dict) else {},
        row=row,
        symbol=symbol.upper(),
    )


def _decl_check_meme_anomaly(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.gate._quality import (  # noqa: PLC0415
        _row_chg24_abs,
        _row_rng24,
        passes_meme_anomaly_gate,
    )
    from hunt_core.gate.delivery import GateResult  # noqa: PLC0415
    from hunt_core.params.store import effective_hunt_params

    _ = setup, delivery_tier
    sym = symbol.upper()
    cal = effective_hunt_params(sym)
    lc = lifecycle if isinstance(lifecycle, dict) else {}
    if passes_meme_anomaly_gate(sym=sym, row=row, lc=lc, cal=cal):
        return None
    chg24 = _row_chg24_abs(row)
    rng24 = _row_rng24(row)
    return GateResult(
        False,
        "not_anomaly",
        f"Не meme-аномалия: chg24={chg24:.1f}% range={rng24:.1f}% "
        f"(нужно ≥{cal.anomaly_min_chg_24h_pct}% или ≥{cal.anomaly_min_range_24h_pct}%)",
    )


def _decl_check_delivery_confluence(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.gate._quality import check_delivery_confluence  # noqa: PLC0415

    _ = delivery_tier
    if not bool(setup.get("confirmed") or setup.get("intrabar_confirmed")):
        return None
    return check_delivery_confluence(
        setup,
        direction=direction,
        symbol=symbol.upper(),
        lifecycle=lifecycle,
        row=row,
    )


def _decl_check_exhaustion_fade(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.gate._quality import check_exhaustion_fade  # noqa: PLC0415

    _ = delivery_tier
    if not bool(setup.get("confirmed") or setup.get("intrabar_confirmed")):
        return None
    return check_exhaustion_fade(
        setup,
        direction=direction,
        symbol=symbol.upper(),
        lifecycle=lifecycle,
        row=row,
    )


def _decl_check_impulse_long(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.gate._quality import check_impulse_long  # noqa: PLC0415

    _ = delivery_tier
    if direction != "long" or not bool(setup.get("confirmed") or setup.get("intrabar_confirmed")):
        return None
    return check_impulse_long(
        setup, lifecycle=lifecycle, row=row, symbol=symbol.upper()
    )


def _decl_check_accumulation_long(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.gate._quality import check_accumulation_long  # noqa: PLC0415

    _ = delivery_tier
    if direction != "long" or not bool(setup.get("confirmed") or setup.get("intrabar_confirmed")):
        return None
    return check_accumulation_long(
        setup, lifecycle=lifecycle, row=row, symbol=symbol.upper()
    )


def _decl_check_wash_baseline(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    from hunt_core.gate.delivery import GateResult  # noqa: PLC0415

    _ = setup, direction, lifecycle, delivery_tier, symbol
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    baseline = market.get("quote_vol_baseline")
    if baseline is None and market.get("quote_vol_history"):
        return GateResult(
            False,
            "wash_no_baseline",
            "Wash gate: quote_vol_baseline не вычислен",
        )
    return None


def _decl_check_ev_shadow(
    *,
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    delivery_tier: str,
    symbol: str,
) -> Any | None:
    import os

    from hunt_core.gate.delivery import GateResult  # noqa: PLC0415

    _ = row, direction, lifecycle, symbol, delivery_tier
    flip_on = os.environ.get("HUNT_EV_FLIP", "0").strip().lower() in {"1", "true", "yes"}
    delivery_on = os.environ.get("HUNT_EV_DELIVERY", "0").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    if not flip_on and not delivery_on:
        return None
    ev_block = setup.get("ev_shadow") if isinstance(setup.get("ev_shadow"), dict) else {}
    ev_val = ev_block.get("ev")
    if ev_val is None:
        return None
    try:
        ev_f = float(ev_val)
    except (TypeError, ValueError):
        return None
    if delivery_on:
        try:
            min_ev = float(os.getenv("HUNT_EV_MIN", "0") or 0)
        except (TypeError, ValueError):
            min_ev = 0.0
        if ev_f < min_ev:
            return GateResult(
                False,
                "ev_delivery_block",
                f"EV {ev_f:.3f} < floor {min_ev:.3f} (HUNT_EV_DELIVERY=1)",
            )
    if flip_on and ev_f < 0:
        return GateResult(
            False,
            "ev_shadow_negative",
            f"EV shadow {ev_f:.3f} < 0 (HUNT_EV_FLIP=1)",
        )
    return None


_DECL_CHECK_DISPATCH: dict[str, Any] = {
    "_decl_check_data_complete": _decl_check_data_complete,
    "_decl_check_data_stale": _decl_check_data_stale,
    "_decl_check_structure_aligned": _decl_check_structure_aligned,
    "_decl_check_lifecycle_context": _decl_check_lifecycle_context,
    "_decl_check_at_level": _decl_check_at_level,
    "_decl_check_rr_floor": _decl_check_rr_floor,
    "_decl_check_playbook": _decl_check_playbook,
    "_decl_check_ev_delivery": _decl_check_ev_delivery,
    "_decl_check_structural_trigger": _decl_check_structural_trigger,
    "_decl_check_ignition_floor": _decl_check_ignition_floor,
    "_decl_check_orderflow_present": _decl_check_orderflow_present,
    "_decl_check_setup_type": _decl_check_setup_type,
    "_decl_check_meme_pump_volume": _decl_check_meme_pump_volume,
    "_decl_check_meme_anomaly": _decl_check_meme_anomaly,
    "_decl_check_ev_shadow": _decl_check_ev_shadow,
    "_decl_check_delivery_confluence": _decl_check_delivery_confluence,
    "_decl_check_exhaustion_fade": _decl_check_exhaustion_fade,
    "_decl_check_impulse_long": _decl_check_impulse_long,
    "_decl_check_accumulation_long": _decl_check_accumulation_long,
    "_decl_check_wash_baseline": _decl_check_wash_baseline,
}


def run_declarative_delivery_gates(
    row: dict[str, Any],
    setup: dict[str, Any],
    direction: str,
    lifecycle: dict[str, Any],
    *,
    tier: Literal["armed", "triggered"] = "triggered",
    symbol: str = "",
) -> Any | None:
    """Run ordered declarative gates; first failure wins."""
    lc = lifecycle if isinstance(lifecycle, dict) else {}
    sym = symbol.upper() or str(row.get("symbol", "")).upper()
    for rule in DELIVERY_GATE_RULES:
        if not _decl_tier_matches(rule.required_for_tier, tier):
            continue
        checker = _DECL_CHECK_DISPATCH.get(rule.check_fn)
        if checker is None:
            continue
        blocked = checker(
            row=row,
            setup=setup,
            direction=direction,
            lifecycle=lc,
            delivery_tier=tier,
            symbol=sym,
        )
        if blocked is not None:
            return blocked
    return None
