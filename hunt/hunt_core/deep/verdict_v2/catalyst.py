"""Scenario catalyst from structure/maps — catalyst ≠ stop by construction."""
from __future__ import annotations

from typing import Any

from hunt_core.deep.verdict_v2._helpers import atr_from_row, safe_float
from hunt_core.deep.verdict_v2.levels import pick_catalyst_level
from hunt_core.deep.verdict_v2.types import CatalystKind, ExpectedPath, ScenarioCatalyst


def build_catalyst(row: dict[str, Any], path: ExpectedPath) -> ScenarioCatalyst:
    if path.direction not in {"long", "short"}:
        return ScenarioCatalyst(
            primary="flow_confirmation",
            label="No directional catalyst",
            trigger_level=None,
            confidence=0.0,
            alternatives=(),
            evidence=("no_direction",),
        )

    structure = row.get("structure") if isinstance(row.get("structure"), dict) else {}
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    price = safe_float(row.get("price"))
    trigger, src = pick_catalyst_level(row, path.direction)
    evidence: list[str] = [src] if src != "none" else []
    primary: CatalystKind = "level_break"
    label = "Key level break"

    if path.direction == "long" and src in {"pool_below", "support", "val"}:
        primary = "liq_sweep"
        label = "Sweep lows then reclaim"
        evidence.append("liq_below")
    elif path.direction == "short" and src in {"pool_above", "resistance", "vah"}:
        primary = "liq_sweep"
        label = "Sweep highs then reject"
        evidence.append("liq_above")
    elif src == "poc":
        primary = "poc_reclaim" if path.direction == "long" else "poc_loss"
        label = "Reclaim POC" if path.direction == "long" else "Lose POC"
        evidence.append("poc_level")
    elif structure.get("bos_direction"):
        primary = "structure_break"
        label = f"BOS {structure.get('bos_direction')}"
        evidence.append("structure_break")
    elif safe_float(market.get("funding_zscore_48h")) > 1.5:
        primary = "funding_flush"
        label = "Funding flush"
        evidence.append("funding_extreme")
    else:
        primary = "flow_confirmation"
        label = "Flow confirmation"
        evidence.append("flow_gate")

    confidence = 0.0
    if trigger and trigger > 0 and price > 0:
        atr = atr_from_row(row)
        dist = abs(price - trigger)
        if atr > 0 and dist <= atr * 2.5:
            confidence = round(min(0.85, 0.45 + (1.0 - dist / (atr * 2.5)) * 0.35), 3)
        elif dist / price <= 0.02:
            confidence = 0.42

    alts = [e for e in evidence if e != evidence[0]] if evidence else []
    return ScenarioCatalyst(
        primary=primary,
        label=label,
        trigger_level=round(trigger, 6) if trigger > 0 else None,
        confidence=confidence,
        alternatives=alts,
        evidence=evidence[:4],
    )
