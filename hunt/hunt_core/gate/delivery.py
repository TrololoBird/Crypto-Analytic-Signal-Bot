"""Delivery gates — thin facade over gate submodules."""
from __future__ import annotations


from hunt_core.data.universe import PINNED_SYMBOLS
from hunt_core.gate._filters import directional_filters
from hunt_core.gate._freshness import (
    DeliveryTier,
    classify_delivery_tier,
    delivery_freshness_block,
    delivery_hard_block,
    entry_chase_tol,
    max_tp1_progress,
    price_in_entry_zone,
    tp1_progress_block,
)
from hunt_core.gate._phase_matrix import (
    DEFAULT_MAX_WR,
    DEFAULT_MIN_SAMPLES,
    DEFAULT_PRIOR_WR,
    PhaseStats,
    disabled_phase_pairs,
    phase_matrix_gate,
)
from hunt_core.gate._prokol import detect_prokol
from hunt_core.gate._registry import (
    _snapshot_tier_from_row,
    register_gate,
    run_gate_pipeline,
)
from hunt_core.gate._report import (
    collect_report_blockers,
    evaluate_alert_gate,
    evaluate_formation,
    evaluate_stale_advice,
    format_setup_snapshot,
    primary_block_for_report,
)
from hunt_core.gate._rr import (
    _close_below_support_in_hard,
    _DELIVERY_MIN_RR_FLOOR,
    _DUMP_CONTINUATION_PHASES,
    _effective_min_rr,
    _FADE_PHASES_SHORT,
    _in_pre_dump_window,
    _late_dump_depth_chase_block,
    _min_rr,
    _PUMP_PHASES_LONG,
    _setup_fuel,
    _short_dump_delivery_too_late,
    _short_dump_first_break_max_fall_pct,
    _SHORT_DUMP_START_LC_PHASES,
    _short_dump_start_max_fall_pct,
    _short_pre_dump_headroom_pct,
    _structural_dump_hard,
    _structural_hard_count,
    _tp2_room_blocks,
    effective_min_rr_for_delivery,
    order_flow_demotes_triggered,
)
from hunt_core.gate._types import BOUNCE_MIN_RISK_REWARD, GateResult, REPORT_BLOCK_PRIORITY
from hunt_core.gate._wash import (
    kinematic_block_reason,
    kinematic_z,
    pump_dump_stage,
    wash_block_reason,
    wash_trading_index,
    wash_volume_z_score,
)

MIN_QUOTE_VOL_24H_USD = 1_000_000.0
MIN_OPEN_INTEREST_USD = 100_000.0


def liquidity_skip_reason(
    *,
    quote_volume: float,
    oi: float | None,
    last_price: float,
    symbol: str = "",
) -> str | None:
    """Return error tag when symbol is too illiquid for reliable signals."""
    sym = symbol.upper()
    if sym in PINNED_SYMBOLS:
        return None
    if float(quote_volume or 0) < MIN_QUOTE_VOL_24H_USD:
        return f"liquidity_low_vol24h:{quote_volume:.0f}"
    if oi is not None and last_price > 0:
        oi_usd = float(oi) * last_price
        if oi_usd < MIN_OPEN_INTEREST_USD:
            return f"liquidity_low_oi:{oi_usd:.0f}"
    return None


__all__ = [
    "BOUNCE_MIN_RISK_REWARD",
    "DEFAULT_MAX_WR",
    "DEFAULT_MIN_SAMPLES",
    "DEFAULT_PRIOR_WR",
    "DeliveryTier",
    "GateResult",
    "MIN_OPEN_INTEREST_USD",
    "MIN_QUOTE_VOL_24H_USD",
    "PINNED_SYMBOLS",
    "REPORT_BLOCK_PRIORITY",
    "PhaseStats",
    "_DELIVERY_MIN_RR_FLOOR",
    "_DUMP_CONTINUATION_PHASES",
    "_FADE_PHASES_SHORT",
    "_PUMP_PHASES_LONG",
    "_SHORT_DUMP_START_LC_PHASES",
    "_close_below_support_in_hard",
    "_effective_min_rr",
    "_in_pre_dump_window",
    "_late_dump_depth_chase_block",
    "_min_rr",
    "_setup_fuel",
    "_short_dump_delivery_too_late",
    "_short_dump_first_break_max_fall_pct",
    "_short_dump_start_max_fall_pct",
    "_short_pre_dump_headroom_pct",
    "_snapshot_tier_from_row",
    "_structural_dump_hard",
    "_structural_hard_count",
    "_tp2_room_blocks",
    "classify_delivery_tier",
    "collect_report_blockers",
    "delivery_freshness_block",
    "delivery_hard_block",
    "detect_prokol",
    "directional_filters",
    "disabled_phase_pairs",
    "effective_min_rr_for_delivery",
    "entry_chase_tol",
    "evaluate_alert_gate",
    "evaluate_formation",
    "evaluate_stale_advice",
    "format_setup_snapshot",
    "kinematic_block_reason",
    "kinematic_z",
    "liquidity_skip_reason",
    "max_tp1_progress",
    "order_flow_demotes_triggered",
    "phase_matrix_gate",
    "price_in_entry_zone",
    "primary_block_for_report",
    "pump_dump_stage",
    "register_gate",
    "run_gate_pipeline",
    "tp1_progress_block",
    "wash_block_reason",
    "wash_trading_index",
    "wash_volume_z_score",
]
