"""RR geometry audit — risk/reward anatomy per deep verdict (P1)."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

from hunt_core.paths import RR_GEOMETRY_AUDIT_JSONL


def rr_audit_enabled() -> bool:
    return os.getenv("HUNT_RR_AUDIT", "1").strip().lower() in {"1", "true", "yes", "on"}


def append_rr_geometry_audit(
    row: dict[str, Any],
    *,
    plan: Any | None,
    verdict: Any | None = None,
) -> None:
    if not rr_audit_enabled() or plan is None:
        return
    try:
        from hunt_core.data.jsonl_io import append_jsonl_lines

        price = float(row.get("price") or 0)
        zone = getattr(plan, "entry_zone", None) or (0.0, 0.0)
        entry_ref = float(getattr(plan, "entry_reference", 0) or 0)
        stop = float(getattr(plan, "stop_loss", 0) or 0)
        tp1 = float(getattr(plan, "take_profit_1", 0) or 0)
        risk_pct = abs(entry_ref - stop) / price * 100.0 if price > 0 and stop > 0 else None
        reward_pct = abs(tp1 - entry_ref) / price * 100.0 if price > 0 and tp1 > 0 else None
        lc = row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else {}
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "symbol": str(row.get("symbol") or "").upper(),
            "action": getattr(getattr(verdict, "signal_decision", None), "action", None)
            if verdict is not None
            else None,
            "phase": lc.get("phase") or lc.get("phase_fusion"),
            "leg_gain_pct": lc.get("leg_gain_pct"),
            "direction": getattr(plan, "direction", None),
            "entry_zone": [round(zone[0], 6), round(zone[1], 6)],
            "entry_reference": entry_ref,
            "stop_loss": stop,
            "tp1": tp1,
            "rr_primary": getattr(plan, "rr_primary", None),
            "rr_tp1": getattr(plan, "rr_tp1", None),
            "geometry_valid": bool(getattr(plan, "take_profit_1", 0) and (
                (getattr(plan, "direction", "") == "long" and float(plan.take_profit_1) > max(plan.entry_zone))
                or (getattr(plan, "direction", "") == "short" and float(plan.take_profit_1) < min(plan.entry_zone))
            )),
            "risk_pct": round(risk_pct, 3) if risk_pct is not None else None,
            "reward_pct": round(reward_pct, 3) if reward_pct is not None else None,
            "gates_failed": list(
                getattr(getattr(verdict, "signal_decision", None), "gates_failed", []) or []
            )
            if verdict is not None
            else [],
        }
        RR_GEOMETRY_AUDIT_JSONL.parent.mkdir(parents=True, exist_ok=True)
        append_jsonl_lines(
            RR_GEOMETRY_AUDIT_JSONL,
            [json.dumps(record, separators=(",", ":"), default=str)],
        )
    except Exception:
        pass


__all__ = ["append_rr_geometry_audit", "rr_audit_enabled"]
