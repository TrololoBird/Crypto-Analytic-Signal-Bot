"""Re-run strategy detectors on historical candle data for SL forensics."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import polars as pl

LOG = logging.getLogger("sl_forensic.strategy_recheck")


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
    timeframe: str,
    work_primary: pl.DataFrame,
    work_1h: pl.DataFrame | None,
    work_15m: pl.DataFrame | None,
) -> Any:
    from bot.domain.schemas import PreparedSymbol, UniverseSymbol

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
    return PreparedSymbol(
        universe=universe,
        work_1h=work_1h if work_1h is not None else work_primary,
        work_15m=work_15m if work_15m is not None else work_primary,
        bid_price=last_close,
        ask_price=last_close,
        spread_bps=0.0,
        work_primary=work_primary,
    )


async def recheck_strategy(
    setup_id: str,
    symbol: str,
    timeframe: str,
    candles: list[dict[str, Any]],
    signal_ts_ms: int,
    settings: Any,
) -> dict[str, Any]:
    """Re-run detector on historical closed-candle slice."""
    from bot.strategies import STRATEGY_CLASSES

    cls = next((c for c in STRATEGY_CLASSES if getattr(c, "setup_id", None) == setup_id), None)
    if cls is None:
        return {"valid": None, "signal_count": 0, "reason": f"strategy {setup_id} not found"}

    if len(candles) < 50:
        return {
            "valid": None,
            "signal_count": 0,
            "reason": f"insufficient candles ({len(candles)}) for recheck",
        }

    try:
        df = _candles_to_df(candles)
        sig_idx = _signal_index(candles, signal_ts_ms)
        # Only data available at signal time (inclusive).
        slice_df = df.head(sig_idx + 1)
        if slice_df.height < 50:
            return {
                "valid": None,
                "signal_count": 0,
                "reason": "insufficient history at signal bar",
            }

        from bot.features.prepare_frame import _prepare_frame

        prepared_frame = _prepare_frame(slice_df)

        tf = timeframe.split("+")[0].strip() or "15m"
        work_1h = prepared_frame if tf == "1h" else prepared_frame
        work_15m = prepared_frame if tf in {"15m", "5m"} else prepared_frame

        prepared = _build_minimal_prepared(
            symbol=symbol,
            timeframe=tf,
            work_primary=prepared_frame,
            work_1h=work_1h,
            work_15m=work_15m,
        )

        strategy = cls(settings=settings)
        result = strategy.detect(prepared, settings)

        if result is None:
            return {
                "valid": False,
                "signal_count": 0,
                "reason": "detector did not fire on confirmed historical slice",
            }

        signals = result if isinstance(result, list) else [result]
        return {
            "valid": len(signals) > 0,
            "signal_count": len(signals),
            "reason": "detector fired on confirmed historical data"
            if signals
            else "no signal",
        }
    except Exception as exc:
        LOG.info("recheck failed for %s/%s: %s", setup_id, symbol, exc)
        return {"valid": None, "signal_count": 0, "reason": f"recheck_failed: {exc}"}


def ts_ms_from_iso(value: str | None) -> int | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)
    except (TypeError, ValueError):
        return None
