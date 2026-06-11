"""EWMA + z-score adaptive move thresholds per symbol (hunt-v2 #4)."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from hunt_watch.paths import ADAPTIVE_THRESHOLDS, EWMA_THRESHOLDS

# Static fallbacks — used until enough samples per symbol.
STATIC_IGNITION_MIN_PCT = 2.5
STATIC_RANGE_HOT_PCT = 8.0
STATIC_PUMP_EXTREME_PCT = 15.0

EWMA_ALPHA = 0.08
MIN_TICK_SAMPLES = 6
MIN_CHANGE_SAMPLES = 4
Z_IGNITION = 2.0
Z_RANGE_HOT = 1.8
Z_PUMP_EXTREME = 2.5
TICK_FLOOR_PCT = 0.8
VAR_FLOOR = 0.05


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
            "tick_sigma_pct": round(math.sqrt(max(self.tick_var, VAR_FLOOR)), 3),
            "chg_n": self.chg_n,
            "chg_mu_abs_pct": round(self.chg_mu, 2),
            "chg_sigma_pct": round(math.sqrt(max(self.chg_var, VAR_FLOOR)), 2),
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
    """Write EWMA stats only — never merge calibration keys."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store.to_dict(), indent=2), encoding="utf-8")
    if path != ADAPTIVE_THRESHOLDS and ADAPTIVE_THRESHOLDS.exists():
        try:
            legacy = _read_ewma_raw(ADAPTIVE_THRESHOLDS) or {}
            if any(k in legacy for k in ("universal", "per_symbol", "outcome_calibration")):
                ADAPTIVE_THRESHOLDS.write_text(
                    json.dumps(store.to_dict(), indent=2),
                    encoding="utf-8",
                )
        except OSError:
            pass


def _sym(store: AdaptiveStore, symbol: str) -> SymbolAdaptive:
    sym = symbol.upper()
    if sym not in store.symbols:
        store.symbols[sym] = SymbolAdaptive(symbol=sym)
    return store.symbols[sym]


def ewma_update(mean: float, var: float, value: float, *, alpha: float = EWMA_ALPHA) -> tuple[float, float]:
    """Online EWMA mean + variance (Welford-style EWMA)."""
    new_mean = alpha * value + (1.0 - alpha) * mean
    dev = value - new_mean
    new_var = alpha * (dev * dev) + (1.0 - alpha) * var
    return new_mean, max(new_var, VAR_FLOOR)


def zscore(value: float, mean: float, var: float) -> float | None:
    sigma = math.sqrt(max(var, VAR_FLOOR))
    if sigma <= 0:
        return None
    z = (value - mean) / sigma
    if not math.isfinite(z):
        return None
    return z


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


def update_from_price_pair(
    store: AdaptiveStore,
    symbol: str,
    *,
    prev_price: float,
    price: float,
) -> float | None:
    """Update tick EWMA from consecutive prices; return delta pct if valid."""
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
    """Adaptive OR static ignition gate on tick delta magnitude."""
    abs_delta = abs(delta_pct)
    st = store.symbols.get(symbol.upper())
    if st is None or st.tick_n < MIN_TICK_SAMPLES:
        ok = abs_delta >= static_min_pct
        return ok, None, "static"
    z = zscore(abs_delta, st.tick_mu, st.tick_var)
    if z is None:
        ok = abs_delta >= static_min_pct
        return ok, None, "static"
    eff_floor = max(TICK_FLOOR_PCT, st.tick_mu + 0.5 * math.sqrt(st.tick_var))
    ok = abs_delta >= eff_floor and z >= Z_IGNITION
    return ok, z, "adaptive"


def change_24h_tier(
    store: AdaptiveStore,
    symbol: str,
    change_24h_pct: float,
) -> tuple[str | None, float | None, str]:
    """Classify 24h move: None | hot | extreme; returns (tier, z, mode)."""
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
    """Effective hot threshold in % for bias helpers."""
    st = store.symbols.get(symbol.upper())
    if st is None or st.chg_n < MIN_CHANGE_SAMPLES:
        return STATIC_RANGE_HOT_PCT
    return max(STATIC_RANGE_HOT_PCT * 0.5, st.chg_mu + Z_RANGE_HOT * math.sqrt(st.chg_var))


def adaptive_extreme_pct(store: AdaptiveStore, symbol: str) -> float:
    st = store.symbols.get(symbol.upper())
    if st is None or st.chg_n < MIN_CHANGE_SAMPLES:
        return STATIC_PUMP_EXTREME_PCT
    return max(STATIC_PUMP_EXTREME_PCT * 0.5, st.chg_mu + Z_PUMP_EXTREME * math.sqrt(st.chg_var))
