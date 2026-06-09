"""Strategy lanes - limit detectors per symbol per kline event (target spec)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bot.market.universe import strategy_fits_for_market_row

if TYPE_CHECKING:
    from collections.abc import Iterable

    from bot.domain.config import BotSettings
    from bot.domain.strategies import StrategyMetadata
    from bot.engine.registry import StrategyRegistry

# When a canonical setup subsumes a spec-only sibling, drop the sibling from lanes.
_SPEC_DELEGATE_CANONICAL: dict[str, str] = {
    "bb_squeeze": "squeeze_setup",
    "atr_expansion": "squeeze_setup",
    "fakeout_detector": "liquidity_sweep",
    "stop_hunt_detection": "liquidity_sweep",
    "cvd_exhaustion": "cvd_divergence",
    "hidden_divergence": "indicator_divergence",
    "rsi_divergence_bottom": "indicator_divergence",
    "breaker_block": "order_block",
}

_STANDARD_KLINE_INTERVALS = frozenset(
    {
        "1m",
        "3m",
        "5m",
        "15m",
        "30m",
        "1h",
        "2h",
        "4h",
        "6h",
        "8h",
        "12h",
        "1d",
        "3d",
        "1w",
        "1M",
    }
)


def select_lane_setups(
    registry: StrategyRegistry,
    *,
    symbol: str,
    interval: str,
    settings: BotSettings,
    strategy_fits: Iterable[str] | None = None,
    market_row: dict[str, object] | None = None,
    apply_interval_filter: bool = True,
    priority_setup_ids: Iterable[str] | None = None,
) -> list[StrategyMetadata]:
    """Return metadata for setups to run on this symbol+interval."""
    _ = symbol  # reserved for per-symbol lane policy
    runtime = settings.runtime
    max_families = int(runtime.max_setup_families_per_symbol)
    min_families = int(runtime.min_setup_families_per_symbol)
    target_families = int(runtime.target_setup_families_per_symbol)
    lane_limit = min(max(target_families, min_families), max_families)
    max_per_family = int(runtime.max_setups_per_family)
    route_all = bool(runtime.route_all_enabled_strategies)
    allow_trigger_interval_fallback = bool(runtime.allow_trigger_interval_fallback)
    allow_timeframe_fallback = bool(runtime.allow_timeframe_fallback)
    priority_ids = frozenset(
        str(item).strip() for item in (priority_setup_ids or ()) if str(item).strip()
    )

    enabled = registry.list_enabled()
    if strategy_fits is not None:
        fit_set = set(strategy_fits)
        enabled = [m for m in enabled if m.strategy_id in fit_set]
    elif not route_all:
        if market_row is not None:
            fits = strategy_fits_for_market_row(market_row)
            fit_set = set(fits)
            enabled = [m for m in enabled if m.strategy_id in fit_set]

    interval_key = str(interval or "").strip()
    if apply_interval_filter:
        if not interval_key or interval_key not in _STANDARD_KLINE_INTERVALS:
            return []
        ordered = _interval_matches(
            enabled,
            interval=interval_key,
            allow_trigger_interval_fallback=allow_trigger_interval_fallback,
            allow_timeframe_fallback=allow_timeframe_fallback,
            priority_ids=priority_ids,
        )
        if not ordered:
            return []
    else:
        ordered = _dedupe_sorted(enabled, priority_ids=priority_ids)

    ordered = _exclude_duplicate_spec_delegates(ordered)
    return _cap_unique_families(ordered, lane_limit, max_per_family=max_per_family)


def _exclude_duplicate_spec_delegates(
    ordered: list[StrategyMetadata],
) -> list[StrategyMetadata]:
    """Drop spec-only siblings when a canonical setup already routes the same detector."""
    selected_ids = {meta.strategy_id for meta in ordered}
    drop: set[str] = set()
    for sibling_id, canonical_id in _SPEC_DELEGATE_CANONICAL.items():
        if canonical_id in selected_ids and sibling_id in selected_ids:
            drop.add(sibling_id)
    if not drop:
        return ordered
    return [meta for meta in ordered if meta.strategy_id not in drop]


def _cap_unique_families(
    ordered: list[StrategyMetadata],
    limit: int,
    *,
    max_per_family: int,
) -> list[StrategyMetadata]:
    """Keep up to *max_per_family* distinct setups per family in sort order."""
    if limit <= 0:
        return []
    result: list[StrategyMetadata] = []
    family_counts: dict[str, int] = {}
    for meta in ordered:
        family = str(meta.family or "")
        count = family_counts.get(family, 0)
        if count >= max_per_family:
            continue
        family_counts[family] = count + 1
        result.append(meta)
        if len(result) >= limit:
            break
    return result


def _dedupe_sorted(
    enabled: list[StrategyMetadata],
    *,
    priority_ids: frozenset[str] = frozenset(),
) -> list[StrategyMetadata]:
    ordered: list[StrategyMetadata] = []
    seen_ids: set[str] = set()
    for meta in sorted(enabled, key=lambda item: _sort_key(item, priority_ids)):
        if meta.strategy_id in seen_ids:
            continue
        ordered.append(meta)
        seen_ids.add(meta.strategy_id)
    return ordered


def _sort_key(
    meta: StrategyMetadata, priority_ids: frozenset[str] = frozenset()
) -> tuple[int, str, str]:
    priority = 0 if meta.strategy_id in priority_ids else 1
    family = str(meta.family or "")
    return priority, family, meta.strategy_id


def _interval_matches(
    enabled: list[StrategyMetadata],
    *,
    interval: str,
    allow_trigger_interval_fallback: bool,
    allow_timeframe_fallback: bool,
    priority_ids: frozenset[str] = frozenset(),
) -> list[StrategyMetadata]:
    primary_matches: list[StrategyMetadata] = []
    trigger_interval_matches: list[StrategyMetadata] = []
    timeframe_matches: list[StrategyMetadata] = []
    for meta in enabled:
        if str(meta.trigger_tf or "15m") == interval:
            primary_matches.append(meta)
            continue
        if allow_trigger_interval_fallback and interval in meta.trigger_intervals:
            trigger_interval_matches.append(meta)
            continue
        if allow_timeframe_fallback and interval in meta.timeframes:
            timeframe_matches.append(meta)
            continue

    ordered: list[StrategyMetadata] = []
    seen_ids: set[str] = set()
    for group in (
        sorted(primary_matches, key=lambda item: _sort_key(item, priority_ids)),
        sorted(trigger_interval_matches, key=lambda item: _sort_key(item, priority_ids)),
        sorted(timeframe_matches, key=lambda item: _sort_key(item, priority_ids)),
    ):
        for meta in group:
            if meta.strategy_id in seen_ids:
                continue
            ordered.append(meta)
            seen_ids.add(meta.strategy_id)
    return ordered


def is_standard_kline_interval(interval: str) -> bool:
    """True when *interval* is a Binance-supported kline interval string."""
    key = str(interval or "").strip()
    return bool(key) and key in _STANDARD_KLINE_INTERVALS
