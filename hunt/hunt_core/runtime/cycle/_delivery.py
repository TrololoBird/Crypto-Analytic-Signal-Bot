"""Delivery orchestration extracted from cycle/_impl.py (Phase 8 / X3)."""
from __future__ import annotations

from typing import Any

from hunt_core.market.streams import HuntCcxtStreams


def evaluate_delivery_row(
    row: dict[str, Any],
    *,
    direction: str,
    setup: dict[str, Any],
    lifecycle: dict[str, Any] | None,
    symbol: str,
    refresh_live_price: bool = False,
    ws_feed: HuntCcxtStreams | None = None,
) -> tuple[Any, Any]:
    use_fast = row.get("tick_path") in {
        "hot_ws",
        "hot_bootstrap",
        "hot_delta",
        "hot_carry",
    }
    if use_fast:
        from hunt_core.deliver.dispatch import evaluate_delivery_fast

        fn = evaluate_delivery_fast
    else:
        from hunt_core.deliver.dispatch import evaluate_delivery

        fn = evaluate_delivery
    return fn(
        row,
        direction=direction,
        setup=setup,
        lifecycle=lifecycle,
        symbol=symbol,
        refresh_live_price=refresh_live_price,
        ws_feed=ws_feed,
    )


def is_armed_setup(setup: dict[str, Any]) -> bool:
    return bool(
        setup.get("early_tier") == "armed"
        or setup.get("intrabar_armed")
        or setup.get("anticipation")
    )


def is_confirmed_setup(setup: dict[str, Any]) -> bool:
    return bool(setup.get("confirmed") or setup.get("intrabar_confirmed"))


__all__ = [
    "evaluate_delivery_row",
    "is_armed_setup",
    "is_confirmed_setup",
]
