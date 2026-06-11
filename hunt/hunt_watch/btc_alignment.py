"""BTC trend + correlation — prefer hunt direction aligned with BTC when correlated."""

from __future__ import annotations

from typing import Any

# Tiered BTC corr (live probe 2026-06-10: meme alts 0.1–0.24, SOXL 0.67).
BTC_CORR_SOFT = 0.45
BTC_CORR_HARD = 0.70
BTC_CORR_SIGNIFICANT = BTC_CORR_HARD  # legacy alias
BTC_TREND_MIN_CHG_PCT = 0.12


def btc_market_context(btc_work_1h: Any | None) -> dict[str, Any]:
    """1h/4h BTC change and trend label from prepared 1h frame."""
    if btc_work_1h is None or getattr(btc_work_1h, "is_empty", lambda: True)():
        return {}
    try:
        closes = [float(x) for x in btc_work_1h["close"].to_list()]
    except (TypeError, KeyError, ValueError):
        return {}
    if len(closes) < 3:
        return {}
    chg_1h = (closes[-1] / closes[-2] - 1.0) * 100.0
    chg_4h = (closes[-1] / closes[-5] - 1.0) * 100.0 if len(closes) >= 5 else None
    if chg_1h >= BTC_TREND_MIN_CHG_PCT:
        trend = "up"
    elif chg_1h <= -BTC_TREND_MIN_CHG_PCT:
        trend = "down"
    else:
        trend = "flat"
    return {
        "btc_chg_1h_pct": round(chg_1h, 2),
        "btc_chg_4h_pct": round(chg_4h, 2) if chg_4h is not None else None,
        "btc_trend": trend,
    }


_SHORT_BIAS_PHASES = frozenset(
    {"exhaustion_at_high", "distribution", "dump_active"}
)
_LONG_BIAS_PHASES = frozenset(
    {
        "post_dump_bounce",
        "accumulation",
        "recovery",
        "breakout_arming",
        "impulse_initiating",
    }
)


def resolve_trade_direction(
    row: dict[str, Any],
) -> tuple[str, dict[str, Any], float, list[str]]:
    """Lifecycle bias first, then BTC corr, then fuel (BEAT long-fuel vs short-bias fix)."""
    dump = row.get("dump") or {}
    long_setup = row.get("long") or {}
    lc = row.get("lifecycle") or {}
    short_fuel = float(dump.get("dump_fuel") or 0)
    long_fuel = float(long_setup.get("long_fuel") or 0)
    bias = str(lc.get("recommended_bias") or "")
    phase = str(lc.get("phase") or "")
    notes: list[str] = []

    if bias == "short" and phase in _SHORT_BIAS_PHASES:
        if short_fuel >= 40 or short_fuel >= long_fuel - 12:
            direction = "short"
            notes.append(f"lifecycle bias=short phase={phase}")
        elif long_fuel >= 75 and short_fuel < 45:
            direction = "long"
            notes.append("long fuel override при weak short")
        else:
            direction = "short"
            notes.append("bias short — приоритет SHORT даже при lower fuel")
    elif bias == "long" and phase in _LONG_BIAS_PHASES:
        if long_fuel >= 40 or long_fuel >= short_fuel - 12:
            direction = "long"
            notes.append(f"lifecycle bias=long phase={phase}")
        elif short_fuel >= 75 and long_fuel < 45:
            direction = "short"
            notes.append("short fuel override при weak long")
        else:
            direction = "long"
            notes.append("bias long — приоритет LONG")
    elif bias == "wait":
        direction = "short" if short_fuel >= long_fuel else "long"
        notes.append("bias=wait — monitor only, pick higher fuel")
    else:
        corr_raw = (row.get("regime") or {}).get("btc_corr_1h")
        direction, notes = correlated_direction(
            short_fuel=short_fuel,
            long_fuel=long_fuel,
            btc_corr_1h=float(corr_raw) if corr_raw is not None else None,
            btc_trend=str((row.get("btc_context") or {}).get("btc_trend") or "flat"),
            symbol=str(row.get("symbol") or ""),
        )

    setup = dump if direction == "short" else long_setup
    fuel = short_fuel if direction == "short" else long_fuel
    return direction, setup, fuel, notes


def correlated_direction(
    *,
    short_fuel: float,
    long_fuel: float,
    btc_corr_1h: float | None,
    btc_trend: str,
    symbol: str = "",
) -> tuple[str, list[str]]:
    """Pick short/long with tiered BTC correlation overlay."""
    from hunt_watch.param_store import btc_corr_thresholds

    notes: list[str] = []
    raw = "short" if short_fuel >= long_fuel else "long"
    th = btc_corr_thresholds(symbol)
    soft_min = float(th.get("corr_soft_min", BTC_CORR_SOFT))
    hard_min = float(th.get("corr_hard_min", BTC_CORR_HARD))
    soft_gap = float(th.get("soft_fuel_gap_max", 10.0))
    hard_gap = float(th.get("hard_fuel_gap_max", 18.0))

    if btc_corr_1h is None or btc_trend == "flat":
        notes.append(
            f"без BTC-фильтра (corr={btc_corr_1h if btc_corr_1h is not None else '—'})"
        )
        return raw, notes

    corr = float(btc_corr_1h)
    abs_corr = abs(corr)
    if abs_corr < soft_min:
        notes.append(f"без BTC-фильтра (corr={corr:+.2f} under {soft_min:.2f})")
        return raw, notes
    # Positive corr: alt moves with BTC. Negative: inverse.
    if btc_trend == "up":
        aligned = "long" if corr > 0 else "short"
        contra = "short" if aligned == "long" else "long"
    else:
        aligned = "short" if corr > 0 else "long"
        contra = "long" if aligned == "short" else "short"

    aligned_fuel = short_fuel if aligned == "short" else long_fuel
    raw_fuel = short_fuel if raw == "short" else long_fuel
    fuel_gap = raw_fuel - aligned_fuel

    tier = "hard" if abs_corr >= hard_min else "soft"
    gap_max = hard_gap if tier == "hard" else soft_gap
    notes.append(
        f"BTC {tier} · 1h {btc_trend} · corr={corr:+.2f} → приоритет {aligned.upper()}"
    )
    if raw != aligned and fuel_gap <= gap_max:
        notes.append(
            f"fuel {raw}={raw_fuel:.0f} vs {aligned}={aligned_fuel:.0f} — BTC {tier} bias"
        )
        return aligned, notes
    if raw != aligned:
        notes.append(f"сильный fuel {raw}={raw_fuel:.0f} перекрывает BTC {tier} bias")
    return raw, notes


def scenario_summary(
    *,
    direction: str,
    setup: dict[str, Any],
    fuel: float,
    lc: dict[str, Any],
    confirmed: bool,
) -> str:
    """One-line probable development path for Telegram."""
    phase = str(setup.get("phase") or "—")
    bias = str(lc.get("phase") or "—")
    if confirmed:
        hard = setup.get("confirm_hard") or []
        tail = ", ".join(str(h) for h in list(hard)[:3]) or "closed-bar"
        return f"✅ Confirm есть · сценарий: {direction.upper()} по {tail}"
    if fuel >= 60:
        return (
            f"⏳ Ждём confirm · вероятен {direction.upper()} "
            f"({phase}) при закрытии 5m/15m · lifecycle {bias}"
        )
    if fuel >= 45:
        return (
            f"👀 Формирование · {direction.upper()} {phase} "
            f"(fuel {fuel:.0f}) — нужен пробой + второй фактор"
        )
    return f"💤 Слабый сетап · {direction.upper()} fuel {fuel:.0f} — мониторинг без входа"


def forming_confirm_gaps(
    setup: dict[str, Any],
    *,
    direction: str,
    tf: dict[str, Any],
) -> list[str]:
    """Human gaps until closed-bar confirm."""
    gaps: list[str] = []
    if direction == "short":
        support = float(setup.get("support_break_level") or 0)
        r5 = float((tf.get("5m_closed") or {}).get("close") or 0)
        if support > 0 and (r5 <= 0 or r5 >= support):
            gaps.append("5m close below support")
        fuel = float(setup.get("dump_fuel") or 0)
        if fuel < 60:
            gaps.append(f"fuel≥60 (сейчас {fuel:.0f})")
        triggers = list(setup.get("triggers") or [])
        if not any("oi_flush" in t or "lost_support" in t or "div" in t for t in triggers):
            gaps.append("второй фактор (OI/div/continuation)")
    else:
        res = float(setup.get("resistance_break_level") or 0)
        r5 = float((tf.get("5m_closed") or {}).get("close") or 0)
        if res > 0 and (r5 <= 0 or r5 <= res):
            gaps.append("5m close above resistance")
        fuel = float(setup.get("long_fuel") or 0)
        if fuel < 60:
            gaps.append(f"fuel≥60 (сейчас {fuel:.0f})")
    return gaps
