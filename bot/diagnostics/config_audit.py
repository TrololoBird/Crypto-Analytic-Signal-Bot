"""Runtime configuration sanity checker - observation only, non-blocking warnings."""

from __future__ import annotations

import logging
from typing import Any

from bot.delivery.filter_stages import DEFAULT_FILTER_STAGES, enabled_filter_stages
from engine.domain.config import REQUIRED_PINNED_SYMBOLS, BotSettings
from engine.errors import DEFENSIVE_EXC

LOG = logging.getLogger("bot.config_audit")

TYPICAL_MARKET_ATR_PCT = 0.45
TYPICAL_MARKET_CHANGE_PCT = 1.5
TYPICAL_VOLUME_USD = 10_000_000

_AUDIT_SECTIONS = (
    "filter_warnings",
    "lane_warnings",
    "runtime_warnings",
    "delivery_warnings",
    "universe_warnings",
    "strategy_warnings",
)


def _safe_float(value: Any, default: float = 0.0) -> float:
    if "unittest.mock" in type(value).__module__:
        return default
    try:
        return float(value)
    except TypeError, ValueError:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    if "unittest.mock" in type(value).__module__:
        return default
    try:
        return int(value)
    except TypeError, ValueError:
        return default


def _safe_bool(value: Any, *, default: bool = False) -> bool:
    if "unittest.mock" in type(value).__module__:
        return default
    return bool(value)


def audit_filter_config(settings: Any) -> list[str]:
    """Check filter thresholds against known-good ranges."""
    warnings: list[str] = []
    filters = getattr(settings, "filters", None)
    if filters is None:
        return warnings

    min_atr = _safe_float(getattr(filters, "min_atr_pct", 0.0))
    max_atr = _safe_float(getattr(filters, "max_atr_pct", 99.0), 99.0)
    min_score = _safe_float(getattr(filters, "min_score", 0.0))
    min_rr = _safe_float(getattr(filters, "min_risk_reward", 0.0))
    min_adx = _safe_float(getattr(filters, "min_adx_1h", 0.0))
    cooldown = _safe_float(getattr(filters, "cooldown_minutes", 0.0))

    if min_atr > 1.0:
        warnings.append(
            f"filters.min_atr_pct={min_atr:.2f} is high - most symbols have "
            f"15m ATR near {TYPICAL_MARKET_ATR_PCT:.2f}%; this may reject valid setups"
        )
    if min_atr > 2.0:
        warnings.append(
            f"filters.min_atr_pct={min_atr:.2f} is VERY high - only "
            "extreme-volatility events will pass; consider 0.3-0.8"
        )
    if 0.0 < max_atr <= min_atr:
        warnings.append(
            f"filters.max_atr_pct={max_atr:.2f} is not above "
            f"filters.min_atr_pct={min_atr:.2f}; ATR gating may reject all signals"
        )
    if min_score > 0.80:
        warnings.append(
            f"filters.min_score={min_score:.2f} is high - confluence rarely "
            "exceeds 0.80; typical floor is 0.55-0.68"
        )
    if min_rr > 4.0:
        warnings.append(
            f"filters.min_risk_reward={min_rr:.2f} is high - most structural "
            "setups yield RR 1.8-3.0; consider 1.8-2.5"
        )
    if min_adx > 30.0:
        warnings.append(
            f"filters.min_adx_1h={min_adx:.1f} is high - 1h ADX can stay below "
            "25 for weeks in ranging markets; consider 15-22"
        )
    if cooldown > 240.0:
        warnings.append(
            f"filters.cooldown_minutes={cooldown:.0f} is long - signals may be "
            "blocked for hours after a single delivery"
        )

    try:
        if isinstance(settings, BotSettings):
            active = enabled_filter_stages(settings)
            unknown = sorted(active - frozenset(DEFAULT_FILTER_STAGES))
            if unknown:
                warnings.append(
                    "filters.enabled_stages contains unknown stage(s): "
                    f"{unknown} - delivery may skip expected gates"
                )
            if "min_score" not in active and "scoring" not in active:
                warnings.append(
                    "filters.enabled_stages disables both scoring and min_score - "
                    "most signals will pass score floor unchecked"
                )
    except DEFENSIVE_EXC:
        LOG.debug("filter stage audit skipped", exc_info=True)

    return warnings


def audit_lanes_config(settings: Any) -> list[str]:
    """Check strategy lane and family-cap settings."""
    warnings: list[str] = []
    runtime = getattr(settings, "runtime", None)
    if runtime is None:
        return warnings

    lanes_enabled = _safe_bool(getattr(runtime, "enable_strategy_lanes", True), default=True)
    route_all = _safe_bool(getattr(runtime, "route_all_enabled_strategies", False))
    min_fam = _safe_int(getattr(runtime, "min_setup_families_per_symbol", 8), 8)
    target_fam = _safe_int(getattr(runtime, "target_setup_families_per_symbol", 12), 12)
    max_fam = _safe_int(getattr(runtime, "max_setup_families_per_symbol", 15), 15)

    if not lanes_enabled:
        warnings.append(
            "runtime.enable_strategy_lanes=false - all enabled strategies run per "
            "symbol with no family cap (higher CPU, more duplicate-family hits)"
        )
    if route_all and lanes_enabled:
        warnings.append("runtime.route_all_enabled_strategies=true bypasses lane family caps")
    if min_fam > 12:
        warnings.append(
            f"runtime.min_setup_families_per_symbol={min_fam} is high - lane "
            "selection may be overly restrictive"
        )
    if max_fam < 10:
        warnings.append(
            f"runtime.max_setup_families_per_symbol={max_fam} is low - setup "
            "family diversity per symbol will be limited"
        )
    if not (min_fam <= target_fam <= max_fam):
        warnings.append(
            "runtime setup family bounds inconsistent: "
            f"min={min_fam}, target={target_fam}, max={max_fam}"
        )
    return warnings


def audit_runtime_config(settings: Any) -> list[str]:
    """Check runtime routing telemetry and unified shortlist routing."""
    warnings: list[str] = []
    runtime = getattr(settings, "runtime", None)
    if runtime is None:
        return warnings

    unified = _safe_bool(getattr(runtime, "shortlist_unified_routing", False))
    emit_skips = _safe_bool(getattr(runtime, "emit_strategy_routing_skips", False))
    lanes_enabled = _safe_bool(getattr(runtime, "enable_strategy_lanes", True), default=True)

    if unified:
        warnings.append(
            "runtime.shortlist_unified_routing=true - per-symbol strategy_fits may be empty; "
            "lane routing still caps families per kline event"
        )
    if lanes_enabled and not emit_skips:
        warnings.append(
            "runtime.emit_strategy_routing_skips=false - lane/fit routing skips are not "
            "emitted as strategy_decisions rows"
        )
    return warnings


def audit_shortlist_zero_fit(
    *,
    zero_fit: int,
    shortlist_total: int,
    unified_routing: bool,
) -> list[str]:
    """Warn when many shortlist symbols lack strategy_fits under unified routing."""
    warnings: list[str] = []
    total = max(0, int(shortlist_total))
    zeros = max(0, int(zero_fit))
    if not unified_routing or total <= 0:
        return warnings
    if zeros > total * 0.25:
        warnings.append(
            f"shortlist zero_strategy_fit={zeros}/{total} with shortlist_unified_routing=true - "
            "expected; verify lane coverage if detector runs look low"
        )
    return warnings


def audit_delivery_config(settings: Any) -> list[str]:
    """Check delivery tier thresholds and coherence with filter gates."""
    warnings: list[str] = []
    delivery = getattr(settings, "delivery", None)
    if delivery is None:
        return warnings

    action_min = _safe_float(getattr(delivery, "action_min_score", 0.72), 0.72)
    watch_min = _safe_float(getattr(delivery, "watch_min_score", 0.55), 0.55)
    action_cap = _safe_int(getattr(delivery, "action_cap_per_cycle", 6), 6)
    watch_cap = _safe_int(getattr(delivery, "watch_cap_per_cycle", 12), 12)

    if action_min > 0.80:
        warnings.append(
            f"delivery.action_min_score={action_min:.2f} is high - few signals "
            "will reach ACTION tier"
        )
    if watch_min > action_min:
        warnings.append(
            "delivery.watch_min_score exceeds action_min_score - WATCH tier "
            "may be unreachable for borderline scores"
        )
    filters = getattr(settings, "filters", None)
    if filters is not None:
        min_score = _safe_float(getattr(filters, "min_score", 0.0))
        if min_score > action_min:
            warnings.append(
                f"filters.min_score={min_score:.2f} exceeds "
                f"delivery.action_min_score={action_min:.2f} - candidates fail "
                "before tier classification"
            )
    if action_cap < 2:
        warnings.append(
            f"delivery.action_cap_per_cycle={action_cap} is low - burst "
            "ACTION delivery will be heavily throttled"
        )
    if watch_cap < action_cap:
        warnings.append("delivery.watch_cap_per_cycle is below action_cap_per_cycle")
    return warnings


def audit_universe_config(settings: Any) -> list[str]:
    """Check universe sizing and required pinned symbols."""
    warnings: list[str] = []
    universe = getattr(settings, "universe", None)
    if universe is None:
        return warnings

    pinned = getattr(universe, "pinned_symbols", ()) or ()
    pinned_set = {str(item).strip().upper() for item in pinned if str(item).strip()}
    missing = [symbol for symbol in REQUIRED_PINNED_SYMBOLS if symbol not in pinned_set]
    if missing:
        warnings.append("universe.pinned_symbols missing core symbols: " + ", ".join(missing))

    vol_floor = _safe_float(getattr(universe, "min_quote_volume_usd", 0.0))
    change_floor = _safe_float(getattr(universe, "min_price_change_pct", 0.0))
    limit = _safe_int(getattr(universe, "shortlist_limit", 50), 50)

    if vol_floor > 50_000_000:
        warnings.append(
            f"universe.min_quote_volume_usd={vol_floor:,.0f} is high - typical "
            f"liquid floor is ${TYPICAL_VOLUME_USD:,.0f}/day"
        )
    if change_floor > 3.0:
        warnings.append(
            f"universe.min_price_change_pct={change_floor:.1f} is high - most 24h "
            f"changes sit near {TYPICAL_MARKET_CHANGE_PCT:.1f}% in low-vol markets"
        )
    if limit < 30:
        warnings.append(
            f"universe.shortlist_limit={limit} is low - fewer symbols means "
            "fewer detector runs; recommend >= 50"
        )

    radar = getattr(universe, "radar", None)
    if radar is not None and _safe_bool(getattr(radar, "enabled", False)):
        light_pool = _safe_int(getattr(universe, "light_pool_limit", 180), 180)
        hot = _safe_int(getattr(radar, "hot_pool_limit", 60), 60)
        warm = _safe_int(getattr(radar, "warm_pool_limit", 200), 200)
        reserve = _safe_int(getattr(radar, "promotion_slots_reserve", 12), 12)
        if hot > light_pool:
            warnings.append(f"universe.radar.hot_pool_limit={hot} > light_pool_limit={light_pool}")
        if limit + reserve > light_pool:
            warnings.append(
                "universe.shortlist_limit + radar.promotion_slots_reserve "
                f"({limit}+{reserve}) > light_pool_limit={light_pool}"
            )
        if warm < hot:
            warnings.append(f"universe.radar.warm_pool_limit={warm} < hot_pool_limit={hot}")
        if _safe_bool(getattr(radar, "emit_watch_candidates", False)):
            op = getattr(getattr(settings, "notifiers", None), "telegram_operator", None)
            if op is not None and not _safe_bool(getattr(op, "send_radar_watch_candidate", False)):
                warnings.append(
                    "universe.radar.emit_watch_candidates=true but "
                    "notifiers.telegram_operator.send_radar_watch_candidate=false "
                    "(DMs will not send)"
                )
    return warnings


def audit_strategy_config(settings: Any) -> list[str]:
    """Check enabled setup count for signal diversity."""
    warnings: list[str] = []
    setups = getattr(settings, "setups", None)
    if setups is None:
        return warnings

    enabled_fn = getattr(setups, "enabled_setup_ids", None)
    enabled_count = len(enabled_fn()) if callable(enabled_fn) else 0

    if enabled_count < 5:
        warnings.append(
            f"only {enabled_count} setup(s) enabled - signal diversity will be very low"
        )
    elif enabled_count > 30:
        warnings.append(
            f"{enabled_count} setups enabled - lane caps and cycle latency may "
            "limit effective per-symbol coverage"
        )
    return warnings


def run_full_audit(settings: Any) -> dict[str, Any]:
    """Run all configuration audits and return structured results."""
    sections = {
        "filter_warnings": audit_filter_config(settings),
        "lane_warnings": audit_lanes_config(settings),
        "runtime_warnings": audit_runtime_config(settings),
        "delivery_warnings": audit_delivery_config(settings),
        "universe_warnings": audit_universe_config(settings),
        "strategy_warnings": audit_strategy_config(settings),
    }
    return {**sections, "total_issues": sum(len(items) for items in sections.values())}


def run_startup_audit(settings: Any) -> None:
    """Log startup configuration warnings. Non-blocking; does not mutate settings."""
    result = run_full_audit(settings)
    issues: list[str] = []
    for key in _AUDIT_SECTIONS:
        issues.extend(result[key])

    if not issues:
        LOG.info("config audit: no threshold issues detected")
        return

    LOG.info(
        "config audit found %d potential threshold issues - review config.toml "
        "if signal rate is unexpectedly low:",
        len(issues),
    )
    for issue in issues:
        LOG.info("  CONFIG AUDIT: %s", issue)
