"""Signal decision — LONG / SHORT / WAIT gates."""
from __future__ import annotations

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
) -> SignalDecision:
    gates_failed: list[str] = []
    action = "wait"
    reason = "default wait"

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

    if strength.score < cfg.gates.strength_min:
        gates_failed.append("strength")
    if fragility.score > cfg.gates.fragility_max:
        gates_failed.append("fragility")
    if data.coverage_score < cfg.gates.data_coverage_min:
        gates_failed.append("coverage")
    if catalyst.confidence < 0.35:
        gates_failed.append("catalyst")
    if cfg.gates.require_timing_c and timing is not None and not timing.ready:
        gates_failed.append("timing_c")
    if plan is None:
        gates_failed.append("no_plan")
    elif plan.rr_primary < cfg.gates.rr_primary_min:
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
