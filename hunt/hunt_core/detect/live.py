"""Live detection entry — feed the fusion engine from the feature lake + current bar."""
from __future__ import annotations

from typing import Any

import polars as pl

from hunt_core.detect import fusion as Fz
from hunt_core.detect import phase as Ph
from hunt_core.detect.magnitude_cache import magnitude_history_for_frame
from hunt_core.detect.result import Detection, build_detection
from hunt_core.detect.windows import DEFAULT_LOOKBACK, build_window


def build_live_detection(
    symbol: str,
    current_vector: dict[str, Any],
    *,
    tf: str = "15m",
    lookback: int = DEFAULT_LOOKBACK,
    q_gate: float = Fz.DEFAULT_Q_GATE,
    q_phase: float = Ph.DEFAULT_Q_PHASE,
) -> Detection:
    """Run the fusion engine for the current closed bar (lake history + current snapshot)."""
    from hunt_core.data.lake import query_features

    try:
        hist = query_features(symbol, tf=tf, limit=max(lookback, 1))
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
        window = build_window(frame, symbol=sym, tf=tf, lookback=lookback)
        return build_detection(window, magnitude_history=None, q_gate=q_gate, q_phase=q_phase)

    ts_max = None
    if "ts" in frame.columns and frame.height:
        ts_max = frame.get_column("ts")[-1]
        if ts_max is not None:
            ts_max = str(ts_max)

    mag_hist = magnitude_history_for_frame(
        frame, symbol=sym, tf=tf, lookback=lookback
    )
    window = build_window(frame, symbol=sym, tf=tf, lookback=lookback, ts_max=ts_max)
    return build_detection(
        window, magnitude_history=mag_hist, q_gate=q_gate, q_phase=q_phase
    )


__all__ = ["build_live_detection"]
