"""Crowded-pump fade scoring — Polars MTF setup + 1m MACD trigger + REST flow."""

from __future__ import annotations

from typing import Any

_TRIGGER_REASONS = frozenset({
    "1m_macd_cross_down",
    "1m_macd_hist_neg",
    "1m_macd_exhaust",
    "below_support",
    "hunt_short_confirmed",
})

_SETUP_REASON_PREFIXES = (
    "1h_rsi=",
    "15m_rsi=",
    "top_ls=",
    "funding_crowded=",
    "phase=",
)


def _fval(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _market_val(market: dict[str, Any], micro: dict[str, Any], key: str) -> Any:
    val = market.get(key)
    if val is not None:
        return val
    return micro.get(key)


def _setup_hits(reasons: list[str]) -> int:
    return sum(1 for r in reasons if r.startswith(_SETUP_REASON_PREFIXES))


def _has_trigger(reasons: list[str]) -> bool:
    return any(
        r in _TRIGGER_REASONS
        or r.startswith("fall_trigger=")
        or r.startswith("below_support=")
        for r in reasons
    )


def score_dump_init(
    *,
    row: dict[str, Any],
    micro: dict[str, Any],
    tf: dict[str, Any],
    prev: dict[str, Any] | None,
) -> tuple[int, list[str], str]:
    """Score 0-100 dump initiation readiness + human verdict."""
    reasons: list[str] = []
    score = 0
    dump = row.get("dump") or {}
    lc = row.get("lifecycle") or {}
    market = row.get("market") or row.get("positioning") or {}
    price = float(row.get("price") or 0)

    phase = str(lc.get("phase") or "")
    fall = float(lc.get("fall_from_high_pct") or 0)
    if phase in ("exhaustion_at_high", "distribution"):
        score += 18
        reasons.append(f"phase={phase}")
    if fall >= 1.5:
        score += min(15, int(fall * 3))
        reasons.append(f"fall_from_high={fall:.1f}%")

    support = float(dump.get("support_break_level") or dump.get("invalidation_above") or 0)
    if support > 0 and price < support and fall >= 2.5:
        score += 20
        reasons.append(f"below_support={support:.5f}")

    # --- REST flow ---
    taker5 = _fval(_market_val(market, micro, "taker_5m"))
    if taker5 is not None and taker5 < 0.98:
        score += 12
        reasons.append(f"taker_5m_sell={taker5:.3f}")
    elif taker5 is not None and taker5 > 1.05:
        score -= 8
        reasons.append(f"taker_5m_buy={taker5:.3f}")

    oi1h = _fval(_market_val(market, micro, "oi_chg_1h"))
    if oi1h is not None and oi1h < -0.005:
        score += 10
        reasons.append(f"oi_flush_1h={oi1h:.3f}")

    oi_z = _fval(market.get("oi_z") or micro.get("oi_z"))
    if oi_z is not None and oi_z > 1.5:
        score += 5
        reasons.append(f"oi_z_elevated={oi_z}")

    fund = _fval(_market_val(market, micro, "funding_pct"))
    if fund is not None and fund >= 0.55:
        score += 10
        reasons.append(f"funding_crowded={fund:.3f}%")
    elif fund is not None and fund > 0.15:
        score += 8
        reasons.append(f"funding_crowded={fund:.3f}%")

    top_ls = _fval(_market_val(market, micro, "top_ls_1h"))
    if top_ls is not None and top_ls >= 1.85:
        score += 6
        reasons.append(f"top_ls={top_ls:.2f}")

    depth = _fval(market.get("depth_imbalance"))
    if depth is not None and depth <= -0.08:
        score += 8
        reasons.append(f"ask_heavy={depth:.2f}")

    # --- Polars MTF setup ---
    m1 = tf.get("1m") or {}
    m5 = tf.get("5m") or {}
    m15 = tf.get("15m") or {}
    m1h = tf.get("1h") or {}

    rsi1h = _fval(m1h.get("closed_rsi14") or m1h.get("rsi14"))
    if rsi1h is not None and rsi1h >= 88:
        score += 10
        reasons.append(f"1h_rsi={rsi1h:.0f}")

    rsi15 = _fval(m15.get("closed_rsi14") or m15.get("rsi14"))
    if rsi15 is not None and rsi15 >= 78:
        score += 8
        reasons.append(f"15m_rsi={rsi15:.0f}")

    rsi5 = _fval(m5.get("closed_rsi14") or m5.get("rsi14"))
    if rsi5 is not None and rsi5 > 70:
        score += 6
        reasons.append(f"5m_rsi={rsi5:.0f}")

    macd5 = _fval(m5.get("closed_macd_hist"))
    if macd5 is not None and macd5 < 0:
        score += 10
        reasons.append("5m_macd_hist_neg")

    # --- Phase 2: 1m MACD trigger (fast dump) ---
    macd1 = _fval(m1.get("closed_macd_hist") if m1.get("closed_macd_hist") is not None else m1.get("macd_hist"))
    if macd1 is not None:
        if macd1 < 0:
            score += 14
            reasons.append("1m_macd_hist_neg")
        elif macd1 <= 0.0002:
            score += 12
            reasons.append("1m_macd_exhaust")

    if prev and macd1 is not None:
        prev_m1 = (prev.get("timeframes") or {}).get("1m") or {}
        prev_macd = _fval(
            prev_m1.get("closed_macd_hist")
            if prev_m1.get("closed_macd_hist") is not None
            else prev_m1.get("macd_hist")
        )
        if prev_macd is not None and prev_macd > 0.0003 and macd1 <= 0:
            score += 16
            reasons.append("1m_macd_cross_down")

    if fall >= 4.0:
        score += 10
        reasons.append(f"fall_trigger={fall:.1f}%")

    if dump.get("confirmed"):
        score += 25
        reasons.append("hunt_short_confirmed")
    elif float(dump.get("dump_score") or 0) >= 68:
        score += 8
        reasons.append(f"dump_score={dump.get('dump_score')}")

    if prev:
        prev_fall = float((prev.get("lifecycle") or {}).get("fall_from_high_pct") or 0)
        if fall > prev_fall + 0.4:
            score += 8
            reasons.append(f"fall_accel +{fall - prev_fall:.1f}%")
        prev_price = float(prev.get("price") or 0)
        if prev_price > 0 and price < prev_price * 0.997:
            score += 6
            reasons.append("price_break_down")

    setup_hits = _setup_hits(reasons)
    trigger = _has_trigger(reasons) or bool(dump.get("confirmed"))

    score = max(0, min(100, score))
    if dump.get("confirmed"):
        verdict = "DUMP_LIKELY"
    elif score >= 85 and trigger and setup_hits >= 2:
        verdict = "DUMP_LIKELY"
    elif score >= 70 and (trigger or (setup_hits >= 3 and fall >= 2.0)):
        verdict = "DUMP_ARMED"
    elif score >= 50 and setup_hits >= 2:
        verdict = "DUMP_WATCH"
    elif setup_hits >= 1:
        verdict = "DUMP_WATCH"
    else:
        verdict = "PUMP_RISK"

    # Squeeze trap: crowded setup + aggressive taker buy, no fall yet → do not arm entry.
    if (
        taker5 is not None
        and taker5 > 1.12
        and fall < 2.0
        and not trigger
        and not dump.get("confirmed")
    ):
        verdict = "DUMP_WATCH"
    return score, reasons, verdict


__all__ = ["score_dump_init"]
