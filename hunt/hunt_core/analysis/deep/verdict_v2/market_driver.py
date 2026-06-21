"""Market driver — pattern whitelist only."""
from __future__ import annotations

from hunt_core.analysis.deep.verdict_v2.types import DriverKind, MarketDriver, PatternCandidate

DRIVER_PATTERN_WHITELIST: dict[DriverKind, frozenset[str]] = {
    "trend_driven": frozenset(
        {"trend_continuation", "trend_acceleration", "bull_pullback", "bear_rally", "distribution", "accumulation"}
    ),
    "positioning_driven": frozenset({"range_bound", "mean_reversion", "value_area_reject"}),
    "liquidity_driven": frozenset({"long_squeeze", "short_squeeze", "liquidity_sweep", "stop_hunt"}),
    "flow_driven": frozenset({"flow_continuation", "flow_reversal", "cvd_divergence_play"}),
    "unknown": frozenset(),
}


def infer_driver(
    engines_evidence: list[str],
    pattern_id: str,
) -> MarketDriver:
    joined = " ".join(engines_evidence).lower()
    if any(k in joined for k in ("bos_", "structure_", "htf_")):
        primary: DriverKind = "trend_driven"
        hypothesis = "Structure and HTF trend drive the scenario"
    elif any(k in joined for k in ("liq", "magnet", "void", "poc")):
        primary = "positioning_driven"
        hypothesis = "Liquidity pools and value magnets define the path"
    elif any(k in joined for k in ("funding", "oi_", "crowded")):
        primary = "positioning_driven"
        hypothesis = "Derivatives positioning skews reward asymmetry"
    elif any(k in joined for k in ("taker", "delta", "cvd", "flow")):
        primary = "flow_driven"
        hypothesis = "Order flow confirms or contradicts structure"
    else:
        primary = "unknown"
        hypothesis = "Mixed drivers — no single causal chain"

    allowed = DRIVER_PATTERN_WHITELIST.get(primary, frozenset())
    secondary = pattern_id if pattern_id in allowed else None
    return MarketDriver(
        primary=primary,
        secondary=secondary,
        hypothesis=hypothesis,
        evidence=engines_evidence[:4],
    )


def filter_patterns_by_driver(
    candidates: list[PatternCandidate],
    driver: MarketDriver,
) -> list[PatternCandidate]:
    allowed = DRIVER_PATTERN_WHITELIST.get(driver.primary)
    if not allowed:
        return candidates
    filtered = [c for c in candidates if c.id in allowed]
    return filtered if filtered else candidates
