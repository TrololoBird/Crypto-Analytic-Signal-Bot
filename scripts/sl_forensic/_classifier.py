"""SL classification taxonomy and forensic metric helpers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import polars as pl


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

    # Include SL candle — wick sweeps often recover within the same bar.
    post = candles[sl_idx : sl_idx + analyze_candles]
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
    *,
    activation_ts_ms: int | None = None,
) -> float:
    """Return entry deviation as multiples of ATR at activation (or signal) time."""
    if entry_price <= 0 or atr_pct <= 0:
        return 0.0
    anchor_ts = activation_ts_ms or signal_created_ts_ms
    candle = _find_candle_at_or_before(candles, anchor_ts)
    if candle is None:
        return 0.0
    close = float(candle["close"])
    pct_move = abs(close - entry_price) / entry_price * 100.0
    return pct_move / atr_pct


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _roc10_at_index(work: pl.DataFrame, end_idx: int, lookback: int = 10) -> float | None:
    if work.height < 2 or "close" not in work.columns:
        return None
    end_idx = max(0, min(end_idx, work.height - 1))
    start_idx = max(0, end_idx - lookback)
    try:
        start = float(work.item(start_idx, "close"))
        end = float(work.item(end_idx, "close"))
    except (TypeError, ValueError):
        return None
    if start <= 0.0 or end <= 0.0:
        return None
    return (end / start - 1.0) * 100.0


def assess_closed_candle_validity(
    candles: list[dict[str, Any]],
    *,
    event_ts_ms: int,
    direction: str,
) -> bool | None:
    """True when momentum on last closed bar agrees with signal direction."""
    if len(candles) < 3:
        return None
    df = pl.DataFrame(
        {
            "ts": [int(c["ts"]) for c in candles],
            "close": [float(c["close"]) for c in candles],
        }
    ).sort("ts")
    idx = _find_candle_index(candles, event_ts_ms)
    if idx is None or idx < 2:
        return None
    roc_prev = _roc10_at_index(df, idx - 1)
    if roc_prev is None:
        return None
    dir_norm = direction.lower()
    if roc_prev > 0.05:
        prev_dir = "long"
    elif roc_prev < -0.05:
        prev_dir = "short"
    else:
        return None
    return prev_dir == dir_norm


def _as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes"}:
            return True
        if lowered in {"0", "false", "no"}:
            return False
    return None


def _tp1_room_pct(entry: float, tp1: float, direction: str) -> float:
    if entry <= 0 or tp1 <= 0:
        return 0.0
    sign = 1.0 if direction.lower() == "long" else -1.0
    return sign * (tp1 - entry) / entry * 100.0


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
    bearish = {"bearish", "bear", "down", "downtrend", "decline", "distribution"}
    bullish = {"bullish", "bull", "up", "uptrend", "markup", "accumulation"}
    if bias in bullish:
        return "ALIGNED" if d == "long" else "AGAINST" if d == "short" else "NEUTRAL"
    if bias in bearish:
        return "ALIGNED" if d == "short" else "AGAINST" if d == "long" else "NEUTRAL"
    return "NEUTRAL"


def classify_sl(case: dict[str, Any]) -> tuple[str, str, str]:
    """Apply SL taxonomy; returns (sl_type, sl_subtype, sl_verdict)."""
    tp1_reached = bool(case.get("post_sl_tp1_reached")) or _as_bool(case.get("post_sl_tp1_reached"))
    tp1_candles = case.get("post_sl_tp1_candles")
    sl_dist = float(case.get("sl_distance_pct") or 0.0)
    recovery = float(case.get("post_sl_max_recovery_pct") or 0.0)
    time_to_sl = int(case.get("time_to_sl_min") or 0)
    entry_price = float(case.get("entry_price") or 0.0)
    tp1_price = float(case.get("tp1_price") or 0.0)
    score = float(case.get("score") or 0.0)
    deviation = float(case.get("entry_deviation_atr_mult") or 0.0)
    recheck = _as_bool(case.get("strategy_recheck_valid"))
    closed_valid = _as_bool(case.get("closed_candle_valid"))
    btc_caused = bool(case.get("btc_caused_sl")) or _as_bool(case.get("btc_caused_sl"))
    btc_match = str(case.get("btc_direction_match") or "NEUTRAL")
    vs_bias = str(case.get("direction_vs_bias") or "NEUTRAL")
    direction = str(case.get("direction") or "")
    symbol = str(case.get("symbol") or "")
    setup_id = str(case.get("setup_id") or "")
    mfe = float(case.get("mfe") or 0.0)
    mae = float(case.get("mae") or 0.0)
    in_trade_tp1 = bool(case.get("tp1_hit_at"))
    tp1_room = _tp1_room_pct(entry_price, tp1_price, direction)
    atr_pct = float(case.get("atr_pct") or 0.0)
    stale_threshold = max(0.15, 1.5 * atr_pct) if atr_pct > 0 else 1.0
    entry_deviation_pct = float(case.get("entry_deviation_pct") or 0.0)

    # TYPE 1: STOP_HUNT — post-SL recovery, in-trade TP1 touch, or partial thesis
    if tp1_reached and tp1_candles is not None and int(tp1_candles) <= 8:
        subtype = "FAST_RECOVERY" if int(tp1_candles) <= 4 else "SLOW_RECOVERY"
        verdict = (
            f"Price swept SL then recovered to TP1 within {tp1_candles} candles — "
            "likely liquidity hunt at stop pool."
        )
        return "STOP_HUNT", subtype, verdict

    if in_trade_tp1:
        subtype = "IN_TRADE_TP1_THEN_STOP"
        verdict = (
            "TP1 was touched in-trade before stop closed — thesis held, "
            "stop placement or BE trail too tight."
        )
        return "STOP_HUNT", subtype, verdict

    # Candle-only setups that fail confirmed-bar recheck are false signals regardless of hold time.
    if recheck is False and setup_id in {
        "btc_correlation",
    }:
        subtype = "FALSE_SIGNAL"
        verdict = (
            f"Detector fires on real-time unclosed candle but NOT on "
            f"confirmed historical data — df[-2] fix required for {setup_id}."
        )
        return "IMMEDIATE_ADVERSE", subtype, verdict

    # TYPE 4: TIMING_OFF — thesis right, SL too tight for pace (check before broad STOP_HUNT)
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

    if recovery >= 1.0 and abs(tp1_room) > 1.5:
        subtype = "POST_SL_RECOVERY"
        verdict = (
            f"Price recovered {recovery:.2f}% toward TP1 after SL exit — "
            "likely stop hunt / liquidity sweep."
        )
        return "STOP_HUNT", subtype, verdict

    if mfe > 0.4 and mae > 0.0 and (mfe / mae) >= 0.4:
        subtype = "PARTIAL_THESIS_THEN_STOP"
        verdict = (
            f"MFE {mfe:.2f}% before SL — partial thesis played out, "
            "stop was too tight for volatility."
        )
        return "STOP_HUNT", subtype, verdict

    # TYPE 2: IMMEDIATE_ADVERSE — strict time gate per taxonomy
    recovery_threshold = 0.15 * sl_dist if sl_dist > 0 else 0.0
    immediate = recovery < recovery_threshold and time_to_sl < 30
    if immediate:
        if btc_caused and btc_match == "SAME":
            subtype = "BTC_DRAG"
            btc_move = float(case.get("btc_move_in_sl_candle_pct") or 0.0)
            verdict = (
                f"BTC moved {btc_move:.2f}% in the SL candle, dragging {symbol} "
                "against the position immediately after entry."
            )
        elif deviation > 1.0 or entry_deviation_pct > stale_threshold:
            subtype = "ENTRY_CHASE"
            verdict = (
                f"Entry was {deviation:.2f}×ATR from mark at activation — "
                "chased move, never moved favorably."
            )
        elif recheck is False:
            subtype = "FALSE_SIGNAL"
            verdict = (
                f"Detector fires on real-time unclosed candle but NOT on "
                f"confirmed historical data — df[-2] fix required for {setup_id}."
            )
        elif closed_valid is False and setup_id in {
            "btc_correlation",
            "ema_bounce",
            "funding_reversal",
        }:
            subtype = "FALSE_SIGNAL"
            verdict = (
                f"Confirmed-bar momentum disagrees with {direction} on {setup_id} — "
                "df[-2] fix required."
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
