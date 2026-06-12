"""Resolve hunt watch universe: pinned anchors + scanner watchlist + ignition."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from hunt_core.domain.config import BotSettings
from hunt_watch.paths import WATCHLIST as WATCHLIST_PATH

WatchMode = Literal["short", "long", "both"]

# Hunt anchors only — memecoin pins removed; dynamic names come from scanner/ignition.
PINNED_SYMBOLS = ("BTCUSDT", "ETHUSDT", "XAUUSDT", "XAGUSDT")
DEFAULT_SYMBOLS = PINNED_SYMBOLS
DEFAULT_MODES: dict[str, WatchMode] = {
    "BTCUSDT": "both",
    "ETHUSDT": "both",
    "XAUUSDT": "both",
    "XAGUSDT": "both",
}
MAX_DYNAMIC_SYMBOLS = 12


def _bias_to_mode(bias: str) -> WatchMode:
    b = str(bias or "").strip().lower()
    if b == "long":
        return "long"
    if b == "short":
        return "short"
    return "both"


def load_watchlist_rows(path: Path = WATCHLIST_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return []
    rows = payload.get("watchlist") if isinstance(payload, dict) else None
    return list(rows) if isinstance(rows, list) else []


def _ignition_bias(meta: dict[str, Any]) -> WatchMode:
    direction = str(meta.get("direction") or "pump").strip().lower()
    if direction == "pump":
        return "short"
    if direction == "dump":
        return "long"
    return "both"


def resolve_watch_universe(
    settings: BotSettings,
    *,
    static_modes: dict[str, WatchMode] | None = None,
    watchlist_path: Path = WATCHLIST_PATH,
    ignited: dict[str, dict[str, Any]] | None = None,
) -> tuple[tuple[str, ...], dict[str, WatchMode]]:
    """Merge pinned anchors, ignition lane, and scanner watchlist into active hunt set."""
    modes: dict[str, WatchMode] = dict(static_modes or {})
    ordered: list[str] = []
    pinned_set = set(PINNED_SYMBOLS)

    def _add(sym: str) -> None:
        s = str(sym).strip().upper()
        if s and s not in ordered:
            ordered.append(s)

    for sym in PINNED_SYMBOLS:
        _add(sym)
        modes.setdefault(sym, DEFAULT_MODES.get(sym, "both"))

    for sym, meta in (ignited or {}).items():
        s = str(sym).strip().upper()
        if not s:
            continue
        _add(s)
        if s not in pinned_set:
            modes[s] = _ignition_bias(meta if isinstance(meta, dict) else {})

    for row in load_watchlist_rows(watchlist_path):
        sym = str(row.get("symbol") or "").strip().upper()
        if not sym:
            continue
        if row.get("suggest_minute_watch") or float(row.get("hunt_score") or 0) >= 45:
            _add(sym)
            bias = str(row.get("watch_bias") or "both")
            if sym in pinned_set:
                continue
            if sym not in modes or row.get("suggest_minute_watch"):
                modes[sym] = _bias_to_mode(bias)

    ignition_extra = min(len(ignited or {}), 6)
    cap = MAX_DYNAMIC_SYMBOLS + len(PINNED_SYMBOLS) + ignition_extra
    symbols = tuple(ordered[: max(cap, len(PINNED_SYMBOLS))])
    return symbols, modes


def effective_watch_mode(
    symbol: str,
    modes: dict[str, WatchMode],
    *,
    lifecycle_bias: str | None = None,
) -> WatchMode:
    sym = symbol.upper()
    base = modes.get(sym, "short")
    if lifecycle_bias not in {"long", "short", "both", "wait"}:
        return base
    if lifecycle_bias == "wait":
        return base
    if sym in PINNED_SYMBOLS:
        if base == "both":
            return lifecycle_bias  # type: ignore[return-value]
        if base == lifecycle_bias:
            return base
        return "both"
    if base == "both":
        return lifecycle_bias  # type: ignore[return-value]
    if base != lifecycle_bias:
        return "both"
    return base
