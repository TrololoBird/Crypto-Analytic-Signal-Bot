"""Pre-pump scanner path (§4.2 — long bounce / squeeze-up)."""
from __future__ import annotations

from typing import Any


from hunt_core.domain.market_regime import HuntCalibratedParams
from hunt_core.params.store import (
    confirm_thresholds,
    effective_hunt_params,
    entry_confirm_tf,
    listings_thresholds,
    scoring_thresholds,
)


def _htf_bias_override(*args, **kwargs):
    from hunt_core.regime.leg_fsm import htf_bias_override
    return htf_bias_override(*args, **kwargs)


import hunt_core.scan._confirm_shared as _confirm_shared

globals().update(
    {k: v for k, v in vars(_confirm_shared).items() if not k.startswith("__")}
)

def confirm_long(
    long_setup: dict[str, Any],
    tf: dict[str, Any],
    *,
    symbol: str = "",
    price: float = 0.0,
    market: dict[str, Any] | None = None,
    cal: HuntCalibratedParams,
    lifecycle_bias: str = "",
    lifecycle_phase: str = "",
) -> tuple[bool, list[str]]:

    if long_setup.get("levels_viable") is False:
        return False, ["veto_levels:" + ",".join(long_setup.get("levels_veto") or [])]
    lst = listings_thresholds(symbol)
    bars_1h = int(long_setup.get("bars_1h") or 0)
    if long_setup.get("young_listing") and bars_1h < int(lst.get("min_1h_bars_confirm", 24)):
        return False, ["veto_young_listing_insufficient_bars"]
    lc_phase = str(
        lifecycle_phase or long_setup.get("lifecycle_phase") or ""
    )
    if lifecycle_bias in {"short", "wait"} and lc_phase not in _LONG_PUMP_PHASES:
        veto = "veto_lifecycle_bias_short" if lifecycle_bias == "short" else "veto_lifecycle_bias_wait"
        return False, [veto]
    phase_4h = _resolve_lifecycle_4h(long_setup)
    blocked_htf, htf_reason = _htf_bias_override(phase_4h, "long")
    if blocked_htf:
        return False, [f"veto_{htf_reason}"]
    mkt = market or {}
    hard: list[str] = []
    resistance = long_setup.get("resistance_break_level") or 0.0
    c1 = _closed_candle(tf, "1m")
    c5 = _closed_candle(tf, "5m")
    r5_close = _closed_tf_close(tf, "5m")
    r15_rsi = _required_closed_rsi(tf, "15m")
    if r15_rsi is None:
        return False, ["veto_data_missing_rsi15m"]
    entry_tf = entry_confirm_tf(symbol, direction="long")
    hard.extend(
        _structural_close_break_triggers(
            direction="long",
            level=float(resistance or 0),
            tf=tf,
            entry_tf=entry_tf,
        )
    )
    lc = long_setup.get("lifecycle") if isinstance(long_setup.get("lifecycle"), dict) else {}
    bounce_pct = float(lc.get("bounce_from_low_pct") or long_setup.get("bounce_from_low_pct") or 0)
    from hunt_core.gate.policy import mtf_confirm_veto  # noqa: PLC0415

    blocked, mtf_reason = mtf_confirm_veto(
        "long",
        tf,
        lc_phase,
        market=mkt,
        fall_from_high_pct=float(long_setup.get("fall_from_high_pct") or 0),
        bounce_from_low_pct=bounce_pct,
    )
    if blocked:
        return False, [f"veto_{mtf_reason}"]
    if long_setup.get("level_expired"):
        from hunt_core.gate.policy import check_mtf_structure_break  # noqa: PLC0415

        allowed, sb_reason = check_mtf_structure_break("long", tf, level_expired=True)
        if not allowed:
            return False, [f"veto_{sb_reason}"]

    if long_resistance_chase_veto(
        resistance, float(price or 0) or r5_close, r5_close
    ):
        return False, ["veto_price_below_resistance"]
    if c5.get("bullish") and c5.get("lower_wick_ratio", 0) >= 0.35 and r15_rsi <= 40:
        hard.append("5m_bounce_oversold")
    if c1.get("bullish") and c5.get("bullish") and c1.get("lower_wick_ratio", 0) >= 0.35:
        hard.append("1m_5m_bull_cascade")
    r15_closed = _closed_tf_block(tf, "15m")
    r1h_closed = _closed_tf_block(tf, "1h")
    if r15_closed.get("closed_bar") and r15_closed.get("pp_long_true"):
        hard.append("pp_long_break")
    elif r1h_closed.get("closed_bar") and r1h_closed.get("pp_long_true"):
        hard.append("pp_long_break")
    hard.extend(
        candle_pattern_hard_triggers(
            long_setup, direction="long", tf=tf, price=float(price or 0)
        )
    )

    fuel = float(long_setup.get("long_fuel") or 0)
    r4h_closed = _closed_tf_block(tf, "4h")
    div = (
        r1h_closed.get("bullish_rsi_div")
        or r4h_closed.get("bullish_rsi_div")
        or r1h_closed.get("bullish_macd_div")
        or r4h_closed.get("bullish_macd_div")
    )
    triggers = long_setup.get("triggers") or []
    structural = [
        h
        for h in hard
        if _is_structural_confirm_trigger(h) or h in {"pp_long_break", "5m_bounce_oversold"} or "engulfing" in h
    ]
    secondary = sum(
        1
        for cond in (
            bool(div),
            "oi_build" in triggers,
            any(str(t).startswith("broke_resistance") for t in triggers),
            any("ws_taker_buy" in str(t) for t in triggers),
            any("spot_lead_pump" in str(t) for t in triggers),
            lc_phase in {"impulse_initiating", "breakout_arming"},
            (lambda imb: isinstance(imb, (int, float)) and float(imb) >= 0.10)(
                _resolve_depth_imbalance(mkt)
            ),
        )
        if cond
    )
    closed_break = any("close_above_resistance" in h for h in structural)
    chg24 = float(long_setup.get("context_chg_24h_pct") or 0)
    pos_raw = long_setup.get("context_pos_in_range")
    if pos_raw is None:
        return False, ["veto_data_missing_pos_in_range"]
    try:
        pos_rng = float(pos_raw)
    except (TypeError, ValueError):
        return False, ["veto_data_missing_pos_in_range"]
    weak_acc = (
        lc_phase == "accumulation"
        and chg24 < -8.0
        and pos_rng < 0.45
    )
    ct = confirm_thresholds(symbol)
    secondary_min = int(ct.get("accumulation_secondary_min", 3)) if weak_acc else 2
    if lc_phase == "accumulation" and closed_break and len(structural) < 2:
        confirmed = fuel >= cal.confirm_min_score and secondary >= secondary_min
    else:
        confirmed = fuel >= cal.confirm_min_score and (
            len(structural) >= 2 or (closed_break and secondary >= secondary_min)
        )
    aligned, of_reason = _orderflow_confirm_aligned("long", mkt, symbol=symbol)
    if not aligned:
        return False, [f"veto_{of_reason}"]
    return confirmed, hard



def phase_long(long_setup: dict[str, Any], confirmed: bool, *, cal: HuntCalibratedParams) -> str:
    if confirmed:
        return "long_confirmed"
    fuel = float(long_setup.get("long_fuel") or 0)
    hard = long_setup.get("confirm_hard") or []
    has_initiation = any(h in _INITIATION_HARD_LONG for h in hard)
    if has_initiation and fuel >= cal.confirm_min_score:
        return "long_imminent"
    if has_initiation and fuel >= cal.forming_min_score:
        return "long_initiating"
    if fuel >= cal.forming_min_score:
        return "long_setup_forming"
    if fuel >= 25:
        return "accumulation_watch"
    return "no_long_yet"



def enrich_long_setup(
    setup: dict[str, Any],
    *,
    price: float = 0.0,
    tf: dict[str, Any] | None = None,
    market: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sym = str(setup.get("symbol") or "")
    sc = scoring_thresholds(sym)
    _apply_ema200_confluence(
        setup, direction="long", score_key="long_score", price=price, tf=tf, symbol=sym
    )
    _apply_squeeze_at_boundary(
        setup, direction="long", score_key="long_score", tf=tf, symbol=sym
    )
    _apply_hidden_div_fuel(setup, direction="long", score_key="long_score", tf=tf)
    _apply_chart_pattern_fuel(setup, direction="long", score_key="long_score", tf=tf)
    _apply_polars_ta_fuel(setup, direction="long", score_key="long_score", tf=tf)
    _apply_distribution_fuel(setup, direction="long", score_key="long_score", tf=tf)
    _apply_research_fuel(setup, direction="long", score_key="long_score", tf=tf)
    _apply_candle_pattern_fuel(
        setup, direction="long", score_key="long_score", tf=tf, price=price
    )
    _apply_ws_orderflow_fuel(setup, direction="long", score_key="long_score", market=market)
    level = float(
        setup.get("resistance_break_level")
        or setup.get("local_resistance")
        or 0
    )
    _apply_prokol_fuel_penalty(
        setup, direction="long", tf=tf, level=level
    )
    setup["long_fuel"] = compute_setup_fuel(setup, direction="long", symbol=sym, tf=tf)
    chg24 = setup.get("context_chg_24h_pct")
    pos = setup.get("context_pos_in_range")
    phase = str(setup.get("lifecycle_phase") or "")
    if (
        phase == "accumulation"
        and chg24 is not None
        and float(chg24) < -8.0
        and pos is not None
        and float(pos) < 0.45
    ):
        setup["long_fuel"] = round(
            min(float(setup["long_fuel"]), float(sc.get("accumulation_long_fuel_cap", 72.0))),
            1,
        )
    return setup


def evaluate_prepump(row: dict[str, Any], *, price: float, tf: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]:
    long = dict(row.get("long") or {})
    long = enrich_long_setup(long, price=price, tf=tf, market=market)
    sym = str(row.get("symbol") or "")
    cal = effective_hunt_params(sym)
    confirmed, _hard = confirm_long(long, tf=tf, market=market, symbol=sym, price=price, cal=cal)
    long["confirmed"] = confirmed
    long["phase"] = phase_long(long, confirmed, cal=cal)
    return long


__all__ = ["confirm_long", "enrich_long_setup", "evaluate_prepump", "phase_long"]
