"""Telegram message formatting for signal-only analytics delivery.

The runtime sends analytical plans, not orders. This module keeps that contract
visible in every Telegram payload and centralizes HTML escaping, length limits,
reason humanization, and preview validation.
"""

from __future__ import annotations

import html
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from bot.delivery.sizing import recommend_position_pct as _recommend_position_pct
from bot.policy.labels import INTERNAL_TRACKING_EVENT_RU, TRACKING_EVENT_RU
from engine.coercion import as_float as _float

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

TELEGRAM_TEXT_LIMIT = 4000
TELEGRAM_SAFE_TEXT_LIMIT = 3900
TELEGRAM_PARSE_MODE = "HTML"
ALLOWED_HTML_TAGS = {
    "b",
    "strong",
    "i",
    "em",
    "u",
    "ins",
    "s",
    "strike",
    "del",
    "span",
    "tg-spoiler",
    "a",
    "code",
    "pre",
    "blockquote",
}
LOCAL_TZ = datetime.now().astimezone().tzinfo or UTC


SETUP_LABELS: dict[str, str] = {
    "absorption": "Absorption",
    "aggression_shift": "Aggression shift",
    "altcoin_season_index": "Altcoin season",
    "atr_expansion": "ATR expansion",
    "bb_squeeze": "BB squeeze",
    "bos_choch": "BOS/CHoCH",
    "breaker_block": "Breaker block",
    "btc_correlation": "BTC correlation",
    "cvd_divergence": "CVD divergence",
    "depth_imbalance": "Depth imbalance",
    "ema_bounce": "EMA bounce",
    "funding_reversal": "Funding reversal",
    "fvg_setup": "FVG retest",
    "hidden_divergence": "Hidden divergence",
    "indicator_divergence": "Indicator divergence",
    "keltner_breakout": "Keltner breakout",
    "liquidation_heatmap": "Liquidation heatmap",
    "liquidity_sweep": "Liquidity sweep",
    "ls_ratio_extreme": "L/S ratio extreme",
    "multi_tf_trend": "Multi-TF trend",
    "oi_divergence": "OI divergence",
    "order_block": "Order block",
    "price_velocity": "Price velocity",
    "rsi_divergence_bottom": "RSI divergence",
    "session_killzone": "Session killzone",
    "spread_strategy": "Spread strategy",
    "squeeze_setup": "Squeeze release",
    "stop_hunt_detection": "Stop hunt",
    "structure_break_retest": "Break and retest",
    "structure_pullback": "Structure pullback",
    "supertrend_follow": "SuperTrend follow",
    "turtle_soup": "Turtle soup",
    "volume_anomaly": "Volume anomaly",
    "volume_climax_reversal": "Volume climax",
    "vwap_trend": "VWAP trend",
    "whale_walls": "Whale walls",
    "wick_trap_reversal": "Wick trap",
    "wyckoff_spring": "Wyckoff spring",
}


REASON_LABELS: dict[str, str] = {
    "adx_ok": "ADX supports trend",
    "adx_ranging_market_downgrade": "ADX treated as penalty in ranging regime",
    "atr_ok": "ATR passed volatility floor",
    "bb_squeeze_release": "squeeze release confirmed",
    "bos_confirmed": "break of structure confirmed",
    "bounce_confirmed": "bounce confirmation",
    "break_confirmed": "breakout confirmed",
    "breakout_confirmed": "breakout confirmed",
    "btc_bias_aligned": "BTC context aligned",
    "choch_confirmed": "CHoCH confirmed",
    "confluence_boost": "multi-setup confluence",
    "cvd_divergence": "CVD divergence",
    "depth_imbalance_ok": "order-book imbalance supports plan",
    "entry_zone_valid": "entry zone valid",
    "flow_aligned": "orderflow aligned",
    "fresh_15m": "15m data fresh",
    "fresh_1h": "1h data fresh",
    "fresh_4h": "4h data fresh",
    "funding_extreme": "funding crowding is extreme",
    "fvg_touched": "FVG zone touched",
    "liquidity_sweep": "liquidity sweep confirmed",
    "mark_price_ok": "mark/ticker price aligned",
    "market_regime_ok": "market regime supports setup",
    "multi_tf_aligned": "multi-timeframe context aligned",
    "order_block_held": "order block held",
    "pullback_to_level": "pullback into actionable level",
    "risk_reward_ok": "risk/reward passed",
    "score_ok": "confluence score passed",
    "spread_ok": "spread within limit",
    "structure_aligned": "market structure aligned",
    "supertrend_aligned": "SuperTrend aligned",
    "sweep_confirmed": "sweep and reclaim confirmed",
    "target_integrity_ok": "targets passed integrity check",
    "volume_confirmed": "volume confirmation",
    "volume_ok": "volume acceptable",
    "vwap_reclaim": "VWAP reclaim",
    "wick_closed_back": "wick trap closed back inside range",
    "wick_pierced": "liquidity wick pierced level",
}


INVALIDATION_BY_SETUP: dict[str, str] = {
    "absorption": "absorption low/high fails and price accepts beyond the stop zone",
    "aggression_shift": "aggressive flow flips against the plan after entry",
    "atr_expansion": "expansion candle loses range and closes back into prior compression",
    "bb_squeeze": "price closes back inside the squeeze range after release",
    "bos_choch": "price closes through the structural pivot used for the setup",
    "breaker_block": "breaker block is reclaimed against the planned direction",
    "cvd_divergence": "price makes a fresh extreme while CVD confirms against the setup",
    "depth_imbalance": "book imbalance and microprice flip against the direction",
    "ema_bounce": "body closes beyond the EMA acceptance zone",
    "funding_reversal": "funding crowding normalizes without price confirmation",
    "fvg_setup": "gap is fully mitigated and price accepts beyond the stop side",
    "hidden_divergence": "trend swing invalidates the divergence structure",
    "indicator_divergence": "indicator divergence is erased by a fresh confirmed extreme",
    "keltner_breakout": "breakout closes back inside the Keltner channel",
    "liquidation_heatmap": "liquidation proxy flips and price accepts through stop zone",
    "liquidity_sweep": "sweep level fails to reclaim and price continues through liquidity",
    "ls_ratio_extreme": "crowd positioning normalizes before price confirms reversal",
    "multi_tf_trend": "1h/4h trend context loses alignment",
    "oi_divergence": "OI and price reconnect without directional follow-through",
    "order_block": "order block boundary fails on closing basis",
    "price_velocity": "velocity candle mean-reverts below/above the trigger body",
    "rsi_divergence_bottom": "RSI divergence is invalidated by a fresh extreme",
    "session_killzone": "session range is lost after the killzone trigger",
    "spread_strategy": "spread widens beyond execution-quality threshold",
    "squeeze_setup": "release fails and closes back inside compression",
    "stop_hunt_detection": "stop-hunt level continues instead of reclaiming",
    "structure_break_retest": "breakout level fails on retest and closes back through it",
    "structure_pullback": "pullback leg closes beyond the protected swing",
    "supertrend_follow": "SuperTrend flips against the plan",
    "turtle_soup": "false breakout becomes a real breakout",
    "volume_anomaly": "volume impulse fails to hold candle acceptance",
    "volume_climax_reversal": "climax wick is absorbed and price accepts beyond it",
    "vwap_trend": "VWAP is lost and accepted against the plan",
    "whale_walls": "wall pressure disappears or flips against the signal",
    "wick_trap_reversal": "wick extreme is broken and accepted beyond stop",
    "wyckoff_spring": "range spring/upthrust fails to reclaim the range",
}


TRACKING_TITLES: dict[str, str] = dict(TRACKING_EVENT_RU)

# Legacy / internal - not sent to channel subscribers
_INTERNAL_TRACKING_TITLES: dict[str, str] = dict(INTERNAL_TRACKING_EVENT_RU)

LIFECYCLE_NOTE_RU: dict[str, str] = {
    "limit_zone_touched": "цена коснулась лимит-зоны - ждите закрытие 15m внутри зоны",
    "zone_not_touched": "зона не затронута",
    "trend_bar_confirm": "трендовое подтверждение свечи",
    "trend_bar_reject": "свеча не подтвердила тренд - ждём следующий 15m close",
    "breakout_accept": "принятие пробоя в зоне",
    "breakout_reject": "пробой не принят - ждём close",
    "reversal_confirm": "разворот подтверждён",
    "reversal_reject": "разворот не подтверждён",
    "close_outside_zone": "close вне зоны - вход не активирован",
}


@dataclass(frozen=True, slots=True)
class TelegramFormatPolicy:
    """Formatting knobs for Telegram signal messages."""

    text_limit: int = TELEGRAM_SAFE_TEXT_LIMIT
    include_disclaimer: bool = False
    include_chart_link: bool = True
    include_reason_limit: int = 0
    include_filter_limit: int = 0
    language: str = "ru"
    compact: bool = True
    include_position_size: bool = False


CHANNEL_SIGNAL_POLICY = TelegramFormatPolicy()


@dataclass(frozen=True, slots=True)
class TelegramValidationIssue:
    """One validation finding for an outgoing Telegram message."""

    severity: str
    code: str
    message: str
    offset: int | None = None


@dataclass(frozen=True, slots=True)
class TelegramValidationReport:
    """Validation report for rendered Telegram HTML."""

    ok: bool
    length: int
    issues: tuple[TelegramValidationIssue, ...] = ()

    @property
    def error_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == "warning")


@dataclass(slots=True)
class SignalMessageFacts:
    """Normalized facts extracted from a signal-like object."""

    symbol: str
    direction: str
    setup_id: str
    timeframe: str
    tracking_ref: str
    score: float
    entry_low: float
    entry_high: float
    stop: float
    take_profit_1: float
    take_profit_2: float
    take_profit_3: float | None
    risk_reward: float
    stop_distance_pct: float
    valid_until: datetime | None
    risk_reward_tp1: float | None = None
    risk_reward_tp2: float | None = None
    risk_reward_tp3: float | None = None
    weighted_risk_reward: float | None = None
    reasons: tuple[str, ...] = ()
    passed_filters: tuple[str, ...] = ()
    scale_weights: tuple[float, float, float] = (0.5, 0.3, 0.2)
    atr_pct: float | None = None
    spread_bps: float | None = None
    adx_1h: float | None = None
    volume_ratio: float | None = None
    oi_change_pct: float | None = None
    funding_rate: float | None = None
    orderflow_delta_ratio: float | None = None
    mark_price: float | None = None
    premium_zscore_5m: float | None = None
    premium_slope_5m: float | None = None
    ls_ratio: float | None = None
    microstructure_bias_score: float | None = None
    microstructure_confidence: float | None = None
    microstructure_label: str | None = None
    microstructure_reason: str | None = None
    microstructure_warnings: tuple[str, ...] = ()
    entry_order_type: str = "limit"
    bias_4h: str | None = None
    bias_1h: str | None = None
    market_regime: str | None = None
    btc_bias: str | None = None
    eth_bias: str | None = None
    sol_bias: str | None = None
    xau_bias: str | None = None
    xag_bias: str | None = None
    pax_bias: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    position_size_pct: float | None = None


def escape_text(value: object) -> str:
    """Escape text for Telegram HTML."""
    return html.escape(str(value if value is not None else ""), quote=True)


def code(value: object) -> str:
    """Render escaped text in a Telegram ``code`` tag."""
    return f"<code>{escape_text(value)}</code>"


def bold(value: object) -> str:
    """Render escaped text in a Telegram ``b`` tag."""
    return f"<b>{escape_text(value)}</b>"


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    numeric = _float(value, default=math.nan)
    return None if not math.isfinite(numeric) else numeric


def format_price(value: float | None) -> str:
    """Format a price compactly without losing low-price precision."""
    if value is None:
        return "n/a"
    numeric = _float(value)
    if abs(numeric) >= 10_000:
        return f"{numeric:,.1f}"
    if abs(numeric) >= 1_000:
        return f"{numeric:,.2f}"
    if abs(numeric) >= 10:
        return f"{numeric:,.3f}"
    if abs(numeric) >= 1:
        return f"{numeric:,.4f}"
    if abs(numeric) >= 0.01:
        return f"{numeric:.5f}"
    return f"{numeric:.8f}"


def format_percent(value: float | None, *, multiply: bool = False, digits: int = 2) -> str:
    """Format a percentage value."""
    if value is None:
        return "n/a"
    numeric = _float(value)
    if multiply:
        numeric *= 100.0
    sign = "+" if numeric > 0 else ""
    return f"{sign}{numeric:.{digits}f}%"


def format_score(score: float | None) -> str:
    """Format score in percent."""
    value = max(0.0, min(_float(score), 1.0))
    return f"{value * 100:.0f}%"


def confidence_label(score: float | None) -> str:
    """Return compact qualitative score label."""
    value = _float(score)
    if value >= 0.78:
        return "high"
    if value >= 0.66:
        return "medium"
    if value >= 0.55:
        return "moderate"
    return "low"


def direction_label(direction: str) -> str:
    """Normalize a signal direction label."""
    normalized = str(direction or "").strip().lower()
    if normalized == "short":
        return "SHORT"
    return "LONG"


def direction_side(direction: str) -> str:
    """Return plain action side, still signal-only."""
    return "sell setup" if direction_label(direction) == "SHORT" else "buy setup"


def setup_label(setup_id: str) -> str:
    """Return a human label for a setup id."""
    normalized = str(setup_id or "").strip()
    return SETUP_LABELS.get(normalized, normalized.replace("_", " ").title())


def parse_datetime(value: object) -> datetime | None:
    """Parse a datetime-like value into timezone-aware UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except TypeError, ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def format_datetime(value: object) -> str:
    """Format a datetime-like value in local timezone with offset."""
    parsed = parse_datetime(value)
    if parsed is None:
        return "n/a"
    local = parsed.astimezone(LOCAL_TZ)
    offset = local.utcoffset() or timedelta(0)
    sign = "+" if offset >= timedelta(0) else "-"
    minutes = abs(int(offset.total_seconds() // 60))
    hours, mins = divmod(minutes, 60)
    return f"{local:%Y-%m-%d %H:%M} UTC{sign}{hours:02d}:{mins:02d}"


def minutes_until(value: object) -> float | None:
    """Return minutes from now until a timestamp."""
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    return max(0.0, (parsed - datetime.now(UTC)).total_seconds() / 60.0)


def tradingview_interval(timeframe: str) -> str:
    """Convert bot timeframe labels to TradingView interval labels."""
    raw = str(timeframe or "").strip().lower()
    mapping = {
        "1m": "1",
        "3m": "3",
        "5m": "5",
        "15m": "15",
        "30m": "30",
        "45m": "45",
        "1h": "60",
        "2h": "120",
        "4h": "240",
        "1d": "1D",
    }
    if raw in mapping:
        return mapping[raw]
    for key, value in mapping.items():
        if key in raw:
            return value
    return "15"


def tradingview_chart_url(symbol: str, timeframe: str) -> str:
    """Return a TradingView chart URL for Binance perpetual futures."""
    safe_symbol = re.sub(r"[^A-Z0-9]", "", str(symbol or "").upper())
    return (
        "https://www.tradingview.com/chart/"
        f"?symbol=BINANCE:{safe_symbol}.P&interval={tradingview_interval(timeframe)}"
    )


def reason_label(reason: str) -> str:
    """Humanize a reason token."""
    raw = str(reason or "").strip()
    if not raw:
        return ""
    if raw in REASON_LABELS:
        return REASON_LABELS[raw]
    if raw.startswith("confluence_") and raw.endswith("_setups"):
        count = raw.removeprefix("confluence_").removesuffix("_setups")
        return f"{count} setup confluence" if count.isdigit() else "setup confluence"
    if raw.startswith("confluence_setups="):
        setups = [
            setup_label(item.strip()) for item in raw.split("=", 1)[1].split(",") if item.strip()
        ]
        return "confluence: " + ", ".join(setups[:5]) if setups else "setup confluence"
    return raw.replace("_", " ").replace(".", ": ")


def _primary_timeframe_fallback_badge(
    passed_filters: tuple[str, ...], *, actual_timeframe: str
) -> str | None:
    """Return a compact badge when prepared primary differs from configured."""
    for item in passed_filters:
        if item == "primary_timeframe_fallback":
            return "TF fallback"
        if item.startswith("primary_timeframe_fallback:"):
            configured = item.split(":", 1)[1].strip()
            if configured and configured != actual_timeframe:
                return f"{configured}→{actual_timeframe}"
    return None


def compact_reason_list(reasons: Iterable[str], *, limit: int) -> list[str]:
    """Return unique human-readable reasons."""
    seen: set[str] = set()
    out: list[str] = []
    for reason in reasons:
        label = reason_label(reason)
        if not label or label in seen:
            continue
        seen.add(label)
        out.append(label)
        if len(out) >= limit:
            break
    return out


def extract_signal_facts(
    signal: Any,
    *,
    pending_expiry_minutes: int | None = None,
    btc_bias: str | None = None,
    eth_bias: str | None = None,
) -> SignalMessageFacts:
    """Normalize a signal-like object for message rendering."""
    created_at = parse_datetime(getattr(signal, "created_at", None)) or datetime.now(UTC)
    valid_until = parse_datetime(getattr(signal, "valid_until", None))
    if valid_until is None and pending_expiry_minutes is not None:
        valid_until = created_at + timedelta(minutes=max(1, int(pending_expiry_minutes)))
    scale = getattr(signal, "scale_weights", (0.5, 0.3, 0.2))
    try:
        scale_tuple = tuple(float(item) for item in scale)
    except TypeError:
        scale_tuple = (0.5, 0.3, 0.2)
    if len(scale_tuple) != 3:
        scale_tuple = (0.5, 0.3, 0.2)
    entry_low = _float(getattr(signal, "entry_low", getattr(signal, "entry_price", 0.0)))
    entry_high = _float(getattr(signal, "entry_high", getattr(signal, "entry_price", 0.0)))
    stop = _float(getattr(signal, "stop", getattr(signal, "stop_price", 0.0)))
    take_profit_1 = _float(getattr(signal, "take_profit_1", getattr(signal, "tp1_price", 0.0)))
    take_profit_2 = _float(getattr(signal, "take_profit_2", getattr(signal, "tp2_price", 0.0)))
    take_profit_3 = _optional_float(getattr(signal, "take_profit_3", getattr(signal, "tp3", None)))
    entry_mid = (entry_low + entry_high) / 2.0
    risk = abs(entry_mid - stop)
    rr1 = _optional_float(getattr(signal, "risk_reward_tp1", None))
    rr2 = _optional_float(getattr(signal, "risk_reward_tp2", None))
    rr3 = _optional_float(getattr(signal, "risk_reward_tp3", None))
    tp3 = take_profit_3 if take_profit_3 is not None else take_profit_2
    if risk > 0.0:
        if rr1 is None:
            rr1 = abs(take_profit_1 - entry_mid) / risk
        if rr2 is None:
            rr2 = abs(take_profit_2 - entry_mid) / risk
        if rr3 is None:
            rr3 = abs(tp3 - entry_mid) / risk
    weighted_rr = _optional_float(getattr(signal, "weighted_risk_reward", None))
    if weighted_rr is None and rr1 is not None and rr2 is not None and rr3 is not None:
        weighted_rr = scale_tuple[0] * rr1 + scale_tuple[1] * rr2 + scale_tuple[2] * rr3
    return SignalMessageFacts(
        symbol=str(getattr(signal, "symbol", "")),
        direction=str(getattr(signal, "direction", "long")),
        setup_id=str(getattr(signal, "setup_id", "")),
        timeframe=str(getattr(signal, "timeframe", "15m")),
        tracking_ref=str(getattr(signal, "tracking_ref", "") or getattr(signal, "tracking_id", "")),
        score=_float(getattr(signal, "score", 0.0)),
        entry_low=entry_low,
        entry_high=entry_high,
        stop=stop,
        take_profit_1=take_profit_1,
        take_profit_2=take_profit_2,
        take_profit_3=take_profit_3,
        risk_reward=_float(getattr(signal, "risk_reward", 0.0)),
        risk_reward_tp1=rr1,
        risk_reward_tp2=rr2,
        risk_reward_tp3=rr3,
        weighted_risk_reward=weighted_rr,
        stop_distance_pct=_float(getattr(signal, "stop_distance_pct", 0.0)),
        valid_until=valid_until,
        reasons=tuple(str(item) for item in getattr(signal, "reasons", ()) or ()),
        passed_filters=tuple(str(item) for item in getattr(signal, "passed_filters", ()) or ()),
        scale_weights=scale_tuple,
        atr_pct=_optional_float(getattr(signal, "atr_pct", None)),
        spread_bps=_optional_float(getattr(signal, "spread_bps", None)),
        adx_1h=_optional_float(getattr(signal, "adx_1h", None)),
        volume_ratio=_optional_float(getattr(signal, "volume_ratio", None)),
        oi_change_pct=_optional_float(getattr(signal, "oi_change_pct", None)),
        funding_rate=_optional_float(getattr(signal, "funding_rate", None)),
        orderflow_delta_ratio=_optional_float(getattr(signal, "orderflow_delta_ratio", None)),
        mark_price=_optional_float(getattr(signal, "mark_price", None)),
        premium_zscore_5m=_optional_float(getattr(signal, "premium_zscore_5m", None)),
        premium_slope_5m=_optional_float(getattr(signal, "premium_slope_5m", None)),
        ls_ratio=_optional_float(getattr(signal, "ls_ratio", None)),
        entry_order_type=str(getattr(signal, "entry_order_type", "limit") or "limit")
        .strip()
        .lower(),
        bias_4h=str(getattr(signal, "bias_4h", None) or "") or None,
        bias_1h=str(getattr(signal, "bias_1h", None) or "") or None,
        market_regime=str(getattr(signal, "market_regime", None) or "") or None,
        microstructure_bias_score=_optional_float(
            getattr(signal, "microstructure_bias_score", None)
        ),
        microstructure_confidence=_optional_float(
            getattr(signal, "microstructure_confidence", None)
        ),
        microstructure_label=getattr(signal, "microstructure_label", None),
        microstructure_reason=getattr(signal, "microstructure_reason", None),
        microstructure_warnings=tuple(
            str(item) for item in getattr(signal, "microstructure_warnings", ()) or ()
        ),
        btc_bias=btc_bias or getattr(signal, "btc_bias", None),
        eth_bias=eth_bias or getattr(signal, "eth_bias", None),
        sol_bias=getattr(signal, "sol_bias", None),
        xau_bias=getattr(signal, "xau_bias", None),
        xag_bias=getattr(signal, "xag_bias", None),
        pax_bias=getattr(signal, "pax_bias", None),
        created_at=created_at,
    )


def mtf_conflict_label(facts: SignalMessageFacts) -> str | None:
    """answers50 Q35: surface HTF bias conflict on delivered signals."""
    direction = direction_label(facts.direction)
    bias_4h = str(facts.bias_4h or "neutral").strip().lower()
    if direction == "LONG" and bias_4h == "downtrend":
        return "CONFLICTED (BEAR_BIAS)"
    if direction == "SHORT" and bias_4h == "uptrend":
        return "CONFLICTED (BULL_BIAS)"
    btc = str(facts.btc_bias or "").strip().lower()
    if direction == "LONG" and btc in {"downtrend", "bear"}:
        return "CONFLICTED (BEAR_BIAS)"
    if direction == "SHORT" and btc in {"uptrend", "bull"}:
        return "CONFLICTED (BULL_BIAS)"
    return None


def market_context_lines(facts: SignalMessageFacts) -> list[str]:
    """Build compact context lines for the signal."""
    items: list[str] = []
    if facts.atr_pct is not None:
        items.append(f"ATR {facts.atr_pct:.2f}%")
    if facts.spread_bps is not None:
        items.append(f"spread {facts.spread_bps:.1f} bps")
    if facts.adx_1h is not None:
        items.append(f"ADX1h {facts.adx_1h:.1f}")
    if facts.volume_ratio is not None:
        items.append(f"vol {facts.volume_ratio:.2f}x")
    if facts.oi_change_pct is not None:
        items.append(f"OI {format_percent(facts.oi_change_pct, multiply=True, digits=2)}")
    if facts.funding_rate is not None:
        items.append(f"funding {format_percent(facts.funding_rate, multiply=True, digits=4)}")
    if facts.ls_ratio is not None:
        items.append(f"L/S {facts.ls_ratio:.2f}")
    if facts.premium_zscore_5m is not None:
        items.append(f"premium z {facts.premium_zscore_5m:.2f}")
    if facts.premium_slope_5m is not None:
        items.append(f"basis slope {facts.premium_slope_5m:+.4f}")
    if facts.orderflow_delta_ratio is not None:
        items.append(f"flow {facts.orderflow_delta_ratio:.2f}")
    if facts.microstructure_bias_score is not None:
        label = facts.microstructure_label or "mixed"
        confidence = facts.microstructure_confidence or 0.0
        items.append(f"micro {label} {facts.microstructure_bias_score:+.2f}/{confidence:.2f}")
    if facts.btc_bias and facts.btc_bias != "neutral":
        items.append(f"BTC {facts.btc_bias}")
    for label, value in (
        ("ETH", facts.eth_bias),
        ("SOL", facts.sol_bias),
        ("XAU", facts.xau_bias),
        ("XAG", facts.xag_bias),
        ("PAXG", facts.pax_bias),
    ):
        if value and value != "neutral":
            items.append(f"{label} {value}")
    return items


def invalidation_text(facts: SignalMessageFacts) -> str:
    """Return setup-specific invalidation text."""
    return INVALIDATION_BY_SETUP.get(facts.setup_id, "stop zone is accepted by the market")


def status_line_for_signal(facts: SignalMessageFacts) -> str:
    """Return the limit-order waiting status line."""
    remaining = minutes_until(facts.valid_until)
    if remaining is None:
        return "лимит в зоне · SL/TP после исполнения"
    if remaining <= 0:
        return "срок лимита истёк"
    return f"лимит в зоне · ждём до {remaining:.0f} мин"


def _channel_header(facts: SignalMessageFacts, *, tier: str | None = None) -> str:
    is_long = direction_label(facts.direction) == "LONG"
    badge = "🟢" if is_long else "🔴"
    ref = code("#" + facts.tracking_ref)
    tier_badge = _tier_badge(tier)
    return (
        f"{tier_badge} {badge} <b>{direction_label(facts.direction)} {code(facts.symbol)}</b> {ref}"
    )


def _tier_badge(tier: str | None) -> str:
    normalized = str(tier or "action").strip().lower()
    label = "WATCH" if normalized == "watch" else "ACTION"
    return code(f"[{label}]")


def _channel_rr_line(facts: SignalMessageFacts) -> str:
    tp3 = facts.take_profit_3 if facts.take_profit_3 is not None else facts.take_profit_2
    same_tp = (
        abs(facts.take_profit_2 - facts.take_profit_1) <= max(abs(facts.take_profit_1), 1.0) * 1e-8
    )
    same_tp = (
        same_tp and abs(tp3 - facts.take_profit_2) <= max(abs(facts.take_profit_2), 1.0) * 1e-8
    )
    risk_pct = code(f"{facts.stop_distance_pct:.1f}%")
    if same_tp:
        rr_value = facts.weighted_risk_reward or facts.risk_reward_tp1 or facts.risk_reward
        return f"RR {code(f'{rr_value:.1f}')} · риск {risk_pct}"
    rr1 = facts.risk_reward_tp1 if facts.risk_reward_tp1 is not None else facts.risk_reward
    rr2 = facts.risk_reward_tp2 if facts.risk_reward_tp2 is not None else rr1
    rr3 = facts.risk_reward_tp3 if facts.risk_reward_tp3 is not None else rr2
    return (
        f"RR1 {code(f'{rr1:.1f}')} · RR2 {code(f'{rr2:.1f}')} · "
        f"RR3 {code(f'{rr3:.1f}')} · риск {risk_pct}"
    )


def _tp_equality_tolerance(price: float) -> float:
    return max(abs(price) * 1e-6, 1e-8)


def _channel_legs_line(facts: SignalMessageFacts) -> str:
    if str(getattr(facts, "entry_order_type", "limit") or "limit").strip().lower() == "market":
        ref = (
            facts.mark_price
            if facts.mark_price and facts.mark_price > 0.0
            else (facts.entry_low + facts.entry_high) / 2.0
        )
        entry = f"ref @ {format_price(ref)}"
    else:
        entry = f"{format_price(facts.entry_low)}-{format_price(facts.entry_high)}"
    tp3 = facts.take_profit_3 if facts.take_profit_3 is not None else facts.take_profit_2
    same_tp = abs(facts.take_profit_2 - facts.take_profit_1) <= _tp_equality_tolerance(
        facts.take_profit_1
    )
    if same_tp:
        targets = f"TP {code(format_price(facts.take_profit_1))}"
    else:
        targets = (
            f"TP1 {code(format_price(facts.take_profit_1))} · "
            f"TP2 {code(format_price(facts.take_profit_2))} · "
            f"TP3 {code(format_price(tp3))}"
        )
    return f"Вход {code(entry)} · SL {code(format_price(facts.stop))} · {targets}"


def manual_entry_skip_hint(symbol: str, *, chase_pct: float = 0.002) -> str:
    """Late-entry guidance for manual channel subscribers (signal-only)."""
    majors = frozenset({"BTCUSDT", "ETHUSDT", "XRPUSDT", "BNBUSDT"})
    metals = frozenset({"XAUUSDT", "XAGUSDT", "PAXGUSDT"})
    sym = str(symbol or "").strip().upper()
    if sym in metals:
        pct = max(chase_pct, 0.005)
    elif sym in majors:
        pct = chase_pct
    else:
        pct = max(chase_pct, 0.004)
    return f"Пропустить, если цена ушла >{pct * 100:.2f}% от зоны входа"


def format_channel_trade_card(
    facts: SignalMessageFacts,
    *,
    status_line: str | None = None,
    include_chart: bool = True,
    tier: str | None = None,
    position_size_pct: float | None = None,
    chase_pct: float | None = None,
) -> str:
    """Unified compact card for Telegram channel (new + edited).

    IV.21: includes invalidation hint.
    IV.22: includes market context (ADX, regime, bias).
    """
    setup_line = f"{escape_text(setup_label(facts.setup_id))} · {code(facts.timeframe)}"
    fallback_badge = _primary_timeframe_fallback_badge(
        facts.passed_filters,
        actual_timeframe=facts.timeframe,
    )
    if fallback_badge:
        setup_line += f" · {code(fallback_badge)}"
    conflict = mtf_conflict_label(facts)
    if conflict:
        setup_line += f" · {code(conflict)}"
    setup_line += f" · {code(format_score(facts.score))}"
    if str(getattr(facts, "entry_order_type", "limit") or "limit").strip().lower() == "market":
        setup_line += f" · {code('MARKET')}"
    lines = [
        _channel_header(facts, tier=tier),
        setup_line,
        _channel_legs_line(facts),
        _channel_rr_line(facts),
    ]
    if position_size_pct is not None:
        lines.append(f"size {code(f'{position_size_pct:.1f}%')}")

    context = market_context_lines(facts)
    if context:
        lines.append(f"ctx {code(' | '.join(context[:4]))}")

    lines.append(f"inv {code(invalidation_text(facts))}")
    lines.append(escape_text(status_line or status_line_for_signal(facts)))
    lines.append(
        f"<i>{escape_text(manual_entry_skip_hint(facts.symbol, chase_pct=chase_pct or 0.002))}</i>"
    )
    if include_chart:
        chart = html.escape(tradingview_chart_url(facts.symbol, facts.timeframe), quote=True)
        lines.append(f'<a href="{chart}">TradingView</a>')
    return "\n".join(lines)


def target_line(facts: SignalMessageFacts) -> str:
    """Render target plan."""
    tp3 = facts.take_profit_3 if facts.take_profit_3 is not None else facts.take_profit_2
    same_tp = abs(facts.take_profit_2 - facts.take_profit_1) <= _tp_equality_tolerance(
        facts.take_profit_1
    )
    if same_tp:
        return f"TP {code(format_price(facts.take_profit_1))}"
    weights = [round(max(0.0, weight) * 100.0) for weight in facts.scale_weights]
    return (
        f"TP1 {code(format_price(facts.take_profit_1))} "
        f"TP2 {code(format_price(facts.take_profit_2))} "
        f"TP3 {code(format_price(tp3))} "
        f"scale {code(f'{weights[0]}/{weights[1]}/{weights[2]}%')}"
    )


def entry_levels_line(facts: SignalMessageFacts) -> str:
    """Render DCA-compatible limit entry levels with explicit size shares."""
    weights = [round(max(0.0, weight) * 100.0) for weight in facts.scale_weights]
    mid = (facts.entry_low + facts.entry_high) / 2.0
    if direction_label(facts.direction) == "SHORT":
        levels = [facts.entry_high, mid, facts.entry_low]
    else:
        levels = [facts.entry_low, mid, facts.entry_high]
    return (
        f"E1 {code(format_price(levels[0]))} {code(str(weights[0]) + '%')} "
        f"E2 {code(format_price(levels[1]))} {code(str(weights[1]) + '%')} "
        f"E3 {code(format_price(levels[2]))} {code(str(weights[2]) + '%')}"
    )


def validate_telegram_html(
    text: str, *, limit: int = TELEGRAM_TEXT_LIMIT
) -> TelegramValidationReport:
    """Validate the subset of Telegram HTML used by this formatter."""
    issues: list[TelegramValidationIssue] = []
    if len(text) > limit:
        issues.append(
            TelegramValidationIssue(
                "error",
                "message_too_long",
                f"message has {len(text)} chars, Telegram limit is {limit}",
            )
        )
    for match in re.finditer(r"<(/?)([a-zA-Z0-9-]+)(?:\\s+[^>]*)?>", text):
        tag = match.group(2).lower()
        if tag not in ALLOWED_HTML_TAGS:
            issues.append(
                TelegramValidationIssue(
                    "error",
                    "unsupported_tag",
                    f"unsupported Telegram HTML tag: {tag}",
                    match.start(),
                )
            )
    raw_entity = re.search(r"&(?!amp;|lt;|gt;|quot;|#\\d+;|#x[0-9A-Fa-f]+;)", text)
    if raw_entity:
        issues.append(
            TelegramValidationIssue(
                "warning",
                "raw_ampersand",
                "raw ampersand may break Telegram HTML parsing",
                raw_entity.start(),
            )
        )
    return TelegramValidationReport(
        ok=not any(issue.severity == "error" for issue in issues),
        length=len(text),
        issues=tuple(issues),
    )


def truncate_preserving_footer(text: str, *, limit: int = TELEGRAM_SAFE_TEXT_LIMIT) -> str:
    """Trim a Telegram message while preserving the signal-only footer."""
    if len(text) <= limit:
        return text
    footer = "\n<i>Signal-only analytics. No auto-trading.</i>"
    room = max(100, limit - len(footer) - 12)
    return text[:room].rstrip() + "\n...\n" + footer


def format_safe_signal_fallback(
    signal: Any,
    *,
    pending_expiry_minutes: int,
    tier: str | None = None,
) -> str:
    """Minimal valid Telegram HTML when primary render fails validation."""
    facts = extract_signal_facts(signal, pending_expiry_minutes=pending_expiry_minutes)
    tier_badge = _tier_badge(tier)
    direction = direction_label(facts.direction)
    ref = code("#" + facts.tracking_ref) if facts.tracking_ref else ""
    lines = [
        f"{tier_badge} <b>{direction} {code(facts.symbol)}</b> {ref}".strip(),
        (
            f"{escape_text(setup_label(facts.setup_id))} · "
            f"{code(facts.timeframe)} · {code(format_score(facts.score))}"
        ),
        (f"SL {code(format_price(facts.stop))} · TP {code(format_price(facts.take_profit_1))}"),
        escape_text("Signal-only analytics. Manual entry."),
    ]
    return "\n".join(lines)


def format_signal_message(
    signal: Any,
    *,
    pending_expiry_minutes: int,
    btc_bias: str | None = None,
    eth_bias: str | None = None,
    policy: TelegramFormatPolicy | None = None,
    tier: str | None = None,
    chase_pct: float | None = None,
) -> str:
    """Render the main Telegram signal message (compact channel card)."""
    policy = policy or CHANNEL_SIGNAL_POLICY
    facts = extract_signal_facts(
        signal,
        pending_expiry_minutes=pending_expiry_minutes,
        btc_bias=btc_bias,
        eth_bias=eth_bias,
    )
    if not policy.compact:
        reasons = compact_reason_list(facts.reasons, limit=policy.include_reason_limit)
        context = market_context_lines(facts)
        lines = [
            (
                f"{bold('SIGNAL-ONLY PLAN')} {code(facts.symbol)} "
                f"{code(direction_label(facts.direction))}"
            ),
            (
                f"{bold(setup_label(facts.setup_id))} {code(facts.timeframe)} | "
                f"score {code(format_score(facts.score))}"
            ),
            f"{bold('Entries')} {entry_levels_line(facts)}",
            f"{bold('Stop')} {code(format_price(facts.stop))}",
            f"{bold('Targets')} {target_line(facts)}",
        ]
        if reasons:
            lines.append(f"{bold('Why')} " + "; ".join(escape_text(item) for item in reasons))
        if context:
            lines.append(f"{bold('Context')} {code(' | '.join(context))}")
        if policy.include_disclaimer:
            lines.append("<i>Signal-only analytics. No auto-trading.</i>")
        rendered = "\n".join(lines)
        return truncate_preserving_footer(rendered, limit=policy.text_limit)

    position_size_pct = None
    if policy.include_position_size:
        try:
            pct = _recommend_position_pct(signal, None)
            position_size_pct = pct if pct > 0.0 else None
        except TypeError, ValueError, AttributeError:
            pass

    rendered = format_channel_trade_card(
        facts,
        include_chart=policy.include_chart_link,
        tier=tier,
        position_size_pct=position_size_pct,
        chase_pct=chase_pct,
    )
    return truncate_preserving_footer(rendered, limit=policy.text_limit)


def tracking_status_text(state: Any) -> str:
    """Render the state line for tracked signal cards."""
    close_reason = getattr(state, "close_reason", None)
    close_price = getattr(state, "close_price", None)
    activated_at = getattr(state, "activated_at", None)
    activation_price = getattr(state, "activation_price", None)
    pending_expires_at = getattr(state, "pending_expires_at", None)
    tp1_hit_at = getattr(state, "tp1_hit_at", None)
    if close_reason == "breakeven_stop":
        be_px = format_price(_optional_float(close_price))
        if tp1_hit_at:
            return f"✅ TP1 взят → BE @ {be_px}"
        return f"⚖️ безубыток @ {be_px}"
    if close_reason == "stop_loss":
        return f"🛑 стоп @ {format_price(_optional_float(close_price))}"
    if close_reason in {"tp1_hit", "tp2_hit"}:
        return f"🎯 цель @ {format_price(_optional_float(close_price))}"
    if close_reason == "expired":
        return "⌛ лимит не исполнен · срок истёк"
    if close_reason:
        return str(close_reason).replace("_", " ")
    if activated_at:
        act_px = format_price(_optional_float(activation_price))
        return f"✅ в сделке @ {act_px}"
    if tp1_hit_at:
        return "🎯 TP1 · TP2 открыт"
    return f"⏳ лимит · до {format_datetime(pending_expires_at)}"


def format_tracked_signal_message(tracked: Any) -> str:
    """Render an editable tracked signal card (same layout as new signals)."""
    state = getattr(tracked, "tracked", tracked)
    facts = extract_signal_facts(state, pending_expiry_minutes=None)
    status = tracking_status_text(state)
    return truncate_preserving_footer(
        format_channel_trade_card(
            facts, status_line=status, include_chart=True, position_size_pct=None
        )
    )


def format_tracking_event_message(event: Any) -> str:
    """Short channel reply on TP/SL (card edit carries full state)."""
    tracked = getattr(event, "tracked", None)
    if tracked is None:
        return "<b>Обновление</b>"
    event_type = str(getattr(event, "event_type", "update"))
    price = format_price(_optional_float(getattr(event, "event_price", None)))
    ref = code("#" + str(getattr(tracked, "tracking_ref", "")))
    sym = code(getattr(tracked, "symbol", ""))
    title = TRACKING_TITLES.get(event_type, event_type.replace("_", " ").upper())
    if event_type == "activated":
        return f"✅ <b>{title}</b> {sym} {ref} @ {code(price)}"
    if event_type == "tp1_hit":
        return f"🎯 <b>{title}</b> {sym} {ref} @ {code(price)}"
    if event_type == "tp2_hit":
        return f"🎯 <b>{title}</b> {sym} {ref} @ {code(price)}"
    if event_type in {"stop_loss", "breakeven_stop"}:
        tp1_hit_at = getattr(tracked, "tp1_hit_at", None)
        if event_type == "breakeven_stop" and tp1_hit_at:
            return f"✅ <b>TP1 взят → BE</b> {sym} {ref} @ {code(price)}"
        icon = "⚖️" if event_type == "breakeven_stop" else "🛑"
        return f"{icon} <b>{title}</b> {sym} {ref} @ {code(price)}"
    if event_type == "expired":
        return f"⌛ <b>{title}</b> {sym} {ref}"
    return f"<b>{escape_text(title)}</b> {sym} {ref} @ {code(price)}"


def format_analytics_companion_message(
    signal: Any,
    *,
    btc_bias: str | None = None,
    eth_bias: str | None = None,
    policy: TelegramFormatPolicy | None = None,
) -> str:
    """Render optional explanatory companion text."""
    policy = policy or TelegramFormatPolicy(text_limit=1800, compact=True)
    facts = extract_signal_facts(
        signal,
        pending_expiry_minutes=None,
        btc_bias=btc_bias,
        eth_bias=eth_bias,
    )
    reasons = compact_reason_list(facts.reasons, limit=8)
    lines = [
        f"{bold('WHY THIS SIGNAL')} {code(facts.symbol)} {code(direction_label(facts.direction))}",
        f"{bold('Setup')} {escape_text(setup_label(facts.setup_id))}",
    ]
    if reasons:
        for idx, reason in enumerate(reasons, start=1):
            lines.append(f"{idx}. {escape_text(reason)}")
    context = market_context_lines(facts)
    if context:
        lines.append(f"{bold('Market context')} {code(' | '.join(context))}")
    if facts.microstructure_reason:
        lines.append(f"{bold('Microstructure')} {escape_text(facts.microstructure_reason)}")
    lines.append(f"{bold('Invalidation')} {escape_text(invalidation_text(facts))}")
    lines.append("<i>Signal-only analytics. No auto-trading.</i>")
    return truncate_preserving_footer("\n".join(lines), limit=policy.text_limit)


def message_preview(text: str, *, max_lines: int = 12) -> dict[str, Any]:
    """Return dashboard-friendly preview metadata for a rendered message."""
    plain = re.sub(r"<[^>]+>", "", text)
    plain = html.unescape(plain)
    lines = [line for line in plain.splitlines() if line.strip()]
    report = validate_telegram_html(text)
    return {
        "parse_mode": TELEGRAM_PARSE_MODE,
        "chars": len(text),
        "ok": report.ok,
        "errors": [issue.message for issue in report.issues if issue.severity == "error"],
        "warnings": [issue.message for issue in report.issues if issue.severity == "warning"],
        "preview_lines": lines[:max_lines],
        "plain_preview": "\n".join(lines[:max_lines]),
    }


def sample_message_from_row(row: Mapping[str, Any]) -> str:
    """Render a Telegram preview from a telemetry row."""

    class _RowSignal:
        pass

    signal: Any = _RowSignal()
    for key, value in row.items():
        setattr(signal, key, value)
    if not hasattr(signal, "entry_low"):
        signal.entry_low = row.get("entry_price") or row.get("entry_mid") or 0.0
    if not hasattr(signal, "entry_high"):
        signal.entry_high = row.get("entry_price") or row.get("entry_mid") or 0.0
    if not hasattr(signal, "stop"):
        signal.stop = row.get("stop_price") or row.get("stop_loss") or 0.0
    if not hasattr(signal, "take_profit_1"):
        signal.take_profit_1 = row.get("tp1_price") or row.get("tp1") or 0.0
    if not hasattr(signal, "take_profit_2"):
        signal.take_profit_2 = row.get("tp2_price") or row.get("tp2") or 0.0
    if not hasattr(signal, "timeframe"):
        signal.timeframe = row.get("timeframe") or "15m"
    if not hasattr(signal, "tracking_ref"):
        signal.tracking_ref = row.get("tracking_ref") or "preview"
    if not hasattr(signal, "created_at"):
        signal.created_at = row.get("ts") or datetime.now(UTC)
    return format_signal_message(signal, pending_expiry_minutes=180)


def diagnostic_format_matrix(signal: Any) -> dict[str, Any]:
    """Return all delivery renderings for live diagnostics."""
    main = format_signal_message(signal, pending_expiry_minutes=180)
    companion = format_analytics_companion_message(signal)
    return {
        "main": message_preview(main),
        "companion": message_preview(companion),
        "main_html": main,
        "companion_html": companion,
    }
