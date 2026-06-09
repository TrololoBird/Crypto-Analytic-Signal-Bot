"""Resolve Polars work frames from signal/catalog entry TF tags."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import polars as pl

    from ..domain.schemas import PreparedSymbol, Signal

_VALID_TFS = frozenset({"5m", "15m", "1h", "4h"})
_FRAME_ATTR = {"5m": "work_5m", "15m": "work_15m", "1h": "work_1h", "4h": "work_4h"}


def normalize_tf_label(tf: str | None, *, default: str = "15m") -> str:
    raw = str(tf or default).strip().lower()
    if "+" in raw:
        raw = raw.split("+", 1)[0].strip()
    if "/" in raw:
        raw = raw.split("/", 1)[0].strip()
    return raw if raw in _VALID_TFS else default


def resolve_entry_frame(
    prepared: PreparedSymbol,
    *,
    entry_tf: str | None = None,
    signal: Signal | None = None,
) -> pl.DataFrame:
    """Return the Polars frame for the signal entry timeframe with safe fallbacks."""
    tf = normalize_tf_label(
        entry_tf or (getattr(signal, "entry_tf", None) if signal is not None else None)
    )
    primary = getattr(prepared, _FRAME_ATTR[tf], None)
    if primary is not None and not primary.is_empty():
        return primary
    for fallback in ("work_15m", "work_1h", "work_5m", "work_4h"):
        frame = getattr(prepared, fallback, None)
        if frame is not None and not frame.is_empty():
            return frame
    return prepared.work_15m


def resolve_context_frame(
    prepared: PreparedSymbol,
    *,
    context_tf: str | None = None,
    signal: Signal | None = None,
) -> pl.DataFrame:
    """HTF context frame — prefers 4h then 1h."""
    if signal is not None and signal.context_tfs:
        for raw_tf in signal.context_tfs:
            tf = normalize_tf_label(raw_tf)
            frame = getattr(prepared, _FRAME_ATTR.get(tf, ""), None)
            if frame is not None and not frame.is_empty():
                return frame
    tf = normalize_tf_label(context_tf, default="4h")
    frame = getattr(prepared, _FRAME_ATTR.get(tf, "work_4h"), None)
    if frame is not None and not frame.is_empty():
        return frame
    if prepared.work_1h is not None and not prepared.work_1h.is_empty():
        return prepared.work_1h
    return resolve_entry_frame(prepared, signal=signal)
