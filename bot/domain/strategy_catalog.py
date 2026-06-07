"""Canonical 38-strategy catalog (docs/research/STRATEGY_CATALOG.md)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic import BaseModel

    from bot.domain.config import BotSettings


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    setup_id: str
    wave: int
    evidence_level: str  # A | B | C
    trigger_tf: str
    pattern_tf: str
    required_tfs: tuple[str, ...]
    trigger_intervals: tuple[str, ...] = ()
    family: str = "continuation"
    confirmation_profile: str = "trend_follow"
    min_volume_ratio: float = 0.85
    min_adx_1h: float = 15.0
    min_rr: float = 1.9
    base_score: float = 0.52


_TF_RANK: dict[str, int] = {
    "1m": 0,
    "3m": 1,
    "5m": 2,
    "15m": 3,
    "30m": 4,
    "1h": 5,
    "2h": 6,
    "4h": 7,
    "6h": 8,
    "8h": 9,
    "12h": 10,
    "1d": 11,
    "3d": 12,
    "1w": 13,
}


def _tf_rank(tf: str) -> int:
    return _TF_RANK.get(tf.strip(), 99)


def _parse_trigger(trigger: str, *, primary: str | None = None) -> tuple[str, tuple[str, ...]]:
    """Split catalog trigger column into primary TF and alternates."""
    raw = trigger.strip()
    if "+clock" in raw:
        raw = raw.split("+", 1)[0].strip()
    parts = tuple(p.strip() for p in raw.split("/") if p.strip())
    if not parts:
        return "15m", ()
    if len(parts) == 1:
        return parts[0], ()
    if primary is not None:
        if primary in parts:
            return primary, tuple(p for p in parts if p != primary)
        return primary, parts
    # Default: LTF-first primary (15m-first bot); HTF closes as alternates.
    ordered = sorted(parts, key=_tf_rank)
    tf = ordered[0]
    alts = tuple(p for p in parts if p != tf)
    return tf, alts


def _parse_pattern(pattern: str) -> str:
    """Normalize catalog pattern column to a single pattern_tf."""
    raw = pattern.strip()
    if not raw:
        return "15m"
    if "/" not in raw:
        return raw
    parts = tuple(p.strip() for p in raw.split("/") if p.strip())
    if not parts:
        return "15m"
    return min(parts, key=_tf_rank)


def _parse_required(required: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in required.replace(" ", "").split(",") if x.strip())


def _e(
    setup_id: str,
    wave: int,
    level: str,
    trigger: str,
    pattern: str,
    required: str,
    *,
    trigger_primary: str | None = None,
    extra_trigger_intervals: tuple[str, ...] = (),
    family: str = "continuation",
    profile: str = "trend_follow",
    min_vol: float = 0.85,
    min_adx: float = 15.0,
    min_rr: float = 1.9,
    base: float = 0.52,
) -> CatalogEntry:
    trigger_tf, trigger_intervals = _parse_trigger(trigger, primary=trigger_primary)
    if extra_trigger_intervals:
        trigger_intervals = tuple(dict.fromkeys((*trigger_intervals, *extra_trigger_intervals)))
    return CatalogEntry(
        setup_id,
        wave,
        level,
        trigger_tf,
        _parse_pattern(pattern),
        _parse_required(required),
        trigger_intervals=trigger_intervals,
        family=family,
        confirmation_profile=profile,
        min_volume_ratio=min_vol,
        min_adx_1h=min_adx,
        min_rr=min_rr,
        base_score=base,
    )


CATALOG_ENTRIES: tuple[CatalogEntry, ...] = (
    _e("structure_pullback", 1, "A", "15m", "15m", "1h,4h,15m", base=0.65),
    _e("structure_break_retest", 1, "A", "15m", "1h/15m", "1h,15m", base=0.65),
    _e(
        "wick_trap_reversal",
        1,
        "A",
        "15m",
        "15m",
        "15m,1h",
        family="reversal",
        profile="countertrend_exhaustion",
        min_vol=1.0,
        base=0.60,
    ),
    _e(
        "squeeze_setup",
        1,
        "A",
        "15m",
        "15m",
        "15m,1h",
        family="breakout",
        profile="breakout_acceptance",
        min_vol=1.2,
        base=0.55,
    ),
    _e("ema_bounce", 1, "A", "15m", "15m", "1h,15m", base=0.70, min_adx=15.0),
    _e("fvg_setup", 1, "A", "15m", "15m", "1h,4h,15m", base=0.62),
    _e("order_block", 1, "A", "15m", "15m", "1h,15m", base=0.65),
    _e("liquidity_sweep", 1, "A", "15m", "15m", "1h,15m", family="reversal", base=0.60),
    _e("bos_choch", 1, "A", "15m", "15m", "1h,15m", family="reversal", base=0.60),
    _e(
        "hidden_divergence",
        2,
        "A",
        "15m",
        "15m",
        "1h,15m",
        family="reversal",
        profile="divergence_reversal",
        min_vol=0.55,
        base=0.60,
    ),
    _e(
        "indicator_divergence",
        2,
        "A",
        "15m",
        "15m",
        "15m,1h",
        family="reversal",
        profile="divergence_reversal",
        min_vol=0.75,
        base=0.60,
    ),
    _e(
        "funding_reversal",
        2,
        "B",
        "1h/15m",
        "15m",
        "1h,15m",
        family="reversal",
        profile="countertrend_exhaustion",
        min_vol=0.85,
        base=0.58,
    ),
    _e(
        "cvd_divergence",
        2,
        "A",
        "15m",
        "15m",
        "15m",
        family="reversal",
        profile="divergence_reversal",
        base=0.60,
    ),
    _e(
        "session_killzone",
        2,
        "B",
        "15m+clock",
        "15m",
        "1h,15m",
        family="breakout",
        min_vol=1.0,
        base=0.52,
    ),
    _e("breaker_block", 2, "A", "15m", "15m", "1h,15m", family="breakout", min_vol=0.70, base=0.58),
    _e(
        "turtle_soup",
        2,
        "A",
        "15m",
        "1h/15m",
        "1h,15m",
        family="reversal",
        profile="countertrend_exhaustion",
        base=0.58,
    ),
    _e("vwap_trend", 2, "A", "15m", "15m", "15m", min_vol=1.05, base=0.65),
    _e("supertrend_follow", 2, "A", "15m", "15m", "4h,1h,15m", base=0.62),
    _e("multi_tf_trend", 3, "A", "15m", "4h/1h", "4h,1h,15m", min_vol=0.90, base=0.68),
    _e(
        "price_velocity",
        3,
        "A",
        "15m",
        "15m",
        "15m",
        family="breakout",
        profile="breakout_acceptance",
        min_vol=1.0,
        min_adx=16.0,
        base=0.58,
    ),
    _e("volume_anomaly", 3, "A", "15m", "15m", "15m", family="breakout", min_vol=1.6, base=0.55),
    _e(
        "volume_climax_reversal",
        3,
        "A",
        "15m",
        "15m",
        "1h,15m",
        family="reversal",
        profile="countertrend_exhaustion",
        min_vol=1.3,
        base=0.55,
    ),
    _e(
        "keltner_breakout",
        3,
        "A",
        "15m",
        "15m",
        "15m,1h",
        family="breakout",
        min_vol=1.25,
        base=0.58,
    ),
    _e("bb_squeeze", 3, "A", "15m", "15m", "15m,4h", family="volatility", min_vol=0.90, base=0.50),
    _e("atr_expansion", 3, "A", "15m", "15m", "15m", family="volatility", base=0.50),
    _e(
        "whale_walls",
        4,
        "C",
        "15m",
        "15m",
        "15m",
        family="orderbook",
        min_vol=0.90,
        min_adx=0.0,
        base=0.48,
    ),
    _e(
        "spread_strategy",
        4,
        "C",
        "15m",
        "15m",
        "15m",
        family="orderbook",
        min_vol=0.90,
        min_adx=0.0,
        base=0.48,
    ),
    _e(
        "depth_imbalance",
        4,
        "C",
        "15m",
        "15m",
        "15m",
        family="orderbook",
        min_vol=0.80,
        min_adx=0.0,
        base=0.48,
    ),
    _e("absorption", 4, "A", "15m", "15m", "15m", family="orderflow", min_vol=0.90, base=0.50),
    _e(
        "aggression_shift", 4, "B", "15m", "15m", "15m", family="orderflow", min_vol=0.90, base=0.50
    ),
    _e(
        "liquidation_heatmap",
        4,
        "B",
        "15m",
        "15m",
        "15m,1h",
        family="liquidity",
        min_vol=0.90,
        base=0.52,
    ),
    _e(
        "stop_hunt_detection",
        4,
        "A",
        "15m",
        "15m",
        "15m,1h",
        family="liquidity",
        min_vol=0.80,
        base=0.52,
    ),
    _e(
        "oi_divergence",
        5,
        "A",
        "4h/15m",
        "15m",
        "4h,15m",
        trigger_primary="4h",
        family="sentiment",
        min_vol=0.0,
        min_adx=0.0,
        base=0.52,
    ),
    _e(
        "ls_ratio_extreme",
        5,
        "B",
        "4h",
        "15m",
        "4h,15m",
        family="sentiment",
        profile="countertrend_exhaustion",
        min_vol=0.90,
        base=0.50,
    ),
    _e(
        "rsi_divergence_bottom",
        5,
        "A",
        "15m",
        "15m",
        "1h,15m",
        family="reversal",
        profile="divergence_reversal",
        base=0.55,
    ),
    _e(
        "wyckoff_spring",
        5,
        "A",
        "15m",
        "1h",
        "1h,15m",
        family="reversal",
        profile="countertrend_exhaustion",
        min_vol=1.05,
        base=0.55,
    ),
    _e(
        "btc_correlation",
        5,
        "B",
        "15m",
        "15m",
        "1h,15m",
        family="multi_asset",
        min_vol=0.70,
        base=0.52,
    ),
    _e(
        "altcoin_season_index",
        5,
        "B",
        "1h",
        "1h",
        "1d,4h,1h",
        extra_trigger_intervals=("4h", "1d"),
        family="multi_asset",
        min_vol=0.80,
        min_adx=0.0,
        base=0.48,
    ),
    _e(
        "orderflow_imbalance",
        3,
        "B",
        "15m",
        "15m",
        "15m,1h",
        family="reversal",
        profile="countertrend_exhaustion",
        min_vol=0.85,
        base=0.55,
    ),
    _e(
        "pinbar_reversal",
        3,
        "B",
        "15m",
        "15m",
        "15m,1h",
        family="reversal",
        profile="countertrend_exhaustion",
        min_vol=0.75,
        base=0.52,
    ),
    _e(
        "fakeout_detector",
        3,
        "B",
        "15m",
        "15m",
        "15m,1h",
        family="reversal",
        profile="countertrend_exhaustion",
        min_vol=1.0,
        base=0.52,
    ),
    _e(
        "cvd_exhaustion",
        3,
        "B",
        "15m",
        "15m",
        "15m,1h",
        family="reversal",
        profile="countertrend_exhaustion",
        min_vol=0.70,
        base=0.52,
    ),
)

CATALOG_SETUP_IDS: frozenset[str] = frozenset(entry.setup_id for entry in CATALOG_ENTRIES)
CATALOG_SETUP_IDS_ORDERED: tuple[str, ...] = tuple(entry.setup_id for entry in CATALOG_ENTRIES)
CATALOG_BY_ID: dict[str, CatalogEntry] = {entry.setup_id: entry for entry in CATALOG_ENTRIES}


def catalog_setup_family(setup_id: str) -> str:
    entry = CATALOG_BY_ID.get(str(setup_id or "").strip())
    return entry.family if entry is not None else "misc"

# Whitelist for [bot.filters.setups] param keys (config.toml.example + delivery overrides).
CATALOG_SETUP_PARAM_KEYS: frozenset[str] = frozenset(
    {
        "adaptive_min_volume_ratio",
        "adx_penalty",
        "adx_penalty_factor",
        "altseason_long_threshold",
        "asia_end_hour_utc",
        "asia_start_hour_utc",
        "atr_mean_window",
        "base_score",
        "bb_pct_b_threshold",
        "bb_squeeze_threshold",
        "bias_mismatch_penalty",
        "body_reversal_close_position_long",
        "body_reversal_close_position_short",
        "bounce_threshold_atr",
        "breakout_atr_mult",
        "breakout_lookback_bars",
        "breakout_threshold",
        "breakout_threshold_atr",
        "btc_dominance_threshold",
        "closed_back_atr_mult",
        "confirmation_lookback_15m",
        "confirmation_lookback_bars",
        "delta_spike_mult",
        "divergence_window",
        "ema_acceptance_atr",
        "ema_deep_pullback_pct",
        "ema_proximity_pct",
        "ema_pullback_atr",
        "ema_pullback_score_penalty",
        "ema_reclaim_lookback_bars",
        "ema_touch_tolerance_pct",
        "enable_15m_range_fallback",
        "external_swing_lookback",
        "fallback_lookback_bars",
        "false_breakout_lookback_1h",
        "funding_extreme_threshold",
        "funding_recent_extreme_lookback_hours",
        "funding_soft_threshold",
        "funding_threshold",
        "historical_funding_score_penalty",
        "liquidation_extreme_threshold",
        "london_end_hour_utc",
        "london_start_hour_utc",
        "long_account_threshold",
        "max_acceptance_close_position_short",
        "max_adverse_depth_imbalance",
        "max_adverse_microprice_bias",
        "max_bb_width",
        "max_break_age_bars",
        "max_close_position_short",
        "max_confirmation_bars",
        "max_ema_extension_atr",
        "max_entry_distance_atr",
        "max_entry_drift_atr",
        "max_recovery_delta_short",
        "max_retest_age_bars",
        "max_retest_distance_atr",
        "max_rsi_long",
        "max_spread_bps",
        "max_sweep_age_bars",
        "max_swing_pair_gap",
        "max_vwap_distance_atr",
        "min_abs_flow_delta",
        "min_abs_oi_change_pct",
        "min_acceptance_close_position_long",
        "min_adx_1h",
        "min_atr_expansion_ratio",
        "min_bb_compression_width",
        "min_body_atr",
        "min_close_position_long",
        "min_confirmation_score",
        "min_delta_threshold",
        "min_depth_imbalance",
        "min_indicator_votes",
        "min_liquidation_score",
        "min_microprice_bias",
        "min_ob_impulse_atr",
        "min_price_change_pct",
        "min_price_delta_pct",
        "min_proxy_shift",
        "min_proxy_volume_ratio",
        "min_proxy_wick_atr",
        "min_recovery_delta_long",
        "min_release_roc10_abs_pct",
        "min_release_width_expansion",
        "min_roc10_abs_pct",
        "min_rr",
        "min_rsi_delta",
        "min_rsi_short",
        "min_shift",
        "min_swings",
        "min_trend_votes",
        "min_vol_breakout",
        "min_volume_ratio",
        "min_wick_atr",
        "mitigation_threshold",
        "near_level_atr",
        "near_range_atr",
        "no_crowd_confirmation_penalty",
        "ny_end_hour_utc",
        "ny_start_hour_utc",
        "ob_max_age",
        "orderflow_conflict_penalty",
        "overlap_end_hour_utc",
        "overlap_start_hour_utc",
        "proxy_lookback_bars",
        "pullback_rsi_long_max",
        "pullback_rsi_short_min",
        "reclaim_lookback_bars",
        "relative_funding_score_penalty",
        "release_lookback",
        "retest_atr_mult",
        "rsi_divergence_threshold",
        "scan_bars",
        "shift_std_mult",
        "short_account_threshold",
        "signal_lookback_bars",
        "sl_atr_mult",
        "sl_buffer_atr",
        "soft_long_account_threshold",
        "soft_short_account_threshold",
        "squeeze_memory_bars",
        "squeeze_release_lookback",
        "strict_1h_structure",
        "strong_wick_multiplier",
        "structure_conflict_penalty",
        "supertrend_opposes_penalty",
        "sweep_tolerance_pct",
        "swing_lookback",
        "threshold_adx",
        "threshold_gap",
        "threshold_tol",
        "touch_buffer_atr",
        "tp_too_close_penalty",
        "trend_conflict_penalty_factor",
        "volume_penalty",
        "volume_threshold",
        "vwap_reclaim_tolerance_pct",
        "wick_atr_threshold",
        "wick_through_atr_mult",
    }
)


def catalog_required_timeframes_for_enabled(settings: BotSettings) -> frozenset[str]:
    """Union of catalog required_tfs for currently enabled setups."""
    required: set[str] = set()
    for setup_id in settings.setups.enabled_setup_ids():
        entry = CATALOG_BY_ID.get(setup_id)
        if entry is not None:
            required.update(entry.required_tfs)
    return frozenset(required)


def verify_setup_config_model(setup_config_model: type[BaseModel]) -> list[str]:
    """Ensure SetupConfig bool fields stay aligned with the strategy catalog."""
    field_names = frozenset(setup_config_model.model_fields)
    missing = sorted(CATALOG_SETUP_IDS - field_names)
    extra = sorted(field_names - CATALOG_SETUP_IDS)
    errors: list[str] = []
    if missing:
        errors.append(f"SetupConfig missing catalog setup fields: {', '.join(missing)}")
    if extra:
        errors.append(f"SetupConfig has fields not in catalog: {', '.join(extra)}")
    return errors


def verify_config_setup_references(settings: BotSettings) -> list[str]:
    """Validate config setup overrides and enabled flags against the catalog."""
    errors: list[str] = []
    unknown_overrides = sorted(set(settings.filters.setups) - CATALOG_SETUP_IDS)
    if unknown_overrides:
        errors.append("filters.setups contains unknown setup ids: " + ", ".join(unknown_overrides))
    for setup_id, params in settings.filters.setups.items():
        unknown_keys = sorted(set(params) - CATALOG_SETUP_PARAM_KEYS)
        if unknown_keys:
            errors.append(
                f"filters.setups.{setup_id} unknown param keys: {', '.join(unknown_keys)}"
            )
    unknown_enabled = sorted(set(settings.setups.enabled_setup_ids()) - CATALOG_SETUP_IDS)
    if unknown_enabled:
        errors.append("enabled setups not in catalog: " + ", ".join(unknown_enabled))
    for symbol, asset in settings.assets.items():
        unknown_excluded = sorted(set(asset.excluded_strategies) - CATALOG_SETUP_IDS)
        if unknown_excluded:
            errors.append(
                f"assets.{symbol}.excluded_strategies unknown setup ids: "
                + ", ".join(unknown_excluded)
            )
        unknown_allowed = sorted(set(asset.allowed_strategies) - CATALOG_SETUP_IDS)
        if unknown_allowed:
            errors.append(
                f"assets.{symbol}.allowed_strategies unknown setup ids: "
                + ", ".join(unknown_allowed)
            )
    available = frozenset(settings.ws.kline_intervals) | frozenset(
        settings.runtime.analysis_kline_intervals
    )
    rest_only_tfs = frozenset({"4h", "1d"})
    missing_tfs = sorted(
        catalog_required_timeframes_for_enabled(settings) - available - rest_only_tfs
    )
    if missing_tfs:
        errors.append(
            "catalog required timeframes not covered by ws.kline_intervals | "
            f"runtime.analysis_kline_intervals: {', '.join(missing_tfs)}"
        )
    return errors


PR10_WAVES: dict[int, frozenset[str]] = {
    wave: frozenset(entry.setup_id for entry in CATALOG_ENTRIES if entry.wave == wave)
    for wave in range(1, 6)
}


def catalog_default_params(setup_id: str) -> dict[str, float | str]:
    entry = CATALOG_BY_ID.get(setup_id)
    if entry is None:
        return {}
    return {
        "base_score": entry.base_score,
        "min_rr": entry.min_rr,
        "min_volume_ratio": entry.min_volume_ratio,
        "min_adx_1h": entry.min_adx_1h,
        "confirmation_profile": entry.confirmation_profile,
        "sl_buffer_atr": 0.85,
    }


def catalog_timeframe_profile(setup_id: str) -> dict[str, object]:
    """Return trigger/pattern/required TF profile for a catalog setup."""
    entry = CATALOG_BY_ID.get(setup_id)
    if entry is None:
        return {}
    return {
        "trigger_tf": entry.trigger_tf,
        "trigger_intervals": entry.trigger_intervals,
        "pattern_tf": entry.pattern_tf,
        "required_tfs": entry.required_tfs,
    }


def intervals_for_catalog_entry(entry: CatalogEntry) -> tuple[str, ...]:
    """Union of trigger, alternate triggers, and required frames for WS/scheduling."""
    return tuple(
        dict.fromkeys(
            (
                entry.trigger_tf,
                *entry.trigger_intervals,
                *entry.required_tfs,
            )
        )
    )


def verify_strategy_wiring(strategy_classes: Sequence[type[Any]]) -> list[str]:
    errors: list[str] = []
    if len(strategy_classes) != len(CATALOG_ENTRIES):
        errors.append(
            f"STRATEGY_CLASSES count {len(strategy_classes)} != catalog {len(CATALOG_ENTRIES)}"
        )
    seen: set[str] = set()
    for cls in strategy_classes:
        setup_id = getattr(cls, "setup_id", None)
        if not setup_id or not isinstance(setup_id, str):
            errors.append(f"{cls.__name__}: missing setup_id")
            continue
        if setup_id in seen:
            errors.append(f"duplicate setup_id: {setup_id}")
        seen.add(setup_id)
        if setup_id not in CATALOG_SETUP_IDS:
            errors.append(f"{cls.__name__}: setup_id {setup_id!r} not in STRATEGY_CATALOG")
        if not hasattr(cls, "detect"):
            errors.append(f"{cls.__name__}: missing detect()")
        if not getattr(cls, "family", None):
            errors.append(f"{setup_id}: missing family")
        if not getattr(cls, "confirmation_profile", None):
            errors.append(f"{setup_id}: missing confirmation_profile")
        entry = CATALOG_BY_ID.get(setup_id)
        if entry is not None:
            if not entry.pattern_tf:
                errors.append(f"{setup_id}: catalog missing pattern_tf")
            if not entry.required_tfs:
                errors.append(f"{setup_id}: catalog missing required_tfs")
    missing = sorted(CATALOG_SETUP_IDS - seen)
    extra = sorted(seen - CATALOG_SETUP_IDS)
    if missing:
        errors.append(f"catalog strategies not registered: {', '.join(missing)}")
    if extra:
        errors.append(f"unknown strategies registered: {', '.join(extra)}")
    return errors


def wave_status(strategy_classes: Sequence[type[Any]]) -> dict[int, bool]:
    registered = {getattr(c, "setup_id", "") for c in strategy_classes}
    return {wave: ids.issubset(registered) for wave, ids in PR10_WAVES.items()}
