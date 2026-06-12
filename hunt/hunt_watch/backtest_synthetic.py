"""Synthetic hunt signals from pump_history leg events for backtest sample growth."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from hunt_watch.pump_history import PumpHistoryStore, load_pump_history

LegKind = Literal["pump", "dump"]


def _parse_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def synthetic_levels(
    kind: LegKind,
    price: float,
    *,
    change_24h_pct: float | None = None,
) -> dict[str, Any]:
    """Approximate hunt TP/SL from leg entry when tick setup is unavailable."""
    if price <= 0:
        return {}
    vol_pct = max(2.5, min(abs(change_24h_pct or 12.0) * 0.12, 7.5))
    sl_pct = round(vol_pct * 0.85, 3)
    tp1_pct = round(vol_pct * 0.55, 3)
    tp2_pct = round(vol_pct * 1.05, 3)
    if kind == "pump":
        return {
            "direction": "short",
            "entry_lo": round(price * 0.998, 6),
            "entry_hi": round(price, 6),
            "stop_loss": round(price * (1.0 + sl_pct / 100.0), 6),
            "tp1": round(price * (1.0 - tp1_pct / 100.0), 6),
            "tp2": round(price * (1.0 - tp2_pct / 100.0), 6),
        }
    return {
        "direction": "long",
        "entry_lo": round(price, 6),
        "entry_hi": round(price * 1.002, 6),
        "stop_loss": round(price * (1.0 - sl_pct / 100.0), 6),
        "tp1": round(price * (1.0 + tp1_pct / 100.0), 6),
        "tp2": round(price * (1.0 + tp2_pct / 100.0), 6),
    }


def atr_pct_from_klines(klines: list[list[Any]], *, period: int = 14) -> float | None:
    """Wilder-style ATR% from raw Binance klines (list rows: [openT,o,h,l,c,...])."""
    if not klines or len(klines) < period + 1:
        return None
    trs: list[float] = []
    prev_close = float(klines[0][4])
    for k in klines[1:]:
        hi, lo, close = float(k[2]), float(k[3]), float(k[4])
        tr = max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
        trs.append(tr)
        prev_close = close
    if len(trs) < period:
        return None
    # Wilder smoothing
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    last_close = float(klines[-1][4])
    if last_close <= 0:
        return None
    return round(atr / last_close * 100.0, 4)


def atr_levels(kind: LegKind, price: float, atr_pct: float) -> dict[str, Any]:
    """Realistic fade levels from actual volatility (ATR%) — enriched path.

    Uses ATR multiples so R:R reflects the symbol's true volatility instead of a
    flat 24h-change heuristic. Fade: short a pump / long a dump.
    """
    if price <= 0 or atr_pct <= 0:
        return {}
    sl_pct = round(atr_pct * 1.5, 3)
    tp1_pct = round(atr_pct * 1.2, 3)
    tp2_pct = round(atr_pct * 2.5, 3)
    if kind == "pump":
        return {
            "direction": "short",
            "entry_lo": round(price * 0.998, 6),
            "entry_hi": round(price, 6),
            "stop_loss": round(price * (1.0 + sl_pct / 100.0), 6),
            "tp1": round(price * (1.0 - tp1_pct / 100.0), 6),
            "tp2": round(price * (1.0 - tp2_pct / 100.0), 6),
            "atr_pct": atr_pct,
        }
    return {
        "direction": "long",
        "entry_lo": round(price, 6),
        "entry_hi": round(price * 1.002, 6),
        "stop_loss": round(price * (1.0 - sl_pct / 100.0), 6),
        "tp1": round(price * (1.0 + tp1_pct / 100.0), 6),
        "tp2": round(price * (1.0 + tp2_pct / 100.0), 6),
        "atr_pct": atr_pct,
    }


def leg_events_to_signals(
    store: PumpHistoryStore | None = None,
    *,
    limit: int = 100,
    dedupe_hours: int = 6,
) -> list[dict[str, Any]]:
    """Build pseudo-signals from pump_history leg_pump / leg_dump events."""
    store = store or load_pump_history()
    events = [
        e
        for e in store.event_log
        if e.get("type") in ("leg_pump", "leg_dump") and e.get("symbol") and e.get("ts")
    ]
    events.sort(key=lambda e: str(e.get("ts") or ""), reverse=True)

    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for event in events:
        sym = str(event["symbol"]).upper()
        kind: LegKind = "dump" if event["type"] == "leg_dump" else "pump"
        ts = _parse_ts(str(event["ts"]))
        if ts is None:
            continue
        bucket = f"{ts.date()}:{ts.hour // max(dedupe_hours, 1)}"
        key = (sym, kind, bucket)
        if key in seen:
            continue
        seen.add(key)
        price = float(event.get("price") or 0)
        if price <= 0:
            continue
        levels = synthetic_levels(
            kind,
            price,
            change_24h_pct=float(event["change_24h_pct"])
            if event.get("change_24h_pct") is not None
            else None,
        )
        if not levels.get("tp1") or not levels.get("stop_loss"):
            continue
        phase = "dump_active" if kind == "pump" else "post_dump_bounce"
        out.append(
            {
                "source": "pump_history",
                "leg_kind": kind,
                "leg_source": event.get("source"),
                "symbol": sym,
                "direction": levels["direction"],
                "lifecycle_phase": phase,
                "entry_lifecycle_phase": phase,
                "opened_at": ts.isoformat(),
                "entry_lo": levels["entry_lo"],
                "entry_hi": levels["entry_hi"],
                "stop_loss": levels["stop_loss"],
                "tp1": levels["tp1"],
                "tp2": levels["tp2"],
                "change_24h_pct": event.get("change_24h_pct"),
            }
        )
        if len(out) >= limit:
            break
    return out
