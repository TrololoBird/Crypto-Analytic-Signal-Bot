"""Re-run strategy detectors on historical candle data for SL forensics."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import polars as pl

from bot.domain.schemas import PreparedSymbol, UniverseSymbol
from bot.features.prepare_frame import _prepare_frame
from bot.runtime.errors import DEFENSIVE_EXC
from bot.strategies import STRATEGY_CLASSES

LOG = logging.getLogger("sl_forensic.strategy_recheck")

# Setups that can be rechecked from OHLCV + stored context alone.
_CANDLE_ONLY_SETUPS = frozenset(
    {
        "btc_correlation",
        "ema_bounce",
        "funding_reversal",
        "multi_tf_trend",
        "price_velocity",
        "rsi_divergence_bottom",
        "supertrend_follow",
        "vwap_trend",
    }
)

_ENRICHMENT_REJECT_PREFIXES = (
    "data.",
    "pattern.wall_proxy",
    "pattern.depth",
    "pattern.orderbook",
)


def _candles_to_df(candles: list[dict[str, Any]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts": [int(c["ts"]) for c in candles],
            "open": [float(c["open"]) for c in candles],
            "high": [float(c["high"]) for c in candles],
            "low": [float(c["low"]) for c in candles],
            "close": [float(c["close"]) for c in candles],
            "volume": [float(c["volume"]) for c in candles],
        }
    ).sort("ts")


def _signal_index(candles: list[dict[str, Any]], signal_ts_ms: int) -> int:
    best_idx = 0
    best_dist = abs(int(candles[0]["ts"]) - signal_ts_ms)
    for idx, candle in enumerate(candles):
        dist = abs(int(candle["ts"]) - signal_ts_ms)
        if dist <= best_dist:
            best_dist = dist
            best_idx = idx
    return best_idx


def _build_minimal_prepared(
    *,
    symbol: str,
    work_primary: pl.DataFrame,
    context: dict[str, Any],
) -> Any:
    last_close = float(work_primary["close"][-1]) if work_primary.height else 0.0
    universe = UniverseSymbol(
        symbol=symbol,
        base_asset=symbol.replace("USDT", ""),
        quote_asset="USDT",
        contract_type="PERPETUAL",
        status="TRADING",
        onboard_date_ms=0,
        quote_volume=0.0,
        price_change_pct=0.0,
        last_price=last_close,
    )
    snapshot = context.get("indicator_snapshot") or {}
    if not isinstance(snapshot, dict):
        snapshot = {}

    bias_4h = str(context.get("bias_4h") or snapshot.get("bias_4h") or "neutral")
    bias_1h = str(context.get("bias_1h") or snapshot.get("bias_1h") or bias_4h)
    btc_bias = context.get("btc_bias") or snapshot.get("btc_bias") or bias_4h

    return PreparedSymbol(
        universe=universe,
        work_1h=work_primary,
        work_15m=work_primary,
        bid_price=last_close,
        ask_price=last_close,
        spread_bps=float(context.get("spread_bps") or snapshot.get("spread_bps") or 0.0),
        work_primary=work_primary,
        bias_4h=bias_4h,
        bias_1h=bias_1h,
        btc_bias=str(btc_bias) if btc_bias else None,
        funding_rate=context.get("funding_rate") or snapshot.get("funding_rate"),
        depth_imbalance=snapshot.get("depth_imbalance"),
        microprice_bias=snapshot.get("microprice_bias"),
        depth_wall_pressure=snapshot.get("depth_wall_pressure"),
    )


def _is_enrichment_reject(reason: str) -> bool:
    lowered = reason.lower()
    return any(lowered.startswith(prefix) for prefix in _ENRICHMENT_REJECT_PREFIXES)


async def recheck_strategy(
    setup_id: str,
    symbol: str,
    _timeframe: str,
    candles: list[dict[str, Any]],
    signal_ts_ms: int,
    settings: Any,
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-run detector on historical closed-candle slice."""
    context = context or {}
    cls = next((c for c in STRATEGY_CLASSES if getattr(c, "setup_id", None) == setup_id), None)
    if cls is None:
        return {"valid": None, "signal_count": 0, "reason": f"strategy {setup_id} not found"}

    if len(candles) < 50:
        return {
            "valid": None,
            "signal_count": 0,
            "reason": f"insufficient candles ({len(candles)}) for recheck",
        }

    if setup_id not in _CANDLE_ONLY_SETUPS:
        return {
            "valid": None,
            "signal_count": 0,
            "reason": f"recheck_skipped: {setup_id} requires live enrichment (orderbook/OI)",
        }

    try:
        df = _candles_to_df(candles)
        sig_idx = _signal_index(candles, signal_ts_ms)
        # Confirmed data only: exclude the forming bar that triggered the live signal.
        confirmed_end = max(1, sig_idx)
        slice_df = df.head(confirmed_end)
        if slice_df.height < 50:
            return {
                "valid": None,
                "signal_count": 0,
                "reason": "insufficient history at confirmed signal bar",
            }

        prepared_frame = _prepare_frame(slice_df)
        prepared = _build_minimal_prepared(
            symbol=symbol,
            work_primary=prepared_frame,
            context=context,
        )

        strategy = cls(settings=settings)
        result = strategy.detect(prepared, settings)
    except DEFENSIVE_EXC as exc:
        msg = str(exc)
        if _is_enrichment_reject(msg):
            return {"valid": None, "signal_count": 0, "reason": f"recheck_skipped: {msg}"}
        LOG.info("recheck failed for %s/%s: %s", setup_id, symbol, exc)
        return {"valid": None, "signal_count": 0, "reason": f"recheck_failed: {exc}"}
    else:
        if result is None:
            return {
                "valid": False,
                "signal_count": 0,
                "reason": "detector did not fire on confirmed historical slice",
            }
        return {
            "valid": True,
            "signal_count": 1,
            "reason": "detector fired on confirmed historical data",
        }


def ts_ms_from_iso(value: str | None) -> int | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)
    except (TypeError, ValueError):
        return None
