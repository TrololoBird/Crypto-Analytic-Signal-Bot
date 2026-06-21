"""Signal decision — LONG / SHORT / WAIT gates."""
from __future__ import annotations

from hunt_core.analysis.deep.verdict_v2.config import VerdictV2Config
from hunt_core.analysis.deep.verdict_v2.types import (
    DataQualityReport,
    ExpectedPath,
    ScenarioCatalyst,
    ScenarioFragility,
    SignalDecision,
    SignalStrength,
    TradePlan,
    TradeQuality,
)
from hunt_core.analysis.deep.verdict_v2.timing_gate import TimingGate


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
) -> SignalDecision:
    gates_failed: list[str] = []
    action = "wait"
    reason = "default wait"

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

    # Trade quality is advisory (R12) — does NOT auto-fail unless no plan
    if gates_failed:
        reason = f"WAIT: {', '.join(gates_failed)}"
        return SignalDecision(action="wait", reason=reason, gates_failed=gates_failed, trade_plan=plan)

    action = path.direction  # type: ignore[assignment]
    reason = f"{action.upper()} path={path.type} strength={strength.label}"
    return SignalDecision(action=action, reason=reason, gates_failed=[], trade_plan=plan)
