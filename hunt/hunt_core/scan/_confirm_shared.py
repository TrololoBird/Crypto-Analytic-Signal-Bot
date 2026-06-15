"""Shared confirm/fuel helpers (wave 3C)."""
from __future__ import annotations

import html
import json
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import polars as pl

from hunt_core.data.universe import watchlist_flags
from hunt_core.domain.market_regime import HuntCalibratedParams
from hunt_core.params.store import (
    confirm_thresholds,
    dump_fast_confirm_enabled,
    effective_hunt_params,
    entry_confirm_tf,
    liquidation_thresholds,
    listings_thresholds,
    orderflow_thresholds,
    scoring_thresholds,
)
from hunt_core.paths import ADAPTIVE_THRESHOLDS, DUMP_HUNT_ALERT_STATE, EWMA_THRESHOLDS, IGNITION_STATE
from hunt_core.errors import optional_finite_float, require_mark_price


def _htf_bias_override(*args, **kwargs):
    from hunt_core.regime.leg_fsm import htf_bias_override
    return htf_bias_override(*args, **kwargs)


from hunt_core.scan.predump_dump_hunt import DumpHuntTier

# Cluster caps prevent correlated triggers (RSI15+RSI1H+div+funding) inflating fuel.
_FUEL_CLUSTER_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "exhaustion",
        (
            "rsi15_overbought",
            "rsi1h_overbought",
            "rsi15_oversold",
            "rsi1h_oversold",
            "bear_div",
            "bull_div",
            "hidden_div",
            "squeeze_at_boundary",
            "rsi_trendline",
            "macd_div",
            "rejection",
            "bounce",
            "overbought",
            "oversold",
            "wick",
            "at_fib",
            "extended",
            "crowded_long_funding",
            "crowded_short_funding",
            "mom10_",
            "kdj_j_",
            "psy_",
            "ts_rank_",
            "bias_",
            "extreme_move_",
            "volume_spike_",
            "sharpe_",
            "low_sharpe",
            "high_sharpe",
        ),
    ),
    (
        "structure",
        (
            "lost_support",
            "below_impulse",
            "broke_resistance",
            "deep_below",
            "distribution",
            "close_below",
            "close_above",
            "ema200_confluence",
            "double_bottom_",
            "head_and_shoulders_",
            # POC is the methodology's primary level — aligned POC is structural fuel.
            # Only the aligned form (poc_contra stays raw-only so it can't add + fuel).
            "poc_aligned",
        ),
    ),
    (
        "flow",
        (
            "taker_",
            "oi_flush",
            "oi_build",
            "microprice",
            "global_ls",
            "crowded_longs",
            "crowded_shorts",
            "oi_build_z",
            "oi_flush_z",
            "ws_cvd",
            "ws_depth",
        ),
    ),
    (
        "micro",
        (
            "ws_liq",
            "ws_taker",
            "spot_lead",
            "regime_",
            "bid_wall",
            "ask_wall",
            "zone_imb",
            "volume_regime",
            # Liquidation-heatmap triggers — the dump-hunt edge — now feed micro fuel
            # (previously raw-only → diluted via the ×0.55 blend floor).
            "liq_cluster",
            "liq_cascade",
            "long_squeeze",
            "short_squeeze",
        ),
    ),
)

_WALL_MAX_DISTANCE_PCT = 2.0
_ZONE_IMB_THRESHOLD = 0.15
_WALL_FUEL_SCORE = 6.0
_ZONE_FUEL_SCORE = 4.0
_WS_DEPTH_IMB_THRESHOLD = 0.10
_WS_DEPTH_FUEL = 6.0
_CVD_DIV_PRICE_MIN_PCT = 0.08
_CVD_DIV_FUEL_5M = 10.0
_CVD_DIV_FUEL_1M = 6.0
_STALE_15M_MAX_GAP_MS = 15 * 60 * 1000

_INITIATION_HARD_DUMP = frozenset(
    {
        "5m_close_below_support",
        "15m_close_below_support",
        "1m_5m_bear_cascade",
        "5m_rejection_exhaustion",
        "ws_liq_cascade_long_flush",
        "pp_short_break",
    }
)

_INITIATION_HARD_LONG = frozenset(
    {
        "5m_close_above_resistance",
        "15m_close_above_resistance",
        "1m_5m_bull_cascade",
        "5m_bounce_oversold",
        "pp_long_break",
    }
)

_STRUCTURAL_CONFIRM_DUMP = frozenset(
    {
        "5m_close_below_support",
        "15m_close_below_support",
        "1m_5m_bear_cascade",
    }
)

_ENTRY_TF_STALE_KEYS = {
    "15m": "stale_15m",
}

# Penalty-only triggers: subtract raw score but must not inflate cluster fuel.
_FUEL_PENALTY_TRIGGERS = frozenset(
    {
        "contra_trend_warning_short",
        "contra_trend_warning_long",
    }
)


def _closed_tf_block(tf: dict[str, Any], interval: str) -> dict[str, Any]:
    key = interval if interval.endswith("_closed") else f"{interval}_closed"
    block = tf.get(key) or {}
    return block if isinstance(block, dict) else {}


def _closed_bar_available(tf: dict[str, Any], interval: str) -> bool:
    return bool(_closed_tf_block(tf, interval).get("closed_bar"))


def _closed_tf_close(tf: dict[str, Any], interval: str) -> float | None:
    block = _closed_tf_block(tf, interval)
    if not block.get("closed_bar"):
        return None
    candle = block.get("candle") if isinstance(block.get("candle"), dict) else {}
    try:
        if candle.get("close") is not None:
            return float(candle.get("close"))
        raw = block.get("close")
        if raw is None:
            return None
        val = float(raw)
        return val if val > 0 else None
    except (TypeError, ValueError):
        return None


def _closed_candle(tf: dict[str, Any], interval: str) -> dict[str, Any]:
    block = _closed_tf_block(tf, interval)
    if not block.get("closed_bar"):
        return {}
    candle = block.get("candle")
    return candle if isinstance(candle, dict) else {}


def _required_closed_rsi(tf: dict[str, Any], interval: str) -> float | None:
    """RSI from closed frame only — None when missing (no silent default)."""
    block = _closed_tf_block(tf, interval)
    if not block.get("closed_bar"):
        return None
    raw = block.get("rsi14")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _entry_tf_stale(tf: dict[str, Any], interval: str) -> bool:
    stale_key = _ENTRY_TF_STALE_KEYS.get(interval)
    return bool(tf.get(stale_key)) if stale_key else False


def _structural_close_break_triggers(
    *,
    direction: Literal["long", "short"],
    level: float,
    tf: dict[str, Any],
    entry_tf: str,
) -> list[str]:
    """Closed-bar structural breaks on entry_confirm_tf (+ independent 15m secondary)."""
    hard: list[str] = []
    if level <= 0:
        return hard
    # Primary entry-TF break — gated only on its OWN bar availability.
    entry_close = _closed_tf_close(tf, entry_tf)
    if entry_close is not None and entry_close > 0 and not _entry_tf_stale(tf, entry_tf):
        if direction == "short" and entry_close < level:
            hard.append(f"{entry_tf}_close_below_support")
        elif direction == "long" and entry_close > level:
            hard.append(f"{entry_tf}_close_above_resistance")
    # 15m secondary is INDEPENDENT of the (faster) primary bar — a missing/stale 1m
    # bar must not drop the 15m structural confirm (dump entry_tf=1m regression).
    if entry_tf != "15m":
        r15_close = _closed_tf_close(tf, "15m")
        if direction == "short" and r15_close and r15_close < level and not tf.get("stale_15m"):
            hard.append("15m_close_below_support")
        elif direction == "long" and r15_close and r15_close > level and not tf.get("stale_15m"):
            hard.append("15m_close_above_resistance")
    return hard


def _is_structural_confirm_trigger(trigger: str) -> bool:
    t = str(trigger)
    if t.endswith("_score_only"):
        return False
    if "cascade" in t:
        return True
    return "close_below_support" in t or "close_above_resistance" in t or t in {
        "pp_short_break",
        "pp_long_break",
    }


def _resolve_lifecycle_4h(setup: dict[str, Any]) -> str:
    direct = setup.get("lifecycle_4h") or setup.get("phase_4h")
    if direct:
        return str(direct)
    lc = setup.get("lifecycle")
    if isinstance(lc, dict):
        return str(lc.get("lifecycle_4h") or lc.get("phase_4h") or "")
    return ""

_HIDDEN_DIV_FUEL = 10.0
_CHART_PATTERN_FUEL = 5.0
_PROKOL_FUEL_PENALTY = 8.0
_PROKOL_TF_TRAP_PENALTY = 12.0
_HIDDEN_DIV_TFS = frozenset({"1h", "4h"})
_CHART_PATTERN_TFS = frozenset({"1h", "4h"})
_POLARS_TA_TF_KEYS = ("15m_closed", "15m", "1h", "4h")
_MOM_ALIGNED_FUEL = 4.0
_KDJ_EXHAUST_FUEL = 6.0
_KDJ_POST_DUMP_FUEL = 6.0
_PSY_EUPHORIA_FUEL = 8.0
_PSY_PANIC_FUEL = 8.0
_TS_RANK_EXHAUST_FUEL = 4.0
_BIAS_EXTREME_FUEL = 6.0
_EXTREME_MOVE_Z = 2.5
_EXTREME_MOVE_FUEL = 8.0
_VOLUME_SPIKE_PERCENTILE = 95.0
_VOLUME_SPIKE_FUEL = 8.0
_CONTRA_TREND_PENALTY = 5.0
_CONTRA_TREND_SLOPE_MIN = 0.05
_SHARPE_PHASE_FUEL = 4.0
_SHARPE_LOW_THRESHOLD = 0.0
_SHARPE_HIGH_THRESHOLD = 1.0
_VOLUME_REGIME_BREAK_FUEL = 5.0
_CANDLE_REVERSAL_FUEL = 6.0
_CANDLE_STAR_FUEL = 10.0
_CANDLE_LEVEL_PCT = 0.02
_CANDLE_TF_KEYS = ("15m_closed", "5m_closed")
_EXHAUSTION_PHASES = frozenset(
    {
        "exhaustion_at_high",
        "exhaustion_watch",
        "distribution",
    }
)
_POST_DUMP_PHASES = frozenset(
    {
        "post_dump_bounce",
        "recovery",
        "accumulation",
    }
)


def _resolve_ema200(setup: dict[str, Any], tf: dict[str, Any] | None) -> float:
    direct = setup.get("ema200")
    if direct is not None:
        try:
            val = float(direct)
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass
    blocks = tf or setup.get("timeframes") or {}
    if not isinstance(blocks, dict):
        return 0.0
    for key in ("1h", "4h", "15m_closed", "15m"):
        block = blocks.get(key) or {}
        if not isinstance(block, dict):
            continue
        raw = block.get("ema200")
        if raw is None:
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if val > 0:
            return val
    return 0.0


def ema200_confluence_trigger(
    *,
    direction: str,
    price: float,
    ema200: float,
    symbol: str = "",
) -> str | None:
    """+8 fuel when price hugs EMA200 in the setup direction (Phase 5A)."""
    sc = scoring_thresholds(symbol)
    confluence_pct = float(sc.get("ema200_confluence_pct", 0.005))
    if price <= 0 or ema200 <= 0:
        return None
    if abs(price - ema200) / price >= confluence_pct:
        return None
    if direction == "long" and price >= ema200:
        return "ema200_confluence_support"
    if direction == "short" and price <= ema200:
        return "ema200_confluence_resistance"
    return None


def _htf_blocks(tf: dict[str, Any] | None, keys: frozenset[str]) -> list[tuple[str, dict[str, Any]]]:
    blocks = tf or {}
    if not isinstance(blocks, dict):
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    for key in keys:
        block = blocks.get(key) or {}
        if isinstance(block, dict) and block.get("status") != "empty":
            out.append((key, block))
    return out


def _price_at_structure_boundary(
    block: dict[str, Any],
    *,
    direction: str,
    symbol: str = "",
) -> bool:
    """Squeeze compression at BB/Donchian edge (Phase 5B)."""
    sc = scoring_thresholds(symbol)
    bb_boundary = float(sc.get("squeeze_bb_boundary", 0.08))
    bb = block.get("bb_pct_b")
    if bb is not None:
        bb_f = float(bb)
        if direction == "long" and bb_f <= bb_boundary:
            return True
        if direction == "short" and bb_f >= 1.0 - bb_boundary:
            return True
    close = float(block.get("close") or 0)
    if close <= 0:
        return False
    tol = close * 0.003
    d_hi = block.get("donchian_high20")
    d_lo = block.get("donchian_low20")
    if direction == "long" and d_lo is not None:
        return abs(close - float(d_lo)) <= tol
    if direction == "short" and d_hi is not None:
        return abs(close - float(d_hi)) <= tol
    return False


def squeeze_at_boundary_trigger(
    *,
    direction: str,
    tf: dict[str, Any] | None,
    symbol: str = "",
) -> str | None:
    """+8 fuel when TTM squeeze is on and price sits at a structure boundary."""
    for tf_key, block in _htf_blocks(tf, _HIDDEN_DIV_TFS):
        if not block.get("squeeze_on"):
            continue
        if _price_at_structure_boundary(block, direction=direction, symbol=symbol):
            return f"squeeze_at_boundary_{tf_key}"
    return None


def hidden_div_trigger(
    *,
    direction: str,
    tf: dict[str, Any] | None,
) -> str | None:
    """+10 fuel on hidden Stoch divergence (1h/4h only, Phase 5C)."""
    flag = "bullish_hidden_stoch_div" if direction == "long" else "bearish_hidden_stoch_div"
    for tf_key, block in _htf_blocks(tf, _HIDDEN_DIV_TFS):
        if block.get(flag):
            return f"hidden_div_{tf_key}"
    return None


def chart_pattern_trigger(
    *,
    direction: str,
    tf: dict[str, Any] | None,
) -> list[str]:
    """+5 fuel per aligned HTF chart pattern (1h/4h only, Phase 6A)."""
    out: list[str] = []
    for tf_key, block in _htf_blocks(tf, _CHART_PATTERN_TFS):
        if direction == "long":
            pat = block.get("double_bottom")
            if isinstance(pat, dict) and pat.get("pattern") == "double_bottom":
                out.append(f"double_bottom_{tf_key}")
        elif direction == "short":
            pat = block.get("head_and_shoulders")
            if isinstance(pat, dict) and pat.get("pattern") == "head_and_shoulders":
                out.append(f"head_and_shoulders_{tf_key}")
    return out


def _apply_fuel_trigger(
    setup: dict[str, Any],
    *,
    score_key: str,
    trigger: str | None,
    fuel: float,
) -> None:
    if not trigger:
        return
    triggers = list(setup.get("triggers") or [])
    if trigger in triggers:
        return
    triggers.append(trigger)
    setup["triggers"] = triggers
    setup[score_key] = round(float(setup.get(score_key) or 0) + fuel, 1)


def _apply_squeeze_at_boundary(
    setup: dict[str, Any],
    *,
    direction: str,
    score_key: str,
    tf: dict[str, Any] | None,
    symbol: str = "",
) -> None:
    sc = scoring_thresholds(symbol)
    _apply_fuel_trigger(
        setup,
        score_key=score_key,
        trigger=squeeze_at_boundary_trigger(direction=direction, tf=tf, symbol=symbol),
        fuel=float(sc.get("squeeze_boundary_fuel", 8.0)),
    )


def _apply_hidden_div_fuel(
    setup: dict[str, Any],
    *,
    direction: str,
    score_key: str,
    tf: dict[str, Any] | None,
) -> None:
    _apply_fuel_trigger(
        setup,
        score_key=score_key,
        trigger=hidden_div_trigger(direction=direction, tf=tf),
        fuel=_HIDDEN_DIV_FUEL,
    )


def _apply_chart_pattern_fuel(
    setup: dict[str, Any],
    *,
    direction: str,
    score_key: str,
    tf: dict[str, Any] | None,
) -> None:
    for trigger in chart_pattern_trigger(direction=direction, tf=tf):
        _apply_fuel_trigger(
            setup,
            score_key=score_key,
            trigger=trigger,
            fuel=_CHART_PATTERN_FUEL,
        )


def _resolve_tf_indicator(
    setup: dict[str, Any],
    tf: dict[str, Any] | None,
    key: str,
) -> float | None:
    """Read polars-ta column from setup dict or TF snapshot blocks (Phase 8A)."""
    direct = setup.get(key)
    if direct is not None:
        try:
            return float(direct)
        except (TypeError, ValueError):
            pass
    blocks = tf or setup.get("timeframes") or {}
    if not isinstance(blocks, dict):
        return None
    for tf_key in _POLARS_TA_TF_KEYS:
        block = blocks.get(tf_key) or {}
        if not isinstance(block, dict) or block.get("status") == "empty":
            continue
        raw = block.get(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _resolve_kdj_j(setup: dict[str, Any], tf: dict[str, Any] | None) -> float | None:
    j = _resolve_tf_indicator(setup, tf, "kdj_j14")
    if j is not None:
        return j
    k = _resolve_tf_indicator(setup, tf, "kdj_k14")
    d = _resolve_tf_indicator(setup, tf, "kdj_d14")
    if k is None or d is None:
        return None
    return 3.0 * k - 2.0 * d


def _lifecycle_phase(setup: dict[str, Any]) -> str:
    return str(setup.get("lifecycle_phase") or setup.get("phase") or "")


def polars_ta_fuel_triggers(
    setup: dict[str, Any],
    *,
    direction: Literal["long", "short"],
    tf: dict[str, Any] | None = None,
) -> list[tuple[str, float]]:
    """polars-ta / pinned-panel fuel overlays when columns are present (Phase 8A)."""
    out: list[tuple[str, float]] = []
    mom = _resolve_tf_indicator(setup, tf, "mom10")
    if mom is not None:
        if direction == "short" and mom < 0:
            out.append(("mom10_bear_aligned", _MOM_ALIGNED_FUEL))
        elif direction == "long" and mom > 0:
            out.append(("mom10_bull_aligned", _MOM_ALIGNED_FUEL))

    kdj_j = _resolve_kdj_j(setup, tf)
    phase = _lifecycle_phase(setup)
    if kdj_j is not None:
        if direction == "short" and kdj_j > 100.0:
            out.append(("kdj_j_exhaustion_short", _KDJ_EXHAUST_FUEL))
        elif direction == "long" and kdj_j < 0.0 and phase in _POST_DUMP_PHASES:
            out.append(("kdj_j_post_dump_long", _KDJ_POST_DUMP_FUEL))

    psy = _resolve_tf_indicator(setup, tf, "psy12")
    if psy is not None:
        if direction == "short" and psy > 83.0:
            out.append(("psy_euphoria_short", _PSY_EUPHORIA_FUEL))
        elif direction == "long" and psy < 17.0:
            out.append(("psy_panic_long", _PSY_PANIC_FUEL))

    ts_rank = _resolve_tf_indicator(setup, tf, "wq_ts_rank_close20")
    if (
        direction == "short"
        and ts_rank is not None
        and ts_rank > 0.95
        and phase in _EXHAUSTION_PHASES
    ):
        out.append(("ts_rank_exhaustion_top", _TS_RANK_EXHAUST_FUEL))

    bias = _resolve_tf_indicator(setup, tf, "bias6")
    if bias is not None:
        if direction == "short" and bias > 8.0:
            out.append(("bias_overbought_short", _BIAS_EXTREME_FUEL))
        elif direction == "long" and bias < -8.0:
            out.append(("bias_oversold_long", _BIAS_EXTREME_FUEL))

    return out


def research_fuel_triggers(
    setup: dict[str, Any],
    *,
    direction: Literal["long", "short"],
    tf: dict[str, Any] | None = None,
) -> list[tuple[str, float]]:
    """polars-ols / polars-trading / polars-ds fuel overlays (Phases 11A–11C)."""
    out: list[tuple[str, float]] = []
    slope = _resolve_tf_indicator(setup, tf, "trend_slope_20")
    if slope is not None:
        if direction == "short" and slope > _CONTRA_TREND_SLOPE_MIN:
            out.append(("contra_trend_warning_short", -_CONTRA_TREND_PENALTY))
        elif direction == "long" and slope < -_CONTRA_TREND_SLOPE_MIN:
            out.append(("contra_trend_warning_long", -_CONTRA_TREND_PENALTY))

    sharpe = _resolve_tf_indicator(setup, tf, "sharpe_20")
    phase = _lifecycle_phase(setup)
    if sharpe is not None:
        if (
            direction == "short"
            and phase in _EXHAUSTION_PHASES
            and sharpe < _SHARPE_LOW_THRESHOLD
        ):
            out.append(("low_sharpe_exhaustion_short", _SHARPE_PHASE_FUEL))
        if (
            direction == "long"
            and phase in _POST_DUMP_PHASES
            and sharpe > _SHARPE_HIGH_THRESHOLD
        ):
            out.append(("high_sharpe_accumulation_long", _SHARPE_PHASE_FUEL))

    if setup.get("volume_regime_break"):
        out.append(("volume_regime_break", _VOLUME_REGIME_BREAK_FUEL))

    return out


def _apply_research_fuel(
    setup: dict[str, Any],
    *,
    direction: Literal["long", "short"],
    score_key: str,
    tf: dict[str, Any] | None,
) -> None:
    for trigger, fuel in research_fuel_triggers(setup, direction=direction, tf=tf):
        _apply_fuel_trigger(setup, score_key=score_key, trigger=trigger, fuel=fuel)


def distribution_fuel_triggers(
    setup: dict[str, Any],
    *,
    direction: Literal["long", "short"],
    tf: dict[str, Any] | None = None,
) -> list[tuple[str, float]]:
    """Return distribution + volume percentile fuel overlays (Phase 12A)."""
    out: list[tuple[str, float]] = []
    zscore = _resolve_tf_indicator(setup, tf, "return_zscore")
    phase = _lifecycle_phase(setup)
    if zscore is not None and abs(zscore) > _EXTREME_MOVE_Z:
        if direction == "short":
            if phase in _EXHAUSTION_PHASES and zscore > 0:
                out.append(("extreme_move_bear", _EXTREME_MOVE_FUEL))
            elif zscore < 0:
                out.append(("extreme_move_bear_cont", _EXTREME_MOVE_FUEL))
        elif direction == "long" and zscore > 0:
            out.append(("extreme_move_bull", _EXTREME_MOVE_FUEL))
        elif direction == "long" and zscore < 0 and phase in {
            "post_dump_bounce",
            "recovery",
            "accumulation",
        }:
            out.append(("extreme_move_bull_bounce", _EXTREME_MOVE_FUEL))

    vol_pct = _resolve_tf_indicator(setup, tf, "volume_percentile")
    if vol_pct is not None and vol_pct > _VOLUME_SPIKE_PERCENTILE:
        out.append(("volume_spike_percentile", _VOLUME_SPIKE_FUEL))

    return out


def _apply_distribution_fuel(
    setup: dict[str, Any],
    *,
    direction: Literal["long", "short"],
    score_key: str,
    tf: dict[str, Any] | None,
) -> None:
    for trigger, fuel in distribution_fuel_triggers(setup, direction=direction, tf=tf):
        _apply_fuel_trigger(setup, score_key=score_key, trigger=trigger, fuel=fuel)


def _apply_polars_ta_fuel(
    setup: dict[str, Any],
    *,
    direction: Literal["long", "short"],
    score_key: str,
    tf: dict[str, Any] | None,
) -> None:
    for trigger, fuel in polars_ta_fuel_triggers(setup, direction=direction, tf=tf):
        _apply_fuel_trigger(setup, score_key=score_key, trigger=trigger, fuel=fuel)


def _near_price_level(price: float, level: float, *, pct: float = _CANDLE_LEVEL_PCT) -> bool:
    if price <= 0 or level <= 0:
        return False
    return abs(price - level) / level <= pct


def _near_support(price: float, setup: dict[str, Any]) -> bool:
    for key in ("local_support", "impulse_low", "support_break_level", "invalidation_below"):
        lvl = float(setup.get(key) or 0)
        if _near_price_level(price, lvl):
            return True
    return False


def _near_resistance(price: float, setup: dict[str, Any]) -> bool:
    for key in ("local_resistance", "impulse_high", "resistance_break_level", "invalidation_above"):
        lvl = float(setup.get(key) or 0)
        if _near_price_level(price, lvl):
            return True
    return False


def _candle_block(tf: dict[str, Any], tf_key: str) -> dict[str, Any]:
    block = tf.get(tf_key) or {}
    candle = block.get("candle") if isinstance(block.get("candle"), dict) else {}
    return candle if isinstance(candle, dict) else {}


def _candle_flag(candle: dict[str, Any], key: str) -> bool:
    raw = candle.get(key)
    if raw is None:
        return False
    try:
        return bool(float(raw) >= 0.5)
    except (TypeError, ValueError):
        return bool(raw)


def candle_pattern_fuel_triggers(
    setup: dict[str, Any],
    *,
    direction: Literal["long", "short"],
    tf: dict[str, Any] | None,
    price: float = 0.0,
) -> list[tuple[str, float]]:
    """Hammer/shooting-star/star pattern fuel overlays (Phase 8B)."""
    blocks = tf or setup.get("timeframes") or {}
    if not isinstance(blocks, dict):
        return []
    px = price or float(setup.get("price") or 0)
    out: list[tuple[str, float]] = []
    for tf_key in _CANDLE_TF_KEYS:
        candle = _candle_block(blocks, tf_key)
        tag = tf_key.removesuffix("_closed")
        if direction == "long":
            if _candle_flag(candle, "candle_hammer") and _near_support(px, setup):
                out.append((f"hammer_at_support_{tag}", _CANDLE_REVERSAL_FUEL))
            if _candle_flag(candle, "candle_morning_star"):
                out.append((f"morning_star_{tag}", _CANDLE_STAR_FUEL))
        else:
            if _candle_flag(candle, "candle_shooting_star") and _near_resistance(px, setup):
                out.append((f"shooting_star_at_resistance_{tag}", _CANDLE_REVERSAL_FUEL))
            if _candle_flag(candle, "candle_evening_star"):
                out.append((f"evening_star_{tag}", _CANDLE_STAR_FUEL))
    return out


def candle_pattern_hard_triggers(
    setup: dict[str, Any],
    *,
    direction: Literal["long", "short"],
    tf: dict[str, Any] | None,
    price: float = 0.0,
) -> list[str]:
    """Engulfing at structure as optional hard confirm (Phase 8B)."""
    blocks = tf or setup.get("timeframes") or {}
    if not isinstance(blocks, dict):
        return []
    px = price or float(setup.get("price") or 0)
    hard: list[str] = []
    for tf_key in _CANDLE_TF_KEYS:
        candle = _candle_block(blocks, tf_key)
        tag = tf_key.removesuffix("_closed")
        if direction == "long" and _candle_flag(candle, "candle_bullish_engulfing"):
            if _near_support(px, setup):
                hard.append(f"{tag}_bullish_engulfing_at_support")
        elif direction == "short" and _candle_flag(candle, "candle_bearish_engulfing"):
            if _near_resistance(px, setup):
                hard.append(f"{tag}_bearish_engulfing_at_resistance")
    return hard


def _apply_candle_pattern_fuel(
    setup: dict[str, Any],
    *,
    direction: Literal["long", "short"],
    score_key: str,
    tf: dict[str, Any] | None,
    price: float = 0.0,
) -> None:
    for trigger, fuel in candle_pattern_fuel_triggers(
        setup, direction=direction, tf=tf, price=price
    ):
        _apply_fuel_trigger(setup, score_key=score_key, trigger=trigger, fuel=fuel)


def _apply_ema200_confluence(
    setup: dict[str, Any],
    *,
    direction: str,
    score_key: str,
    price: float = 0.0,
    tf: dict[str, Any] | None = None,
    symbol: str = "",
) -> None:
    sc = scoring_thresholds(symbol)
    px = price or float(setup.get("price") or 0)
    ema200 = _resolve_ema200(setup, tf)
    trig = ema200_confluence_trigger(direction=direction, price=px, ema200=ema200, symbol=symbol)
    if not trig:
        return
    triggers = list(setup.get("triggers") or [])
    if trig in triggers:
        return
    triggers.append(trig)
    setup["triggers"] = triggers
    setup[score_key] = round(float(setup.get(score_key) or 0) + float(sc.get("ema200_fuel", 8.0)), 1)


def _apply_prokol_fuel_penalty(
    setup: dict[str, Any],
    *,
    direction: str,
    tf: dict[str, Any] | None,
    level: float,
) -> None:
    """Tag prokol trap on setup; fuel adjustment is applied via compute_setup_fuel."""
    from hunt_core.gate.delivery import detect_prokol

    if level <= 0 or not tf:
        return

    trap = detect_prokol(level=level, break_direction=direction, tf=tf)
    if not trap.get("prokol"):
        return
    triggers = list(setup.get("triggers") or [])
    tag = f"prokol_trap_{direction}"
    if tag not in triggers:
        triggers.append(tag)
    setup["triggers"] = triggers
    setup["prokol_trap"] = trap


def long_resistance_chase_veto(
    resistance: float,
    price: float,
    r5_close: float,
) -> bool:
    """Veto late long chase; allow 0.5% retest when 5m closed above resistance."""
    if resistance <= 0:
        return False
    if price <= 0:
        return False
    ratio = 0.995 if r5_close > resistance else 0.998
    return price < resistance * ratio


def _wall_dict(market: dict[str, Any], side: str) -> dict[str, Any] | None:
    key = "nearest_bid_wall" if side == "bid" else "nearest_ask_wall"
    raw = market.get(key)
    return raw if isinstance(raw, dict) else None


def _resolve_depth_imbalance(market: dict[str, Any]) -> float | None:
    """Prefer WS top-20 depth imbalance over REST/L1 book fields (Phase 7C)."""
    ws = market.get("ws_depth_imbalance")
    if ws is not None:
        try:
            return float(ws)
        except (TypeError, ValueError):
            pass
    rest = market.get("depth_imbalance")
    if rest is not None:
        try:
            return float(rest)
        except (TypeError, ValueError):
            pass
    live = market.get("live_depth_imbalance")
    if live is not None:
        try:
            return float(live)
        except (TypeError, ValueError):
            pass
    return None


def ws_depth_fuel_triggers(
    market: dict[str, Any],
    *,
    direction: Literal["long", "short"],
) -> list[tuple[str, float]]:
    """WS top-20 depth imbalance fuel (Phase 7C)."""
    imb = _resolve_depth_imbalance(market)
    if imb is None:
        return []
    if direction == "short" and imb <= -_WS_DEPTH_IMB_THRESHOLD:
        return [("ws_depth_ask_heavy", _WS_DEPTH_FUEL)]
    if direction == "long" and imb >= _WS_DEPTH_IMB_THRESHOLD:
        return [("ws_depth_bid_heavy", _WS_DEPTH_FUEL)]
    return []


def ws_cvd_divergence_fuel_triggers(
    market: dict[str, Any],
    *,
    direction: Literal["long", "short"],
) -> list[tuple[str, float]]:
    """CVD vs price divergence from WS agg trades (Phase 7D)."""
    out: list[tuple[str, float]] = []
    windows = (
        ("5m", "ws_cvd_5m", "ws_price_chg_5m", _CVD_DIV_FUEL_5M),
        ("1m", "ws_cvd_1m", "ws_price_chg_1m", _CVD_DIV_FUEL_1M),
    )
    for label, cvd_key, px_key, fuel in windows:
        cvd_raw = market.get(cvd_key)
        px_raw = market.get(px_key)
        if cvd_raw is None or px_raw is None:
            continue
        try:
            cvd = float(cvd_raw)
            px_chg = float(px_raw)
        except (TypeError, ValueError):
            continue
        if direction == "short" and px_chg >= _CVD_DIV_PRICE_MIN_PCT and cvd < 0.0:
            out.append((f"ws_cvd_bear_div_{label}", fuel))
        elif direction == "long" and px_chg <= -_CVD_DIV_PRICE_MIN_PCT and cvd > 0.0:
            out.append((f"ws_cvd_bull_div_{label}", fuel))
    return out


def _apply_ws_orderflow_fuel(
    setup: dict[str, Any],
    *,
    direction: Literal["long", "short"],
    score_key: str,
    market: dict[str, Any] | None,
) -> None:
    mkt = market or {}
    for trigger, fuel in (
        *ws_depth_fuel_triggers(mkt, direction=direction),
        *ws_cvd_divergence_fuel_triggers(mkt, direction=direction),
    ):
        _apply_fuel_trigger(setup, score_key=score_key, trigger=trigger, fuel=fuel)


def wall_depth_fuel_triggers(
    market: dict[str, Any],
    *,
    direction: Literal["long", "short"],
    price: float = 0.0,
    symbol: str = "",
) -> tuple[float, list[str]]:
    """Book wall + zone-imbalance fuel overlays (Phase 2B/2C)."""

    sc = scoring_thresholds(symbol)
    wall_dist = float(sc.get("wall_max_distance_pct", 2.0))
    wall_fuel = float(sc.get("wall_fuel_score", 6.0))
    zone_thresh = float(sc.get("zone_imb_threshold", 0.15))
    zone_fuel = float(sc.get("zone_fuel_score", 4.0))
    score = 0.0
    triggers: list[str] = []
    bid_wall = _wall_dict(market, "bid")
    ask_wall = _wall_dict(market, "ask")
    zone = market.get("depth_zone_imbalance")
    zone_map = zone if isinstance(zone, dict) else {}
    mark: float | None = None
    try:
        mark = require_mark_price(price, market)
    except Exception:
        mark = optional_finite_float(price) or optional_finite_float((market or {}).get("last_price"))
    if mark is None:
        return score, triggers

    if direction == "long" and bid_wall:
        dist = float(bid_wall.get("distance_pct") or 999.0)
        sig = float(bid_wall.get("significance_pct") or 0.0)
        px = float(bid_wall.get("price_center") or 0.0)
        below = mark <= 0 or px <= mark
        if sig >= 0.5 and dist <= wall_dist and below:
            score += wall_fuel
            triggers.append("bid_wall_support")

    if direction == "short" and ask_wall:
        dist = float(ask_wall.get("distance_pct") or 999.0)
        sig = float(ask_wall.get("significance_pct") or 0.0)
        px = float(ask_wall.get("price_center") or 0.0)
        above = mark <= 0 or px >= mark
        if sig >= 0.5 and dist <= wall_dist and above:
            score += wall_fuel
            triggers.append("ask_wall_resistance")

    best_band: str | None = None
    best_mag = 0.0
    for band, imb in zone_map.items():
        try:
            val = float(imb)
        except (TypeError, ValueError):
            continue
        if direction == "long" and val >= zone_thresh and val > best_mag:
            best_mag = val
            best_band = str(band)
        elif direction == "short" and val <= -zone_thresh and abs(val) > best_mag:
            best_mag = abs(val)
            best_band = str(band)
    if best_band is not None:
        score += zone_fuel
        tag = "bid_heavy" if direction == "long" else "ask_heavy"
        triggers.append(f"zone_imb_{tag}_{best_band}")

    return score, triggers


def _cluster_for_trigger(trigger: str) -> str | None:
    t = str(trigger).lower()
    for cluster, needles in _FUEL_CLUSTER_RULES:
        if any(n in t for n in needles):
            return cluster
    return None


def cluster_fuel(triggers: list[str], *, raw_score: float, symbol: str = "") -> float:
    """Deduplicated fuel: sum of per-cluster contributions, each capped."""
    sc = scoring_thresholds(symbol)
    cap = float(sc.get("cluster_cap", 28.0))
    w_default = float(sc.get("trigger_weight_default", 12.0))
    w_structure = float(sc.get("trigger_weight_structure", 28.0))
    w_close = float(sc.get("trigger_weight_close_break", 22.0))
    w_div = float(sc.get("trigger_weight_div", 18.0))
    w_trend = float(sc.get("trigger_weight_trendline", 8.0))
    w_reject = float(sc.get("trigger_weight_rejection", 16.0))
    blend = float(sc.get("fuel_raw_blend_ratio", 0.55))
    buckets: dict[str, float] = {c: 0.0 for c, _ in _FUEL_CLUSTER_RULES}
    for trig in triggers:
        if str(trig) in _FUEL_PENALTY_TRIGGERS:
            continue
        cluster = _cluster_for_trigger(trig)
        if cluster is None:
            continue
        w = w_default
        if "lost_support" in trig or "broke_resistance" in trig:
            w = w_structure
        elif "close_below" in trig or "close_above" in trig or "cascade" in trig:
            w = w_close
        elif "div" in trig:
            w = w_div
        elif "trendline" in trig:
            w = w_trend
        elif "rejection" in trig or "bounce" in trig:
            w = w_reject
        buckets[cluster] = min(cap, buckets[cluster] + w)
    fuel = sum(buckets.values())
    return round(min(100.0, max(fuel, min(raw_score * blend, 100.0))), 1)


def compute_setup_fuel(
    setup: dict[str, Any],
    *,
    direction: Literal["short", "long"],
    symbol: str = "",
    tf: dict[str, Any] | None = None,
) -> float:
    """Cluster fuel + prokol penalty — must match enrich_dump/long_setup output."""
    score_key = "dump_score" if direction == "short" else "long_score"
    triggers = list(setup.get("triggers") or [])
    raw = float(setup.get(score_key) or 0)
    fuel = cluster_fuel(triggers, raw_score=raw, symbol=symbol)
    if not tf:
        return fuel
    if direction == "short":
        level = float(setup.get("support_break_level") or setup.get("local_support") or 0)
    else:
        level = float(
            setup.get("resistance_break_level") or setup.get("local_resistance") or 0
        )
    if level <= 0:
        return fuel

    from hunt_core.gate.delivery import detect_prokol

    trap = detect_prokol(level=level, break_direction=direction, tf=tf)
    if not trap.get("prokol"):
        return fuel
    penalty = _PROKOL_TF_TRAP_PENALTY if trap.get("tf_trap") else _PROKOL_FUEL_PENALTY
    return round(max(0.0, fuel - penalty), 1)


def _orderflow_confirm_aligned(
    direction: str,
    mkt: dict[str, Any],
    *,
    symbol: str = "",
) -> tuple[bool, str]:
    """60s taker delta must align with confirm direction when WS data is present."""
    of = orderflow_thresholds(symbol)
    if not of.get("require_ws_align", True):
        return True, ""
    agg60 = mkt.get("agg_trade_delta_60s")
    if agg60 is None:
        return True, ""
    try:
        val = float(agg60)
    except (TypeError, ValueError):
        return False, "orderflow_data_invalid"
    buy_min = float(of.get("taker_buy_min", 0.58))
    sell_max = float(of.get("taker_sell_max", 0.42))
    if direction == "long" and val < buy_min:
        return False, "orderflow_sell_pressure_vs_long"
    if direction == "short" and val > sell_max:
        return False, "orderflow_buy_pressure_vs_short"
    return True, ""

