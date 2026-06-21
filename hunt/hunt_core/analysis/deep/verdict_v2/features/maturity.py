"""Maturity features — read-only for patterns/path (R5)."""
from __future__ import annotations

from typing import Any

from hunt_core.analysis.deep.verdict_v2._helpers import clamp01, safe_float
from hunt_core.analysis.deep.verdict_v2.types import MaturityFeatures


def extract_maturity(row: dict[str, Any]) -> MaturityFeatures:
    tf = row.get("timeframes") if isinstance(row.get("timeframes"), dict) else {}
    snap = tf.get("4h") or tf.get("1d") or {}
    trend_age = safe_float(snap.get("trend_age"))
    bars_cross = safe_float(snap.get("bars_since_cross"))
    ema_sep = safe_float(snap.get("ema_separation_pct"))
    evidence: list[str] = []
    if trend_age > 0:
        evidence.append(f"trend_age={trend_age:.0f}")
    if bars_cross > 0:
        evidence.append(f"bars_cross={bars_cross:.0f}")
    if ema_sep != 0:
        evidence.append(f"ema_sep={ema_sep:.2f}%")
    maturity = clamp01(trend_age / 40.0 + bars_cross / 30.0 + abs(ema_sep) / 5.0)
    return MaturityFeatures(
        maturity_score=round(maturity, 3),
        trend_age=trend_age,
        bars_since_cross=bars_cross,
        ema_separation_pct=ema_sep,
        evidence=evidence,
    )
