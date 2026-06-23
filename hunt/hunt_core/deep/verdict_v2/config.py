"""Verdict V2 configuration."""
from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hunt_core.paths import VERDICT_V2_CALIBRATION_JSON, VERDICT_V2_GATE_OVERRIDES_JSON


@dataclass
class SignalGates:
    strength_min: float = 0.50
    fragility_max: float = 0.65
    trade_quality_min: float = 0.45
    rr_primary_min: float = 0.75
    data_coverage_min: float = 0.50
    require_timing_c: bool = True


@dataclass
class TradePlanConfig:
    entry_atr_pad: float = 0.25
    stop_atr_fallback: float = 1.5
    min_rr_tp1: float = 1.0


@dataclass
class VerdictV2Config:
    enabled: bool = True
    horizon_primary: str = "B"
    pattern_ambiguity_spread: float = 0.08
    path_ambiguity_spread: float = 0.15
    fragility_high_threshold: float = 0.65
    disagreement_high_threshold: float = 0.65
    trade_rr_favorable: float = 1.2
    trade_rr_poor: float = 0.8
    tg_verbose: bool = False
    signal_queue_enabled: bool = True
    signal_queue_top_n: int = 3
    signal_queue_tg_footer: bool = True
    signal_queue_tg_batch: bool = True
    signal_queue_tg_min_rank: int = 2
    signal_queue_ttl_hours: float = 2.5
    gates: SignalGates = field(default_factory=SignalGates)
    trade_plan: TradePlanConfig = field(default_factory=TradePlanConfig)
    priorities_a: dict[str, float] = field(
        default_factory=lambda: {
            "macro_trend": 0.26,
            "structural": 0.22,
            "positioning": 0.20,
            "derivatives": 0.14,
            "flow": 0.10,
            "execution_pressure": 0.04,
            "cross_consensus": 0.04,
        }
    )
    priorities_b: dict[str, float] = field(
        default_factory=lambda: {
            "macro_trend": 0.16,
            "structural": 0.16,
            "positioning": 0.20,
            "derivatives": 0.14,
            "flow": 0.14,
            "execution_pressure": 0.08,
            "cross_consensus": 0.08,
        }
    )
    priorities_c: dict[str, float] = field(
        default_factory=lambda: {
            "macro_trend": 0.06,
            "structural": 0.08,
            "positioning": 0.18,
            "derivatives": 0.10,
            "flow": 0.20,
            "execution_pressure": 0.16,
            "cross_consensus": 0.14,
        }
    )


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _defaults_path() -> Path:
    return Path(__file__).resolve().parents[3] / "config.defaults.toml"


def _load_toml_section(dotted: str) -> dict[str, Any]:
    path = _defaults_path()
    if not path.is_file():
        return {}
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    deep = raw.get("deep") if isinstance(raw.get("deep"), dict) else {}
    node: Any = deep
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return {}
        node = node.get(part, {})
    return dict(node) if isinstance(node, dict) else {}


def _float_map(block: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for k, v in block.items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def _load_json_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _apply_runtime_gate_tune(cfg: VerdictV2Config, root: dict[str, Any]) -> VerdictV2Config:
    _ = root
    if os.getenv("HUNT_V2_STRENGTH_MIN"):
        return cfg
    overrides = _load_json_file(VERDICT_V2_GATE_OVERRIDES_JSON)
    if overrides.get("strength_min") is not None:
        cfg.gates.strength_min = float(overrides["strength_min"])
    return cfg


_KNOWN_VERDICT_V2_ROOT_KEYS = frozenset(
    {
        "enabled",
        "horizon_primary",
        "pattern_ambiguity_spread",
        "fragility_high_threshold",
        "disagreement_high_threshold",
        "trade_rr_favorable",
        "trade_rr_poor",
        "tg_verbose",
        "signal_queue_enabled",
        "signal_queue_top_n",
        "signal_queue_tg_footer",
        "signal_queue_tg_batch",
        "signal_queue_tg_min_rank",
        "signal_queue_ttl_hours",
    }
)


def _reject_unknown_verdict_v2_keys(section: dict[str, Any], *, allowed: frozenset[str], label: str) -> None:
    unknown = sorted(
        k for k, v in section.items() if k not in allowed and not isinstance(v, dict)
    )
    if unknown:
        raise ValueError(f"Unknown {label} keys (fail-closed): {', '.join(unknown)}")


def load_verdict_v2_config() -> VerdictV2Config:
    root = _load_toml_section("verdict_v2")
    _reject_unknown_verdict_v2_keys(root, allowed=_KNOWN_VERDICT_V2_ROOT_KEYS, label="verdict_v2")
    gates_t = _load_toml_section("verdict_v2.signal_gates")
    tp_t = _load_toml_section("verdict_v2.trade_plan")
    pa = _float_map(_load_toml_section("verdict_v2.priorities_a"))
    pb = _float_map(_load_toml_section("verdict_v2.priorities_b"))
    pc = _float_map(_load_toml_section("verdict_v2.priorities_c"))

    gates = SignalGates(
        strength_min=float(os.getenv("HUNT_V2_STRENGTH_MIN", gates_t.get("strength_min", 0.50)) or 0.50),
        fragility_max=float(os.getenv("HUNT_V2_FRAGILITY_MAX", gates_t.get("fragility_max", 0.65)) or 0.65),
        trade_quality_min=float(
            os.getenv("HUNT_V2_TRADE_QUALITY_MIN", gates_t.get("trade_quality_min", 0.45)) or 0.45
        ),
        rr_primary_min=float(os.getenv("HUNT_V2_RR_PRIMARY_MIN", gates_t.get("rr_primary_min", 0.75)) or 0.75),
        data_coverage_min=float(
            os.getenv("HUNT_V2_DATA_COVERAGE_MIN", gates_t.get("data_coverage_min", 0.50)) or 0.50
        ),
        require_timing_c=bool(gates_t.get("require_timing_c", True)),
    )
    tp = TradePlanConfig(
        entry_atr_pad=float(os.getenv("HUNT_V2_ENTRY_ATR_PAD", tp_t.get("entry_atr_pad", 0.25)) or 0.25),
        stop_atr_fallback=float(
            os.getenv("HUNT_V2_STOP_ATR_FALLBACK", tp_t.get("stop_atr_fallback", 1.5)) or 1.5
        ),
        min_rr_tp1=float(tp_t.get("min_rr_tp1", 1.0) or 1.0),
    )
    verbose_env = os.getenv("HUNT_DEEP_TG_VERBOSE", "").strip().lower()
    verbose = verbose_env in {"1", "true", "yes"} if verbose_env else bool(root.get("tg_verbose", False))
    cfg = VerdictV2Config(
        enabled=bool(root.get("enabled", True)),
        horizon_primary=str(root.get("horizon_primary", "B") or "B"),
        pattern_ambiguity_spread=float(root.get("pattern_ambiguity_spread", 0.08) or 0.08),
        fragility_high_threshold=float(root.get("fragility_high_threshold", 0.65) or 0.65),
        disagreement_high_threshold=float(root.get("disagreement_high_threshold", 0.65) or 0.65),
        trade_rr_favorable=float(root.get("trade_rr_favorable", 1.2) or 1.2),
        trade_rr_poor=float(root.get("trade_rr_poor", 0.8) or 0.8),
        tg_verbose=verbose,
        signal_queue_enabled=bool(root.get("signal_queue_enabled", True)),
        signal_queue_top_n=int(root.get("signal_queue_top_n", 3) or 3),
        signal_queue_tg_footer=bool(root.get("signal_queue_tg_footer", True)),
        signal_queue_tg_batch=_env_bool(
            "HUNT_SIGNAL_QUEUE_TG_BATCH",
            bool(root.get("signal_queue_tg_batch", True)),
        ),
        signal_queue_tg_min_rank=int(
            os.getenv("HUNT_SIGNAL_QUEUE_TG_MIN_RANK", root.get("signal_queue_tg_min_rank", 2)) or 2
        ),
        signal_queue_ttl_hours=float(root.get("signal_queue_ttl_hours", 2.5) or 2.5),
        gates=gates,
        trade_plan=tp,
    )
    if pa:
        cfg.priorities_a = pa
    if pb:
        cfg.priorities_b = pb
    if pc:
        cfg.priorities_c = pc
    return _apply_runtime_gate_tune(cfg, root)
