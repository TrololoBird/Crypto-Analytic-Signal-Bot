"""1m return CUSUM pump/dump cycle + BTC-residual decoupling (Phase 4C)."""
from __future__ import annotations

from typing import Any, Literal

import polars as pl

from hunt_core.features.snapshot import btc_beta_1h

PumpEvent = Literal["start", "peak", "end"]

_DEFAULT_CUSUM_THRESHOLD = 2.5
_DEFAULT_RESIDUAL_THRESHOLD = 2.0
_CUSUM_CLIP = 500.0


def _log_returns(close: pl.Series) -> pl.Series:
    c = close.cast(pl.Float64, strict=False)
    return (c / c.shift(1)).log().fill_null(0.0).fill_nan(0.0)


def _standardized_returns(returns: pl.Series, *, span: int = 96) -> pl.Series:
    mu = returns.ewm_mean(span=span, min_periods=8)
    dev = (returns - mu).ewm_std(span=span, min_periods=8).fill_null(1e-6).clip(lower_bound=1e-6)
    return ((returns - mu) / dev).fill_null(0.0).fill_nan(0.0)


def cusum_series(z: pl.Series, *, threshold: float = _DEFAULT_CUSUM_THRESHOLD) -> pl.Series:
    """Signed CUSUM on standardized returns — mean-reverting via EWM anchor."""
    pos = 0.0
    neg = 0.0
    out: list[float] = []
    for v in z.to_list():
        try:
            x = float(v or 0.0)
        except (TypeError, ValueError):
            x = 0.0
        pos = max(0.0, pos + x - threshold * 0.25)
        neg = min(0.0, neg + x + threshold * 0.25)
        out.append(max(-_CUSUM_CLIP, min(_CUSUM_CLIP, pos if x >= 0 else neg)))
    return pl.Series(out)


def _discrete_events(cusum: pl.Series, *, threshold: float) -> list[dict[str, Any]]:
    """Map CUSUM crossings to {start, peak, end} events on the 1m series."""
    events: list[dict[str, Any]] = []
    vals = [float(v or 0.0) for v in cusum.to_list()]
    if not vals:
        return events
    state = "idle"
    peak_idx = 0
    peak_val = 0.0
    for i, v in enumerate(vals):
        if state == "idle" and abs(v) >= threshold:
            state = "up" if v > 0 else "down"
            events.append({"idx": i, "event": "start", "side": "pump" if v > 0 else "dump", "cusum": round(v, 3)})
            peak_idx = i
            peak_val = v
        elif state in {"up", "down"}:
            if (state == "up" and v >= peak_val) or (state == "down" and v <= peak_val):
                peak_val = v
                peak_idx = i
            elif abs(v) < threshold * 0.35:
                events.append(
                    {
                        "idx": peak_idx,
                        "event": "peak",
                        "side": "pump" if state == "up" else "dump",
                        "cusum": round(peak_val, 3),
                    }
                )
                events.append({"idx": i, "event": "end", "side": "pump" if state == "up" else "dump", "cusum": round(v, 3)})
                state = "idle"
                peak_val = 0.0
    if state != "idle":
        events.append(
            {
                "idx": peak_idx,
                "event": "peak",
                "side": "pump" if state == "up" else "dump",
                "cusum": round(peak_val, 3),
            }
        )
    return events


def detect_pump_cycle_events(
    work_1m: pl.DataFrame | None,
    *,
    threshold: float = _DEFAULT_CUSUM_THRESHOLD,
) -> dict[str, Any]:
    """Standardized 1m return CUSUM → discrete cycle events {start, peak, end}."""
    empty: dict[str, Any] = {
        "active": False,
        "side": None,
        "event": None,
        "cusum": 0.0,
        "events": [],
    }
    if work_1m is None or work_1m.is_empty() or "close" not in work_1m.columns:
        return empty
    rets = _log_returns(work_1m["close"])
    z = _standardized_returns(rets)
    cusum = cusum_series(z, threshold=threshold)
    events = _discrete_events(cusum, threshold=threshold)
    cur = float(cusum[-1]) if cusum.len() else 0.0
    side: str | None = None
    if cur >= threshold:
        side = "pump"
    elif cur <= -threshold:
        side = "dump"
    last_event: PumpEvent | None = None
    if events:
        last_event = events[-1].get("event")  # type: ignore[assignment]
    return {
        "active": side is not None,
        "side": side,
        "event": last_event,
        "cusum": round(cur, 3),
        "events": events[-6:],
    }


def btc_residual_returns(
    sym_work_1m: pl.DataFrame,
    btc_work_1m: pl.DataFrame,
    *,
    beta: float | None = None,
    lookback: int = 48,
) -> pl.Series | None:
    """Symbol return minus beta × BTC return on aligned 1m bars."""
    if sym_work_1m.is_empty() or btc_work_1m.is_empty():
        return None
    if beta is None:
        beta = btc_beta_1h(sym_work_1m, btc_work_1m, lookback=lookback)
    if beta is None:
        return None
    sym_r = _log_returns(sym_work_1m["close"])
    btc_r = _log_returns(btc_work_1m["close"])
    n = min(sym_r.len(), btc_r.len())
    if n < 12:
        return None
    residual = sym_r.tail(n) - float(beta) * btc_r.tail(n)
    return residual.fill_null(0.0).fill_nan(0.0)


def btc_decoupled_flags(
    sym_work_1m: pl.DataFrame | None,
    btc_work_1m: pl.DataFrame | None,
    *,
    beta: float | None = None,
    threshold: float = _DEFAULT_RESIDUAL_THRESHOLD,
) -> dict[str, bool]:
    """Residual CUSUM changepoint → btc_decoupled_pump / btc_decoupled_dump."""
    out = {"pump": False, "dump": False}
    if sym_work_1m is None or btc_work_1m is None:
        return out
    residual = btc_residual_returns(sym_work_1m, btc_work_1m, beta=beta)
    if residual is None or residual.len() < 12:
        return out
    z = _standardized_returns(residual)
    cusum = cusum_series(z, threshold=threshold)
    cur = float(cusum[-1]) if cusum.len() else 0.0
    out["pump"] = cur >= threshold
    out["dump"] = cur <= -threshold
    return out


__all__ = [
    "PumpEvent",
    "btc_decoupled_flags",
    "btc_residual_returns",
    "cusum_series",
    "detect_pump_cycle_events",
]
