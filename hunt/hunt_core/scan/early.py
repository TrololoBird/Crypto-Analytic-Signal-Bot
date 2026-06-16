"""Adaptive thresholds, ignition, and early alerts (wave 3C)."""
from __future__ import annotations

import html
import json
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import polars as pl

from hunt_core.data.universe import watchlist_flags
from hunt_core.params.store import effective_hunt_params
from hunt_core.paths import ADAPTIVE_THRESHOLDS, EWMA_THRESHOLDS, IGNITION_STATE

EWMA_ALPHA = 0.12
VAR_FLOOR = 1e-8
MIN_TICK_SAMPLES = 12
MIN_CHANGE_SAMPLES = 8
STATIC_IGNITION_MIN_PCT = 2.5
STATIC_RANGE_HOT_PCT = 8.0
STATIC_PUMP_EXTREME_PCT = 15.0
Z_IGNITION = 2.0
Z_RANGE_HOT = 1.5
Z_PUMP_EXTREME = 2.5
TICK_FLOOR_PCT = 0.35

def _sigma(var: float) -> float:
    return float(pl.Series([max(var, VAR_FLOOR)]).sqrt()[0])


@dataclass(slots=True)
class SymbolAdaptive:
    symbol: str
    tick_mu: float = 0.0
    tick_var: float = 1.0
    tick_n: int = 0
    chg_mu: float = 5.0
    chg_var: float = 16.0
    chg_n: int = 0

    def to_public(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "tick_n": self.tick_n,
            "tick_mu_abs_pct": round(self.tick_mu, 3),
            "tick_sigma_pct": round(_sigma(self.tick_var), 3),
            "chg_n": self.chg_n,
            "chg_mu_abs_pct": round(self.chg_mu, 2),
            "chg_sigma_pct": round(_sigma(self.chg_var), 2),
            "adaptive_ready": self.tick_n >= MIN_TICK_SAMPLES or self.chg_n >= MIN_CHANGE_SAMPLES,
        }


@dataclass(slots=True)
class AdaptiveStore:
    symbols: dict[str, SymbolAdaptive] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AdaptiveStore:
        symbols: dict[str, SymbolAdaptive] = {}
        for sym, item in (raw.get("symbols") or {}).items():
            if not isinstance(item, dict):
                continue
            symbols[str(sym).upper()] = SymbolAdaptive(
                symbol=str(sym).upper(),
                tick_mu=float(item.get("tick_mu") or 0),
                tick_var=float(item.get("tick_var") or 1),
                tick_n=int(item.get("tick_n") or 0),
                chg_mu=float(item.get("chg_mu") or 5),
                chg_var=float(item.get("chg_var") or 16),
                chg_n=int(item.get("chg_n") or 0),
            )
        return cls(symbols=symbols)

    def to_dict(self) -> dict[str, Any]:
        return {"symbols": {sym: asdict(st) for sym, st in self.symbols.items()}}


def _read_ewma_raw(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return None
    return raw if isinstance(raw, dict) else None


def load_adaptive_store(path: Path = EWMA_THRESHOLDS) -> AdaptiveStore:
    for candidate in (path, EWMA_THRESHOLDS, ADAPTIVE_THRESHOLDS):
        raw = _read_ewma_raw(candidate)
        if raw is None:
            continue
        if "symbols" in raw:
            return AdaptiveStore.from_dict(raw)
    return AdaptiveStore()


def save_adaptive_store(store: AdaptiveStore, path: Path = EWMA_THRESHOLDS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store.to_dict(), indent=2), encoding="utf-8")
    if path != ADAPTIVE_THRESHOLDS and ADAPTIVE_THRESHOLDS.exists():
        try:
            legacy = _read_ewma_raw(ADAPTIVE_THRESHOLDS) or {}
            if any(k in legacy for k in ("universal", "per_symbol", "outcome_calibration")):
                ADAPTIVE_THRESHOLDS.write_text(json.dumps(store.to_dict(), indent=2), encoding="utf-8")
        except OSError:
            pass


def _sym(store: AdaptiveStore, symbol: str) -> SymbolAdaptive:
    sym = symbol.upper()
    if sym not in store.symbols:
        store.symbols[sym] = SymbolAdaptive(symbol=sym)
    return store.symbols[sym]


def ewma_update(mean: float, var: float, value: float, *, alpha: float = EWMA_ALPHA) -> tuple[float, float]:
    new_mean = alpha * value + (1.0 - alpha) * mean
    dev = value - new_mean
    new_var = alpha * (dev * dev) + (1.0 - alpha) * var
    return new_mean, max(new_var, VAR_FLOOR)


def zscore(value: float, mean: float, var: float) -> float | None:
    sigma = _sigma(var)
    if sigma <= 0:
        return None
    z = (value - mean) / sigma
    zf = float(z)
    if not (zf == zf):
        return None
    return zf


def update_tick_delta(store: AdaptiveStore, symbol: str, delta_pct: float) -> None:
    st = _sym(store, symbol)
    abs_delta = abs(delta_pct)
    st.tick_mu, st.tick_var = ewma_update(st.tick_mu, st.tick_var, abs_delta)
    st.tick_n += 1


def update_change_24h(store: AdaptiveStore, symbol: str, change_24h_pct: float) -> None:
    st = _sym(store, symbol)
    abs_chg = abs(change_24h_pct)
    st.chg_mu, st.chg_var = ewma_update(st.chg_mu, st.chg_var, abs_chg)
    st.chg_n += 1


def update_from_price_pair(store: AdaptiveStore, symbol: str, *, prev_price: float, price: float) -> float | None:
    if prev_price <= 0 or price <= 0:
        return None
    delta_pct = (price / prev_price - 1.0) * 100.0
    update_tick_delta(store, symbol, delta_pct)
    return delta_pct


def ignition_passes(
    store: AdaptiveStore,
    symbol: str,
    *,
    delta_pct: float,
    static_min_pct: float = STATIC_IGNITION_MIN_PCT,
) -> tuple[bool, float | None, str]:
    abs_delta = abs(delta_pct)
    st = store.symbols.get(symbol.upper())
    if st is None or st.tick_n < MIN_TICK_SAMPLES:
        return abs_delta >= static_min_pct, None, "static"
    z = zscore(abs_delta, st.tick_mu, st.tick_var)
    if z is None:
        return abs_delta >= static_min_pct, None, "static"
    eff_floor = max(TICK_FLOOR_PCT, st.tick_mu + 0.5 * _sigma(st.tick_var))
    return abs_delta >= eff_floor and z >= Z_IGNITION, z, "adaptive"


def change_24h_tier(store: AdaptiveStore, symbol: str, change_24h_pct: float) -> tuple[str | None, float | None, str]:
    move = abs(change_24h_pct)
    st = store.symbols.get(symbol.upper())
    if st is None or st.chg_n < MIN_CHANGE_SAMPLES:
        if move >= STATIC_PUMP_EXTREME_PCT:
            return "extreme", None, "static"
        if move >= STATIC_RANGE_HOT_PCT:
            return "hot", None, "static"
        return None, None, "static"
    z = zscore(move, st.chg_mu, st.chg_var)
    if z is None:
        if move >= STATIC_PUMP_EXTREME_PCT:
            return "extreme", None, "static"
        if move >= STATIC_RANGE_HOT_PCT:
            return "hot", None, "static"
        return None, None, "static"
    if z >= Z_PUMP_EXTREME:
        return "extreme", z, "adaptive"
    if z >= Z_RANGE_HOT:
        return "hot", z, "adaptive"
    return None, z, "adaptive"


def adaptive_hot_pct(store: AdaptiveStore, symbol: str) -> float:
    st = store.symbols.get(symbol.upper())
    if st is None or st.chg_n < MIN_CHANGE_SAMPLES:
        return STATIC_RANGE_HOT_PCT
    return max(STATIC_RANGE_HOT_PCT * 0.5, st.chg_mu + Z_RANGE_HOT * _sigma(st.chg_var))


def adaptive_extreme_pct(store: AdaptiveStore, symbol: str) -> float:
    st = store.symbols.get(symbol.upper())
    if st is None or st.chg_n < MIN_CHANGE_SAMPLES:
        return STATIC_PUMP_EXTREME_PCT
    return max(STATIC_PUMP_EXTREME_PCT * 0.5, st.chg_mu + Z_PUMP_EXTREME * _sigma(st.chg_var))

ADVISORY_PHASES = frozenset({
    "impulse_initiating", "post_dump_bounce", "distribution", "exhaustion_at_high", "dump_initiating",
})

def is_advisory_phase(lifecycle: dict[str, Any] | None) -> bool:
    return isinstance(lifecycle, dict) and str(lifecycle.get("phase") or "") in ADVISORY_PHASES

def _prescan_outlier(row: dict[str, Any] | None, direction: str) -> dict[str, Any]:
    ol = (row or {}).get("prescan_outlier") or {}
    if not isinstance(ol, dict):
        return {}
    want = "pump" if direction == "long" else "dump"
    return ol if str(ol.get("direction") or "") == want else {}

def combined_advisory_signal(row: dict[str, Any] | None, *, direction: str) -> dict[str, Any]:
    ign = _ignition_pump(row) if direction == "long" else {}
    ol = _prescan_outlier(row, direction)
    sources = [s for s, ok in (("ignition", bool(ign)), ("outlier", bool(ol))) if ok]
    return {
        "active": bool(sources),
        "ignition_pct": float(ign.get("price_delta_pct") or 0) if ign else 0.0,
        "outlier_pct": float(ol.get("change_pct") or 0) if ol else 0.0,
        "cross_venues": int(ol.get("cross_venues") or 0) if ol else 0,
        "oi_divergence": ol.get("oi_divergence") if ol else None,
        "sources": tuple(sources),
    }

LIQ_BURST_MIN_NOTIONAL_USD = 250_000.0
LIQ_BURST_MIN_EVENTS = 5
LIQ_BURST_SIDE_SKEW = 0.65

@dataclass(frozen=True, slots=True)
class LiquidationBurst:
    symbol: str
    direction: IgnitionDirection
    total_notional_usd: float
    events: int
    score: float
    window_s: int

def detect_liquidation_burst(rollups, *, symbol, events, window_seconds=300, min_notional_usd=LIQ_BURST_MIN_NOTIONAL_USD, min_events=LIQ_BURST_MIN_EVENTS, side_skew=LIQ_BURST_SIDE_SKEW):
    if not rollups:
        return None
    total = _safe_float(rollups.get("liquidation_total_notional"), 0.0) or 0.0
    score = _safe_float(rollups.get("liquidation_score"))
    if score is None or total < min_notional_usd or events < min_events:
        return None
    long_share = 1.0 - score
    if score >= side_skew:
        direction = "pump"
    elif long_share >= side_skew:
        direction = "dump"
    else:
        return None
    return LiquidationBurst(str(symbol).upper(), direction, total, int(events), round(score, 4), int(window_seconds))

def liquidation_burst_from_streams(ws_feed, symbol, *, window_seconds=300):
    if ws_feed is None:
        return None
    return detect_liquidation_burst(ws_feed.liquidation_rollups(symbol, window_seconds=window_seconds), symbol=symbol, events=ws_feed.liquidation_events(symbol, window_seconds=window_seconds), window_seconds=window_seconds)


def format_liquidation_burst_advisory(burst: LiquidationBurst) -> str:
    sym = html.escape(str(burst.symbol).replace("USDT", "-USDT"))
    arrow = "🚀" if burst.direction == "pump" else "📉"
    bias = "short fade" if burst.direction == "pump" else "long bounce"
    notional_m = float(burst.total_notional_usd) / 1e6
    return (
        f"⚡ <b>LIQ BURST</b> {arrow} <code>{sym}</code>\n"
        f"<code>${notional_m:.2f}M</code> · <code>{burst.events}</code> events · "
        f"<code>{burst.window_s}s</code> window · score <code>{burst.score:.2f}</code>\n"
        f"Bias: <b>{bias}</b> · early advisory only\n"
        f"<i>Signal-only · liquidation radar · открывай сделку вручную.</i>"
    )


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

EarlyKind = Literal["none", "prep", "imminent", "start", "confirm"]

SHORT_PREP_LC = frozenset({"exhaustion_at_high", "distribution"})
LONG_PREP_LC = frozenset({
    "post_dump_bounce",
    "accumulation",
    "recovery",
    "breakout_arming",
    "impulse_initiating",
})

SHORT_PREP_SETUP = frozenset({"exhaustion_watch", "dump_setup_forming"})
SHORT_START_SETUP = frozenset({"dump_imminent", "dump_initiating"})
LONG_PREP_SETUP = frozenset({"accumulation_watch", "long_setup_forming"})
LONG_START_SETUP = frozenset({"long_imminent", "long_initiating"})

EARLY_COOLDOWN_MIN = {
    "prep": 12,
    "imminent": 8,
    "start": 6,
}


def early_telegram_enabled(symbol: str) -> bool:
    # Read env var at call time (not module import time) — dotenv loads after early.py
    # is first imported by _impl.py, so a module-level constant always sees the default.
    if os.getenv("HUNT_EARLY_DUMP_TG", "0").strip().lower() in {"1", "true", "yes"}:
        return True
    flags = watchlist_flags(symbol)
    return bool(flags.get("early_telegram") or flags.get("dump_hunt"))

# Kept for any external code that references the constant directly.
EARLY_TELEGRAM_ENABLED = os.getenv("HUNT_EARLY_DUMP_TG", "0").strip().lower() in {
    "1",
    "true",
    "yes",
}
TP1_PARTIAL_FIX_PCT = 80


@dataclass(frozen=True, slots=True)
class EarlyAlert:
    kind: EarlyKind
    tier: str  # prep | imminent | start
    message: str


def _lc(lifecycle: Any | None) -> dict[str, Any]:
    if isinstance(lifecycle, dict):
        return lifecycle
    if lifecycle is None:
        return {}
    phase = getattr(lifecycle, "phase", None)
    if hasattr(phase, "value"):
        phase = phase.value
    return {
        "phase": phase,
        "recommended_bias": getattr(lifecycle, "recommended_bias", None),
        "short_entry_ok": getattr(lifecycle, "short_entry_ok", None),
        "fall_from_high_pct": getattr(lifecycle, "fall_from_high_pct", None),
        "bounce_from_low_pct": getattr(lifecycle, "bounce_from_low_pct", None),
    }


def _fuel(setup: dict[str, Any], direction: str) -> float:
    if direction == "short":
        return max(
            float(setup.get("dump_fuel") or 0),
            float(setup.get("dump_score") or 0),
        )
    return max(
        float(setup.get("long_fuel") or 0),
        float(setup.get("long_score") or 0),
    )


def _ignition_pump(row: dict[str, Any] | None) -> dict[str, Any]:
    ign = (row or {}).get("ignition") or {}
    if str(ign.get("direction") or "") == "pump" and ign.get("active"):
        return ign
    return {}


def evaluate_early_alert(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    lifecycle: Any | None = None,
    row: dict[str, Any] | None = None,
) -> EarlyAlert:
    """Whether to send preparation/start Telegram (separate from full confirm)."""
    sym = symbol.upper()
    cal = effective_hunt_params(sym)
    wl = watchlist_flags(sym)
    dump_hunt = bool(wl.get("dump_hunt"))
    lc = _lc(lifecycle)
    lc_phase = str(lc.get("phase") or "")
    setup_phase = str(setup.get("phase") or "")
    fuel = _fuel(setup, direction)
    confirmed = bool(setup.get("confirmed"))
    hard = [str(h) for h in (setup.get("confirm_hard") or [])]
    triggers = [str(t) for t in (setup.get("triggers") or [])]

    if direction == "short":
        price = float((row or {}).get("price") or 0)
        from hunt_core.gate.delivery import price_in_entry_zone  # noqa: PLC0415

        in_zone = price > 0 and price_in_entry_zone(setup, price, direction="short")
        if lc_phase not in SHORT_PREP_LC and not (
            lc_phase == "dump_active" and setup_phase in SHORT_START_SETUP
        ):
            return EarlyAlert("none", "", "")
        if lc_phase in SHORT_PREP_LC and not lc.get("short_entry_ok", True):
            if not (in_zone and setup_phase in SHORT_START_SETUP | {"dump_setup_forming"}):
                return EarlyAlert("none", "", "")

        if confirmed:
            return EarlyAlert("confirm", "confirm", "full_confirm")

        fall_pct = float(lc.get("fall_from_high_pct") or 0)
        if (
            in_zone
            and setup_phase in SHORT_START_SETUP | {"dump_setup_forming", "dump_imminent"}
            and fuel >= cal.forming_min_score
            and fall_pct < 3.0
        ):
            return EarlyAlert(
                "start",
                "start",
                f"В зоне входа · {setup_phase} · fuel {fuel:.0f} · жди/входи по confirm",
            )

        support = float(setup.get("support_break_level") or 0)
        below_support = support > 0 and price > 0 and price < support

        if dump_hunt and below_support and fuel >= cal.forming_min_score + 10:
            return EarlyAlert(
                "start",
                "start",
                f"Пробой support {support:.5f} · fuel {fuel:.0f} · открывай шорт",
            )

        if dump_hunt and fuel >= cal.confirm_min_score and setup_phase in SHORT_PREP_SETUP | SHORT_START_SETUP:
            if below_support or any(
                k in h
                for h in hard
                for k in ("close_below_support", "live_below_support", "rejection", "cascade")
            ):
                return EarlyAlert(
                    "imminent",
                    "imminent",
                    f"Dump hunt armed · {setup_phase} · fuel {fuel:.0f}",
                )

        if setup_phase in SHORT_START_SETUP and fuel >= cal.forming_min_score:
            has_struct = any(
                k in h
                for h in hard
                for k in (
                    "close_below_support",
                    "live_below_support",
                    "rejection",
                    "cascade",
                    "lost_support",
                )
            )
            if has_struct or fuel >= cal.confirm_min_score - 2:
                return EarlyAlert(
                    "start",
                    "start",
                    f"Дамп стартует · {setup_phase} · fuel {fuel:.0f}",
                )

        if setup_phase == "dump_imminent" and fuel >= cal.forming_min_score:
            return EarlyAlert(
                "imminent",
                "imminent",
                f"Дамп imminent · fuel {fuel:.0f} · жди closed-bar",
            )

        if (
            dump_hunt
            and lc_phase in SHORT_PREP_LC
            and setup_phase in SHORT_PREP_SETUP | {"dump_setup_forming"}
            and fuel >= cal.forming_min_score + 20
        ):
            fall = float(lc.get("fall_from_high_pct") or 0)
            return EarlyAlert(
                "imminent",
                "imminent",
                f"Fade-zone charged · fuel {fuel:.0f} · fall {fall:.1f}%",
            )

        if (
            lc_phase in SHORT_PREP_LC
            and setup_phase in SHORT_PREP_SETUP | {"dump_setup_forming"}
            and fuel >= cal.forming_min_score
        ):
            fall = float(lc.get("fall_from_high_pct") or 0)
            return EarlyAlert(
                "prep",
                "prep",
                f"Подготовка шорта · {lc_phase} · fuel {fuel:.0f} · fall {fall:.1f}%",
            )

        if (
            lc_phase in SHORT_PREP_LC
            and fuel >= cal.confirm_min_score - 5
            and setup_phase in SHORT_PREP_SETUP | {"dump_setup_forming", "exhaustion_watch"}
        ):
            return EarlyAlert(
                "prep",
                "prep",
                f"Fade-zone watch · fuel {fuel:.0f} · {setup_phase}",
            )

    else:
        ign = _ignition_pump(row)
        ign_pct = float(ign.get("price_delta_pct") or 0)
        long_ok_phase = lc_phase in LONG_PREP_LC or bool(ign)

        if not long_ok_phase:
            return EarlyAlert("none", "", "")

        if confirmed:
            return EarlyAlert("confirm", "confirm", "full_confirm")

        broke_res = any("broke_resistance" in t for t in triggers)
        if setup_phase in LONG_START_SETUP and fuel >= cal.forming_min_score:
            has_struct = any(
                k in h
                for h in hard
                for k in ("close_above_resistance", "bounce", "cascade", "broke_resistance")
            )
            if has_struct or broke_res or fuel >= cal.confirm_min_score - 2:
                return EarlyAlert(
                    "start",
                    "start",
                    f"Памп стартует · {setup_phase} · fuel {fuel:.0f}",
                )

        if ign and ign_pct >= 2.0 and fuel >= cal.forming_min_score:
            if broke_res or setup_phase in LONG_START_SETUP:
                return EarlyAlert(
                    "start",
                    "start",
                    f"Ignition +{ign_pct:.1f}% · {setup_phase} · fuel {fuel:.0f}",
                )
            return EarlyAlert(
                "prep",
                "prep",
                f"Ignition заряд +{ign_pct:.1f}% · {lc_phase or 'pump'} · fuel {fuel:.0f}",
            )

        if setup_phase == "long_imminent" and fuel >= cal.forming_min_score:
            return EarlyAlert(
                "imminent",
                "imminent",
                f"Памп imminent · fuel {fuel:.0f}",
            )

        if lc_phase == "impulse_initiating" and fuel >= cal.forming_min_score:
            rally = float(lc.get("bounce_from_low_pct") or 0)
            if broke_res or fuel >= cal.confirm_min_score - 8:
                return EarlyAlert(
                    "start",
                    "start",
                    f"Импульс вверх · fuel {fuel:.0f} · rally {rally:.1f}%",
                )
            return EarlyAlert(
                "prep",
                "prep",
                f"Импульс формируется · fuel {fuel:.0f} · rally {rally:.1f}%",
            )

        if lc_phase == "breakout_arming" and fuel >= cal.forming_min_score:
            return EarlyAlert(
                "prep",
                "prep",
                f"База заряжена (squeeze) · fuel {fuel:.0f} · жди пробой",
            )

        if (
            setup_phase in LONG_PREP_SETUP | {"long_setup_forming"}
            and fuel >= cal.forming_min_score
        ):
            rally = float(lc.get("bounce_from_low_pct") or 0)
            return EarlyAlert(
                "prep",
                "prep",
                f"Подготовка лонга · {lc_phase} · fuel {fuel:.0f} · rally {rally:.1f}%",
            )

    return EarlyAlert("none", "", "")


def early_cooldown_ok(
    symbol: str,
    direction: str,
    tier: str,
    state: dict[str, str],
    *,
    now: datetime,
) -> bool:
    if tier not in EARLY_COOLDOWN_MIN:
        return True
    # Tier hierarchy: after 🚨 start was sent, re-sending 🟡 prep for the same
    # symbol+direction inside the window is noise (prep↔start oscillation gave
    # 76 would-sends on a 2-symbol replay) — an equal-or-higher tier on cooldown
    # silences this one too.
    order = ("prep", "imminent", "start")
    rank = order.index(tier) if tier in order else 0
    for other in order[rank:]:
        key = f"early:{symbol.upper()}:{direction.lower()}:{other}"
        raw = state.get(key)
        if not raw:
            continue
        try:
            last = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        if now - last < timedelta(minutes=EARLY_COOLDOWN_MIN.get(other, 30)):
            return False
    return True


def mark_early_sent(
    symbol: str,
    direction: str,
    tier: str,
    state: dict[str, str],
    *,
    now: datetime,
) -> None:
    state[f"early:{symbol.upper()}:{direction.lower()}:{tier}"] = now.isoformat()


def format_early_telegram(
    row: dict[str, Any],
    *,
    direction: str,
    setup: dict[str, Any],
    lifecycle: Any | None,
    alert: EarlyAlert,
) -> str:
    sym = html.escape(str(row.get("symbol", "?")).replace("USDT", "-USDT"))
    lc = _lc(lifecycle)
    fuel = _fuel(setup, direction)
    price = row.get("price")
    chg = row.get("chg_24h_pct")
    lc_phase = html.escape(str(lc.get("phase") or "—"))
    setup_phase = html.escape(str(setup.get("phase") or "—"))
    ign = _ignition_pump(row)
    ign_txt = (
        f" · ignition <code>+{float(ign.get('price_delta_pct') or 0):.1f}%</code>"
        if ign
        else ""
    )

    if direction == "short":
        badge = {"prep": "🟠", "imminent": "🔴", "start": "🚨"}.get(alert.tier, "🔴")
        title = {"prep": "DUMP PREP", "imminent": "DUMP IMMINENT", "start": "DUMP START"}.get(
            alert.tier, "DUMP WATCH"
        )
    else:
        badge = {"prep": "🟡", "imminent": "🟢", "start": "🚨"}.get(alert.tier, "🟢")
        title = {"prep": "PUMP PREP", "imminent": "PUMP IMMINENT", "start": "PUMP START"}.get(
            alert.tier, "PUMP WATCH"
        )

    hard = setup.get("confirm_hard") or []
    triggers = setup.get("triggers") or []
    hard_txt = html.escape(", ".join(str(h) for h in hard[:5]))
    trig_txt = html.escape(", ".join(str(t) for t in triggers[:5]))

    lines = [
        f"{badge} <b>{title}</b> {sym}",
        f"<i>{html.escape(alert.message)}</i>",
        f"Цена <code>{price}</code> · 24h <code>{chg}%</code>{ign_txt}",
        f"Lifecycle <code>{lc_phase}</code> · setup <code>{setup_phase}</code> · fuel <code>{fuel:.0f}</code>",
    ]
    if hard_txt:
        lines.append(f"Hard partial: <code>{hard_txt}</code>")
    if trig_txt:
        lines.append(f"Triggers: <code>{trig_txt}</code>")
    ez = setup.get("entry_zone") or []
    if len(ez) >= 2:
        lines.append(
            f"Entry zone <code>{ez[0]}</code>–<code>{ez[1]}</code> · "
            f"SL <code>{setup.get('stop_loss')}</code> · TP1 <code>{setup.get('tp1')}</code>"
        )
    lines.append("<i>Early hunt alert · prep/start — не auto-trade</i>")
    return "\n".join(lines)
