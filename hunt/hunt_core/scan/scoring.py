"""Dump/long scoring + confirm wrappers (P2 extract from tick_assembly)."""
from __future__ import annotations

from typing import Any

from hunt_core.scan._confirm_shared import wall_depth_fuel_triggers
from hunt_core.scan.predump import confirm_dump as _se_confirm_dump, phase_dump as _se_phase_dump
from hunt_core.scan.prepump import confirm_long as _se_confirm_long, phase_long as _se_phase_long
from hunt_core.data.collect import SnapshotTier
from hunt_core.levels.levels import (
    build_liquidity_context,
    structural_long_levels,
    structural_short_levels,
)
from hunt_core.gate.delivery import directional_filters
from hunt_core.params.store import collect_thresholds, effective_hunt_params
from hunt_core.track.pump_history import score_bonus

def dump_analysis(
    *,
    symbol: str = "",
    price: float,
    tf: dict[str, Any],
    market: dict[str, Any],
    regime: dict[str, Any],
    impulse_high: float,
    impulse_low: float,
    support_break_level: float,
    fib: dict[str, float],
    prev_oi: float | None,
    cur_oi: float | None,
    local_support: float,
    local_resistance: float,
    lifecycle_phase: str = "",
    fall_from_high_pct: float = 0.0,
    pos_in_range: float = 0.5,
    range_pct_24h: float = 0.0,
    leg_gain_pct: float = 0.0,
    pump_stats: dict[str, Any] | None = None,
    tier: SnapshotTier = "full",
    book_walls: dict[str, Any] | None = None,
    cross_microstructure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    triggers: list[str] = []
    score = 0.0
    th = collect_thresholds(symbol)
    # Closed bars only on HTF — live 1h/4h RSI repaints intra-bar (methodology:
    # closed-only). Divergence flags are pivot-based, identical on live/closed.
    r15, r1, r5, r1h, r4h = (
        tf.get("15m_closed") or tf.get("15m", {}),
        tf.get("1m_closed") or tf.get("1m", {}),
        tf.get("5m_closed") or tf.get("5m", {}),
        tf.get("1h_closed") or tf.get("1h", {}),
        tf.get("4h_closed") or tf.get("4h", {}),
    )

    if r15.get("rsi14", 0) >= th.get("rsi15_overbought", 72):
        score += 12
        triggers.append("rsi15_overbought")
    if r1h.get("rsi14", 0) >= th.get("rsi1h_overbought", 72):
        score += 10
        triggers.append("rsi1h_overbought")
    if r4h.get("bearish_rsi_div"):
        score += 15
        triggers.append("bear_div_4h")
    if r1h.get("bearish_rsi_div"):
        score += 12
        triggers.append("bear_div_1h")
    if r1h.get("rsi_trendline_bearish_break"):
        score += 8
        triggers.append("rsi_trendline_bearish_break_1h")
    if r4h.get("bearish_macd_div"):
        score += 8
        triggers.append("macd_div_4h")
    if r1h.get("bearish_macd_div"):
        score += 6
        triggers.append("macd_div_1h")
    poc_dir = regime.get("poc_direction_15m") or regime.get("poc_direction_1h")
    if poc_dir == "short":
        score += 10
        triggers.append("poc_aligned")
    elif poc_dir == "long":
        score -= 8
        triggers.append("poc_contra")

    c1, c5, c15 = r1.get("candle", {}), r5.get("candle", {}), r15.get("candle", {})
    if c1.get("bearish") and c1.get("upper_wick_ratio", 0) >= 0.4:
        score += 16
        triggers.append("1m_rejection")
    if c5.get("bearish") and c5.get("upper_wick_ratio", 0) >= 0.35:
        score += 14
        triggers.append("5m_rejection")
    if c15.get("upper_wick_ratio", 0) >= 0.5 and not c15.get("bullish", True):
        score += 10
        triggers.append("15m_rejection_wick")

    for tf_key, pts in (("15m_closed", 22), ("1h_closed", 18)):
        pp_blk = tf.get(tf_key) or {}
        if pp_blk.get("pp_short_true"):
            score += pts
            triggers.append(f"pp_short_break_{tf_key}")
        elif pp_blk.get("pp_short_early"):
            score += max(8, pts // 2)
            triggers.append(f"pp_short_early_{tf_key}")

    if price >= fib.get("ext_1272", 0) * 0.985:
        score += 10
        triggers.append("at_fib_1272")
    if price > impulse_high * (1.0 + th.get("extended_above_impulse_pct", 3.0) / 100.0):
        score += 8
        triggers.append("extended_above_impulse_high")

    support_trigger = round(support_break_level, 6)
    r5_live = tf.get("5m_closed", {}).get("close") or price
    if support_trigger and r5_live < support_trigger:
        score += th.get("structure_break_score", 28)
        triggers.append(f"lost_support_{support_trigger}")
    elif impulse_high and r5_live < round(impulse_high * 0.998, 6):
        score += 12
        triggers.append(f"below_impulse_high_{round(impulse_high * 0.998, 6)}")

    # Mid-dump continuation (JCT -21% lesson): top-biased triggers go quiet while the
    # dump keeps printing lower closes — credit fresh structural weakness instead.
    if lifecycle_phase == "dump_active":
        if fall_from_high_pct >= 12.0:
            score += 14
            triggers.append("dump_continuation")
        if c5.get("bearish") and c15.get("bearish"):
            score += 10
            triggers.append("bear_momentum_5m_15m")
        if 0 < (r15.get("rsi14") or 0) <= 45:
            score += 8
            triggers.append("rsi15_bear_regime")

    taker = market.get("taker_5m") or market.get("taker_1h")
    if taker is not None and taker < th.get("taker_sell_max", 0.98):
        score += 10
        triggers.append("taker_sell_pressure")
    if prev_oi and cur_oi and cur_oi < prev_oi * th.get("oi_flush_ratio", 0.997):
        score += 10
        triggers.append("oi_flush")
    micro = market.get("microprice_bias")
    if micro is not None and micro < th.get("microprice_sell_max", -0.05):
        score += 8
        triggers.append("microprice_sell_bias")
    if regime.get("regime_4h") == "downtrend":
        score += 8
        triggers.append("regime_4h_bear")
    fund = market.get("funding_pct")
    if fund is not None and fund > th.get("funding_crowded_long_pct", 0.05):
        score += 6
        triggers.append("crowded_long_funding")
    # WS smoothed basis premium — overheated perp favors fade short (Q02).
    basis_ap = market.get("basis_ap_bps")
    if basis_ap is not None and float(basis_ap) >= th.get("basis_ap_premium_bps", 100.0):
        score += 8
        triggers.append(f"basis_ap_premium_{float(basis_ap):.0f}bps")

    # Series fuel (hunt-v3 item 6): OI flush vs own 4h distribution beats the
    # single 2-point delta; crowded longs (BLESS global L/S 2.06) feed the dump.
    oi_z = market.get("oi_z")
    if oi_z is not None and oi_z <= -1.5:
        score += 8
        triggers.append(f"oi_flush_z{oi_z}")
    gls_z = market.get("gls_z")
    gls = market.get("global_ls_5m") or market.get("global_ls_1h")
    if gls_z is not None and gls_z >= 1.5:
        score += 8
        triggers.append(f"crowded_longs_z{gls_z}")
    elif gls is not None and float(gls) >= 2.0:
        score += 6
        triggers.append("global_ls_extreme_long")

    # Live WS: liquidation cascades + sub-minute taker flow (no REST equivalent).
    liq_score = market.get("liquidation_score_5m")
    if liq_score is not None and float(liq_score) <= 0.30:
        score += 12
        triggers.append(f"ws_liq_cascade_{float(liq_score):.2f}")
        if fall_from_high_pct >= 5.0:
            score += 15
            triggers.append("long_squeeze_cascade")
    liq_n = market.get("liq_events_5m")
    if liq_n is not None and int(liq_n) >= 8:
        score += 6
        triggers.append(f"ws_liq_storm_{liq_n}")
    ws_agg = market.get("agg_trade_delta_30s")
    if ws_agg is not None and float(ws_agg) < 0.42:
        score += 6
        triggers.append("ws_taker_sell_30s")
    ws_agg60 = market.get("agg_trade_delta_60s")
    if ws_agg60 is not None and float(ws_agg60) <= 0.42:
        score += 8
        triggers.append("ws_taker_sell_60s")
    spot_lead = market.get("spot_lead_return_1m")
    if spot_lead is not None and float(spot_lead) <= -0.4:
        score += 8
        triggers.append(f"spot_lead_dump_{float(spot_lead):.2f}")
    nearest_long_liq = market.get("liq_heatmap_nearest_long")
    if nearest_long_liq and price > 0:
        dist = abs(float(nearest_long_liq) - price) / price * 100.0
        if dist <= 1.0:
            score += 10
            triggers.append("liq_cluster_nearby")
    cascade = market.get("liq_cascade_risk")
    if cascade == "long_flush":
        score += 8
        triggers.append("liq_cascade_aligned")

    wall_score, wall_triggers = wall_depth_fuel_triggers(market, direction="short", price=price, symbol=symbol)
    score += wall_score
    triggers.extend(wall_triggers)

    hist_bonus, hist_flags = score_bonus(pump_stats, watch_bias="short")
    if hist_bonus:
        score += hist_bonus
        triggers.extend(hist_flags)

    flt_delta, flt_triggers, flt_blocks = directional_filters(
        tf,
        direction="short",
        pos_in_range=pos_in_range,
        symbol=symbol,
        lifecycle_phase=lifecycle_phase,
        fall_from_high_pct=fall_from_high_pct,
    )
    score = max(0.0, score + flt_delta)
    triggers.extend(flt_triggers)

    # Strict ATR: missing 15m ATR vetoes the setup inside levels (no synthetic fallback).
    atr15 = float(r15.get("atr14") or 0)
    atr1h_raw = float(r1h.get("atr14") or 0)
    atr1h = atr1h_raw if atr1h_raw > 0 else None
    residual_vol = r15.get("residual_vol")
    if residual_vol is not None and atr15 > 0:
        try:
            rv = float(residual_vol)
            if rv > 0:
                atr15 = max(atr15, rv)
        except (TypeError, ValueError):
            pass
    liq_ctx = build_liquidity_context(
        price=price,
        regime=regime,
        book_walls=book_walls,
        cross_micro=cross_microstructure,
        tf_15m=r15,
        tf_1d=tf.get("1d_closed") or tf.get("1d"),
    )
    levels = structural_short_levels(
        price=price,
        impulse_high=impulse_high,
        impulse_low=impulse_low,
        fib=fib,
        atr15=atr15,
        atr1h=atr1h,
        local_support=local_support,
        local_resistance=local_resistance,
        range_pct_24h=range_pct_24h,
        leg_gain_pct=leg_gain_pct,
        fall_from_high_pct=fall_from_high_pct,
        symbol=symbol,
        lifecycle_phase=lifecycle_phase,
        liquidity=liq_ctx,
        poc_direction=str((regime or {}).get("poc_direction_1h") or ""),
    )

    return {
        "dump_score": round(score, 1),
        "triggers": triggers,
        "filter_blocks": flt_blocks,
        "levels_viable": levels.get("viable", True),
        "levels_veto": levels.get("veto") or [],
        "support_break_level": support_trigger,
        "fib_1272": fib.get("ext_1272"),
        "resistance_liq": fib.get("ext_1272"),  # legacy alias — not liquidation heatmap
        "entry_zone": levels["entry_zone"],
        "stop_loss": levels["stop_loss"],
        "tp1": levels["tp1"],
        "tp2": levels["tp2"],
        "tp1_label": levels.get("tp1_label", ""),
        "tp2_label": levels.get("tp2_label", ""),
        "level_mode": levels.get("level_mode", ""),
        "risk_reward": levels.get("risk_reward"),
        "sl_dist_pct": levels.get("sl_dist_pct"),
        "tp2_dist_pct": levels.get("tp2_dist_pct"),
        "invalidation_above": levels["invalidation_above"],
    }


def confirm_dump(
    dump: dict[str, Any],
    tf: dict[str, Any],
    *,
    symbol: str = "",
    price: float = 0.0,
    market: dict[str, Any] | None = None,
    lifecycle_bias: str = "",
) -> tuple[bool, list[str]]:
    return _se_confirm_dump(
        dump,
        tf,
        symbol=symbol,
        price=price,
        market=market,
        cal=effective_hunt_params(symbol),
        lifecycle_bias=lifecycle_bias,
    )


def phase_dump(dump: dict[str, Any], confirmed: bool, *, symbol: str = "", lifecycle_note: str | None = None) -> str:
    return _se_phase_dump(
        dump,
        confirmed,
        lifecycle_note=lifecycle_note,
        cal=effective_hunt_params(symbol),
    )


def phase_long(long_setup: dict[str, Any], confirmed: bool, *, symbol: str = "") -> str:
    return _se_phase_long(long_setup, confirmed, cal=effective_hunt_params(symbol))


def long_analysis(
    *,
    symbol: str = "",
    price: float,
    tf: dict[str, Any],
    market: dict[str, Any],
    regime: dict[str, Any],
    impulse_low: float,
    impulse_high: float,
    fib: dict[str, float],
    prev_oi: float | None,
    cur_oi: float | None,
    lifecycle_phase: str | None = None,
    fall_from_high_pct: float = 0.0,
    pos_in_range: float = 0.5,
    range_pct_24h: float = 0.0,
    leg_gain_pct: float = 0.0,
    pump_stats: dict[str, Any] | None = None,
    chg_24h_pct: float = 0.0,
    book_walls: dict[str, Any] | None = None,
    cross_microstructure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    triggers: list[str] = []
    score = 0.0
    th = collect_thresholds(symbol)
    # Closed bars only on HTF — live 1h/4h RSI repaints intra-bar (methodology:
    # closed-only). Divergence flags are pivot-based, identical on live/closed.
    r15, r1, r5, r1h, r4h = (
        tf.get("15m_closed") or tf.get("15m", {}),
        tf.get("1m_closed") or tf.get("1m", {}),
        tf.get("5m_closed") or tf.get("5m", {}),
        tf.get("1h_closed") or tf.get("1h", {}),
        tf.get("4h_closed") or tf.get("4h", {}),
    )
    if r15.get("rsi14", 50) <= th.get("rsi15_oversold", 32):
        score += 12
        triggers.append("rsi15_oversold")
    if r1h.get("rsi14", 50) <= th.get("rsi1h_oversold", 35):
        score += 10
        triggers.append("rsi1h_oversold")
    if r4h.get("bullish_rsi_div"):
        score += 15
        triggers.append("bull_div_4h")
    if r1h.get("bullish_rsi_div"):
        score += 12
        triggers.append("bull_div_1h")
    if r1h.get("rsi_trendline_bullish_break"):
        score += 8
        triggers.append("rsi_trendline_bullish_break_1h")
    if r4h.get("bullish_macd_div"):
        score += 8
        triggers.append("macd_div_4h")
    if r1h.get("bullish_macd_div"):
        score += 6
        triggers.append("macd_div_1h")
    poc_dir = regime.get("poc_direction_15m") or regime.get("poc_direction_1h")
    if poc_dir == "long":
        score += 10
        triggers.append("poc_aligned")
    elif poc_dir == "short":
        score -= 8
        triggers.append("poc_contra")

    c1, c5, c15 = r1.get("candle", {}), r5.get("candle", {}), r15.get("candle", {})
    if c1.get("bullish") and c1.get("lower_wick_ratio", 0) >= 0.4:
        score += 16
        triggers.append("1m_bounce")
    if c5.get("bullish") and c5.get("lower_wick_ratio", 0) >= 0.35:
        score += 14
        triggers.append("5m_bounce")
    if c15.get("lower_wick_ratio", 0) >= 0.5 and c15.get("bullish"):
        score += 10
        triggers.append("15m_bounce_wick")

    for tf_key, pts in (("15m_closed", 22), ("1h_closed", 18)):
        pp_blk = tf.get(tf_key) or {}
        if pp_blk.get("pp_long_true"):
            score += pts
            triggers.append(f"pp_long_break_{tf_key}")
        elif pp_blk.get("pp_long_early"):
            score += max(8, pts // 2)
            triggers.append(f"pp_long_early_{tf_key}")

    support_zone = fib.get("ret_382") or impulse_low
    if support_zone and price <= support_zone * 1.015:
        score += 10
        triggers.append("at_fib_support")
    if price < impulse_low * (1.0 - th.get("extended_below_impulse_pct", 3.0) / 100.0):
        score += 8
        triggers.append("deep_below_impulse_low")

    resistance_break = round(impulse_high * 0.998, 6)
    r5_closed = float((tf.get("5m_closed") or {}).get("close") or 0)
    if r5_closed > resistance_break:
        score += th.get("structure_break_score", 28)
        triggers.append(f"broke_resistance_{resistance_break}")
    elif price > resistance_break and r5_closed > 0:
        score += 8
        triggers.append("live_above_resistance_unconfirmed")

    taker = market.get("taker_5m") or market.get("taker_1h")
    if taker is not None and taker > th.get("taker_buy_min", 1.02):
        score += 10
        triggers.append("taker_buy_pressure")
    if prev_oi and cur_oi and cur_oi > prev_oi * th.get("oi_build_ratio", 1.003):
        score += 10
        triggers.append("oi_build")
    micro = market.get("microprice_bias")
    if micro is not None and micro > th.get("microprice_buy_min", 0.05):
        score += 8
        triggers.append("microprice_buy_bias")
    if regime.get("regime_4h") == "uptrend":
        score += 8
        triggers.append("regime_4h_bull")
    fund = market.get("funding_pct")
    if fund is not None and fund < th.get("funding_crowded_short_pct", -0.02):
        score += 6
        triggers.append("crowded_short_funding")
    # Smoothed basis (ap − index) — report Q02; prefer over raw mark−index for scoring.
    basis_ap = market.get("basis_ap_bps")
    if basis_ap is not None and float(basis_ap) <= th.get("basis_ap_discount_bps", -80.0):
        score += 6
        triggers.append(f"basis_ap_discount_{float(basis_ap):.0f}bps")
    drop_from_high = (impulse_high - price) / impulse_high if impulse_high else 0.0
    still_below_structure = impulse_high > 0 and price < impulse_high * 0.92
    if drop_from_high >= 0.08 and r15.get("rsi14", 50) <= 38:
        if still_below_structure:
            score += 6
            triggers.append("post_dump_oversold_watch_only")
        else:
            score += 18
            triggers.append("post_dump_oversold_bounce")
    if drop_from_high >= 0.12 and c5.get("bullish") and c5.get("lower_wick_ratio", 0) >= 0.3:
        if still_below_structure:
            score += 4
            triggers.append("capitulation_wick_watch")
        else:
            score += 14
            triggers.append("capitulation_wick")
    if lifecycle_phase == "dump_active" or (
        still_below_structure and max(drop_from_high * 100.0, fall_from_high_pct) >= 12.0
    ):
        cap = 42.0 if lifecycle_phase == "dump_active" else 48.0
        if score > cap:
            score = cap
            triggers.append("mid_dump_long_cap")

    # Series fuel (item 6): OI build with crowd short = short-squeeze powder.
    oi_z = market.get("oi_z")
    if oi_z is not None and oi_z >= 1.5:
        score += 8
        triggers.append(f"oi_build_z{oi_z}")
    gls_z = market.get("gls_z")
    gls = market.get("global_ls_5m") or market.get("global_ls_1h")
    if gls_z is not None and gls_z <= -1.5:
        score += 8
        triggers.append(f"crowded_shorts_z{gls_z}")
    elif gls is not None and 0 < float(gls) <= 0.5:
        score += 6
        triggers.append("global_ls_extreme_short")

    liq_score = market.get("liquidation_score_5m")
    if liq_score is not None and float(liq_score) >= 0.70:
        score += 12
        triggers.append(f"ws_liq_squeeze_{float(liq_score):.2f}")
    liq_n = market.get("liq_events_5m")
    if liq_n is not None and int(liq_n) >= 8:
        score += 6
        triggers.append(f"ws_liq_storm_{liq_n}")
    ws_agg = market.get("agg_trade_delta_30s")
    if ws_agg is not None and float(ws_agg) > 0.58:
        score += 6
        triggers.append("ws_taker_buy_30s")
    ws_agg60 = market.get("agg_trade_delta_60s")
    if ws_agg60 is not None and float(ws_agg60) >= 0.58:
        score += 8
        triggers.append("ws_taker_buy_60s")
    spot_lead = market.get("spot_lead_return_1m")
    if spot_lead is not None and float(spot_lead) >= 0.4:
        score += 8
        triggers.append(f"spot_lead_pump_{float(spot_lead):.2f}")

    if lifecycle_phase in ("impulse_initiating", "breakout_arming"):
        score += 14
        triggers.append(f"initial_pump_{lifecycle_phase}")
    if lifecycle_phase == "impulse_initiating" and leg_gain_pct >= 25.0 and pos_in_range >= 0.45:
        score += 10
        triggers.append(f"leg_gain_impulse_{leg_gain_pct:.0f}")

    wall_score, wall_triggers = wall_depth_fuel_triggers(market, direction="long", price=price, symbol=symbol)
    score += wall_score
    triggers.extend(wall_triggers)

    hist_bonus, hist_flags = score_bonus(pump_stats, watch_bias="long")
    if hist_bonus:
        score += hist_bonus
        triggers.extend(hist_flags)

    flt_delta, flt_triggers, flt_blocks = directional_filters(
        tf,
        direction="long",
        pos_in_range=pos_in_range,
        symbol=symbol,
        lifecycle_phase=lifecycle_phase or "",
        fall_from_high_pct=fall_from_high_pct,
        chg_24h_pct=chg_24h_pct,
    )
    score = max(0.0, score + flt_delta)
    triggers.extend(flt_triggers)

    atr15 = float(r15.get("atr14") or 0)
    atr1h_raw = float(r1h.get("atr14") or 0)
    atr1h = atr1h_raw if atr1h_raw > 0 else None
    residual_vol = r15.get("residual_vol")
    if residual_vol is not None and atr15 > 0:
        try:
            rv = float(residual_vol)
            if rv > 0:
                atr15 = max(atr15, rv)
        except (TypeError, ValueError):
            pass
    liq_ctx = build_liquidity_context(
        price=price,
        regime=regime,
        book_walls=book_walls,
        cross_micro=cross_microstructure,
        tf_15m=r15,
        tf_1d=tf.get("1d_closed") or tf.get("1d"),
    )
    levels = structural_long_levels(
        price=price,
        impulse_high=impulse_high,
        impulse_low=impulse_low,
        fib=fib,
        atr15=atr15,
        atr1h=atr1h,
        local_support=support_zone or impulse_low,
        local_resistance=resistance_break,
        range_pct_24h=range_pct_24h,
        leg_gain_pct=leg_gain_pct,
        fall_from_high_pct=fall_from_high_pct,
        symbol=symbol,
        lifecycle_phase=lifecycle_phase,
        liquidity=liq_ctx,
    )

    return {
        "long_score": round(score, 1),
        "triggers": triggers,
        "filter_blocks": flt_blocks,
        "levels_viable": levels.get("viable", True),
        "levels_veto": levels.get("veto") or [],
        "resistance_break_level": resistance_break,
        "support_zone": support_zone,
        "entry_zone": levels["entry_zone"],
        "stop_loss": levels["stop_loss"],
        "tp1": levels["tp1"],
        "tp2": levels["tp2"],
        "tp1_label": levels.get("tp1_label", ""),
        "tp2_label": levels.get("tp2_label", ""),
        "level_mode": levels.get("level_mode", ""),
        "risk_reward": levels.get("risk_reward"),
        "sl_dist_pct": levels.get("sl_dist_pct"),
        "tp2_dist_pct": levels.get("tp2_dist_pct"),
        "invalidation_below": levels["invalidation_below"],
        "context_chg_24h_pct": round(float(chg_24h_pct), 2),
        "context_pos_in_range": round(float(pos_in_range), 3),
        "lifecycle_phase": lifecycle_phase,
    }


def confirm_long(
    long_setup: dict[str, Any],
    tf: dict[str, Any],
    *,
    symbol: str = "",
    price: float = 0.0,
    market: dict[str, Any] | None = None,
    lifecycle_bias: str = "",
    lifecycle_phase: str = "",
) -> tuple[bool, list[str]]:
    return _se_confirm_long(
        long_setup,
        tf,
        symbol=symbol,
        price=price,
        market=market,
        cal=effective_hunt_params(symbol),
        lifecycle_bias=lifecycle_bias,
        lifecycle_phase=lifecycle_phase,
    )
