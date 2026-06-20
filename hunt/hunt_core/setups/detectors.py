"""Hunt setup detectors — catalog implementations."""
from __future__ import annotations



# --- merged from dump_initiation.py ---
from typing import Any

from hunt_core.setups.catalog import SetupEvidence, atr_from_tf, confirm_tf_chain
from hunt_core.params.store import scoring_thresholds


def _cex_atomic_metrics(
    row: dict[str, Any],
    prepared: dict[str, Any],
) -> tuple[float, float, float | None]:
    tf = prepared.get("timeframes") if isinstance(prepared.get("timeframes"), dict) else {}
    market = row.get("market") or prepared.get("market") or {}
    r1 = tf.get("1m_closed") or tf.get("1m") or {}
    candle = r1.get("candle") or {}
    o = float(candle.get("open") or 0)
    c = float(candle.get("close") or r1.get("close") or 0)
    if o > 0:
        ret_1m = (c - o) / o
    else:
        ret_1m = 0.0
    z_vol = float(r1.get("vol_ratio") or (tf.get("15m_closed") or {}).get("vol_ratio") or 0)
    buy_share = market.get("agg_trade_buy_ratio_60s") or market.get("agg_trade_buy_ratio_30s")
    if buy_share is None:
        buy_share = market.get("agg_trade_delta_60s") or market.get("agg_trade_delta_30s")
    try:
        buy_f = float(buy_share) if buy_share is not None else None
    except (TypeError, ValueError):
        buy_f = None
    return ret_1m, z_vol, buy_f


def detect_btc_decoupled(
    row: dict[str, Any],
    prepared: dict[str, Any],
) -> SetupEvidence | None:
    """BTC-residual changepoint — alt moving independently of beta-adjusted BTC."""
    market = row.get("market") or prepared.get("market") or {}
    pump = bool(market.get("btc_decoupled_pump"))
    dump = bool(market.get("btc_decoupled_dump"))
    if not pump and not dump:
        return None
    direction = "long" if pump else "short"
    price = float(row.get("price") or prepared.get("price") or 0)
    tf = prepared.get("timeframes") if isinstance(prepared.get("timeframes"), dict) else {}
    atr = atr_from_tf(tf) or (price * 0.02 if price > 0 else 0.01)
    if direction == "long":
        sl = price - atr * 1.5 if price > 0 else 0.0
        tp1 = price + atr * 2.5 if price > 0 else 0.0
        tp2 = price + atr * 4.0 if price > 0 else 0.0
        reason = "btc_decoupled_pump"
    else:
        sl = price + atr * 1.5 if price > 0 else 0.0
        tp1 = price - atr * 2.5 if price > 0 else 0.0
        tp2 = price - atr * 4.0 if price > 0 else 0.0
        reason = "btc_decoupled_dump"
    pc = prepared.get("pump_cycle") if isinstance(prepared.get("pump_cycle"), dict) else {}
    strength = 0.55
    if dump and str(pc.get("phase") or "") == "peak":
        strength += 0.12
    return SetupEvidence(
        setup_id="btc_decoupled",
        direction=direction,
        strength=min(1.0, strength),
        confirmed=True,
        reasons=(reason,),
        entry=price,
        stop_loss=sl,
        tp1=tp1,
        tp2=tp2,
    )


def detect_cex_pump(
    row: dict[str, Any],
    prepared: dict[str, Any],
) -> SetupEvidence | None:
    th = scoring_thresholds(str(row.get("symbol") or prepared.get("symbol") or ""))
    ret_min = float(th.get("cex_pump_ret_1m_min", 0.02))
    z_min = float(th.get("cex_z_vol_30m_min", 3.0))
    buy_min = float(th.get("cex_pump_buy_share_min", 0.65))
    ret_1m, z_vol, buy_share = _cex_atomic_metrics(row, prepared)
    if ret_1m < ret_min or z_vol < z_min or buy_share is None or buy_share < buy_min:
        return None
    price = float(row.get("price") or prepared.get("price") or 0)
    tf = prepared.get("timeframes") if isinstance(prepared.get("timeframes"), dict) else {}
    atr = atr_from_tf(tf) or (price * 0.02 if price > 0 else 0.01)
    sl = price - atr * 1.5 if price > 0 else 0.0
    tp1 = price + atr * 2.5 if price > 0 else 0.0
    tp2 = price + atr * 4.0 if price > 0 else 0.0
    strength = min(
        1.0,
        0.45 + min(0.25, ret_1m / max(ret_min, 1e-6) * 0.08) + min(0.20, (z_vol - z_min) * 0.04),
    )
    reasons = (
        f"ret_1m={ret_1m:.3f}",
        f"z_vol={z_vol:.1f}",
        f"buy_share={buy_share:.2f}",
    )
    return SetupEvidence(
        setup_id="cex_pump",
        direction="long",
        strength=strength,
        confirmed=True,
        reasons=reasons,
        entry=price,
        stop_loss=sl,
        tp1=tp1,
        tp2=tp2,
    )


def detect_cex_dump(
    row: dict[str, Any],
    prepared: dict[str, Any],
) -> SetupEvidence | None:
    th = scoring_thresholds(str(row.get("symbol") or prepared.get("symbol") or ""))
    ret_max = float(th.get("cex_dump_ret_1m_max", -0.02))
    z_min = float(th.get("cex_z_vol_30m_min", 3.0))
    buy_max = float(th.get("cex_dump_buy_share_max", 0.35))
    ret_1m, z_vol, buy_share = _cex_atomic_metrics(row, prepared)
    if ret_1m > ret_max or z_vol < z_min or buy_share is None or buy_share > buy_max:
        return None
    price = float(row.get("price") or prepared.get("price") or 0)
    tf = prepared.get("timeframes") if isinstance(prepared.get("timeframes"), dict) else {}
    atr = atr_from_tf(tf) or (price * 0.02 if price > 0 else 0.01)
    sl = price + atr * 1.5 if price > 0 else 0.0
    tp1 = price - atr * 2.5 if price > 0 else 0.0
    tp2 = price - atr * 4.0 if price > 0 else 0.0
    strength = min(
        1.0,
        0.45 + min(0.25, abs(ret_1m) / max(abs(ret_max), 1e-6) * 0.08) + min(0.20, (z_vol - z_min) * 0.04),
    )
    reasons = (
        f"ret_1m={ret_1m:.3f}",
        f"z_vol={z_vol:.1f}",
        f"buy_share={buy_share:.2f}",
    )
    return SetupEvidence(
        setup_id="cex_dump",
        direction="short",
        strength=strength,
        confirmed=True,
        reasons=reasons,
        entry=price,
        stop_loss=sl,
        tp1=tp1,
        tp2=tp2,
    )


def detect_dump_initiation(
    row: dict[str, Any],
    prepared: dict[str, Any],
) -> SetupEvidence | None:
    tf = prepared.get("timeframes") if isinstance(prepared.get("timeframes"), dict) else {}
    closed = confirm_tf_chain(tf, "5m_closed", "15m_closed")
    if closed is None:
        return None
    block, close = closed

    lc = row.get("lifecycle") or prepared.get("lifecycle") or {}
    phase = str(lc.get("phase") or "")
    fall = float(lc.get("fall_from_high_pct") or 0)
    pc = prepared.get("pump_cycle") if isinstance(prepared.get("pump_cycle"), dict) else {}
    pc_phase = str(pc.get("phase") or "")
    if pc_phase == "peak" and fall >= 8.0:
        return None
    price = float(row.get("price") or prepared.get("price") or close)
    dump = row.get("dump") or prepared.get("dump") or {}
    support = float(dump.get("support_break_level") or lc.get("local_support") or 0)
    rsi = float(block.get("rsi14") or 0)
    bearish = bool((block.get("candle") or {}).get("bearish"))
    struct = prepared.get("structure") if isinstance(prepared.get("structure"), dict) else {}
    candle = block.get("candle") or {}
    wick_ratio = float(candle.get("upper_wick_ratio") or 0)

    reasons: list[str] = []
    strength = 0.0
    if pc_phase == "start" and fall <= 5.0:
        strength += 0.15
        reasons.append("pump_cycle_start")
    if phase in {"exhaustion_at_high", "distribution", "dump_initiating"}:
        strength += 0.28
        reasons.append(f"phase={phase}")
    if fall <= 5.0:
        strength += 0.18
        reasons.append(f"pre_dump_fall={fall:.1f}%")
    if support > 0 and close < support:
        strength += 0.30
        reasons.append("close_below_support")
    elif support > 0 and close >= support * 0.998:
        if bearish and wick_ratio >= 0.35:
            strength += 0.20
            reasons.append("distribution_rejection_wick")
        pp_short = struct.get("pp_short") if isinstance(struct.get("pp_short"), dict) else {}
        if pp_short.get("pp_short_early"):
            strength += 0.22
            reasons.append("pp_short_early")
        if struct.get("setup_type") == "sweep_reclaim" or "sweep" in str(
            struct.get("event") or ""
        ).lower():
            strength += 0.18
            reasons.append("sweep_reclaim_forming")
    if rsi >= 65.0:
        strength += 0.12
        reasons.append(f"rsi_ob={rsi:.0f}")
    if bearish:
        strength += 0.12
        reasons.append("bearish_closed_bar")

    if strength < 0.45:
        return None

    forming_confirmed = strength >= 0.50 and (
        "close_below_support" in reasons
        or "distribution_rejection_wick" in reasons
        or "pp_short_early" in reasons
        or "sweep_reclaim_forming" in reasons
    )
    confirmed = strength >= 0.55 or forming_confirmed

    atr = atr_from_tf(tf) or price * 0.02
    sl = max(support, price) + atr * 1.5 if support > 0 else price + atr * 2.0
    tp1 = price - atr * 2.5
    tp2 = price - atr * 4.0
    return SetupEvidence(
        setup_id="dump_initiation",
        direction="short",
        strength=min(1.0, strength),
        confirmed=confirmed,
        reasons=tuple(reasons[:6]),
        entry=price,
        stop_loss=sl,
        tp1=tp1,
        tp2=tp2,
    )

# --- merged from squeeze_expansion.py ---



def detect_squeeze_expansion(
    row: dict[str, Any],
    prepared: dict[str, Any],
) -> SetupEvidence | None:
    tf = prepared.get("timeframes") if isinstance(prepared.get("timeframes"), dict) else {}
    closed = confirm_tf_chain(tf, "15m_closed", "5m_closed")
    if closed is None:
        return None
    block, close = closed

    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    if not isinstance(prepared.get("market"), dict):
        prepared_market = market
    else:
        prepared_market = prepared.get("market") or market
    try:
        map_acc = float(prepared_market.get("map_vp_accumulation") or 0)
    except (TypeError, ValueError):
        map_acc = 0.0
    map_pre_pump = map_acc >= 0.50 and (
        prepared_market.get("map_accum_bid_absorption")
        or prepared_market.get("map_void_above")
    )

    squeeze_on = bool(block.get("squeeze_on"))
    bb_pctile = block.get("bb_width_pctile")
    bb_tight = bb_pctile is not None and float(bb_pctile) <= 0.30
    if not squeeze_on and not bb_tight and not map_pre_pump:
        return None

    atr_pct = float(block.get("atr_pct") or 0)
    candle = block.get("candle") or {}
    body = abs(float(candle.get("close") or close) - float(candle.get("open") or close))
    atr = atr_from_tf(tf) or close * 0.02
    range_pct = (body / close * 100.0) if close > 0 else 0.0

    lc = row.get("lifecycle") or prepared.get("lifecycle") or {}
    phase = str(lc.get("phase") or "")
    early_phase = phase in {"accumulation", "breakout_arming"}

    if map_pre_pump and early_phase and range_pct < max(0.35, atr_pct * 0.15):
        direction = "long"
        reasons = ["map_pre_pump_coil", f"map_vp_acc={map_acc:.2f}"]
        if prepared_market.get("map_void_above"):
            reasons.append("map_void_above")
        strength = 0.52 + min(0.30, map_acc * 0.35)
        sl = close - atr * 1.6
        void_above = prepared_market.get("map_void_above")
        liq_target = prepared_market.get("liq_heatmap_nearest_short")
        tp_magnet = void_above or liq_target
        if tp_magnet is not None:
            try:
                tp1 = float(tp_magnet)
            except (TypeError, ValueError):
                tp1 = close + atr * 2.8
        else:
            tp1 = close + atr * 2.8
        tp2 = close + atr * 4.5
        return SetupEvidence(
            setup_id="squeeze_expansion",
            direction=direction,
            strength=min(1.0, strength),
            confirmed=False,
            reasons=tuple(reasons),
            entry=close,
            stop_loss=sl,
            tp1=tp1,
            tp2=tp2,
        )

    if range_pct < max(0.35, atr_pct * 0.15):
        return None

    bullish = bool(candle.get("bullish"))
    bearish = bool(candle.get("bearish"))
    if not bullish and not bearish:
        return None

    direction = "long" if bullish else "short"
    reasons = ["squeeze_release", f"bar_move={range_pct:.2f}%"]
    strength = 0.55 + min(0.25, range_pct / 2.0)

    if direction == "long":
        sl = close - atr * 1.8
        tp1 = close + atr * 2.8
        tp2 = close + atr * 4.5
    else:
        sl = close + atr * 1.8
        tp1 = close - atr * 2.8
        tp2 = close - atr * 4.5

    return SetupEvidence(
        setup_id="squeeze_expansion",
        direction=direction,
        strength=min(1.0, strength),
        confirmed=True,
        reasons=tuple(reasons),
        entry=close,
        stop_loss=sl,
        tp1=tp1,
        tp2=tp2,
    )

# --- merged from liquidity_sweep.py ---



def detect_liquidity_sweep(
    row: dict[str, Any],
    prepared: dict[str, Any],
) -> SetupEvidence | None:
    tf = prepared.get("timeframes") if isinstance(prepared.get("timeframes"), dict) else {}
    closed = confirm_tf_chain(tf, "15m_closed", "5m_closed")
    if closed is None:
        return None
    block, close = closed

    candle = block.get("candle") or {}
    upper_wick = float(candle.get("upper_wick_ratio") or 0)
    lower_wick = float(candle.get("lower_wick_ratio") or 0)
    lc = row.get("lifecycle") or prepared.get("lifecycle") or {}
    support = float(lc.get("local_support") or 0)
    resistance = float(lc.get("local_resistance") or 0)
    price = float(row.get("price") or prepared.get("price") or close)
    atr = atr_from_tf(tf) or price * 0.02

    reasons: list[str] = []
    direction = None
    strength = 0.0

    if lower_wick >= 0.35 and support > 0 and close > support:
        direction = "long"
        strength = 0.50 + min(0.30, lower_wick)
        reasons.extend(["sweep_low", f"reclaim_above={support:.6f}", f"wick={lower_wick:.2f}"])
    elif upper_wick >= 0.35 and resistance > 0 and close < resistance:
        direction = "short"
        strength = 0.50 + min(0.30, upper_wick)
        reasons.extend(["sweep_high", f"fail_below={resistance:.6f}", f"wick={upper_wick:.2f}"])

    if direction is None or strength < 0.55:
        return None

    if direction == "long":
        sl = min(support * 0.995, close - atr * 1.2) if support > 0 else close - atr * 1.5
        tp1 = close + atr * 2.2
        tp2 = close + atr * 3.8
    else:
        sl = max(resistance * 1.005, close + atr * 1.2) if resistance > 0 else close + atr * 1.5
        tp1 = close - atr * 2.2
        tp2 = close - atr * 3.8

    return SetupEvidence(
        setup_id="liquidity_sweep",
        direction=direction,  # type: ignore[arg-type]
        strength=min(1.0, strength),
        confirmed=True,
        reasons=tuple(reasons[:6]),
        entry=close,
        stop_loss=sl,
        tp1=tp1,
        tp2=tp2,
    )

# --- merged from bos_choch.py ---



def detect_bos_choch(
    row: dict[str, Any],
    prepared: dict[str, Any],
) -> SetupEvidence | None:
    tf = prepared.get("timeframes") if isinstance(prepared.get("timeframes"), dict) else {}
    closed = confirm_tf_chain(tf, "1h_closed", "15m_closed")
    if closed is None:
        return None
    block, close = closed

    struct = block.get("structure") or prepared.get("structure") or {}
    event = str(struct.get("event") or struct.get("bos_choch") or "").lower()
    swing_break = bool(struct.get("swing_break") or struct.get("break_confirmed"))
    pivot = float(struct.get("break_level") or struct.get("pivot") or 0)

    direction = None
    reasons: list[str] = []
    if "choch_bull" in event or (swing_break and struct.get("direction") == "bull"):
        direction = "long"
        reasons.append("choch_bull")
    elif "choch_bear" in event or (swing_break and struct.get("direction") == "bear"):
        direction = "short"
        reasons.append("choch_bear")
    elif "bos_bull" in event or event == "bos_up":
        direction = "long"
        reasons.append("bos_bull")
    elif "bos_bear" in event or event == "bos_down":
        direction = "short"
        reasons.append("bos_bear")

    if direction is None:
        ema50 = float(block.get("ema50") or 0)
        ema200 = float(block.get("ema200") or 0)
        if ema50 > 0 and ema200 > 0 and close > ema50 > ema200:
            direction = "long"
            reasons.append("ema_stack_bull")
        elif ema50 > 0 and ema200 > 0 and close < ema50 < ema200:
            direction = "short"
            reasons.append("ema_stack_bear")

    if direction is None:
        return None

    price = float(row.get("price") or prepared.get("price") or close)
    atr = atr_from_tf(tf) or price * 0.02
    strength = 0.58
    if pivot > 0:
        reasons.append(f"break_level={pivot:.6f}")
        strength += 0.08

    if direction == "long":
        sl = (pivot * 0.998 if pivot > 0 else close - atr * 1.5)
        tp1 = close + atr * 2.5
        tp2 = close + atr * 4.0
    else:
        sl = (pivot * 1.002 if pivot > 0 else close + atr * 1.5)
        tp1 = close - atr * 2.5
        tp2 = close - atr * 4.0

    return SetupEvidence(
        setup_id="bos_choch",
        direction=direction,  # type: ignore[arg-type]
        strength=min(1.0, strength),
        confirmed=True,
        reasons=tuple(reasons[:6]),
        entry=close,
        stop_loss=sl,
        tp1=tp1,
        tp2=tp2,
    )

# --- merged from value_accept_reject.py ---



def detect_value_accept_reject(
    row: dict[str, Any],
    prepared: dict[str, Any],
) -> SetupEvidence | None:
    tf = prepared.get("timeframes") if isinstance(prepared.get("timeframes"), dict) else {}
    closed = confirm_tf_chain(tf, "15m_closed", "1h_closed")
    if closed is None:
        return None
    block, close = closed

    vp = (
        block.get("volume_profile")
        or prepared.get("volume_profile")
        or (row.get("session") or {}).get("volume_profile")
        or {}
    )
    if not isinstance(vp, dict):
        vp = {}
    poc = float(vp.get("poc") or vp.get("poc_price") or 0)
    vah = float(vp.get("vah") or vp.get("value_area_high") or 0)
    val = float(vp.get("val") or vp.get("value_area_low") or 0)
    if poc <= 0 and vah <= 0 and val <= 0:
        return None

    price = float(row.get("price") or prepared.get("price") or close)
    atr = atr_from_tf(tf) or price * 0.02
    tol = atr * 0.35
    reasons: list[str] = []
    direction = None
    strength = 0.0

    if vah > 0 and close > vah + tol:
        direction = "long"
        strength = 0.62
        reasons.extend(["accept_above_vah", f"vah={vah:.6f}"])
    elif val > 0 and close < val - tol:
        direction = "short"
        strength = 0.62
        reasons.extend(["reject_below_val", f"val={val:.6f}"])
    elif poc > 0:
        dist = abs(close - poc)
        if dist <= tol:
            candle = block.get("candle") or {}
            if bool(candle.get("bearish")) and close < poc:
                direction = "short"
                strength = 0.55
                reasons.extend(["reject_poc", f"poc={poc:.6f}"])
            elif bool(candle.get("bullish")) and close > poc:
                direction = "long"
                strength = 0.55
                reasons.extend(["accept_poc", f"poc={poc:.6f}"])

    if direction is None or strength < 0.50:
        return None

    if direction == "long":
        sl = (val - tol) if val > 0 else close - atr * 1.6
        tp1 = close + atr * 2.0
        tp2 = (vah + atr) if vah > 0 else close + atr * 3.5
    else:
        sl = (vah + tol) if vah > 0 else close + atr * 1.6
        tp1 = close - atr * 2.0
        tp2 = (val - atr) if val > 0 else close - atr * 3.5

    return SetupEvidence(
        setup_id="value_accept_reject",
        direction=direction,  # type: ignore[arg-type]
        strength=min(1.0, strength),
        confirmed=True,
        reasons=tuple(reasons[:6]),
        entry=close,
        stop_loss=sl,
        tp1=tp1,
        tp2=tp2,
    )

# --- merged from oi_cascade.py ---



def detect_oi_cascade(
    row: dict[str, Any],
    prepared: dict[str, Any],
) -> SetupEvidence | None:
    tf = prepared.get("timeframes") if isinstance(prepared.get("timeframes"), dict) else {}
    closed = confirm_tf_chain(tf, "5m_closed", "15m_closed")
    if closed is None:
        return None
    block, close = closed

    market = row.get("market") or prepared.get("market") or row.get("positioning") or {}
    oi_1h = float(market.get("oi_chg_1h") or 0)
    oi_z = float(market.get("oi_z") or 0)
    taker = float(market.get("taker_5m") or 0)
    from hunt_core.contract import parse_liquidation_score

    # liquidation_score 0.0 is a valid extreme long-flush (strongest bearish) — a
    # plain `or` would discard it, so prefer 5m only when actually present (A3).
    liq = parse_liquidation_score(market.get("liquidation_score_5m"))
    if liq is None:
        liq = parse_liquidation_score(market.get("liquidation_score_1m"))
    lc = row.get("lifecycle") or prepared.get("lifecycle") or {}
    fall = float(lc.get("fall_from_high_pct") or 0)

    reasons: list[str] = []
    strength = 0.0
    if oi_1h <= -0.008:
        strength += 0.28
        reasons.append(f"oi_flush_1h={oi_1h:.3f}")
    if oi_z >= 1.2 and oi_1h < 0:
        strength += 0.12
        reasons.append(f"oi_z={oi_z:.2f}")
    if taker > 0 and taker < 0.96:
        strength += 0.18
        reasons.append(f"taker_sell={taker:.3f}")
    if liq is not None and liq >= 0.55:
        strength += 0.15
        reasons.append(f"liq_score={liq:.2f}")
    if fall >= 2.0:
        strength += 0.12
        reasons.append(f"fall={fall:.1f}%")

    bearish = bool((block.get("candle") or {}).get("bearish"))
    if bearish:
        strength += 0.10
        reasons.append("bearish_closed_bar")

    if strength < 0.50:
        return None

    price = float(row.get("price") or prepared.get("price") or close)
    atr = atr_from_tf(tf) or price * 0.02
    resistance = float(lc.get("local_resistance") or price + atr)
    sl = resistance + atr * 0.8
    tp1 = price - atr * 2.4
    tp2 = price - atr * 4.0

    return SetupEvidence(
        setup_id="oi_cascade",
        direction="short",
        strength=min(1.0, strength),
        confirmed=True,
        reasons=tuple(reasons[:6]),
        entry=price,
        stop_loss=sl,
        tp1=tp1,
        tp2=tp2,
    )

# --- merged from accumulation_breakout.py ---



def detect_accumulation_breakout(
    row: dict[str, Any],
    prepared: dict[str, Any],
) -> SetupEvidence | None:
    tf = prepared.get("timeframes") if isinstance(prepared.get("timeframes"), dict) else {}
    closed = confirm_tf_chain(tf, "15m_closed", "1h_closed")
    if closed is None:
        return None
    block, close = closed

    lc = row.get("lifecycle") or prepared.get("lifecycle") or {}
    phase = str(lc.get("phase") or "")
    if phase not in {"accumulation", "breakout_arming", "recovery", "impulse_initiating"}:
        return None

    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    if isinstance(prepared.get("market"), dict):
        market = prepared.get("market") or market
    try:
        map_acc = float(market.get("map_vp_accumulation") or 0)
    except (TypeError, ValueError):
        map_acc = 0.0
    pre_pump_start = (
        phase in {"accumulation", "breakout_arming"}
        and map_acc >= 0.55
        and market.get("map_accum_bid_absorption")
    )

    session = row.get("session") or prepared.get("session") or {}
    pos = float(session.get("pos_in_range") or 0.5)
    resistance = float(lc.get("local_resistance") or session.get("high_24h") or 0)
    support = float(lc.get("local_support") or session.get("low_24h") or 0)
    candle = block.get("candle") or {}
    bullish = bool(candle.get("bullish"))

    if resistance <= 0:
        return None
    if pre_pump_start:
        price = float(row.get("price") or prepared.get("price") or close)
        atr = atr_from_tf(tf) or price * 0.02
        reasons = [f"phase={phase}", "map_pre_pump_start", f"map_vp_acc={map_acc:.2f}"]
        if market.get("map_cvd_divergence") == "bullish_div":
            reasons.append("map_cvd_bullish_div")
        strength = 0.56 + min(0.32, map_acc * 0.40)
        sl = support if support > 0 else close - atr * 1.8
        liq_target = market.get("liq_heatmap_nearest_short")
        void_above = market.get("map_void_above")
        tp_magnet = void_above or liq_target
        if tp_magnet is not None:
            try:
                tp1 = float(tp_magnet)
            except (TypeError, ValueError):
                tp1 = close + atr * 2.5
        else:
            tp1 = close + atr * 2.5
        tp2 = close + atr * 4.2
        return SetupEvidence(
            setup_id="accumulation_breakout",
            direction="long",
            strength=min(1.0, strength),
            confirmed=False,
            reasons=tuple(reasons[:6]),
            entry=close,
            stop_loss=sl,
            tp1=tp1,
            tp2=tp2,
        )

    if close <= resistance or not bullish:
        return None

    price = float(row.get("price") or prepared.get("price") or close)
    atr = atr_from_tf(tf) or price * 0.02
    reasons = [f"phase={phase}", f"break_res={resistance:.6f}", f"pos={pos:.2f}"]
    strength = 0.52 + min(0.28, (close - resistance) / resistance * 100.0)

    sl = support if support > 0 else close - atr * 1.8
    tp1 = close + atr * 2.5
    tp2 = close + atr * 4.2

    return SetupEvidence(
        setup_id="accumulation_breakout",
        direction="long",
        strength=min(1.0, strength),
        confirmed=True,
        reasons=tuple(reasons[:6]),
        entry=close,
        stop_loss=sl,
        tp1=tp1,
        tp2=tp2,
    )
