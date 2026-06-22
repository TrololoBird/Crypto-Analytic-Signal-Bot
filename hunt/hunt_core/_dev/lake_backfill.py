"""Backfill feature lake parquet from CCXT history (soak accelerator)."""
from __future__ import annotations

import argparse
import asyncio
import copy
import sys
from datetime import UTC, datetime
from typing import Any

from hunt_core.data.lake import FeatureLakeWriter
from hunt_core.domain.config import load_settings
from hunt_core.features.feature_engine import FeatureExtractError, build_feature_vector
from hunt_core.features.prepare import min_required_bars
from hunt_core.features.snapshot import attach_pp_flags, tf_snapshot_for_symbol
from hunt_core.paths import LAKE_PARQUET

DEFAULT_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "WIFUSDT",
    "ARBUSDT",
    "LINKUSDT",
    "AVAXUSDT",
    "DOGEUSDT",
    "SUIUSDT",
)
DEFAULT_BARS = 90
_WARMUP = 35


def _bar_ts(work: Any, idx: int) -> str:
    for col in ("close_time", "open_time", "ts", "time"):
        if col not in work.columns:
            continue
        raw = work.item(idx, col)
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(int(raw) / 1000.0, tz=UTC).isoformat()
        return str(raw)
    msg = "no timestamp column in prepared work_15m"
    raise FeatureExtractError(msg)


def _closed_snap(work: Any, symbol: str, idx: int) -> dict[str, Any]:
    """Build closed-bar TF snapshot at absolute index ``idx`` (causal slice)."""
    end = idx + 2
    if end > work.height:
        end = work.height
    slice_df = work.slice(0, end)
    snap = tf_snapshot_for_symbol(slice_df, symbol, closed=True, candle_patterns=True)
    return attach_pp_flags(snap, slice_df, closed=True)


def _historical_map_features(prepared: Any, symbol: str, idx: int, price: float) -> dict[str, Any]:
    """Causal VP map features for one historical bar (lake backfill)."""
    from hunt_core.maps.engine import MapBundle, derive_map_features
    from hunt_core.maps.volume_profile import build_volume_profile_map

    end = idx + 2
    frames: dict[str, Any] = {}
    for tf in ("15m", "1h", "4h", "1d"):
        work = getattr(prepared, f"work_{tf}", None)
        if work is not None and getattr(work, "height", 0) >= end:
            frames[tf] = work.slice(0, end)
    if not frames:
        return {}
    vp = build_volume_profile_map(symbol=symbol.upper(), current_price=price, frames=frames)
    if vp is None:
        return {}
    bundle = MapBundle(symbol=symbol.upper(), ts_ms=0, volume_profile=vp)
    return derive_map_features(bundle, current_price=price)


async def _fetch_batch_context(client: Any, settings: Any) -> tuple[Any, ...]:
    from hunt_core.runtime.tick_assembly import safe_fetch

    premium_all = await safe_fetch(client.fetch_premium_index_all(), context="premium_index_all") or {}
    funding_info_all = await safe_fetch(client.fetch_funding_info_all(), context="funding_info_all") or {}
    exchange_list = await safe_fetch(client.fetch_exchange_symbols(), context="exchange_symbols") or []
    exchange_by_sym = {r.symbol: r for r in exchange_list}
    ticker_raw = await safe_fetch(client.fetch_ticker_24h(), context="ticker_24h") or []
    ticker_by_sym = {str(t.get("symbol")): t for t in ticker_raw if t.get("symbol")}
    btc_work_1h = None
    btc_df = await safe_fetch(client.fetch_klines_cached("BTCUSDT", "1h", limit=500), context="btc_klines_1h")
    if btc_df is not None and not btc_df.is_empty():
        from hunt_core.features.prepare_frame import _prepare_frame

        btc_work_1h = _prepare_frame(btc_df)
    return premium_all, funding_info_all, exchange_by_sym, ticker_by_sym, btc_work_1h


def _build_oi_series(raw_oi: list[dict[str, Any]]) -> dict[int, float]:
    """Return {timestamp_ms: oi_value} from raw OI history rows."""
    out: dict[int, float] = {}
    for item in raw_oi:
        ts = item.get("timestamp") or item.get("ts")
        val = item.get("openInterestAmount") or item.get("openInterest")
        if ts is not None and val is not None:
            try:
                out[int(ts)] = float(val)
            except (TypeError, ValueError):
                pass
    return out


def _oi_for_bar(
    oi_series: dict[int, float],
    bar_ts_ms: int,
    *,
    window_ms: int = 15 * 60 * 1000,
) -> float | None:
    """Find the OI value at or just before bar_ts_ms in the OI series."""
    best_ts: int | None = None
    best_val: float | None = None
    for ts, val in oi_series.items():
        if bar_ts_ms - window_ms <= ts <= bar_ts_ms:
            if best_ts is None or ts > best_ts:
                best_ts = ts
                best_val = val
    return best_val


def _per_bar_oi_metrics(
    oi_series: dict[int, float],
    bar_ts_ms: int,
    prev_ts_ms: int,
) -> dict[str, float | None]:
    """Compute oi_change_pct and oi_slope_5m for a bar."""
    cur = _oi_for_bar(oi_series, bar_ts_ms)
    prev = _oi_for_bar(oi_series, prev_ts_ms)
    oi_chg: float | None = None
    if cur is not None and prev is not None and prev != 0.0:
        oi_chg = (cur - prev) / abs(prev) * 100.0
    # slope: OLS over up to last 5 readings before bar_ts_ms
    sorted_ts = sorted(ts for ts in oi_series if ts <= bar_ts_ms)
    recent = sorted_ts[-5:] if len(sorted_ts) >= 2 else []
    oi_slope: float | None = None
    if len(recent) >= 2:
        import numpy as np
        vals = [oi_series[t] for t in recent]
        xs = list(range(len(vals)))
        slope = float(np.polyfit(xs, vals, 1)[0])
        mean_val = float(np.mean(vals))
        if mean_val != 0.0:
            oi_slope = slope / mean_val
    return {"oi_change_pct": oi_chg, "oi_slope_5m": oi_slope}


def _bar_ts_ms(work: Any, idx: int) -> int | None:
    """Return bar timestamp as int milliseconds (open or close time)."""
    import datetime as _dt
    for col in ("open_time", "close_time", "time", "ts"):
        if col not in work.columns:
            continue
        raw = work.item(idx, col)
        if raw is None:
            continue
        # Polars datetime → milliseconds
        if isinstance(raw, _dt.datetime):
            return int(raw.timestamp() * 1000)
        try:
            v = int(raw)
            return v if v > 1_000_000_000_000 else v * 1000
        except (TypeError, ValueError):
            pass
    return None


async def _backfill_symbol(
    client: Any,
    settings: Any,
    symbol: str,
    *,
    bars: int,
    writer: FeatureLakeWriter,
    batch_ctx: tuple[Any, ...],
) -> int:
    from hunt_core.market.symbol_gate import is_allowed_for_analysis
    from hunt_core.runtime.tick_assembly import snapshot_symbol

    sym = symbol.upper()
    premium_all, funding_info_all, exchange_by_sym, ticker_by_sym, btc_work_1h = batch_ctx
    if not is_allowed_for_analysis(sym, exchange=client.exchange):
        print(f"  {sym}: skip (not tradable)", file=sys.stderr)
        return 0

    minimums = min_required_bars(
        min_bars_15m=settings.filters.min_bars_15m,
        min_bars_1h=settings.filters.min_bars_1h,
        min_bars_4h=settings.filters.min_bars_4h,
    )
    row = await snapshot_symbol(
        client,
        settings,
        minimums,
        sym,
        watch_mode="both",
        prev_oi=None,
        premium_all=premium_all,
        funding_info_all=funding_info_all,
        btc_work_1h=btc_work_1h,
        exchange_by_sym=exchange_by_sym,
        ticker_by_sym=ticker_by_sym,
        tier="full",
        hunt_fusion=False,
    )
    if row.get("error"):
        print(f"  {sym}: snapshot error — {row.get('error')}", file=sys.stderr)
        return 0

    prepared = row.get("_prepared")
    if prepared is None:
        print(f"  {sym}: no prepared object", file=sys.stderr)
        return 0
    work = getattr(prepared, "work_15m", None)
    if work is None or work.height < _WARMUP + 2:
        print(f"  {sym}: insufficient work_15m ({getattr(work, 'height', 0)})", file=sys.stderr)
        return 0

    # Fetch OI history for per-bar oi_change_pct / oi_slope_5m
    oi_series: dict[int, float] = {}
    try:
        raw_oi = await client.fetch_oi_history_raw(sym, period="5m", limit=500)
        oi_series = _build_oi_series(raw_oi)
    except Exception:
        pass

    start = max(_WARMUP, work.height - bars - 1)
    written = 0
    base_row = copy.deepcopy(row)
    for idx in range(start, work.height - 1):
        try:
            closed = _closed_snap(work, sym, idx)
        except Exception:
            continue
        if closed.get("status") == "empty" or not closed.get("closed_bar"):
            continue
        # Patch per-bar FRAME_SOURCES (zscore30, delta_ratio, cvd) from work[idx].
        # _closed_frame_block always reads work[-2]; in backfill idx varies.
        from hunt_core.features.feature_engine import _FRAME_SOURCES
        work_cols = getattr(work, "columns", [])
        for _fn in _FRAME_SOURCES:
            if _fn not in closed and _fn in work_cols:
                try:
                    closed[_fn] = work.item(idx, _fn)
                except Exception:
                    pass
        hist = copy.deepcopy(base_row)
        hist["ts"] = _bar_ts(work, idx)
        hist["price"] = float(closed.get("close") or work.item(idx, "close"))
        hist.setdefault("timeframes", {})["15m_closed"] = closed
        map_feats = _historical_map_features(prepared, sym, idx, hist["price"])
        market = hist.setdefault("market", {})
        if isinstance(market, dict):
            if map_feats:
                market.update(map_feats)
            if oi_series:
                bar_ms = _bar_ts_ms(work, idx)
                prev_ms = _bar_ts_ms(work, idx - 1) if idx > 0 else None
                if bar_ms is not None and prev_ms is not None:
                    oi_metrics = _per_bar_oi_metrics(oi_series, bar_ms, prev_ms)
                    for k, v in oi_metrics.items():
                        if v is not None:
                            market[k] = v
                            # Inject under all aliases so _prepared_value finds it
                            if k == "oi_change_pct":
                                market["oi_chg_1h"] = v
        try:
            vector = build_feature_vector(
                prepared, hist, symbol=sym, tf="15m", require_closed=True
            )
        except FeatureExtractError:
            continue
        writer.enqueue(sym, hist["ts"], "15m", vector.to_dict())
        written += 1

    return written


async def _run(symbols: tuple[str, ...], *, bars: int, wipe: bool) -> int:
    from hunt_core.bootstrap import bootstrap
    from hunt_core.market.factory import create_hunt_market_plane_from_settings

    bootstrap()
    settings = load_settings()
    if wipe and LAKE_PARQUET.exists():
        import shutil

        shutil.rmtree(LAKE_PARQUET)
        LAKE_PARQUET.mkdir(parents=True, exist_ok=True)
        print(f"wiped {LAKE_PARQUET}")

    plane = await create_hunt_market_plane_from_settings(settings)
    writer = FeatureLakeWriter()
    total = 0
    try:
        await plane.client.load_markets()
        batch_ctx = await _fetch_batch_context(plane.client, settings)
        for sym in symbols:
            n = await _backfill_symbol(
                plane.client,
                settings,
                sym,
                bars=bars,
                writer=writer,
                batch_ctx=batch_ctx,
            )
            print(f"  {sym}: {n} bars enqueued")
            total += n
    finally:
        writer.close()
        await plane.aclose()

    print(f"lake_backfill done | symbols={len(symbols)} rows={total} path={LAKE_PARQUET}")
    return 0 if total > 0 else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Backfill hunt feature lake from CCXT 15m history")
    p.add_argument("symbols", nargs="*", default=list(DEFAULT_SYMBOLS))
    p.add_argument("--bars", type=int, default=DEFAULT_BARS)
    p.add_argument("--wipe", action="store_true", help="delete existing parquet lake first")
    args = p.parse_args(argv)
    symbols = tuple(dict.fromkeys(s.upper() for s in args.symbols))
    return asyncio.run(_run(symbols, bars=args.bars, wipe=args.wipe))


if __name__ == "__main__":
    raise SystemExit(main())
