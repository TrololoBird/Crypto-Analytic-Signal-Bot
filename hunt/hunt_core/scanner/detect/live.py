"""Live detection entry — feed the fusion engine from the feature lake + current bar."""
from __future__ import annotations

from typing import Any

import polars as pl

from hunt_core.scanner.detect import fusion as Fz
from hunt_core.scanner.detect import phase as Ph
from hunt_core.scanner.detect.config import fusion_params
from hunt_core.scanner.detect.magnitude_cache import magnitude_history_for_frame
from hunt_core.scanner.detect.result import Detection, build_detection
from hunt_core.scanner.detect.windows import build_window


def _neutral_detection(
    symbol: str,
    *,
    tf: str,
    reason: str,
) -> Detection:
    """Fail-closed detection when the current bar is not closed."""
    empty = pl.DataFrame()
    window = build_window(empty, symbol=symbol.upper(), tf=tf)
    det = build_detection(window, magnitude_history=None)
    return Detection(
        symbol=window.symbol,
        tf=tf,
        side="none",
        phase=det.phase,
        watch_ok=False,
        gate_open=False,
        confidence=0.0,
        magnitude=0.0,
        price=window.price,
        fusion=det.fusion,
        gate=Fz.GateDecision(
            gate_open=False,
            threshold=None,
            q=0.0,
            reason=reason,
        ),
        phase_info=det.phase_info,
        factors=det.factors,
        reasons=[reason],
    )


def build_live_detection(
    symbol: str,
    current_vector: dict[str, Any],
    *,
    tf: str = "15m",
    lookback: int | None = None,
    q_gate: float | None = None,
    q_phase: float | None = None,
    context: dict[str, Any] | None = None,
) -> Detection:
    """Run the fusion engine for the current **closed** bar (lake history + closed snapshot)."""
    if not current_vector.get("closed_bar", True):
        return _neutral_detection(
            symbol,
            tf=tf,
            reason="forming_bar_blocked",
        )

    fp = fusion_params()
    lb = fp.lookback if lookback is None else lookback
    from hunt_core.data.lake import query_features

    try:
        hist = query_features(symbol, tf=tf, limit=max(lb, 1))
    except Exception:
        hist = pl.DataFrame()

    try:
        cur = pl.DataFrame([current_vector])
    except Exception:
        cur = pl.DataFrame()

    if hist.height and cur.height:
        frame = pl.concat([hist, cur], how="diagonal_relaxed")
    elif cur.height:
        frame = cur
    else:
        frame = hist

    sym = symbol.upper()
    if frame.height == 0:
        window = build_window(frame, symbol=sym, tf=tf, lookback=lb)
        return build_detection(
            window,
            magnitude_history=None,
            q_gate=q_gate,
            q_phase=q_phase,
            context=context,
        )

    ts_max = None
    if "ts" in frame.columns and frame.height:
        ts_max = frame.get_column("ts")[-1]
        if ts_max is not None:
            ts_max = str(ts_max)

    mag_hist = magnitude_history_for_frame(frame, symbol=sym, tf=tf, lookback=lb)
    window = build_window(frame, symbol=sym, tf=tf, lookback=lb, ts_max=ts_max)
    return build_detection(
        window,
        magnitude_history=mag_hist,
        q_gate=q_gate,
        q_phase=q_phase,
        context=context,
    )


__all__ = ["build_live_detection"]
