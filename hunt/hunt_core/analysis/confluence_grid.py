"""/signals level map grid (§N.2)."""
from __future__ import annotations

from typing import Any


def build_confluence_grid(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Level map: POC/structure/fib magnets per TF."""
    grid: list[dict[str, Any]] = []
    for tf_name in ("1h", "15m", "5m"):
        block = (row.get("timeframes") or {}).get(tf_name) or {}
        if not block or block.get("status") == "empty":
            continue
        entry = {
            "tf": tf_name,
            "poc": block.get("poc") or block.get("poc_1h"),
            "vah": block.get("vah"),
            "val": block.get("val"),
            "support": block.get("local_support") or block.get("donchian_low20"),
            "resistance": block.get("local_resistance") or block.get("donchian_high20"),
        }
        grid.append(entry)
    regime = row.get("regime") or {}
    if regime.get("poc_1h"):
        grid.append({"tf": "regime", "poc": regime.get("poc_1h"), "note": "session POC"})
    return grid


def format_grid_telegram(grid: list[dict[str, Any]]) -> str:
    if not grid:
        return ""
    lines = ["<b>Level map</b>"]
    for g in grid[:6]:
        tf = g.get("tf", "?")
        parts = [f"{k}={g[k]}" for k in ("poc", "support", "resistance", "vah", "val") if g.get(k)]
        lines.append(f"· {tf}: " + ", ".join(parts[:4]))
    return "\n".join(lines)


__all__ = ["build_confluence_grid", "format_grid_telegram"]
