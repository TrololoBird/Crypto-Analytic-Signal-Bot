"""Delivery-path helpers retained after the fusion detection cutover.

Small, self-contained utilities the declarative delivery gate still reads from
market/setup rows (evidence coverage, MTF accumulation confluence, order-flow
alignment, kline CVD flow). Detection scoring lives in ``detect/*``; this module
only supports the preserved ``validate_signal_contract → gate → deliver`` path.
"""
from __future__ import annotations

import math
from typing import Any, Literal

from hunt_core.contract import parse_liquidation_score
from hunt_core.params.store import orderflow_thresholds

FUEL_EVIDENCE_KEYS_SHORT: tuple[tuple[str, ...], ...] = (
    ("funding_pct", "funding_rate"),
    ("funding_zscore_48h",),
    ("oi_chg_1h", "oi_change_pct"),
    ("oi_chg_5m",),
    ("oi_z",),
    ("gls_z",),
    ("taker_5m", "taker_1h", "taker_ratio"),
    ("top_ls_5m", "top_ls_1h", "top_position_ls_ratio", "ls_ratio"),
    ("global_ls_5m", "global_ls_1h", "global_ls_ratio", "global_account_ls_ratio"),
    ("top_vs_global_ls_gap",),
    ("basis_pct", "basis_ap_bps", "basis", "basis_bps", "basis_5m"),
    ("premium_zscore_5m", "premium_slope_5m"),
    ("liquidation_score_5m",),
    ("liq_cascade_risk",),
    ("liq_forward_confidence", "liq_forward_weight"),
    ("liq_heatmap_nearest_long", "liq_heatmap_nearest_short"),
    ("map_sticky_wall_count", "map_stacked_imbalance", "map_cvd_divergence"),
    ("agg_trade_delta_60s",),
    ("taker_imbalance_cusum",),
    ("depth_imbalance", "ws_depth_imbalance", "live_depth_imbalance"),
)

FUEL_EVIDENCE_KEYS_LONG: tuple[tuple[str, ...], ...] = FUEL_EVIDENCE_KEYS_SHORT

_EVIDENCE_ADJUSTED_MIN_FUEL_CAP = 95.0
_EVIDENCE_COVERAGE_FLOOR = 0.5
EARLY_ADVISORY_MIN_IGNITION = 25.0
_CVD_DIV_PRICE_MIN_PCT = 0.08
_CVD_DIV_FUEL_5M = 10.0
_CVD_DIV_FUEL_1M = 6.0


def _fuel(mkt: dict[str, Any], key: str) -> float:
    try:
        return float(mkt.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _resolve_depth_imbalance(market: dict[str, Any]) -> float | None:
    for key in ("ws_depth_imbalance", "depth_imbalance", "live_depth_imbalance"):
        raw = market.get(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _market_finite_scalar(market: dict[str, Any], key: str) -> bool:
    raw = market.get(key)
    if raw is None:
        return False
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return False
    return math.isfinite(val)


def _fuel_evidence_slot_present(market: dict[str, Any], slot: tuple[str, ...]) -> bool:
    if "liquidation_score_5m" in slot:
        if parse_liquidation_score(market.get("liquidation_score_5m")) is not None:
            return True
    if "liq_cascade_risk" in slot:
        if market.get("liq_cascade_risk") in {"long_flush", "short_squeeze"}:
            return True
    if any(k in slot for k in ("depth_imbalance", "ws_depth_imbalance", "live_depth_imbalance")):
        if _resolve_depth_imbalance(market) is not None:
            return True
    for key in slot:
        if key in {"liquidation_score_5m", "liq_cascade_risk"}:
            continue
        if key in {"depth_imbalance", "ws_depth_imbalance", "live_depth_imbalance"}:
            continue
        if key == "map_sticky_wall_count":
            try:
                if int(market.get(key) or 0) >= 1:
                    return True
            except (TypeError, ValueError):
                pass
            continue
        if key == "map_stacked_imbalance" and market.get(key):
            return True
        if key == "map_cvd_divergence" and market.get(key):
            return True
        if _market_finite_scalar(market, key):
            return True
    return False


def long_resistance_chase_veto(resistance: float, price: float, r5_close: float) -> bool:
    """Veto late long chase; allow 0.5% retest when 5m closed above resistance."""
    if resistance <= 0 or price <= 0:
        return False
    ratio = 0.995 if r5_close > resistance else 0.998
    return price < resistance * ratio


def _tf_closed_block(tf: dict[str, Any] | None, interval: str) -> dict[str, Any]:
    if not isinstance(tf, dict):
        return {}
    closed_key = "1m_closed" if interval == "1m" else f"{interval}_closed"
    block = tf.get(closed_key) or tf.get(interval)
    return block if isinstance(block, dict) else {}


def kline_bar_flow(
    tf: dict[str, Any] | None,
    interval: str,
) -> tuple[float | None, float | None]:
    """Closed-bar CVD delta + price change % from prepared klines."""
    block = _tf_closed_block(tf, interval)
    if not block:
        return None, None
    delta: float | None = None
    try:
        cur = block.get("session_cvd")
        prev = block.get("session_cvd_prev")
        if cur is not None and prev is not None:
            delta = float(cur) - float(prev)
        elif cur is not None:
            delta = float(cur)
    except (TypeError, ValueError):
        delta = None
    px_chg: float | None = None
    try:
        o = float(block.get("open") or 0)
        c = float(block.get("close") or 0)
        if o > 0 and c > 0:
            px_chg = (c - o) / o * 100.0
    except (TypeError, ValueError):
        px_chg = None
    return delta, px_chg


def resolve_flow_cvd_px(
    market: dict[str, Any],
    tf: dict[str, Any] | None,
    *,
    interval: str,
) -> tuple[float | None, float | None, str]:
    """Prefer closed-bar kline CVD delta; WS rolling CVD is enhancement only."""
    delta, px_chg = kline_bar_flow(tf, interval)
    if delta is not None and px_chg is not None:
        return delta, px_chg, "kline"
    mkt = market or {}
    try:
        cvd_raw = mkt.get(f"ws_cvd_{interval}")
        px_raw = mkt.get(f"ws_price_chg_{interval}")
        if cvd_raw is not None and px_raw is not None:
            return float(cvd_raw), float(px_raw), "ws"
    except (TypeError, ValueError):
        pass
    return None, None, ""


def inject_kline_flow_into_market(market: dict[str, Any], tf: dict[str, Any] | None) -> None:
    """Stamp kline flow on market for telemetry — never overwrites WS keys."""
    if not isinstance(market, dict) or not isinstance(tf, dict):
        return
    for interval in ("1m", "5m", "15m"):
        delta, px_chg = kline_bar_flow(tf, interval)
        if delta is not None:
            market[f"kline_cvd_delta_{interval}"] = round(delta, 6)
        if px_chg is not None:
            market[f"kline_price_chg_{interval}"] = round(px_chg, 4)
    for interval in ("1m", "15m"):
        block = tf.get(f"{interval}_closed") or tf.get(interval) or {}
        if not isinstance(block, dict):
            continue
        cusum = block.get("taker_imbalance_cusum")
        if cusum is not None and market.get("taker_imbalance_cusum") is None:
            market["taker_imbalance_cusum"] = round(float(cusum), 3)
            break
    sources: list[str] = []
    for interval in ("1m", "5m"):
        _cvd, _px, src = resolve_flow_cvd_px(market, tf, interval=interval)
        if src:
            sources.append(f"{interval}:{src}")
    if sources:
        market["flow_cvd_source"] = ",".join(sources)


def ws_cvd_divergence_fuel_triggers(
    market: dict[str, Any],
    *,
    direction: Literal["long", "short"],
    tf: dict[str, Any] | None = None,
) -> list[tuple[str, float]]:
    """CVD vs price divergence — WS agg trades when live, else kline bar delta."""
    out: list[tuple[str, float]] = []
    for label, fuel in (("5m", _CVD_DIV_FUEL_5M), ("1m", _CVD_DIV_FUEL_1M)):
        cvd, px_chg, src = resolve_flow_cvd_px(market, tf, interval=label)
        if cvd is None or px_chg is None:
            continue
        prefix = "kline" if src == "kline" else "ws"
        if direction == "short" and px_chg >= _CVD_DIV_PRICE_MIN_PCT and cvd < 0.0:
            out.append((f"{prefix}_cvd_bear_div_{label}", fuel))
        elif direction == "long" and px_chg <= -_CVD_DIV_PRICE_MIN_PCT and cvd > 0.0:
            out.append((f"{prefix}_cvd_bull_div_{label}", fuel))
    return out


def maps_flow_confirms(
    mkt: dict[str, Any],
    *,
    direction: Literal["long", "short"],
) -> bool:
    try:
        if int(mkt.get("map_absorption_count") or 0) >= 1:
            return True
    except (TypeError, ValueError):
        pass
    if direction == "long" and mkt.get("map_accum_bid_absorption"):
        return True
    stacked = mkt.get("map_stacked_imbalance")
    if direction == "short" and stacked == "sell_stack":
        return True
    if direction == "long" and stacked == "buy_stack":
        return True
    cvd = mkt.get("map_cvd_divergence")
    if direction == "short" and cvd == "bearish_div":
        return True
    if direction == "long" and cvd == "bullish_div":
        return True
    if direction == "long" and _fuel(mkt, "liq_squeeze_fuel_short") >= 0.6:
        return True
    if direction == "short" and _fuel(mkt, "liq_squeeze_fuel_long") >= 0.6:
        return True
    return False


def maps_accumulation_confirms(
    mkt: dict[str, Any],
    *,
    direction: Literal["long", "short"],
    pos_in_range: float | None = None,
) -> bool:
    """Leading pre-pump accumulation confluence from maps VP + orderbook."""
    if direction != "long":
        return False
    if _fuel(mkt, "map_accumulation_score") >= 0.55:
        return True
    try:
        acc = float(mkt.get("map_vp_accumulation") or 0)
    except (TypeError, ValueError):
        return False
    if acc < 0.45:
        return False
    signals = 0
    if mkt.get("map_accum_bid_absorption"):
        signals += 1
    if mkt.get("map_ask_thinning"):
        signals += 1
    if mkt.get("map_cvd_divergence") == "bullish_div":
        signals += 1
    contraction = mkt.get("map_vp_va_contraction")
    if contraction is not None:
        try:
            if float(contraction) < 0.85:
                signals += 1
        except (TypeError, ValueError):
            pass
    if pos_in_range is not None:
        try:
            if float(pos_in_range) <= 0.45:
                signals += 1
        except (TypeError, ValueError):
            pass
    if mkt.get("map_void_above") and acc >= 0.50:
        signals += 1
    return signals >= 2


def count_fuel_evidence(
    market: dict[str, Any],
    *,
    direction: Literal["short", "long"],
) -> tuple[int, int]:
    slots = FUEL_EVIDENCE_KEYS_SHORT if direction == "short" else FUEL_EVIDENCE_KEYS_LONG
    present = sum(1 for slot in slots if _fuel_evidence_slot_present(market, slot))
    return present, len(slots)


def evidence_coverage_ratio(
    market: dict[str, Any],
    *,
    direction: Literal["short", "long"],
) -> float:
    present, total = count_fuel_evidence(market, direction=direction)
    if total <= 0:
        return 1.0
    return round(min(1.0, max(0.0, present / total)), 4)


def evidence_adjusted_min_fuel(
    base_min: float,
    coverage: float,
    *,
    min_coverage_for_delivery: float = 0.65,
) -> float | None:
    if coverage < min_coverage_for_delivery:
        return None
    adjusted = float(base_min) / max(float(coverage), _EVIDENCE_COVERAGE_FLOOR)
    return round(min(_EVIDENCE_ADJUSTED_MIN_FUEL_CAP, adjusted), 1)


def _orderflow_confirm_aligned(
    direction: str,
    mkt: dict[str, Any],
    *,
    symbol: str = "",
) -> tuple[bool, str]:
    of = orderflow_thresholds(symbol)
    if not of.get("require_ws_align", True):
        return True, ""
    stacked = mkt.get("map_stacked_imbalance")
    cvd_div = mkt.get("map_cvd_divergence")
    if direction == "short":
        if stacked == "sell_stack" or cvd_div == "bearish_div":
            return True, ""
    elif direction == "long":
        if stacked == "buy_stack" or cvd_div == "bullish_div":
            return True, ""
    agg60 = mkt.get("agg_trade_delta_60s")
    if agg60 is None:
        if maps_flow_confirms(mkt, direction=direction):  # type: ignore[arg-type]
            return True, ""
        return True, ""
    try:
        val = float(agg60)
    except (TypeError, ValueError):
        return False, "orderflow_data_invalid"
    buy_min = float(of.get("taker_buy_min", 0.58))
    sell_max = float(of.get("taker_sell_max", 0.42))
    if direction == "short":
        if stacked == "buy_stack" and val > sell_max:
            return False, "orderflow_maps_buy_stack_vs_short"
        if val > sell_max:
            return False, "orderflow_buy_pressure_vs_short"
    if direction == "long":
        if stacked == "sell_stack" and val < buy_min:
            return False, "orderflow_maps_sell_stack_vs_long"
        if val < buy_min:
            return False, "orderflow_sell_pressure_vs_long"
    return True, ""


def closed_bar_candle(tf: dict[str, Any], interval: str) -> dict[str, Any]:
    """Wick/rejection reads only on fully closed bars — never the live tail."""
    blk = tf.get(f"{interval}_closed")
    if not isinstance(blk, dict) or not blk or blk.get("closed_bar") is False:
        return {}
    candle = blk.get("candle")
    return candle if isinstance(candle, dict) else {}


def cluster_fuel(triggers: list[str], *, raw_score: float, symbol: str = "") -> float:
    """Legacy cluster fuel — passthrough when HUNT_LEGACY_FUEL is off (fusion default)."""
    from hunt_core.setups.catalog import legacy_fuel_merge_enabled

    _ = triggers, symbol
    raw = float(raw_score or 0)
    if not legacy_fuel_merge_enabled():
        return round(min(100.0, max(0.0, raw)), 1)
    return round(min(100.0, max(0.0, raw)), 1)


__all__ = [
    "EARLY_ADVISORY_MIN_IGNITION",
    "closed_bar_candle",
    "cluster_fuel",
    "count_fuel_evidence",
    "evidence_adjusted_min_fuel",
    "evidence_coverage_ratio",
    "inject_kline_flow_into_market",
    "kline_bar_flow",
    "long_resistance_chase_veto",
    "maps_accumulation_confirms",
    "maps_flow_confirms",
    "resolve_flow_cvd_px",
    "ws_cvd_divergence_fuel_triggers",
    "_orderflow_confirm_aligned",
]
