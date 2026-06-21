"""Per-tick watch loop — snapshot, delivery, follow-ups (Phase 8 split)."""
from __future__ import annotations

import asyncio
import html
import time
from typing import Any

from hunt_core import clock
from hunt_core.data.collect import (
    SnapshotTier,
    TickBatchCache,
    refresh_tick_batch_cache,
    safe_fetch,
    sort_symbols_for_tick,
)
from hunt_core.data.lake import (
    buffer_tracker_state,
    flush_lake,
)
from hunt_core.data.universe import clear_signal_notify, load_pending_notify
from hunt_core.deliver.digest import get_advisory_digest
from hunt_core.deliver.dispatch import (
    evaluate_forming_gate,
    mark_unified_sent,
    unified_cooldown_ok,
)
from hunt_core.deliver.telegram import TelegramBroadcaster
from hunt_core.errors import defensive_exc_types
from hunt_core.features.prepare import _prepare_frame
from hunt_core.features.feature_engine import FeatureExtractError, build_feature_vector
from hunt_core.features.prepare_columns import book_walls_from_row, feature_vector_from_row
from hunt_core.market import HuntCcxtClient, HuntCcxtSpotCompanion, HuntCcxtStreams
from hunt_core.market.cross import attach_cross_fields, merge_ws_cross_into_snapshot
def promote_initial_pump_lifecycle(*_a, **_k) -> None:
    # Legacy pump-ignition lifecycle promotion; the fusion phase handles this now.
    return None


def record_delivery_fsm(*_a, **_k) -> None:
    # Legacy delivery FSM telemetry; removed with the lifecycle FSM.
    return None
from hunt_core.runtime.cycle._cycle_confirm import (
    _advisory_tg_enabled,
    _confirm_blocked_bias_wait,
    _confirm_delivery_suppressed,
    _maybe_emit_scanner_continuation_wait,
    _should_emit_blocked_telemetry,
    hunt_auto_confirm_blocked,
)
from hunt_core.runtime.cycle._cycle_advisory import (
    _cooldown_ok,
    _entry_past_tp1,
    _maybe_send_early_alert,
    _maybe_send_liq_burst_advisory,
)
from hunt_core.runtime.cycle._cycle_reconcile import (
    _deliver_followup,
    _reconcile_inwatch_active,
    _reconcile_orphan_signals,
    _record_followup_side_effects,
)
from hunt_core.runtime.state import LOG, SNIPER_CONFIG, WatchMode, SymbolStateStore
from hunt_core.data.universe import PINNED_SYMBOLS, effective_watch_mode
def dump_hunt_skip_reason(*_a, **_k) -> str:
    return "dump_hunt_disabled"  # legacy dump-hunt advisory removed


def format_dump_hunt_telegram(*_a, **_k) -> str:
    return ""


async def maybe_send_dump_hunt_telegram(*_a, **_k) -> bool:
    return False


def early_telegram_enabled(*_a, **_k) -> bool:
    return False  # legacy early/ignition advisory removed


from hunt_core.detect.routing import resolve_delivery_mode, route_tick
from hunt_core.track.events import append_signal_event, record_funnel_stage, record_lifecycle_funnel
from hunt_core.track.candidates import (
    load_setup_candidates_state,
    process_setup_candidate,
    promote_to_confirm,
    save_setup_candidates_state,
)
from hunt_core.track.pump_history import (
    record_signal_open as record_pump_signal_open,
)
from hunt_core.track.tracker import (
    evaluate_followups,
    global_confirm_burst_cap_reached,
    iter_active_tracker_symbols,
    latch_row_setups,
    load_tracker_state,
    reconcile_active_from_ticker,
    register_signal_open,
    symbol_daily_tg_cap_reached,
    symbol_loss_streak_cooldown,
    symbol_repeat_loser_blocked,
)
from hunt_core.runtime.tick_assembly import hot_tick_symbol, snapshot_symbol
from hunt_core.data.lake import FeatureLakeWriter


def _record_outcome_ledger(
    *,
    symbol: str,
    direction: str,
    row: dict[str, Any],
    setup: dict[str, Any] | None = None,
    delivered: bool = False,
    blockers: list[str] | None = None,
    event: str = "blocked",
) -> None:
    from hunt_core.track.outcome_ledger import append_ledger_event, build_ledger_record

    try:
        record = build_ledger_record(
            symbol=symbol,
            direction=direction,
            event=event,
            row=row,
            setup=setup,
            blockers=blockers,
            delivered=delivered,
        )
        append_ledger_event(record)
    except Exception as exc:
        LOG.warning(
            "outcome_ledger_failed | symbol=%s direction=%s event=%s error=%s",
            symbol,
            direction,
            event,
            exc,
        )


async def run_tick(
    symbols: tuple[str, ...],
    *,
    settings: Any,
    minimums: dict[str, int],
    client: HuntCcxtClient,
    prev_oi: dict[str, float | None],
    last_bias: dict[str, str],
    last_lifecycle_phase: dict[str, str],
    mode_map: dict[str, WatchMode],
    broadcaster: TelegramBroadcaster | None,
    send_telegram: bool,
    ticker_by_sym: dict[str, dict[str, Any]] | None = None,
    ignition_by_sym: dict[str, dict[str, Any]] | None = None,
    pump_stats_by_sym: dict[str, dict[str, Any]] | None = None,
    pump_store: Any | None = None,
    ws_feed: HuntCcxtStreams | None = None,
    spot_companion: HuntCcxtSpotCompanion | None = None,
    batch_cache: TickBatchCache | None = None,
    tier: SnapshotTier = "full",
    cross_ex_cache: dict[str, dict[str, Any]] | None = None,
    prescan_outlier_by_sym: dict[str, dict[str, Any]] | None = None,
    symbol_state: SymbolStateStore | None = None,
    feature_lake: FeatureLakeWriter | None = None,
    tier_by_symbol: dict[str, SnapshotTier] | None = None,
    snapshot_parallel: int | None = None,
    hot_path: bool = False,
) -> list[dict[str, Any]]:
    from hunt_core.runtime.cycle import _impl as _tick_impl

    _load_state = _tick_impl._load_state
    _save_state = _tick_impl._save_state
    _phase_long = _tick_impl._phase_long
    _evaluate_delivery_row = _tick_impl._evaluate_delivery_row
    _overlay_ws_tickers = _tick_impl._overlay_ws_tickers
    _refresh_live_price = _tick_impl._refresh_live_price
    HUNT_SNAPSHOT_PARALLEL = _tick_impl.HUNT_SNAPSHOT_PARALLEL
    _HOT_TICK_TIMEOUT_S = _tick_impl._HOT_TICK_TIMEOUT_S
    HUNT_SNIPER_MODE = _tick_impl.HUNT_SNIPER_MODE
    HUNT_SNIPER_LIVE_PHASES = _tick_impl.HUNT_SNIPER_LIVE_PHASES
    HUNT_SNIPER_TOP_LS_MAX = _tick_impl.HUNT_SNIPER_TOP_LS_MAX
    HUNT_SNIPER_CHASE_TOL = _tick_impl.HUNT_SNIPER_CHASE_TOL
    SYMBOL_TICK_TIMEOUT_S = _tick_impl.SYMBOL_TICK_TIMEOUT_S
    from hunt_core.domain.config import SQUEEZE_COOLDOWN_MINUTES, SQUEEZE_MIN_VOL_24H_M

    state = _load_state()
    tracker_state = load_tracker_state()
    setup_candidates_state = load_setup_candidates_state()
    now = clock.now_utc()
    rows: list[dict[str, Any]] = []
    notify_pending = {str(p.get("symbol")): p for p in load_pending_notify()}

    def _tier_for(sym: str) -> SnapshotTier:
        if tier_by_symbol and sym in tier_by_symbol:
            return tier_by_symbol[sym]
        return tier

    batch_tier: SnapshotTier = (
        "full" if any(_tier_for(s) == "full" for s in symbols) else tier
    )
    parallel = max(1, int(snapshot_parallel or HUNT_SNAPSHOT_PARALLEL))
    try:
        cache = batch_cache or TickBatchCache()
        need_btc = any(s != "BTCUSDT" for s in symbols)
        await refresh_tick_batch_cache(
            cache,
            client,
            safe_fetch=safe_fetch,
            prepare_frame=_prepare_frame,
            need_btc=need_btc,
            tier=batch_tier,
        )
        premium_all = cache.premium_all
        funding_info_all = cache.funding_info_all
        exchange_by_sym = cache.exchange_by_sym
        btc_work_1h = cache.btc_work_1h
        btc_work_1m = cache.btc_work_1m
        if ticker_by_sym is None:
            ticker_raw = await safe_fetch(
                client.fetch_ticker_24h,
                context="ticker_24h",
                client=client,
            ) or []
            ticker_by_sym = {str(t.get("symbol")): t for t in ticker_raw if t.get("symbol")}
        if batch_tier == "full" and spot_companion is not None and symbols:
            full_syms = [s for s in symbols if _tier_for(s) == "full"]
            futures_mids = {
                s: float((ticker_by_sym.get(s) or {}).get("last_price") or 0) or None
                for s in full_syms
            }
            try:
                spot_n = await spot_companion.refresh_symbols(
                    full_syms, futures_mid_by_symbol=futures_mids
                )
                LOG.debug("spot_companion_refresh", symbols=len(full_syms), updated=spot_n)
            except defensive_exc_types(asyncio.IncompleteReadError, OSError, ConnectionError) as exc:
                LOG.warning("spot_companion_refresh_failed", error=repr(exc))

        ordered = sort_symbols_for_tick(
            symbols,
            ignition_by_sym=ignition_by_sym,
            last_bias=last_bias,
        )
        if tier == "fast":
            LOG.debug("watch_tick_fast_tier", symbols=len(ordered), head=list(ordered[:4]))

        _overlay_ws_tickers(ticker_by_sym, ordered, ws_feed)
        tick_started = time.monotonic()

        async def _snapshot_one(sym: str) -> tuple[str, dict[str, Any]]:
            sym_tier = _tier_for(sym)
            mode = effective_watch_mode(
                sym,
                mode_map,
                lifecycle_bias=last_bias.get(sym),
            )
            try:
                snap_timeout = _HOT_TICK_TIMEOUT_S if hot_path else SYMBOL_TICK_TIMEOUT_S
                if hot_path:
                    row = await asyncio.wait_for(
                        hot_tick_symbol(
                            client,
                            settings,
                            minimums,
                            sym,
                            watch_mode=mode,
                            prev_oi=prev_oi.get(sym),
                            premium_all=premium_all,
                            funding_info_all=funding_info_all,
                            btc_work_1h=btc_work_1h,
                            btc_work_1m=btc_work_1m,
                            exchange_by_sym=exchange_by_sym,
                            ticker_by_sym=ticker_by_sym,
                            ws_feed=ws_feed,
                            spot_companion=spot_companion,
                            pump_stats=(
                                pump_stats_by_sym.get(sym) if pump_stats_by_sym else None
                            ),
                            symbol_state=symbol_state,
                        ),
                        timeout=snap_timeout,
                    )
                else:
                    row = await asyncio.wait_for(
                        snapshot_symbol(
                            client,
                            settings,
                            minimums,
                            sym,
                            watch_mode=mode,
                            prev_oi=prev_oi.get(sym),
                            premium_all=premium_all,
                            funding_info_all=funding_info_all,
                            btc_work_1h=btc_work_1h,
                            btc_work_1m=btc_work_1m,
                            exchange_by_sym=exchange_by_sym,
                            ticker_by_sym=ticker_by_sym,
                            ws_feed=ws_feed,
                            spot_companion=spot_companion,
                            pump_stats=(
                                pump_stats_by_sym.get(sym) if pump_stats_by_sym else None
                            ),
                            tier=sym_tier,
                            symbol_state=symbol_state,
                        ),
                        timeout=snap_timeout,
                    )
                return sym, row
            except TimeoutError:
                LOG.warning(
                    "watch_symbol_timeout",
                    symbol=sym,
                    timeout_s=snap_timeout,
                    hot_path=hot_path,
                )
                return sym, {
                    "ts": now.isoformat(),
                    "symbol": sym,
                    "error": "symbol_tick_timeout",
                    "tick_path": "hot_error" if hot_path else "rest_error",
                    "snapshot_tier": tier,
                    "hot_tick_no_rest": hot_path,
                }
            except defensive_exc_types(asyncio.IncompleteReadError) as exc:
                LOG.warning("dump_symbol_failed", symbol=sym, error=repr(exc))
                return sym, {
                    "ts": now.isoformat(),
                    "symbol": sym,
                    "error": repr(exc),
                    "tick_path": "hot_error" if hot_path else "rest_error",
                    "snapshot_tier": tier,
                    "hot_tick_no_rest": hot_path,
                }

        sem = asyncio.Semaphore(parallel)

        async def _bounded_snapshot(sym: str) -> tuple[str, dict[str, Any]]:
            async with sem:
                return await _snapshot_one(sym)

        snap_pairs = await asyncio.gather(*[_bounded_snapshot(s) for s in ordered])
        row_by_sym = dict(snap_pairs)
        snap_elapsed = round(time.monotonic() - tick_started, 2)
        if len(ordered) > 1 or hot_path:
            full_n = sum(1 for s in ordered if _tier_for(s) == "full")
            LOG.info(
                "watch_snapshot_batch",
                symbols=len(ordered),
                parallel=parallel,
                elapsed_s=snap_elapsed,
                tier=tier,
                hot_path=hot_path,
                full_symbols=full_n,
                fast_symbols=len(ordered) - full_n,
                used_weight_1m=client.used_weight_1m(),  # Binance IP budget; cap 2400/min
            )

        advisory_sent_tick: set[str] = set()
        tg_confirm_sent_this_tick = False

        for symbol in ordered:
            try:
                row = row_by_sym.get(symbol)
                if row is None:
                    continue
                if row.get("error"):
                    LOG.info(
                        "watch_symbol_data_reject",
                        symbol=symbol,
                        error=row.get("error"),
                        no_signal_reason=row.get("no_signal_reason"),
                        violations=(row.get("data_violations") or [])[:4],
                    )
                    rows.append(row)
                    continue
                if feature_lake is not None and _tier_for(symbol) == "full":
                    if symbol in PINNED_SYMBOLS:
                        pass
                    else:
                        try:
                            vector = build_feature_vector(
                                row.get("_prepared"),
                                row,
                                symbol=symbol,
                                tf="15m",
                            )
                            feature_lake.enqueue(symbol, str(row.get("ts")), "15m", vector.to_dict())
                        except FeatureExtractError as exc:
                            LOG.warning(
                                "feature_lake_enqueue_skipped",
                                symbol=symbol,
                                error=str(exc),
                            )
                mode = effective_watch_mode(
                    symbol,
                    mode_map,
                    lifecycle_bias=last_bias.get(symbol),
                )
                row = latch_row_setups(tracker_state, row)
                oi_val = (row.get("market") or row.get("positioning") or {}).get("oi")
                if oi_val is not None:
                    prev_oi[symbol] = float(oi_val)
                if cross_ex_cache and symbol in cross_ex_cache:
                    cx = dict(cross_ex_cache[symbol])
                    if ws_feed is not None:
                        cx = merge_ws_cross_into_snapshot(
                            cx,
                            ws_feed.live_funding_cross(symbol),
                        )
                    attach_cross_fields(row, cx)
                if not row.get("error"):
                    row["plane"] = "hunt"
                    from hunt_core.runtime.tick_jsonl import (
                        ensure_fusion_lifecycle_fields,
                        resolve_row_mtf,
                    )

                    active = row.get("long") if (row.get("long") or {}).get("confirmed") else row.get("dump")
                    row["lifecycle"] = ensure_fusion_lifecycle_fields(
                        row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else None,
                        setup=active if isinstance(active, dict) else None,
                    )
                    mtf = resolve_row_mtf(row, symbol=symbol)
                    if mtf is not None:
                        row["mtf"] = mtf
                rows.append(row)
                if ignition_by_sym and symbol in ignition_by_sym:
                    row["ignited"] = True
                    row["ignition"] = ignition_by_sym[symbol]
                if prescan_outlier_by_sym and symbol in prescan_outlier_by_sym:
                    row["prescan_outlier"] = prescan_outlier_by_sym[symbol]
                promote_initial_pump_lifecycle(row, symbol=symbol)
                if pump_stats_by_sym and symbol in pump_stats_by_sym:
                    row["pump_history"] = pump_stats_by_sym[symbol]
                dump = row.get("dump") or {}
                long_setup = row.get("long") or {}
                from hunt_core.data.frame_cache import get_frame_cache
                from hunt_core.detect.setup_fields import setup_conviction_pct

                lifecycle_raw = row.get("lifecycle") or (dump.get("lifecycle") if dump else None)
                get_frame_cache().mark_priority(
                    symbol,
                    max(
                        setup_conviction_pct(dump if isinstance(dump, dict) else {}, direction="short"),
                        setup_conviction_pct(
                            long_setup if isinstance(long_setup, dict) else {}, direction="long"
                        ),
                    ),
                )
                if not hot_path:
                    prev_path = get_frame_cache().get_last_tick_path(symbol)
                    if prev_path in {
                        "hot_ws",
                        "hot_delta",
                        "hot_bootstrap",
                        "hot_carry",
                    } and dump.get(
                        "confirmed"
                    ):
                        from hunt_core.deliver.dispatch import shadow_full_lane_recheck

                        shadow_full_lane_recheck(
                            row,
                            direction="short",
                            setup=dump,
                            lifecycle=lifecycle_raw if isinstance(lifecycle_raw, dict) else None,
                            symbol=symbol,
                            broadcaster=broadcaster,
                            send_telegram=False,
                        )
                get_frame_cache().set_last_tick_path(
                    symbol, str(row.get("tick_path") or "")
                )
                if lifecycle_raw and isinstance(lifecycle_raw, dict):
                    last_bias[symbol] = str(lifecycle_raw.get("recommended_bias") or "")
                    lc_phase = str(lifecycle_raw.get("phase") or "")
                    prev_phase = last_lifecycle_phase.get(symbol)
                    if lc_phase and lc_phase != prev_phase:
                        record_lifecycle_funnel(
                            symbol=symbol,
                            phase=lc_phase,
                            prev_phase=prev_phase,
                            bias=last_bias[symbol],
                        )
                        last_lifecycle_phase[symbol] = lc_phase
                    mode = effective_watch_mode(
                        symbol,
                        mode_map,
                        lifecycle_bias=last_bias[symbol],
                    )
                    row["watch_mode"] = mode
                from hunt_core.detect.setup_fields import setup_conviction_pct

                def _tick_conviction(setup: dict[str, Any]) -> float:
                    if not setup:
                        return 0.0
                    raw = setup.get("fusion_score")
                    if raw is not None:
                        try:
                            return round(float(raw), 1)
                        except (TypeError, ValueError):
                            pass
                    return round(setup_conviction_pct(setup), 1)

                def _tick_gate(setup: dict[str, Any]) -> str:
                    if not setup:
                        return "idle"
                    if setup.get("confirmed"):
                        return "confirmed"
                    reason = str(setup.get("gate_reason") or "").strip()
                    return reason or str(setup.get("phase") or "forming")

                LOG.info(
                    "watch_tick",
                    symbol=symbol,
                    mode=mode,
                    price=row.get("price"),
                    hunt_phase=(lifecycle_raw or {}).get("phase"),
                    short_score=_tick_conviction(dump),
                    short_phase=dump.get("phase") or "—",
                    short_confirmed=bool(dump.get("confirmed")),
                    short_gate=_tick_gate(dump),
                    long_score=_tick_conviction(long_setup),
                    long_phase=long_setup.get("phase") or "—",
                    long_confirmed=bool(long_setup.get("confirmed")),
                    long_gate=_tick_gate(long_setup),
                    data_missing=(row.get("data_quality") or {}).get("fields_missing") or [],
                )
                skip_short = dump.get("hunt_skipped") or (
                    dump and not dump.get("confirmed") and dump.get("gate_reason")
                )
                skip_long = long_setup.get("hunt_skipped") or (
                    long_setup and not long_setup.get("confirmed") and long_setup.get("gate_reason")
                )
                if skip_short or skip_long:
                    LOG.info(
                        "watch_hunt_skipped",
                        symbol=symbol,
                        phase=(lifecycle_raw or {}).get("phase"),
                        short_skipped=skip_short or "",
                        long_skipped=skip_long or "",
                    )
                tick_routes = route_tick(row)
                row["setup_routes"] = [
                    {
                        "path": c.path,
                        "direction": c.direction,
                        "delivery_mode": resolve_delivery_mode(c.lifecycle, c.setup),
                    }
                    for c in tick_routes
                ]
                sq = row.get("squeeze")
                ignited = bool(row.get("ignited"))
                for cand in tick_routes:
                    if cand.path == "early_advisory" and cand.setup.get("advisory"):
                        continue
                    if cand.path == "early_armed":
                        base = dump if cand.direction == "short" else long_setup
                        if isinstance(base, dict):
                            for k, v in cand.setup.items():
                                if v is not None:
                                    base[k] = v
                        process_setup_candidate(
                            setup_candidates_state,
                            symbol=symbol,
                            direction=cand.direction,
                            setup=cand.setup,
                            row=row,
                            lifecycle=lifecycle_raw,
                            now=now,
                        )
                        continue
                    cand_dir = cand.direction
                    cand_setup = cand.setup
                    if not cand_setup:
                        continue
                    process_setup_candidate(
                        setup_candidates_state,
                        symbol=symbol,
                        direction=cand_dir,
                        setup=cand_setup,
                        row=row,
                        lifecycle=lifecycle_raw,
                        now=now,
                        squeeze=sq if cand_dir == "short" or sq else None,
                        ignition=ignited,
                        forming=not bool(cand_setup.get("confirmed")),
                    )
                    record_delivery_fsm(
                        symbol,
                        cand_dir,  # type: ignore[arg-type]
                        cand_setup,
                        tracker_active=bool(
                            (tracker_state.get("signals") or {}).get(f"{symbol}:{cand_dir}")
                        ),
                        state=symbol_state,
                    )
                kline_events = await _reconcile_inwatch_active(
                    client, tracker_state, symbol=symbol, now=now
                )
                followup_sent_keys: set[str] = set()
                for fu in kline_events:
                    LOG.info(
                        "watch_followup_kline",
                        symbol=fu.symbol,
                        followup_event=fu.event,
                        detail=fu.detail,
                    )
                    if await _deliver_followup(
                        broadcaster,
                        fu,
                        row,
                        tracker_state,
                        now=now,
                        send_telegram=send_telegram,
                    ):
                        followup_sent_keys.add(fu.message_key)
                followups = evaluate_followups(tracker_state, row, now=now)
                for fu in followups:
                    if fu.message_key in followup_sent_keys:
                        continue
                    LOG.info(
                        "watch_followup",
                        symbol=fu.symbol,
                        followup_event=fu.event,
                        detail=fu.detail,
                    )
                    if await _deliver_followup(
                        broadcaster,
                        fu,
                        row,
                        tracker_state,
                        now=now,
                        send_telegram=send_telegram,
                    ):
                        followup_sent_keys.add(fu.message_key)
                if followup_sent_keys:
                    _record_followup_side_effects(
                        [*kline_events, *followups],
                        sent_keys=followup_sent_keys,
                        now=now,
                        pump_store=pump_store,
                    )
                squeeze = row.get("squeeze")
                if squeeze and float(row.get("vol_24h_m") or 0) >= SQUEEZE_MIN_VOL_24H_M:
                    LOG.info(
                        "hunt_squeeze_charged",
                        symbol=symbol,
                        bb_width_pctile=squeeze.get("bb_width_pctile_1h"),
                        donchian_pct=squeeze.get("donchian_width_pct_1h"),
                        oi_z=squeeze.get("oi_z"),
                        gls_z=squeeze.get("gls_z"),
                    )
                    sq_dir = "short"
                    try:
                        from hunt_core.deliver.telegram import squeeze_trade_direction

                        sq_dir = squeeze_trade_direction(row)
                    except Exception:
                        pass
                    if (
                        _advisory_tg_enabled()
                        and send_telegram
                        and broadcaster is not None
                        and unified_cooldown_ok(
                            state,
                            symbol=symbol,
                            direction=sq_dir,
                            stage="squeeze",
                            now=now,
                        )
                        and _cooldown_ok(
                            symbol,
                            "squeeze",
                            state,
                            now=now,
                            minutes=SQUEEZE_COOLDOWN_MINUTES,
                        )
                    ):
                        from hunt_core.deliver.templates import format_squeeze_telegram

                        result = await broadcaster.send_html(format_squeeze_telegram(row))
                        if result.status == "sent":
                            state[f"{symbol}:squeeze"] = now.isoformat()
                            mark_unified_sent(
                                state,
                                symbol=symbol,
                                direction=sq_dir,
                                stage="squeeze",
                                now=now,
                            )
                            advisory_sent_tick.add(f"{symbol}:{sq_dir}")
                            LOG.info(
                                "hunt_squeeze_telegram_sent",
                                symbol=symbol,
                                message_id=result.message_id,
                            )

                if ws_feed is not None and _tier_for(symbol) == "full":
                    await _maybe_send_liq_burst_advisory(
                        broadcaster,
                        symbol=symbol,
                        ws_feed=ws_feed,
                        state=state,
                        now=now,
                        send_telegram=send_telegram,
                    )

                pend = notify_pending.get(symbol)
                if (
                    pend
                    and send_telegram
                    and broadcaster is not None
                    and not row.get("error")
                ):
                    ndir = str(pend.get("direction") or "short")
                    nsetup = dump if ndir == "short" else long_setup
                    lc_dict = lifecycle_raw if isinstance(lifecycle_raw, dict) else {}
                    from hunt_core.detect.setup_fields import setup_conviction_pct, setup_meets_strength

                    nconviction = setup_conviction_pct(nsetup or {}, direction=ndir)
                    nphase = str((nsetup or {}).get("phase") or "")
                    await_phase = str(pend.get("await_phase") or "dump_confirmed")
                    min_conviction = float(pend.get("min_fuel") or 70.0)
                    notify_on_forming = bool(pend.get("notify_on_forming"))
                    forming_phases = frozenset(
                        {
                            "dump_setup_forming",
                            "dump_imminent",
                            "dump_initiating",
                            "exhaustion_watch",
                        }
                    )
                    forming_ready = (
                        notify_on_forming
                        and nsetup
                        and not bool(nsetup.get("confirmed"))
                        and nconviction >= min_conviction
                        and nphase in forming_phases
                        and str(lc_dict.get("phase") or "")
                        in ("exhaustion_at_high", "distribution", "dump_initiating")
                    )
                    phase_ready = (
                        nsetup
                        and not bool(nsetup.get("confirmed"))
                        and nphase == await_phase
                        and nconviction >= min_conviction
                    )
                    if forming_ready or phase_ready:
                        record_funnel_stage(
                            "fuel",
                            symbol=symbol,
                            direction=ndir,
                            detail=nphase,
                            payload={
                                "conviction": round(nconviction, 1),
                                "min_conviction": min_conviction,
                            },
                        )
                    if nsetup and bool(nsetup.get("confirmed")):
                        if hunt_auto_confirm_blocked(symbol):
                            append_signal_event(
                                "blocked",
                                symbol=symbol,
                                direction=ndir,
                                detail="pinned_monitor_only",
                                payload={"block_code": "pinned_monitor_only"},
                            )
                            clear_signal_notify(symbol)
                            continue
                        gate, delivery_tier = _evaluate_delivery_row(
                            row,
                            hot_path=hot_path,
                            direction=ndir,
                            setup=nsetup,
                            lifecycle=lc_dict,
                            symbol=symbol,
                            refresh_live_price=True,
                            ws_feed=ws_feed,
                        )
                        if not gate.ok or delivery_tier is None:
                            LOG.info(
                                "signal_notify_blocked",
                                symbol=symbol,
                                direction=ndir,
                                reason=gate.code if not gate.ok else "stale_tier",
                            )
                            clear_signal_notify(symbol)
                        else:
                            record_funnel_stage(
                                "tier",
                                symbol=symbol,
                                direction=ndir,
                                detail=str(delivery_tier),
                                payload={"gate": gate.code},
                            )
                            if str(delivery_tier).upper() == "ARMED":
                                record_funnel_stage(
                                    "armed",
                                    symbol=symbol,
                                    direction=ndir,
                                    detail=str(delivery_tier),
                                    payload={
                                        "gate": gate.code,
                                        "phase": str(lc_dict.get("phase") or ""),
                                        "fuel": nfuel,
                                    },
                                )
                            if not unified_cooldown_ok(
                                state,
                                symbol=symbol,
                                direction=ndir,
                                stage="confirm",
                                now=now,
                            ):
                                LOG.info(
                                    "signal_notify_blocked",
                                    symbol=symbol,
                                    direction=ndir,
                                    reason="unified_cooldown",
                                )
                                clear_signal_notify(symbol)
                                continue
                            sym_label = html.escape(symbol.replace("USDT", "-USDT"))
                            from hunt_core.deliver.templates import format_telegram_confirm

                            body = format_telegram_confirm(
                                row,
                                direction=ndir,
                                confirm_reasons=list(nsetup.get("confirm_hard") or []),
                                delivery_tier=delivery_tier,
                            )
                            notify_msg = f"🔔 <b>/signal confirm</b> {sym_label}\n{body}"
                            notify_result = await broadcaster.send_html(notify_msg)
                            if notify_result.status == "sent":
                                clear_signal_notify(symbol)
                                mark_unified_sent(
                                    state,
                                    symbol=symbol,
                                    direction=ndir,
                                    stage="confirm",
                                    now=now,
                                )
                                LOG.info(
                                    "signal_notify_sent",
                                    symbol=symbol,
                                    direction=ndir,
                                    message_id=notify_result.message_id,
                                )
                    elif (
                        _advisory_tg_enabled()
                        and (forming_ready or phase_ready)
                        and ndir == "short"
                        and early_telegram_enabled(symbol)
                    ):
                        price_now = _refresh_live_price(
                            row, ws_feed=ws_feed, symbol=symbol
                        )
                        if _entry_past_tp1(nsetup, direction=ndir, price=price_now):
                            LOG.info(
                                "signal_notify_skipped_past_tp1",
                                symbol=symbol,
                                direction=ndir,
                                price=price_now,
                            )
                        else:
                            tier: str = (
                                "likely" if bool(nsetup.get("confirmed")) else
                                "armed" if setup_meets_strength(
                                    nsetup or {}, direction=ndir, symbol=symbol, tier="confirm"
                                ) else
                                "prep"
                            )
                            skip = dump_hunt_skip_reason(
                                symbol=symbol,
                                tier=tier,  # type: ignore[arg-type]
                                price=price_now,
                                setup=nsetup,
                                lifecycle=lc_dict,
                                now=now,
                                price_stale=bool(row.get("price_stale")),
                            )
                            if skip:
                                LOG.debug(
                                    "signal_notify_skipped",
                                    symbol=symbol,
                                    reason=skip,
                                    tier=tier,
                                )
                            elif f"{symbol}:{ndir}" in advisory_sent_tick:
                                LOG.debug(
                                    "signal_notify_skipped",
                                    symbol=symbol,
                                    reason="advisory_sent_same_tick",
                                    tier=tier,
                                )
                            elif not unified_cooldown_ok(
                                state,
                                symbol=symbol,
                                direction=ndir,
                                stage="dump_hunt",
                                now=now,
                            ):
                                LOG.debug(
                                    "signal_notify_skipped",
                                    symbol=symbol,
                                    reason="unified_cooldown",
                                    tier=tier,
                                )
                            else:
                                imp = row.get("impulse") or {}
                                notify_msg = format_dump_hunt_telegram(
                                    symbol=symbol,
                                    tier=tier,  # type: ignore[arg-type]
                                    price=price_now,
                                    setup=nsetup,
                                    lifecycle=lc_dict,
                                    chg_24h=float(row.get("chg_24h_pct") or 0),
                                    impulse_low=float(
                                        row.get("impulse_low")
                                        or imp.get("hunt_low")
                                        or 0
                                    ),
                                    atr15=float(
                                        ((row.get("timeframes") or {}).get("15m") or {}).get("atr14")
                                        or 0
                                    ),
                                    note=f"forming · {nphase} · fuel {nfuel:.0f}",
                                )
                                sent = await maybe_send_dump_hunt_telegram(
                                    broadcaster,
                                    symbol=symbol,
                                    tier=tier,  # type: ignore[arg-type]
                                    message=notify_msg,
                                    now=now,
                                    price=price_now,
                                    setup=nsetup,
                                    lifecycle=lc_dict,
                                )
                                if sent and advisory_digest_enabled():
                                    get_advisory_digest().enqueue(
                                        symbol=symbol,
                                        direction=ndir,
                                        tier=tier,
                                        score=nfuel,
                                        change_24h_pct=float(row.get("chg_24h_pct") or 0),
                                        phase=nphase,
                                        note=f"forming · fuel {nfuel:.0f}",
                                    )
                                if sent:
                                    mark_unified_sent(
                                        state,
                                        symbol=symbol,
                                        direction=ndir,
                                        stage="dump_hunt",
                                        now=now,
                                    )
                                    advisory_sent_tick.add(f"{symbol}:{ndir}")
                                    LOG.info(
                                        "signal_notify_forming_sent",
                                        symbol=symbol,
                                        direction=ndir,
                                        tier=tier,
                                        phase=nphase,
                                        fuel=nfuel,
                                    )
                                    append_signal_event(
                                        "forming_notify",
                                        symbol=symbol,
                                        direction=ndir,
                                        detail=nphase,
                                        payload={"fuel": nfuel, "tier": tier},
                                    )

                if send_telegram and broadcaster is not None and not row.get("error"):
                    routed_dirs = {
                        c.direction
                        for c in tick_routes
                        if c.path != "early_advisory" and isinstance(c.setup, dict) and c.setup
                    }
                    for direction, setup in (("short", dump), ("long", long_setup)):
                        if routed_dirs and direction not in routed_dirs:
                            continue
                        if not setup:
                            continue
                        _maybe_emit_scanner_continuation_wait(
                            symbol=symbol,
                            direction=direction,
                            setup=setup,
                            lifecycle_raw=lifecycle_raw,
                            now=now,
                        )
                        from hunt_core.detect.setup_fields import ev_primary_delivery_qualified

                        _ev_primary_live = ev_primary_delivery_qualified(
                            setup,
                            direction=direction,
                            symbol=symbol,
                        )
                        from hunt_core.detect.delivery_support import mission_delivery_block

                        _lc = lifecycle_raw if isinstance(lifecycle_raw, dict) else {}
                        _lc_phase = str(_lc.get("phase") or "")
                        _mission = mission_delivery_block(
                            direction=direction,
                            lifecycle=_lc,
                            setup=setup,
                            symbol=symbol,
                        )
                        if _mission is not None:
                            append_signal_event(
                                "blocked",
                                symbol=symbol,
                                direction=direction,
                                detail=_mission.code,
                                payload={
                                    "block_code": _mission.code,
                                    "lifecycle_phase": _lc_phase,
                                    "phase": setup.get("phase"),
                                },
                            )
                            continue
                        if hunt_auto_confirm_blocked(symbol):
                            append_signal_event(
                                "blocked",
                                symbol=symbol,
                                direction=direction,
                                detail="pinned_monitor_only",
                                payload={"block_code": "pinned_monitor_only"},
                            )
                            continue
                        if row.get("price_stale"):
                            append_signal_event(
                                "blocked",
                                symbol=symbol,
                                direction=direction,
                                detail="price_stale",
                                payload={
                                    "block_code": "price_stale",
                                    "lifecycle_phase": _lc_phase,
                                    "price_source": row.get("price_source"),
                                },
                            )
                            continue
                        if HUNT_SNIPER_MODE and not _ev_primary_live:
                            _live_phases = (
                                HUNT_SNIPER_LIVE_PHASES
                                if direction == "short"
                                else SNIPER_CONFIG.live_phases_long
                            )
                            if _lc_phase not in _live_phases:
                                append_signal_event(
                                    "blocked",
                                    symbol=symbol,
                                    direction=direction,
                                    detail=f"sniper_phase:{_lc_phase}",
                                    payload={
                                        "block_code": "sniper_phase_block",
                                        "lifecycle_phase": _lc_phase,
                                        "phase": setup.get("phase"),
                                    },
                                )
                                continue
                            if direction == "short" and _lc.get("short_entry_ok") is not True:
                                LOG.warning(
                                    "sniper_block_short_entry_not_ok",
                                    symbol=symbol,
                                    phase=_lc_phase,
                                    bias=_lc.get("recommended_bias"),
                                )
                                append_signal_event(
                                    "blocked",
                                    symbol=symbol,
                                    direction=direction,
                                    detail="sniper_short_entry_not_ok",
                                    payload={
                                        "block_code": "sniper_short_entry_not_ok",
                                        "lifecycle_phase": _lc_phase,
                                        "bias": _lc.get("recommended_bias"),
                                    },
                                )
                                continue
                            if direction == "long" and _lc.get("long_entry_ok") is False:
                                append_signal_event(
                                    "blocked",
                                    symbol=symbol,
                                    direction=direction,
                                    detail="sniper_long_entry_not_ok",
                                    payload={
                                        "block_code": "sniper_long_entry_not_ok",
                                        "lifecycle_phase": _lc_phase,
                                    },
                                )
                                continue
                            _px = float(row["price"])
                            from hunt_core.levels.levels import reanchor_setup_levels

                            reanchor_setup_levels(
                                setup,
                                row,
                                direction=direction,
                                live_price=_px,
                                symbol=symbol,
                            )
                            _ez = setup.get("entry_zone")
                            try:
                                _zone_lo = float(_ez[0])
                            except (TypeError, ValueError, IndexError, KeyError):
                                LOG.error(
                                    "sniper_block_bad_entry_geometry", symbol=symbol,
                                    entry_zone=_ez, price=row.get("price"),
                                )
                                continue
                            if direction == "short" and _px < _zone_lo * (1.0 - HUNT_SNIPER_CHASE_TOL):
                                LOG.warning(
                                    "sniper_block_late_chase", symbol=symbol, price=_px,
                                    entry_zone_lo=_zone_lo,
                                    ext_pct=round((_zone_lo - _px) / _zone_lo * 100.0, 2),
                                )
                                continue
                            if direction == "short":
                                _top_ls_f = effective_top_ls(row.get("market"))
                                if _top_ls_f is not None and _top_ls_f >= HUNT_SNIPER_TOP_LS_MAX:
                                    LOG.info(
                                        "sniper_block_top_ls_squeeze",
                                        symbol=symbol,
                                        top_ls=_top_ls_f,
                                        max=HUNT_SNIPER_TOP_LS_MAX,
                                    )
                                    continue
                        from hunt_core.detect.probe import prepare_anticipation_delivery

                        prepare_anticipation_delivery(
                            row,
                            setup,
                            direction=direction,
                            ws_feed=ws_feed,
                        )
                        from hunt_core.runtime.cycle._delivery import is_armed_setup, is_confirmed_setup

                        confirmed_setup = is_confirmed_setup(setup)
                        armed_setup = is_armed_setup(setup) and not confirmed_setup
                        confirm_gate = None
                        confirm_tier: str | None = None
                        if (
                            send_telegram
                            and broadcaster is not None
                            and armed_setup
                        ):
                            if hunt_auto_confirm_blocked(symbol):
                                append_signal_event(
                                    "blocked",
                                    symbol=symbol,
                                    direction=direction,
                                    detail="pinned_monitor_only",
                                    payload={"block_code": "pinned_monitor_only"},
                                )
                                continue
                            armed_gate, armed_tier = _evaluate_delivery_row(
                                row,
                                hot_path=hot_path,
                                direction=direction,
                                setup=setup,
                                lifecycle=lifecycle_raw
                                if isinstance(lifecycle_raw, dict)
                                else None,
                                symbol=symbol,
                                refresh_live_price=False,
                                ws_feed=ws_feed,
                            )
                            if (
                                armed_gate.ok
                                and armed_tier == "armed"
                                and unified_cooldown_ok(
                                    state,
                                    symbol=symbol,
                                    direction=direction,
                                    stage="armed",
                                    now=now,
                                )
                                and _cooldown_ok(symbol, direction, state, now=now)
                            ):
                                from hunt_core.deliver.templates import format_telegram_confirm

                                msg = format_telegram_confirm(
                                    row,
                                    direction=direction,
                                    confirm_reasons=setup.get("confirm_hard") or [],
                                    delivery_tier="armed",
                                )
                                result = await broadcaster.send_html(msg)
                                if result.status == "sent":
                                    from hunt_core.track.events import record_sent_delivery

                                    record_sent_delivery(
                                        symbol=symbol,
                                        direction=direction,
                                        message_id=result.message_id,
                                        html=msg,
                                        setup=setup,
                                        delivery_tier="armed",
                                        price=float(row.get("price") or 0) or None,
                                    )
                                    register_signal_open(
                                        tracker_state,
                                        symbol=symbol,
                                        direction=direction,
                                        price=float(row.get("price") or 0),
                                        setup={**setup, "delivery_tier": "armed", "telegram_sent": True},
                                        lifecycle=lifecycle_raw
                                        if isinstance(lifecycle_raw, dict)
                                        else None,
                                        now=now,
                                        entry_message_id=result.message_id,
                                    )
                                    mark_unified_sent(
                                        state,
                                        symbol=symbol,
                                        direction=direction,
                                        stage="armed",
                                        now=now,
                                    )
                                    tg_confirm_sent_this_tick = True
                                    continue
                        if (
                            send_telegram
                            and broadcaster is not None
                            and not confirmed_setup
                        ):
                            if await _maybe_send_early_alert(
                                broadcaster,
                                symbol=symbol,
                                direction=direction,
                                setup=setup,
                                row=row,
                                lifecycle_raw=lifecycle_raw,
                                state=state,
                                mode=mode,
                                now=now,
                            ):
                                advisory_sent_tick.add(f"{symbol}:{direction}")
                        if confirmed_setup and _confirm_delivery_suppressed(
                            tracker_state,
                            state,
                            symbol=symbol,
                            direction=direction,
                            now=now,
                        ):
                            continue
                        if confirmed_setup and symbol_loss_streak_cooldown(
                            tracker_state,
                            symbol=symbol,
                            direction=direction,
                            now=now,
                        ):
                            LOG.info(
                                "watch_telegram_skipped_loss_streak",
                                symbol=symbol,
                                direction=direction,
                            )
                            continue
                        if confirmed_setup and symbol_daily_tg_cap_reached(
                            tracker_state,
                            symbol=symbol,
                            direction=direction,
                            now=now,
                        ):
                            LOG.info(
                                "watch_telegram_skipped_daily_cap",
                                symbol=symbol,
                                direction=direction,
                            )
                            continue
                        if confirmed_setup and symbol_repeat_loser_blocked(
                            tracker_state,
                            symbol=symbol,
                            now=now,
                        ):
                            LOG.info(
                                "watch_telegram_skipped_repeat_loser",
                                symbol=symbol,
                                direction=direction,
                            )
                            continue
                        if confirmed_setup and global_confirm_burst_cap_reached(
                            tracker_state,
                            now=now,
                        ):
                            LOG.info(
                                "watch_telegram_skipped_global_burst",
                                symbol=symbol,
                                direction=direction,
                            )
                            append_signal_event(
                                "blocked",
                                symbol=symbol,
                                direction=direction,
                                detail="global_confirm_burst_cap",
                                payload={"block_code": "global_confirm_burst_cap"},
                            )
                            continue
                        if confirmed_setup:
                            confirm_gate, confirm_tier = _evaluate_delivery_row(
                                row,
                                hot_path=hot_path,
                                direction=direction,
                                setup=setup,
                                lifecycle=lifecycle_raw
                                if isinstance(lifecycle_raw, dict)
                                else None,
                                symbol=symbol,
                                refresh_live_price=False,
                                ws_feed=ws_feed,
                            )
                            if not confirm_gate.ok or confirm_tier is None:
                                lc = lifecycle_raw if isinstance(lifecycle_raw, dict) else {}
                                block_code = str(confirm_gate.code or "")
                                if _should_emit_blocked_telemetry(
                                    symbol,
                                    direction,
                                    block_code,
                                    now,
                                ):
                                    LOG.info(
                                        "watch_alert_blocked",
                                        symbol=symbol,
                                        direction=direction,
                                        score=setup.get("dump_score")
                                        or setup.get("long_score"),
                                        hunt_phase=lc.get("phase"),
                                        block_code=confirm_gate.code,
                                        reason=confirm_gate.message,
                                    )
                                    append_signal_event(
                                        "blocked",
                                        symbol=symbol,
                                        direction=direction,
                                        detail=confirm_gate.message,
                                        payload={
                                            "block_code": confirm_gate.code,
                                            "score": setup.get("dump_score")
                                            or setup.get("long_score"),
                                            "fuel": setup.get("dump_fuel")
                                            or setup.get("long_fuel"),
                                            "phase": setup.get("phase"),
                                            "lifecycle_phase": lc.get("phase"),
                                        },
                                    )
                                    _record_outcome_ledger(
                                        symbol=symbol,
                                        direction=direction,
                                        row=row,
                                        setup=setup,
                                        delivered=False,
                                        blockers=[str(confirm_gate.code or "")],
                                        event="blocked",
                                    )
                                process_setup_candidate(
                                    setup_candidates_state,
                                    symbol=symbol,
                                    direction=direction,
                                    setup=setup,
                                    row=row,
                                    lifecycle=lifecycle_raw,
                                    now=now,
                                    blocked=True,
                                    block_code=str(confirm_gate.code or ""),
                                )
                                continue
                        elif not evaluate_forming_gate(
                            setup,
                            direction=direction,
                            symbol=symbol,
                            lifecycle=lifecycle_raw,
                            row=row,
                            sniper_config=SNIPER_CONFIG,
                        ).ok:
                            from hunt_core.detect.setup_fields import setup_conviction_pct, setup_meets_strength

                            lc = lifecycle_raw if isinstance(lifecycle_raw, dict) else {}
                            conviction = setup_conviction_pct(setup, direction=direction)
                            if setup_meets_strength(
                                setup, direction=direction, symbol=symbol, tier="forming"
                            ):
                                LOG.info(
                                    "watch_setup_forming",
                                    symbol=symbol,
                                    direction=direction,
                                    conviction=round(conviction, 1),
                                    phase=setup.get("phase"),
                                    lifecycle_phase=lc.get("phase"),
                                    confirmed=False,
                                )
                                append_signal_event(
                                    "forming",
                                    symbol=symbol,
                                    direction=direction,
                                    detail=setup.get("phase") or "",
                                    payload={
                                        "conviction": conviction,
                                        "lifecycle_phase": lc.get("phase"),
                                    },
                                )
                            continue
                        # Lifecycle phase overrides static pin: a confirmed short in a
                        # live dump (or long in a bounce) must not die to mode=long/short.
                        lc_phase = str((lifecycle_raw or {}).get("phase") or "")
                        if (
                            direction == "short"
                            and mode not in ("short", "both")
                            and lc_phase
                            not in ("dump_active", "exhaustion_at_high", "distribution", "dump_initiating")
                        ):
                            continue
                        if (
                            direction == "long"
                            and mode not in ("long", "both")
                            and lc_phase
                            not in (
                                "post_dump_bounce",
                                "accumulation",
                                "recovery",
                                "breakout_arming",
                                "impulse_initiating",
                            )
                        ):
                            continue
                        if not _cooldown_ok(symbol, direction, state, now=now):
                            continue
                        from hunt_core.track.tracker import recent_stop_hit_cooldown

                        if recent_stop_hit_cooldown(
                            tracker_state,
                            symbol=symbol,
                            direction=direction,
                            now=now,
                        ):
                            LOG.info(
                                "watch_telegram_skipped_post_sl_cooldown",
                                symbol=symbol,
                                direction=direction,
                            )
                            continue
                        if not unified_cooldown_ok(
                            state,
                            symbol=symbol,
                            direction=direction,
                            stage="confirm",
                            now=now,
                        ):
                            LOG.info(
                                "watch_telegram_skipped_unified_cooldown",
                                symbol=symbol,
                                direction=direction,
                            )
                            continue
                        if _confirm_blocked_bias_wait(
                            direction=direction,
                            lifecycle=lifecycle_raw,
                            setup=setup,
                            symbol=symbol,
                        ):
                            LOG.info(
                                "watch_telegram_skipped_bias_wait",
                                symbol=symbol,
                                direction=direction,
                                phase="dump_active",
                            )
                            append_signal_event(
                                "blocked",
                                symbol=symbol,
                                direction=direction,
                                detail="bias_wait_dump_active",
                                payload={"block_code": "bias_wait_dump_active"},
                            )
                            continue
                        price_now = _refresh_live_price(
                            row, ws_feed=ws_feed, symbol=symbol
                        )
                        if _entry_past_tp1(setup, direction=direction, price=price_now):
                            LOG.info(
                                "watch_telegram_skipped_past_tp1",
                                symbol=symbol,
                                direction=direction,
                                price=price_now,
                                tp1=setup.get("tp1"),
                            )
                            continue
                        if tg_confirm_sent_this_tick:
                            LOG.info(
                                "watch_telegram_skipped_tick_cap",
                                symbol=symbol,
                                direction=direction,
                            )
                            continue
                        # Confirm delivery always passes full lane on live price
                        # (HOME-USDT 2026-06-18: hot lane bypassed family_vote / RR).
                        gate, delivery_tier = _evaluate_delivery_row(
                            row,
                            hot_path=False,
                            direction=direction,
                            setup=setup,
                            lifecycle=lifecycle_raw
                            if isinstance(lifecycle_raw, dict)
                            else None,
                            symbol=symbol,
                            refresh_live_price=False,
                            ws_feed=ws_feed,
                        )
                        if not gate.ok or delivery_tier is None:
                            LOG.info(
                                "watch_telegram_skipped_gate",
                                symbol=symbol,
                                direction=direction,
                                reason=gate.code if not gate.ok else "stale_tier",
                            )
                            continue
                        from hunt_core.deliver.templates import format_telegram_confirm

                        msg = format_telegram_confirm(
                            row,
                            direction=direction,
                            confirm_reasons=setup.get("confirm_hard") or [],
                            delivery_tier=delivery_tier,
                        )
                        result = await broadcaster.send_html(msg)
                        key = f"{symbol}:{direction}"
                        if result.status == "sent":
                            from hunt_core.track.events import record_sent_delivery

                            record_sent_delivery(
                                symbol=symbol,
                                direction=direction,
                                message_id=result.message_id,
                                html=msg,
                                setup=setup,
                                delivery_tier=str(delivery_tier or ""),
                                price=float(row.get("price") or 0) or None,
                            )
                            tg_confirm_sent_this_tick = True
                            state[key] = now.isoformat()
                            mark_unified_sent(
                                state,
                                symbol=symbol,
                                direction=direction,
                                stage="confirm",
                                now=now,
                            )
                            if pump_store is not None:
                                record_pump_signal_open(
                                    pump_store,
                                    symbol=symbol,
                                    direction=direction,
                                    now=now,
                                )
                            setup_latch = {
                                **setup,
                                "telegram_sent": True,
                                "delivery_tier": delivery_tier,
                            }
                            register_signal_open(
                                tracker_state,
                                symbol=symbol,
                                direction=direction,
                                price=price_now,
                                setup=setup_latch,
                                lifecycle=lifecycle_raw
                                if isinstance(lifecycle_raw, dict)
                                else None,
                                now=now,
                                entry_message_id=result.message_id,
                                features_open=feature_vector_from_row(row),
                                book_walls=book_walls_from_row(row),
                            )
                            _record_outcome_ledger(
                                symbol=symbol,
                                direction=direction,
                                row=row,
                                setup=setup_latch,
                                delivered=True,
                                event="delivered",
                            )
                            promote_to_confirm(
                                setup_candidates_state,
                                symbol=symbol,
                                direction=direction,
                                price=price_now,
                                now=now,
                            )
                            LOG.info(
                                "watch_telegram_sent",
                                symbol=symbol,
                                direction=direction,
                                message_id=result.message_id,
                                delivery_tier=delivery_tier,
                                price=price_now,
                                price_source=row.get("price_source"),
                                snapshot_batch_s=snap_elapsed,
                            )
                            append_signal_event(
                                "confirmed",
                                symbol=symbol,
                                direction=direction,
                                detail=f"telegram_sent:{delivery_tier}",
                                payload={
                                    "message_id": result.message_id,
                                    "delivery_tier": delivery_tier,
                                    "score": setup.get("dump_score")
                                    or setup.get("long_score"),
                                    "phase": setup.get("phase"),
                                },
                            )
                            append_signal_event(
                                "delivered",
                                symbol=symbol,
                                direction=direction,
                                detail=str(delivery_tier),
                                payload={
                                    "message_id": result.message_id,
                                    "delivery_tier": delivery_tier,
                                    "score": setup.get("dump_score")
                                    or setup.get("long_score"),
                                    "fuel": setup.get("dump_fuel")
                                    or setup.get("long_fuel"),
                                },
                            )
                        else:
                            LOG.warning(
                                "watch_telegram_failed",
                                symbol=symbol,
                                direction=direction,
                                status=result.status,
                                reason=result.reason,
                            )
            except Exception:
                LOG.exception("watch_symbol_process_failed", symbol=symbol)

        # Ticker safety net: symbols rotated out of this tick's batch still get
        # SL/TP extremes from the already-fetched 24h ticker (MEGA @ SL while
        # last_checked froze — universe rotation gap).
        seen = set(symbols)
        active_syms = {sym for sym, _ in iter_active_tracker_symbols(tracker_state)}
        missing_active = active_syms - seen
        ticker_events: list[Any] = []
        if missing_active and ticker_by_sym:
            ticker_now = clock.now_utc()
            ticker_events = reconcile_active_from_ticker(
                tracker_state,
                ticker_by_sym=ticker_by_sym,
                now=ticker_now,
                only_symbols=missing_active,
                ws_feed=ws_feed,
            )
            if ticker_events:
                LOG.info(
                    "watch_ticker_reconcile",
                    symbols=sorted(missing_active),
                    events=len(ticker_events),
                )

        # Orphan reconciliation: active signals whose symbol left the watchlist
        # would otherwise never close (PLAYUSDT held TP2 for 18h unnoticed).
        orphan_events = await _reconcile_orphan_signals(
            client, tracker_state, seen_symbols=seen, now=clock.now_utc()
        )
        orphan_events = ticker_events + orphan_events
        if orphan_events:
            orphan_now = clock.now_utc()
            orphan_sent: set[str] = set()
            for fu in orphan_events:
                LOG.info(
                    "watch_followup_orphan",
                    symbol=fu.symbol,
                    followup_event=fu.event,
                    detail=fu.detail,
                )
                if await _deliver_followup(
                    broadcaster,
                    fu,
                    {"symbol": fu.symbol},
                    tracker_state,
                    now=orphan_now,
                    send_telegram=send_telegram,
                ):
                    orphan_sent.add(fu.message_key)
            if orphan_sent:
                _record_followup_side_effects(
                    orphan_events,
                    sent_keys=orphan_sent,
                    now=orphan_now,
                    pump_store=pump_store,
                )
        if send_telegram and broadcaster is not None:
            await get_advisory_digest().maybe_flush(broadcaster)
        from hunt_core.runtime.tick_state import hunt_scan_store

        hunt_scan_store().put_many(rows)
        return rows
    finally:
        _save_state(state)
        buffer_tracker_state(tracker_state)
        save_setup_candidates_state(setup_candidates_state)
        flush_lake()



__all__ = ["run_tick"]
