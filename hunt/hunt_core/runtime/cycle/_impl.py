"""Hunter per-tick cycle — run_loop / run_tick (H-B rewrite)."""
from __future__ import annotations



import asyncio
import json
import os
from typing import Any

from hunt_core.domain.config import SYMBOL_TICK_TIMEOUT_S
from hunt_core.params.store import effective_hunt_params

from hunt_core.market.live_price import apply_live_price_to_row
from hunt_core.market import HuntCcxtStreams

from hunt_core.data.lake import (
    buffer_cooldown_state,
)
from hunt_core.runtime.state import (
    LOG,
    SNIPER_CONFIG,
    STATE_PATH,
)


HUNT_SNIPER_MODE = SNIPER_CONFIG.enabled
HUNT_SNIPER_LIVE_PHASES = SNIPER_CONFIG.live_phases
HUNT_SNIPER_TOP_LS_MAX = SNIPER_CONFIG.top_ls_max
HUNT_SNIPER_REQUIRE_TOP_LS = SNIPER_CONFIG.require_top_ls
HUNT_SNIPER_CHASE_TOL = SNIPER_CONFIG.chase_tol
HUNT_SNAPSHOT_PARALLEL = max(1, int(os.getenv("HUNT_SNAPSHOT_PARALLEL", "6")))
_HOT_TICK_TIMEOUT_S = float(os.getenv("HUNT_HOT_TICK_TIMEOUT_S", "60") or 60)
_TICK_LOCK = asyncio.Lock()


def _evaluate_delivery_row(
    row: dict[str, Any],
    *,
    hot_path: bool,
    direction: str,
    setup: dict[str, Any],
    lifecycle: dict[str, Any] | None,
    symbol: str,
    refresh_live_price: bool = False,
    ws_feed: HuntCcxtStreams | None = None,
) -> tuple[Any, Any]:
    from hunt_core.runtime.cycle._delivery import evaluate_delivery_row

    return evaluate_delivery_row(
        row,
        hot_path=hot_path,
        direction=direction,
        setup=setup,
        lifecycle=lifecycle,
        symbol=symbol,
        refresh_live_price=refresh_live_price,
        ws_feed=ws_feed,
    )


def _overlay_ws_tickers(
    ticker_by_sym: dict[str, dict[str, Any]],
    symbols: tuple[str, ...] | list[str],
    ws_feed: HuntCcxtStreams | None,
) -> None:
    """Prefer WS last over batch REST ticker for snapshot price seed."""
    if ws_feed is None:
        return
    for sym in symbols:
        lt = ws_feed.live_ticker(sym)
        if not lt:
            continue
        last = float(lt.get("last") or 0)
        if last <= 0:
            continue
        base = dict(ticker_by_sym.get(sym) or {"symbol": sym})
        base["last_price"] = last
        ticker_by_sym[sym] = base


def _refresh_live_price(
    row: dict[str, Any],
    *,
    ws_feed: HuntCcxtStreams | None,
    symbol: str,
) -> float:
    prev = float(row.get("price") or 0)
    px = apply_live_price_to_row(row, ws_feed=ws_feed)
    delta = row.get("price_stale_delta_pct")
    if delta is not None and abs(float(delta)) >= 0.05:
        LOG.info(
            "live_price_refresh",
            symbol=symbol,
            price=px,
            prev=prev,
            delta_pct=delta,
            source=row.get("price_source"),
        )
    return px





def _phase_long(long_setup: dict[str, Any], confirmed: bool, *, symbol: str = "") -> str:
    return str(long_setup.get("phase") or ("pre_pump" if confirmed else "neutral"))


def _load_state() -> dict[str, str]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_state(state: dict[str, str]) -> None:
    buffer_cooldown_state(state, STATE_PATH)


from hunt_core.runtime.cycle._cycle_tick import run_tick


async def run_hot_kline_tick(
    symbols: tuple[str, ...],
    ctx: dict[str, Any],
) -> list[dict[str, Any]]:
    """Hot 1m-close tick — serialized with cold tick via ``_TICK_LOCK``."""
    async with _TICK_LOCK:
        return await run_tick(
            symbols,
            **{
                **{k: v for k, v in ctx.items() if k not in ("active", "tier", "hot_path")},
                "tier": "hot",
                "hot_path": True,
            },
        )


from hunt_core.runtime.cycle._cycle_loop import run_loop


__all__ = ["SYMBOL_TICK_TIMEOUT_S", "run_tick", "run_loop"]
