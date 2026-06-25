"""/signals level map grid (§N.2)."""
from __future__ import annotations

from typing import Any


def build_confluence_grid(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Level map: POC/structure/fib magnets per TF."""
    price = float(row.get("price") or 0)
    grid: list[dict[str, Any]] = []
    for tf_name in ("1h", "15m", "5m"):
        block = (row.get("timeframes") or {}).get(tf_name) or {}
        if not block or block.get("status") == "empty":
            continue
        support = block.get("local_support") or block.get("donchian_low20")
        resistance = block.get("local_resistance") or block.get("donchian_high20")
        # Validate support/resistance against current price
        if price > 0:
            if support is not None and float(support) >= price:
                support = None
            if resistance is not None and float(resistance) <= price:
                resistance = None
        entry = {
            "tf": tf_name,
            "poc": block.get("poc") or block.get("poc_1h"),
            "vah": block.get("vah"),
            "val": block.get("val"),
            "support": support,
            "resistance": resistance,
        }
        grid.append(entry)
    regime = row.get("regime") or {}
    if regime.get("poc_1h"):
        grid.append({"tf": "regime", "poc": regime.get("poc_1h"), "note": "session POC"})
    return grid


def format_grid_telegram(grid: list[dict[str, Any]]) -> str:
    if not grid:
        return ""
    from hunt_core.deliver._labels import fmt_price  # noqa: PLC0415

    lines = ["<b>Карта уровней</b> <i>(POC/структура · не стакан и не ликвидации)</i>"]
    _K_RU = {"poc": "POC", "support": "поддержка", "resistance": "сопротивл", "vah": "VAH", "val": "VAL"}
    for g in grid[:6]:
        tf = g.get("tf", "?")
        # fmt_price avoids float noise like poc=0.42266075000000003 (MLIVE-9).
        parts = [f"{_K_RU.get(k, k)}={fmt_price(g[k])}" for k in ("poc", "support", "resistance", "vah", "val") if g.get(k)]
        lines.append(f"· {tf}: " + ", ".join(parts[:4]))
    return "\n".join(lines)


__all__ = ["build_confluence_grid", "format_grid_telegram"]
