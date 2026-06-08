from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, cast

from ..features.prepare import _swing_points
from ..setups import _build_signal, _compute_dynamic_score, _reject
from ..setups.utils import get_dynamic_params
from ._common import confirmed_pattern_frame
from .roadmap_base import RoadmapSetup

if TYPE_CHECKING:
    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

LOG = logging.getLogger(__name__)


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else default
    return default


def _finite_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _orderflow_conflicts(
    prepared: PreparedSymbol,
    direction: str,
    *,
    max_adverse_depth: float,
    max_adverse_micro: float,
) -> tuple[bool, dict[str, float]]:
    depth = _finite_or_none(prepared.depth_imbalance)
    micro = _finite_or_none(prepared.microprice_bias)
    details: dict[str, float] = {}
    if depth is not None:
        details["depth_imbalance"] = depth
    if micro is not None:
        details["microprice_bias"] = micro
    if direction == "long":
        return (
            bool(
                (depth is not None and depth <= -max_adverse_depth)
                or (micro is not None and micro <= -max_adverse_micro)
            ),
            details,
        )
    return (
        bool(
            (depth is not None and depth >= max_adverse_depth)
            or (micro is not None and micro >= max_adverse_micro)
        ),
        details,
    )


_DEFAULT_KILLZONE_WINDOWS: tuple[tuple[str, int, int], ...] = (
    ("Overlap", 13, 16),
    ("London", 7, 10),
    ("NY", 13, 16),
    ("Asia", 0, 3),
)


def _hour_param(params: dict[str, object], key: str, default: int) -> int:
    value = params.get(key, default)
    try:
        hour = int(float(cast("Any", value)))
    except (TypeError, ValueError):
        return default
    return max(0, min(hour, 24))


def _session_windows_from_params(
    params: dict[str, object] | None = None,
) -> tuple[tuple[str, int, int], ...]:
    raw = params or {}
    return (
        (
            "Overlap",
            _hour_param(raw, "overlap_start_hour_utc", 13),
            _hour_param(raw, "overlap_end_hour_utc", 16),
        ),
        (
            "London",
            _hour_param(raw, "london_start_hour_utc", 7),
            _hour_param(raw, "london_end_hour_utc", 10),
        ),
        (
            "NY",
            _hour_param(raw, "ny_start_hour_utc", 13),
            _hour_param(raw, "ny_end_hour_utc", 17),
        ),
        (
            "Asia",
            _hour_param(raw, "asia_start_hour_utc", 0),
            _hour_param(raw, "asia_end_hour_utc", 3),
        ),
        (
            "PreLondon",
            _hour_param(raw, "pre_london_start_hour_utc", 5),
            _hour_param(raw, "pre_london_end_hour_utc", 7),
        ),
        (
            "NYClose",
            _hour_param(raw, "ny_close_start_hour_utc", 20),
            _hour_param(raw, "ny_close_end_hour_utc", 22),
        ),
    )


def _hour_in_window(hour: int, start: int, end: int) -> bool:
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _active_killzone_name(
    now_utc: datetime | int,
    params: dict[str, object] | None = None,
) -> str | None:
    raw = params or {}
    # DST-aware path (default): delegate to the shared session module so killzone
    # UTC boundaries follow London/NY daylight saving instead of drifting in summer.
    if bool(raw.get("session_dst_aware", True)) and isinstance(now_utc, datetime):
        from bot.domain.sessions import active_killzone  # noqa: PLC0415

        return active_killzone(now_utc)
    # Legacy fixed-UTC windows (operator override via session_dst_aware=false).
    hour = now_utc.hour if isinstance(now_utc, datetime) else int(now_utc)
    for name, start, end in _session_windows_from_params(raw):
        if _hour_in_window(hour, start, end):
            return name
    return None


def _in_killzone(now_utc: datetime | int, params: dict[str, object] | None = None) -> bool:
    return _active_killzone_name(now_utc, params) is not None


def _is_in_buffer_zone(hour: int, params: dict[str, object] | None = None) -> bool:
    """Check if hour falls in pre/post buffer zone of any session (III.18)."""
    pre = int(params.get("pre_session_buffer_hours", 1)) if params else 1
    post = int(params.get("post_session_buffer_hours", 1)) if params else 1
    for _name, start, _end in _session_windows_from_params(params):
        for offset, direction in [(pre, -1), (post, 1)]:
            for buf_h in range(1, offset + 1):
                buf_start = (start + direction * buf_h) % 24
                buf_end = (start + direction * (buf_h - 1)) % 24
                if _hour_in_window(
                    hour,
                    buf_start,
                    buf_end if buf_end != buf_start else (buf_end + 1) % 24,
                ):
                    return True
    return False


def _latest_bar_time_utc(prepared: PreparedSymbol) -> datetime:
    frame = confirmed_pattern_frame(prepared.work_15m)
    if frame.height > 0 and "time" in frame.columns:
        last_bar_time = frame.item(-1, "time")
        if isinstance(last_bar_time, datetime):
            return (
                last_bar_time.replace(tzinfo=UTC)
                if last_bar_time.tzinfo is None
                else last_bar_time.astimezone(UTC)
            )
    return datetime.now(UTC)


__all__ = ["detect_session_killzone"]


def detect_session_killzone(
    prepared: PreparedSymbol,
    _settings: BotSettings,
    effective_params: dict[str, float],
    *,
    setup_id: str,
    family: str,
) -> Signal | None:
    defaults = effective_params
    base_score = _as_float(
        effective_params.get("base_score", defaults["base_score"]),
        defaults["base_score"],
    )
    min_volume_ratio = _as_float(
        effective_params.get("min_volume_ratio", defaults["min_volume_ratio"]),
        defaults["min_volume_ratio"],
    )
    sl_buffer_atr = _as_float(
        effective_params.get("sl_buffer_atr", defaults["sl_buffer_atr"]),
        defaults["sl_buffer_atr"],
    )
    min_rr = _as_float(effective_params.get("min_rr", defaults["min_rr"]), defaults["min_rr"])
    min_adx_1h = _as_float(
        effective_params.get("min_adx_1h", defaults["min_adx_1h"]),
        defaults["min_adx_1h"],
    )
    breakout_lookback = max(
        5,
        int(
            _as_float(
                effective_params.get(
                    "breakout_lookback_bars",
                    defaults["breakout_lookback_bars"],
                ),
                defaults["breakout_lookback_bars"],
            )
        ),
    )
    breakout_atr_mult = _as_float(
        effective_params.get("breakout_atr_mult", defaults["breakout_atr_mult"]),
        defaults["breakout_atr_mult"],
    )
    w = confirmed_pattern_frame(prepared.work_15m)
    if w.height < 20:
        _reject(prepared, setup_id, "insufficient_15m_bars", bars=w.height)
        return None
    if "time" not in w.columns:
        _reject(prepared, setup_id, "time_missing")
        return None
    now_utc = _latest_bar_time_utc(prepared)
    session_name = _active_killzone_name(now_utc, cast("dict[str, object]", effective_params))
    buffer_active = session_name is None and _is_in_buffer_zone(
        now_utc.hour, cast("dict[str, object]", effective_params)
    )
    if session_name is None and not buffer_active:
        _reject(
            prepared,
            setup_id,
            "schedule_inactive",
            stage="context",
            hour=now_utc.hour,
        )
        return None

    atr = _as_float(w.item(-1, "atr14"))
    if atr <= 0 or math.isnan(atr):
        _reject(prepared, setup_id, "atr_invalid", atr=atr)
        return None

    price = prepared.mark_price or prepared.universe.last_price
    if not price or price <= 0:
        _reject(prepared, setup_id, "price_missing")
        return None

    # ADX check on 1h
    w1h = confirmed_pattern_frame(prepared.work_1h)
    if w1h.height < 3:
        _reject(prepared, setup_id, "insufficient_1h_bars", bars=w1h.height)
        return None
    adx_1h = _as_float(w1h.item(-1, "adx14"))
    if adx_1h < min_adx_1h:
        _reject(
            prepared,
            setup_id,
            "adx_too_low",
            adx_1h=adx_1h,
            min_adx_1h=min_adx_1h,
        )
        return None

    # Last 3 bars directional check with volume
    last3 = w.tail(3)
    opens = last3["open"].to_numpy()
    closes = last3["close"].to_numpy()
    if "volume_ratio20" not in last3.columns:
        _reject(prepared, setup_id, "volume_ratio_missing")
        return None
    vol_ratios = last3["volume_ratio20"].to_numpy()

    if any(math.isnan(v) for v in vol_ratios):
        _reject(prepared, setup_id, "volume_ratio_nan")
        return None
    avg_vol_ratio = float(sum(vol_ratios) / len(vol_ratios))
    if avg_vol_ratio < min_volume_ratio:
        _reject(
            prepared,
            setup_id,
            "average_volume_too_low",
            avg_vol_ratio=avg_vol_ratio,
        )
        return None

    bullish_bars = sum(1 for o, c in zip(opens, closes, strict=False) if c > o)
    bearish_bars = sum(1 for o, c in zip(opens, closes, strict=False) if c < o)

    if bullish_bars >= 2:
        direction = "long"
    elif bearish_bars >= 2:
        direction = "short"
    else:
        _reject(prepared, setup_id, "directional_momentum_missing")
        return None

    penalty_reasons: list[str] = []
    close_position = _as_float(w.item(-1, "close_position"), 0.5)
    bar_high = _as_float(w.item(-1, "high"))
    bar_low = _as_float(w.item(-1, "low"))
    bar_close = _as_float(w.item(-1, "close"))
    range_len = min(breakout_lookback, w.height - 1)
    prior_range = w.slice(w.height - range_len - 1, range_len)
    prior_high = _as_float(prior_range["high"].max())
    prior_low = _as_float(prior_range["low"].min())
    breakout_buffer = max(0.0, atr * breakout_atr_mult)
    if direction == "long":
        breakout_ok = bar_close > prior_high + breakout_buffer or (
            bar_high > prior_high + breakout_buffer
            and bar_close > prior_high
            and close_position
            >= _as_float(
                effective_params.get(
                    "min_close_position_long",
                    defaults["min_close_position_long"],
                ),
                defaults["min_close_position_long"],
            )
        )
    else:
        breakout_ok = bar_close < prior_low - breakout_buffer or (
            bar_low < prior_low - breakout_buffer
            and bar_close < prior_low
            and close_position
            <= _as_float(
                effective_params.get(
                    "max_close_position_short",
                    defaults["max_close_position_short"],
                ),
                defaults["max_close_position_short"],
            )
        )
    if not breakout_ok:
        soft_accept = avg_vol_ratio >= min_volume_ratio * 1.15 and (
            (direction == "long" and close_position >= 0.55)
            or (direction == "short" and close_position <= 0.45)
        )
        if soft_accept:
            penalty_reasons.append("session_momentum_no_range_break_penalty")
        else:
            _reject(
                prepared,
                setup_id,
                "session_breakout_missing",
                direction=direction,
                prior_high=prior_high,
                prior_low=prior_low,
                close=bar_close,
                close_position=close_position,
            )
            return None

    orderflow_conflict, orderflow_details = _orderflow_conflicts(
        prepared,
        direction,
        max_adverse_depth=_as_float(
            effective_params.get(
                "max_adverse_depth_imbalance",
                defaults["max_adverse_depth_imbalance"],
            ),
            defaults["max_adverse_depth_imbalance"],
        ),
        max_adverse_micro=_as_float(
            effective_params.get(
                "max_adverse_microprice_bias",
                defaults["max_adverse_microprice_bias"],
            ),
            defaults["max_adverse_microprice_bias"],
        ),
    )
    if orderflow_conflict:
        penalty_reasons.append("orderflow_conflict_penalty")

    structure_conflict = False
    if (
        _as_float(
            effective_params.get(
                "strict_1h_structure",
                defaults["strict_1h_structure"],
            ),
            defaults["strict_1h_structure"],
        )
        > 0.0
    ):
        structure_1h = str(getattr(prepared, "structure_1h", "") or "")
        regime_1h = str(getattr(prepared, "regime_1h_confirmed", "") or "")
        if direction == "long" and (structure_1h == "downtrend" or regime_1h == "downtrend"):
            structure_conflict = True
        if direction == "short" and (structure_1h == "uptrend" or regime_1h == "uptrend"):
            structure_conflict = True
    if structure_conflict:
        penalty_reasons.append("structure_conflict_penalty")

    # --- Structural SL: beyond session high/low (killzone boundary) + configured ATR buffer ---
    scan20 = w.tail(20)
    session_high = _as_float(scan20["high"].max())
    session_low = _as_float(scan20["low"].min())
    entry_price = prior_high if direction == "long" else prior_low

    # Look for prior session levels from 1h data
    w1h = confirmed_pattern_frame(prepared.work_1h)

    if direction == "long":
        stop = session_low - atr * sl_buffer_atr
        risk = entry_price - stop
        if risk <= 0:
            _reject(
                prepared,
                setup_id,
                "risk_non_positive_long",
                stop=stop,
                price=entry_price,
            )
            return None
        # TP1: prior session's major level (previous 1h swing high or session high)
        tp1 = None
        if w1h.height > 5:
            sh_mask, sl_mask = _swing_points(w1h, n=3, include_unconfirmed_tail=True)
            sh_prices = w1h.filter(sh_mask)["high"]
            tp1_cands = sh_prices.filter(sh_prices > entry_price)
            tp1 = float(tp1_cands[0]) if tp1_cands.len() > 0 else None
        # TP2: next killzone range midpoint (above)
        killzone_range = session_high - session_low
        tp2 = session_high + killzone_range * 0.5 if killzone_range > 0 else None
    else:
        stop = session_high + atr * sl_buffer_atr
        risk = stop - entry_price
        if risk <= 0:
            _reject(
                prepared,
                setup_id,
                "risk_non_positive_short",
                stop=stop,
                price=entry_price,
            )
            return None
        # TP1: prior session's major level (previous 1h swing low)
        tp1 = None
        if w1h.height > 5:
            _, sl_mask = _swing_points(w1h, n=3, include_unconfirmed_tail=True)
            sl_prices = w1h.filter(sl_mask)["low"]
            tp1_cands = sl_prices.filter(sl_prices < entry_price)
            tp1 = float(tp1_cands[-1]) if tp1_cands.len() > 0 else None
        # TP2: next killzone range midpoint (below)
        killzone_range = session_high - session_low
        tp2 = session_low - killzone_range * 0.5 if killzone_range > 0 else None

    # Validate: TP1 must be at least 1.5x risk distance.
    # If structural target is missing/too close, use deterministic RR fallback
    # instead of dropping an otherwise valid setup.
    min_required = risk * min_rr
    if tp1 is None or abs(tp1 - entry_price) < min_required:
        tp1 = entry_price + min_required if direction == "long" else entry_price - min_required
        fallback_note = f"tp1_rr_fallback_{min_rr:.2f}"
    else:
        fallback_note = None
    if tp2 is None or abs(tp2 - entry_price) <= abs(tp1 - entry_price):
        tp2 = (
            entry_price + risk * max(2.0, min_rr + 0.35)
            if direction == "long"
            else entry_price - risk * max(2.0, min_rr + 0.35)
        )

    rsi = float(w.item(-1, "rsi14") or 50.0)
    vol_ratio = float(w.item(-1, "volume_ratio20") or 1.0)
    score = _compute_dynamic_score(
        direction=direction,
        base_score=base_score,
        vol_ratio=vol_ratio,
        rsi=rsi,
    )
    # Session quality multiplier: Overlap has the highest liquidity, Asia the lowest
    _SESSION_QUALITY: dict[str, float] = {
        "Overlap": 1.08,  # London + NY active simultaneously
        "NY": 1.04,  # Major institutional session
        "London": 1.04,  # Major institutional session
        "PreLondon": 0.98,  # Setup window before London open
        "NYClose": 0.93,  # Reduced liquidity, end of NY session
        "Asia": 0.88,  # Lower crypto futures liquidity
    }
    if session_name and session_name in _SESSION_QUALITY:
        score = min(1.0, score * _SESSION_QUALITY[session_name])

    if buffer_active:
        score *= _as_float(
            effective_params.get("buffer_zone_penalty", defaults["buffer_zone_penalty"]),
            defaults["buffer_zone_penalty"],
        )
        penalty_reasons.append("buffer_zone_penalty")

    session_label = session_name or "Buffer"
    reasons = [
        f"Session killzone {direction}: {session_label} {now_utc.strftime('%H:%M')}UTC",
        f"adx1h={adx_1h:.1f} avg_vol={avg_vol_ratio:.2f}",
        f"limit_entry={entry_price:.4f}",
        f"sl_buffer_atr={sl_buffer_atr:.2f}",
    ]
    reasons.extend(penalty_reasons)
    if fallback_note:
        reasons.append(fallback_note)
    if orderflow_details:
        reasons.append(" ".join(f"{key}={value:.3f}" for key, value in orderflow_details.items()))
    if orderflow_conflict:
        score *= _as_float(
            effective_params.get(
                "orderflow_conflict_penalty",
                defaults["orderflow_conflict_penalty"],
            ),
            defaults["orderflow_conflict_penalty"],
        )
    if structure_conflict:
        score *= _as_float(
            effective_params.get(
                "structure_conflict_penalty",
                defaults["structure_conflict_penalty"],
            ),
            defaults["structure_conflict_penalty"],
        )
    if "session_momentum_no_range_break_penalty" in penalty_reasons:
        score *= 0.90

    return _build_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=direction,
        score=score,
        timeframe="15m+1h",
        reasons=reasons,
        strategy_family=family,
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        price_anchor=entry_price,
        atr=atr,
    )


class SessionKillzoneSetup(RoadmapSetup):
    setup_id = "session_killzone"
    ENTRY_ORDER_TYPE: ClassVar[str] = "limit"
    family = "breakout"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)

    DEFAULTS: ClassVar[dict[str, float]] = {
        **RoadmapSetup.DEFAULTS,
        "base_score": 0.55,
        "min_volume_ratio": 1.0,
        "min_adx_1h": 14.0,
        "sl_buffer_atr": 0.75,
        "bias_mismatch_penalty": 0.75,
        "min_rr": 1.9,
        "breakout_lookback_bars": 20,
        "breakout_atr_mult": 0.05,
        "min_close_position_long": 0.58,
        "max_close_position_short": 0.42,
        "max_adverse_depth_imbalance": 0.10,
        "max_adverse_microprice_bias": 0.10,
        "strict_1h_structure": 0.0,
        "orderflow_conflict_penalty": 0.88,
        "structure_conflict_penalty": 0.82,
        "asia_start_hour_utc": 0,
        "asia_end_hour_utc": 3,
        "london_start_hour_utc": 7,
        "london_end_hour_utc": 10,
        "ny_start_hour_utc": 13,
        "ny_end_hour_utc": 17,
        "overlap_start_hour_utc": 13,
        "overlap_end_hour_utc": 16,
        "pre_london_start_hour_utc": 5,
        "pre_london_end_hour_utc": 7,
        "ny_close_start_hour_utc": 20,
        "ny_close_end_hour_utc": 22,
        "pre_session_buffer_hours": 1,
        "post_session_buffer_hours": 1,
        "buffer_zone_penalty": 0.88,
    }

    def active_session_name(
        self,
        prepared: PreparedSymbol,
        settings: BotSettings | None = None,
    ) -> str | None:
        params = self.get_optimizable_params(settings)
        dynamic_params = get_dynamic_params(prepared, self.setup_id)
        now_utc = _latest_bar_time_utc(prepared)
        return _active_killzone_name(
            now_utc,
            {**params, **dynamic_params},
        )

    def is_active_now(
        self,
        prepared: PreparedSymbol,
        settings: BotSettings | None = None,
    ) -> bool:
        return self.active_session_name(prepared, settings) is not None

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return detect_session_killzone(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["SessionKillzoneSetup"]
