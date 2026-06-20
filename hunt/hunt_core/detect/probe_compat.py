"""Compatibility shims for the /signal probe path on the fusion engine."""
from __future__ import annotations

from typing import Any


def _setup_strength(setup: dict[str, Any]) -> float:
    raw = setup.get("fusion_score")
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    try:
        return float(setup.get("p_win") or 0) * 100.0
    except (TypeError, ValueError):
        return 0.0


def resolve_trade_direction(row: dict[str, Any]) -> tuple[str, dict[str, Any], float, list[str]]:
    """(direction, setup, fusion_score, notes) from the fusion setups on the row."""
    lc = row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else {}
    dump = row.get("dump") if isinstance(row.get("dump"), dict) else {}
    long_b = row.get("long") if isinstance(row.get("long"), dict) else {}
    bias = str(lc.get("recommended_bias") or "")
    if bias in {"long", "short"}:
        direction = bias
    elif dump.get("confirmed"):
        direction = "short"
    elif long_b.get("confirmed"):
        direction = "long"
    else:
        direction = (
            "short"
            if _setup_strength(dump) >= _setup_strength(long_b)
            else "long"
        )
    setup = dump if direction == "short" else long_b
    return direction, setup, _setup_strength(setup), []


def probe_header(row: dict[str, Any]) -> tuple[str, str, str]:
    """(badge, dir_label, header_sub) for the probe header line."""
    direction, setup, score, _ = resolve_trade_direction(row)
    confirmed = bool(setup.get("confirmed"))
    badge = "CONFIRM" if confirmed else ("WATCH" if score >= 60 else "MONITOR")
    dir_label = "SHORT" if direction == "short" else "LONG"
    phase = str(setup.get("phase") or (row.get("lifecycle") or {}).get("phase") or "—")
    return badge, dir_label, f"{phase} · fusion {score:.0f}"


def scenario_summary(
    *,
    direction: str,
    setup: dict[str, Any],
    fuel: float,
    lc: dict[str, Any] | None = None,
    confirmed: bool = False,
    row: dict[str, Any] | None = None,
) -> str:
    """One-line fusion read for the probe body."""
    phase = str(setup.get("phase") or (lc or {}).get("phase") or "—")
    side = "pre-dump" if direction == "short" else "pre-pump"
    score = fuel if fuel else _setup_strength(setup)
    return f"<i>Fusion: {side} · phase {phase} · score {score:.0f}</i>"


def forming_confirm_gaps(
    setup: dict[str, Any],
    *,
    direction: str = "",
    tf: dict[str, Any] | None = None,
    row: dict[str, Any] | None = None,
    price: float = 0.0,
) -> list[str]:
    """The fusion gate is binary (confirmed or not); there are no forming sub-gaps."""
    return []


def hunt_confirmed_direction(row: dict[str, Any]) -> str:
    """Confirmed delivery side from the fusion setups (\"short\"/\"long\"/\"\")."""
    dump = row.get("dump") if isinstance(row.get("dump"), dict) else {}
    long_b = row.get("long") if isinstance(row.get("long"), dict) else {}
    if dump.get("confirmed") or dump.get("intrabar_confirmed"):
        return "short"
    if long_b.get("confirmed") or long_b.get("intrabar_confirmed"):
        return "long"
    return ""


def btc_market_context(btc_work_1h: Any | None) -> dict[str, Any]:
    """1h/4h BTC change + trend label from a prepared 1h frame (fusion-independent)."""
    if btc_work_1h is None or getattr(btc_work_1h, "is_empty", lambda: True)():
        return {}
    try:
        closes = [float(x) for x in btc_work_1h["close"].to_list()]
    except (TypeError, KeyError, ValueError):
        return {}
    if len(closes) < 3:
        return {}
    chg_1h = (closes[-1] / closes[-2] - 1.0) * 100.0
    chg_4h = (closes[-1] / closes[-5] - 1.0) * 100.0 if len(closes) >= 5 else None
    trend = "up" if chg_1h >= 0.12 else "down" if chg_1h <= -0.12 else "flat"
    return {
        "btc_chg_1h_pct": round(chg_1h, 2),
        "btc_chg_4h_pct": round(chg_4h, 2) if chg_4h is not None else None,
        "btc_trend": trend,
    }


__all__ = [
    "btc_market_context",
    "forming_confirm_gaps",
    "hunt_confirmed_direction",
    "probe_header",
    "resolve_trade_direction",
    "scenario_summary",
]
