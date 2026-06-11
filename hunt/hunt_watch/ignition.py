"""Fast pump/dump ignition from consecutive 24h-ticker snapshots (no extra API calls)."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from hunt_watch.adaptive_thresholds import (
    AdaptiveStore,
    ignition_passes,
    update_tick_delta,
)
from hunt_watch.paths import IGNITION_STATE

IgnitionDirection = Literal["pump", "dump"]

# Defaults mirror watch.py — override via process_ticker_snapshots kwargs if needed.
DEFAULT_WINDOW_S = 300.0
DEFAULT_MIN_PCT = 2.5
DEFAULT_MIN_VOL_DELTA_USD = 250_000.0
DEFAULT_MIN_QVOL_USD = 3_000_000.0
DEFAULT_TTL_S = 7200.0

# Majors — too noisy / already on default watchlist.
_IGNITION_SKIP = frozenset({"BTCUSDT", "ETHUSDT", "BNBUSDT"})


@dataclass(slots=True)
class TickerPoint:
    price: float
    quote_volume: float
    ts: float  # epoch seconds (UTC)


@dataclass(frozen=True, slots=True)
class IgnitionEvent:
    symbol: str
    direction: IgnitionDirection
    price_delta_pct: float
    vol_delta_usd: float
    quote_volume_usd: float
    window_s: float
    ignited_at: str


@dataclass(slots=True)
class ActiveIgnition:
    symbol: str
    direction: IgnitionDirection
    price_delta_pct: float
    vol_delta_usd: float
    quote_volume_usd: float
    window_s: float
    ignited_at: str
    expires_at: str
    notified: bool = False

    def to_row(self) -> dict[str, Any]:
        return {
            "active": True,
            "direction": self.direction,
            "price_delta_pct": round(self.price_delta_pct, 2),
            "vol_delta_usd": round(self.vol_delta_usd, 0),
            "quote_volume_usd": round(self.quote_volume_usd, 0),
            "window_s": round(self.window_s, 1),
            "ignited_at": self.ignited_at,
            "expires_at": self.expires_at,
        }


@dataclass(slots=True)
class IgnitionState:
    prev_snapshot: dict[str, TickerPoint] = field(default_factory=dict)
    active: dict[str, ActiveIgnition] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> IgnitionState:
        prev: dict[str, TickerPoint] = {}
        for sym, pt in (raw.get("prev_snapshot") or {}).items():
            if not isinstance(pt, dict):
                continue
            price = _safe_float(pt.get("price"))
            vol = _safe_float(pt.get("quote_volume"))
            ts = _safe_float(pt.get("ts"))
            if price and price > 0 and vol is not None and ts is not None:
                prev[str(sym).upper()] = TickerPoint(price=price, quote_volume=vol, ts=ts)
        active: dict[str, ActiveIgnition] = {}
        for sym, item in (raw.get("active") or {}).items():
            if not isinstance(item, dict):
                continue
            direction = str(item.get("direction") or "pump")
            if direction not in ("pump", "dump"):
                direction = "pump"
            active[str(sym).upper()] = ActiveIgnition(
                symbol=str(sym).upper(),
                direction=direction,  # type: ignore[arg-type]
                price_delta_pct=float(item.get("price_delta_pct") or 0),
                vol_delta_usd=float(item.get("vol_delta_usd") or 0),
                quote_volume_usd=float(item.get("quote_volume_usd") or 0),
                window_s=float(item.get("window_s") or 0),
                ignited_at=str(item.get("ignited_at") or ""),
                expires_at=str(item.get("expires_at") or ""),
                notified=bool(item.get("notified")),
            )
        return cls(prev_snapshot=prev, active=active)

    def to_dict(self) -> dict[str, Any]:
        return {
            "prev_snapshot": {
                sym: {
                    "price": pt.price,
                    "quote_volume": pt.quote_volume,
                    "ts": pt.ts,
                }
                for sym, pt in self.prev_snapshot.items()
            },
            "active": {
                sym: {
                    **asdict(ig),
                }
                for sym, ig in self.active.items()
            },
        }


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        numeric = float(value)
    except TypeError, ValueError:
        return default
    if not math.isfinite(numeric):
        return default
    return numeric


def detect_ignitions(
    current_rows: list[dict[str, Any]],
    prev: dict[str, TickerPoint],
    *,
    now: datetime,
    now_ts: float,
    window_s: float = DEFAULT_WINDOW_S,
    min_pct: float = DEFAULT_MIN_PCT,
    min_vol_delta_usd: float = DEFAULT_MIN_VOL_DELTA_USD,
    min_qvol_usd: float = DEFAULT_MIN_QVOL_USD,
    adaptive: AdaptiveStore | None = None,
) -> tuple[list[IgnitionEvent], dict[str, TickerPoint]]:
    """Compare ticker rows to previous snapshot; return new ignition events + updated snapshot."""
    events: list[IgnitionEvent] = []
    next_snap: dict[str, TickerPoint] = {}

    for row in current_rows:
        sym = str(row.get("symbol") or "").strip().upper()
        if not sym or sym in _IGNITION_SKIP:
            continue
        price = _safe_float(row.get("last_price"))
        quote_volume = _safe_float(row.get("quote_volume"), 0.0)
        if price is None or price <= 0 or quote_volume is None or quote_volume <= 0:
            continue
        next_snap[sym] = TickerPoint(price=price, quote_volume=quote_volume, ts=now_ts)

        baseline = prev.get(sym)
        if baseline is None:
            continue
        age_s = now_ts - baseline.ts
        if age_s <= 0 or age_s > window_s:
            continue
        if quote_volume < min_qvol_usd:
            continue
        price_delta_pct = (price / baseline.price - 1.0) * 100.0
        vol_delta = quote_volume - baseline.quote_volume
        if adaptive is not None:
            move_ok, _z, _mode = ignition_passes(
                adaptive, sym, delta_pct=price_delta_pct, static_min_pct=min_pct
            )
            update_tick_delta(adaptive, sym, price_delta_pct)
        else:
            move_ok = abs(price_delta_pct) >= min_pct
        if not move_ok or vol_delta < min_vol_delta_usd:
            continue
        direction: IgnitionDirection = "pump" if price_delta_pct > 0 else "dump"
        events.append(
            IgnitionEvent(
                symbol=sym,
                direction=direction,
                price_delta_pct=price_delta_pct,
                vol_delta_usd=vol_delta,
                quote_volume_usd=quote_volume,
                window_s=age_s,
                ignited_at=now.isoformat(),
            )
        )

    return events, next_snap


def merge_ignitions(
    state: IgnitionState,
    events: list[IgnitionEvent],
    *,
    now: datetime,
    ttl_s: float = DEFAULT_TTL_S,
) -> tuple[list[IgnitionEvent], IgnitionState]:
    """Apply TTL expiry, register new ignitions; return only newly-added events."""
    expires_cutoff = now
    active: dict[str, ActiveIgnition] = {}
    for sym, ig in state.active.items():
        try:
            exp = datetime.fromisoformat(ig.expires_at)
        except ValueError:
            continue
        if exp > expires_cutoff:
            active[sym] = ig

    new_events: list[IgnitionEvent] = []
    for ev in events:
        sym = ev.symbol.upper()
        if sym in active:
            # Refresh TTL on repeat spike within window.
            active[sym] = ActiveIgnition(
                symbol=sym,
                direction=ev.direction,
                price_delta_pct=ev.price_delta_pct,
                vol_delta_usd=ev.vol_delta_usd,
                quote_volume_usd=ev.quote_volume_usd,
                window_s=ev.window_s,
                ignited_at=ev.ignited_at,
                expires_at=(now + timedelta(seconds=ttl_s)).isoformat(),
                notified=active[sym].notified,
            )
            continue
        active[sym] = ActiveIgnition(
            symbol=sym,
            direction=ev.direction,
            price_delta_pct=ev.price_delta_pct,
            vol_delta_usd=ev.vol_delta_usd,
            quote_volume_usd=ev.quote_volume_usd,
            window_s=ev.window_s,
            ignited_at=ev.ignited_at,
            expires_at=(now + timedelta(seconds=ttl_s)).isoformat(),
            notified=False,
        )
        new_events.append(ev)

    state.active = active
    return new_events, state


def process_ticker_snapshots(
    ticker_rows: list[dict[str, Any]],
    state: IgnitionState,
    *,
    now: datetime | None = None,
    window_s: float = DEFAULT_WINDOW_S,
    min_pct: float = DEFAULT_MIN_PCT,
    min_vol_delta_usd: float = DEFAULT_MIN_VOL_DELTA_USD,
    min_qvol_usd: float = DEFAULT_MIN_QVOL_USD,
    ttl_s: float = DEFAULT_TTL_S,
    adaptive: AdaptiveStore | None = None,
) -> tuple[list[IgnitionEvent], IgnitionState]:
    """Full tick: detect → merge → refresh prev snapshot."""
    now = now or datetime.now(UTC)
    now_ts = now.timestamp()
    events, next_snap = detect_ignitions(
        ticker_rows,
        state.prev_snapshot,
        now=now,
        now_ts=now_ts,
        window_s=window_s,
        min_pct=min_pct,
        min_vol_delta_usd=min_vol_delta_usd,
        min_qvol_usd=min_qvol_usd,
        adaptive=adaptive,
    )
    state.prev_snapshot = next_snap
    new_events, state = merge_ignitions(state, events, now=now, ttl_s=ttl_s)
    return new_events, state


def load_ignition_state(path: Path = IGNITION_STATE) -> IgnitionState:
    if not path.exists():
        return IgnitionState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return IgnitionState()
    if not isinstance(raw, dict):
        return IgnitionState()
    return IgnitionState.from_dict(raw)


def save_ignition_state(state: IgnitionState, path: Path = IGNITION_STATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")


def active_ignition_map(state: IgnitionState) -> dict[str, dict[str, Any]]:
    return {sym: ig.to_row() for sym, ig in state.active.items()}


def mark_ignition_notified(state: IgnitionState, symbol: str) -> None:
    sym = symbol.upper()
    if sym in state.active:
        state.active[sym].notified = True


def pending_ignition_alerts(state: IgnitionState) -> list[ActiveIgnition]:
    return [ig for ig in state.active.values() if not ig.notified]


def format_ignition_telegram(ig: ActiveIgnition | IgnitionEvent) -> str:
    sym = str(ig.symbol).replace("USDT", "-USDT")
    direction = ig.direction
    arrow = "🚀" if direction == "pump" else "📉"
    bias = "short fade" if direction == "pump" else "long bounce"
    pct = ig.price_delta_pct
    vol_m = ig.vol_delta_usd / 1e6
    qvol_m = ig.quote_volume_usd / 1e6
    window = getattr(ig, "window_s", 0)
    return (
        f"🔥 <b>IGNITION</b> {arrow} <code>{sym}</code>\n"
        f"<code>{pct:+.2f}%</code> in <code>{window:.0f}s</code> · "
        f"vol +<code>${vol_m:.2f}M</code> · 24h qvol <code>${qvol_m:.1f}M</code>\n"
        f"Watch bias: <b>{bias}</b> · added to minute-watch\n"
        f"<i>Signal-only · ignition radar · открывай сделку вручную.</i>"
    )
