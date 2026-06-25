"""squeeze_setup - canonical strategy detector."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from engine.features.prepare import _swing_points as _sp

from ..setups import _build_signal, _compute_dynamic_score, _reject
from ..setups.spec_runtime import SpecDetectorSetup, run_setup_detection
from ._common import LOGGER, SpecHit, _latest_values, confirmed_pattern_frame, with_spec_columns
from ._roadmap import (
    _build_atr_signal,
    _confirmed_context_conflict,
    _missing_columns,
    _prev,
)

if TYPE_CHECKING:
    import polars as pl

    from engine.domain.config import BotSettings
    from engine.domain.schemas import PreparedSymbol, Signal

__all__ = ["detect_bb_squeeze_release", "detect_squeeze_setup"]


def detect_bb_squeeze_release(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    """BB/KC squeeze release (merged from former bb_squeeze strategy)."""
    work = with_spec_columns(frame)
    if work.height < 30:
        return None
    if "spec_squeeze" not in work.columns:
        LOGGER.warning("detect_bb_squeeze_release: spec_squeeze column missing")
        return None
    row = _latest_values(work)
    atr = row.get("spec_atr14", 0.0)
    if atr <= 0.0:
        return None
    try:
        was_squeeze = bool(work.item(-2, "spec_squeeze"))
        is_squeeze = bool(work.item(-1, "spec_squeeze"))
    except (IndexError, ValueError, TypeError):
        return None
    if not was_squeeze or is_squeeze:
        return None
    direction = "long" if row["close"] > row.get("spec_ema20", row["close"]) else "short"
    ema20 = row.get("spec_ema20", row["close"])
    return SpecHit(
        strategy="squeeze_setup",
        direction=direction,
        entry=ema20,
        stop_basis=row["low"] if direction == "long" else row["high"],
        atr=atr,
        timeframe=timeframe,
        reasons=("bb_kc_squeeze_released",),
        vol_ratio=row.get("volume_ratio20", 1.0),
        rsi=row.get("rsi14", 50.0),
    )


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _bb_kc_squeeze_active(
    work_15m: pl.DataFrame,
    *,
    bb_squeeze_threshold: float,
    min_bb_compression_width: float,
    bb_pct_b_threshold: float,
) -> tuple[bool, str]:
    """Detect a genuine BB + Keltner Channel squeeze."""
    if work_15m.height < 30:
        return False, ""

    bb_pct_b = _as_float(work_15m.item(-1, "bb_pct_b"), 0.5)
    bb_width = _as_float(work_15m.item(-1, "bb_width"))
    kc_upper = _as_float(work_15m.item(-1, "kc_upper"))
    kc_lower = _as_float(work_15m.item(-1, "kc_lower"))
    close = float(work_15m.item(-1, "close"))

    if kc_upper <= 0 or kc_lower <= 0 or bb_width <= 0:
        return False, ""

    if work_15m.height >= 30:
        bb_width_history = work_15m["bb_width"].tail(30)
        width_q25 = _as_float(bb_width_history.quantile(0.25), min_bb_compression_width)
        compression_cap = max(min_bb_compression_width, 0.0)
        if bb_squeeze_threshold > 0:
            compression_cap = (
                min(compression_cap, bb_squeeze_threshold)
                if compression_cap > 0
                else bb_squeeze_threshold
            )
        if width_q25 > 0:
            compression_cap = min(compression_cap, width_q25) if compression_cap > 0 else width_q25
        was_compressed = compression_cap > 0 and bb_width <= compression_cap
    else:
        compression_cap = min_bb_compression_width
        if bb_squeeze_threshold > 0:
            compression_cap = (
                min(compression_cap, bb_squeeze_threshold)
                if compression_cap > 0
                else bb_squeeze_threshold
            )
        was_compressed = compression_cap > 0 and bb_width <= compression_cap

    breakout_up = close > kc_upper and bb_pct_b > bb_pct_b_threshold
    breakout_down = close < kc_lower and bb_pct_b < (1.0 - bb_pct_b_threshold)

    if was_compressed and breakout_up:
        return True, "long"
    if was_compressed and breakout_down:
        return True, "short"

    return False, ""


def _bb_kc_squeeze_release(
    work_15m: pl.DataFrame,
    *,
    bb_squeeze_threshold: float,
    min_bb_compression_width: float,
    bb_pct_b_threshold: float,
    release_lookback: int,
    min_width_expansion: float,
    min_roc10_abs_pct: float,
) -> tuple[bool, str, str]:
    if work_15m.height < max(30, release_lookback + 2):
        return False, "", ""
    if {"squeeze_on", "squeeze_off"}.issubset(set(work_15m.columns)):
        lookback = max(2, min(release_lookback, work_15m.height - 1))
        prior = work_15m.slice(work_15m.height - lookback - 1, lookback)
        was_squeezed = bool(prior["squeeze_on"].fill_null(0).max())
        released_now = _as_float(work_15m.item(-1, "squeeze_off")) > 0.0
        if was_squeezed and released_now:
            hist = (
                _as_float(work_15m.item(-1, "squeeze_hist"))
                if "squeeze_hist" in work_15m.columns
                else 0.0
            )
            roc10 = _as_float(work_15m.item(-1, "roc10")) if "roc10" in work_15m.columns else 0.0
            direction = "long" if (hist > 0.0 or roc10 > 0.0) else "short"
            if hist == 0.0 and roc10 == 0.0:
                return False, "", ""
            return True, direction, "polars_squeeze_release"

    required = {"bb_width", "bb_pct_b", "kc_upper", "kc_lower", "close", "roc10"}
    if not required.issubset(set(work_15m.columns)):
        return False, "", ""

    current_width = _as_float(work_15m.item(-1, "bb_width"))
    bb_pct_b = _as_float(work_15m.item(-1, "bb_pct_b"), 0.5)
    close = _as_float(work_15m.item(-1, "close"))
    kc_upper = _as_float(work_15m.item(-1, "kc_upper"))
    kc_lower = _as_float(work_15m.item(-1, "kc_lower"))
    roc10 = _as_float(work_15m.item(-1, "roc10"))
    if min(current_width, close, kc_upper, kc_lower) <= 0.0:
        return False, "", ""

    previous_widths = work_15m["bb_width"].slice(
        max(0, work_15m.height - release_lookback - 1),
        release_lookback,
    )
    compressed_width = _as_float(previous_widths.min())
    width_q25 = _as_float(previous_widths.quantile(0.25), min_bb_compression_width)
    compression_cap = max(min_bb_compression_width, 0.0)
    if bb_squeeze_threshold > 0:
        compression_cap = (
            min(compression_cap, bb_squeeze_threshold)
            if compression_cap > 0
            else bb_squeeze_threshold
        )
    if width_q25 > 0:
        compression_cap = min(compression_cap, width_q25) if compression_cap > 0 else width_q25
    if compressed_width <= 0.0 or compressed_width > compression_cap:
        return False, "", ""
    if current_width < compressed_width * max(1.0, min_width_expansion):
        return False, "", ""

    breakout_up = close > kc_upper and bb_pct_b >= bb_pct_b_threshold and roc10 >= min_roc10_abs_pct
    breakout_down = (
        close < kc_lower and bb_pct_b <= (1.0 - bb_pct_b_threshold) and roc10 <= -min_roc10_abs_pct
    )
    if breakout_up:
        return True, "long", "bb_kc_recent_compression_release_long"
    if breakout_down:
        return True, "short", "bb_kc_recent_compression_release_short"
    return False, "", ""


def _detect_squeeze_setup_extended(
    prepared: PreparedSymbol,
    _settings: BotSettings,
    defaults: dict[str, float],
    effective: dict[str, float],
    _setup_id: str,
    family: str,
) -> Signal | None:
    dynamic_params = effective
    work_15m = confirmed_pattern_frame(prepared.work_15m)
    # FIX 2026-05-21: the spec layer is intentionally strict; when it misses,
    # keep the prepared squeeze_on/off and compression-window fallback live.
    bb_squeeze_threshold = _as_float(
        dynamic_params.get("bb_squeeze_threshold", defaults["bb_squeeze_threshold"]),
        defaults["bb_squeeze_threshold"],
    )
    min_bb_compression_width = _as_float(
        dynamic_params.get("min_bb_compression_width", defaults["min_bb_compression_width"]),
        defaults["min_bb_compression_width"],
    )
    bb_pct_b_threshold = _as_float(
        dynamic_params.get("bb_pct_b_threshold", defaults["bb_pct_b_threshold"]),
        defaults["bb_pct_b_threshold"],
    )
    volume_threshold = _as_float(
        dynamic_params.get("volume_threshold", defaults["volume_threshold"]),
        defaults["volume_threshold"],
    )
    sl_buffer_atr = _as_float(
        dynamic_params.get("sl_buffer_atr", defaults["sl_buffer_atr"]),
        defaults["sl_buffer_atr"],
    )
    min_rr = _as_float(dynamic_params.get("min_rr", defaults["min_rr"]), defaults["min_rr"])
    base_score = _as_float(
        dynamic_params.get("base_score", defaults["base_score"]),
        defaults["base_score"],
    )
    funding_extreme_threshold = _as_float(
        dynamic_params.get("funding_extreme_threshold", defaults["funding_extreme_threshold"]),
        defaults["funding_extreme_threshold"],
    )
    liquidation_extreme_threshold = _as_float(
        dynamic_params.get(
            "liquidation_extreme_threshold",
            defaults["liquidation_extreme_threshold"],
        ),
        defaults["liquidation_extreme_threshold"],
    )
    release_lookback = max(
        3,
        int(dynamic_params.get("release_lookback", defaults["release_lookback"])),
    )
    min_release_width_expansion = _as_float(
        dynamic_params.get(
            "min_release_width_expansion",
            defaults["min_release_width_expansion"],
        ),
        defaults["min_release_width_expansion"],
    )
    min_release_roc10_abs_pct = _as_float(
        dynamic_params.get(
            "min_release_roc10_abs_pct",
            defaults["min_release_roc10_abs_pct"],
        ),
        defaults["min_release_roc10_abs_pct"],
    )
    no_crowd_confirmation_penalty = _as_float(
        dynamic_params.get(
            "no_crowd_confirmation_penalty",
            defaults["no_crowd_confirmation_penalty"],
        ),
        defaults["no_crowd_confirmation_penalty"],
    )

    if work_15m.height < 30:
        _reject(prepared, "squeeze_setup", "insufficient_bars")
        return None

    is_squeeze, squeeze_dir = _bb_kc_squeeze_active(
        work_15m,
        bb_squeeze_threshold=bb_squeeze_threshold,
        min_bb_compression_width=min_bb_compression_width,
        bb_pct_b_threshold=bb_pct_b_threshold,
    )
    release_reason = ""
    if not is_squeeze:
        is_squeeze, squeeze_dir, release_reason = _bb_kc_squeeze_release(
            work_15m,
            bb_squeeze_threshold=bb_squeeze_threshold,
            min_bb_compression_width=min_bb_compression_width,
            bb_pct_b_threshold=bb_pct_b_threshold,
            release_lookback=release_lookback,
            min_width_expansion=min_release_width_expansion,
            min_roc10_abs_pct=min_release_roc10_abs_pct,
        )
    if not is_squeeze:
        _reject(prepared, "squeeze_setup", "no_bb_kc_squeeze")
        return None

    funding = prepared.funding_rate
    liq_score = prepared.liquidation_score
    crowd_aligned = False
    crowd_reason = ""

    if funding is not None and abs(funding) >= funding_extreme_threshold:
        if funding > 0 and squeeze_dir == "short":
            crowd_aligned = True
            crowd_reason = f"funding={funding:.4f} (longs crowded)"
        elif funding < 0 and squeeze_dir == "long":
            crowd_aligned = True
            crowd_reason = f"funding={funding:.4f} (shorts crowded)"

    if liq_score is not None and abs(liq_score) >= liquidation_extreme_threshold:
        if liq_score > 0 and squeeze_dir == "long":
            crowd_aligned = True
            crowd_reason = f"liq_score={liq_score:.3f} (short liquidations bullish)"
        elif liq_score < 0 and squeeze_dir == "short":
            crowd_aligned = True
            crowd_reason = f"liq_score={liq_score:.3f} (long liquidations bearish)"

    direction = squeeze_dir
    if _confirmed_context_conflict(prepared, direction):
        _reject(
            prepared,
            "squeeze_setup",
            "htf_context_conflict",
            direction=direction,
        )
        return None

    oi_chg = prepared.oi_change_pct
    oi_drop_limit = -8.0 if oi_chg is not None and abs(oi_chg) > 1.0 else -0.08
    if oi_chg is not None and oi_chg < oi_drop_limit:
        _reject(prepared, "squeeze_setup", "oi_falling_too_fast", oi_change_pct=oi_chg)
        return None

    atr = _as_float(work_15m.item(-1, "atr14"))
    if atr <= 0.0:
        _reject(prepared, "squeeze_setup", "atr_non_positive", atr=atr)
        return None
    vol_ratio = _as_float(work_15m.item(-1, "volume_ratio20"), 1.0)
    rsi = _as_float(work_15m.item(-1, "rsi14"), 50.0)

    if vol_ratio < volume_threshold:
        strong_release = (
            release_reason == "polars_squeeze_release"
            and "squeeze_hist" in work_15m.columns
            and abs(_as_float(work_15m.item(-1, "squeeze_hist"))) >= atr * 0.15
        )
        if not strong_release:
            _reject(prepared, "squeeze_setup", "volume_too_low", vol_ratio=vol_ratio)
            return None

    if direction == "short" and rsi > 70.0:
        reasons_rsi_penalty = "rsi_short_overbought_penalty"
    elif direction == "long" and rsi < 30.0:
        reasons_rsi_penalty = "rsi_long_oversold_penalty"
    else:
        reasons_rsi_penalty = None

    reasons = [
        f"bb_kc_squeeze breakout={direction} bb_pct_b>{bb_pct_b_threshold:.2f}",
        f"vol_ratio={vol_ratio:.2f} min={volume_threshold:.2f}",
        f"bb_width<={min_bb_compression_width:.4f} sl_buffer_atr={sl_buffer_atr:.2f}",
        f"rsi={rsi:.1f}",
    ]
    if release_reason:
        reasons.append(release_reason)
    if crowd_aligned:
        reasons.append(crowd_reason)
    else:
        reasons.append("crowd_context_neutral")
    if reasons_rsi_penalty:
        reasons.append(reasons_rsi_penalty)

    # --- Compute structural SL/TP ---
    pre_breakout = work_15m.slice(max(0, work_15m.height - 11), 10)
    if pre_breakout.height < 3:
        pre_breakout = work_15m.slice(max(0, work_15m.height - 6), 5)
    price_anchor = (
        _as_float(pre_breakout["high"].max())
        if direction == "long"
        else _as_float(pre_breakout["low"].min())
    )
    reasons.append(f"limit_entry={price_anchor:.4f}")

    if direction == "long":
        # SL: below pre-breakout swing low + configured ATR buffer
        stop = _as_float(pre_breakout["low"].min()) - atr * sl_buffer_atr
        # TP1: first swing/fractal in breakout direction on 15m
        _sh_mask, _sl_mask = _sp(work_15m, n=3, include_unconfirmed_tail=True)
        sh_prices = work_15m.filter(_sh_mask)["high"]
        tp1_candidates = sh_prices.filter(sh_prices > price_anchor)
        tp1 = _as_float(tp1_candidates[0]) if tp1_candidates.len() > 0 else None
        # TP2: squeeze range height projected from entry
        squeeze_range = _as_float(pre_breakout["high"].max()) - _as_float(pre_breakout["low"].min())
        tp2 = price_anchor + squeeze_range if squeeze_range > 0 else None
    else:
        # SL: above pre-breakout swing high + configured ATR buffer
        stop = _as_float(pre_breakout["high"].max()) + atr * sl_buffer_atr
        _, _sl15 = _sp(work_15m, n=2)
        sl_prices = work_15m.filter(_sl15)["low"]
        tp1_candidates = sl_prices.filter(sl_prices < price_anchor)
        tp1 = _as_float(tp1_candidates[-1]) if tp1_candidates.len() > 0 else None
        squeeze_range = _as_float(pre_breakout["high"].max()) - _as_float(pre_breakout["low"].min())
        tp2 = price_anchor - squeeze_range if squeeze_range > 0 else None

    risk = abs(price_anchor - stop)
    if risk <= 0:
        _reject(prepared, "squeeze_setup", "invalid_stop", stop=stop)
        return None
    if tp1 is None or abs(tp1 - price_anchor) < risk * min_rr:
        tp1 = price_anchor + risk * min_rr if direction == "long" else price_anchor - risk * min_rr
        reasons.append(f"tp1_rr_fallback_{min_rr:.2f}")
    if tp2 is None or abs(tp2 - price_anchor) <= abs(tp1 - price_anchor):
        tp2 = (
            price_anchor + risk * max(2.0, min_rr + 0.35)
            if direction == "long"
            else price_anchor - risk * max(2.0, min_rr + 0.35)
        )

    score = _compute_dynamic_score(
        direction=direction,
        base_score=base_score,
        vol_ratio=vol_ratio,
        rsi=rsi,
        structure_clarity=0.5,
    )
    if not crowd_aligned:
        score *= no_crowd_confirmation_penalty
    if reasons_rsi_penalty:
        score *= 0.90

    return _build_signal(
        prepared=prepared,
        setup_id="squeeze_setup",
        direction=direction,
        score=score,
        timeframe="15m+1h",
        reasons=reasons,
        strategy_family=family,
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        price_anchor=price_anchor,
        atr=atr,
    )


def _detect_atr_expansion_fallback(
    prepared: PreparedSymbol,
    _settings: BotSettings,
    params: dict[str, float],
    *,
    setup_id: str,
    family: str,
) -> Signal | None:
    """ATR expansion breakout (merged from atr_expansion)."""
    work = prepared.work_15m
    missing = _missing_columns(work, ("open", "high", "low", "close", "atr14"))
    if missing:
        _reject(prepared, setup_id, "missing_columns", missing_fields=missing)
        return None

    lookback = max(1, min(int(params.get("signal_lookback_bars", 8)), 12))
    start_idx = max(1, work.height - lookback)
    best: dict[str, float | int | str] | None = None
    min_ratio = float(params.get("min_atr_expansion_ratio", 2.5))
    min_body_atr = float(params.get("min_body_atr", 0.25))

    for idx in range(work.height - 2, start_idx - 1, -1):
        open_ = _as_float(work.item(idx, "open"))
        high = _as_float(work.item(idx, "high"))
        low = _as_float(work.item(idx, "low"))
        close = _as_float(work.item(idx, "close"))
        prev_close = _as_float(work.item(idx - 1, "close"))
        atr = _as_float(work.item(idx, "atr14"))
        if min(open_, high, low, close, prev_close, atr) <= 0.0:
            continue
        true_range = max(high - low, abs(high - prev_close), abs(low - prev_close))
        ratio = true_range / atr if atr > 0.0 else 0.0
        if ratio < min_ratio:
            continue
        body_atr = abs(close - open_) / max(atr, 1e-12)
        if body_atr < min_body_atr:
            continue
        candidate = {
            "score": ratio + body_atr,
            "ratio": ratio,
            "body_atr": body_atr,
            "direction": "long" if close >= open_ else "short",
            "signal_lag": work.height - 1 - idx,
            "timeframe": "15m",
        }
        if best is None or float(candidate["score"]) > float(best["score"]):
            best = candidate
    if best is None:
        _reject(
            prepared,
            setup_id,
            "indicator.atr_expansion_too_low",
            min_atr_expansion_ratio=min_ratio,
            min_body_atr=min_body_atr,
        )
        return None

    direction = str(best["direction"])
    ratio = float(best["ratio"])
    body_atr = float(best["body_atr"])
    signal_lag = int(best["signal_lag"])
    obv_penalty = 1.0
    if "obv_above_ema" in work.columns:
        obv_val = float(work["obv_above_ema"][-1] or 0.0)
        if (direction == "long" and obv_val <= 0.0) or (direction == "short" and obv_val > 0.0):
            obv_penalty = 0.85
    entry_anchor = _prev(work, "low", 0.0) if direction == "long" else _prev(work, "high", 0.0)
    clarity = min((ratio - 1.0) / 1.0, 1.0) * obv_penalty
    reasons = [
        f"atr_expansion_{direction}",
        f"atr_ratio={ratio:.2f}",
        f"body_atr={body_atr:.2f}",
        f"signal_lag={signal_lag}",
    ]
    if obv_penalty < 1.0:
        reasons.append("obv_opposes_breakout")
    return _build_atr_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=direction,
        params=params,
        confirmed_bar=True,
        reasons=reasons,
        family=family,
        entry_anchor=entry_anchor or None,
        timeframe="15m",
        structure_clarity=clarity,
    )


def detect_squeeze_setup(
    prepared: PreparedSymbol,
    settings: BotSettings,
    defaults: dict[str, float],
    effective: dict[str, float],
    setup_id: str,
    family: str,
) -> Signal | None:
    spec_kwargs = None
    signal = run_setup_detection(
        prepared=prepared,
        settings=settings,
        setup_id=setup_id,
        family=family,
        defaults=defaults,
        effective=effective,
        spec_detect=detect_bb_squeeze_release,
        extended_detect=_detect_squeeze_setup_extended,
        spec_kwargs=spec_kwargs,
    )
    if signal is not None:
        return signal
    merged = {**defaults, **effective}
    return _detect_atr_expansion_fallback(
        prepared,
        settings,
        merged,
        setup_id=setup_id,
        family=family,
    )


__all__ = ["_detect_squeeze_setup_extended", "detect_bb_squeeze_release", "detect_squeeze_setup"]


class SqueezeSetup(SpecDetectorSetup):
    setup_id = "squeeze_setup"
    ENTRY_ORDER_TYPE: ClassVar[str] = "limit"
    family = "breakout"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)

    DEFAULTS: ClassVar[dict[str, float]] = {
        "base_score": 0.55,
        "bb_squeeze_threshold": 4.5,
        "min_bb_compression_width": 4.5,
        "bb_pct_b_threshold": 0.8,
        "volume_threshold": 1.2,
        "sl_buffer_atr": 0.4,
        "bias_mismatch_penalty": 0.75,
        "min_rr": 1.9,
        "funding_extreme_threshold": 0.00015,
        "liquidation_extreme_threshold": 0.2,
        "release_lookback": 12,
        "min_release_width_expansion": 1.5,
        "min_release_roc10_abs_pct": 0.35,
        "no_crowd_confirmation_penalty": 0.92,
        "min_atr_expansion_ratio": 2.5,
        "min_body_atr": 0.25,
        "signal_lookback_bars": 8,
    }

    detect_setup = detect_squeeze_setup


__all__ = ["SqueezeSetup"]
