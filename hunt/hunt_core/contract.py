"""Signal trade-plan contract helpers.

The bot is signal-only: each emitted setup must be directly usable as a
manual limit-order plan. This module keeps the plan math centralized so
individual detectors do not drift into point entries or partial target gaps.
"""
from __future__ import annotations



import ast
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypedDict

from hunt_core.errors import DEFENSIVE_EXC

if TYPE_CHECKING:
    from collections.abc import Mapping

LOG = logging.getLogger("bot.contracts")

DEFAULT_SCALE_WEIGHTS: tuple[float, float, float] = (0.5, 0.3, 0.2)
DEFAULT_TARGET_RR: tuple[float, float, float] = (1.9, 3.0, 5.0)
DEFAULT_MIN_RISK_REWARD = 1.9
RISK_REWARD_EPSILON = 1e-9

# Single source of truth for entry-zone width.
# ALL callers (_build_signal, detectors, contract validation) must use this.
# 0.35×ATR gives a realistic limit-order fill window while keeping stop
# geometrically outside the zone in _build_atr_signal (min_risk clamp uses
# entry_pad + 0.06×ATR buffer).
SIGNAL_ENTRY_PAD_ATR: float = 0.35


def resolve_target_rr(settings: Any | None = None) -> tuple[float, float, float]:
    """Return configured TP RR ladder or module default."""
    if settings is None:
        return DEFAULT_TARGET_RR
    delivery = getattr(settings, "delivery", None)
    configured = getattr(delivery, "target_rr", None) if delivery is not None else None
    if not configured:
        return DEFAULT_TARGET_RR
    try:
        values = tuple(float(item) for item in configured)
    except (TypeError, ValueError):
        return DEFAULT_TARGET_RR
    if len(values) != 3 or not all(math.isfinite(item) and item > 0.0 for item in values):
        return DEFAULT_TARGET_RR
    return (values[0], values[1], values[2])


_TIMEFRAME_MINUTES: dict[str, int] = {
    "1m": 1,
    "3m": 3,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "2h": 120,
    "4h": 240,
    "1d": 1440,
}

_FAMILY_TTL_BARS: dict[str, int] = {
    "breakout": 10,
    "continuation": 24,
    "trend_follow": 24,
    "momentum": 12,
    "volatility": 12,
    "reversal": 30,
    "sentiment": 24,
    "liquidity": 20,
    "orderflow": 10,
    "orderbook": 8,
    "multi_asset": 12,
    "session": 8,
}

# Source of truth: target TTL in minutes (answers.md Part 3). Bars are derived at runtime.
_SETUP_TTL_MINUTES: dict[str, int] = {
    "fvg_setup": 120,
    "order_block": 180,
    "breaker_block": 120,
    "bos_choch": 120,
    "structure_break_retest": 120,
    "structure_pullback": 120,
    "ema_bounce": 120,
    "turtle_soup": 120,
    "funding_reversal": 120,
    "hidden_divergence": 120,
    "rsi_divergence_bottom": 120,
    "ls_ratio_extreme": 240,
    "oi_divergence": 120,
    "btc_correlation": 30,
    "wyckoff_spring": 180,
    "liquidity_sweep": 45,
    "wick_trap_reversal": 45,
    "cvd_divergence": 45,
    "squeeze_setup": 45,
    "keltner_breakout": 45,
    "absorption": 45,
    "liquidation_heatmap": 45,
    "indicator_divergence": 90,
    "price_velocity": 30,
    "volume_anomaly": 30,
    "volume_climax_reversal": 90,
    "vwap_trend": 90,
    "supertrend_follow": 120,
    "multi_tf_trend": 120,
    "altcoin_season_index": 240,
    "pinbar_reversal": 120,
}

# Legacy bar table kept for display/back-compat; derived from minutes when possible.
_SETUP_TTL_BARS: dict[str, int] = {
    # 1h entry TF strategies (~120-180min target)
    "fvg_setup": 2,  # 1h × 2 = 120min
    "order_block": 3,  # 1h × 3 = 180min
    "breaker_block": 2,  # 1h × 2 = 120min
    "bos_choch": 8,  # 15m+1h × 8 = 120min
    "structure_break_retest": 8,  # 15m+1h × 8 = 120min
    "structure_pullback": 8,  # 15m+1h × 8 = 120min
    "ema_bounce": 8,  # 15m+1h × 8 = 120min
    "turtle_soup": 8,  # 15m+1h × 8 = 120min
    "funding_reversal": 2,  # 1h × 2 = 120min
    "hidden_divergence": 8,  # 15m+1h × 8 = 120min
    "rsi_divergence_bottom": 2,  # 1h × 2 = 120min
    "ls_ratio_extreme": 4,  # 1h × 4 = 240min (sentiment, slower cadence)
    "oi_divergence": 2,  # 1h × 2 = 120min
    "btc_correlation": 2,  # 1h × 2 = 120min
    "wyckoff_spring": 3,  # 1h × 3 = 180min
    # 15m trigger strategies (~45min target)
    "liquidity_sweep": 3,  # 15m × 3 = 45min
    "wick_trap_reversal": 3,  # 15m+1h × 3 = 45min
    "cvd_divergence": 3,  # 15m+1h × 3 = 45min
    "squeeze_setup": 3,  # 15m+1h × 3 = 45min
    "keltner_breakout": 3,  # 15m+1h × 3 = 45min
    "absorption": 3,  # 15m × 3 = 45min
    "liquidation_heatmap": 3,  # 15m × 3 = 45min
    # momentum/breakout (~30-90min)
    "indicator_divergence": 6,  # 15m × 6 = 90min
    "price_velocity": 2,  # 15m × 2 = 30min
    "volume_anomaly": 2,  # 15m × 2 = 30min
    "volume_climax_reversal": 6,  # 15m × 6 = 90min
    "vwap_trend": 6,  # 15m+1h × 6 = 90min
    "supertrend_follow": 8,  # 15m × 8 = 120min
    "multi_tf_trend": 8,  # 15m+1h × 8 = 120min
    "altcoin_season_index": 4,  # 1h × 4 = 240min
    "pinbar_reversal": 8,  # 1h × 8 = 120min
}


@dataclass(frozen=True, slots=True)
class TradePlan:
    direction: str
    entry_low: float
    entry_high: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    valid_until: datetime
    scale_weights: tuple[float, float, float]
    ttl_bars: int
    entry_zone_width_pct: float
    risk_reward_tp1: float
    risk_reward_tp2: float
    risk_reward_tp3: float
    single_target_mode: bool
    integrity_status: str

    @property
    def entry_mid(self) -> float:
        return (self.entry_low + self.entry_high) / 2.0

    @property
    def entry_zone(self) -> tuple[float, float]:
        return (self.entry_low, self.entry_high)


@dataclass(frozen=True, slots=True)
class SignalContractIssue:
    field: str
    reason: str
    value: object = None

    def to_dict(self) -> dict[str, object]:
        return {
            "field": self.field,
            "reason": self.reason,
            "value": self.value,
        }


def finite_float(value: object, default: float | None = None) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else default
    return default


def positive_float(value: object, default: float | None = None) -> float | None:
    numeric = finite_float(value, default=None)
    if numeric is None or numeric <= 0.0:
        return default
    return numeric


def normalize_direction(direction: str) -> str | None:
    value = str(direction or "").strip().lower()
    if value in {"long", "buy", "bull", "bullish"}:
        return "long"
    if value in {"short", "sell", "bear", "bearish"}:
        return "short"
    return None


def timeframe_minutes(timeframe: str | None) -> int:
    raw = str(timeframe or "15m").lower().strip()
    primary = raw.split("+", 1)[0].strip()
    if primary in _TIMEFRAME_MINUTES:
        return _TIMEFRAME_MINUTES[primary]
    if primary.endswith("m"):
        numeric = positive_float(primary[:-1])
        return int(numeric) if numeric else 15
    if primary.endswith("h"):
        numeric = positive_float(primary[:-1])
        return int(numeric * 60) if numeric else 60
    return 15


def default_ttl_bars(setup_id: str, strategy_family: str, timeframe: str | None = None) -> int:
    if setup_id in _SETUP_TTL_MINUTES:
        tf_min = max(1, timeframe_minutes(timeframe))
        return max(1, min(96, round(_SETUP_TTL_MINUTES[setup_id] / tf_min)))
    if setup_id in _SETUP_TTL_BARS:
        return _SETUP_TTL_BARS[setup_id]
    family = str(strategy_family or "").strip().lower()
    if family in _FAMILY_TTL_BARS:
        return _FAMILY_TTL_BARS[family]
    minutes = timeframe_minutes(timeframe)
    if minutes <= 5:
        return 24
    if minutes >= 60:
        return 8
    return 24


def valid_until_from(
    *,
    created_at: datetime | None,
    setup_id: str,
    strategy_family: str,
    timeframe: str | None,
    ttl_bars: int | None = None,
) -> datetime:
    anchor = created_at or datetime.now(UTC)
    anchor = anchor.replace(tzinfo=UTC) if anchor.tzinfo is None else anchor.astimezone(UTC)
    bars = (
        int(ttl_bars)
        if ttl_bars is not None
        else default_ttl_bars(
            setup_id,
            strategy_family,
            timeframe,
        )
    )
    bars = max(1, min(bars, 96))
    return anchor + timedelta(minutes=timeframe_minutes(timeframe) * bars)


def normalize_scale_weights(
    weights: tuple[float, float, float] | list[float] | None,
) -> tuple[float, float, float]:
    if not weights or len(weights) != 3:
        return DEFAULT_SCALE_WEIGHTS
    cleaned: list[float] = []
    for value in weights:
        numeric = finite_float(value, default=0.0) or 0.0
        cleaned.append(max(0.0, numeric))
    total = sum(cleaned)
    if total <= 0.0:
        return DEFAULT_SCALE_WEIGHTS
    normalized = tuple(round(value / total, 4) for value in cleaned)
    drift = round(1.0 - sum(normalized), 4)
    return (normalized[0] + drift, normalized[1], normalized[2])


def _target_from_risk(direction: str, entry_mid: float, risk: float, rr: float) -> float:
    if direction == "long":
        return entry_mid + risk * rr
    return entry_mid - risk * rr


def _target_is_ordered(
    direction: str, entry_mid: float, targets: tuple[float, float, float]
) -> bool:
    tp1, tp2, tp3 = targets
    if direction == "long":
        return entry_mid < tp1 <= tp2 <= tp3
    return entry_mid > tp1 >= tp2 >= tp3


def _normalize_targets(
    *,
    direction: str,
    entry_mid: float,
    stop_loss: float,
    tp1: float,
    tp2: float,
    tp3: float | None,
    target_rr: tuple[float, float, float] = DEFAULT_TARGET_RR,
) -> tuple[float, float, float, bool, str] | None:
    rr1, rr2, rr3 = target_rr
    risk = abs(entry_mid - stop_loss)
    if risk <= 0.0:
        return None
    price_floor = max(entry_mid * 0.02, 1e-8)
    target_values = [
        positive_float(tp1),
        positive_float(tp2),
        positive_float(tp3),
    ]
    for idx, value in enumerate(target_values):
        if value is None:
            fallback_rr = (rr1, rr2, rr3)[idx]
            target_values[idx] = _target_from_risk(direction, entry_mid, risk, fallback_rr)
    resolved: list[float] = [float(value) for value in target_values if value is not None]
    if len(resolved) != 3:
        return None
    targets: tuple[float, float, float] = (resolved[0], resolved[1], resolved[2])
    if direction == "long":
        candidates = sorted(targets)
        candidates[0] = max(candidates[0], _target_from_risk(direction, entry_mid, risk, rr1))
        candidates[1] = max(candidates[1], _target_from_risk(direction, entry_mid, risk, rr2))
        candidates[2] = max(candidates[2], _target_from_risk(direction, entry_mid, risk, rr3))
    else:
        candidates = sorted(targets, reverse=True)
        candidates[0] = max(
            price_floor,
            min(candidates[0], _target_from_risk(direction, entry_mid, risk, rr1)),
        )
        candidates[1] = max(
            price_floor,
            min(candidates[1], _target_from_risk(direction, entry_mid, risk, rr2)),
        )
        candidates[2] = max(
            price_floor,
            min(candidates[2], _target_from_risk(direction, entry_mid, risk, rr3)),
        )
        candidates = sorted(candidates, reverse=True)
    normalized_list = [float(value) for value in candidates]
    if len(normalized_list) != 3:
        return None
    normalized: tuple[float, float, float] = (
        normalized_list[0],
        normalized_list[1],
        normalized_list[2],
    )
    if not _target_is_ordered(direction, entry_mid, normalized):
        if direction == "long":
            normalized = (
                _target_from_risk(direction, entry_mid, risk, rr1),
                _target_from_risk(direction, entry_mid, risk, rr2),
                _target_from_risk(direction, entry_mid, risk, rr3),
            )
        else:
            short_targets = [
                max(price_floor, _target_from_risk(direction, entry_mid, risk, rr))
                for rr in (rr1, rr2, rr3)
            ]
            short_targets.sort(reverse=True)
            normalized = (short_targets[0], short_targets[1], short_targets[2])
    scale = max(abs(entry_mid), *(abs(value) for value in normalized), 1.0)
    tolerance = scale * 1e-8
    single_target_mode = (
        abs(normalized[1] - normalized[0]) <= tolerance
        and abs(normalized[2] - normalized[1]) <= tolerance
    )
    status = "valid"
    original = tuple(targets)
    if any(abs(a - b) > tolerance for a, b in zip(original, normalized, strict=True)):
        status = "normalized"
    if single_target_mode:
        status = "single_target" if status == "valid" else "normalized_single_target"
    return normalized[0], normalized[1], normalized[2], single_target_mode, status


def build_trade_plan(
    *,
    direction: str,
    setup_id: str,
    strategy_family: str,
    timeframe: str,
    price_anchor: float,
    atr: float,
    stop_loss: float,
    tp1: float,
    tp2: float,
    tp3: float | None = None,
    entry_pad_atr_mult: float = SIGNAL_ENTRY_PAD_ATR,
    created_at: datetime | None = None,
    ttl_bars: int | None = None,
    scale_weights: tuple[float, float, float] | list[float] | None = None,
    target_rr: tuple[float, float, float] = DEFAULT_TARGET_RR,
) -> TradePlan | None:
    normalized_direction = normalize_direction(direction)
    if normalized_direction is None:
        return None
    anchor = positive_float(price_anchor)
    atr_value = positive_float(atr)
    stop_value = positive_float(stop_loss)
    if anchor is None or atr_value is None or stop_value is None:
        return None
    tolerance = max(anchor * 1e-8, 1e-8)
    if normalized_direction == "long" and stop_value >= anchor - tolerance:
        return None
    if normalized_direction == "short" and stop_value <= anchor + tolerance:
        return None

    entry_pad = max(atr_value * max(0.0, float(entry_pad_atr_mult)), anchor * 0.0005)
    entry_low = max(tolerance, anchor - entry_pad)
    entry_high = max(entry_low + tolerance, anchor + entry_pad)

    # ── Authoritative stop clamp ─────────────────────────────────────────────
    # build_trade_plan is the single owner of entry-zone geometry.  Callers
    # compute stop independently (ATR buffers, sweep levels) and cannot know
    # the exact zone boundary.  Clamp here so validate_signal_contract never
    # fires a zone-geometry violation — the zone IS the invariant, stop adapts.
    _stop_clearance = max(atr_value * 0.02, tolerance * 4)
    if normalized_direction == "long" and stop_value >= entry_low:
        stop_value = entry_low - _stop_clearance
    elif normalized_direction == "short" and stop_value <= entry_high:
        stop_value = entry_high + _stop_clearance
    # ────────────────────────────────────────────────────────────────────────

    entry_mid = (entry_low + entry_high) / 2.0
    normalized_targets = _normalize_targets(
        direction=normalized_direction,
        entry_mid=entry_mid,
        stop_loss=stop_value,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        target_rr=target_rr,
    )
    if normalized_targets is None:
        return None
    target_1, target_2, target_3, single_target_mode, integrity_status = normalized_targets
    risk = abs(entry_mid - stop_value)
    if risk <= 0.0:
        return None
    ttl = (
        int(ttl_bars)
        if ttl_bars is not None
        else default_ttl_bars(
            setup_id,
            strategy_family,
            timeframe,
        )
    )
    entry_zone_width_pct = (entry_high - entry_low) / entry_mid * 100.0 if entry_mid > 0 else 0.0
    return TradePlan(
        direction=normalized_direction,
        entry_low=min(entry_low, entry_high),
        entry_high=max(entry_low, entry_high),
        stop_loss=stop_value,
        tp1=target_1,
        tp2=target_2,
        tp3=target_3,
        valid_until=valid_until_from(
            created_at=created_at,
            setup_id=setup_id,
            strategy_family=strategy_family,
            timeframe=timeframe,
            ttl_bars=ttl,
        ),
        scale_weights=normalize_scale_weights(scale_weights),
        ttl_bars=max(1, min(ttl, 96)),
        entry_zone_width_pct=entry_zone_width_pct,
        risk_reward_tp1=abs(target_1 - entry_mid) / risk,
        risk_reward_tp2=abs(target_2 - entry_mid) / risk,
        risk_reward_tp3=abs(target_3 - entry_mid) / risk,
        single_target_mode=single_target_mode,
        integrity_status=integrity_status,
    )


def validate_signal_contract(
    signal: Any,
    *,
    now: datetime | None = None,
    min_risk_reward: float | None = None,
) -> list[SignalContractIssue]:
    effective_min_rr = (
        float(min_risk_reward) if min_risk_reward is not None else DEFAULT_MIN_RISK_REWARD
    )
    issues: list[SignalContractIssue] = []
    direction = normalize_direction(getattr(signal, "direction", ""))
    if direction is None:
        issues.append(
            SignalContractIssue("direction", "invalid", getattr(signal, "direction", None))
        )
        direction = "long"

    entry_low = positive_float(getattr(signal, "entry_low", None))
    entry_high = positive_float(getattr(signal, "entry_high", None))
    stop_loss = positive_float(
        getattr(signal, "stop_loss", None)
        if hasattr(signal, "stop_loss")
        else getattr(signal, "stop", None)
    )
    tp1 = positive_float(
        getattr(signal, "tp1", None)
        if hasattr(signal, "tp1")
        else getattr(signal, "take_profit_1", None)
    )
    tp2 = positive_float(
        getattr(signal, "tp2", None)
        if hasattr(signal, "tp2")
        else getattr(signal, "take_profit_2", None)
    )
    tp3 = positive_float(
        getattr(signal, "tp3", None)
        if hasattr(signal, "tp3")
        else getattr(signal, "take_profit_3", None)
    )
    valid_until = getattr(signal, "valid_until", None)
    scale_weights_raw = getattr(signal, "scale_weights", DEFAULT_SCALE_WEIGHTS)

    required_values = {
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop_loss": stop_loss,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
    }
    for field, value in required_values.items():
        if value is None:
            issues.append(
                SignalContractIssue(field, "missing_or_non_positive", getattr(signal, field, None))
            )
    if entry_low is not None and entry_high is not None:
        if entry_low >= entry_high:
            issues.append(SignalContractIssue("entry_zone", "not_a_range", (entry_low, entry_high)))
        entry_mid = (entry_low + entry_high) / 2.0
        if stop_loss is not None:
            if direction == "long" and stop_loss >= entry_low:
                issues.append(
                    SignalContractIssue(
                        "stop_loss",
                        "long_stop_not_below_entry_low",
                        stop_loss,
                    )
                )
            if direction == "short" and stop_loss <= entry_high:
                issues.append(
                    SignalContractIssue(
                        "stop_loss",
                        "short_stop_not_above_entry_high",
                        stop_loss,
                    )
                )
            if direction == "long" and stop_loss >= entry_mid:
                issues.append(
                    SignalContractIssue("stop_loss", "long_stop_not_below_entry", stop_loss)
                )
            if direction == "short" and stop_loss <= entry_mid:
                issues.append(
                    SignalContractIssue("stop_loss", "short_stop_not_above_entry", stop_loss)
                )
        if tp1 is not None and tp2 is not None and tp3 is not None:
            if direction == "long" and not (entry_mid < tp1 <= tp2 <= tp3):
                issues.append(
                    SignalContractIssue("targets", "long_targets_not_ordered", (tp1, tp2, tp3))
                )
            if direction == "short" and not (entry_mid > tp1 >= tp2 >= tp3):
                issues.append(
                    SignalContractIssue("targets", "short_targets_not_ordered", (tp1, tp2, tp3))
                )
            if stop_loss is not None:
                worst = entry_high if direction == "short" else entry_low
                risk = abs(worst - stop_loss)
                reward = abs(tp1 - worst)
                if risk <= 0.0:
                    issues.append(SignalContractIssue("risk_reward", "zero_or_negative_risk", risk))
                else:
                    risk_reward = reward / risk
                    if risk_reward + RISK_REWARD_EPSILON < effective_min_rr:
                        issues.append(
                            SignalContractIssue(
                                "risk_reward",
                                "tp1_rr_below_minimum",
                                round(risk_reward, 6),
                            )
                        )
    try:
        scale_weights = [float(item) for item in scale_weights_raw]
    except (TypeError, ValueError):
        scale_weights = []
    if len(scale_weights) < 2:
        issues.append(
            SignalContractIssue(
                "scale_weights", "less_than_two_entry_allocations", scale_weights_raw
            )
        )
    elif any(not math.isfinite(item) or item <= 0.0 for item in scale_weights):
        issues.append(
            SignalContractIssue("scale_weights", "non_positive_or_non_finite", scale_weights_raw)
        )
    else:
        total_weight = sum(scale_weights)
        max_weight = max(scale_weights)
        if max_weight <= 1.0 and total_weight > 1.000001:
            issues.append(
                SignalContractIssue(
                    "scale_weights", "fraction_sum_above_one", round(total_weight, 6)
                )
            )
        if max_weight > 1.0 and total_weight > 100.000001:
            issues.append(
                SignalContractIssue(
                    "scale_weights", "percent_sum_above_100", round(total_weight, 6)
                )
            )
    if not isinstance(valid_until, datetime):
        issues.append(SignalContractIssue("valid_until", "missing_or_not_datetime", valid_until))
    else:
        check_now = now or datetime.now(UTC)
        if check_now.tzinfo is None:
            check_now = check_now.replace(tzinfo=UTC)
        if valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=UTC)
        if valid_until <= check_now.astimezone(UTC):
            issues.append(SignalContractIssue("valid_until", "expired", valid_until.isoformat()))
    return issues


def signal_contract_row(signal: Any) -> dict[str, object]:
    issues = validate_signal_contract(signal)
    valid_until = getattr(signal, "valid_until", None)
    return {
        "symbol": getattr(signal, "symbol", None),
        "setup_id": getattr(signal, "setup_id", None),
        "direction": getattr(signal, "direction", None),
        "entry_zone": [
            getattr(signal, "entry_low", None),
            getattr(signal, "entry_high", None),
        ],
        "stop_loss": getattr(signal, "stop_loss", getattr(signal, "stop", None)),
        "tp1": getattr(signal, "tp1", getattr(signal, "take_profit_1", None)),
        "tp2": getattr(signal, "tp2", getattr(signal, "take_profit_2", None)),
        "tp3": getattr(signal, "tp3", getattr(signal, "take_profit_3", None)),
        "valid_until": valid_until.isoformat()
        if isinstance(valid_until, datetime)
        else valid_until,
        "scale_weights": list(getattr(signal, "scale_weights", DEFAULT_SCALE_WEIGHTS)),
        "ok": not issues,
        "issues": [issue.to_dict() for issue in issues],
    }

Direction = Literal["short", "long"]
BtOutcome = Literal["tp1_hit", "tp2_hit", "sl_hit", "timeout"]
CloseReason = Literal[
    "stop_hit",
    "tp1",
    "tp2",
    "invalidate",
    "lifecycle_stale",
    "bias_flip",
    "timeout",
    "manual",
    "reclaim",
]


class LifecycleBlock(TypedDict, total=False):
    phase: str
    recommended_bias: str
    short_entry_ok: bool
    long_entry_ok: bool
    fall_from_high_pct: float | None
    bounce_from_low_pct: float | None


class DumpBlock(TypedDict, total=False):
    phase: str
    score: float | None
    fuel: float | None
    triggers: list[str]
    confirm_hard: list[str]
    confirmed: bool
    entry_zone: list[float] | None
    support_break_level: float | None
    stop_loss: float | None
    tp1: float | None
    tp2: float | None
    invalidation_above: float | None
    levels_viable: bool
    levels_veto: str | None


class LongBlock(TypedDict, total=False):
    confirmed: bool
    score: float | None
    fuel: float | None
    entry_zone: list[float] | None
    stop_loss: float | None
    tp1: float | None
    tp2: float | None


class MarketBlock(TypedDict, total=False):
    taker_5m: float | None
    oi_chg_1h: float | None
    oi_z_score: float | None
    funding_pct: float | None
    top_ls_1h: float | None
    depth_imbalance: float | None
    liquidation_score_5m: float | None
    microprice_bias: float | None


class TickRow(TypedDict, total=False):
    ts: str
    symbol: str
    price: float
    chg_24h_pct: float | None
    range_24h_pct: float | None
    lifecycle: LifecycleBlock
    dump: DumpBlock
    long: LongBlock
    market: MarketBlock
    regime: dict[str, Any]
    session: dict[str, Any]
    book_walls: dict[str, Any]


class TrackerFeatureVector(TypedDict, total=False):
    ts: str | None
    price: float | None
    market: dict[str, Any]
    regime: dict[str, Any]
    lifecycle_phase: str | None
    lifecycle_bias: str | None
    fall_from_high_pct: float | None
    bounce_from_low_pct: float | None
    pos_in_range: float | None


class SignalRecord(TypedDict, total=False):
    symbol: str
    direction: Direction
    entry_lo: float
    entry_hi: float
    stop_loss: float
    tp1: float
    tp2: float
    invalidation_above: float | None
    invalidation_below: float | None
    fuel: float | None
    entry_lifecycle_phase: str | None
    entry_lifecycle_bias: str | None
    close_reason: CloseReason | str | None
    exit_price: float | None
    pnl_pct: float | None
    mfe_pct: float | None
    duration_min: float | None
    extreme_hi: float | None
    extreme_lo: float | None
    entry_message_id: int | None
    opened_at: str | None
    closed_at: str | None
    features_open: TrackerFeatureVector
    features_peak: TrackerFeatureVector
    features_close: TrackerFeatureVector


class OutcomeRecord(TypedDict, total=False):
    symbol: str
    direction: Direction
    lifecycle_phase: str
    fuel: float | None
    entry_lo: float
    entry_hi: float
    stop_loss: float
    tp1: float
    tp2: float
    bt_outcome: BtOutcome
    bt_mfe_pct: float | None
    bt_mae_pct: float | None
    bt_candles_to_tp1: int | None
    opened_at: str | None
    source: str
    grade_id: str | None


def normalize_tick_row(row: dict[str, Any]) -> dict[str, Any]:
    """Dedupe positioning==market; ensure nested dicts."""
    out = dict(row)
    market = out.get("market") or out.get("positioning") or {}
    if isinstance(market, dict):
        out["market"] = dict(market)
    out.pop("positioning", None)
    for key in ("lifecycle", "dump", "long", "regime", "session", "book_walls"):
        val = out.get(key)
        if val is not None and not isinstance(val, dict):
            out[key] = {}
    return out


def outcome_from_row(row: dict[str, Any], *, source: str) -> OutcomeRecord:
    """Build OutcomeRecord from graded JSONL row."""
    phase = row.get("lifecycle_phase") or row.get("entry_lifecycle_phase") or "unknown"
    return OutcomeRecord(
        symbol=str(row.get("symbol", "")),
        direction=row.get("direction", "short"),  # type: ignore[typeddict-item]
        lifecycle_phase=str(phase),
        fuel=row.get("fuel"),
        entry_lo=float(row.get("entry_lo") or row.get("entry_lo", 0)),
        entry_hi=float(row.get("entry_hi") or row.get("entry_hi", 0)),
        stop_loss=float(row.get("stop_loss") or 0),
        tp1=float(row.get("tp1") or 0),
        tp2=float(row.get("tp2") or 0),
        bt_outcome=row.get("bt_outcome", "timeout"),  # type: ignore[typeddict-item]
        bt_mfe_pct=row.get("bt_mfe_pct"),
        bt_mae_pct=row.get("bt_mae_pct"),
        bt_candles_to_tp1=row.get("bt_candles_to_tp1"),
        opened_at=row.get("opened_at"),
        source=source,
        grade_id=row.get("grade_id"),
    )


# --- Feature Contract ---

PUBLIC_FEATURE_SCHEMA_VERSION = "v1"
PUBLIC_FEATURE_FIELDS: tuple[str, ...] = (
    "rsi_15m",
    "rsi_1h",
    "rsi_4h",
    "adx_1h",
    "adx_4h",
    "atr_pct_15m",
    "volume_ratio_15m",
    "macd_histogram_15m",
    "ema20_above_ema50_15m",
    "ema50_above_ema200_15m",
    "ema20_above_ema50_1h",
    "ema50_above_ema200_1h",
    "supertrend_dir_1h",
    "supertrend_dir_15m",
    "obv_above_ema_15m",
    "bb_pct_b_15m",
    "bb_width_15m",
    "funding_rate",
    "oi_current",
    "oi_change_pct",
    "oi_slope_5m",
    "ls_ratio",
    "global_ls_ratio",
    "top_trader_position_ratio",
    "top_vs_global_ls_gap",
    "liquidation_score",
    "mark_index_spread_bps",
    "premium_zscore_5m",
    "premium_slope_5m",
    "context_snapshot_age_seconds",
    "depth_imbalance",
    "microprice_bias",
    "agg_trade_delta_30s",
    "aggression_shift",
    "spot_lead_return_1m",
    "spot_futures_spread_bps",
    "mark_price_age_seconds",
    "ticker_price_age_seconds",
    "book_ticker_age_seconds",
    "data_source_mix",
    "market_regime",
    "vah_1h",
    "val_1h",
    "vah_15m",
    "val_15m",
    "funding_rate_zscore_48h",
    "liquidation_cascade_5m",
)

# CCXT source map for ops/debug when readiness gates fail (see hunt/docs/CCXT.md).
MARKET_FIELD_CCXT_SOURCE: dict[str, str] = {
    "mark_price": "WS watchMarkPrices | REST fetchMarkOHLCV",
    "funding_rate": "REST fetchFundingRate | WS watchMarkPrices",
    "funding_trend": "REST fetchFundingRateHistory",
    "oi_current": "REST fetchOpenInterest",
    "oi_change_pct": "REST fetchOpenInterest + history",
    "oi_slope_5m": "REST fetchOpenInterest series",
    "ls_ratio": "implicit fapiDataGetTopLongShortAccountRatio",
    "top_position_ls_ratio": "implicit fapiDataGetTopLongShortPositionRatio",
    "global_ls_ratio": "implicit fapiDataGetGlobalLongShortAccountRatio",
    "taker_ratio": "implicit fapiDataGetTakerlongshortRatio",
    "premium_zscore_5m": "REST fetchPremiumIndexOHLCV / mark-index",
    "premium_slope_5m": "REST fetchPremiumIndexOHLCV",
    "mark_index_spread_bps": "WS watchMarkPrices | REST mark/index",
    "bid_price": "REST fetchOrderBook | WS watchOrderBookForSymbols | watchBidsAsks",
    "ask_price": "REST fetchOrderBook | WS watchOrderBookForSymbols | watchBidsAsks",
    "bid_qty": "REST fetchOrderBook | WS watchOrderBookForSymbols",
    "ask_qty": "REST fetchOrderBook | WS watchOrderBookForSymbols",
    "depth_imbalance": "REST fetchOrderBook depth | WS watchOrderBookForSymbols",
    "microprice_bias": "REST fetchOrderBook | WS watchOrderBookForSymbols",
    "agg_trade_delta_30s": "REST fetchTrades | WS watchTradesForSymbols",
    "aggression_shift": "REST fetchTrades | WS watchTradesForSymbols",
    "liquidation_score": "WS watchLiquidationsForSymbols",
    "liquidation_cascade_5m": "WS watchLiquidationsForSymbols",
    "spot_lead_return_1m": "REST spot fetchOHLCV (HuntCcxtSpotCompanion)",
    "spot_futures_spread_bps": "REST spot + futures ticker",
    "basis": "implicit fapiDataGetBasis | REST mark/index OHLCV",
}

PRIVATE_KEYS = {"balance", "position", "order", "account", "margin"}


def validate_public_feature_payload(payload: Mapping[str, Any]) -> None:
    if any(key in payload for key in PRIVATE_KEYS):
        msg = f"Private data in public feature payload: {payload.keys()}"
        raise ValueError(msg)
    expected = set(PUBLIC_FEATURE_FIELDS)
    provided = set(payload.keys())

    missing = tuple(sorted(expected - provided))
    extra = tuple(sorted(provided - expected))
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append(f"missing={missing}")
        if extra:
            details.append(f"extra={extra}")
        raise ValueError("public feature payload schema mismatch: " + "; ".join(details))


def normalize_public_feature_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    validate_public_feature_payload(payload)
    return {name: payload.get(name) for name in PUBLIC_FEATURE_FIELDS}


def _normalized_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(parsed) or math.isinf(parsed):
        return default
    return parsed


def _normalized_bool(value: Any, *, default: bool | None = None) -> bool | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return bool(value)


def build_public_feature_snapshot(prepared: Any) -> dict[str, Any]:
    """Build a normalized public feature snapshot from PreparedSymbol-like data."""
    if prepared is None:
        return normalize_public_feature_payload(dict.fromkeys(PUBLIC_FEATURE_FIELDS))

    features: dict[str, Any] = {}

    def _frame_value(frame: Any, column: str) -> float | None:
        if frame is None or getattr(frame, "is_empty", lambda: True)():
            return None
        if column not in getattr(frame, "columns", []):
            return None
        try:
            return _normalized_float(frame.item(-1, column))
        except DEFENSIVE_EXC as exc:
            LOG.debug("public feature snapshot read failed | column=%s error=%s", column, exc)
            return None

    def _ema_stack(frame: Any, fast: str, slow: str) -> bool | None:
        fast_value = _frame_value(frame, fast)
        slow_value = _frame_value(frame, slow)
        if fast_value is None or slow_value is None or slow_value <= 0.0:
            return None
        return fast_value > slow_value

    work_15m = getattr(prepared, "work_15m", None)
    work_1h = getattr(prepared, "work_1h", None)
    work_4h = getattr(prepared, "work_4h", None)

    features["rsi_15m"] = _frame_value(work_15m, "rsi14")
    features["rsi_1h"] = _frame_value(work_1h, "rsi14")
    features["rsi_4h"] = _frame_value(work_4h, "rsi14")
    features["adx_1h"] = _frame_value(work_1h, "adx14")
    features["adx_4h"] = _frame_value(work_4h, "adx14")
    features["atr_pct_15m"] = _frame_value(work_15m, "atr_pct")
    features["volume_ratio_15m"] = _frame_value(work_15m, "volume_ratio20")
    features["macd_histogram_15m"] = _frame_value(work_15m, "macd_hist")

    features["ema20_above_ema50_15m"] = _normalized_bool(_ema_stack(work_15m, "ema20", "ema50"))
    features["ema50_above_ema200_15m"] = _normalized_bool(_ema_stack(work_15m, "ema50", "ema200"))
    features["ema20_above_ema50_1h"] = _normalized_bool(_ema_stack(work_1h, "ema20", "ema50"))
    features["ema50_above_ema200_1h"] = _normalized_bool(_ema_stack(work_1h, "ema50", "ema200"))

    features["supertrend_dir_1h"] = _frame_value(work_1h, "supertrend_dir")
    features["supertrend_dir_15m"] = _frame_value(work_15m, "supertrend_dir")
    features["obv_above_ema_15m"] = _frame_value(work_15m, "obv_above_ema")
    features["bb_pct_b_15m"] = _frame_value(work_15m, "bb_pct_b")
    features["bb_width_15m"] = _frame_value(work_15m, "bb_width")

    features["funding_rate"] = _normalized_float(getattr(prepared, "funding_rate", None))
    features["oi_current"] = _normalized_float(getattr(prepared, "oi_current", None))
    features["oi_change_pct"] = _normalized_float(getattr(prepared, "oi_change_pct", None))
    features["oi_slope_5m"] = _normalized_float(getattr(prepared, "oi_slope_5m", None))
    features["ls_ratio"] = _normalized_float(getattr(prepared, "ls_ratio", None))
    features["global_ls_ratio"] = _normalized_float(getattr(prepared, "global_ls_ratio", None))
    features["top_trader_position_ratio"] = _normalized_float(
        getattr(prepared, "top_trader_position_ratio", None)
    )
    features["top_vs_global_ls_gap"] = _normalized_float(
        getattr(prepared, "top_vs_global_ls_gap", None)
    )
    features["liquidation_score"] = _normalized_float(getattr(prepared, "liquidation_score", None))
    features["mark_index_spread_bps"] = _normalized_float(
        getattr(prepared, "mark_index_spread_bps", None)
    )
    features["premium_zscore_5m"] = _normalized_float(getattr(prepared, "premium_zscore_5m", None))
    features["premium_slope_5m"] = _normalized_float(getattr(prepared, "premium_slope_5m", None))
    features["context_snapshot_age_seconds"] = _normalized_float(
        getattr(prepared, "context_snapshot_age_seconds", None)
    )
    features["depth_imbalance"] = _normalized_float(getattr(prepared, "depth_imbalance", None))
    features["microprice_bias"] = _normalized_float(getattr(prepared, "microprice_bias", None))
    features["agg_trade_delta_30s"] = _normalized_float(
        getattr(prepared, "agg_trade_delta_30s", None)
    )
    features["aggression_shift"] = _normalized_float(getattr(prepared, "aggression_shift", None))
    features["spot_lead_return_1m"] = _normalized_float(
        getattr(prepared, "spot_lead_return_1m", None)
    )
    features["spot_futures_spread_bps"] = _normalized_float(
        getattr(prepared, "spot_futures_spread_bps", None)
    )
    features["mark_price_age_seconds"] = _normalized_float(
        getattr(prepared, "mark_price_age_seconds", None)
    )
    features["ticker_price_age_seconds"] = _normalized_float(
        getattr(prepared, "ticker_price_age_seconds", None)
    )
    features["book_ticker_age_seconds"] = _normalized_float(
        getattr(prepared, "book_ticker_age_seconds", None)
    )
    features["data_source_mix"] = (
        getattr(prepared, "data_source_mix", "futures_only") or "futures_only"
    )
    features["market_regime"] = getattr(prepared, "market_regime", "neutral") or "neutral"
    features["vah_1h"] = _normalized_float(getattr(prepared, "vah_1h", None))
    features["val_1h"] = _normalized_float(getattr(prepared, "val_1h", None))
    features["vah_15m"] = _normalized_float(getattr(prepared, "vah_15m", None))
    features["val_15m"] = _normalized_float(getattr(prepared, "val_15m", None))
    features["funding_rate_zscore_48h"] = _normalized_float(
        getattr(prepared, "funding_rate_zscore_48h", None)
    )
    cascade = getattr(prepared, "liquidation_cascade_5m", None)
    if cascade is None:
        features["liquidation_cascade_5m"] = None
    else:
        features["liquidation_cascade_5m"] = bool(cascade)

    return normalize_public_feature_payload(features)


# --- Hunt delivery contract (Phase 3b) ---

DeliveryTierKind = Literal["armed", "triggered"]
DeliveryStageKind = Literal["early", "dump_hunt", "squeeze", "confirm", "analyze"]


class SetupDeliveryContract(TypedDict, total=False):
    """Typed payload for Telegram delivery + tracker registration."""

    symbol: str
    direction: Literal["short", "long"]
    setup_id: str
    delivery_tier: DeliveryTierKind
    delivery_stage: DeliveryStageKind
    entry_lo: float
    entry_hi: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    invalidation_above: float | None
    invalidation_below: float | None
    fuel: float | None
    score: float | None
    lifecycle_phase: str | None
    lifecycle_bias: str | None
    confirm_hard: list[str]
    triggers: list[str]
    risk_reward: float | None
    gate_code: str | None
    card_html: str | None
    telegram_message_id: int | None
    opened_at: str | None


def build_setup_delivery_contract(
    row: dict[str, Any],
    *,
    direction: str,
    setup: dict[str, Any],
    delivery_tier: str,
    delivery_stage: DeliveryStageKind = "confirm",
    gate_code: str | None = None,
    card_html: str | None = None,
) -> SetupDeliveryContract:
    """Materialize delivery contract from live tick row + setup block."""
    ez = setup.get("entry_zone") or [0, 0]
    try:
        entry_lo = float(ez[0])
        entry_hi = float(ez[1])
    except (TypeError, ValueError, IndexError):
        entry_lo = entry_hi = 0.0
    lc = row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else {}
    fuel_key = "dump_fuel" if direction == "short" else "long_fuel"
    score_key = "dump_score" if direction == "short" else "long_score"
    tp2 = setup.get("tp2") or setup.get("tp1")
    return SetupDeliveryContract(
        symbol=str(row.get("symbol") or "").upper(),
        direction="short" if direction == "short" else "long",
        setup_id=str(setup.get("setup_id") or setup.get("phase") or "unknown"),
        delivery_tier="armed" if str(delivery_tier).lower() == "armed" else "triggered",
        delivery_stage=delivery_stage,
        entry_lo=entry_lo,
        entry_hi=entry_hi,
        stop_loss=float(setup.get("stop_loss") or 0),
        tp1=float(setup.get("tp1") or 0),
        tp2=float(tp2 or 0),
        tp3=float(tp2 or 0),
        invalidation_above=setup.get("invalidation_above"),
        invalidation_below=setup.get("invalidation_below"),
        fuel=float(setup.get(fuel_key) or 0) if setup.get(fuel_key) is not None else None,
        score=float(setup.get(score_key) or 0) if setup.get(score_key) is not None else None,
        lifecycle_phase=str(lc.get("phase") or setup.get("lifecycle_phase") or ""),
        lifecycle_bias=str(lc.get("recommended_bias") or ""),
        confirm_hard=list(setup.get("confirm_hard") or []),
        triggers=list(setup.get("triggers") or []),
        risk_reward=setup.get("risk_reward"),
        gate_code=gate_code,
        card_html=card_html,
        opened_at=str(row.get("ts") or ""),
    )


# --- Runtime Contract ---

RUNTIME_CALL_PATH_FILES: tuple[Path, ...] = (
    Path("main.py"),
    Path("bot/cli.py"),
    Path("bot/__init__.py"),
    Path("bot/runtime/bot.py"),
)

RUNTIME_PUBLIC_IMPORT_CONTRACT: tuple[str, ...] = (
    "SignalBot",
    "BotSettings",
    "load_settings",
)

SCAFFOLD_IMPORT_BLOCKLIST: tuple[str, ...] = (
    "scaffold",
    "experimental",
    "prototype",
)


def imported_module_names(file_path: Path) -> set[str]:
    tree = ast.parse(file_path.read_text(encoding="utf-8"))
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)
    return imported_names


def assert_runtime_import_contract(imported_names: set[str]) -> None:
    for blocked in SCAFFOLD_IMPORT_BLOCKLIST:
        if any(blocked in name for name in imported_names):
            msg = f"runtime import contract violation: blocked import fragment {blocked!r}"
            raise ValueError(msg)


def assert_runtime_call_path_is_clean() -> None:
    imported_names: set[str] = set()
    for file_path in RUNTIME_CALL_PATH_FILES:
        imported_names.update(imported_module_names(file_path))
    assert_runtime_import_contract(imported_names)
