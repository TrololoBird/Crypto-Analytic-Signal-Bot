"""Scenario catalyst from structure/maps."""
from __future__ import annotations

from typing import Any

from hunt_core.analysis.deep.verdict_v2._helpers import safe_float
from hunt_core.analysis.deep.verdict_v2.types import CatalystKind, ExpectedPath, ScenarioCatalyst


def build_catalyst(row: dict[str, Any], path: ExpectedPath) -> ScenarioCatalyst:
    structure = row.get("structure") if isinstance(row.get("structure"), dict) else {}
    regime = row.get("regime") if isinstance(row.get("regime"), dict) else {}
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    price = safe_float(row.get("price"))
    poc = safe_float(regime.get("poc_1h"))
    pools = structure.get("liquidity_pools") if isinstance(structure.get("liquidity_pools"), dict) else {}
    evidence: list[str] = []
    trigger: float | None = None
    primary: CatalystKind = "level_break"
    label = "Key level break"

    if path.direction == "long" and pools.get("nearest_below"):
        trigger = safe_float(pools["nearest_below"])
        primary = "liq_sweep"
        label = "Sweep lows then reclaim"
        evidence.append("liq_below")
    elif path.direction == "short" and pools.get("nearest_above"):
        trigger = safe_float(pools["nearest_above"])
        primary = "liq_sweep"
        label = "Sweep highs then reject"
        evidence.append("liq_above")
    elif poc > 0 and price > 0:
        trigger = poc
        if price < poc and path.direction == "long":
            primary = "poc_reclaim"
            label = "Reclaim POC"
        elif price > poc and path.direction == "short":
            primary = "poc_loss"
            label = "Lose POC"
        evidence.append("poc_level")
    elif structure.get("bos_direction"):
        primary = "structure_break"
        label = f"BOS {structure.get('bos_direction')}"
        kl = structure.get("key_levels") if isinstance(structure.get("key_levels"), dict) else {}
        trigger = safe_float(kl.get("last_swing_high") or kl.get("last_swing_low") or 0) or None
        evidence.append("structure_break")
    elif safe_float(market.get("funding_zscore_48h")) > 1.5:
        primary = "funding_flush"
        label = "Funding flush"
        evidence.append("funding_extreme")
    else:
        primary = "flow_confirmation"
        label = "Flow confirmation"
        evidence.append("flow_gate")

    alts = [e for e in evidence if e != evidence[0]] if evidence else []
    return ScenarioCatalyst(
        primary=primary,
        label=label,
        trigger_level=trigger,
        confidence=0.55 if trigger else 0.4,
        alternatives=alts,
        evidence=evidence[:4],
    )
