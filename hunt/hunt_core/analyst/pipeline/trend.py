from __future__ import annotations

from typing import Any

from hunt_core.analyst.pipeline._helpers import safe_float
from hunt_core.analyst.pipeline.config import TrendConfig
from hunt_core.analyst.pipeline.types import ModuleResult


def _resolve_tf_key(row: dict[str, Any]) -> str:
    tf = row.get("timeframes") if isinstance(row.get("timeframes"), dict) else {}
    for key in ("4h_closed", "4h", "1h_closed", "1h"):
        block = tf.get(key)
        if isinstance(block, dict) and block.get("status") != "empty":
            close = safe_float(block.get("close"))
            if close > 0:
                return key
    return "4h"


def _resolve_snap(row: dict[str, Any], tf_key: str) -> dict[str, Any]:
    tf = row.get("timeframes") if isinstance(row.get("timeframes"), dict) else {}
    return tf.get(tf_key) or {}


def run_trend_module(row: dict[str, Any], cfg: TrendConfig, direction: str = "long") -> ModuleResult:
    tf_key = _resolve_tf_key(row)
    snap = _resolve_snap(row, tf_key)

    ker = safe_float(snap.get("ker_10"))
    ema_slope = safe_float(snap.get("ema50_slope_5"))

    evidence: list[str] = []
    if ker != 0.0:
        evidence.append(f"ker={ker:.3f}")
    if ema_slope != 0.0:
        evidence.append(f"ema_slope={ema_slope:.4f}")

    has_ker = "ker_10" in snap and ker != 0.0
    has_ema_slope = "ema50_slope_5" in snap
    if not has_ker and not has_ema_slope:
        return ModuleResult(
            status="UNKNOWN",
            reason="Нет данных тренда (KER/EMA)",
            details={"tf_key": tf_key, "evidence": evidence},
        )

    if ker < cfg.ker_max_caution:
        return ModuleResult(
            status="CAUTION",
            reason=f"KER={ker:.3f} < {cfg.ker_max_caution} — флэт/mean-reversion",
            details={"ker": ker, "ema_slope": ema_slope, "evidence": evidence},
        )

    if direction == "long":
        if ker > cfg.ker_min_trend and ema_slope > 0:
            return ModuleResult(
                status="PASS",
                reason=f"Тренд вверх: KER={ker:.3f}, EMA50 slope=+{ema_slope:.4f}",
                details={"ker": ker, "ema_slope": ema_slope, "evidence": evidence, "direction": "long"},
            )
        if ker > cfg.ker_min_trend and ema_slope == 0:
            return ModuleResult(
                status="CAUTION",
                reason=f"KER={ker:.3f} но EMA50 slope=0 — флэт (sizing ↓{cfg.slope_flat_reduce_pct*100:.0f}%)",
                details={"ker": ker, "ema_slope": ema_slope, "evidence": evidence, "direction": "long", "sizing_reduce": cfg.slope_flat_reduce_pct},
            )
        if ker > cfg.ker_min_trend and ema_slope < 0:
            return ModuleResult(
                status="FAIL",
                reason=f"KER={ker:.3f} но EMA50 slope={ema_slope:.4f} < 0 — не лонг",
                details={"ker": ker, "ema_slope": ema_slope, "evidence": evidence, "direction": "long"},
            )
        if ker <= cfg.ker_min_trend and ema_slope > 0:
            return ModuleResult(
                status="CAUTION",
                reason=f"EMA50↑ но KER={ker:.3f} ≤ {cfg.ker_min_trend}",
                details={"ker": ker, "ema_slope": ema_slope, "evidence": evidence, "direction": "long"},
            )
        return ModuleResult(
            status="FAIL",
            reason=f"Тренд не подтверждён: KER={ker:.3f}, EMA slope={ema_slope:.4f}",
            details={"ker": ker, "ema_slope": ema_slope, "evidence": evidence, "direction": "long"},
        )

    if ker > cfg.ker_min_trend and ema_slope < 0:
        return ModuleResult(
            status="PASS",
            reason=f"Тренд вниз: KER={ker:.3f}, EMA50 slope={ema_slope:.4f}",
            details={"ker": ker, "ema_slope": ema_slope, "evidence": evidence, "direction": "short"},
        )
    if ker > cfg.ker_min_trend and ema_slope == 0:
        return ModuleResult(
            status="CAUTION",
            reason=f"KER={ker:.3f} но EMA50 slope=0 — флэт (sizing ↓{cfg.slope_flat_reduce_pct*100:.0f}%)",
            details={"ker": ker, "ema_slope": ema_slope, "evidence": evidence, "direction": "short", "sizing_reduce": cfg.slope_flat_reduce_pct},
        )
    if ker > cfg.ker_min_trend and ema_slope > 0:
        return ModuleResult(
            status="FAIL",
            reason=f"KER={ker:.3f} но EMA50 slope={ema_slope:.4f} > 0 — не шорт",
            details={"ker": ker, "ema_slope": ema_slope, "evidence": evidence, "direction": "short"},
        )
    if ker <= cfg.ker_min_trend and ema_slope < 0:
        return ModuleResult(
            status="CAUTION",
            reason=f"EMA50↓ но KER={ker:.3f} ≤ {cfg.ker_min_trend}",
            details={"ker": ker, "ema_slope": ema_slope, "evidence": evidence, "direction": "short"},
        )
    return ModuleResult(
        status="FAIL",
        reason=f"Тренд не подтверждён: KER={ker:.3f}, EMA slope={ema_slope:.4f}",
        details={"ker": ker, "ema_slope": ema_slope, "evidence": evidence, "direction": "short"},
    )
