"""Probe/display accessors over the fusion engine output (single source).

These read the fusion setups already on the row to drive the ``/signal`` probe and
delivery formatting — direction pick, header badge, one-line summary, BTC context. No
legacy detection is re-run; the values come straight from the engine's ``confidence_score`` /
``confirmed`` / ``phase`` fields.
"""
from __future__ import annotations

from typing import Any


def _setup_strength(setup: dict[str, Any]) -> float:
    try:
        return float(setup.get("confidence_score") or 0) * 100.0
    except (TypeError, ValueError):
        return 0.0


def resolve_trade_direction(row: dict[str, Any]) -> tuple[str, dict[str, Any], float, list[str]]:
    """(direction, setup, strength, notes) from the fusion setups on the row."""
    lc = row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else {}
    dump = row.get("dump") if isinstance(row.get("dump"), dict) else {}
    long_b = row.get("long") if isinstance(row.get("long"), dict) else {}
    bias = str(lc.get("recommended_bias") or "")
    if bias in {"long", "short"}:
        direction = bias
    elif dump.get("impulse_confirmed"):
        direction = "short"
    elif long_b.get("impulse_confirmed"):
        direction = "long"
    else:
        direction = "short" if _setup_strength(dump) >= _setup_strength(long_b) else "long"
    setup = dump if direction == "short" else long_b
    return direction, setup, _setup_strength(setup), []


def probe_header(row: dict[str, Any]) -> tuple[str, str, str]:
    direction, setup, score, _ = resolve_trade_direction(row)
    confirmed = bool(setup.get("impulse_confirmed"))
    badge = "CONFIRM" if confirmed else ("WATCH" if score >= 60 else "MONITOR")
    dir_label = "SHORT" if direction == "short" else "LONG"
    phase = str(setup.get("phase") or (row.get("lifecycle") or {}).get("phase") or "—")
    return badge, dir_label, f"{phase} · fusion {score:.0f}"


def scenario_summary(
    *,
    direction: str,
    setup: dict[str, Any],
    fuel: float = 0.0,
    lc: dict[str, Any] | None = None,
    confirmed: bool = False,
    row: dict[str, Any] | None = None,
) -> str:
    phase = str(setup.get("phase") or (lc or {}).get("phase") or "—")
    side = "pre-dump" if direction == "short" else "pre-pump"
    score = fuel if fuel else _setup_strength(setup)
    return f"<i>Fusion: {side} · phase {phase} · score {score:.0f}</i>"


def forming_confirm_gaps(setup: dict[str, Any], **_k: Any) -> list[str]:
    """The fusion gate is binary (confirmed or not) — no forming sub-gaps."""
    return []


def hunt_confirmed_direction(row: dict[str, Any]) -> str:
    dump = row.get("dump") if isinstance(row.get("dump"), dict) else {}
    long_b = row.get("long") if isinstance(row.get("long"), dict) else {}
    if dump.get("impulse_confirmed") or dump.get("intrabar_confirmed"):
        return "short"
    if long_b.get("impulse_confirmed") or long_b.get("intrabar_confirmed"):
        return "long"
    return ""


def _safe_float(val: object, default: float = 0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def btc_market_context(btc_work_1h: Any | None, btc_work_4h: Any | None = None) -> dict[str, Any]:
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

    out: dict[str, Any] = {
        "btc_chg_1h_pct": round(chg_1h, 2),
        "btc_chg_4h_pct": round(chg_4h, 2) if chg_4h is not None else None,
        "btc_trend": trend,
    }

    # BTC EMA check — prefer 4h timeframe, try EMA50 first then EMA200
    btc_price = _safe_float(closes[-1])
    btc_ema50 = None
    btc_ema200 = _safe_float(btc_work_1h["ema200"][-1]) if "ema200" in btc_work_1h.columns else None
    if btc_work_4h is not None and not getattr(btc_work_4h, "is_empty", lambda: True)():
        try:
            btc_price_4h = float(btc_work_4h["close"][-1])
            col_ema50 = "ema50"
            col_ema200 = "ema200"
            if col_ema50 in btc_work_4h.columns:
                btc_ema50 = float(btc_work_4h[col_ema50][-1])
                if btc_ema50 > 0:
                    btc_price = btc_price_4h
            if col_ema200 in btc_work_4h.columns:
                btc_ema200_4h = float(btc_work_4h[col_ema200][-1])
                if btc_ema200_4h > 0 and btc_ema50 is None:
                    btc_price = btc_price_4h
                    btc_ema200 = btc_ema200_4h
        except Exception:
            pass
    if btc_ema50 is not None and btc_ema50 > 0 and btc_price > 0:
        out["btc_above_ema50"] = btc_price > btc_ema50
        out["btc_price"] = round(btc_price, 2)
        out["btc_ema50"] = round(btc_ema50, 2)
    elif btc_ema200 is not None and btc_ema200 > 0 and btc_price > 0:
        out["btc_above_ema200"] = btc_price > btc_ema200
        out["btc_price"] = round(btc_price, 2)
        out["btc_ema200"] = round(btc_ema200, 2)
    return out


__all__ = [
    "btc_market_context",
    "forming_confirm_gaps",
    "hunt_confirmed_direction",
    "probe_header",
    "resolve_trade_direction",
    "scenario_summary",
]
