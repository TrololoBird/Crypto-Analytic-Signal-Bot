"""Signal decision — LONG / SHORT / WAIT gates."""
from __future__ import annotations

import os

from hunt_core.deep.verdict_v2.config import VerdictV2Config
from hunt_core.deep.verdict_v2.reconcile import ReconciliationResult
from hunt_core.deep.verdict_v2.types import (
    DataQualityReport,
    ExpectedPath,
    ScenarioCatalyst,
    ScenarioFragility,
    SignalDecision,
    SignalStrength,
    TradePlan,
    TradeQuality,
)
from hunt_core.deep.verdict_v2.timing_gate import TimingGate


def _mid_leg_context_wait(row: dict[str, object] | None) -> str | None:
    """Block deep signal on MID + extended leg — late-chase context (P2)."""
    if os.getenv("HUNT_DEEP_BLOCK_MID", "1").strip().lower() in {"0", "false", "no", "off"}:
        return None
    if not isinstance(row, dict):
        return None
    lc = row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else {}
    phase = str(lc.get("phase_fusion") or lc.get("phase") or "")
    if phase != "mid":
        return None
    try:
        leg = float(lc.get("leg_gain_pct") or 0)
    except (TypeError, ValueError):
        leg = 0.0
    if leg >= 8.0:
        return f"mid_leg leg_gain={leg:.1f}%"
    return None


def decide_signal(
    path: ExpectedPath,
    strength: SignalStrength,
    fragility: ScenarioFragility,
    trade_q: TradeQuality,
    plan: TradePlan | None,
    data: DataQualityReport,
    catalyst: ScenarioCatalyst,
    cfg: VerdictV2Config,
    *,
    timing: TimingGate | None = None,
    reconcile: ReconciliationResult | None = None,
    row: dict[str, object] | None = None,
) -> SignalDecision:
    gates_failed: list[str] = []
    action = "wait"
    reason = "default wait"

    mid_block = _mid_leg_context_wait(row)
    if mid_block:
        gates_failed.append("mid_leg")
        return SignalDecision(
            action="wait",
            reason=f"WAIT: {mid_block}",
            gates_failed=gates_failed,
            trade_plan=plan,
        )

    if reconcile is not None and reconcile.level == "strong_conflict":
        gates_failed.append("context_conflict")
        caveat = reconcile.caveats[0] if reconcile.caveats else "контекст против сделки"
        return SignalDecision(
            action="wait",
            reason=f"WAIT: контекст против — {caveat}",
            gates_failed=gates_failed,
            trade_plan=plan,
        )

    if path.direction == "neutral" or path.type == "range":
        gates_failed.append("path_neutral")
        return SignalDecision(action="wait", reason="Path neutral/range — no directional signal", gates_failed=gates_failed)

    # timing_c is a binary veto — if bar not closed, no point evaluating strength/RR
    if cfg.gates.require_timing_c and timing is not None and not timing.ready:
        gates_failed.append("timing_c")
        return SignalDecision(
            action="wait",
            reason="WAIT: timing_c — ждём закрытия бара",
            gates_failed=gates_failed,
            trade_plan=plan,
        )

    if strength.score < cfg.gates.strength_min:
        gates_failed.append("strength")
    if fragility.score > cfg.gates.fragility_max:
        gates_failed.append("fragility")
    if data.coverage_score < cfg.gates.data_coverage_min:
        gates_failed.append("coverage")
    if catalyst.confidence < cfg.gates.catalyst_min:
        gates_failed.append("catalyst")
    if trade_q.score < cfg.gates.trade_quality_min:
        gates_failed.append("trade_quality")
    if plan is None:
        gates_failed.append("no_plan")
    else:
        from hunt_core.deep.plan import plan_geometry_valid

        plan_ok = plan_geometry_valid(
            {
                "entry_zone": [plan.entry_zone[0], plan.entry_zone[1]],
                "tp1": plan.take_profit_1,
            },
            direction=plan.direction,  # type: ignore[arg-type]
        )
        if not plan_ok:
            gates_failed.append("plan_geometry")
        else:
            rr_floor = cfg.gates.rr_primary_min
            if strength.label == "weak":
                rr_floor = max(rr_floor, 1.8)
            elif strength.label == "moderate":
                rr_floor = max(rr_floor, 1.3)
            if plan.rr_primary < rr_floor:
                gates_failed.append("rr_primary")

    if gates_failed:
        reason = f"WAIT: {', '.join(gates_failed)}"
        return SignalDecision(action="wait", reason=reason, gates_failed=gates_failed, trade_plan=plan)

    action = path.direction  # type: ignore[assignment]
    reason = f"{action.upper()} path={path.type} strength={strength.label}"
    if reconcile and reconcile.level == "mild_conflict":
        caveat = reconcile.caveats[0] if reconcile.caveats else "контекст частично против"
        reason += f" · осторожно: {caveat}"
    return SignalDecision(action=action, reason=reason, gates_failed=[], trade_plan=plan)
