"""Watchlist mutations for /signal and scanner."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hunt_watch.paths import WATCHLIST

SIGNAL_NOTIFY = WATCHLIST.parent / "signal_notify.json"


def load_watchlist_payload(path: Path = WATCHLIST) -> dict[str, Any]:
    if not path.exists():
        return {"watchlist": [], "updated_at": None}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"watchlist": [], "updated_at": None}
    if not isinstance(payload, dict):
        return {"watchlist": [], "updated_at": None}
    payload.setdefault("watchlist", [])
    return payload


def add_to_watchlist(
    symbol: str,
    *,
    source: str = "signal_cmd",
    hunt_score: float = 0.0,
    watch_bias: str = "both",
    note: str = "",
    path: Path = WATCHLIST,
) -> bool:
    """Add or update symbol for minute watch. Returns True if newly added."""
    sym = symbol.strip().upper()
    if not sym.endswith("USDT"):
        sym = f"{sym}USDT"
    payload = load_watchlist_payload(path)
    rows: list[dict[str, Any]] = list(payload.get("watchlist") or [])
    now = datetime.now(UTC).isoformat()
    for row in rows:
        if str(row.get("symbol", "")).upper() == sym:
            row["suggest_minute_watch"] = True
            row["hunt_score"] = max(float(row.get("hunt_score") or 0), hunt_score)
            row["watch_bias"] = watch_bias
            row["source"] = source
            row["updated_at"] = now
            if note:
                row["note"] = note
            payload["watchlist"] = rows
            payload["updated_at"] = now
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return False
    rows.append(
        {
            "symbol": sym,
            "hunt_score": round(hunt_score, 1),
            "watch_bias": watch_bias,
            "suggest_minute_watch": True,
            "source": source,
            "note": note,
            "added_at": now,
        }
    )
    payload["watchlist"] = rows
    payload["updated_at"] = now
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def load_pending_notify(path: Path = SIGNAL_NOTIFY) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    pending = payload.get("pending") or []
    return [p for p in pending if isinstance(p, dict) and p.get("symbol")]


def clear_signal_notify(symbol: str, *, path: Path = SIGNAL_NOTIFY) -> None:
    sym = symbol.strip().upper()
    if not sym.endswith("USDT"):
        sym = f"{sym}USDT"
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    pending = [p for p in (payload.get("pending") or []) if p.get("symbol") != sym]
    payload["pending"] = pending
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def register_signal_notify(
    symbol: str,
    *,
    direction: str,
    phase: str,
    path: Path = SIGNAL_NOTIFY,
) -> None:
    sym = symbol.strip().upper()
    payload: dict[str, Any] = {"pending": []}
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {"pending": []}
    pending = [p for p in payload.get("pending") or [] if p.get("symbol") != sym]
    pending.append(
        {
            "symbol": sym,
            "direction": direction,
            "await_phase": phase,
            "registered_at": datetime.now(UTC).isoformat(),
        }
    )
    payload["pending"] = pending[-50:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
