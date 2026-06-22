"""Expansion Engine configuration.

Loads ``[hunt.expansion]`` from ``config.defaults.toml`` (and optional repo-root
``config.toml`` overlay). Env vars ``HUNT_EXPANSION_*`` override TOML. When
``apply_calibration`` is true and ``data/expansion_calibration.json`` has enough
samples, learned block multipliers are applied to the weight tables at load time.
"""
from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from hunt_core.paths import EXPANSION_CALIBRATION_JSON, ROOT


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _env_flag(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _defaults_path() -> Path:
    return ROOT / "config.defaults.toml"


def _repo_config_path() -> Path:
    return ROOT.parent / "config.toml"


def _load_toml_section() -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for path in (_defaults_path(), _repo_config_path()):
        if not path.is_file():
            continue
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        hunt = raw.get("hunt") if isinstance(raw.get("hunt"), dict) else {}
        section = hunt.get("expansion") if isinstance(hunt.get("expansion"), dict) else {}
        if section:
            merged.update(section)
    return merged


def _float_map(block: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for k, v in block.items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


# Up (pre-pump) evidence weights — used by the probability model.
_DEFAULT_UP_WEIGHTS: dict[str, float] = {
    "compression": 0.10,
    "absorption": 0.10,
    "supply_exhaustion": 0.12,
    "fuel_imbalance": 0.12,
    "liquidity": 0.08,
    "structure": 0.06,
    "strength": 0.06,
    "market_maker_trap": 0.08,
    "liquidity_sweep": 0.05,
    "liquidity_vacuum": 0.08,
    "short_squeeze_potential": 0.09,
    "wyckoff_spring": 0.06,
}

# Down (pre-dump) evidence weights.
_DEFAULT_DOWN_WEIGHTS: dict[str, float] = {
    "funding": 0.12,
    "distribution_quality": 0.18,
    "fuel_imbalance": 0.08,
    "liquidity": 0.08,
    "structure": 0.08,
    "strength": 0.06,
    "long_squeeze_potential": 0.10,
    "oi_concentration": 0.08,
    "breakout_failure": 0.12,
    "wyckoff_upthrust": 0.06,
    "liquidity_sweep": 0.04,
}

# Trigger-proximity weights (close to launch, not general readiness).
_DEFAULT_TRIGGER_WEIGHTS: dict[str, float] = {
    "activation_distance": 0.35,
    "delta_momentum": 0.25,
    "compression": 0.15,
    "fuel_imbalance": 0.15,
    "liquidity_vacuum": 0.10,
}

# Expansion-quality weights (setup quality, separate from direction).
_DEFAULT_QUALITY_WEIGHTS: dict[str, float] = {
    "compression": 0.12,
    "fuel_imbalance": 0.12,
    "liquidity": 0.10,
    "supply_exhaustion": 0.10,
    "fractal_alignment": 0.14,
    "state_persistence": 0.12,
    "cycle_context": 0.08,
    "market_maker_trap": 0.10,
    "liquidity_sweep": 0.06,
    "liquidity_vacuum": 0.06,
}


def _apply_weight_multipliers(
    weights: dict[str, float],
    multipliers: dict[str, float],
) -> dict[str, float]:
    if not multipliers:
        return dict(weights)
    out = dict(weights)
    for name, mult in multipliers.items():
        if name in out:
            out[name] = round(out[name] * float(mult), 6)
    return out


def load_calibration_multipliers() -> dict[str, float]:
    """Read persisted block multipliers from the outcome-learning rollup."""
    if not EXPANSION_CALIBRATION_JSON.is_file():
        return {}
    try:
        raw = json.loads(EXPANSION_CALIBRATION_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict) or raw.get("status") != "ok":
        return {}
    mults = raw.get("multipliers")
    if not isinstance(mults, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in mults.items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


@dataclass(frozen=True)
class ExpansionConfig:
    enabled: bool = True
    # Delta / persistence
    delta_lookback_bars: int = 120
    persistence_threshold: float = 0.80
    # Level thresholds
    forecast_min_quality: float = 0.55
    execution_min_trigger: float = 0.50
    fake_breakout_block: float = 0.55  # suppress execution above this risk
    # State derivation
    state_strong_prob: float = 0.62
    state_dominance_ratio: float = 1.20
    state_pivot_prob: float = 0.45
    # Scan
    scan_top_n: int = 50
    # Runtime
    watch_stamp: bool = False
    operator_commands: bool = False
    lab_runtime: bool = False
    watch_stamp_tiers: frozenset[str] = frozenset({"full", "fast"})
    review_loop: bool = True
    review_interval_s: float = 3600.0
    apply_calibration: bool = True
    history_persist: bool = True
    history_persist_samples: int = 40
    history_persist_max_symbols: int = 64
    runtime_save_interval_s: float = 300.0
    # Pinned TG alerts (separate from Verdict deep change messages)
    tg_pinned_alerts: bool = True
    tg_on_change: bool = True
    tg_min_quality: float = 0.45
    tg_min_trigger: float = 0.50
    tg_stale_hours: float = 4.0
    tg_cooldown_min: int = 45
    # Universe scan TG (watch rows — excludes pinned, batched digest)
    tg_universe_scan: bool = True
    tg_universe_interval_s: float = 900.0
    tg_universe_top_n: int = 5
    tg_universe_min_opp: float = 0.35
    # Weight tables
    up_weights: dict[str, float] = field(default_factory=lambda: dict(_DEFAULT_UP_WEIGHTS))
    down_weights: dict[str, float] = field(default_factory=lambda: dict(_DEFAULT_DOWN_WEIGHTS))
    trigger_weights: dict[str, float] = field(default_factory=lambda: dict(_DEFAULT_TRIGGER_WEIGHTS))
    quality_weights: dict[str, float] = field(default_factory=lambda: dict(_DEFAULT_QUALITY_WEIGHTS))


def invalidate_expansion_config_cache() -> None:
    load_expansion_config.cache_clear()


@lru_cache(maxsize=1)
def load_expansion_config() -> ExpansionConfig:
    toml = _load_toml_section()
    up = _float_map(toml.get("up_weights") or {}) if isinstance(toml.get("up_weights"), dict) else {}
    down = _float_map(toml.get("down_weights") or {}) if isinstance(toml.get("down_weights"), dict) else {}
    trigger = (
        _float_map(toml.get("trigger_weights") or {})
        if isinstance(toml.get("trigger_weights"), dict)
        else {}
    )
    quality = (
        _float_map(toml.get("quality_weights") or {})
        if isinstance(toml.get("quality_weights"), dict)
        else {}
    )

    apply_cal = _env_flag("HUNT_EXPANSION_APPLY_CALIBRATION", bool(toml.get("apply_calibration", True)))

    up_w = {**_DEFAULT_UP_WEIGHTS, **up} if up else dict(_DEFAULT_UP_WEIGHTS)
    down_w = {**_DEFAULT_DOWN_WEIGHTS, **down} if down else dict(_DEFAULT_DOWN_WEIGHTS)
    trigger_w = {**_DEFAULT_TRIGGER_WEIGHTS, **trigger} if trigger else dict(_DEFAULT_TRIGGER_WEIGHTS)
    quality_w = {**_DEFAULT_QUALITY_WEIGHTS, **quality} if quality else dict(_DEFAULT_QUALITY_WEIGHTS)
    if apply_cal:
        mults = load_calibration_multipliers()
        if mults:
            up_w = _apply_weight_multipliers(up_w, mults)
            down_w = _apply_weight_multipliers(down_w, mults)
            trigger_w = _apply_weight_multipliers(trigger_w, mults)
            quality_w = _apply_weight_multipliers(quality_w, mults)

    return ExpansionConfig(
        enabled=_env_flag("HUNT_EXPANSION_ENABLED", bool(toml.get("enabled", True))),
        delta_lookback_bars=_env_int(
            "HUNT_EXPANSION_DELTA_LOOKBACK",
            int(toml.get("delta_lookback_bars", 120) or 120),
        ),
        persistence_threshold=_env_float(
            "HUNT_EXPANSION_PERSISTENCE_THRESHOLD",
            float(toml.get("persistence_threshold", 0.80) or 0.80),
        ),
        forecast_min_quality=_env_float(
            "HUNT_EXPANSION_FORECAST_MIN_QUALITY",
            float(toml.get("forecast_min_quality", 0.55) or 0.55),
        ),
        execution_min_trigger=_env_float(
            "HUNT_EXPANSION_EXECUTION_MIN_TRIGGER",
            float(toml.get("execution_min_trigger", 0.50) or 0.50),
        ),
        fake_breakout_block=_env_float(
            "HUNT_EXPANSION_FAKE_BLOCK",
            float(toml.get("fake_breakout_block", 0.55) or 0.55),
        ),
        state_strong_prob=_env_float(
            "HUNT_EXPANSION_STATE_STRONG_PROB",
            float(toml.get("state_strong_prob", 0.62) or 0.62),
        ),
        state_dominance_ratio=_env_float(
            "HUNT_EXPANSION_STATE_DOMINANCE_RATIO",
            float(toml.get("state_dominance_ratio", 1.20) or 1.20),
        ),
        state_pivot_prob=_env_float(
            "HUNT_EXPANSION_STATE_PIVOT_PROB",
            float(toml.get("state_pivot_prob", 0.45) or 0.45),
        ),
        scan_top_n=_env_int("HUNT_EXPANSION_SCAN_TOP_N", int(toml.get("scan_top_n", 50) or 50)),
        watch_stamp=_env_flag("HUNT_EXPANSION_WATCH_STAMP", bool(toml.get("watch_stamp", False))),
        operator_commands=_env_flag(
            "HUNT_EXPANSION_OPERATOR",
            bool(toml.get("operator_commands", False)),
        ),
        lab_runtime=_env_flag("HUNT_EXPANSION_LAB", bool(toml.get("lab_runtime", False))),
        review_loop=_env_flag("HUNT_EXPANSION_REVIEW_LOOP", bool(toml.get("review_loop", True))),
        review_interval_s=_env_float(
            "HUNT_EXPANSION_REVIEW_INTERVAL",
            float(toml.get("review_interval_s", 3600) or 3600),
        ),
        apply_calibration=apply_cal,
        history_persist=_env_flag(
            "HUNT_EXPANSION_HISTORY_PERSIST",
            bool(toml.get("history_persist", True)),
        ),
        history_persist_samples=_env_int(
            "HUNT_EXPANSION_HISTORY_SAMPLES",
            int(toml.get("history_persist_samples", 40) or 40),
        ),
        history_persist_max_symbols=_env_int(
            "HUNT_EXPANSION_HISTORY_MAX_SYMBOLS",
            int(toml.get("history_persist_max_symbols", 64) or 64),
        ),
        runtime_save_interval_s=_env_float(
            "HUNT_EXPANSION_RUNTIME_SAVE_INTERVAL",
            float(toml.get("runtime_save_interval_s", 300) or 300),
        ),
        tg_pinned_alerts=_env_flag(
            "HUNT_EXPANSION_TG_PINNED",
            bool(toml.get("tg_pinned_alerts", True)),
        ),
        tg_on_change=_env_flag(
            "HUNT_EXPANSION_TG_ON_CHANGE",
            bool(toml.get("tg_on_change", True)),
        ),
        tg_min_quality=_env_float(
            "HUNT_EXPANSION_TG_MIN_QUALITY",
            float(toml.get("tg_min_quality", 0.45) or 0.45),
        ),
        tg_min_trigger=_env_float(
            "HUNT_EXPANSION_TG_MIN_TRIGGER",
            float(toml.get("tg_min_trigger", 0.50) or 0.50),
        ),
        tg_stale_hours=_env_float(
            "HUNT_EXPANSION_TG_STALE_HOURS",
            float(toml.get("tg_stale_hours", 4.0) or 4.0),
        ),
        tg_cooldown_min=_env_int(
            "HUNT_EXPANSION_TG_COOLDOWN_MIN",
            int(toml.get("tg_cooldown_min", 45) or 45),
        ),
        tg_universe_scan=_env_flag(
            "HUNT_EXPANSION_TG_UNIVERSE",
            bool(toml.get("tg_universe_scan", True)),
        ),
        tg_universe_interval_s=_env_float(
            "HUNT_EXPANSION_UNIVERSE_INTERVAL",
            float(toml.get("tg_universe_interval_s", 900) or 900),
        ),
        tg_universe_top_n=_env_int(
            "HUNT_EXPANSION_UNIVERSE_TOP_N",
            int(toml.get("tg_universe_top_n", 5) or 5),
        ),
        tg_universe_min_opp=_env_float(
            "HUNT_EXPANSION_UNIVERSE_MIN_OPP",
            float(toml.get("tg_universe_min_opp", 0.35) or 0.35),
        ),
        up_weights=up_w,
        down_weights=down_w,
        trigger_weights=trigger_w,
        quality_weights=quality_w,
    )


__all__ = [
    "EXPANSION_CALIBRATION_JSON",
    "ExpansionConfig",
    "invalidate_expansion_config_cache",
    "load_calibration_multipliers",
    "load_expansion_config",
]
