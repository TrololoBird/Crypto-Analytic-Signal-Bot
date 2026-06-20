"""Forecast bands panel for deep analysis."""
from __future__ import annotations

import html
from typing import Any

from hunt_core.deliver._labels import fmt_price


def build_forecast_panel(forecasts: dict[str, dict[str, Any] | None]) -> str:
    if not forecasts:
        return ""
    lines = ["🎯 <b>Forecasts</b>"]
    labels = {
        "predump_short": "Pre-dump ↓",
        "prepump_long": "Coil ↑",
        "ignition_long": "Ignition ↑",
    }
    for key, label in labels.items():
        fc = forecasts.get(key)
        if not isinstance(fc, dict):
            continue
        conf = float(fc.get("confidence") or 0)
        tlo = fc.get("target_lo")
        thi = fc.get("target_hi")
        if tlo is None:
            continue
        if thi is not None and float(thi) != float(tlo):
            band = f"{fmt_price(float(tlo))}–{fmt_price(float(thi))}"
        else:
            band = fmt_price(float(tlo))
        move = fc.get("expected_move_pct")
        move_s = ""
        if move is not None and abs(float(move)) >= 0.05:
            mv = float(move)
            move_s = f" ({'+' if mv >= 0 else ''}{mv:.0f}%)"
        factors = fc.get("factors") or []
        fac_s = ""
        if factors:
            fac_s = " · " + ", ".join(html.escape(str(f)) for f in factors[:3])
        lines.append(f"  {label}: <code>{band}</code> {conf:.0%}{move_s}{fac_s}")
    return "\n".join(lines) if len(lines) > 1 else ""


def build_structural_forecast_panel(
    forecasts: dict[str, dict[str, Any] | None],
    row: dict[str, Any] | None = None,
) -> str:
    """Maps-derived target bands — no pump/dump archetype labels."""
    if not forecasts:
        return ""
    lines = ["🎯 <b>Structure targets</b> (maps / liquidity magnets)"]
    labels = {
        "structural_up": "↑ Upside band",
        "structural_down": "↓ Downside band",
    }
    for key, label in labels.items():
        fc = forecasts.get(key)
        if not isinstance(fc, dict):
            continue
        tlo = fc.get("target_lo")
        thi = fc.get("target_hi")
        if tlo is None:
            continue
        conf = float(fc.get("confidence") or 0)
        if thi is not None and float(thi) != float(tlo):
            band = f"{fmt_price(float(tlo))}–{fmt_price(float(thi))}"
        else:
            band = fmt_price(float(tlo))
        move = fc.get("expected_move_pct")
        move_s = ""
        if move is not None and abs(float(move)) >= 0.05:
            mv = float(move)
            move_s = f" ({'+' if mv >= 0 else ''}{mv:.1f}%)"
        factors = fc.get("factors") or []
        fac_s = ""
        if factors:
            fac_s = " · " + ", ".join(html.escape(str(f)) for f in factors[:3])
        lines.append(f"  {label}: <code>{band}</code> {conf:.0%}{move_s}{fac_s}")
    return "\n".join(lines) if len(lines) > 1 else ""


__all__ = ["build_forecast_panel", "build_structural_forecast_panel"]
