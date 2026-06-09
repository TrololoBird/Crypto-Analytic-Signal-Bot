"""Resolve hunt watch universe: pinned + defaults + hunt_watchlist.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from bot.domain.config import BotSettings

from hunt_watch.paths import WATCHLIST as WATCHLIST_PATH

WatchMode = Literal["short", "long", "both"]
DEFAULT_SYMBOLS = ("JCTUSDT", "BEATUSDT", "VELVETUSDT", "HYPEUSDT", "BTCUSDT")
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
    except (OSError, json.JSONDecodeError):
        return []
    rows = payload.get("watchlist") if isinstance(payload, dict) else None
    return list(rows) if isinstance(rows, list) else []


def resolve_watch_universe(
    settings: BotSettings,
    *,
    static_modes: dict[str, WatchMode] | None = None,
    watchlist_path: Path = WATCHLIST_PATH,
) -> tuple[tuple[str, ...], dict[str, WatchMode]]:
    """Merge pinned, defaults, and scanner watchlist into active hunt set."""
    modes: dict[str, WatchMode] = dict(static_modes or {})
    ordered: list[str] = []

    def _add(sym: str) -> None:
        s = str(sym).strip().upper()
        if s and s not in ordered:
            ordered.append(s)

    for sym in getattr(settings.universe, "pinned_symbols", ()) or ():
        _add(str(sym))
        modes.setdefault(str(sym).upper(), "both")

    for sym in DEFAULT_SYMBOLS:
        _add(sym)
        modes.setdefault(sym, modes.get(sym, "short"))

    for row in load_watchlist_rows(watchlist_path):
        sym = str(row.get("symbol") or "").strip().upper()
        if not sym:
            continue
        if row.get("suggest_minute_watch") or float(row.get("hunt_score") or 0) >= 45:
            _add(sym)
            bias = str(row.get("watch_bias") or "both")
            if sym not in modes or row.get("suggest_minute_watch"):
                modes[sym] = _bias_to_mode(bias)

    cap = MAX_DYNAMIC_SYMBOLS + len(getattr(settings.universe, "pinned_symbols", ()) or ())
    symbols = tuple(ordered[: max(cap, len(ordered))])
    return symbols, modes


def effective_watch_mode(
    symbol: str,
    modes: dict[str, WatchMode],
    *,
    lifecycle_bias: str | None = None,
) -> WatchMode:
    sym = symbol.upper()
    base = modes.get(sym, "short")
    if lifecycle_bias in {"long", "short", "both", "wait"} and sym not in DEFAULT_SYMBOLS:
        if lifecycle_bias == "wait":
            return base
        if base == "both":
            return lifecycle_bias  # type: ignore[return-value]
        if base != lifecycle_bias:
            return "both"
    return base
