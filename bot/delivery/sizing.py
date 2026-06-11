"""Position sizing helpers for п.34 — Kelly-based size and partial TP distribution."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.domain.config import BotSettings
    from engine.domain.schemas import Signal

RISK_PER_TRADE_DEFAULT = 0.02
MAX_RISK_PER_TRADE = 0.05

# п.28: module-level win-rate cache, keyed by setup_id.
# Updated by delivery_orchestrator from DB via update_strategy_win_rates().
_WIN_RATE_CACHE: dict[str, dict[str, float]] = {}


def update_strategy_win_rates(rates: dict[str, dict[str, float]]) -> None:
    """Refresh the win-rate cache from delivery_orchestrator (п.28).

    ``rates`` maps setup_id → {win_rate, avg_r_multiple, total}.
    """
    _WIN_RATE_CACHE.clear()
    _WIN_RATE_CACHE.update(rates)


def kelly_fraction(
    win_rate: float,
    avg_win_r: float,
    avg_loss_r: float,
    *,
    conservative: float = 0.25,
) -> float:
    """Kelly fraction = WR / |avg_loss| - (1-WR) / avg_win → clamped [0, 1].

    ``conservative`` multiplier (default 0.25) halves Kelly for safety.
    """
    if not (0.0 < win_rate < 1.0) or avg_win_r <= 0.0 or avg_loss_r >= 0.0:
        return 0.0
    b = avg_win_r / abs(avg_loss_r)
    p = win_rate
    kelly = (p * b - (1.0 - p)) / b
    return max(0.0, min(kelly * max(0.0, conservative), 1.0))


def recommend_position_pct(
    signal: Signal,
    _settings: BotSettings,
    *,
    win_rate: float | None = None,
    avg_win_pct: float | None = None,
    avg_loss_pct: float | None = None,
    risk_decimal: float = RISK_PER_TRADE_DEFAULT,
) -> float:
    """Recommend position size as % of portfolio.

    Uses Kelly when win-rate is available; falls back to fixed % of capital
    at risk defined by ``risk_decimal`` (default 2%).
    """
    stop_distance = abs(float(getattr(signal, "stop_distance_pct", 0.0) or 0.0))
    if stop_distance <= 0.0:
        stop_distance = 1.0
    # п.28: pull from cache when not explicitly supplied
    if win_rate is None:
        cached = _WIN_RATE_CACHE.get(getattr(signal, "setup_id", "") or "")
        if cached:
            win_rate = cached.get("win_rate")
            if avg_win_pct is None:
                avg_r = cached.get("avg_r_multiple", 0.0) or 0.0
                avg_win_pct = float(avg_r) if avg_r > 0.0 else None
            if avg_loss_pct is None and win_rate:
                avg_loss_pct = -1.0  # conservative 1R avg loss assumption
    if (
        win_rate is not None
        and avg_win_pct is not None
        and avg_loss_pct is not None
        and win_rate > 0.0
        and avg_loss_pct != 0.0
    ):
        frac = kelly_fraction(win_rate, avg_win_pct, abs(avg_loss_pct))
        if frac > 0.0:
            risk_kelly = min(risk_decimal * (1.0 + frac * 2.0), MAX_RISK_PER_TRADE)
            return round(min(risk_kelly / (stop_distance / 100.0), 1.0), 4)
    return round(min(risk_decimal / (stop_distance / 100.0), 1.0), 4)


def default_scale_weights(
    *,
    certainty: float = 1.0,
) -> tuple[float, float, float]:
    """Return TP-split weights by signal certainty.

    High certainty (≥0.72) → aggressive: 40/30/30.
    Low certainty (<0.60) → conservative: 60/30/10.
    """
    if certainty >= 0.72:
        return (0.40, 0.30, 0.30)
    if certainty >= 0.65:
        return (0.50, 0.30, 0.20)
    return (0.60, 0.30, 0.10)
