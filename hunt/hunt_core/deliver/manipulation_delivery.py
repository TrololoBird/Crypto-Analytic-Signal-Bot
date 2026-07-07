"""Deliver manipulation reversal setups (scanner/detect/patterns.py).

Scanner's sole signal path — two patterns:
- Pattern A (long): impulse→absorption→bokovik→sweep→break
- Pattern B (short): HTF trend→sweep→fade→LTF_confirm

Non-pinned universe only. Pinned symbols are Prizrak's Deep module exclusively.
"""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timezone
from typing import Any

from hunt_core.deliver._labels import fmt_price
from hunt_core.deliver.lab import send_lane_html
from hunt_core.scanner.detect.patterns import ManipulationSetup, detect_manipulation_setup

_LOG = logging.getLogger(__name__)

_SL_BUFFER_PCT = 0.02
_MIN_RR = 1.2
_MAX_TARGET_PCT = 20.0

_TIMEFRAMES = ("1d", "4h", "1h", "15m", "5m")
_LOOKBACK_BY_TF = {"1d": 220, "4h": 120, "1h": 120, "15m": 700, "5m": 1000}


# User's explicit correction: don't wait for the dump/pump to already be
# running (it "may pass in three minutes") — enter as the impulse starts
# EXHAUSTING, deliberately BEFORE full confirmation, which means price can
# still move a bit further against the position before genuinely turning.
# The stop already accounts for that (anchored beyond the full sweep extreme,
# not the entry) — this adds the other missing piece: an explicit averaging
# ("довор") level between entry and stop, and a market/limit order-type label
# matching Prizrak's own format, instead of a single bare "entry price".
_AVERAGING_FRACTION = 0.5  # how far from entry toward stop the averaging limit sits


def _geometry(setup: ManipulationSetup, *, price: float) -> dict[str, Any] | None:
    if setup.target is None:
        return None  # no real structural target — abstain, never fabricate one
    target_dist_pct = abs(price - setup.target) / price * 100.0
    if target_dist_pct > _MAX_TARGET_PCT:
        return None  # цель нереалистично далеко — пропускаем
    if setup.direction == "short":
        stop = setup.sweep_extreme * (1 + _SL_BUFFER_PCT)
        risk = stop - price
        reward = price - setup.target
    else:
        stop = setup.sweep_extreme * (1 - _SL_BUFFER_PCT)
        risk = price - stop
        reward = setup.target - price
    if risk <= 0 or reward <= 0:
        return None
    rr = reward / risk
    if rr < _MIN_RR:
        return None
    averaging_price = price + (stop - price) * _AVERAGING_FRACTION
    return {
        "entry_lo": min(price, averaging_price),
        "entry_hi": max(price, averaging_price),
        "averaging_price": averaging_price,
        "stop": stop,
        "rr": rr,
    }


def _format_manipulation_signal(symbol: str, setup: ManipulationSetup, *, price: float, geo: dict[str, Any]) -> str:
    sym = html.escape(symbol.replace("USDT", "-USDT"))
    side_label = "SHORT" if setup.direction == "short" else "LONG"
    emoji = "🔴" if setup.direction == "short" else "🟢"
    pattern_label = f"Pattern {setup.pattern_type}"
    micro_line = ""
    if setup.micro_tf:
        tag = "подтверждён" if setup.micro_confirmed else "не найден"
        micro_line = f"Разворот на {setup.micro_tf}: <b>{tag}</b>\n"
    lines = [
        f"{emoji} <b>Манипуляция {pattern_label}</b> · <code>{sym}</code> · <b>{side_label}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"Score: <b>{setup.score:.0%}</b> · Шаги: {setup.steps_covered}/{setup.total_steps}",
        f"Свип уровня {setup.macro_tf} <code>{fmt_price(setup.swept_level)}</code> → "
        f"экстремум <code>{fmt_price(setup.sweep_extreme)}</code> ({setup.meso_tf})",
        micro_line.rstrip("\n") if micro_line else "",
        f"📍 Вход (рыночный / лимит): <code>{fmt_price(geo['entry_lo'])} — {fmt_price(geo['entry_hi'])}</code>",
        f"➕ Довор (если пойдёт против ещё): <code>{fmt_price(geo['averaging_price'])}</code>",
        f"🛑 Стоп (за структуру): <code>{fmt_price(geo['stop'])}</code>",
        f"🎯 Цель (структурная зона): <code>{fmt_price(setup.target)}</code>",
        f"R:R ≈ <code>{geo['rr']:.2f}</code>",
        f"<i>почему: {html.escape(', '.join(setup.evidence))}</i>",
        "<i>Держим до цели/стопа — не микро-триггер</i>",
    ]
    return "\n".join(line for line in lines if line)


_PARALLEL_SEMAPHORE = 10


async def _fetch_symbol_data(
    client: Any, symbol: str, sem: asyncio.Semaphore,
) -> tuple[str, dict[str, list[list[float]]]]:
    """Parallel OHLCV fetch for one symbol. CCXT Pro async REST via asyncio.gather."""
    ohlcv_by_tf: dict[str, list[list[float]]] = {}
    async def _fetch(tf: str) -> tuple[str, list[list[float]] | None]:
        try:
            bars = await client.fetch_ohlcv_list(symbol, tf, limit=_LOOKBACK_BY_TF[tf])
            return tf, bars
        except Exception:
            _LOG.debug("manipulation_fetch_failed sym=%s tf=%s", symbol, tf, exc_info=True)
            return tf, None
    # Parallel per-TF fetch within symbol
    async with sem:
        tfs = await asyncio.gather(*[_fetch(tf) for tf in _TIMEFRAMES], return_exceptions=True)
    for tf, bars in tfs:
        if isinstance(tf, str) and bars:
            ohlcv_by_tf[tf] = bars
    return symbol, ohlcv_by_tf


async def deliver_manipulation_setups(
    symbols: list[str],
    client: Any,
    broadcaster: Any,
    *,
    tracker_state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Scan ``symbols`` for manipulation reversal setups — parallel REST + Polars detection.

    Uses asyncio.gather with semaphore for CCXT Pro async REST.
    Detection runs on Polars DataFrames via scanner/detect/patterns.py.
    """
    from hunt_core.track.tracker import has_active_signal, register_signal_open
    results: list[dict[str, Any]] = []
    now_dt = datetime.now(timezone.utc)
    sem = asyncio.Semaphore(_PARALLEL_SEMAPHORE)
    fetch_tasks = [_fetch_symbol_data(client, s, sem) for s in symbols]
    outcomes = await asyncio.gather(*fetch_tasks, return_exceptions=True)

    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            _LOG.warning("manipulation_gather_exception", error=repr(outcome))
            continue
        symbol, ohlcv_by_tf = outcome
        if not ohlcv_by_tf.get("1d"):
            continue

        setup = detect_manipulation_setup(ohlcv_by_tf)
        if setup is None:
            continue

        if tracker_state is not None and has_active_signal(tracker_state, symbol=symbol, direction=setup.direction):
            continue  # course: runs to completion — don't re-fire while the prior call is still open

        # setup.entry_ref anchors to the confirmation candle's own close when
        # micro confirmation fired — falls back to the last meso close only
        # when there was no micro confirmation to anchor to.
        if setup.entry_ref is not None and setup.entry_ref > 0:
            price = setup.entry_ref
        else:
            meso_bars = ohlcv_by_tf.get(setup.meso_tf) or ohlcv_by_tf["1d"]
            price = float(meso_bars[-1][4])
        if price <= 0:
            continue
        geo = _geometry(setup, price=price)
        if geo is None:
            continue

        text = _format_manipulation_signal(symbol, setup, price=price, geo=geo)
        try:
            result = await send_lane_html(broadcaster, text)
        except Exception:
            _LOG.exception("manipulation_delivery_send_failed sym=%s", symbol)
            continue

        message_id = getattr(result, "message_id", None)
        if tracker_state is not None:
            setup_dict = {
                "stop_loss": geo["stop"],
                "tp1": setup.target,
                "entry_zone": [geo["entry_lo"], geo["entry_hi"]],
                "averaging_price": geo["averaging_price"],
                "entry_type": f"manipulation_{setup.pattern_type}",
                "risk_reward": geo["rr"],
                "level_source": "manipulation_structural",
                "telegram_sent": True,
                "delivery_tier": "triggered",
                "phase": "manipulation",
                "pattern_type": setup.pattern_type,
                "score": setup.score,
                "steps": f"{setup.steps_covered}/{setup.total_steps}",
                "dump_score": 0,
                "dump_fuel": 0,
                "long_score": 0,
                "long_fuel": 0,
                "confirm_hard": [],
            }
            lifecycle = {"phase": "pre_dump" if setup.direction == "short" else "pre_pump"}
            try:
                register_signal_open(
                    tracker_state,
                    symbol=symbol,
                    direction=setup.direction,
                    price=price,
                    setup=setup_dict,
                    lifecycle=lifecycle,
                    now=now_dt,
                    entry_message_id=message_id,
                )
            except Exception:
                _LOG.exception("manipulation_tracker_register_failed sym=%s", symbol)

        results.append({"symbol": symbol, "direction": setup.direction, "message_id": message_id,
                         "pattern_type": setup.pattern_type, "score": setup.score})
        _LOG.info(
            "manipulation_delivered sym=%s dir=%s pattern=%s score=%.2f target=%s rr=%.2f steps=%d/%d",
            symbol, setup.direction, setup.pattern_type, setup.score,
            setup.target, geo["rr"], setup.steps_covered, setup.total_steps,
        )

    return results


__all__ = ["deliver_manipulation_setups"]
