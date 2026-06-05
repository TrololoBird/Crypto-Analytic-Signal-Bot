"""SL classification taxonomy and forensic metric helpers."""

from __future__ import annotations

import json
from typing import Any


def _find_candle_index(candles: list[dict[str, Any]], ts_ms: int) -> int | None:
    if not candles:
        return None
    best_idx = 0
    best_dist = abs(int(candles[0]["ts"]) - ts_ms)
    for idx, candle in enumerate(candles):
        dist = abs(int(candle["ts"]) - ts_ms)
        if dist < best_dist:
            best_dist = dist
            best_idx = idx
    return best_idx


def _find_candle_at_or_before(candles: list[dict[str, Any]], ts_ms: int) -> dict[str, Any] | None:
    if not candles:
        return None
    chosen: dict[str, Any] | None = None
    for candle in candles:
        if int(candle["ts"]) <= ts_ms:
            chosen = candle
        else:
            break
    return chosen or candles[0]


def compute_post_sl_action(
    candles: list[dict[str, Any]],
    sl_hit_ts_ms: int,
    entry_price: float,
    sl_price: float,
    tp1_price: float,
    direction: str,
    analyze_candles: int = 60,
) -> dict[str, Any]:
    """Analyze price action after the SL hit candle."""
    sl_idx = _find_candle_index(candles, sl_hit_ts_ms)
    if sl_idx is None or entry_price <= 0:
        return {
            "post_sl_candles_analyzed": 0,
            "post_sl_max_adverse_pct": 0.0,
            "post_sl_max_recovery_pct": 0.0,
            "post_sl_tp1_reached": False,
            "post_sl_tp1_candles": None,
            "post_sl_price_at_close": None,
        }

    post = candles[sl_idx + 1 : sl_idx + 1 + analyze_candles]
    if not post:
        return {
            "post_sl_candles_analyzed": 0,
            "post_sl_max_adverse_pct": 0.0,
            "post_sl_max_recovery_pct": 0.0,
            "post_sl_tp1_reached": False,
            "post_sl_tp1_candles": None,
            "post_sl_price_at_close": None,
        }

    is_long = direction.lower() == "long"
    max_adverse = 0.0
    max_recovery = 0.0
    tp1_reached = False
    tp1_candles: int | None = None

    for offset, candle in enumerate(post, start=1):
        high = float(candle["high"])
        low = float(candle["low"])
        if is_long:
            adverse = max(0.0, (sl_price - low) / entry_price * 100.0)
            recovery = max(0.0, (high - entry_price) / entry_price * 100.0)
            if tp1_price and high >= tp1_price and not tp1_reached:
                tp1_reached = True
                tp1_candles = offset
        else:
            adverse = max(0.0, (high - sl_price) / entry_price * 100.0)
            recovery = max(0.0, (entry_price - low) / entry_price * 100.0)
            if tp1_price and low <= tp1_price and not tp1_reached:
                tp1_reached = True
                tp1_candles = offset
        max_adverse = max(max_adverse, adverse)
        max_recovery = max(max_recovery, recovery)

    close_idx = min(sl_idx + 8, len(candles) - 1)
    price_at_close = float(candles[close_idx]["close"]) if close_idx > sl_idx else None

    return {
        "post_sl_candles_analyzed": len(post),
        "post_sl_max_adverse_pct": max_adverse,
        "post_sl_max_recovery_pct": max_recovery,
        "post_sl_tp1_reached": tp1_reached,
        "post_sl_tp1_candles": tp1_candles,
        "post_sl_price_at_close": price_at_close,
    }


def compute_btc_correlation(
    symbol_candles: list[dict[str, Any]],
    btc_candles: list[dict[str, Any]],
    sl_hit_ts_ms: int,
) -> dict[str, Any]:
    """Compute BTC correlation metrics for the SL candle."""
    sym_idx = _find_candle_index(symbol_candles, sl_hit_ts_ms)
    if sym_idx is None:
        return {
            "btc_move_in_sl_candle_pct": 0.0,
            "btc_direction_match": "NEUTRAL",
            "btc_caused_sl": False,
        }

    sym_candle = symbol_candles[sym_idx]
    sym_open = float(sym_candle["open"])
    sym_close = float(sym_candle["close"])
    sym_move = ((sym_close - sym_open) / sym_open * 100.0) if sym_open else 0.0

    btc_candle = _find_candle_at_or_before(btc_candles, int(sym_candle["ts"]))
    if btc_candle is None:
        return {
            "btc_move_in_sl_candle_pct": 0.0,
            "btc_direction_match": "NEUTRAL",
            "btc_caused_sl": False,
        }

    btc_open = float(btc_candle["open"])
    btc_close = float(btc_candle["close"])
    btc_move = ((btc_close - btc_open) / btc_open * 100.0) if btc_open else 0.0

    if sym_move == 0.0 or btc_move == 0.0:
        direction_match = "NEUTRAL"
    elif (sym_move > 0 and btc_move > 0) or (sym_move < 0 and btc_move < 0):
        direction_match = "SAME"
    elif (sym_move > 0 and btc_move < 0) or (sym_move < 0 and btc_move > 0):
        direction_match = "OPPOSITE"
    else:
        direction_match = "NEUTRAL"

    return {
        "btc_move_in_sl_candle_pct": btc_move,
        "btc_direction_match": direction_match,
        "btc_caused_sl": abs(btc_move) > 1.5,
    }


def compute_entry_deviation(
    candles: list[dict[str, Any]],
    entry_price: float,
    signal_created_ts_ms: int,
    atr_pct: float,
) -> float:
    """Return entry deviation as multiples of ATR."""
    if entry_price <= 0 or atr_pct <= 0:
        return 0.0
    candle = _find_candle_at_or_before(candles, signal_created_ts_ms)
    if candle is None:
        return 0.0
    close = float(candle["close"])
    pct_move = abs(close - entry_price) / entry_price * 100.0
    return pct_move / atr_pct


def extract_indicator_snapshot(features_json: str | None) -> dict[str, Any]:
    """Parse features JSON into key indicator values."""
    if not features_json:
        return {}
    try:
        raw = json.loads(features_json)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}

    keys = (
        "rsi_1h",
        "adx_1h",
        "volume_ratio",
        "funding_rate",
        "oi_change_pct",
        "atr_pct",
        "spread_bps",
        "confirmed_bar",
        "entry_candle_was_confirmed",
    )
    snapshot: dict[str, Any] = {}
    for key in keys:
        if key in raw and raw[key] is not None:
            snapshot[key] = raw[key]

    nested = raw.get("indicators") or raw.get("features") or {}
    if isinstance(nested, dict):
        for key, value in nested.items():
            if key not in snapshot and value is not None:
                snapshot[key] = value
    return snapshot


def direction_vs_bias(direction: str, btc_bias: str | None) -> str:
    """Classify signal direction against BTC bias."""
    d = direction.lower()
    bias = (btc_bias or "neutral").lower()
    if bias in {"bullish", "bull", "up"}:
        return "ALIGNED" if d == "long" else "AGAINST" if d == "short" else "NEUTRAL"
    if bias in {"bearish", "bear", "down"}:
        return "ALIGNED" if d == "short" else "AGAINST" if d == "long" else "NEUTRAL"
    return "NEUTRAL"


def classify_sl(case: dict[str, Any]) -> tuple[str, str, str]:
    """Apply SL taxonomy; returns (sl_type, sl_subtype, sl_verdict)."""
    tp1_reached = bool(case.get("post_sl_tp1_reached"))
    tp1_candles = case.get("post_sl_tp1_candles")
    sl_dist = float(case.get("sl_distance_pct") or 0.0)
    recovery = float(case.get("post_sl_max_recovery_pct") or 0.0)
    time_to_sl = int(case.get("time_to_sl_min") or 0)
    entry_price = float(case.get("entry_price") or 0.0)
    tp1_price = float(case.get("tp1_price") or 0.0)
    score = float(case.get("score") or 0.0)
    deviation = float(case.get("entry_deviation_atr_mult") or 0.0)
    recheck = case.get("strategy_recheck_valid")
    btc_caused = bool(case.get("btc_caused_sl"))
    btc_match = str(case.get("btc_direction_match") or "NEUTRAL")
    vs_bias = str(case.get("direction_vs_bias") or "NEUTRAL")
    direction = str(case.get("direction") or "")
    symbol = str(case.get("symbol") or "")
    setup_id = str(case.get("setup_id") or "")

    # TYPE 1: STOP_HUNT
    if tp1_reached and tp1_candles is not None and int(tp1_candles) <= 8:
        subtype = "FAST_RECOVERY" if int(tp1_candles) <= 4 else "SLOW_RECOVERY"
        verdict = (
            f"Price swept SL then recovered to TP1 within {tp1_candles} candles — "
            "likely liquidity hunt at stop pool."
        )
        return "STOP_HUNT", subtype, verdict

    # TYPE 2: IMMEDIATE_ADVERSE
    recovery_threshold = 0.15 * sl_dist if sl_dist > 0 else 0.0
    if recovery < recovery_threshold and time_to_sl < 30:
        if btc_caused and btc_match == "SAME":
            subtype = "BTC_DRAG"
            btc_move = float(case.get("btc_move_in_sl_candle_pct") or 0.0)
            verdict = (
                f"BTC moved {btc_move:.2f}% in SL candle, dragging {symbol} "
                "against the position immediately after entry."
            )
        elif deviation > 1.0:
            subtype = "ENTRY_CHASE"
            verdict = (
                f"Entry was {deviation:.2f}×ATR from mark at activation — "
                "chased move, never moved favorably."
            )
        elif recheck is False:
            subtype = "FALSE_SIGNAL"
            verdict = (
                f"Detector does not fire on confirmed historical data for {setup_id} — "
                "likely unclosed-candle false positive."
            )
        elif vs_bias == "AGAINST":
            subtype = "REGIME_FADE"
            verdict = (
                f"{direction} signal traded against confirmed BTC bias "
                f"({case.get('btc_bias', 'unknown')})."
            )
        else:
            subtype = "IMMEDIATE_ADVERSE"
            verdict = "Price never moved favorably; entry timing or direction was wrong."
        return "IMMEDIATE_ADVERSE", subtype, verdict

    # TYPE 4: TIMING_OFF (before THESIS_FAILED per spec order)
    if tp1_reached and tp1_candles is not None and int(tp1_candles) > 8:
        if time_to_sl < 15 and int(tp1_candles) > 20:
            subtype = "PREMATURE_SL"
            verdict = (
                f"SL hit in {time_to_sl} min but TP1 reached {tp1_candles} candles later — "
                "SL too tight for move pace."
            )
        elif int(tp1_candles) <= 48:
            subtype = "CORRECT_DIRECTION_WRONG_TIMING"
            verdict = (
                f"Thesis validated (TP1 in {tp1_candles} candles) but SL was too tight "
                "for the move's timing."
            )
        else:
            subtype = "SLOW_RECOVERY"
            verdict = f"TP1 reached after {tp1_candles} candles — timing/TTL mismatch."
        return "TIMING_OFF", subtype, verdict

    # TYPE 3: THESIS_FAILED (default)
    tp1_dist_pct = 0.0
    if entry_price > 0 and tp1_price > 0:
        tp1_dist_pct = abs(tp1_price - entry_price) / entry_price * 100.0
    thesis_threshold = 0.3 * tp1_dist_pct if tp1_dist_pct > 0 else 0.0

    if score < 0.58:
        subtype = "WEAK_SETUP"
        verdict = f"Low score ({score:.2f}) setup failed; market did not respect the edge."
    elif vs_bias == "AGAINST":
        subtype = "WRONG_DIRECTION"
        verdict = f"Direction {direction} conflicted with BTC bias; thesis failed."
    elif recheck is True:
        subtype = "INDICATOR_FAILURE"
        verdict = (
            "Detector still valid on confirmed data but market ignored the setup — "
            "genuine thesis failure."
        )
    else:
        subtype = "THESIS_FAILED"
        if thesis_threshold > 0 and recovery < thesis_threshold:
            verdict = "Price continued against position with insufficient recovery — thesis failed."
        else:
            verdict = "Setup did not play out; classified as thesis failure."

    return "THESIS_FAILED", subtype, verdict
