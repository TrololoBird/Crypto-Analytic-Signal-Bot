"""Runtime data-capability gate: strategy pool vs PreparedSymbol fields."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from bot.market.strategy_pools import SETUP_DATA_POOL

if TYPE_CHECKING:
    from bot.domain.schemas import PreparedSymbol


@dataclass(frozen=True, slots=True)
class DataCapabilityResult:
    ready: bool
    reason: str | None = None
    pool: str = "klines"


def data_pool_for_setup(setup_id: str) -> str:
    return str(SETUP_DATA_POOL.get(str(setup_id or "").strip(), "klines"))


def assess_strategy_data_capability(
    setup_id: str,
    prepared: PreparedSymbol,
) -> DataCapabilityResult:
    """Return whether public data for this setup's pool is present on the symbol."""
    pool = data_pool_for_setup(setup_id)
    str(getattr(prepared, "symbol", "") or "").strip().upper()

    if pool == "klines":
        return DataCapabilityResult(ready=True, pool=pool)

    if pool == "orderbook":
        if getattr(prepared, "depth_imbalance", None) is None:
            return DataCapabilityResult(
                ready=False,
                reason="data.orderbook_not_ready",
                pool=pool,
            )
        return DataCapabilityResult(ready=True, pool=pool)

    if pool == "positioning":
        missing: list[str] = []
        if getattr(prepared, "oi_change_pct", None) is None:
            missing.append("oi_change_pct")
        if getattr(prepared, "funding_rate", None) is None:
            missing.append("funding_rate")
        if missing:
            return DataCapabilityResult(
                ready=False,
                reason="data.positioning_not_ready",
                pool=pool,
            )
        return DataCapabilityResult(ready=True, pool=pool)

    if pool == "orderflow":
        has_book = getattr(prepared, "depth_imbalance", None) is not None
        has_micro = getattr(prepared, "microprice_bias", None) is not None
        if not has_book and not has_micro:
            return DataCapabilityResult(
                ready=False,
                reason="data.orderflow_not_ready",
                pool=pool,
            )
        return DataCapabilityResult(ready=True, pool=pool)

    if pool == "multi_asset":
        return DataCapabilityResult(ready=True, pool=pool)

    return DataCapabilityResult(ready=True, pool=pool, reason=None)
