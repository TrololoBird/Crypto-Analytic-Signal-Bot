"""Pattern A (long) and Pattern B (short) state machine matchers — Polars-first.

Main entry point: ``detect_manipulation_setup(ohlcv_by_tf)`` returns a
``ManipulationSetup`` if any pattern is detected, or None.

All OHLCV data is converted to Polars DataFrames at the entry point before
any detection runs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import polars as pl

from hunt_core.scanner.detect.events import (
    ohlcv_to_df, compute_features,
    atr, detect_impulse, detect_consecutive_impulse,
    detect_absorption, detect_one_candle_absorption,
    detect_bokovik, detect_sweep_low, detect_sweep_high,
    candle_fade_ratio, rejection_at_peak,
    bos_up, bos_down, choch_bull, choch_bear,
    no_liquidity_above,
)
from hunt_core.scanner.detect.state import (
    Direction, PatternType, SymbolState, Bokovik, Sweep, reset_state,
)
from hunt_core.scanner.detect.scoring import (
    compute_score_a, compute_score_b,
)

_MACRO_TF = "1d"
_MESO_TF_PRIORITY = ("4h", "1h")
_MICRO_TF_PRIORITY = ("15m", "5m")
_MACRO_LOOKBACK_BARS = 180
_MACRO_EXCLUDE_RECENT = 7
_MESO_RECENT_CANDIDATES = 12
_BOKOVIK_WINDOW = 30


@dataclass
class ManipulationSetup:
    direction: Direction
    pattern_type: PatternType
    score: float
    macro_tf: str = _MACRO_TF
    meso_tf: str = ""
    micro_tf: str | None = None
    micro_confirmed: bool = False
    swept_level: float = 0.0
    sweep_extreme: float = 0.0
    target: float | None = None
    entry_ref: float | None = None
    evidence: tuple[str, ...] = ()
    steps_covered: int = 0
    total_steps: int = 0
    bokovik_count: int = 0


def _macro_extreme(df: pl.DataFrame, *, direction: Direction) -> float | None:
    exclude = _MACRO_EXCLUDE_RECENT
    body = df[:-exclude] if exclude < len(df) else df
    window = body.tail(_MACRO_LOOKBACK_BARS)
    if window.height == 0:
        return None
    if direction == "short":
        return float(window["high"].max())
    return float(window["low"].min())


def _pattern_a_long(
    macro_df: pl.DataFrame,
    meso_df: pl.DataFrame,
    meso_tf: str,
    micro_15m: pl.DataFrame | None,
    micro_5m: pl.DataFrame | None,
) -> ManipulationSetup | None:
    """Pattern A: impulse→absorption→bokovik-1→sweep→bokovik-2→break→entry."""
    state = reset_state("")
    state.pattern_type = "A"
    state.meso_tf = meso_tf

    meso_atr = atr(meso_df, 14)
    if meso_atr <= 0:
        return None

    # Step 1: Impulse (green, up to 30 bars back)
    imp_ok, imp_idx = detect_impulse(meso_df, lookback=30, direction="up")
    if not imp_ok:
        imp_ok, imp_idx = detect_consecutive_impulse(meso_df, min_count=3)
    if not imp_ok or imp_idx is None:
        return None
    state.impulse_detected = True
    state.steps_covered = max(state.steps_covered, 1)
    state.total_steps = 6
    state.evidence.append(f"impulse_detected({meso_tf})")

    # Step 2: Absorption
    if not detect_absorption(meso_df, imp_idx):
        return None
    state.absorption_detected = True
    state.steps_covered = max(state.steps_covered, 2)
    state.evidence.append(f"absorption_detected({meso_tf})")

    # Step 3: Bokovik-1 (post-impulse window — skip impulse + absorption bars)
    bok_start = (imp_idx + 6) if imp_idx is not None else None
    b1 = detect_bokovik(meso_df, window=_BOKOVIK_WINDOW, start_idx=bok_start)
    if b1 is not None:
        state.bokoviks.append(Bokovik(
            lo=b1["lo"], hi=b1["hi"], touches=b1["touches"],
            atr_ratio=b1["atr_ratio"], width_pct=b1["width_pct"],
            tf=meso_tf,
        ))
        state.steps_covered = max(state.steps_covered, 3)
        state.evidence.append(f"bokovik1_{b1['touches']}touches_{b1['width_pct']:.1f}%({meso_tf})")
    else:
        return None

    # Step 4: Sweep below bokovik
    sweep_ok, sweep_extreme, _ = detect_sweep_low(meso_df, state.bokoviks[0].lo)
    current_price = float(meso_df["close"][-1])
    if sweep_ok:
        state.sweep = Sweep(
            extreme=sweep_extreme, level_breached=state.bokoviks[0].lo,
            direction="below", tf=meso_tf,
        )
        state.steps_covered = max(state.steps_covered, 4)
        state.evidence.append(f"sweep_below_{sweep_extreme:.8g}({meso_tf})")

        # Step 4b: Bokovik-2 after sweep
        sweep_idx = int(next((i for i in range(len(meso_df)-1, -1, -1)
                              if float(meso_df["low"][i]) < state.bokoviks[0].lo), 0))
        b2 = detect_bokovik(meso_df, window=_BOKOVIK_WINDOW, start_idx=sweep_idx + 2)
        if b2 is not None:
            state.bokoviks.append(Bokovik(
                lo=b2["lo"], hi=b2["hi"], touches=b2["touches"],
                atr_ratio=b2["atr_ratio"], width_pct=b2["width_pct"],
                tf=meso_tf,
            ))
            state.steps_covered = max(state.steps_covered, 5)
            state.evidence.append(f"bokovik2_{b2['touches']}touches_{b2['width_pct']:.1f}%({meso_tf})")

        macro_low = _macro_extreme(macro_df, direction="long")
        if macro_low is None:
            return None
        state.macro_extreme = macro_low
        state.entry_ref = current_price

        # Step 5: Structure break up
        micro_df = micro_15m if micro_15m is not None and len(micro_15m) > 20 else meso_df.tail(50)
        if bos_up(micro_df) or choch_bull(micro_df):
            state.structure_broken = True
            state.steps_covered = max(state.steps_covered, 6)
            state.evidence.append("structure_break_up")
            state.micro_tf = "15m"

        tp_high = float(meso_df["high"].max())
        state.target = tp_high

    else:
        # Pattern A3: no sweep — pure accumulation
        macro_low = _macro_extreme(macro_df, direction="long")
        if macro_low is None:
            return None
        state.macro_extreme = macro_low
        state.entry_ref = current_price

        micro_df = micro_15m if micro_15m is not None and len(micro_15m) > 20 else meso_df.tail(50)
        if bos_up(micro_df) or choch_bull(micro_df):
            state.structure_broken = True
            state.steps_covered = max(state.steps_covered, 5)
            state.total_steps = 5
            state.evidence.append("structure_break_up_no_sweep")
            state.micro_tf = "15m"
        else:
            return None

        state.target = float(meso_df["high"].max())

    state.score = compute_score_a(state)
    if state.score < 0.50:
        return None
    return _build_setup(state)


def _prior_swing_high(df: pl.DataFrame, lookback: int = 60, *, exclude_last: int = 15) -> float | None:
    """Most recent swing high BEFORE the pump (exclude last N bars = the impulse itself)."""
    from hunt_core.scanner.detect.events import compute_features
    body = df.tail(lookback)[:-exclude_last] if exclude_last > 0 else df.tail(lookback)
    if len(body) < 10:
        return None
    df_c = compute_features(body)
    swing_vals = df_c.filter(pl.col("_swing_high"))["high"]
    if swing_vals.is_empty():
        return None
    return float(swing_vals.tail(1)[0])


def _pattern_b_short(
    macro_df: pl.DataFrame,
    meso_df: pl.DataFrame,
    meso_tf: str,
    micro_15m: pl.DataFrame | None,
    micro_5m: pl.DataFrame | None,
) -> ManipulationSetup | None:
    """Pattern B: HTF trend→sweep→no_liquidity→fade→break→entry.
    
    The sweep target is the 180d macro extreme first; if too far (>5%),
    falls back to the most recent swing high on the meso timeframe
    (course: "обновляют предыдущий максимум").
    """
    state = reset_state("")
    state.pattern_type = "B"
    state.meso_tf = meso_tf
    state.total_steps = 5

    current_price = float(meso_df["close"][-1])

    # Sweep target: try 180d macro high first, then fall back to recent swing high
    macro_high = _macro_extreme(macro_df, direction="short")
    sweep_target = None
    if macro_high is not None:
        dist = (macro_high - current_price) / macro_high * 100.0 if macro_high > 0 else 999
        if dist <= 5.0:
            sweep_target = macro_high
            state.macro_extreme = macro_high
            state.evidence.append(f"macro_high_180d={macro_high:.8g}")
    if sweep_target is None:
        prior_high = _prior_swing_high(meso_df, lookback=60)
        if prior_high is not None and prior_high < float(meso_df["high"].tail(20).max()):
            sweep_target = prior_high
            state.macro_extreme = prior_high
            state.evidence.append(f"swing_high_local={prior_high:.8g}({meso_tf})")
    if sweep_target is None:
        return None
    state.steps_covered = max(state.steps_covered, 1)

    # Step 2: Sweep above the target level
    sweep_ok, sweep_extreme, _ = detect_sweep_high(meso_df, sweep_target)
    if not sweep_ok:
        return None
    pump_high = float(meso_df["high"].tail(20).max())  # actual pump peak, not the first break above target
    state.sweep = Sweep(
        extreme=sweep_extreme, level_breached=macro_high or sweep_target,
        direction="above", tf=meso_tf,
    )
    state.steps_covered = max(state.steps_covered, 2)
    state.evidence.append(f"sweep_above_{sweep_extreme:.8g}({meso_tf})")

    # Step 3: No liquidity above (pump peak is the new local high)
    if not no_liquidity_above(meso_df, current_price, pump_high=pump_high):
        return None
    state.steps_covered = max(state.steps_covered, 3)
    state.evidence.append("no_liquidity_above")

    # Step 4: Candle fade (at pump peak) or instant rejection (V-top)
    body_ratio, range_ratio = candle_fade_ratio(meso_df, n=8, peak_high=pump_high)
    fade_ok = body_ratio <= 0.50 and range_ratio <= 0.60
    reject_ok = rejection_at_peak(meso_df, pump_high)
    if fade_ok or reject_ok:
        state.candle_fade = fade_ok
        state.instant_rejection = reject_ok
        state.steps_covered = max(state.steps_covered, 4)
        cause = f"candle_fade" if fade_ok else f"instant_rejection"
        state.evidence.append(f"{cause}_br={body_ratio:.2f}_rr={range_ratio:.2f}({meso_tf})")
    else:
        return None

    # Step 5: LTF confirmation
    micro_df = micro_15m if micro_15m is not None and len(micro_15m) > 20 else meso_df.tail(50)
    if bos_down(micro_df) or choch_bear(micro_df):
        state.structure_broken = True
        state.ltf_confirmed = True
        state.steps_covered = max(state.steps_covered, 5)
        state.evidence.append("ltf_confirmed")
        state.micro_tf = "15m"
    else:
        state.evidence.append("ltf_not_confirmed")

    state.entry_ref = current_price
    state.target = float(meso_df["low"].min())

    state.score = compute_score_b(state)
    if state.score < 0.50:
        return None
    return _build_setup(state)


def _build_setup(state: SymbolState) -> ManipulationSetup:
    return ManipulationSetup(
        direction="long" if state.pattern_type in ("A", "A2", "A3") else "short",
        pattern_type=state.pattern_type or "A",
        score=round(state.score, 2),
        macro_tf=state.macro_tf,
        meso_tf=state.meso_tf,
        micro_tf=state.micro_tf,
        micro_confirmed=state.structure_broken,
        swept_level=state.macro_extreme,
        sweep_extreme=state.sweep.extreme if state.sweep else state.macro_extreme,
        target=state.target,
        entry_ref=state.entry_ref,
        evidence=tuple(state.evidence),
        steps_covered=state.steps_covered,
        total_steps=state.total_steps,
        bokovik_count=len(state.bokoviks),
    )


def detect_manipulation_setup(
    ohlcv_by_tf: dict[str, list[list[float]]],
    *,
    cfg: Any = None,
) -> ManipulationSetup | None:
    """Top-down multi-TF manipulation detection — Polars-first.

    Converts raw CCXT OHLCV to Polars DataFrames at entry, then runs
    pattern matchers. Tries Pattern A (long) then Pattern B (short).
    """
    macro_raw = ohlcv_by_tf.get(_MACRO_TF)
    if not macro_raw or len(macro_raw) < _MACRO_LOOKBACK_BARS // 2:
        return None
    macro_df = ohlcv_to_df(macro_raw)

    meso_tf = next((tf for tf in _MESO_TF_PRIORITY if ohlcv_by_tf.get(tf)), None)
    if meso_tf is None:
        return None
    meso_df = ohlcv_to_df(ohlcv_by_tf[meso_tf])
    if len(meso_df) < _MESO_RECENT_CANDIDATES + 1:
        return None

    micro_15m = ohlcv_to_df(ohlcv_by_tf["15m"]) if ohlcv_by_tf.get("15m") else None
    micro_5m = ohlcv_to_df(ohlcv_by_tf["5m"]) if ohlcv_by_tf.get("5m") else None

    setup = _pattern_a_long(macro_df, meso_df, meso_tf, micro_15m, micro_5m)
    if setup is not None:
        return setup

    setup = _pattern_b_short(macro_df, meso_df, meso_tf, micro_15m, micro_5m)
    if setup is not None:
        return setup

    return None


__all__ = ["Direction", "ManipulationSetup", "detect_manipulation_setup"]
