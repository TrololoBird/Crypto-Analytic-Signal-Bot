"""Gate registry — extension hooks and pre-blockers for the report stack (#26)."""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from hunt_core.scanner.gate._types import GateResult
from hunt_core.scanner.gate.policy import direction_block_reason
from hunt_core.track.events import record_funnel_stage

if TYPE_CHECKING:
    from hunt_core.deliver.dispatch import SniperConfig

GateChecker = Callable[..., GateResult | None]

_GATE_REGISTRY: list[tuple[str, GateChecker]] = []


def register_gate(name: str, fn: GateChecker) -> None:
    """Register a gate checker — first failure short-circuits the pipeline."""
    if any(existing == name for existing, _ in _GATE_REGISTRY):
        return
    _GATE_REGISTRY.append((name, fn))


def _gate_edge_policy(
    *,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    lifecycle: dict[str, Any] | None,
    symbol: str = "",
    sniper_config: SniperConfig | None = None,
) -> GateResult | None:
    _ = setup, row, lifecycle, symbol, sniper_config
    edge_block = direction_block_reason(direction)
    if edge_block:
        return GateResult(ok=False, code=edge_block, message=edge_block)
    return None


def _gate_stale(
    *,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    lifecycle: dict[str, Any] | None,
    symbol: str = "",
    sniper_config: SniperConfig | None = None,
) -> GateResult | None:
    from hunt_core.scanner.gate._freshness import delivery_hard_block

    _ = lifecycle, symbol, sniper_config
    stale = delivery_hard_block(direction=direction, setup=setup, row=row)
    if stale:
        return GateResult(
            ok=False,
            code=stale,
            message="Setup устарел — цена уже за TP1 или нет геометрии входа",
        )
    return None


def _gate_wash(
    *,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    lifecycle: dict[str, Any] | None,
    symbol: str = "",
    sniper_config: SniperConfig | None = None,
) -> GateResult | None:
    from hunt_core.scanner.gate._wash import wash_block_reason

    _ = setup, sniper_config
    sym = symbol or str(row.get("symbol", ""))
    lc = lifecycle if isinstance(lifecycle, dict) else {}
    wash = wash_block_reason(row=row, lifecycle=lc)
    if wash:
        record_funnel_stage("wash", symbol=sym, direction=direction, detail=wash)
        return GateResult(
            ok=False,
            code=wash,
            message="Подозрение на wash / манипуляцию объёмом",
        )
    return None


def _gate_kinematic(
    *,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    lifecycle: dict[str, Any] | None,
    symbol: str = "",
    sniper_config: SniperConfig | None = None,
) -> GateResult | None:
    from hunt_core.scanner.gate._wash import kinematic_block_reason

    _ = setup, sniper_config
    sym = symbol or str(row.get("symbol", ""))
    lc = lifecycle if isinstance(lifecycle, dict) else {}
    phase = str(lc.get("phase") or "")
    chase = kinematic_block_reason(row=row, direction=direction, lifecycle_phase=phase)
    if chase:
        record_funnel_stage("kinematic", symbol=sym, direction=direction, detail=chase)
        return GateResult(
            ok=False,
            code=chase,
            message="Слишком быстрое движение — поздний вход",
        )
    return None


def _snapshot_tier_from_row(
    row: dict[str, Any],
    setup: dict[str, Any],
    *,
    fast_lane: bool = False,
) -> str:
    tier = str(
        row.get("snapshot_tier")
        or setup.get("delivery_lane")
        or ("fast" if fast_lane else "full")
    ).lower()
    if tier == "hot":
        return "fast"
    return tier


def _gate_data_completeness(
    *,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    lifecycle: dict[str, Any] | None,
    symbol: str = "",
    sniper_config: SniperConfig | None = None,
    fast_lane: bool = False,
) -> GateResult | None:
    from hunt_core.data.completeness import delivery_derivatives_complete

    _ = direction, lifecycle, symbol, sniper_config
    tier = _snapshot_tier_from_row(row, setup, fast_lane=fast_lane)
    ok, missing = delivery_derivatives_complete(row, tier=tier)
    if ok:
        return None
    detail = ", ".join(missing[:8])
    if len(missing) > 8:
        detail += f" (+{len(missing) - 8})"
    return GateResult(
        ok=False,
        code="data_incomplete",
        message=f"Деривативы неполные ({tier}): {detail}",
    )


def _gate_move(
    *,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    lifecycle: dict[str, Any] | None,
    symbol: str = "",
    sniper_config: SniperConfig | None = None,
) -> GateResult | None:
    from hunt_core.scanner.gate._move import evaluate_move_significance
    from hunt_core.scanner.gate._report import log_strategic_shadow
    from hunt_core.scanner.gate._strategic import strategic_gates_hard

    _ = lifecycle, sniper_config
    price = float(row.get("price") or setup.get("price") or 0)
    shadow = not strategic_gates_hard()
    res = evaluate_move_significance(setup, direction=direction, price=price, shadow=shadow)
    if res.code == "move_shadow_warn":
        log_strategic_shadow(
            symbol=symbol or str(row.get("symbol", "")),
            direction=direction,
            setup=setup,
            row=row,
            code=res.code,
            message=res.message,
        )
    if not res.ok:
        return GateResult(ok=False, code=res.code, message=res.message)
    return None


def _gate_tradability(
    *,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    lifecycle: dict[str, Any] | None,
    symbol: str = "",
    sniper_config: SniperConfig | None = None,
) -> GateResult | None:
    from hunt_core.scanner.gate._report import log_strategic_shadow
    from hunt_core.scanner.gate._strategic import strategic_gates_hard
    from hunt_core.scanner.gate._tradability import evaluate_tradability

    _ = direction, lifecycle, sniper_config
    price = float(row.get("price") or setup.get("price") or 0)
    shadow = not strategic_gates_hard()
    res = evaluate_tradability(setup, price=price, shadow=shadow)
    if res.code == "tradability_shadow_warn":
        log_strategic_shadow(
            symbol=symbol or str(row.get("symbol", "")),
            direction=direction,
            setup=setup,
            row=row,
            code=res.code,
            message=res.message,
        )
    if not res.ok:
        return GateResult(ok=False, code=res.code, message=res.message)
    return None


def _gate_squeeze_predump(
    *,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    lifecycle: dict[str, Any] | None,
    symbol: str = "",
    sniper_config: SniperConfig | None = None,
) -> GateResult | None:
    from hunt_core.scanner.playbook import squeeze_blocks_predump_short

    _ = setup, sniper_config, lifecycle, symbol
    if direction != "short":
        return None
    if squeeze_blocks_predump_short(row):
        return GateResult(
            ok=False,
            code="squeeze_blocks_predump_short",
            message="Crowded shorts + negative funding — squeeze risk blocks predump",
        )
    return None


def pipeline_pre_blockers(
    *,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    lifecycle: dict[str, Any] | None,
    symbol: str,
    sniper_config: Any | None = None,
    fast_lane: bool = False,
) -> list[GateResult]:
    """Registry gates folded into the single report/live stack (#26)."""
    out: list[GateResult] = []
    for checker in (
        _gate_data_completeness,
        _gate_stale,
        _gate_wash,
        _gate_kinematic,
        _gate_move,
        _gate_tradability,
        _gate_squeeze_predump,
    ):
        blocked = checker(
            direction=direction,
            setup=setup,
            row=row,
            lifecycle=lifecycle,
            symbol=symbol,
            sniper_config=sniper_config,
            **({"fast_lane": fast_lane} if checker is _gate_data_completeness else {}),
        )
        if blocked is not None and not blocked.ok:
            out.append(blocked)
    return out


def _gate_mission(
    *,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    lifecycle: dict[str, Any] | None,
    symbol: str = "",
    sniper_config: SniperConfig | None = None,
) -> GateResult | None:
    from hunt_core.scanner.gate._mission import mission_delivery_block

    _ = sniper_config
    sym = symbol or str(row.get("symbol", ""))
    return mission_delivery_block(
        direction=direction,
        lifecycle=lifecycle if isinstance(lifecycle, dict) else {},
        setup=setup,
        symbol=sym,
    )


def _register_builtin_gates() -> None:
    register_gate("edge_policy", _gate_edge_policy)
    register_gate("mission", _gate_mission)
    register_gate("data_completeness", _gate_data_completeness)
    register_gate("stale", _gate_stale)
    register_gate("wash", _gate_wash)
    register_gate("kinematic", _gate_kinematic)
    register_gate("move_significance", _gate_move)
    register_gate("tradability", _gate_tradability)
    register_gate("squeeze_predump", _gate_squeeze_predump)


_register_builtin_gates()


def run_gate_pipeline(
    *,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    lifecycle: dict[str, Any] | None,
    symbol: str = "",
    sniper_config: SniperConfig | None = None,
    fast_lane: bool = False,
) -> GateResult:
    """Single declarative + report stack (#26); registry kept for extension hooks only."""
    from hunt_core.scanner.gate._report import evaluate_alert_gate

    sym = symbol or str(row.get("symbol", ""))
    return evaluate_alert_gate(
        setup,
        direction=direction,
        symbol=sym,
        lifecycle=lifecycle,
        row=row,
        sniper_config=sniper_config,
        fast_lane=fast_lane,
    )


__all__ = [
    "GateChecker",
    "_snapshot_tier_from_row",
    "pipeline_pre_blockers",
    "register_gate",
    "run_gate_pipeline",
]
