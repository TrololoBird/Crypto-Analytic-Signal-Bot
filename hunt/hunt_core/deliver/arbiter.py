"""Single confirm-boundary authority arbiter (A1)."""
from __future__ import annotations

from typing import Any

from hunt_core.scanner.gate._types import GateResult
from hunt_core.track.outcome_ledger import build_authority_snapshot


def evaluate_confirm_authorities(
    *,
    row: dict[str, Any],
    direction: str,
    setup: dict[str, Any],
    blockers: list[str] | None = None,
    lifecycle: dict[str, Any] | None = None,
) -> GateResult:
    """Require fusion confirmed + playbook pass + mission pass before production TG."""
    _ = lifecycle
    if not isinstance(setup, dict):
        return GateResult(ok=False, code="invalid_setup", message="invalid_setup")

    if not (setup.get("impulse_confirmed") or setup.get("intrabar_confirmed")):
        return GateResult(ok=False, code="not_confirmed", message="not_confirmed")

    # Pre-phase signals use Dual-Gate as authority — skip playbook N-of-M.
    # pre_phase_blocked + impulse_confirmed also skip (main gate authority is sufficient).
    st = setup.get("signal_type")
    if st != "pre_phase" and not (st == "pre_phase_blocked" and setup.get("impulse_confirmed")):
        mf = row.get("manipulation_fusion") if isinstance(row.get("manipulation_fusion"), dict) else {}
        req_n = mf.get("required_n")
        pass_n = mf.get("pass_count")
        if req_n is not None:
            try:
                if int(pass_n or 0) < int(req_n):
                    return GateResult(ok=False, code="playbook_fail", message="playbook_fail")
            except (TypeError, ValueError):
                return GateResult(ok=False, code="playbook_fail", message="playbook_fail")

    snap = build_authority_snapshot(
        setup=setup,
        row=row,
        blockers=blockers,
        delivered=False,
    )
    if not snap.get("fusion_gate_open"):
        return GateResult(ok=False, code="fusion_gate_closed", message="fusion_gate_closed")

    # Pre-phase signals use Dual-Gate as authority — skip playbook_pass_ok check
    st = setup.get("signal_type")
    if snap.get("playbook_pass_ok") is False and st != "pre_phase" and not (st == "pre_phase_blocked" and setup.get("impulse_confirmed")):
        return GateResult(ok=False, code="playbook_fail", message="playbook_fail")

    if not snap.get("mission_pass"):
        return GateResult(ok=False, code="mission_block", message="mission_block")

    from hunt_core.deliver.cross_module import cross_module_delivery_block

    cross = cross_module_delivery_block(row, direction=direction)
    if cross:
        return GateResult(ok=False, code=cross, message=cross)

    return GateResult(ok=True, code="arbiter_pass", message="arbiter_pass")
