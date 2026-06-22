"""Timing/trigger gates — 15m/5m confirm horizon C only (R14)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hunt_core.analysis.trend_engine import trend_from_snapshot
from hunt_core.deep.verdict_v2._helpers import direction_bias, safe_float
from hunt_core.deep.verdict_v2.types import HorizonForecast


@dataclass(frozen=True, slots=True)
class TimingGate:
    confirmed_15m: bool
    trigger_5m: bool
    ready: bool
    evidence: list[str]


def _horizon_c_confirms(
    horizons: dict[str, HorizonForecast] | None,
    path_direction: str,
    evidence: list[str],
) -> bool:
    if not horizons:
        return False
    c = horizons.get("C")
    if c is None:
        return False
    c_dir = direction_bias(c.dominant)
    if c_dir == path_direction:
        evidence.append("horizon_c_align")
        return True
    if c_dir == "neutral" and c.conviction < 0.15:
        evidence.append("horizon_c_neutral")
        return True
    return False


def _15m_soft_confirm(m15: dict[str, Any], path_direction: str, evidence: list[str]) -> bool:
    t15 = trend_from_snapshot(m15, require_adx=False)
    adx15 = safe_float(m15.get("adx14"))
    rsi15 = safe_float(m15.get("rsi14"), 50.0)
    if path_direction == "long":
        if t15 == "bull":
            evidence.append("15m_bull")
            return True
        if t15 != "bear" and rsi15 < 55:
            evidence.append("15m_not_bear")
            return True
        if adx15 < 22:
            evidence.append("15m_weak_bear")
            return True
        evidence.append("15m_counter_bear")
        return False
    if t15 == "bear":
        evidence.append("15m_bear")
        return True
    if t15 != "bull" and rsi15 > 45:
        evidence.append("15m_not_bull")
        return True
    if adx15 < 22:
        evidence.append("15m_weak_bull")
        return True
    evidence.append("15m_counter_bull")
    return False


def assess_timing_gate(
    row: dict[str, Any],
    path_direction: str,
    *,
    horizons: dict[str, HorizonForecast] | None = None,
) -> TimingGate:
    """15m/5m never vote blend — confirm horizon C timing only."""
    tf = row.get("timeframes") if isinstance(row.get("timeframes"), dict) else {}
    m15 = tf.get("15m_closed") or tf.get("15m") or {}
    m5 = tf.get("5m_closed") or tf.get("5m") or {}
    evidence: list[str] = []

    if path_direction not in {"long", "short"}:
        return TimingGate(False, False, True, ["neutral_path"])

    confirmed = _horizon_c_confirms(horizons, path_direction, evidence)
    if not confirmed:
        if not m15 or m15.get("status") == "empty":
            evidence.append("15m_missing")
            confirmed = False
        else:
            confirmed = _15m_soft_confirm(m15, path_direction, evidence)

    trigger = True
    if m5 and m5.get("status") != "empty":
        t5 = trend_from_snapshot(m5, require_adx=False)
        adx5 = safe_float(m5.get("adx14"))
        if path_direction == "long" and t5 == "bear" and adx5 > 28:
            trigger = False
            evidence.append("5m_counter_bear")
        elif path_direction == "short" and t5 == "bull" and adx5 > 28:
            trigger = False
            evidence.append("5m_counter_bull")
        else:
            evidence.append("5m_ok")
    else:
        evidence.append("5m_neutral_pass")

    ready = confirmed and trigger
    return TimingGate(confirmed_15m=confirmed, trigger_5m=trigger, ready=ready, evidence=evidence)
