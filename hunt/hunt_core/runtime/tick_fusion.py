"""Fusion detection wiring extracted from tick_assembly (debloat)."""
from __future__ import annotations

from typing import Any

import structlog

LOG = structlog.get_logger("hunt_core.runtime.tick_fusion")


def run_fusion_detection(
    *,
    prepared: Any,
    result: dict[str, Any],
    symbol: str,
    structure: dict[str, Any],
    price: float,
    hunt_h: float,
    hunt_l: float,
) -> tuple[Any, str, str, dict[str, Any]]:
    """Build live fusion detection from closed 15m feature vector."""
    from hunt_core.hunter.detect.live import build_live_detection
    from hunt_core.hunter.gate._lifecycle import fusion_lifecycle_dict
    from hunt_core.features.feature_engine import build_feature_vector
    from hunt_core.data.tick_jsonl import ensure_fusion_lifecycle_fields

    leg_gain_pct = round((hunt_h - hunt_l) / hunt_l * 100.0, 1) if hunt_l > 0 else 0.0
    fall_from_high_pct = round((hunt_h - price) / hunt_h * 100.0, 2) if hunt_h > 0 else 0.0
    structure_bias = str(structure.get("structure_bias") or "")

    detection = None
    try:
        vector = build_feature_vector(
            prepared, result, symbol=symbol, tf="15m", require_closed=True
        )
        detection = build_live_detection(symbol, vector.to_dict(), context=result)
    except Exception as exc:
        LOG.debug("fusion_detection_skipped | symbol=%s error=%s", symbol, exc)

    side = detection.side if detection is not None else "none"
    phase_val = detection.phase if detection is not None else "neutral"
    lifecycle_dict = fusion_lifecycle_dict(
        detection,
        structure_bias=structure_bias,
        fall_from_high_pct=fall_from_high_pct,
        leg_gain_pct=leg_gain_pct,
    )
    lifecycle_dict = ensure_fusion_lifecycle_fields(lifecycle_dict)
    if detection is not None and detection.quarantine_factors:
        result["fusion_quarantine"] = {
            f.name: {
                "score": round(f.score, 4),
                "active": f.active,
                "kind": f.kind,
                "detail": f.detail,
            }
            for f in detection.quarantine_factors
        }
    return detection, side, phase_val, lifecycle_dict


def apply_fusion_setups(
    *,
    detection: Any,
    side: str,
    phase_val: str,
    lifecycle_dict: dict[str, Any],
    result: dict[str, Any],
    symbol: str,
    intra_bar: Any | None = None,
) -> None:
    """Write dump/long setup dicts from fusion detection onto the tick row."""
    from hunt_core.hunter.detect.delivery_setup import build_delivery_setup

    def _stub_setup(direction: str) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "direction": direction,
            "impulse_confirmed": False,
            "phase": phase_val,
            "lifecycle_phase": phase_val,
            "lifecycle": lifecycle_dict,
        }

    if detection is not None and side in {"long", "short"}:
        active = build_delivery_setup(detection, result, intra_bar=intra_bar)
        if side == "short":
            result["dump"] = active
            result["long"] = _stub_setup("long")
        else:
            result["long"] = active
            result["dump"] = _stub_setup("short")
    else:
        stub = {
            "symbol": symbol,
            "impulse_confirmed": False,
            "phase": phase_val,
            "lifecycle_phase": phase_val,
            "lifecycle": lifecycle_dict,
        }
        result["dump"] = {**stub, "direction": "short"}
        result["long"] = {**stub, "direction": "long"}
