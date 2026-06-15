"""Load hunt/config.defaults.toml as universal threshold defaults (P1.14).

Merged under hunt_calibration.json overrides in param_store.universal_section().
"""
from __future__ import annotations



import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

_DEFAULTS_PATH = Path(__file__).resolve().parents[1] / "config.defaults.toml"


@lru_cache(maxsize=1)
def load_config_defaults_toml() -> dict[str, Any]:
    """Parse config.defaults.toml into param_store universal section keys."""
    if not _DEFAULTS_PATH.exists():
        return {}
    try:
        raw = tomllib.loads(_DEFAULTS_PATH.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}

    out: dict[str, Any] = {}
    scanner = raw.get("scanner")
    if isinstance(scanner, dict):
        out["scanner"] = {
            k: v
            for k, v in {
                "hot_range_pct": scanner.get("range_hot_pct"),
                "pump_extreme_pct": scanner.get("pump_extreme_pct"),
                "pos_near_high": scanner.get("pos_near_high"),
                "pos_near_low": scanner.get("pos_near_low"),
            }.items()
            if v is not None
        }

    confirm_root = raw.get("confirm") if isinstance(raw.get("confirm"), dict) else None
    if isinstance(confirm_root, dict):
        confirm_cfg: dict[str, Any] = {}
        for tf_key in ("entry_confirm_tf", "entry_confirm_tf_dump", "entry_confirm_tf_long"):
            tf_val = confirm_root.get(tf_key)
            if isinstance(tf_val, str) and tf_val.strip():
                confirm_cfg[tf_key] = tf_val.strip().lower()
        fast = confirm_root.get("dump_fast_confirm")
        if isinstance(fast, bool):
            confirm_cfg["dump_fast_confirm"] = fast
        if confirm_cfg:
            out["confirm"] = {**out.get("confirm", {}), **confirm_cfg}

    confirm_short = confirm_root.get("short") if isinstance(confirm_root, dict) else None
    if isinstance(confirm_short, dict):
        gates: dict[str, Any] = {}
        if confirm_short.get("min_score") is not None:
            gates["confirm_min_score"] = confirm_short["min_score"]
        if confirm_short.get("min_score_without_div") is not None:
            gates["confirm_min_score_no_div"] = confirm_short["min_score_without_div"]
        if confirm_short.get("forming_min_score") is not None:
            gates["forming_min_score"] = confirm_short["forming_min_score"]
        if gates:
            out["gates"] = gates

    levels = raw.get("levels", {}).get("adaptive") if isinstance(raw.get("levels"), dict) else None
    if isinstance(levels, dict):
        out["levels"] = {
            k: v
            for k, v in {
                "sl_max_pct_normal": levels.get("sl_max_pct_normal"),
                "sl_max_pct_hot": levels.get("sl_max_pct_hot"),
                "sl_max_pct_parabolic": levels.get("sl_max_pct_parabolic"),
                "hot_range_pct": levels.get("hot_range_pct"),
                "parabolic_range_pct": levels.get("parabolic_range_pct"),
                "parabolic_leg_gain_pct": levels.get("parabolic_leg_gain_pct"),
            }.items()
            if v is not None
        }

    premature = raw.get("gate", {}).get("premature_exhaustion_short") if isinstance(raw.get("gate"), dict) else None
    if isinstance(premature, dict):
        out["lifecycle"] = {
            k: v
            for k, v in {
                "premature_exhaustion_pos": premature.get("pos_min"),
                "premature_exhaustion_bounce_pct": premature.get("bounce_from_low_pct"),
                "premature_exhaustion_pos_tight": premature.get("pos_min_tight"),
                "premature_exhaustion_bounce_tight_pct": premature.get("bounce_from_low_pct_tight"),
            }.items()
            if v is not None
        }

    lifecycle_sq = raw.get("lifecycle", {}).get("squeeze") if isinstance(raw.get("lifecycle"), dict) else None
    if isinstance(lifecycle_sq, dict):
        lc_block = dict(out.get("lifecycle") or {})
        for k, v in {
            "squeeze_bb_width_pctile_max": lifecycle_sq.get("bb_width_pctile_max"),
            "squeeze_donchian_width_pct_max": lifecycle_sq.get("donchian_width_pct_max"),
            "rsi_exhaustion_enter": lifecycle_sq.get("rsi_exhaustion_enter"),
            "rsi_exhaustion_exit": lifecycle_sq.get("rsi_exhaustion_exit"),
            "taker_buy_min": lifecycle_sq.get("taker_buy_min"),
            "taker_sell_max": lifecycle_sq.get("taker_sell_max"),
            "cascade_wick_ratio_min": lifecycle_sq.get("cascade_wick_ratio_min"),
        }.items():
            if v is not None:
                lc_block[k] = v
        if lc_block:
            out["lifecycle"] = lc_block

    collect = raw.get("collect")
    if isinstance(collect, dict):
        out["collect"] = {k: v for k, v in collect.items() if v is not None}

    scoring = raw.get("scoring")
    if isinstance(scoring, dict):
        out["scoring"] = {k: v for k, v in scoring.items() if v is not None}

    tracker = raw.get("tracker")
    if isinstance(tracker, dict):
        out["tracker"] = {
            k: v for k, v in tracker.items() if v is not None
        }

    return out


def universal_section_from_defaults(section: str) -> dict[str, Any]:
    block = load_config_defaults_toml().get(section)
    return dict(block) if isinstance(block, dict) else {}
