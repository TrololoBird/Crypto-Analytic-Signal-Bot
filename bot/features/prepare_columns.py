"""Prepare-frame indicator groups for selective / lazy computation."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

# Groups map to blocks inside _add_advanced_indicators and tail pipeline stages.
ALL_PREPARE_GROUPS: frozenset[str] = frozenset(
    {
        "supertrend",
        "obv",
        "bb",
        "keltner",
        "hma",
        "psar",
        "aroon",
        "stoch",
        "oscillators",
        "fisher",
        "squeeze",
        "chandelier",
        "volume_profile",
        "zscore",
        "candles",
        "microstructure",
        "ols",
        "session",
        "tail_metrics",
        "stoch_rsi",
        "ichimoku",
        "kama",
        "heikin_ashi",
        "pivot_points",
    }
)

# Not referenced by strategies or enrichment telemetry — safe to skip on live path.
LIVE_SKIPPABLE_GROUPS: frozenset[str] = frozenset(
    {
        "psar",
        "aroon",
        "hma",
        "volume_profile",
        "stoch_rsi",
        "ichimoku",
        "kama",
        "heikin_ashi",
        "pivot_points",
    }
)

GROUP_DEPENDENCIES: dict[str, frozenset[str]] = {
    "squeeze": frozenset({"bb", "keltner"}),
}

STRATEGY_PREPARE_GROUPS: dict[str, frozenset[str]] = {
    "squeeze_setup": frozenset(
        {
            "squeeze",
            "bb",
            "keltner",
            "supertrend",
            "obv",
            "stoch",
            "oscillators",
            "zscore",
            "candles",
        }
    ),
    "bb_squeeze": frozenset({"squeeze", "bb", "keltner", "zscore", "obv"}),
    "wick_trap_reversal": frozenset({"supertrend", "stoch", "oscillators", "candles"}),
    "liquidity_sweep": frozenset({"supertrend", "bb", "candles"}),
    "order_block": frozenset({"bb", "keltner", "candles"}),
    "breaker_block": frozenset({"bb", "keltner", "candles"}),
    "stop_hunt_detection": frozenset({"supertrend", "bb", "candles"}),
    "volume_climax_reversal": frozenset({"oscillators", "stoch", "candles"}),
    "volume_anomaly": frozenset({"oscillators", "bb", "candles"}),
    "supertrend_follow": frozenset({"supertrend", "obv", "ichimoku", "heikin_ashi"}),
    "price_velocity": frozenset({"zscore", "ols"}),
    "keltner_breakout": frozenset({"keltner", "bb", "squeeze"}),
    "multi_tf_trend": frozenset({"ichimoku"}),
    "hidden_divergence": frozenset({"stoch_rsi"}),
    "indicator_divergence": frozenset({"stoch_rsi"}),
    "ema_bounce": frozenset({"heikin_ashi", "kama"}),
    "vwap_trend": frozenset({"heikin_ashi"}),
    "atr_expansion": frozenset({"obv"}),
}

_BASE_LIVE_GROUPS: frozenset[str] = frozenset(
    {
        "supertrend",
        "obv",
        "bb",
        "keltner",
        "stoch",
        "oscillators",
        "fisher",
        "squeeze",
        "chandelier",
        "zscore",
        "candles",
        "microstructure",
        "ols",
        "session",
        "tail_metrics",
    }
)


def _expand_group_dependencies(groups: Iterable[str]) -> frozenset[str]:
    expanded = set(groups)
    changed = True
    while changed:
        changed = False
        for group in tuple(expanded):
            for dependency in GROUP_DEPENDENCIES.get(group, frozenset()):
                if dependency not in expanded:
                    expanded.add(dependency)
                    changed = True
    return frozenset(expanded)


def resolve_prepare_groups(
    enabled_setup_ids: tuple[str, ...] | None,
    *,
    full: bool = False,
) -> frozenset[str] | None:
    """Return active indicator groups for the live prepare path.

    ``None`` means compute every group (backtests / tests).
    """
    if full or not enabled_setup_ids:
        return None

    groups = set(_BASE_LIVE_GROUPS)
    for setup_id in enabled_setup_ids:
        groups |= STRATEGY_PREPARE_GROUPS.get(setup_id, _BASE_LIVE_GROUPS)

    groups -= LIVE_SKIPPABLE_GROUPS
    return _expand_group_dependencies(groups)


def group_active(active_groups: frozenset[str] | None, group: str) -> bool:
    return active_groups is None or group in active_groups


__all__ = [
    "ALL_PREPARE_GROUPS",
    "GROUP_DEPENDENCIES",
    "LIVE_SKIPPABLE_GROUPS",
    "STRATEGY_PREPARE_GROUPS",
    "group_active",
    "resolve_prepare_groups",
]
