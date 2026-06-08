"""Trade plan builder - centralized entry/TP/SL math for manual signals."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from bot.domain.limit_entry import limit_delivery_ready

from .contract import (
    DEFAULT_SCALE_WEIGHTS,
    DEFAULT_TARGET_RR,
    TradePlan,
    build_trade_plan,
    default_ttl_bars,
    normalize_scale_weights,
    resolve_target_rr,
    valid_until_from,
)

if TYPE_CHECKING:
    from datetime import datetime

__all__ = [
    "DEFAULT_SCALE_WEIGHTS",
    "DEFAULT_TARGET_RR",
    "TradePlan",
    "TradePlanBuilder",
    "build_trade_plan",
    "default_ttl_bars",
    "evaluate_publish_readiness",
    "normalize_scale_weights",
    "valid_until_from",
]


class TradePlanBuilder:
    """Thin facade over ``contract.build_trade_plan`` for delivery/runtime."""

    @staticmethod
    def build(
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
        now: datetime | None = None,
        target_rr: tuple[float, float, float] | None = None,
        settings: Any | None = None,
    ) -> TradePlan | None:
        effective_rr = target_rr or resolve_target_rr(settings)
        return build_trade_plan(
            direction=direction,
            setup_id=setup_id,
            strategy_family=strategy_family,
            timeframe=timeframe,
            price_anchor=price_anchor,
            atr=atr,
            stop_loss=stop_loss,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            entry_pad_atr_mult=entry_pad_atr_mult,
            created_at=created_at if created_at is not None else now,
            ttl_bars=ttl_bars,
            scale_weights=scale_weights,
            target_rr=effective_rr,
        )

    @staticmethod
    def from_signal_fields(signal: Any, *, atr: float, price_anchor: float) -> TradePlan | None:
        return build_trade_plan(
            direction=str(getattr(signal, "direction", "") or ""),
            setup_id=str(getattr(signal, "setup_id", "") or ""),
            strategy_family=str(getattr(signal, "strategy_family", "") or "continuation"),
            timeframe=str(getattr(signal, "timeframe", "") or "15m"),
            price_anchor=price_anchor,
            atr=atr,
            stop_loss=float(getattr(signal, "stop", 0.0) or 0.0),
            tp1=float(getattr(signal, "take_profit_1", 0.0) or 0.0),
            tp2=float(getattr(signal, "take_profit_2", 0.0) or 0.0),
        )


def evaluate_publish_readiness(
    *,
    direction: str,
    mark_price: float | None,
    entry_low: float,
    entry_high: float,
    stop: float,
    chase_pct: float,
    entry_order_type: str = "limit",
    atr_pct: float | None = None,
) -> tuple[bool, str | None, dict[str, object]]:
    """Publish-time limit gate - delegates to domain limit_entry semantics."""
    ready, reason, details = limit_delivery_ready(
        direction=direction,
        mark_price=mark_price,
        entry_low=entry_low,
        entry_high=entry_high,
        stop=stop,
        chase_pct=chase_pct,
        entry_order_type=entry_order_type,
        atr_pct=atr_pct,
    )
    return ready, reason, dict(details)
