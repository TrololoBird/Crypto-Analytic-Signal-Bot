"""Build forensic case dicts from bot.db outcome rows."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aiosqlite

from engine.errors import DEFENSIVE_EXC
from scripts.sl_forensic._classifier import (
    assess_closed_candle_validity,
    classify_sl,
    compute_btc_correlation,
    compute_entry_deviation,
    compute_post_sl_action,
    direction_vs_bias,
    extract_indicator_snapshot,
)
from scripts.sl_forensic._confirmed_candle import infer_confirmed_candle
from scripts.sl_forensic._paths import SL_RESULTS
from scripts.sl_forensic._strategy_recheck import recheck_strategy, ts_ms_from_iso

EXPORT_QUERY = """
    SELECT
        so.tracking_id,
        so.signal_id,
        so.setup_id,
        so.symbol,
        so.direction,
        so.timeframe,
        so.result,
        so.pnl_pct,
        so.created_at AS signal_created_at,
        so.activated_at AS entry_activated_at,
        so.closed_at AS sl_hit_at,
        so.time_to_entry_min,
        so.time_to_exit_min,
        so.entry_price,
        so.exit_price AS sl_price,
        so.mfe,
        so.mae,
        so.features,
        as2.score,
        as2.atr_pct,
        as2.spread_bps,
        as2.take_profit_1 AS tp1_price,
        as2.take_profit_2 AS tp2_price,
        as2.initial_stop,
        as2.entry_mid,
        as2.activation_price,
        as2.risk_reward,
        as2.bias_4h,
        as2.tp1_hit_at,
        mc.market_regime,
        mc.btc_bias
    FROM signal_outcomes so
    JOIN active_signals as2 ON so.tracking_id = as2.tracking_id
    LEFT JOIN market_context mc ON mc.id = 1
    WHERE so.result IS NOT NULL
    ORDER BY so.closed_at DESC
"""


def _sl_distance_pct(entry: float | None, sl: float | None) -> float:
    if not entry or not sl or entry <= 0:
        return 0.0
    return abs(entry - sl) / entry * 100.0


def _rr_ratio(entry: float | None, sl: float | None, tp1: float | None) -> float | None:
    if not entry or not sl or not tp1 or entry <= 0:
        return None
    risk = abs(entry - sl)
    reward = abs(tp1 - entry)
    if risk <= 0:
        return None
    return reward / risk


def row_to_base_case(row: aiosqlite.Row) -> dict[str, Any]:
    time_to_exit = int(row["time_to_exit_min"] or 0)
    time_to_entry = int(row["time_to_entry_min"] or 0)
    entry = float(row["entry_price"] or 0)
    initial_stop = float(row["initial_stop"] or 0)
    exit_price = float(row["sl_price"] or 0)
    result = str(row["result"] or "")
    if result in {"breakeven_stop", "trailing_stop"} and initial_stop > 0:
        sl = initial_stop
    else:
        sl = exit_price or initial_stop
    tp1 = float(row["tp1_price"] or 0)
    features = row["features"]
    snapshot = extract_indicator_snapshot(features)
    funding = snapshot.get("funding_rate")
    entry_mid = float(row["entry_mid"] or entry)
    activation = float(row["activation_price"] or entry)
    entry_deviation_pct = abs(activation - entry_mid) / entry_mid * 100.0 if entry_mid > 0 else 0.0
    return {
        "tracking_id": row["tracking_id"],
        "signal_id": row["signal_id"],
        "setup_id": row["setup_id"],
        "symbol": row["symbol"],
        "direction": row["direction"],
        "timeframe": row["timeframe"] or "15m",
        "result": result,
        "pnl_pct": float(row["pnl_pct"] or 0),
        "signal_created_at": row["signal_created_at"],
        "entry_activated_at": row["entry_activated_at"],
        "sl_hit_at": row["sl_hit_at"],
        "tp1_hit_at": row["tp1_hit_at"],
        "time_to_entry_min": time_to_entry,
        "time_to_sl_min": max(0, time_to_exit - time_to_entry),
        "entry_price": entry,
        "sl_price": sl,
        "tp1_price": tp1,
        "sl_distance_pct": _sl_distance_pct(entry, sl),
        "rr_ratio": _rr_ratio(entry, sl, tp1),
        "mfe": float(row["mfe"] or 0),
        "mae": float(row["mae"] or 0),
        "score": float(row["score"] or 0),
        "atr_pct": float(row["atr_pct"] or snapshot.get("atr_pct") or 0),
        "spread_bps": float(row["spread_bps"] or snapshot.get("spread_bps") or 0),
        "funding_rate": float(funding) if funding is not None else None,
        "bias_4h": row["bias_4h"] or snapshot.get("bias_4h"),
        "market_regime": row["market_regime"] or "unknown",
        "btc_bias": row["btc_bias"] or row["bias_4h"] or "neutral",
        "direction_vs_bias": direction_vs_bias(
            str(row["direction"]),
            row["btc_bias"] or row["bias_4h"],
        ),
        "entry_deviation_pct": entry_deviation_pct,
        "features": features,
        "indicator_snapshot": snapshot,
    }


async def enrich_sl_case(
    case: dict[str, Any],
    *,
    settings: Any,
    fetcher: Any | None,
    do_recheck: bool = True,
) -> dict[str, Any]:
    """Add candle replay, classification, and confirmed-candle inference."""
    signal_ts = ts_ms_from_iso(case.get("signal_created_at"))
    sl_ts = ts_ms_from_iso(case.get("sl_hit_at"))
    anchor_ts = signal_ts or sl_ts
    assess_closed: bool | None = None

    if fetcher is not None and anchor_ts is not None:
        tf = str(case.get("timeframe") or "15m").split("+")[0].strip() or "15m"
        try:
            windows = await fetcher.fetch_window(
                str(case["symbol"]),
                anchor_ts_ms=anchor_ts,
                sl_hit_ts_ms=sl_ts or anchor_ts,
                signal_tf=tf,
            )
            signal_candles = windows.get(tf, [])
            btc_candles = windows.get("BTC_signal_tf", [])

            post = compute_post_sl_action(
                signal_candles,
                sl_ts or anchor_ts,
                float(case["entry_price"] or 0),
                float(case["sl_price"] or 0),
                float(case["tp1_price"] or 0),
                str(case["direction"]),
            )
            case.update(post)
            case["post_sl_max_recovery"] = post.get("post_sl_max_recovery_pct")
            case["post_sl_max_adverse"] = post.get("post_sl_max_adverse_pct")

            btc = compute_btc_correlation(
                signal_candles,
                btc_candles,
                sl_ts or anchor_ts,
            )
            case.update(btc)
            case["btc_move_sl_candle_pct"] = btc.get("btc_move_in_sl_candle_pct")

            case["entry_deviation_atr_mult"] = compute_entry_deviation(
                signal_candles,
                float(case["entry_price"] or 0),
                signal_ts or anchor_ts,
                float(case["atr_pct"] or 0),
                activation_ts_ms=ts_ms_from_iso(case.get("entry_activated_at")),
            )
            case["entry_deviation_atr"] = case["entry_deviation_atr_mult"]

            act_ts = ts_ms_from_iso(case.get("entry_activated_at")) or signal_ts
            if act_ts is not None and signal_candles:
                assess_closed = assess_closed_candle_validity(
                    signal_candles,
                    event_ts_ms=act_ts,
                    direction=str(case["direction"]),
                )

            if do_recheck:
                recheck = await recheck_strategy(
                    str(case["setup_id"]),
                    str(case["symbol"]),
                    str(case["timeframe"]),
                    signal_candles,
                    signal_ts or anchor_ts,
                    settings,
                    context=case,
                )
                valid = recheck.get("valid")
                if valid is True:
                    case["strategy_recheck_valid"] = 1
                    case["false_signal_recheck"] = 1
                elif valid is False:
                    case["strategy_recheck_valid"] = 0
                    case["false_signal_recheck"] = 0
                else:
                    case["strategy_recheck_valid"] = None
                    case["false_signal_recheck"] = None
                case["strategy_recheck_reason"] = recheck.get("reason")
        except DEFENSIVE_EXC:
            case.setdefault("entry_deviation_atr", 0.0)
            case.setdefault("entry_deviation_atr_mult", 0.0)

    if "entry_deviation_atr" not in case:
        case["entry_deviation_atr"] = case.get("entry_deviation_atr_mult") or 0.0

    confirmed = infer_confirmed_candle(
        setup_id=str(case["setup_id"]),
        signal_created_at=case.get("signal_created_at"),
        features_snapshot=case.get("indicator_snapshot"),
        assess_closed_valid=assess_closed,
    )
    case["confirmed_candle"] = confirmed
    case["entry_candle_was_confirmed"] = confirmed if confirmed is not None else 0

    if str(case.get("result") or "") in SL_RESULTS:
        sl_type, sl_subtype, sl_verdict = classify_sl(case)
        case["sl_type"] = sl_type
        case["sl_subtype"] = sl_subtype
        case["sl_verdict"] = sl_verdict

    return case
