#!/usr/bin/env python3
"""One-shot live reversal probe: all TFs + full indicator panel for one symbol."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from datetime import UTC, datetime
from typing import Any

from hunt_watch.bootstrap import bootstrap

bootstrap()

from hunt_watch.lifecycle import assess_hunt_lifecycle

from hunt_core.data_readiness import kline_fetch_limit
from hunt_core.domain.config import load_settings
from hunt_core.domain.schemas import SymbolFrames, UniverseSymbol
from hunt_core.errors import DEFENSIVE_EXC
from hunt_core.features.pivots import _pivot_rows, with_spec_columns
from hunt_core.features.prepare import _prepare_frame, min_required_bars, prepare_symbol
from hunt_core.market import HuntCcxtClient

# Same TF set as hunt watch
TF_KEYS = ("1m", "3m", "5m", "15m", "1h", "4h", "1d")

# Indicator columns worth printing for reversal hunting (exclude raw OHLCV duplicates)
REVERSAL_COLS = (
    "rsi14",
    "adx14",
    "plus_di14",
    "minus_di14",
    "atr14",
    "atr_pct",
    "macd_hist",
    "macd_line",
    "stoch_k14",
    "stoch_d14",
    "stoch_rsi14",
    "bb_pct_b",
    "bb_width",
    "bb_width_pctile50",
    "squeeze_on",
    "squeeze_off",
    "squeeze_hist",
    "supertrend_dir",
    "vwap_deviation_atr14",
    "vwap_deviation_z20",
    "obv_above_ema",
    "cmf20",
    "mfi14",
    "fisher",
    "fisher_signal",
    "willr14",
    "cci20",
    "zscore30",
    "donchian_high20",
    "donchian_low20",
    "chandelier_dir",
    "psar_reversal",
    "delta_ratio",
    "session_cvd",
    "rolling_cvd_24h",
    "close_ols_slope_atr20",
    "realized_vol_20",
    "volume_ratio20",
)


def _col(df: Any, name: str, *, idx: int = -1) -> float | None:
    if df is None or df.is_empty() or name not in df.columns:
        return None
    try:
        v = float(df.item(idx, name))
        return None if not math.isfinite(v) else v
    except (TypeError, ValueError, IndexError):
        return None


def _rsi_div(df: Any, *, idx: int = -1) -> dict[str, bool]:
    if df is None or df.is_empty() or "rsi14" not in df.columns:
        return {"bear_div": False, "bull_div": False}
    spec = with_spec_columns(df)
    highs = _pivot_rows(spec, price_column="high", indicator_column="rsi14", pivot="high")
    lows = _pivot_rows(spec, price_column="low", indicator_column="rsi14", pivot="low")
    bear = bull = False
    if len(highs) >= 2:
        o, n = highs[-2], highs[-1]
        bear = n["price"] > o["price"] and n["indicator"] < o["indicator"]
    if len(lows) >= 2:
        o, n = lows[-2], lows[-1]
        bull = n["price"] < o["price"] and n["indicator"] > o["indicator"]
    return {"bear_div": bear, "bull_div": bull}


def _reversal_score(snap: dict[str, Any]) -> tuple[int, list[str]]:
    """Heuristic reversal votes from one TF snapshot."""
    score = 0
    notes: list[str] = []
    rsi = snap.get("rsi14")
    if rsi is not None:
        if rsi <= 30:
            score += 2
            notes.append("rsi_oversold")
        elif rsi >= 70:
            score -= 2
            notes.append("rsi_overbought")
    vdev = snap.get("vwap_deviation_atr14")
    if vdev is not None:
        if vdev <= -1.5:
            score += 2
            notes.append("vwap_stretch_down")
        elif vdev >= 1.5:
            score -= 2
            notes.append("vwap_stretch_up")
    if snap.get("bull_div"):
        score += 3
        notes.append("bull_rsi_div")
    if snap.get("bear_div"):
        score -= 3
        notes.append("bear_rsi_div")
    st = snap.get("supertrend_dir")
    if st == 1:
        score += 1
        notes.append("supertrend_up")
    elif st == -1:
        score -= 1
        notes.append("supertrend_down")
    if snap.get("squeeze_on"):
        notes.append("squeeze_compressed")
    sqh = snap.get("squeeze_hist")
    if sqh is not None and float(sqh) > 0:
        score += 1
        notes.append("squeeze_releasing_up")
    elif sqh is not None and float(sqh) < 0:
        score -= 1
        notes.append("squeeze_releasing_down")
    adx = snap.get("adx14")
    pdi = snap.get("plus_di14")
    mdi = snap.get("minus_di14")
    if adx and adx >= 25 and pdi and mdi:
        if pdi > mdi * 1.2:
            score += 1
            notes.append("adx_bull")
        elif mdi > pdi * 1.2:
            score -= 1
            notes.append("adx_bear")
    if snap.get("psar_reversal"):
        notes.append("psar_flip")
    obv = snap.get("obv_above_ema")
    if obv is True:
        score += 1
        notes.append("obv_above_ema")
    elif obv is False:
        score -= 1
        notes.append("obv_below_ema")
    return score, notes


def _tf_panel(df: Any, *, tf: str, closed: bool = False) -> dict[str, Any]:
    if df is None or df.is_empty():
        return {"tf": tf, "status": "empty"}
    idx = -2 if closed and df.height >= 2 else -1
    out: dict[str, Any] = {"tf": tf, "bars": int(df.height), "close": _col(df, "close", idx=idx)}
    for name in REVERSAL_COLS:
        if name in df.columns:
            val = _col(df, name, idx=idx)
            if val is not None:
                out[name] = round(val, 4) if isinstance(val, float) else val
    div = _rsi_div(df, idx=idx)
    out.update(div)
    vote, notes = _reversal_score(out)
    out["reversal_score"] = vote
    out["reversal_notes"] = notes
    out["bias"] = "long_reversal" if vote >= 3 else ("short_reversal" if vote <= -3 else "neutral")
    return out


async def _safe(coro: Any) -> Any:
    try:
        return await coro
    except DEFENSIVE_EXC:
        return None


async def run(symbol: str) -> dict[str, Any]:
    sym = symbol.upper()
    settings = load_settings()
    minimums = min_required_bars(
        min_bars_15m=settings.filters.min_bars_15m,
        min_bars_1h=settings.filters.min_bars_1h,
        min_bars_4h=settings.filters.min_bars_4h,
    )
    limits = {
        "1m": min(1500, max(1440, kline_fetch_limit(int(minimums.get("5m", 300)), "5m") * 2)),
        "3m": 480,
        "5m": kline_fetch_limit(int(minimums.get("5m", 300)), "5m"),
        "15m": kline_fetch_limit(int(minimums.get("15m", 400)), "15m"),
        "1h": kline_fetch_limit(int(minimums.get("1h", 400)), "1h"),
        "4h": kline_fetch_limit(int(minimums.get("4h", 200)), "4h"),
        "1d": 90,
    }
    client = HuntCcxtClient.from_settings(settings)
    try:
        exchange = await _safe(client.fetch_exchange_symbols()) or []
        ticker_raw = await _safe(client.fetch_ticker_24h()) or []
        ticker = next((t for t in ticker_raw if str(t.get("symbol")) == sym), None)
        meta = next((r for r in exchange if r.symbol == sym), None)
        if ticker is None or meta is None:
            return {"symbol": sym, "error": "symbol_or_ticker_missing"}

        price = float(ticker.get("last_price") or 0)
        kline_map: dict[str, Any] = {}
        for tf in TF_KEYS:
            kline_map[tf] = await _safe(client.fetch_klines_cached(sym, tf, limit=limits[tf]))

        pack_tasks = {
            "oi": client.fetch_open_interest(sym),
            "oi_series": client.fetch_open_interest_series(sym, period="5m", limit=48),
            "gls_series": client.fetch_global_ls_series(sym, period="5m", limit=48),
            "ls_1h": client.fetch_long_short_ratio(sym, period="1h"),
            "global_ls_1h": client.fetch_global_ls_ratio(sym, period="1h"),
            "taker_1h": client.fetch_taker_ratio(sym, period="1h"),
            "funding": client.fetch_funding_rate(sym),
            "basis_5m": client.fetch_basis(sym, period="5m"),
            "book": client.fetch_order_book_depth_snapshot(sym, limit=100),
        }
        pack_results = await asyncio.gather(*pack_tasks.values(), return_exceptions=True)
        pack = {
            k: (None if isinstance(v, BaseException) else v)
            for k, v in zip(pack_tasks.keys(), pack_results, strict=True)
        }

        item = UniverseSymbol(
            symbol=sym,
            base_asset=meta.base_asset,
            quote_asset=meta.quote_asset,
            contract_type=meta.contract_type,
            status=meta.status,
            onboard_date_ms=meta.onboard_date_ms,
            quote_volume=float(ticker.get("quote_volume") or 0),
            price_change_pct=float(ticker.get("price_change_percent") or 0),
            last_price=price,
            shortlist_bucket="reversal_probe",
            seed_source="reversal_probe",
            strategy_fits=(),
        )
        frames = SymbolFrames(
            symbol=sym,
            df_15m=kline_map.get("15m"),
            df_1h=kline_map.get("1h"),
            df_5m=kline_map.get("5m"),
            df_4h=kline_map.get("4h"),
            bid_price=(pack.get("book") or {}).get("bid_price") if isinstance(pack.get("book"), dict) else None,
            ask_price=(pack.get("book") or {}).get("ask_price") if isinstance(pack.get("book"), dict) else None,
            bid_qty=(pack.get("book") or {}).get("bid_qty") if isinstance(pack.get("book"), dict) else None,
            ask_qty=(pack.get("book") or {}).get("ask_qty") if isinstance(pack.get("book"), dict) else None,
            frame_source_flags=("reversal_probe",),
        )
        prepared = prepare_symbol(item, frames, minimums=minimums, settings=settings)
        work_map = {
            "1m": _prepare_frame(kline_map["1m"]) if kline_map.get("1m") is not None else None,
            "3m": _prepare_frame(kline_map["3m"]) if kline_map.get("3m") is not None else None,
            "5m": prepared.work_5m if prepared else None,
            "15m": prepared.work_15m if prepared else None,
            "1h": prepared.work_1h if prepared else None,
            "4h": prepared.work_4h if prepared else None,
            "1d": _prepare_frame(kline_map["1d"]) if kline_map.get("1d") is not None else None,
        }

        panels: list[dict[str, Any]] = []
        for tf in TF_KEYS:
            df = work_map.get(tf)
            panels.append(_tf_panel(df, tf=tf, closed=False))
            if tf in {"5m", "15m", "1h"}:
                panels.append(_tf_panel(df, tf=f"{tf}_closed", closed=True))

        # Aggregate reversal consensus
        scores = [p["reversal_score"] for p in panels if "reversal_score" in p]
        avg = round(sum(scores) / len(scores), 2) if scores else 0.0
        long_votes = sum(1 for p in panels if p.get("bias") == "long_reversal")
        short_votes = sum(1 for p in panels if p.get("bias") == "short_reversal")

        # Hunt lifecycle (uses 1h impulse like BEAT)
        ih, il = 0.0, 0.0
        if work_map.get("1h") is not None and not work_map["1h"].is_empty():
            highs = [float(x) for x in work_map["1h"]["high"].to_list()[-48:]]
            lows = [float(x) for x in work_map["1h"]["low"].to_list()[-48:]]
            ih, il = max(highs), min(lows)
        sess_hi = max(float(x) for x in work_map["1m"]["high"].to_list()[-1440:]) if work_map.get("1m") is not None and work_map["1m"].height else ih
        sess_lo = min(float(x) for x in work_map["1m"]["low"].to_list()[-1440:]) if work_map.get("1m") is not None and work_map["1m"].height else il
        tf_snap = {tf.replace("_closed", ""): panels[i] for i, tf in enumerate([p["tf"] for p in panels])}
        lifecycle = assess_hunt_lifecycle(
            price=price,
            hunt_high=ih,
            hunt_low=il,
            session={"high_24h": sess_hi, "low_24h": sess_lo, "pos_in_range": (price - sess_lo) / (sess_hi - sess_lo) if sess_hi > sess_lo else 0.5},
            tf={},
            market={"taker_1h": pack.get("taker_1h"), "oi": pack.get("oi")},
        )

        indicator_cols = sorted(
            c for c in (work_map["1h"].columns if work_map.get("1h") is not None else [])
            if c not in {"open", "high", "low", "close", "open_time", "volume", "quote_volume", "taker_buy_volume", "trades"}
        )

        return {
            "ts": datetime.now(UTC).isoformat(),
            "symbol": sym,
            "price": price,
            "chg_24h_pct": round(float(ticker.get("price_change_percent") or 0), 2),
            "vol_24h_m": round(float(ticker.get("quote_volume") or 0) / 1e6, 1),
            "market_enrichment": {
                "oi": pack.get("oi"),
                "funding": pack.get("funding"),
                "ls_1h": pack.get("ls_1h"),
                "global_ls_1h": pack.get("global_ls_1h"),
                "taker_1h": pack.get("taker_1h"),
                "basis_5m": pack.get("basis_5m"),
                "oi_series_len": len(pack.get("oi_series") or []),
                "gls_series_len": len(pack.get("gls_series") or []),
            },
            "indicator_column_count_1h": len(indicator_cols),
            "indicator_columns_1h": indicator_cols,
            "panels": panels,
            "consensus": {
                "avg_reversal_score": avg,
                "long_reversal_votes": long_votes,
                "short_reversal_votes": short_votes,
                "verdict": (
                    "LONG_REVERSAL_ZONE"
                    if avg >= 2 and long_votes > short_votes
                    else ("SHORT_REVERSAL_ZONE" if avg <= -2 and short_votes > long_votes else "NO_CLEAR_REVERSAL")
                ),
            },
            "lifecycle": {
                "phase": lifecycle.phase.value,
                "bias": lifecycle.recommended_bias,
                "fall_from_high_pct": lifecycle.fall_from_high_pct,
                "bounce_from_low_pct": lifecycle.bounce_from_low_pct,
                "invalidate_short": lifecycle.invalidate_short,
                "reasons": list(lifecycle.reasons),
            },
        }
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Live reversal probe — all TFs + indicators")
    parser.add_argument("--symbol", default="BEATUSDT")
    parser.add_argument("--json", action="store_true", help="Raw JSON output")
    args = parser.parse_args()
    result = asyncio.run(run(args.symbol))
    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return

    print(f"\n=== REVERSAL PROBE {result.get('symbol')} @ {result.get('price')} ===")
    print(f"24h: {result.get('chg_24h_pct')}%  vol: {result.get('vol_24h_m')}M")
    c = result.get("consensus") or {}
    print(f"VERDICT: {c.get('verdict')}  avg_score={c.get('avg_reversal_score')}  long_votes={c.get('long_reversal_votes')}  short_votes={c.get('short_reversal_votes')}")
    lc = result.get("lifecycle") or {}
    print(f"LIFECYCLE: {lc.get('phase')} bias={lc.get('bias')} fall={lc.get('fall_from_high_pct')}% bounce={lc.get('bounce_from_low_pct')}%")
    print(f"Indicators on 1h frame: {result.get('indicator_column_count_1h')} cols")
    print("\n--- Per-TF reversal panel ---")
    for p in result.get("panels") or []:
        if p.get("status") == "empty":
            print(f"  {p['tf']:12s} EMPTY")
            continue
        notes = ",".join(p.get("reversal_notes") or []) or "—"
        print(
            f"  {p['tf']:12s} close={p.get('close')} rsi={p.get('rsi14')} "
            f"st_dir={p.get('supertrend_dir')} vwap_atr={p.get('vwap_deviation_atr14')} "
            f"bb_pctile={p.get('bb_width_pctile50')} score={p.get('reversal_score'):+d} "
            f"bias={p.get('bias')} | {notes}"
        )
    me = result.get("market_enrichment") or {}
    print(f"\nEnrichment: OI={me.get('oi')} fund={me.get('funding')} gls={me.get('global_ls_1h')} taker={me.get('taker_1h')}")


if __name__ == "__main__":
    main()
