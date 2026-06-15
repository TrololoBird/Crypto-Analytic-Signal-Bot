"""Full tick assembly orchestration (P2 — snapshot + scoring + lifecycle)."""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from hunt_core.data.collect import (
    SnapshotTier,
    _book_from_pack,
    _fetch_rest_pack,
    _overlay_ws_market,
    kline_limits,
    resolve_kline_map,
    safe_fetch,
    ws_orderflow_fresh,
)
from hunt_core.data.completeness import (
    REQUIRED_SIGNAL_KLINE_TFS,
    audit_kline_integrity,
    repair_kline_map_gaps,
)
from hunt_core.detect.scoring import (
    confirm_dump as _confirm_dump,
    confirm_long as _confirm_long,
    dump_analysis as _dump_analysis,
    long_analysis as _long_analysis,
    phase_dump as _phase,
    phase_long as _phase_long,
)
from hunt_core.analysis.pinned_deep import prepare_htf_frame
from hunt_core.analysis.deep_signal import build_liquidity_scenarios
from hunt_core.scan._engine_impl import enrich_dump_setup, enrich_long_setup
from hunt_core.regime.leg_fsm import (
    apply_short_invalidation,
    assess_hunt_lifecycle,
    attach_regime,
    effective_support_break,
    lifecycle_to_dict,
    stabilize as stabilize_lifecycle,
)
from hunt_core.features.prepare import _prepare_frame, prepare_symbol
from hunt_core.features.prepare_columns import (
    book_walls_from_depth,
    patch_work_4h,
    resolve_prepare_groups_for_symbol,
    should_use_young_lite_path,
)
from hunt_core.features import snapshot as _snapshot_mod
from hunt_core.features.snapshot import (
    WatchMode,
    apply_cross_exchange_flat,
    apply_rest_enrichments_local,
    attach_cross_market_fields,
    attach_pp_flags,
    attach_research_setup_fields,
    btc_beta_1h,
    btc_corr_1h,
    col as _col,
    data_quality_report,
    distribution_stats,
    enrich_work_research_frames,
    format_squeeze_telegram,
    impulse_context,
    kline_integrity_reject,
    lite_prepared,
    market_snapshot,
    merge_research_tf_fields,
    merge_ws_kline_closed,
    regime_snapshot,
    session_stats,
    squeeze_watch,
    tf_snapshot,
    tf_snapshot_for_symbol,
    tf_snapshot_lite,
)
from hunt_core.gate.delivery import liquidity_skip_reason
from hunt_core.data.universe import PINNED_SYMBOLS
from hunt_core.data_readiness import assess_symbol_data_readiness, kline_fetch_limit
from hunt_core.domain.market_regime import symbol_regime_features
from hunt_core.domain.schemas import SymbolFrames, UniverseSymbol
from hunt_core.features.fib import leg_fib_levels
from hunt_core.market import HuntCcxtClient, HuntCcxtSpotCompanion, HuntCcxtStreams
from hunt_core.market.client import normalize_depth_levels
from hunt_core.market.live_price import resolve_live_price
from hunt_core.runtime.settings import SymbolStateStore, merge_hunt_extremes

LOG = logging.getLogger("hunt_core.runtime.tick_assembly")

# Backward-compat re-exports
kline_limits = kline_limits
safe_fetch = safe_fetch
squeeze_watch = squeeze_watch
format_squeeze_telegram = format_squeeze_telegram

async def snapshot_symbol(
    client: HuntCcxtClient,
    settings: Any,
    minimums: dict[str, int],
    symbol: str,
    *,
    watch_mode: WatchMode,
    prev_oi: float | None,
    premium_all: dict[str, dict[str, float]],
    funding_info_all: dict[str, dict[str, float | int]],
    btc_work_1h: Any | None,
    exchange_by_sym: dict[str, Any],
    ticker_by_sym: dict[str, dict[str, Any]],
    ws_feed: HuntCcxtStreams | None = None,
    spot_companion: HuntCcxtSpotCompanion | None = None,
    stagger_klines_ms: int = 0,
    pump_stats: dict[str, Any] | None = None,
    tier: SnapshotTier = "full",
    symbol_state: SymbolStateStore | None = None,
) -> dict[str, Any]:
    meta = exchange_by_sym.get(symbol)
    ticker = ticker_by_sym.get(symbol)
    if meta is None or ticker is None:
        return {
            "ts": datetime.now(UTC).isoformat(),
            "symbol": symbol,
            "error": f"symbol_meta_or_ticker_missing:{symbol}",
        }
    price = float(ticker.get("last_price") or 0)
    market_row = {
        "symbol": symbol,
        "base_asset": meta.base_asset,
        "quote_asset": meta.quote_asset,
        "contract_type": meta.contract_type,
        "status": meta.status,
        "onboard_date_ms": meta.onboard_date_ms,
        "quote_volume": float(ticker.get("quote_volume") or 0),
        "price_change_percent": float(ticker.get("price_change_percent") or 0),
        "price_change_pct": float(ticker.get("price_change_percent") or 0),
        "last_price": price,
        "trade_count": float(ticker.get("trade_count") or 0),
    }
    item = UniverseSymbol(
        symbol=symbol,
        base_asset=meta.base_asset,
        quote_asset=meta.quote_asset,
        contract_type=meta.contract_type,
        status=meta.status,
        onboard_date_ms=meta.onboard_date_ms,
        quote_volume=market_row["quote_volume"],
        price_change_pct=market_row["price_change_percent"],
        last_price=price,
        shortlist_bucket="dump_watch",
        seed_source="dump_minute_watch",
        strategy_fits=(),
    )
    limits = kline_limits(minimums, symbol)
    if stagger_klines_ms > 0 and tier == "full":
        _base_tfs = ("1m", "5m", "15m", "1h", "4h", "1d")
        tf_order = _base_tfs + (("1w",) if "1w" in limits else ())
        kline_map: dict[str, Any] = {}
        fetch_errors: dict[str, str] = {}
        for name in tf_order:
            res = await safe_fetch(
                client.fetch_klines_cached(symbol, name, limit=limits[name]),
                context=f"klines.{name}",
            )
            kline_map[name] = res
            if res is None:
                fetch_errors[name] = "fetch_failed"
            await asyncio.sleep(stagger_klines_ms / 1000.0)
    else:
        kline_map, fetch_errors = await resolve_kline_map(
            client, symbol, limits, tier=tier, safe_fetch=safe_fetch
        )
    kline_map, fetch_errors = await repair_kline_map_gaps(
        client,
        symbol,
        kline_map,
        fetch_errors,
        required_tfs=REQUIRED_SIGNAL_KLINE_TFS,
    )
    integrity = audit_kline_integrity(
        kline_map,
        symbol=symbol,
        settings=settings,
        required_tfs=REQUIRED_SIGNAL_KLINE_TFS,
        fetch_errors=fetch_errors,
    )
    if not integrity.complete:
        return kline_integrity_reject(
            symbol=symbol,
            report=integrity,
            fetch_errors=fetch_errors,
        )
    df_1m = kline_map["1m"]
    df_5m = kline_map["5m"]
    pack = await _fetch_rest_pack(client, symbol, tier=tier, ws_feed=ws_feed)
    # leverageBracket is signed USER_DATA — public-only hunt uses default liq tiers
    liq_skip = liquidity_skip_reason(
        quote_volume=market_row["quote_volume"],
        oi=float(pack.get("oi") or 0) if pack.get("oi") is not None else None,
        last_price=price,
        symbol=symbol,
    )
    if liq_skip:
        return {
            "ts": datetime.now(UTC).isoformat(),
            "symbol": symbol,
            "error": liq_skip,
            "liquidity_skip": True,
        }
    book = _book_from_pack(pack)
    depth_raw = pack.get("book_depth") if isinstance(pack.get("book_depth"), dict) else {}
    book_bids = normalize_depth_levels(depth_raw.get("bids") or depth_raw.get("bid_levels"))
    book_asks = normalize_depth_levels(depth_raw.get("asks") or depth_raw.get("ask_levels"))
    frames = SymbolFrames(
        symbol=symbol,
        df_15m=kline_map["15m"],
        df_1h=kline_map["1h"],
        df_5m=df_5m,
        df_4h=kline_map["4h"],
        bid_price=book.get("bid_price"),
        ask_price=book.get("ask_price"),
        bid_qty=book.get("bid_qty"),
        ask_qty=book.get("ask_qty"),
        book_bids=book_bids or None,
        book_asks=book_asks or None,
        frame_source_flags=("frames_rest_full",),
    )

    prepared = prepare_symbol(item, frames, minimums=minimums, settings=settings)
    young_listing = False
    bars_4h = int(kline_map["4h"].height if kline_map.get("4h") is not None else 0)
    bars_1h = int(kline_map["1h"].height if kline_map.get("1h") is not None else 0)
    if prepared is None:
        young_listing = True
        if should_use_young_lite_path(bars_4h=bars_4h, bars_1h=bars_1h):
            # SOXL/SKHYNIX: native 4h prepare empty despite 50–160 raw bars — synth from 1h.
            prepared = lite_prepared(kline_map, symbol=symbol)
        else:
            relaxed = {"5m": 144, "15m": 96, "1h": 24, "4h": 6}
            prepared = prepare_symbol(item, frames, minimums=relaxed, settings=settings)
            if prepared is None:
                prepared = lite_prepared(kline_map, symbol=symbol)
            else:
                patch_work_4h(prepared, kline_map, symbol=symbol)
    else:
        patch_work_4h(prepared, kline_map, symbol=symbol)

    prep_groups = resolve_prepare_groups_for_symbol(symbol)
    work_1m = _prepare_frame(df_1m, active_groups=prep_groups)
    delta_raw = None
    if prepared.work_15m is not None and not prepared.work_15m.is_empty():
        delta_raw = _col(prepared.work_15m, "delta_ratio", None)
    delta = None if delta_raw is None else float(delta_raw)
    premium_row = premium_all.get(symbol) or premium_all.get(symbol.upper())
    funding_info = funding_info_all.get(symbol) or funding_info_all.get(symbol.upper())
    apply_rest_enrichments_local(
        prepared,
        client=client,
        symbol=symbol,
        pack=pack,
        book=book,
        premium_row=premium_row,
        funding_info=funding_info,
        delta=delta,
    )
    if not young_listing:
        readiness = assess_symbol_data_readiness(prepared, settings, universe_item=item)
        if not readiness.ready:
            reason = readiness.reason or "data.not_ready"
            return {
                "ts": datetime.now(UTC).isoformat(),
                "symbol": symbol,
                "error": reason,
                "no_signal_reason": reason,
                "data_readiness": {
                    "ready": False,
                    "reason": reason,
                    "details": dict(readiness.details),
                },
            }

    if symbol != "BTCUSDT" and btc_work_1h is not None:
        corr = btc_corr_1h(prepared.work_1h, btc_work_1h)
        if corr is not None:
            prepared.btc_corr_1h = corr
        beta = btc_beta_1h(prepared.work_1h, btc_work_1h)
        if beta is not None:
            prepared.btc_beta_1h = beta

    enrich_work_research_frames(prepared)

    impulse = impulse_context(prepared.work_4h, prepared.work_1h, symbol)
    ih4, il4 = impulse["impulse_high_4h"], impulse["impulse_low_4h"]
    rest_h, rest_l = impulse["hunt_high"], impulse["hunt_low"]
    fib_4h = leg_fib_levels(ih4, il4, direction="down")
    session = session_stats(work_1m)

    if kline_map.get("1d") is not None:
        work_1d_snap = (
            prepare_htf_frame(kline_map["1d"], symbol)
            if symbol in PINNED_SYMBOLS
            else _prepare_frame(kline_map["1d"], active_groups=prep_groups)
        )
        if work_1d_snap is not None and not work_1d_snap.is_empty():
            probe = tf_snapshot_for_symbol(work_1d_snap, symbol)
            tf_1d = (
                probe
                if probe.get("status") != "empty" and probe.get("rsi14") is not None
                else tf_snapshot_lite(kline_map["1d"])
            )
        else:
            tf_1d = tf_snapshot_lite(kline_map["1d"])
    else:
        tf_1d = {"status": "empty"}

    tf = {
        "1m": tf_snapshot(work_1m),
        "1m_closed": tf_snapshot(work_1m, closed=True),
        "3m": {"status": "empty"},
        "3m_closed": {"status": "empty"},
        "5m": tf_snapshot(prepared.work_5m),
        "5m_closed": tf_snapshot(prepared.work_5m, closed=True, candle_patterns=True),
        "15m": attach_pp_flags(tf_snapshot_for_symbol(prepared.work_15m, symbol), prepared.work_15m),
        "15m_closed": attach_pp_flags(tf_snapshot_for_symbol(prepared.work_15m, symbol, closed=True, candle_patterns=True), prepared.work_15m, closed=True),
        "1h": attach_pp_flags(tf_snapshot_for_symbol(prepared.work_1h, symbol, rsi_trendline=True, hidden_stoch_div=True, chart_patterns=True), prepared.work_1h),
        "1h_closed": attach_pp_flags(tf_snapshot_for_symbol(prepared.work_1h, symbol, closed=True, rsi_trendline=True, hidden_stoch_div=True, chart_patterns=True), prepared.work_1h, closed=True),
        "4h": tf_snapshot_for_symbol(prepared.work_4h, symbol, hidden_stoch_div=True, chart_patterns=True),
        "4h_closed": tf_snapshot_for_symbol(prepared.work_4h, symbol, closed=True, hidden_stoch_div=True, chart_patterns=True),
        "1d": tf_1d,
    }
    if "1w" in limits and kline_map.get("1w") is not None:
        work_1w = (
            prepare_htf_frame(kline_map["1w"], symbol)
            if symbol in PINNED_SYMBOLS
            else _prepare_frame(kline_map["1w"], active_groups=prep_groups)
        )
        tf["1w"] = (
            tf_snapshot_for_symbol(work_1w, symbol)
            if work_1w is not None and not work_1w.is_empty()
            else tf_snapshot_lite(kline_map["1w"])
        )
    merge_ws_kline_closed(tf, symbol, ws_feed, tf_key="1m_closed")
    merge_ws_kline_closed(tf, symbol, ws_feed, tf_key="5m_closed")
    merge_ws_kline_closed(tf, symbol, ws_feed, tf_key="15m_closed")
    tf["stale_15m"] = _snapshot_mod._stale_15m_flag(tf)
    for _stf in REQUIRED_SIGNAL_KLINE_TFS:
        closed_key = f"{_stf}_closed" if _stf != "1m" else "1m_closed"
        block = tf.get(closed_key) or tf.get(_stf) or {}
        close_ms = block.get("close_time_ms") if isinstance(block, dict) else None
        if close_ms is None:
            tf[f"stale_{_stf}"] = True
        else:
            from hunt_core.data.completeness import TF_MS

            interval = TF_MS.get(_stf, 300_000)
            age = int(datetime.now(UTC).timestamp() * 1000) - int(close_ms)
            tf[f"stale_{_stf}"] = age > int(interval * 2.5)
    if prepared.work_15m is not None and not prepared.work_15m.is_empty():
        _regime_feats = symbol_regime_features(prepared.work_15m)
        for _tf_key in ("15m", "15m_closed"):
            _block = tf.get(_tf_key)
            if isinstance(_block, dict) and _block.get("status") != "empty":
                if _regime_feats.get("return_entropy_50") is not None:
                    _block["return_entropy_50"] = _regime_feats["return_entropy_50"]
                if _regime_feats.get("volume_regime_break"):
                    _block["volume_regime_break"] = True
    ws_snap = ws_feed.snapshot(symbol) if ws_feed is not None else None
    _overlay_ws_market(prepared, ws_snap)
    live_px, live_src = resolve_live_price(
        symbol,
        ws_feed=ws_feed,
        book=book,
        ws_snap=ws_snap,
        fallback=price,
    )
    if live_px > 0:
        price = live_px
        market_row["last_price"] = live_px
    spot_extra = (
        spot_companion.enrichments_for(symbol) if spot_companion is not None else None
    )
    market = market_snapshot(
        prepared,
        pack=pack,
        book=book,
        premium_row=premium_row,
        ticker=ticker,
        ws_snap=ws_snap,
        spot_extra=spot_extra,
    )
    await attach_cross_market_fields(
        market,
        client=client,
        symbol=symbol,
        ws_feed=ws_feed,
    )
    regime = regime_snapshot(prepared)
    if prepared.work_15m is not None and not prepared.work_15m.is_empty():
        regime.update(symbol_regime_features(prepared.work_15m))
    hunt_h, hunt_l, session_mem = merge_hunt_extremes(
        symbol,
        price=price,
        rest_hunt_high=rest_h,
        rest_hunt_low=rest_l,
        lifecycle_phase="",
        market=market,
    )
    fib_hunt = leg_fib_levels(hunt_h, hunt_l, direction="down")
    fib = {**fib_4h, "hunt": fib_hunt}
    result: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(),
        "snapshot_tier": tier,
        "symbol": symbol,
        "watch_mode": watch_mode,
        "young_listing": young_listing,
        "price": price,
        "price_source": live_src if live_px > 0 else "ticker_batch",
        "chg_24h_pct": round(float(ticker.get("price_change_percent") or 0), 2),
        "vol_24h_m": market.get("vol_24h_m"),
        # NOTE: "positioning" was a byte-identical alias of "market" — it
        # doubled every JSONL row (~45% of file size). Readers fall back
        # market -> positioning for old rows.
        "market": market,
        "regime": regime,
        "timeframes": tf,
        "session": session,
        "squeeze": squeeze_watch(tf, market),
        "impulse": impulse,
        "impulse_high": hunt_h,
        "impulse_low": hunt_l,
        "session_memory": session_mem,
        "fib": fib,
        "kline_limits": limits,
        "data_quality": data_quality_report(
            prepared,
            frames=frames,
            df_1m=df_1m,
            pack=pack,
            book=book,
            tf=tf,
        ),
        "book_walls": book_walls_from_depth(pack.get("book_depth")),
        "cross_microstructure": None,
        "_prepared": prepared,
    }

    if symbol in PINNED_SYMBOLS:
        try:
            import polars as pl
            from hunt_core.features.prepare_columns import align_series_to_klines

            mark_1d, index_1d = await asyncio.gather(
                safe_fetch(client.fetch_mark_ohlcv(symbol, "1d", limit=30)),
                safe_fetch(client.fetch_index_ohlcv(symbol, "1d", limit=30)),
            )
            if (
                mark_1d is not None
                and index_1d is not None
                and not mark_1d.is_empty()
                and not index_1d.is_empty()
            ):
                aligned = align_series_to_klines(
                    mark_1d.rename({"close": "mark_close"}),
                    index_1d.rename({"close": "index_close"}),
                )
                if not aligned.is_empty():
                    latest_mark = float(aligned["mark_close"][-1])
                    latest_index = float(aligned["index_close"][-1])
                    if latest_index > 0:
                        result["mark_index_divergence_pct"] = round(
                            (latest_mark - latest_index) / latest_index * 100.0, 4
                        )
                    basis = (
                        (pl.col("mark_close") - pl.col("index_close"))
                        / pl.col("index_close")
                        * 100.0
                    )
                    basis_s = aligned.with_columns(basis.alias("basis_pct"))["basis_pct"]
                    if basis_s.len() >= 7:
                        result["mark_index_slope_7d"] = round(
                            float(basis_s[-1] - basis_s[-7]), 4
                        )
        except Exception:
            pass
        try:
            from hunt_core.market.cross import attach_cross_microstructure

            await attach_cross_microstructure(client, result)
            cx_walls = (result.get("cross_microstructure") or {}).get("book_walls")
            if isinstance(cx_walls, dict) and cx_walls.get("bid_levels"):
                result["book_walls"] = cx_walls
        except Exception as exc:
            LOG.warning("cross_microstructure_snapshot_failed | symbol=%s error=%s", symbol, exc)
    try:
        result["liquidity_scenarios"] = build_liquidity_scenarios(result).to_dict()
    except Exception as exc:
        LOG.warning("liquidity_scenarios_failed | symbol=%s error=%s", symbol, exc)

    apply_cross_exchange_flat(result)

    lifecycle = stabilize_lifecycle(
        symbol,
        assess_hunt_lifecycle(
            price=price,
            hunt_high=hunt_h,
            hunt_low=hunt_l,
            session=session,
            tf=tf,
            market=market,
            symbol=symbol,
            state=symbol_state,
        ),
        state=symbol_state,
    )
    leg_gain_pct = (
        round((hunt_h - hunt_l) / hunt_l * 100.0, 1) if hunt_l > 0 else 0.0
    )
    lifecycle = attach_regime(
        lifecycle,
        prepared={"symbol": symbol, "price": price, "timeframes": tf, "session": session},
        market=market,
        symbol=symbol,
        state=symbol_state,
    )
    lifecycle_dict = lifecycle_to_dict(lifecycle, leg_gain_pct=leg_gain_pct)
    result["lifecycle"] = lifecycle_dict
    merge_hunt_extremes(
        symbol,
        price=price,
        rest_hunt_high=rest_h,
        rest_hunt_low=rest_l,
        lifecycle_phase=lifecycle.phase.value,
        market=market,
    )

    # Both sides are always analyzed; watch_mode gates Telegram only (VELVET dump_active
    # was invisible because pinned mode=long skipped _dump_analysis entirely).
    pos_in_range = float(session.get("pos_in_range") or 0.5)
    support_level = effective_support_break(
        impulse_high=hunt_h,
        lifecycle=lifecycle,
        pos_in_range=pos_in_range,
    )
    range_pct_24h = float(session.get("range_pct_24h") or 0)
    dump = _dump_analysis(
        symbol=symbol,
        price=price,
        tf=tf,
        market=market,
        regime=regime,
        impulse_high=hunt_h,
        impulse_low=hunt_l,
        support_break_level=support_level,
        fib=fib_hunt,
        prev_oi=prev_oi,
        cur_oi=prepared.oi_current,
        local_support=lifecycle.local_support,
        local_resistance=lifecycle.local_resistance,
        lifecycle_phase=lifecycle.phase.value,
        fall_from_high_pct=lifecycle.fall_from_high_pct,
        pos_in_range=pos_in_range,
        range_pct_24h=range_pct_24h,
        leg_gain_pct=leg_gain_pct,
        pump_stats=pump_stats,
        book_walls=result.get("book_walls"),
        cross_microstructure=result.get("cross_microstructure"),
    )
    dump = enrich_dump_setup(dump, price=price, tf=tf, market=market)
    attach_research_setup_fields(dump, tf=tf, regime=regime)
    dump["lifecycle_phase"] = lifecycle.phase.value
    dump["lifecycle_4h"] = lifecycle.phase_4h.value
    dump["lifecycle"] = lifecycle_dict
    dump["fall_from_high_pct"] = lifecycle.fall_from_high_pct
    dump["young_listing"] = young_listing
    dump["bars_1h"] = bars_1h
    confirmed, confirm_hard = _confirm_dump(
        dump,
        tf,
        symbol=symbol,
        price=price,
        market=market,
        lifecycle_bias=str(lifecycle.recommended_bias or ""),
    )
    confirmed, confirm_hard, lifecycle_note = apply_short_invalidation(
        confirmed,
        confirm_hard,
        lifecycle,
        dump=dump,
    )
    dump["confirm_hard"] = confirm_hard
    dump["phase"] = _phase(dump, confirmed, symbol=symbol, lifecycle_note=lifecycle_note)
    dump["confirmed"] = confirmed
    dump["monitor_ok"] = lifecycle.short_confirm_ok
    dump["lifecycle"] = lifecycle_dict
    if lifecycle_note:
        dump["lifecycle_note"] = lifecycle_note
    if lifecycle.invalidate_short:
        from hunt_core.regime.leg_fsm import apply_invalidate_short_fuel_cap

        had_high_fuel = float(dump.get("dump_fuel") or 0) > 32.0
        apply_invalidate_short_fuel_cap(dump)
        if had_high_fuel:
            dump["phase"] = _phase(
                dump, confirmed=False, symbol=symbol, lifecycle_note="lifecycle_invalidate_short"
            )
    result["dump"] = dump

    chg24 = float(result.get("chg_24h_pct") or 0)
    long_setup = _long_analysis(
        symbol=symbol,
        price=price,
        tf=tf,
        market=market,
        regime=regime,
        impulse_low=hunt_l,
        impulse_high=hunt_h,
        fib=fib_hunt,
        prev_oi=prev_oi,
        cur_oi=prepared.oi_current,
        lifecycle_phase=lifecycle.phase.value,
        fall_from_high_pct=lifecycle.fall_from_high_pct,
        pos_in_range=pos_in_range,
        range_pct_24h=range_pct_24h,
        leg_gain_pct=leg_gain_pct,
        pump_stats=pump_stats,
        chg_24h_pct=chg24,
        book_walls=result.get("book_walls"),
        cross_microstructure=result.get("cross_microstructure"),
    )
    long_setup = enrich_long_setup(long_setup, price=price, tf=tf, market=market)
    attach_research_setup_fields(long_setup, tf=tf, regime=regime)
    long_setup["young_listing"] = young_listing
    long_setup["bars_1h"] = bars_1h
    long_setup["lifecycle_4h"] = lifecycle.phase_4h.value
    long_setup["lifecycle"] = lifecycle_dict
    long_confirmed, long_hard = _confirm_long(
        long_setup,
        tf,
        symbol=symbol,
        price=price,
        market=market,
        lifecycle_bias=str(lifecycle.recommended_bias or ""),
        lifecycle_phase=lifecycle.phase.value,
    )
    long_setup["confirm_hard"] = long_hard
    long_setup["lifecycle_phase"] = lifecycle.phase.value
    long_setup["phase"] = _phase_long(long_setup, long_confirmed, symbol=symbol)
    long_setup["confirmed"] = long_confirmed
    result["long"] = long_setup

    from hunt_core.features.factors import build_factor_panel

    result["factor_panel"] = build_factor_panel(result)

    return result


