"""Pre-activation revalidation for pending limit plans (research: deep-research-report-2).

Before promoting pending → active, re-check that the thesis still holds at touch/fill
time. Publish-time filters are not re-run automatically; this gate is the lifecycle
counterpart to delivery ``entry_staleness`` and reversal confirmation profiles.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from bot.domain.limit_entry import (
    confirm_strategy_activation,
    normalize_confirmation_profile,
)
from bot.domain.regime_gates import (
    activation_supertrend_blocked,
    trend_regime_blocks_reversal,
)
from bot.persistence.tracked import TrackedSignalState, parse_state_dt
from bot.setups.bar_patterns import engulfing_confirm, pin_bar_confirm

_REVERSAL_PROFILES = frozenset({"countertrend_exhaustion", "divergence_reversal"})
_MAX_CONTEXT_AGE_SECONDS = 120.0


def _feature_float(features: dict[str, Any] | None, *keys: str) -> float | None:
    if not features:
        return None
    for key in keys:
        raw = features.get(key)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            return value
    return None


def _feature_bool(features: dict[str, Any] | None, key: str) -> bool | None:
    if not features:
        return None
    raw = features.get(key)
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes"}
    return None


def _adverse_deviation_pct(*, direction: str, anchor: float, price: float) -> float:
    if anchor <= 0.0 or price <= 0.0:
        return 0.0
    if direction == "long":
        return max(0.0, (anchor - price) / anchor * 100.0)
    return max(0.0, (price - anchor) / anchor * 100.0)


def _zone_invalidated_before_fill(tracked: TrackedSignalState, *, price: float) -> bool:
    """True when price has traded through the structural stop side before activation."""
    stop = tracked.initial_stop or tracked.stop
    if stop is None or stop <= 0.0:
        return False
    if tracked.direction == "long" and price <= float(stop):
        return True
    return tracked.direction == "short" and price >= float(stop)


def evaluate_activation_confluence(
    tracked: TrackedSignalState,
    features: dict[str, Any] | None,
    *,
    min_confirmations: int = 3,
) -> tuple[bool, str]:
    """Light 3-of-5 confluence recheck at activation (report-2 §G, report-4 §G)."""
    direction = tracked.direction
    profile = normalize_confirmation_profile(tracked.confirmation_profile)
    vol_ratio = _feature_float(features, "volume_ratio_15m", "volume_ratio20") or 0.0
    rsi = _feature_float(features, "rsi_15m", "rsi14") or 50.0
    ema20_above_50 = _feature_bool(features, "ema20_above_ema50_15m")
    ema20_above_50_1h = _feature_bool(features, "ema20_above_ema50_1h")
    bias_4h = str((features or {}).get("bias_4h") or "neutral").lower()
    micro = _feature_float(features, "microprice_bias")
    agg = _feature_float(features, "agg_trade_delta_30s")

    if profile == "breakout_acceptance":
        vol_mult = 1.1
    elif profile in _REVERSAL_PROFILES:
        vol_mult = 0.7
    else:
        vol_mult = 1.0
    volume_ok = vol_ratio >= vol_mult

    trend_ok = False
    if direction == "long":
        if ema20_above_50 is True or ema20_above_50_1h is True or bias_4h == "uptrend":
            trend_ok = True
    elif direction == "short":
        if ema20_above_50 is False or ema20_above_50_1h is False or bias_4h == "downtrend":
            trend_ok = True

    momentum_ok = False
    if direction == "long" and 30.0 < rsi < 65.0:
        momentum_ok = True
    elif direction == "short" and 35.0 < rsi < 70.0:
        momentum_ok = True

    htf_ok = False
    if direction == "long" and bias_4h in {"uptrend", "neutral"}:
        htf_ok = True
    elif direction == "short" and bias_4h in {"downtrend", "neutral"}:
        htf_ok = True

    micro_ok = False
    if micro is not None:
        if direction == "long" and micro >= 0.05:
            micro_ok = True
        elif direction == "short" and micro <= -0.05:
            micro_ok = True
    if agg is not None:
        if direction == "long" and agg >= 0.0:
            micro_ok = True
        elif direction == "short" and agg <= 0.0:
            micro_ok = True

    legs = {
        "trend": trend_ok,
        "momentum": momentum_ok,
        "volume": volume_ok,
        "htf": htf_ok,
        "microstructure": micro_ok,
    }
    count = sum(legs.values())
    if count >= min_confirmations:
        return True, f"activation_confluence_ok count={count}"
    failed = [name for name, ok in legs.items() if not ok]
    return False, f"activation_confluence_failed count={count}<{min_confirmations} missing={','.join(failed)}"


def evaluate_pre_activation(
    tracked: TrackedSignalState,
    *,
    price: float,
    now: datetime,
    features: dict[str, Any] | None = None,
    bar_open: float | None = None,
    bar_close: float | None = None,
    bar_high: float | None = None,
    bar_low: float | None = None,
    staleness_atr_mult: float = 1.2,
    max_pending_minutes: int | None = None,
    min_score_at_activation: float = 0.65,
    score_decay_per_15m_bar: float = 0.03,
    context_max_age_seconds: float = 120.0,
    activation_confluence_enabled: bool = True,
    activation_min_confirmations: int = 3,
    reversal_activation_pin_required: bool = False,
) -> tuple[bool, str]:
    """Return (ok, note). When ok is False the pending plan must not activate."""
    if tracked.activated_at is not None:
        return True, "already_active"

    if _zone_invalidated_before_fill(tracked, price=price):
        return False, "zone_invalidated_stop_breached"

    anchor = float(tracked.entry_mid or (tracked.entry_low + tracked.entry_high) / 2.0)
    atr_pct = _feature_float(features, "atr_pct", "atr_pct_15m") or float(tracked.atr_pct or 0.0)
    if atr_pct > 0.0:
        adverse = _adverse_deviation_pct(direction=tracked.direction, anchor=anchor, price=price)
        threshold = max(0.15, float(staleness_atr_mult) * atr_pct)
        if adverse > threshold:
            return False, f"activation_staleness adverse={adverse:.2f}%>{threshold:.2f}%"

    created = parse_state_dt(tracked.created_at)
    if created is not None and max_pending_minutes is not None and max_pending_minutes > 0:
        age_min = (now.astimezone(UTC) - created.astimezone(UTC)).total_seconds() / 60.0
        if age_min > float(max_pending_minutes):
            return False, f"pending_too_old age_min={age_min:.0f}"

    publish_score = float(tracked.score or 0.0)
    if publish_score > 0.0 and created is not None and score_decay_per_15m_bar > 0.0:
        age_bars = max(0, int((now.astimezone(UTC) - created.astimezone(UTC)).total_seconds() / 900.0))
        effective_score = publish_score * (1.0 - float(score_decay_per_15m_bar) * age_bars)
        if effective_score < float(min_score_at_activation):
            return False, (
                f"activation_score_decay effective={effective_score:.3f}"
                f"<min={min_score_at_activation:.2f} bars={age_bars}"
            )

    context_age = _feature_float(features, "context_snapshot_age_seconds")
    if context_age is not None and context_age > float(context_max_age_seconds):
        return False, f"activation_context_stale age_s={context_age:.0f}"

    profile = normalize_confirmation_profile(tracked.confirmation_profile)
    st15 = _feature_float(features, "supertrend_dir_15m")
    st1h = _feature_float(features, "supertrend_dir_1h")
    if activation_supertrend_blocked(tracked.direction, st15, st1h):
        if tracked.direction == "short":
            return False, "activation_blocked_supertrend_up_short"
        return False, "activation_blocked_supertrend_down_long"

    market_regime = str((features or {}).get("market_regime") or "").lower()
    bias_1h = str((features or {}).get("bias_1h") or (features or {}).get("bias_4h") or "").lower()
    blocked, block_note = trend_regime_blocks_reversal(
        tracked.setup_id,
        market_regime,
        bias_1h,
        tracked.direction,
    )
    if blocked and block_note:
        return False, block_note

    if profile in _REVERSAL_PROFILES and bar_high is not None and bar_low is not None:
        if bar_close is None or bar_open is None:
            return False, "await_bar_confirm"
        ok, note = confirm_strategy_activation(
            direction=tracked.direction,
            confirmation_profile=profile,
            entry_low=tracked.entry_low,
            entry_high=tracked.entry_high,
            open_=bar_open,
            close=bar_close,
            high=bar_high,
            low=bar_low,
        )
        if not ok:
            return False, note
        if reversal_activation_pin_required:
            pin_ok = pin_bar_confirm(
                tracked.direction, bar_open, bar_high, bar_low, bar_close
            )
            engulf_ok = engulfing_confirm(
                tracked.direction,
                bar_open,
                bar_high,
                bar_low,
                bar_close,
                bar_open,
                bar_high,
                bar_low,
                bar_close,
            )
            if not (pin_ok or engulf_ok):
                return False, "activation_reversal_pin_required"

    if activation_confluence_enabled:
        conf_ok, conf_note = evaluate_activation_confluence(
            tracked,
            features,
            min_confirmations=activation_min_confirmations,
        )
        if not conf_ok:
            return False, conf_note

    return True, "activation_ok"
