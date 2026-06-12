"""Orphan signal reconciliation."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from hunt_watch.signal_tracker import HuntFollowUp, reconcile_signal


def reconcile_orphan(
    state: dict[str, Any],
    *,
    symbol: str,
    direction: str,
    hi: float,
    lo: float,
    last_price: float,
    ts: datetime,
) -> list[HuntFollowUp]:
    """Reconcile one active signal against kline extremes."""
    return reconcile_signal(
        state,
        symbol=symbol,
        direction=direction,
        hi=hi,
        lo=lo,
        last_price=last_price,
        ts=ts,
    )
