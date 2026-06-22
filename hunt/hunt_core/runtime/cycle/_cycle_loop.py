"""Watch main loop — universe, prescan, tick scheduling (Phase 8 split)."""
from __future__ import annotations

import asyncio
import faulthandler
import json
import os
import time
from typing import Any

from hunt_core import clock
from hunt_core.data.collect import TickBatchCache, safe_fetch
from hunt_core.data.lake import FeatureLakeWriter, buffer_tick_rows, flush_lake
from hunt_core.data.scanner import (
    PrescanDebounceQueue,
    PrescanEngine,
    apply_quality_gates,
    prescan_from_tickers,
)
from hunt_core.data.baseline_store import batch_update_baselines
from hunt_core.data.universe import MAX_PRESCAN_MERGE, PINNED_SYMBOLS, resolve_watch_universe
from hunt_core.deliver.digest import DigestCandidate, get_digest_scheduler
from hunt_core.deliver.telegram import TelegramBroadcaster
from hunt_core.domain.config import (
    SCAN_INTERVAL_S,
    TICK_ROTATE_INTERVAL_S,
    TICK_ROTATE_MIN_BYTES,
)
from hunt_core.domain.market_regime import (
    REGIME_REFRESH_S,
    apply_snapshot,
    load_regime_file,
    refresh_market_regime,
)
from hunt_core.errors import DEFENSIVE_EXC, defensive_exc_types
from hunt_core.features.prepare import min_required_bars
from hunt_core.shared.market import (
    CrossExchangeConfig,
    HuntLoadPlanner,
    apply_cross_exchange_env,
    create_hunt_market_plane_from_settings,
    fetch_secondary_ticker_overlay,
    load_cross_exchange_config,
    refresh_cross_exchange_cache,
)
from hunt_core.params.store import migrate_calibration_split, prescan_thresholds
from hunt_core.runtime.cycle._cycle_confirm import _advisory_tg_enabled
from hunt_core.runtime.cycle._cycle_tick import run_tick
from hunt_core.runtime.state import LOG, OUT_PATH, SYMBOL_WATCH_MODES, new_session_state, should_stop
from hunt_core.runtime.telegram_commands import build_hunt_telegram_commands
from hunt_core.runtime.tick_io import rotate_hunt_ticks, rotate_telemetry_jsonl
from hunt_core.track.events import record_funnel_stage
from hunt_core.track.pump_history import (
    backfill_from_jsonl,
    load_pump_history,
    observe_prices,
    save_pump_history,
)
from hunt_core.track.tracker import iter_active_tracker_symbols, load_tracker_state
from hunt_core.domain.config import load_settings


def _build_digest_candidates(
    gated_ticker_rows: list[dict[str, Any]],
) -> list[DigestCandidate]:
    """Score gated tickers into pump/dump candidates for the scheduled digest.

    Score = |24h change %| with a mild liquidity weight so a thin-volume mover
    does not outrank a high-volume one at equal magnitude.
    """
    out: list[DigestCandidate] = []
    for row in gated_ticker_rows:
        sym = str(row.get("symbol") or "").strip().upper()
        if not sym:
            continue
        chg_raw = row.get("price_change_percent")
        if chg_raw is None:
            chg_raw = row.get("price_change_pct")
        try:
            chg = float(chg_raw)
        except (TypeError, ValueError):
            continue
        if not chg:
            continue
        try:
            qvol = float(row.get("quote_volume") or row.get("quoteVolume") or 0.0)
        except (TypeError, ValueError):
            qvol = 0.0
        liq_w = 1.0 + min(qvol / 1e8, 1.0) * 0.25
        out.append(
            DigestCandidate(
                symbol=sym,
                direction="pump" if chg > 0 else "dump",
                score=abs(chg) * liq_w,
                change_24h_pct=chg,
            )
        )
    return out


async def run_loop(
    cli_symbols: tuple[str, ...],
    interval_s: int,
    once: bool,
    *,
    send_telegram: bool,
) -> None:

    from hunt_core.runtime.cycle import _impl as _loop_impl

    run_hot_kline_tick = _loop_impl.run_hot_kline_tick
    _overlay_ws_tickers = _loop_impl._overlay_ws_tickers
    _TICK_LOCK = _loop_impl._TICK_LOCK

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _prev_loop_handler = asyncio.get_running_loop().get_exception_handler()

    def _hunt_loop_exc_handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        if exc is not None:
            from hunt_core.shared.market import HuntCcxtStreams

            if HuntCcxtStreams._ws_transport_fatal(exc):
                LOG.debug("asyncio_orphan_ws | %s", exc)
                return
        if _prev_loop_handler is not None:
            _prev_loop_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    asyncio.get_running_loop().set_exception_handler(_hunt_loop_exc_handler)
    if migrate_calibration_split():
        LOG.info("hunt_calibration_migrated", path="hunt/data/hunt_calibration.json")
    try:
        from hunt_core._dev.rebuild_calibration import rebuild_calibration
        from hunt_core.params.store import invalidate_calibration_cache

        cal = rebuild_calibration(dry_run=False)
        invalidate_calibration_cache()
        LOG.info(
            "hunt_calibration_rebuilt",
            version=cal.get("version"),
            n_outcomes=(cal.get("outcome_stats") or {}).get("n"),
        )
    except Exception:
        LOG.exception("hunt_calibration_rebuild_failed")
    try:
        rot_stats = rotate_hunt_ticks()
        if rot_stats.get("appended_lines") or rot_stats.get("archived"):
            LOG.info("hunt_tick_rotate", **rot_stats)
        tel_stats = rotate_telemetry_jsonl()
        if tel_stats.get("rotated"):
            LOG.info("hunt_telemetry_rotate", **tel_stats)
    except Exception:
        LOG.exception("hunt_tick_rotate_failed")
    settings = load_settings()
    broadcaster: TelegramBroadcaster | None = None
    if send_telegram:
        if not settings.tg_token or not settings.target_chat_id:
            msg = "Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)"
            raise RuntimeError(msg)
        for attempt in range(3):
            try:
                broadcaster = TelegramBroadcaster(settings.tg_token, settings.target_chat_id)
                await broadcaster.preflight_check()
                LOG.info("watch_telegram_ready", chat=settings.target_chat_id, mode="confirm_only")
                break
            except DEFENSIVE_EXC as exc:
                LOG.warning("watch_telegram_preflight_failed", attempt=attempt + 1, error=repr(exc))
                broadcaster = None
                if attempt < 2:
                    await asyncio.sleep(2.0 * (attempt + 1))
        if broadcaster is None:
            LOG.warning("watch_telegram_disabled", reason="preflight_failed")
            send_telegram = False

    minimums = min_required_bars(
        min_bars_15m=settings.filters.min_bars_15m,
        min_bars_1h=settings.filters.min_bars_1h,
        min_bars_4h=settings.filters.min_bars_4h,
    )
    cross_cfg: CrossExchangeConfig = load_cross_exchange_config()
    apply_cross_exchange_env(cross_cfg)
    LOG.info(
        "hunt_multi_exchange",
        enabled=cross_cfg.enabled,
        ws=cross_cfg.ws_enabled,
        exchanges=",".join(cross_cfg.exchanges),
        refresh_s=cross_cfg.refresh_interval_s,
        max_symbols=cross_cfg.max_symbols_per_refresh,
    )
    plane = await create_hunt_market_plane_from_settings(settings)
    client = plane.client
    ws_feed = plane.streams
    spot_companion = plane.spot
    ws_feed.set_symbols(list(cli_symbols))
    await ws_feed.start()
    from hunt_core.runtime.hot_loop import HotKlineLoop

    hot_kline_loop = HotKlineLoop(run_hot_tick=run_hot_kline_tick)
    if not once:
        hot_kline_loop.start(ws_feed, once=False)
    # Persistent across ticks: kline/OI caches live in client; oi_flush/oi_build need prev tick.
    prev_oi: dict[str, float | None] = {}
    last_bias: dict[str, str] = {}
    last_lifecycle_phase: dict[str, str] = {}
    symbol_state = new_session_state()
    from hunt_core.data.frame_cache import reset_frame_cache

    reset_frame_cache()
    feature_lake = FeatureLakeWriter()
    prescan_debounce = PrescanDebounceQueue(
        debounce_s=float(
            os.getenv(
                "HUNT_PRESCAN_DEBOUNCE_S",
                str(prescan_thresholds()["debounce_s"]),
            )
            or prescan_thresholds()["debounce_s"]
        ),
    )
    prescan_engine = PrescanEngine()
    load_planner = HuntLoadPlanner()
    digest_scheduler = get_digest_scheduler()
    _lake_warmed_syms: set[str] = set()
    pump_store = load_pump_history()
    if not pump_store.symbols and not pump_store.event_log:
        backfill_from_jsonl(pump_store)
        save_pump_history(pump_store)

    # --once smoke: skip heavy first-tick scan/cross-ex (full watchlist prescan).
    _now_mono = time.monotonic()
    last_scan = _now_mono if once else 0.0
    last_regime = _now_mono if once else 0.0
    last_cross_ex = _now_mono if once else 0.0
    _cross_ex_cache: dict[str, dict[str, Any]] = {}
    # P1.8: secondary-CEX 24h ticker overlay for the prescan outlier matrix.
    _secondary_ticker_overlay: dict[str, dict[str, Any]] = {}
    last_secondary_tickers = 0.0
    last_tick_rotate = time.monotonic()
    batch_cache = TickBatchCache()
    cached = load_regime_file()
    if cached is not None:
        apply_snapshot(cached)
    if not once:
        try:
            await refresh_market_regime(client)
            last_regime = time.monotonic()
        except Exception:
            LOG.exception("market_regime_startup_failed")
    elif cached is not None:
        LOG.info("watch_once_regime_cached", regime=getattr(cached, "regime", None))

    _startup_tg = os.getenv("HUNT_STARTUP_TELEGRAM", "1").strip().lower()
    if (
        broadcaster is not None
        and send_telegram
        and not once
        and _startup_tg not in {"0", "false", "no"}
    ):
        cross_line = ", ".join(cross_cfg.exchanges) if cross_cfg.enabled else "off"
        try:
            await broadcaster.send_html(
                "🟢 <b>Hunt live</b>\n"
                f"Interval {interval_s}s · confirm-only alerts\n"
                f"Cross-intel: {cross_line}\n"
                "<i>Не auto-trade</i>"
            )
            LOG.info("watch_startup_telegram_sent", chat=settings.target_chat_id)
        except Exception:
            LOG.exception("watch_startup_telegram_failed")

    # /signal polling conflicts with a second getUpdates consumer — only when TG sends enabled.
    tg_cmds = (
        build_hunt_telegram_commands(settings, client=client)
        if send_telegram and settings.tg_token
        else None
    )
    tg_task: asyncio.Task[None] | None = None
    if tg_cmds is not None:
        tg_task = asyncio.create_task(tg_cmds.run_forever(), name="hunt_tg_commands")
        LOG.info("hunt_telegram_commands_scheduled")

    deep_task: asyncio.Task[None] | None = None
    if not once and os.getenv("HUNT_DEEP_PINNED_LOOP", "1").strip().lower() not in {"0", "false", "no"}:
        from hunt_core.runtime.deep_assembly import deep_pinned_loop

        deep_task = asyncio.create_task(
            deep_pinned_loop(client, broadcaster, send_telegram=send_telegram),
            name="deep_pinned_loop",
        )
        LOG.info("deep_pinned_loop_scheduled")

    expansion_review_task: asyncio.Task[None] | None = None
    from hunt_core.analysis.expansion_engine.config import load_expansion_config

    exp_cfg = load_expansion_config()
    if exp_cfg.enabled and exp_cfg.lab_runtime:
        try:
            from hunt_core.analysis.expansion_engine.runtime_state import load_expansion_runtime_state

            load_expansion_runtime_state()
        except Exception:
            LOG.exception("expansion_fsm_load_failed")

    if not once and exp_cfg.enabled and exp_cfg.lab_runtime and exp_cfg.review_loop:
        from hunt_core.runtime.deep_assembly import expansion_outcome_review_loop

        expansion_review_task = asyncio.create_task(
            expansion_outcome_review_loop(client),
            name="expansion_outcome_review_loop",
        )
        LOG.info("expansion_review_loop_scheduled", interval_s=exp_cfg.review_interval_s)

    expansion_universe_task: asyncio.Task[None] | None = None
    if not once and exp_cfg.enabled and exp_cfg.lab_runtime and exp_cfg.tg_universe_scan and send_telegram:
        from hunt_core.runtime.expansion_universe_scan import expansion_universe_scan_loop

        expansion_universe_task = asyncio.create_task(
            expansion_universe_scan_loop(broadcaster, send_telegram=send_telegram),
            name="expansion_universe_scan_loop",
        )
        LOG.info(
            "expansion_universe_scan_scheduled",
            interval_s=exp_cfg.tg_universe_interval_s,
            top_n=exp_cfg.tg_universe_top_n,
        )

    # Hang watchdog: if a cycle stalls (e.g. an unbounded loop in scan/levels on
    # degenerate data), faulthandler dumps every Python thread's stack — it works
    # even while the GIL is held by a tight loop — then hard-exits so the process
    # stops being a frozen zombie and can be restarted.
    faulthandler.enable()
    _wd_timeout_s = float(os.getenv("HUNT_WATCHDOG_S", "300") or 300)
    _wd_file = (OUT_PATH.parent / "hunt_watchdog.log").open("a", buffering=1)
    LOG.info("hunt_watchdog_armed", timeout_s=_wd_timeout_s)
    _pinned_brief_sent = False
    try:
        tick_ctx: dict[str, Any] | None = None
        while not should_stop():
            started = time.monotonic()
            try:
                if not once and time.monotonic() - last_regime >= REGIME_REFRESH_S:
                    try:
                        snap = await refresh_market_regime(client)
                        last_regime = time.monotonic()
                        LOG.info(
                            "market_regime_tick",
                            regime=snap.regime,
                            anomaly_chg=snap.params.anomaly_min_chg_24h_pct,
                            n_liquid=snap.n_liquid,
                        )
                    except Exception:
                        LOG.exception("market_regime_refresh_failed")
                        last_regime = time.monotonic()

                if not once and time.monotonic() - last_scan >= SCAN_INTERVAL_S:
                    try:
                        from hunt_core.data.scanner import run_scan

                        summary = await run_scan(
                            limit=30, min_score=45.0, client=client
                        )
                        LOG.info(
                            "hunt_scan_refresh",
                            watch=summary.get("watch_count"),
                            priority=summary.get("priority_count"),
                        )
                    except defensive_exc_types(asyncio.IncompleteReadError) as exc:
                        LOG.warning("hunt_scan_refresh_failed", error=repr(exc))
                    last_scan = time.monotonic()

                settings = load_settings()
                now = clock.now_utc()
                await client.apply_pending_proxy_at_cycle()
                ticker_raw = await safe_fetch(
                    client.fetch_ticker_24h,
                    context="ticker_24h",
                    client=client,
                ) or []
                ticker_by_sym = {str(t.get("symbol")): t for t in ticker_raw if t.get("symbol")}
                ignition_by_sym: dict[str, Any] = {}
                ex = client.exchange
                from hunt_core.shared.market import gate_symbol_dict_keys, gate_symbol_list

                ignition_by_sym = gate_symbol_dict_keys(
                    ignition_by_sym, exchange=ex, label="ignition"
                )
                # P1.6: prescan outliers feed an internal debounce queue, NOT
                # Telegram. Ready (debounced) symbols merge into the watch universe.
                gated_ticker_rows = [
                    t for t in ticker_raw if apply_quality_gates(t)[0]
                ]
                # P1.8: refresh secondary-CEX ticker overlay on the cross-ex cadence
                # (soft — a stale/empty overlay leaves prescan on primary only).
                if (
                    not once
                    and cross_cfg.enabled
                    and len(gated_ticker_rows) <= 100
                    and (
                        not _secondary_ticker_overlay
                        or time.monotonic() - last_secondary_tickers
                        >= cross_cfg.refresh_interval_s
                    )
                ):
                    try:
                        _secondary_ticker_overlay = await fetch_secondary_ticker_overlay(
                            client, cfg=cross_cfg
                        )
                    except Exception:
                        LOG.exception("secondary_ticker_overlay_refresh_failed")
                    last_secondary_tickers = time.monotonic()
                # P1.10: primary OI % change overlay (cached ratio → percent; None
                # when unseen, so divergence stays soft).
                _oi_change_by_sym: dict[str, float | None] = {}
                for _t in gated_ticker_rows:
                    _sym = str(_t.get("symbol") or "")
                    if not _sym:
                        continue
                    _ratio = client.get_cached_oi_change(_sym)
                    _oi_change_by_sym[_sym] = (
                        _ratio * 100.0 if _ratio is not None else None
                    )
                batch_update_baselines(gated_ticker_rows, oi_by_sym=_oi_change_by_sym)
                _prescan_hits = prescan_from_tickers(
                    gated_ticker_rows,
                    engine=prescan_engine,
                    secondary_overlay=_secondary_ticker_overlay,
                    oi_change_by_sym=_oi_change_by_sym,
                )
                prescan_debounce.offer(_prescan_hits)
                # P1.17: strongest outlier per symbol for the early-advisory merge.
                prescan_outlier_by_sym: dict[str, dict[str, Any]] = {}
                for _h in _prescan_hits:
                    prev = prescan_outlier_by_sym.get(_h.symbol)
                    if prev is None or _h.energy > prev.get("energy", 0.0):
                        prescan_outlier_by_sym[_h.symbol] = {
                            "direction": _h.direction,
                            "change_pct": _h.change_pct,
                            "energy": _h.energy,
                            "readiness_direction": _h.readiness_direction,
                            "interval": _h.interval,
                            "cross_venues": _h.cross_venues,
                            "oi_divergence": _h.oi_divergence,
                        }
                prescan_ready = prescan_debounce.drain_ready()
                if prescan_ready:
                    LOG.info(
                        "hunt_prescan_debounce_ready",
                        count=len(prescan_ready),
                        head=[d.symbol for d in prescan_ready[:6]],
                    )
                    for d in prescan_ready[:12]:
                        record_funnel_stage(
                            "prescan",
                            symbol=d.symbol,
                            direction=d.direction,
                            detail=f"{d.interval}:{d.change_pct:.1f}%",
                        )
                price_map = {
                    sym: float(row.get("last_price") or 0)
                    for sym, row in ticker_by_sym.items()
                    if float(row.get("last_price") or 0) > 0
                }
                observe_prices(pump_store, price_map, now=now)
                pump_stats_by_sym = {
                    sym: st.to_public() for sym, st in pump_store.symbols.items()
                }
                if once:
                    merged = list(dict.fromkeys(s.upper() for s in cli_symbols))
                    mode_map = {
                        s: SYMBOL_WATCH_MODES.get(s, "short") for s in merged
                    }
                    prescan_pinned_ready: set[str] = set()
                else:
                    full_symbols, mode_map = resolve_watch_universe(
                        settings,
                        static_modes=SYMBOL_WATCH_MODES,
                        ignited=ignition_by_sym,
                    )
                    merged = list(full_symbols)
                    for sym in cli_symbols:
                        s = sym.upper()
                        if s not in merged:
                            merged.append(s)
                        mode_map.setdefault(s, SYMBOL_WATCH_MODES.get(s, "short"))
                    # P1.6 merge: debounced prescan outliers join the ignition path.
                    prescan_merge_cap = int(
                        os.getenv(
                            "HUNT_PRESCAN_MERGE_CAP",
                            str(prescan_thresholds()["merge_cap"]),
                        )
                        or prescan_thresholds()["merge_cap"]
                    )
                    prescan_to_merge = prescan_ready[: max(prescan_merge_cap, 0)]
                    prescan_pinned_ready = set()
                    if len(prescan_ready) > len(prescan_to_merge):
                        LOG.info(
                            "hunt_prescan_merge_capped",
                            ready=len(prescan_ready),
                            merged=len(prescan_to_merge),
                            cap=prescan_merge_cap,
                        )
                    prescan_pinned_ready: set[str] = set()
                    for d in prescan_to_merge:
                        s = d.symbol.upper()
                        if s not in merged:
                            merged.append(s)
                        energy = float(getattr(d, "energy", 0) or 0)
                        if s in PINNED_SYMBOLS and energy >= 20.0:
                            prescan_pinned_ready.add(s)
                        mode_map.setdefault(
                            s, "short" if d.direction in {"dump", "bear"} else "long"
                        )
                    # Keep open tracker positions in every tick batch — otherwise
                    # SL/TP followups stall until orphan kline reconcile.
                    tracker_pin = load_tracker_state()
                    pinned_n = 0
                    for sym, direction in iter_active_tracker_symbols(tracker_pin):
                        if sym not in merged:
                            merged.append(sym)
                            pinned_n += 1
                        mode_map.setdefault(
                            sym, "short" if direction == "short" else "long"
                        )
                    if pinned_n:
                        LOG.info("watch_tracker_pin", symbols=pinned_n)
                merged = gate_symbol_list(merged, exchange=ex, label="watch_universe")
                active = tuple(dict.fromkeys(merged))
                # Warm the feature lake for any symbol that has never been backfilled.
                # Needed so the phase classifier (requires close × 30+) does not return
                # NEUTRAL on every tick for freshly-promoted scanner candidates.
                _cold = [
                    s for s in active
                    if s not in _lake_warmed_syms
                ]
                if _cold:
                    from hunt_core.data.lake import query_features as _qf
                    from hunt_core.data.lake_warmup import ensure_lake_warm
                    _really_cold = [
                        s for s in _cold
                        if _qf(s, tf="15m", limit=31).height < 30
                    ]
                    if _really_cold:
                        LOG.info("lake_warmup_start", symbols=_really_cold)
                        _warmup_writer = FeatureLakeWriter()
                        await ensure_lake_warm(client, _really_cold, writer=_warmup_writer)
                        _warmup_writer.close()
                    _lake_warmed_syms.update(_cold)
                hunt_active = tuple(
                    s
                    for s in active
                    if s not in PINNED_SYMBOLS or s in prescan_pinned_ready
                )
                load_plan = load_planner.plan_tick(
                    hunt_active,
                    ignited=set(ignition_by_sym.keys()),
                    interval_s=float(interval_s),
                )
                LOG.info(
                    "hunt_load_plan",
                    symbols=len(hunt_active),
                    ws_symbols=len(active),
                    pinned_excluded=len(active) - len(hunt_active),
                    parallel=load_plan.parallel,
                    full=load_plan.full_count,
                    fast=load_plan.fast_count,
                    est_weight=load_plan.estimated_binance_weight,
                    est_fapi=load_plan.estimated_fapi_calls,
                    cross_max=load_plan.cross_max_symbols,
                    skip_secondary=load_plan.skip_secondary_tickers,
                )
                _overlay_ws_tickers(ticker_by_sym, active, ws_feed)
                ws_feed.set_symbols(
                    list(active),
                    priority=list(ignition_by_sym.keys()) + list(cli_symbols),
                )
                ws_n = min(len(active), 24) + 1
                if ws_feed.kline_ws_enabled:
                    ws_n += min(len(active), 24)
                LOG.info(
                    "watch_universe",
                    symbols=len(hunt_active),
                    ws_symbols=len(active),
                    ignited=len(ignition_by_sym),
                    ws_streams=ws_n,
                    kline_ws=ws_feed.kline_ws_enabled,
                    kline_interval="1m",
                    list=list(active)[:8],
                )

                if (
                    not once
                    and cross_cfg.enabled
                    and (
                        not _cross_ex_cache
                        or time.monotonic() - last_cross_ex >= cross_cfg.refresh_interval_s
                    )
                ):
                    try:
                        from dataclasses import replace

                        cross_cfg_tick = replace(
                            cross_cfg,
                            max_symbols_per_refresh=load_plan.cross_max_symbols,
                        )
                        await refresh_cross_exchange_cache(
                            client,
                            active,
                            _cross_ex_cache,
                            cfg=cross_cfg_tick,
                        )
                    except Exception:
                        LOG.exception("cross_exchange_refresh_failed")
                    last_cross_ex = time.monotonic()

                tick_ctx = {
                    "active": active,
                    "settings": settings,
                    "minimums": minimums,
                    "client": client,
                    "prev_oi": prev_oi,
                    "last_bias": last_bias,
                    "last_lifecycle_phase": last_lifecycle_phase,
                    "mode_map": mode_map,
                    "broadcaster": broadcaster,
                    "send_telegram": send_telegram,
                    "ticker_by_sym": ticker_by_sym,
                    "ignition_by_sym": ignition_by_sym,
                    "pump_stats_by_sym": pump_stats_by_sym,
                    "pump_store": pump_store,
                    "ws_feed": ws_feed,
                    "spot_companion": spot_companion,
                    "batch_cache": batch_cache,
                    "tier": "full",
                    "tier_by_symbol": load_plan.tier_by_symbol,
                    "snapshot_parallel": load_plan.parallel,
                    "cross_ex_cache": _cross_ex_cache,
                    "prescan_outlier_by_sym": prescan_outlier_by_sym,
                    "symbol_state": symbol_state,
                    "feature_lake": feature_lake,
                    "hot_path": False,
                }
                hot_kline_loop.set_tick_ctx(tick_ctx)
                if not once:
                    faulthandler.cancel_dump_traceback_later()
                    faulthandler.dump_traceback_later(
                        _wd_timeout_s, repeat=False, file=_wd_file, exit=True
                    )
                async with _TICK_LOCK:
                    rows = await run_tick(
                        hunt_active, **{k: v for k, v in tick_ctx.items() if k != "active"}
                    )
                if (
                    not once
                    and not _pinned_brief_sent
                    and send_telegram
                    and broadcaster is not None
                ):
                    from hunt_core.runtime.pinned_brief import (
                        deliver_pinned_startup_brief,
                        pinned_startup_brief_enabled,
                    )

                    if pinned_startup_brief_enabled():
                        try:
                            n_brief = await deliver_pinned_startup_brief(
                                broadcaster, client=client
                            )
                            LOG.info("watch_pinned_startup_brief", sent=n_brief)
                        except Exception:
                            LOG.exception("watch_pinned_startup_brief_failed")
                        _pinned_brief_sent = True
                ban_telemetry = client.rest_gate.guard.telemetry
                if ban_telemetry.last_at_mono and (
                    time.monotonic() - ban_telemetry.last_at_mono < interval_s + 5
                ):
                    LOG.warning(
                        "hunt_ccxt_ban_telemetry",
                        kind=ban_telemetry.last_kind,
                        context=ban_telemetry.last_context,
                        ip_bans=ban_telemetry.ip_ban_count,
                        rate_limits=ban_telemetry.rate_limit_count,
                        pause_remaining_s=round(client.rest_gate.guard.remaining_pause_s(), 1),
                        weight_used=client.rest_gate.weight_budget.used_weight,
                    )
                # P1.7: scheduled pump/dump digest (1h/3h/6h) — distinct from the
                # per-tick advisory batch. Candidates come from gated tickers.
                if send_telegram and broadcaster is not None:
                    digest_candidates = _build_digest_candidates(gated_ticker_rows)
                    sent_digest = await digest_scheduler.maybe_emit(
                        broadcaster, digest_candidates
                    )
                    if sent_digest:
                        LOG.info("hunt_digest_scheduled_sent", candidates=len(digest_candidates))
                save_pump_history(pump_store)
                buffer_tick_rows(rows)
                try:
                    if exp_cfg.lab_runtime:
                        from hunt_core.analysis.expansion_engine.runtime_state import (
                            maybe_save_expansion_runtime_state,
                        )

                        maybe_save_expansion_runtime_state()
                except Exception:
                    LOG.debug("expansion_runtime_maybe_save_failed", exc_info=True)
                if not once:
                    faulthandler.cancel_dump_traceback_later()
                if (
                    OUT_PATH.exists()
                    and OUT_PATH.stat().st_size >= TICK_ROTATE_MIN_BYTES
                    and time.monotonic() - last_tick_rotate >= TICK_ROTATE_INTERVAL_S
                ):
                    try:
                        rot_stats = rotate_hunt_ticks()
                        if rot_stats.get("appended_lines") or rot_stats.get("archived"):
                            LOG.info("hunt_tick_rotate_periodic", **rot_stats)
                        tel_stats = rotate_telemetry_jsonl()
                        if tel_stats.get("rotated"):
                            LOG.info("hunt_telemetry_rotate_periodic", **tel_stats)
                        last_tick_rotate = time.monotonic()
                    except Exception:
                        LOG.exception("hunt_tick_rotate_periodic_failed")
                if once:
                    print(json.dumps(rows, indent=2, default=str))
                    break
            except Exception:
                LOG.exception("dump_watch_tick_error")
                faulthandler.cancel_dump_traceback_later()
                if once:
                    raise
            if once:
                break
            deadline = started + max(1.0, float(interval_s))
            while time.monotonic() < deadline and not should_stop():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(1.0, remaining))
    finally:
        faulthandler.cancel_dump_traceback_later()
        try:
            _wd_file.close()
        except Exception:
            LOG.exception("hunt_watchdog_close_failed")
        try:
            flush_lake()
        except Exception:
            LOG.exception("tick_buffer_flush_failed")
        try:
            from hunt_core.maps.engine import get_map_store
            from hunt_core.paths import MAPS_LAKE_JSONL

            get_map_store().flush_lake(MAPS_LAKE_JSONL)
        except Exception:
            LOG.exception("maps_lake_flush_failed")
        feature_lake.close()
        if tg_task is not None:
            tg_task.cancel()
            try:
                await tg_task
            except asyncio.CancelledError:
                pass
        if deep_task is not None:
            deep_task.cancel()
            try:
                await deep_task
            except asyncio.CancelledError:
                pass
        if expansion_review_task is not None:
            expansion_review_task.cancel()
            try:
                await expansion_review_task
            except asyncio.CancelledError:
                pass
        if expansion_universe_task is not None:
            expansion_universe_task.cancel()
            try:
                await expansion_universe_task
            except asyncio.CancelledError:
                pass
        if exp_cfg.lab_runtime:
            try:
                from hunt_core.analysis.expansion_engine.runtime_state import save_expansion_runtime_state

                save_expansion_runtime_state()
            except Exception:
                LOG.exception("expansion_fsm_save_failed")
        if tg_cmds is not None:
            await tg_cmds.close()
        await hot_kline_loop.stop()
        try:
            await plane.aclose()
        except Exception:
            LOG.exception("hunt_plane_close_failed")


__all__ = ["run_loop"]
