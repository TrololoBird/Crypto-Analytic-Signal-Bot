"""Per-symbol session memory — hunt_high/low beyond REST impulse window."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hunt_watch.paths import SESSION_DIR

SESSION_TTL_HOURS = 48.0
_PHASE_WINDOW_HOURS = 2.0


@dataclass(slots=True)
class SymbolSession:
    symbol: str
    hunt_high: float = 0.0
    hunt_low: float = 0.0
    price_high: float = 0.0
    price_low: float = 0.0
    last_price: float = 0.0
    last_phase: str | None = None
    phase_history: list[dict[str, str]] = field(default_factory=list)
    ws_liq_min_5m: float | None = None
    ws_agg_min_30s: float | None = None
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _path(symbol: str, root: Path = SESSION_DIR) -> Path:
    return root / f"{symbol.upper()}.json"


def load_session(symbol: str, *, root: Path = SESSION_DIR) -> SymbolSession | None:
    p = _path(symbol, root)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    sym = str(raw.get("symbol") or symbol).upper()
    return SymbolSession(
        symbol=sym,
        hunt_high=float(raw.get("hunt_high") or 0),
        hunt_low=float(raw.get("hunt_low") or 0),
        price_high=float(raw.get("price_high") or 0),
        price_low=float(raw.get("price_low") or 0),
        last_price=float(raw.get("last_price") or 0),
        last_phase=raw.get("last_phase"),
        phase_history=list(raw.get("phase_history") or []),
        ws_liq_min_5m=raw.get("ws_liq_min_5m"),
        ws_agg_min_30s=raw.get("ws_agg_min_30s"),
        updated_at=str(raw.get("updated_at") or ""),
    )


def save_session(sess: SymbolSession, *, root: Path = SESSION_DIR) -> None:
    root.mkdir(parents=True, exist_ok=True)
    sess.updated_at = datetime.now(UTC).isoformat()
    _path(sess.symbol, root).write_text(
        json.dumps(sess.to_dict(), indent=2),
        encoding="utf-8",
    )


def _prune_phase_history(history: list[dict[str, str]], *, now: datetime) -> list[dict[str, str]]:
    cutoff = now - timedelta(hours=_PHASE_WINDOW_HOURS)
    kept: list[dict[str, str]] = []
    for item in history:
        try:
            ts = datetime.fromisoformat(str(item.get("ts") or ""))
        except ValueError:
            continue
        if ts >= cutoff:
            kept.append(item)
    return kept[-40:]


def merge_hunt_extremes(
    symbol: str,
    *,
    price: float,
    rest_hunt_high: float,
    rest_hunt_low: float,
    lifecycle_phase: str,
    market: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[float, float, dict[str, Any]]:
    """Blend REST impulse window with rolling session peaks (48h TTL)."""
    ts = now or datetime.now(UTC)
    sym = symbol.upper()
    sess = load_session(sym) or SymbolSession(symbol=sym)

    if sess.updated_at:
        try:
            last = datetime.fromisoformat(sess.updated_at)
            if ts - last > timedelta(hours=SESSION_TTL_HOURS):
                sess = SymbolSession(symbol=sym)
        except ValueError:
            pass

    if price > 0:
        sess.last_price = price
        if sess.price_high <= 0 or price > sess.price_high:
            sess.price_high = price
        if sess.price_low <= 0 or price < sess.price_low:
            sess.price_low = price

    rh = max(rest_hunt_high, sess.hunt_high, sess.price_high, price) if price > 0 else max(
        rest_hunt_high, sess.hunt_high
    )
    candidates_lo = [x for x in (rest_hunt_low, sess.hunt_low, sess.price_low, price) if x > 0]
    rl = min(candidates_lo) if candidates_lo else rest_hunt_low

    sess.hunt_high = rh
    sess.hunt_low = rl if rl > 0 else sess.hunt_low

    phase = str(lifecycle_phase or "")
    if phase and phase != sess.last_phase:
        sess.phase_history = _prune_phase_history(sess.phase_history, now=ts)
        sess.phase_history.append({"ts": ts.isoformat(), "phase": phase})
        sess.last_phase = phase
    elif phase:
        sess.last_phase = phase

    mkt = market or {}
    liq = mkt.get("liquidation_score_5m")
    if liq is not None:
        v = float(liq)
        sess.ws_liq_min_5m = v if sess.ws_liq_min_5m is None else min(sess.ws_liq_min_5m, v)
    agg = mkt.get("agg_trade_delta_30s")
    if agg is not None:
        v = float(agg)
        sess.ws_agg_min_30s = v if sess.ws_agg_min_30s is None else min(sess.ws_agg_min_30s, v)

    save_session(sess)
    meta = {
        "session_hunt_high": round(sess.hunt_high, 6),
        "session_hunt_low": round(sess.hunt_low, 6),
        "phase_changes_2h": len(sess.phase_history),
        "ws_liq_min_5m": sess.ws_liq_min_5m,
        "rest_hunt_high": round(rest_hunt_high, 6),
        "merged": True,
    }
    return round(rh, 6), round(rl, 6) if rl > 0 else round(rest_hunt_low, 6), meta
