"""Module 1 Deep — signal helpers: BTC context, trade direction resolution."""
from __future__ import annotations

from typing import Any


BTC_CORR_SOFT = 0.45
BTC_CORR_HARD = 0.70
BTC_CORR_SIGNIFICANT = BTC_CORR_HARD
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


def _readiness_display_ge(
    a: float | None, b: float | None, *, slack: float = 0.0
) -> bool:
    if a is None or b is None:
        return False
    return a >= b - slack


def _readiness_display_gt(
    a: float | None, b: float | None, *, margin: float = 0.0
) -> bool:
    if a is None or b is None:
        return False
    return a > b + margin


def _setup_strength(setup: dict[str, Any], *, direction: str) -> float:
    for key in ("delivery_p_win", "p_win", "catalog_p_win"):
        try:
            raw = setup.get(key)
            if raw is not None:
                return min(100.0, max(0.0, float(raw) * 100.0))
        except (TypeError, ValueError):
            continue
    for key in ("fusion_score", "magnitude", f"{direction}_fuel", "dump_fuel", "long_fuel"):
        try:
            raw = setup.get(key)
            if raw is not None:
                val = float(raw)
                if key.endswith("_fuel") or key == "fusion_score":
                    return min(100.0, max(0.0, val))
                return min(100.0, max(0.0, val * 100.0 if val <= 1.0 else val))
        except (TypeError, ValueError):
            continue
    return 0.0


def correlated_direction(
    *,
    short_strength: float,
    long_strength: float,
    btc_corr_1h: float | None,
    btc_trend: str,
    symbol: str = "",
) -> tuple[str, list[str]]:
    """Pick short/long with tiered BTC correlation overlay."""
    from hunt_core.params.store import btc_corr_thresholds

    notes: list[str] = []
    raw = "short" if short_strength >= long_strength else "long"
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
    if btc_trend == "up":
        aligned = "long" if corr > 0 else "short"
    else:
        aligned = "short" if corr > 0 else "long"

    aligned_strength = short_strength if aligned == "short" else long_strength
    raw_strength = short_strength if raw == "short" else long_strength
    strength_gap = raw_strength - aligned_strength

    tier = "hard" if abs_corr >= hard_min else "soft"
    gap_max = hard_gap if tier == "hard" else soft_gap
    notes.append(
        f"BTC {tier} · 1h {btc_trend} · corr={corr:+.2f} → приоритет {aligned.upper()}"
    )
    if raw != aligned and strength_gap <= gap_max:
        notes.append(
            f"conv {raw}={raw_strength:.0f} vs {aligned}={aligned_strength:.0f} — BTC {tier} bias"
        )
        return aligned, notes
    if raw != aligned:
        notes.append(f"сильный conv {raw}={raw_strength:.0f} перекрывает BTC {tier} bias")
    return raw, notes


def resolve_trade_direction(
    row: dict[str, Any],
) -> tuple[str, dict[str, Any], float, list[str]]:
    """Lifecycle bias first, then structure/BTC corr, then conviction (P-first)."""
    dump = row.get("dump") or {}
    long_setup = row.get("long") or {}
    lc = row.get("lifecycle") or {}
    structure = row.get("structure") if isinstance(row.get("structure"), dict) else {}
    short_strength = _setup_strength(dump, direction="short")
    long_strength = _setup_strength(long_setup, direction="long")
    bias = str(lc.get("recommended_bias") or "")
    phase = str(lc.get("phase") or "")
    struct_bias = str(
        structure.get("structure_bias") or lc.get("structure_bias") or ""
    ).lower()
    notes: list[str] = []

    if bias == "short" and phase in _SHORT_BIAS_PHASES:
        if short_strength >= 40 or short_strength >= long_strength - 12:
            direction = "short"
            notes.append(f"lifecycle bias=short phase={phase}")
        elif long_strength >= 75 and short_strength < 45:
            direction = "long"
            notes.append("long conviction override при weak short")
        else:
            direction = "short"
            notes.append("bias short — приоритет SHORT даже при lower conviction")
    elif bias == "long" and phase in _LONG_BIAS_PHASES:
        if long_strength >= 40 or long_strength >= short_strength - 12:
            direction = "long"
            notes.append(f"lifecycle bias=long phase={phase}")
        elif short_strength >= 75 and long_strength < 45:
            direction = "short"
            notes.append("short conviction override при weak long")
        else:
            direction = "long"
            notes.append("bias long — приоритет LONG")
    elif struct_bias in {"short", "long"} and bias in {"", "wait"}:
        direction = struct_bias
        notes.append(f"structure bias={struct_bias}")
    elif bias == "wait":
        direction = "short" if short_strength >= long_strength else "long"
        notes.append("bias=wait — monitor only, pick higher conviction")
    else:
        corr_raw = (row.get("regime") or {}).get("btc_corr_1h")
        direction, notes = correlated_direction(
            short_strength=short_strength,
            long_strength=long_strength,
            btc_corr_1h=float(corr_raw) if corr_raw is not None else None,
            btc_trend=str((row.get("btc_context") or {}).get("btc_trend") or "flat"),
            symbol=str(row.get("symbol") or ""),
        )

    setup = dump if direction == "short" else long_setup
    strength = short_strength if direction == "short" else long_strength

    from hunt_core.deliver.dispatch import (
        display_readiness_score,
        geometry_block_reason,
    )

    short_geo = geometry_block_reason(dump, row=row, direction="short")
    long_geo = geometry_block_reason(long_setup, row=row, direction="long")
    short_display = display_readiness_score(dump, direction="short", row=row)
    long_display = display_readiness_score(long_setup, direction="long", row=row)
    suppress_flip = bias == "wait" and phase in _SHORT_BIAS_PHASES

    if direction == "short" and short_geo and not suppress_flip:
        if not long_geo and _readiness_display_ge(long_display, short_display, slack=15):
            direction = "long"
            setup = long_setup
            strength = long_strength
            notes.append(f"SHORT headwind ({short_geo}) — показан LONG")
        elif long_geo and _readiness_display_gt(long_display, short_display, margin=8):
            direction = "long"
            setup = long_setup
            strength = long_strength
            notes.append(f"SHORT blocked ({short_geo}); LONG выше по display")
        else:
            notes.append(f"⚠️ SHORT conviction высокий, но {short_geo}")
    elif direction == "short" and short_geo and suppress_flip:
        notes.append(f"dump_active/wait — lean SHORT ({short_geo}), без flip на LONG")
    elif direction == "long" and long_geo:
        if not short_geo and _readiness_display_ge(short_display, long_display, slack=15):
            direction = "short"
            setup = dump
            strength = short_strength
            notes.append(f"LONG headwind ({long_geo}) — показан SHORT")
        elif short_geo and _readiness_display_gt(short_display, long_display, margin=8):
            direction = "short"
            setup = dump
            strength = short_strength
            notes.append(f"LONG blocked ({long_geo}); SHORT выше по display")
        else:
            notes.append(f"⚠️ LONG conviction высокий, но {long_geo}")

    return direction, setup, strength, notes
