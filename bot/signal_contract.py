"""Signal trade-plan contract helpers.

The bot is signal-only: each emitted setup must be directly usable as a
manual limit-order plan. This module keeps the plan math centralized so
individual detectors do not drift into point entries or partial target gaps.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Any


UTC = timezone.utc

DEFAULT_SCALE_WEIGHTS: tuple[float, float, float] = (0.5, 0.3, 0.2)
DEFAULT_TARGET_RR: tuple[float, float, float] = (1.5, 3.0, 5.0)

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
    "continuation": 16,
    "trend_follow": 16,
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

_SETUP_TTL_BARS: dict[str, int] = {
    "fvg_setup": 20,
    "order_block": 20,
    "breaker_block": 18,
    "bos_choch": 10,
    "structure_break_retest": 10,
    "structure_pullback": 16,
    "ema_bounce": 10,
    "liquidity_sweep": 20,
    "stop_hunt_detection": 20,
    "turtle_soup": 20,
    "wick_trap_reversal": 24,
    "session_killzone": 8,
    "funding_reversal": 24,
    "ls_ratio_extreme": 24,
    "liquidation_heatmap": 12,
    "depth_imbalance": 8,
    "whale_walls": 8,
    "spread_strategy": 6,
    "aggression_shift": 8,
    "absorption": 8,
    "cvd_divergence": 16,
    "hidden_divergence": 30,
    "indicator_divergence": 30,
    "rsi_divergence_bottom": 30,
    "bb_squeeze": 12,
    "squeeze_setup": 12,
    "keltner_breakout": 12,
    "atr_expansion": 10,
    "price_velocity": 8,
    "volume_anomaly": 10,
    "volume_climax_reversal": 18,
    "multi_tf_trend": 18,
    "vwap_trend": 12,
    "supertrend_follow": 12,
    "btc_correlation": 10,
    "altcoin_season_index": 12,
    "oi_divergence": 18,
    "wyckoff_spring": 24,
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
    return 16


def valid_until_from(
    *,
    created_at: datetime | None,
    setup_id: str,
    strategy_family: str,
    timeframe: str | None,
    ttl_bars: int | None = None,
) -> datetime:
    anchor = created_at or datetime.now(UTC)
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=UTC)
    else:
        anchor = anchor.astimezone(UTC)
    bars = int(ttl_bars) if ttl_bars is not None else default_ttl_bars(
        setup_id,
        strategy_family,
        timeframe,
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


def _target_is_ordered(direction: str, entry_mid: float, targets: tuple[float, float, float]) -> bool:
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
) -> tuple[float, float, float, bool, str] | None:
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
            target_values[idx] = _target_from_risk(direction, entry_mid, risk, DEFAULT_TARGET_RR[idx])
    targets = tuple(float(value) for value in target_values)
    if direction == "long":
        candidates = sorted(targets)
        candidates[0] = max(candidates[0], _target_from_risk(direction, entry_mid, risk, 1.5))
        candidates[1] = max(candidates[1], _target_from_risk(direction, entry_mid, risk, 3.0))
        candidates[2] = max(candidates[2], _target_from_risk(direction, entry_mid, risk, 5.0))
    else:
        candidates = sorted(targets, reverse=True)
        candidates[0] = max(
            price_floor,
            min(candidates[0], _target_from_risk(direction, entry_mid, risk, 1.5)),
        )
        candidates[1] = max(
            price_floor,
            min(candidates[1], _target_from_risk(direction, entry_mid, risk, 3.0)),
        )
        candidates[2] = max(
            price_floor,
            min(candidates[2], _target_from_risk(direction, entry_mid, risk, 5.0)),
        )
        candidates = sorted(candidates, reverse=True)
    normalized = tuple(float(value) for value in candidates)
    if not _target_is_ordered(direction, entry_mid, normalized):
        if direction == "long":
            normalized = tuple(
                _target_from_risk(direction, entry_mid, risk, rr) for rr in DEFAULT_TARGET_RR
            )
        else:
            normalized = tuple(
                max(price_floor, _target_from_risk(direction, entry_mid, risk, rr))
                for rr in DEFAULT_TARGET_RR
            )
            normalized = tuple(sorted(normalized, reverse=True))
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
    entry_pad_atr_mult: float = 0.08,
    created_at: datetime | None = None,
    ttl_bars: int | None = None,
    scale_weights: tuple[float, float, float] | list[float] | None = None,
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
    entry_mid = (entry_low + entry_high) / 2.0
    normalized_targets = _normalize_targets(
        direction=normalized_direction,
        entry_mid=entry_mid,
        stop_loss=stop_value,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
    )
    if normalized_targets is None:
        return None
    target_1, target_2, target_3, single_target_mode, integrity_status = normalized_targets
    risk = abs(entry_mid - stop_value)
    if risk <= 0.0:
        return None
    ttl = int(ttl_bars) if ttl_bars is not None else default_ttl_bars(
        setup_id,
        strategy_family,
        timeframe,
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


def validate_signal_contract(signal: Any, *, now: datetime | None = None) -> list[SignalContractIssue]:
    issues: list[SignalContractIssue] = []
    direction = normalize_direction(getattr(signal, "direction", ""))
    if direction is None:
        issues.append(SignalContractIssue("direction", "invalid", getattr(signal, "direction", None)))
        direction = "long"

    entry_low = positive_float(getattr(signal, "entry_low", None))
    entry_high = positive_float(getattr(signal, "entry_high", None))
    stop_loss = positive_float(
        getattr(signal, "stop_loss", None)
        if hasattr(signal, "stop_loss")
        else getattr(signal, "stop", None)
    )
    tp1 = positive_float(getattr(signal, "tp1", None) if hasattr(signal, "tp1") else getattr(signal, "take_profit_1", None))
    tp2 = positive_float(getattr(signal, "tp2", None) if hasattr(signal, "tp2") else getattr(signal, "take_profit_2", None))
    tp3 = positive_float(getattr(signal, "tp3", None) if hasattr(signal, "tp3") else getattr(signal, "take_profit_3", None))
    valid_until = getattr(signal, "valid_until", None)

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
            issues.append(SignalContractIssue(field, "missing_or_non_positive", getattr(signal, field, None)))
    if entry_low is not None and entry_high is not None:
        if entry_low >= entry_high:
            issues.append(SignalContractIssue("entry_zone", "not_a_range", (entry_low, entry_high)))
        entry_mid = (entry_low + entry_high) / 2.0
        if stop_loss is not None:
            if direction == "long" and stop_loss >= entry_mid:
                issues.append(SignalContractIssue("stop_loss", "long_stop_not_below_entry", stop_loss))
            if direction == "short" and stop_loss <= entry_mid:
                issues.append(SignalContractIssue("stop_loss", "short_stop_not_above_entry", stop_loss))
        if tp1 is not None and tp2 is not None and tp3 is not None:
            if direction == "long" and not (entry_mid < tp1 <= tp2 <= tp3):
                issues.append(SignalContractIssue("targets", "long_targets_not_ordered", (tp1, tp2, tp3)))
            if direction == "short" and not (entry_mid > tp1 >= tp2 >= tp3):
                issues.append(SignalContractIssue("targets", "short_targets_not_ordered", (tp1, tp2, tp3)))
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
        "valid_until": valid_until.isoformat() if isinstance(valid_until, datetime) else valid_until,
        "scale_weights": list(getattr(signal, "scale_weights", DEFAULT_SCALE_WEIGHTS)),
        "ok": not issues,
        "issues": [issue.to_dict() for issue in issues],
    }
