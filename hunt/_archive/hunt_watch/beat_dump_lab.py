"""BEAT dump experiment — full indicator matrix + multi-layer scenario engine.

Not a 4-rule heuristic: every numeric column on each TF votes into semantic clusters
(exhaustion, trend_break, reversal, volatility, flow, structure), cross-TF consensus,
REST positioning layer, and ranked dump scenarios with per-indicator evidence.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from engine.data_readiness import kline_fetch_limit
from engine.domain.config import load_settings
from engine.domain.schemas import SymbolFrames, UniverseSymbol
from engine.errors import DEFENSIVE_EXC
from engine.features.pivots import _pivot_rows, with_spec_columns
from engine.features.prepare import _prepare_frame, min_required_bars, prepare_symbol
from engine.market.data import BinanceFuturesMarketData
from engine.market.rest_impl import BinanceClientImpl

from hunt_watch.data_completeness import (
    FULL_INDICATOR_COLUMNS,
    CompletenessReport,
    DataIncompleteError,
    audit_beat_dump_tick,
    finite_float,
    series_chg_pct_strict,
    series_z_strict,
)
from hunt_watch.lifecycle import assess_hunt_lifecycle
from hunt_watch.levels import structural_short_levels

TF_KEYS = ("1m", "3m", "5m", "15m", "1h", "4h")

OHLCV_SKIP = frozenset(
    {
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "taker_buy_volume",
        "taker_buy_base_volume",
        "taker_buy_quote_volume",
        "trades",
        "num_trades",
        "open_time",
        "close_time",
        "ignore",
        "time",
        "ha_open",
        "ha_high",
        "ha_low",
        "ha_close",
    }
)

CLUSTERS = (
    "exhaustion",
    "trend_break",
    "reversal",
    "volatility",
    "flow",
    "structure",
)

TF_WEIGHTS: dict[str, float] = {
    "1m": 0.05,
    "3m": 0.08,
    "5m": 0.12,
    "15m": 0.18,
    "1h": 0.25,
    "4h": 0.32,
}

# Per-indicator dump semantics: polarity high = overbought/exhaustion fuels dump;
# low = bearish reading when value is low; neg = more negative is more dump;
# dir = -1 is dump; bool = 1.0 when truthy.
INDICATOR_RULES: dict[str, dict[str, Any]] = {
    "rsi14": {"cluster": "exhaustion", "polarity": "high", "warm": 58, "hot": 68, "extreme": 75, "weight": 1.3},
    "stoch_k14": {"cluster": "exhaustion", "polarity": "high", "warm": 70, "hot": 80, "extreme": 88, "weight": 1.0},
    "stoch_d14": {"cluster": "exhaustion", "polarity": "high", "warm": 70, "hot": 80, "extreme": 88, "weight": 0.8},
    "stoch_rsi14": {"cluster": "exhaustion", "polarity": "high", "warm": 0.75, "hot": 0.85, "extreme": 0.92, "weight": 1.0},
    "mfi14": {"cluster": "exhaustion", "polarity": "high", "warm": 65, "hot": 72, "extreme": 80, "weight": 0.9},
    "willr14": {"cluster": "exhaustion", "polarity": "high", "warm": -25, "hot": -15, "extreme": -8, "weight": 0.8},
    "cci20": {"cluster": "exhaustion", "polarity": "high", "warm": 80, "hot": 120, "extreme": 180, "weight": 0.7},
    "bb_pct_b": {"cluster": "exhaustion", "polarity": "high", "warm": 0.85, "hot": 0.95, "extreme": 1.05, "weight": 1.0},
    "vwap_deviation_atr14": {
        "cluster": "exhaustion",
        "polarity": "high",
        "warm": 1.0,
        "hot": 1.5,
        "extreme": 2.2,
        "weight": 1.1,
    },
    "vwap_deviation_z20": {"cluster": "exhaustion", "polarity": "high", "warm": 1.2, "hot": 1.8, "extreme": 2.5, "weight": 0.9},
    "zscore30": {"cluster": "exhaustion", "polarity": "high", "warm": 1.5, "hot": 2.0, "extreme": 2.8, "weight": 0.8},
    "fisher": {"cluster": "exhaustion", "polarity": "high", "warm": 1.2, "hot": 1.8, "extreme": 2.5, "weight": 0.7},
    "adx14": {"cluster": "trend_break", "polarity": "high", "warm": 22, "hot": 28, "extreme": 38, "weight": 0.6},
    "minus_di14": {"cluster": "trend_break", "polarity": "high", "warm": 18, "hot": 25, "extreme": 35, "weight": 0.9},
    "plus_di14": {"cluster": "trend_break", "polarity": "low", "warm": 30, "hot": 22, "extreme": 15, "weight": 0.9},
    "macd_hist": {"cluster": "trend_break", "polarity": "neg", "warm": 0.0, "hot": -0.0001, "extreme": -0.001, "weight": 1.0},
    "macd_line": {"cluster": "trend_break", "polarity": "neg_slope", "weight": 0.7},
    "supertrend_dir": {"cluster": "trend_break", "polarity": "dir", "weight": 1.2},
    "chandelier_dir": {"cluster": "trend_break", "polarity": "dir", "weight": 0.8},
    "close_ols_slope_atr20": {"cluster": "trend_break", "polarity": "neg", "warm": 0.0, "hot": -0.05, "extreme": -0.15, "weight": 0.9},
    "close_ols_slope_pct20": {"cluster": "trend_break", "polarity": "neg", "warm": 0.0, "hot": -0.1, "extreme": -0.3, "weight": 0.7},
    "slope5": {"cluster": "trend_break", "polarity": "neg", "warm": 0.0, "hot": -0.05, "extreme": -0.2, "weight": 0.6},
    "roc10": {"cluster": "trend_break", "polarity": "neg", "warm": 0.0, "hot": -1.0, "extreme": -3.0, "weight": 0.7},
    "candle_gravestone": {"cluster": "reversal", "polarity": "bool", "weight": 1.2},
    "candle_bearish_engulfing": {"cluster": "reversal", "polarity": "bool", "weight": 1.3},
    "candle_dragonfly": {"cluster": "reversal", "polarity": "bool_inverse", "weight": 0.4},
    "psar_reversal": {"cluster": "reversal", "polarity": "bool", "weight": 1.0},
    "squeeze_on": {"cluster": "volatility", "polarity": "bool", "weight": 0.5},
    "squeeze_hist": {"cluster": "volatility", "polarity": "neg", "warm": 0.0, "hot": -0.0001, "extreme": -0.001, "weight": 0.9},
    "bb_width_pctile50": {"cluster": "volatility", "polarity": "low", "warm": 0.35, "hot": 0.22, "extreme": 0.12, "weight": 0.8},
    "bb_width": {"cluster": "volatility", "polarity": "low", "weight": 0.4},
    "realized_vol_20": {"cluster": "volatility", "polarity": "high", "warm": 0.02, "hot": 0.04, "extreme": 0.07, "weight": 0.5},
    "atr_pct": {"cluster": "volatility", "polarity": "high", "warm": 1.5, "hot": 2.5, "extreme": 4.0, "weight": 0.5},
    "delta_ratio": {"cluster": "flow", "polarity": "low", "warm": 0.52, "hot": 0.48, "extreme": 0.42, "weight": 1.1},
    "volume_ratio20": {"cluster": "flow", "polarity": "high", "warm": 1.3, "hot": 1.8, "extreme": 2.5, "weight": 0.8},
    "cmf20": {"cluster": "flow", "polarity": "low", "warm": 0.05, "hot": 0.0, "extreme": -0.08, "weight": 0.9},
    "obv_above_ema": {"cluster": "flow", "polarity": "bool_inverse", "weight": 0.9},
    "session_cvd": {"cluster": "flow", "polarity": "neg", "weight": 0.7},
    "rolling_cvd_24h": {"cluster": "flow", "polarity": "neg", "weight": 0.8},
    "signed_order_flow": {"cluster": "flow", "polarity": "neg", "weight": 0.7},
    "tob_imbalance": {"cluster": "flow", "polarity": "low", "warm": 0.0, "hot": -0.1, "extreme": -0.25, "weight": 0.8},
    "close_position": {"cluster": "structure", "polarity": "low", "warm": 0.55, "hot": 0.4, "extreme": 0.25, "weight": 0.9},
    "donchian_high20": {"cluster": "structure", "polarity": "near_high", "weight": 0.6},
    "pivot_r1": {"cluster": "structure", "polarity": "near_level", "weight": 0.5},
    "pivot_r2": {"cluster": "structure", "polarity": "near_level", "weight": 0.6},
}


@dataclass
class TickState:
    """Prior tick for delta computation."""

    ts: str = ""
    cluster_scores: dict[str, float] = field(default_factory=dict)
    composite: float = 0.0
    feature_hash: dict[str, float] = field(default_factory=dict)


def _col(df: Any, name: str, default: float = 0.0, *, idx: int = -1) -> float:
    if df is None or df.is_empty() or name not in df.columns:
        return default
    try:
        v = float(df.item(idx, name))
        return default if not math.isfinite(v) else v
    except (TypeError, ValueError, IndexError):
        return default


def _candle_shape(df: Any, *, idx: int = -1) -> dict[str, Any]:
    o, h, l, c = (_col(df, x, idx=idx) for x in ("open", "high", "low", "close"))
    body = abs(c - o)
    rng = max(h - l, 1e-12)
    upper = h - max(o, c)
    lower = min(o, c) - l
    return {
        "bearish": c < o,
        "bullish": c > o,
        "body_ratio": round(body / rng, 3),
        "upper_wick_ratio": round(upper / rng, 3),
        "lower_wick_ratio": round(lower / rng, 3),
    }


def _percentile_rank(values: list[float], current: float) -> float:
    if len(values) < 8:
        return 0.5
    below = sum(1 for v in values if v <= current)
    return below / len(values)


def _require_col(df: Any, name: str, *, idx: int = -1, ctx: str = "") -> float:
    if df is None or df.is_empty() or name not in df.columns:
        raise DataIncompleteError((f"{ctx}.{name}=missing_column",))
    try:
        v = float(df.item(idx, name))
    except (TypeError, ValueError, IndexError) as exc:
        raise DataIncompleteError((f"{ctx}.{name}=read_error",)) from exc
    if not math.isfinite(v):
        raise DataIncompleteError((f"{ctx}.{name}=non_finite",))
    return v


def _infer_rule(name: str) -> dict[str, Any] | None:
    n = name.lower()
    if "rsi" in n or "stoch" in n:
        return {"cluster": "exhaustion", "polarity": "high", "warm": 60, "hot": 70, "extreme": 80, "weight": 0.5}
    if n.endswith("_dir"):
        return {"cluster": "trend_break", "polarity": "dir", "weight": 0.5}
    if "candle" in n and "bull" not in n:
        return {"cluster": "reversal", "polarity": "bool", "weight": 0.5}
    if "vol" in n or "atr" in n:
        return {"cluster": "volatility", "polarity": "high", "warm": 0.5, "hot": 0.75, "extreme": 0.9, "weight": 0.3}
    if "cvd" in n or "flow" in n or "delta" in n:
        return {"cluster": "flow", "polarity": "neg", "weight": 0.4}
    return None


def _score_value(
    name: str,
    value: float,
    rules: dict[str, Any],
    *,
    df: Any,
    idx: int,
    close: float,
) -> float:
    pol: str = rules.get("polarity", "high")
    weight = float(rules.get("weight", 1.0))
    if pol == "bool":
        return weight if value >= 0.5 else 0.0
    if pol == "bool_inverse":
        return weight if value < 0.5 else 0.0
    if pol == "dir":
        if value <= -0.5:
            return weight
        if value >= 0.5:
            return 0.0
        return weight * 0.25
    if pol == "neg_slope" and df is not None and df.height >= 3:
        prev = _col(df, name, idx=idx - 1)
        if prev > 0 and value < prev:
            drop = (prev - value) / max(abs(prev), 1e-12)
            return min(weight, weight * min(1.0, drop * 5))
        return 0.0
    if pol == "near_high" and close > 0:
        dist = abs(value - close) / close
        if dist <= 0.005:
            return weight
        if dist <= 0.015:
            return weight * 0.5
        return 0.0
    if pol == "near_level" and close > 0:
        dist = abs(close - value) / close
        if dist <= 0.008:
            return weight
        return 0.0

    warm = float(rules.get("warm", 50))
    hot = float(rules.get("hot", 65))
    extreme = float(rules.get("extreme", 75))

    if pol == "high":
        if value <= warm:
            s = 0.0
        elif value <= hot:
            s = 0.25 + 0.35 * (value - warm) / max(hot - warm, 1e-9)
        elif value <= extreme:
            s = 0.6 + 0.3 * (value - hot) / max(extreme - hot, 1e-9)
        else:
            s = min(1.0, 0.92 + 0.08 * (value - extreme) / max(abs(extreme), 1e-9))
    elif pol == "low":
        if value >= warm:
            s = 0.0
        elif value >= hot:
            s = 0.25 + 0.35 * (warm - value) / max(warm - hot, 1e-9)
        elif value >= extreme:
            s = 0.6 + 0.3 * (hot - value) / max(hot - extreme, 1e-9)
        else:
            s = min(1.0, 0.92 + 0.08 * (extreme - value) / max(abs(extreme), 1e-9))
    else:  # neg
        if value >= warm:
            s = 0.0
        elif value >= hot:
            s = 0.25 + 0.35 * (warm - value) / max(warm - hot, 1e-9)
        elif value >= extreme:
            s = 0.6 + 0.3 * (hot - value) / max(hot - extreme, 1e-9)
        else:
            s = min(1.0, 0.92 + 0.08 * (extreme - value) / max(abs(extreme), 1e-9))
    return s * weight


def _rsi_div_flags(df: Any, *, idx: int = -1) -> dict[str, bool]:
    if df is None or df.is_empty() or "rsi14" not in df.columns:
        return {"bear_div": False, "bull_div": False}
    spec = with_spec_columns(df)
    highs = _pivot_rows(spec, price_column="high", indicator_column="rsi14", pivot="high")
    lows = _pivot_rows(spec, price_column="low", indicator_column="rsi14", pivot="low")
    bear = bull = False
    if len(highs) >= 2:
        o, n = highs[-2], highs[-1]
        bear = n["price"] > o["price"] and n["indicator"] < o["indicator"]
    if len(lows) >= 2:
        o, n = lows[-2], lows[-1]
        bull = n["price"] < o["price"] and n["indicator"] > o["indicator"]
    return {"bear_div": bear, "bull_div": bull}


def _extract_feature_row(df: Any, *, idx: int, ctx: str) -> dict[str, float]:
    if df is None or df.is_empty():
        raise DataIncompleteError((f"{ctx}.no_frame",))
    out: dict[str, float] = {}
    for name in sorted(FULL_INDICATOR_COLUMNS):
        v = _require_col(df, name, idx=idx, ctx=f"{ctx}.{name}")
        out[name] = round(v, 6)
    return out


def score_tf_panel(
    df: Any,
    *,
    tf: str,
    closed: bool,
) -> dict[str, Any]:
    """Score one TF using the full prepared column set (post-completeness gate)."""
    bar = "closed" if closed else "live"
    ctx = f"panel.{tf}.{bar}"
    if df is None or df.is_empty():
        raise DataIncompleteError((f"{ctx}.no_frame",))
    if closed and df.height < 2:
        raise DataIncompleteError((f"{ctx}.closed_bar_unavailable",))
    idx = -2 if closed else -1
    close = _require_col(df, "close", idx=idx, ctx=ctx)
    cluster_sums: dict[str, float] = {c: 0.0 for c in CLUSTERS}
    cluster_w: dict[str, float] = {c: 0.0 for c in CLUSTERS}
    contributions: list[dict[str, Any]] = []
    features = _extract_feature_row(df, idx=idx, ctx=ctx)

    for name, value in features.items():
        rules = INDICATOR_RULES.get(name) or _infer_rule(name)
        if rules is None:
            hist: list[float] = []
            if df.height >= 20 and name in df.columns:
                start = max(0, df.height + idx - 80)
                for i in range(start, df.height + idx):
                    if i < 0:
                        continue
                    hist.append(_col(df, name, idx=i))
            pr = _percentile_rank(hist, value)
            if pr >= 0.88:
                contrib = 0.35
                cluster = "exhaustion"
            elif pr <= 0.12:
                contrib = 0.25
                cluster = "trend_break"
            else:
                continue
            cluster_sums[cluster] += contrib
            cluster_w[cluster] += 1.0
            contributions.append(
                {
                    "indicator": name,
                    "value": round(value, 4),
                    "contrib": round(contrib, 3),
                    "cluster": cluster,
                    "source": "percentile",
                }
            )
            continue

        contrib = _score_value(name, value, rules, df=df, idx=idx, close=close)
        if contrib <= 0:
            continue
        cluster = str(rules.get("cluster", "structure"))
        cluster_sums[cluster] += contrib
        cluster_w[cluster] += float(rules.get("weight", 1.0))
        contributions.append(
            {
                "indicator": name,
                "value": round(value, 4),
                "contrib": round(contrib, 3),
                "cluster": cluster,
                "source": "rule",
            }
        )

    # EMA structure: close below ema20/50
    e20 = _require_col(df, "ema20", idx=idx, ctx=f"{ctx}.ema20")
    e50 = _require_col(df, "ema50", idx=idx, ctx=f"{ctx}.ema50")
    if close and e20 and close < e20:
        w = 0.9
        cluster_sums["trend_break"] += w
        cluster_w["trend_break"] += w
        contributions.append(
            {"indicator": "close_below_ema20", "value": round((close / e20 - 1) * 100, 2), "contrib": w, "cluster": "trend_break", "source": "structure"}
        )
    if close and e50 and close < e50:
        w = 0.7
        cluster_sums["trend_break"] += w * 0.8
        cluster_w["trend_break"] += w
        contributions.append(
            {"indicator": "close_below_ema50", "value": round((close / e50 - 1) * 100, 2), "contrib": round(w * 0.8, 3), "cluster": "trend_break", "source": "structure"}
        )

    candle = _candle_shape(df, idx=idx)
    if candle.get("bearish") and float(candle.get("upper_wick_ratio", 0)) >= 0.35:
        w = 1.0 + float(candle["upper_wick_ratio"])
        cluster_sums["reversal"] += w
        cluster_w["reversal"] += w
        contributions.append(
            {
                "indicator": "upper_wick_rejection",
                "value": candle["upper_wick_ratio"],
                "contrib": round(w, 3),
                "cluster": "reversal",
                "source": "candle",
            }
        )

    div = _rsi_div_flags(df, idx=idx)
    if div["bear_div"]:
        w = 1.4
        cluster_sums["reversal"] += w
        cluster_w["reversal"] += w
        contributions.append(
            {"indicator": "bear_rsi_div", "value": 1.0, "contrib": w, "cluster": "reversal", "source": "pivot"}
        )

    cluster_scores = {
        c: round(cluster_sums[c] / max(cluster_w[c], 1e-9), 3) for c in CLUSTERS
    }
    fuel = round(sum(cluster_scores[c] * w for c, w in zip(CLUSTERS, (0.22, 0.2, 0.18, 0.1, 0.15, 0.15), strict=True)), 3)
    contributions.sort(key=lambda x: float(x["contrib"]), reverse=True)

    return {
        "tf": tf,
        "closed": closed,
        "bars": int(df.height),
        "close": round(close, 6),
        "features": features,
        "feature_count": len(features),
        "cluster_scores": cluster_scores,
        "dump_fuel": fuel,
        "top_contributors": contributions[:25],
        "candle": candle,
        "bear_rsi_div": div["bear_div"],
        "bull_rsi_div": div["bull_div"],
        "ema20": round(e20, 6),
        "ema50": round(e50, 6),
    }


def score_market_layer(pack: dict[str, Any], *, price: float) -> dict[str, Any]:
    """REST positioning / microstructure fuel for dump (all inputs verified finite)."""
    scores: list[tuple[str, float, str]] = []
    oi_series = [float(x) for x in pack["oi_series"]]
    gls_series = [float(x) for x in pack["gls_series"]]
    oi_z = series_z_strict(oi_series, field="oi_series")
    gls_z = series_z_strict(gls_series, field="gls_series")
    if oi_z >= 1.2:
        scores.append(("oi_crowded_long", min(1.0, oi_z / 3.0), f"oi_z={oi_z}"))
    if gls_z >= 1.0:
        scores.append(("gls_crowded_long", min(1.0, gls_z / 2.5), f"gls_z={gls_z}"))
    funding = finite_float(pack["funding"], field="funding")
    if funding > 0.0003:
        scores.append(("funding_long_crowded", min(1.0, funding * 2000), f"fund={funding:.5f}"))
    taker = finite_float(pack["taker_1h"], field="taker_1h")
    if taker < 0.49:
        scores.append(("taker_sell_pressure", min(1.0, (0.5 - taker) * 4), f"taker={taker:.3f}"))
    basis = finite_float(pack["basis_5m"], field="basis_5m")
    if basis < -0.05:
        scores.append(("basis_discount", min(1.0, abs(basis) / 0.3), f"basis={basis:.3f}%"))
    book = pack["book_depth"]
    bid_q = finite_float(book["bid_qty"], field="book.bid_qty")
    ask_q = finite_float(book["ask_qty"], field="book.ask_qty")
    if ask_q > bid_q * 1.15:
        imb = ask_q / bid_q
        scores.append(("depth_ask_heavy", min(1.0, (imb - 1.0) / 0.5), f"ask/bid={imb:.2f}"))
    agg = pack["agg_trades"]
    delta = finite_float(getattr(agg, "delta_ratio"), field="agg.delta_ratio")
    if delta < 0.47:
        scores.append(("agg_sell_delta", min(1.0, (0.5 - delta) * 3), f"agg_delta={delta:.3f}"))

    fuel = round(sum(s for _, s, _ in scores) / max(len(scores), 1), 3) if scores else 0.0
    return {
        "fuel": fuel,
        "signals": [{"id": a, "score": round(b, 3), "detail": c} for a, b, c in scores],
        "oi_z": oi_z,
        "gls_z": gls_z,
        "oi_chg_4h_pct": series_chg_pct_strict(oi_series, field="oi_series"),
        "gls_chg_4h_pct": series_chg_pct_strict(gls_series, field="gls_series"),
        "funding": funding,
        "taker_1h": taker,
        "taker_15m": finite_float(pack["taker_15m"], field="taker_15m"),
        "basis_5m": basis,
        "oi": finite_float(pack["oi"], field="oi"),
        "oi_chg_5m": finite_float(pack["oi_chg_5m"], field="oi_chg_5m"),
        "oi_chg_1h": finite_float(pack["oi_chg_1h"], field="oi_chg_1h"),
        "bid_price": finite_float(book["bid_price"], field="book.bid_price"),
        "ask_price": finite_float(book["ask_price"], field="book.ask_price"),
        "agg_delta_ratio": delta,
    }


SCENARIO_TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "exhaustion_distribution",
        "name": "Exhaustion → distribution",
        "horizon": "15m–2h",
        "needs": {"exhaustion": 0.45, "reversal": 0.35, "trend_break": 0.3},
        "blocks": {},
    },
    {
        "id": "squeeze_breakdown",
        "name": "Squeeze release down",
        "horizon": "30m–4h",
        "needs": {"volatility": 0.35, "trend_break": 0.4, "flow": 0.3},
        "blocks": {"exhaustion": 0.2},
    },
    {
        "id": "cascade_continuation",
        "name": "Cascade continuation",
        "horizon": "5m–1h",
        "needs": {"trend_break": 0.5, "flow": 0.4, "structure": 0.35},
        "blocks": {},
        "lifecycle": ("dump_active", "distribution"),
    },
    {
        "id": "delayed_top_fade",
        "name": "Delayed top fade (HTF div)",
        "horizon": "1h–4h",
        "needs": {"reversal": 0.45, "exhaustion": 0.4},
        "blocks": {},
        "requires_div_htf": True,
    },
    {
        "id": "liquidity_flush",
        "name": "Liquidity flush (OI + sell flow)",
        "horizon": "15m–1h",
        "needs": {"flow": 0.45, "structure": 0.35},
        "blocks": {},
        "market_min": 0.35,
    },
    {
        "id": "flash_rejection",
        "name": "Flash rejection (LTF timing)",
        "horizon": "5m–30m",
        "needs": {"reversal": 0.5, "exhaustion": 0.35},
        "blocks": {},
        "ltf_min": 0.45,
    },
]


def _aggregate_clusters(panels: dict[str, dict[str, Any]]) -> dict[str, float]:
    agg = {c: 0.0 for c in CLUSTERS}
    wsum = 0.0
    for key, panel in panels.items():
        if panel.get("status") == "empty":
            continue
        tf = key.replace("_closed", "")
        w = TF_WEIGHTS.get(tf, 0.1)
        if key.endswith("_closed"):
            w *= 1.15
        for c in CLUSTERS:
            agg[c] += float(panel.get("cluster_scores", {}).get(c, 0)) * w
        wsum += w
    return {c: round(agg[c] / max(wsum, 1e-9), 3) for c in CLUSTERS}


def _cross_tf_alignment(panels: dict[str, dict[str, Any]]) -> float:
    fuels: list[float] = []
    weights: list[float] = []
    for key, panel in panels.items():
        if panel.get("status") == "empty":
            continue
        tf = key.replace("_closed", "")
        w = TF_WEIGHTS.get(tf, 0.1)
        fuels.append(float(panel.get("dump_fuel", 0)))
        weights.append(w)
    if not fuels:
        return 0.0
    mean = sum(f * w for f, w in zip(fuels, weights, strict=True)) / sum(weights)
    hot = sum(1 for f in fuels if f >= 0.35)
    return round(min(1.0, mean * 0.7 + (hot / len(fuels)) * 0.3), 3)


def _collect_evidence(panels: dict[str, dict[str, Any]], *, top_n: int = 30) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, panel in panels.items():
        for row in panel.get("top_contributors") or []:
            rows.append({**row, "tf": key, "contrib": float(row.get("contrib", 0))})
    rows.sort(key=lambda r: r["contrib"], reverse=True)
    return rows[:top_n]


def build_scenarios(
    *,
    panels: dict[str, dict[str, Any]],
    cluster_scores: dict[str, float],
    market: dict[str, Any],
    lifecycle_phase: str,
    levels: dict[str, Any],
    composite: float,
    alignment: float,
) -> list[dict[str, Any]]:
    htf_div = any(
        panels.get(tf, {}).get("bear_rsi_div") for tf in ("4h", "4h_closed", "1h", "1h_closed")
    )
    ltf_fuel = max(
        float(panels.get(k, {}).get("dump_fuel", 0))
        for k in ("1m", "1m_closed", "3m", "3m_closed", "5m", "5m_closed")
    )
    evidence = _collect_evidence(panels)
    scenarios: list[dict[str, Any]] = []

    for tmpl in SCENARIO_TEMPLATES:
        needs: dict[str, float] = tmpl.get("needs", {})
        blocks: dict[str, float] = tmpl.get("blocks", {})
        met: list[str] = []
        missing: list[str] = []
        match = 0.0
        need_total = 0.0
        for c, thr in needs.items():
            need_total += thr
            val = cluster_scores.get(c, 0)
            if val >= thr:
                met.append(f"{c}>={thr:.2f} ({val:.2f})")
                match += min(val, 1.0) * thr
            else:
                missing.append(f"{c} need {thr:.2f} got {val:.2f}")
        blocked = any(cluster_scores.get(c, 0) < thr for c, thr in blocks.items())
        if blocked:
            continue
        lc = tmpl.get("lifecycle")
        if lc and lifecycle_phase not in lc:
            missing.append(f"lifecycle need {lc} got {lifecycle_phase}")
        if tmpl.get("requires_div_htf") and not htf_div:
            missing.append("htf_bear_rsi_div")
        if tmpl.get("market_min") and float(market.get("fuel", 0)) < float(tmpl["market_min"]):
            missing.append(f"market_fuel<{tmpl['market_min']}")
        if tmpl.get("ltf_min") and ltf_fuel < float(tmpl["ltf_min"]):
            missing.append(f"ltf_fuel<{tmpl['ltf_min']}")

        coverage = match / max(need_total, 1e-9)
        conf = int(
            min(
                100,
                max(
                    0,
                    coverage * 55
                    + alignment * 25
                    + composite * 15
                    + float(market.get("fuel", 0)) * 5,
                ),
            )
        )
        if not met and conf < 25:
            continue
        scenarios.append(
            {
                "id": tmpl["id"],
                "name": tmpl["name"],
                "horizon": tmpl["horizon"],
                "confidence": conf,
                "triggers_met": met,
                "triggers_missing": missing,
                "evidence": [e for e in evidence if e["contrib"] >= 0.5][:15],
                "levels": {
                    "entry_zone": levels.get("entry_zone"),
                    "stop": levels.get("stop"),
                    "tp1": levels.get("tp1"),
                    "tp2": levels.get("tp2"),
                    "rr": levels.get("rr"),
                },
            }
        )
    scenarios.sort(key=lambda s: s["confidence"], reverse=True)
    return scenarios


def _fib_levels(high: float, low: float) -> dict[str, float]:
    leg = high - low
    return {
        "ext_1272": round(high + leg * 0.272, 6),
        "ext_1618": round(high + leg * 0.618, 6),
        "ret_236": round(high - leg * 0.236, 6),
        "ret_382": round(high - leg * 0.382, 6),
        "ret_50": round(high - leg * 0.5, 6),
    }


async def _require_fetch(coro: Any, field: str) -> Any:
    try:
        result = await coro
    except DEFENSIVE_EXC as exc:
        raise DataIncompleteError((f"fetch.{field}=exception:{type(exc).__name__}",)) from exc
    if result is None:
        raise DataIncompleteError((f"fetch.{field}=null",))
    return result


async def fetch_rest_pack(client: BinanceFuturesMarketData, symbol: str) -> dict[str, Any]:
    """Fetch full REST pack — any failure aborts the tick (no silent nulls)."""
    pack = {
        "oi": await _require_fetch(client.fetch_open_interest(symbol), "oi"),
        "oi_chg_5m": await _require_fetch(client.fetch_open_interest_change(symbol, period="5m"), "oi_chg_5m"),
        "oi_chg_1h": await _require_fetch(client.fetch_open_interest_change(symbol, period="1h"), "oi_chg_1h"),
        "ls_5m": await _require_fetch(client.fetch_long_short_ratio(symbol, period="5m"), "ls_5m"),
        "ls_1h": await _require_fetch(client.fetch_long_short_ratio(symbol, period="1h"), "ls_1h"),
        "top_ls_5m": await _require_fetch(client.fetch_top_position_ls_ratio(symbol, period="5m"), "top_ls_5m"),
        "top_ls_1h": await _require_fetch(client.fetch_top_position_ls_ratio(symbol, period="1h"), "top_ls_1h"),
        "global_ls_5m": await _require_fetch(client.fetch_global_ls_ratio(symbol, period="5m"), "global_ls_5m"),
        "global_ls_1h": await _require_fetch(client.fetch_global_ls_ratio(symbol, period="1h"), "global_ls_1h"),
        "taker_5m": await _require_fetch(client.fetch_taker_ratio(symbol, period="5m"), "taker_5m"),
        "taker_15m": await _require_fetch(client.fetch_taker_ratio(symbol, period="15m"), "taker_15m"),
        "taker_1h": await _require_fetch(client.fetch_taker_ratio(symbol, period="1h"), "taker_1h"),
        "funding": await _require_fetch(client.fetch_funding_rate(symbol), "funding"),
        "basis_5m": await _require_fetch(client.fetch_basis(symbol, period="5m"), "basis_5m"),
        "agg_trades": await _require_fetch(client.fetch_agg_trade_snapshot(symbol, limit=100), "agg_trades"),
        "book_depth": await _require_fetch(
            client.fetch_order_book_depth_snapshot(symbol, limit=100), "book_depth"
        ),
        "oi_series": await _require_fetch(
            client.fetch_open_interest_series(symbol, period="5m", limit=48), "oi_series"
        ),
        "gls_series": await _require_fetch(
            client.fetch_global_ls_series(symbol, period="5m", limit=48), "gls_series"
        ),
    }
    depth = pack["book_depth"]
    if not isinstance(depth, dict) or depth.get("bid_price") is None:
        raise DataIncompleteError(("fetch.book_depth=invalid_shape",))
    return pack


def _incomplete_tick(
    sym: str,
    report: CompletenessReport,
    *,
    prior: TickState | None,
) -> tuple[dict[str, Any], TickState]:
    ts = datetime.now(UTC).isoformat()
    row: dict[str, Any] = {
        "ts": ts,
        "symbol": sym,
        "analyzable": False,
        "verdict": "DATA_INCOMPLETE",
        "violation_count": len(report.violations),
        "violations": list(report.violations),
        "data_audit": report.details,
        "composite_dump_score": 0.0,
        "cross_tf_alignment": 0.0,
        "cluster_scores": {c: 0.0 for c in CLUSTERS},
        "scenarios": [],
        "top_evidence": [],
        "feature_matrix": {},
        "market_layer": {},
        "delta_60s": _delta_vs_prior(prior, {c: 0.0 for c in CLUSTERS}, 0.0),
    }
    return row, prior or TickState()


def _delta_vs_prior(state: TickState | None, cluster_scores: dict[str, float], composite: float) -> dict[str, Any]:
    if state is None or not state.ts:
        return {"first_tick": True}
    deltas = {c: round(cluster_scores.get(c, 0) - state.cluster_scores.get(c, 0), 3) for c in CLUSTERS}
    return {
        "composite_delta": round(composite - state.composite, 3),
        "cluster_deltas": deltas,
        "warming": [c for c, d in deltas.items() if d >= 0.05],
        "cooling": [c for c, d in deltas.items() if d <= -0.05],
    }


async def run_tick(
    client: BinanceFuturesMarketData,
    symbol: str,
    *,
    prior: TickState | None = None,
) -> tuple[dict[str, Any], TickState]:
    sym = symbol.upper()
    settings = load_settings()
    minimums = min_required_bars(
        min_bars_15m=settings.filters.min_bars_15m,
        min_bars_1h=settings.filters.min_bars_1h,
        min_bars_4h=settings.filters.min_bars_4h,
    )
    limits = {
        "1m": min(1500, max(1440, kline_fetch_limit(int(minimums.get("5m", 300)), "5m") * 2)),
        "3m": 480,
        "5m": kline_fetch_limit(int(minimums.get("5m", 300)), "5m"),
        "15m": kline_fetch_limit(int(minimums.get("15m", 400)), "15m"),
        "1h": kline_fetch_limit(int(minimums.get("1h", 400)), "1h"),
        "4h": kline_fetch_limit(int(minimums.get("4h", 200)), "4h"),
    }

    try:
        ticker_raw = await _require_fetch(client.fetch_ticker_24h(), "ticker_24h")
        ticker = next((t for t in ticker_raw if str(t.get("symbol")) == sym), None)
        if ticker is None:
            return _incomplete_tick(
                sym,
                CompletenessReport.fail([f"ticker.{sym}=missing"]),
                prior=prior,
            )

        kline_map: dict[str, Any] = {}
        for tf in TF_KEYS:
            kline_map[tf] = await _require_fetch(
                client.fetch_klines_cached(sym, tf, limit=limits[tf]),
                f"klines.{tf}",
            )

        pack = await fetch_rest_pack(client, sym)
        book = pack["book_depth"]

        exchange = await _require_fetch(client.fetch_exchange_symbols(), "exchange_symbols")
        meta = next((r for r in exchange if r.symbol == sym), None)
        if meta is None:
            return _incomplete_tick(
                sym,
                CompletenessReport.fail([f"exchange.{sym}=missing"]),
                prior=prior,
            )

        price = finite_float(ticker.get("last_price"), field="ticker.last_price")
        item = UniverseSymbol(
            symbol=sym,
            base_asset=meta.base_asset,
            quote_asset=meta.quote_asset,
            contract_type=meta.contract_type,
            status=meta.status,
            onboard_date_ms=meta.onboard_date_ms,
            quote_volume=finite_float(ticker.get("quote_volume"), field="ticker.quote_volume"),
            price_change_pct=finite_float(ticker.get("price_change_percent"), field="ticker.chg_24h"),
            last_price=price,
            shortlist_bucket="beat_dump_lab",
            seed_source="beat_dump_lab",
            strategy_fits=(),
        )
        frames = SymbolFrames(
            symbol=sym,
            df_15m=kline_map["15m"],
            df_1h=kline_map["1h"],
            df_5m=kline_map["5m"],
            df_4h=kline_map["4h"],
            bid_price=finite_float(book["bid_price"], field="book.bid_price"),
            ask_price=finite_float(book["ask_price"], field="book.ask_price"),
            bid_qty=finite_float(book["bid_qty"], field="book.bid_qty"),
            ask_qty=finite_float(book["ask_qty"], field="book.ask_qty"),
            frame_source_flags=("beat_dump_lab",),
        )
        if prepare_symbol(item, frames, minimums=minimums, settings=settings) is None:
            return _incomplete_tick(
                sym,
                CompletenessReport.fail([f"prepare_symbol.{sym}=insufficient_history"]),
                prior=prior,
            )

        work: dict[str, Any] = {}
        for tf in TF_KEYS:
            work[tf] = _prepare_frame(kline_map[tf], active_groups=None)

        completeness = audit_beat_dump_tick(
            symbol=sym,
            ticker=ticker,
            kline_map=kline_map,
            prepared_map=work,
            pack=pack,
            settings=settings,
            tf_keys=TF_KEYS,
        )
        if not completeness.complete:
            return _incomplete_tick(sym, completeness, prior=prior)
    except DataIncompleteError as exc:
        return _incomplete_tick(sym, CompletenessReport.fail(list(exc.violations)), prior=prior)

    panels: dict[str, dict[str, Any]] = {}
    feature_matrix: dict[str, dict[str, float]] = {}
    total_features = 0
    for tf in TF_KEYS:
        df = work.get(tf)
        live = score_tf_panel(df, tf=tf, closed=False)
        closed = score_tf_panel(df, tf=tf, closed=True)
        panels[tf] = live
        panels[f"{tf}_closed"] = closed
        feature_matrix[tf] = live.get("features") or {}
        feature_matrix[f"{tf}_closed"] = closed.get("features") or {}
        total_features += int(live.get("feature_count", 0)) + int(closed.get("feature_count", 0))

    cluster_scores = _aggregate_clusters(panels)
    alignment = _cross_tf_alignment(panels)
    market = score_market_layer(pack, price=price)
    composite = round(
        sum(cluster_scores[c] * w for c, w in zip(CLUSTERS, (0.22, 0.2, 0.18, 0.1, 0.15, 0.15), strict=True)) * 0.75
        + alignment * 0.15
        + float(market.get("fuel", 0)) * 0.1,
        3,
    )

    df_4h = work.get("4h")
    df_1h = work.get("1h")
    impulse_high = max(
        _require_col(df_4h, "high", idx=-2, ctx="impulse.4h.high"),
        _require_col(df_1h, "high", idx=-1, ctx="impulse.1h.high"),
        price,
    )
    impulse_low = min(
        _require_col(df_4h, "low", idx=-2, ctx="impulse.4h.low"),
        _require_col(df_1h, "low", idx=-1, ctx="impulse.1h.low"),
        price,
    )
    fib = _fib_levels(impulse_high, impulse_low)
    tf_snap = {
        tf: {
            "close": panels.get(f"{tf}_closed", {}).get("close") or panels.get(tf, {}).get("close"),
            "rsi14": (panels.get(f"{tf}_closed") or panels.get(tf) or {}).get("features", {}).get("rsi14"),
            "bearish_rsi_div": panels.get(f"{tf}_closed", panels.get(tf, {})).get("bear_rsi_div"),
            "candle": panels.get(f"{tf}_closed", panels.get(tf, {})).get("candle"),
        }
        for tf in TF_KEYS
    }
    sess_hi = impulse_high
    sess_lo = impulse_low
    df_1m = work.get("1m")
    if df_1m is not None and not df_1m.is_empty():
        look = min(1440, df_1m.height)
        sess_hi = max(float(x) for x in df_1m["high"].to_list()[-look:])
        sess_lo = min(float(x) for x in df_1m["low"].to_list()[-look:])
    pos = (price - sess_lo) / (sess_hi - sess_lo) if sess_hi > sess_lo else 0.5
    lifecycle = assess_hunt_lifecycle(
        price=price,
        hunt_high=impulse_high,
        hunt_low=impulse_low,
        session={"high_24h": sess_hi, "low_24h": sess_lo, "pos_in_range": pos},
        tf=tf_snap,
        market={
            "taker_1h": pack.get("taker_1h"),
            "oi": pack.get("oi"),
            "oi_z": market.get("oi_z"),
            "gls_z": market.get("gls_z"),
        },
    )
    atr15 = _require_col(work["15m"], "atr14", idx=-1, ctx="levels.atr15")
    levels = structural_short_levels(
        price=price,
        impulse_high=impulse_high,
        impulse_low=impulse_low,
        fib=fib,
        atr15=atr15,
        local_support=lifecycle.local_support,
        local_resistance=lifecycle.local_resistance,
    )
    scenarios = build_scenarios(
        panels=panels,
        cluster_scores=cluster_scores,
        market=market,
        lifecycle_phase=lifecycle.phase.value,
        levels=levels,
        composite=composite,
        alignment=alignment,
    )
    delta = _delta_vs_prior(prior, cluster_scores, composite)
    ts = datetime.now(UTC).isoformat()

    verdict: Literal["DUMP_IMMINENT", "DUMP_SETUP", "WATCH", "NO_EDGE", "DATA_INCOMPLETE"] = "NO_EDGE"
    if composite >= 0.55 and alignment >= 0.45:
        verdict = "DUMP_IMMINENT" if composite >= 0.7 else "DUMP_SETUP"
    elif composite >= 0.38:
        verdict = "WATCH"

    row: dict[str, Any] = {
        "ts": ts,
        "symbol": sym,
        "price": price,
        "chg_24h_pct": round(float(ticker.get("price_change_percent") or 0), 2),
        "vol_24h_m": round(float(ticker.get("quote_volume") or 0) / 1e6, 2),
        "indicator_columns_per_tf": len(feature_matrix.get("1h") or {}),
        "total_feature_points": total_features,
        "feature_matrix": feature_matrix,
        "cluster_scores": cluster_scores,
        "analyzable": True,
        "tf_dump_fuel": {k: panels[k]["dump_fuel"] for k in panels},
        "cross_tf_alignment": alignment,
        "market_layer": market,
        "rest_pack_summary": {
            "oi": market["oi"],
            "oi_chg_5m": market["oi_chg_5m"],
            "oi_chg_1h": market["oi_chg_1h"],
            "funding": market["funding"],
            "taker_1h": market["taker_1h"],
            "taker_15m": market["taker_15m"],
            "basis_5m": market["basis_5m"],
            "oi_z": market["oi_z"],
            "gls_z": market["gls_z"],
            "oi_series_len": len(pack["oi_series"]),
            "gls_series_len": len(pack["gls_series"]),
            "agg_delta_ratio": market["agg_delta_ratio"],
            "bid_price": market["bid_price"],
            "ask_price": market["ask_price"],
        },
        "data_audit": completeness.details,
        "lifecycle": {
            "phase": lifecycle.phase.value,
            "bias": lifecycle.recommended_bias,
            "fall_from_high_pct": round(lifecycle.fall_from_high_pct, 2),
            "local_support": lifecycle.local_support,
            "local_resistance": lifecycle.local_resistance,
            "short_confirm_ok": lifecycle.short_confirm_ok,
            "reasons": list(lifecycle.reasons),
        },
        "levels": levels,
        "composite_dump_score": composite,
        "verdict": verdict,
        "top_evidence": _collect_evidence(panels, top_n=20),
        "scenarios": scenarios,
        "delta_60s": delta,
    }

    new_state = TickState(
        ts=ts,
        cluster_scores=dict(cluster_scores),
        composite=composite,
    )
    return row, new_state


def make_client() -> BinanceFuturesMarketData:
    settings = load_settings()
    return BinanceFuturesMarketData(
        binance_client=BinanceClientImpl(
            rest_timeout_seconds=60.0,
            futures_data_request_limit_per_5m=settings.runtime.futures_data_request_limit_per_5m,
            proxy_url=settings.network.proxy_url,
            trust_env=settings.network.trust_env,
        ),
    )
