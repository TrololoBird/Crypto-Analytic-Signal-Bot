from __future__ import annotations

from datetime import UTC, datetime

import msgspec


class TrackedSignalState(msgspec.Struct, kw_only=True):
    tracking_id: str
    tracking_ref: str
    signal_key: str
    symbol: str
    setup_id: str
    direction: str
    timeframe: str
    entry_tf: str = ""
    pattern_tf: str = ""
    context_tfs: tuple[str, ...] = ()
    created_at: str
    pending_expires_at: str
    active_expires_at: str
    entry_low: float
    entry_high: float
    entry_mid: float
    initial_stop: float | None = None
    stop: float = 0.0
    stop_price: float | None = None
    take_profit_1: float = 0.0
    tp1_price: float | None = None
    take_profit_2: float = 0.0
    tp2_price: float | None = None
    take_profit_3: float = 0.0
    tp3_price: float | None = None
    valid_until: str | None = None
    scale_weights: tuple[float, float, float] = (0.5, 0.3, 0.2)
    ttl_bars: int | None = None
    single_target_mode: bool = False
    target_integrity_status: str = "unchecked"
    score: float = 0.0
    risk_reward: float = 0.0
    reasons: tuple[str, ...] = ()
    signal_message_id: int | None = None
    bias_4h: str = "neutral"
    quote_volume: float | None = None
    spread_bps: float | None = None
    atr_pct: float | None = None
    orderflow_delta_ratio: float | None = None
    entry_order_type: str = "limit"
    confirmation_profile: str = "trend_follow"
    status: str = "pending"
    entry_zone_touched_at: str | None = None
    entry_confirm_pending_at: str | None = None
    last_lifecycle_note: str | None = None
    trailing_stop: float | None = None
    activated_at: str | None = None
    activation_price: float | None = None
    closed_at: str | None = None
    close_reason: str | None = None
    close_price: float | None = None
    tp1_hit_at: str | None = None
    tp2_hit_at: str | None = None
    last_checked_at: str | None = None
    last_price: float | None = None
    max_favorable_pct: float = 0.0
    max_adverse_pct: float = 0.0


def tp1_required_move_pct(tracked: TrackedSignalState) -> float | None:
    """Minimum favorable move % from entry to TP1."""
    entry = tracked.activation_price or tracked.entry_mid
    tp1 = float(tracked.take_profit_1 or 0.0)
    if entry is None or float(entry) <= 0.0 or tp1 <= 0.0:
        return None
    return abs(tp1 - float(entry)) / float(entry) * 100.0


def tp1_reached_from_excursion(
    tracked: TrackedSignalState,
    *,
    tolerance_pct: float = 0.05,
) -> bool:
    """True when MAE/MFE tracking shows price reached TP1 before terminal close."""
    if tracked.tp1_hit_at:
        return True
    required = tp1_required_move_pct(tracked)
    if required is None:
        return False
    return float(tracked.max_favorable_pct or 0.0) >= required - tolerance_pct


def resolve_terminal_close_reason(tracked: TrackedSignalState, reason: str) -> str:
    """Map terminal closes to tp1_hit when TP1 was reached (G1 fix-sl tracking)."""
    if tracked.activated_at is None:
        return reason
    if reason == "expired" and (
        tracked.tp1_hit_at is not None or tp1_reached_from_excursion(tracked)
    ):
        return "tp1_hit"
    if reason == "breakeven_stop" and tracked.tp1_hit_at is not None:
        return "tp1_hit"
    return reason


def parse_state_dt(value: str | None) -> datetime | None:
    """Parse ISO datetime used by tracked-signal persistence."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError, TypeError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt
