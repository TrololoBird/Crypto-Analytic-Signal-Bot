"""Watch main loop — universe, prescan, tick scheduling (Phase 8 split)."""
from __future__ import annotations

import asyncio
import faulthandler
import html
import json
import os
import time
from types import SimpleNamespace
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
from hunt_core.data.universe import MAX_PRESCAN_MERGE, resolve_watch_universe
from hunt_core.deliver.digest import DigestCandidate, get_digest_scheduler
from hunt_core.deliver.telegram import TelegramBroadcaster
from hunt_core.domain.config import (
    IGNITION_MIN_VOL_DELTA_USD,
    IGNITION_TELEGRAM_ENABLED,
    IGNITION_TTL_S,
    IGNITION_WINDOW_S,
    SCAN_INTERVAL_S,
    TICK_ROTATE_INTERVAL_S,
    TICK_ROTATE_MIN_BYTES,
)
from hunt_core.domain.market_regime import (
    REGIME_REFRESH_S,
    active_params,
    apply_snapshot,
    load_regime_file,
    refresh_market_regime,
)
from hunt_core.errors import DEFENSIVE_EXC, defensive_exc_types
from hunt_core.features.prepare import min_required_bars
from hunt_core.market.capacity import HuntLoadPlanner
from hunt_core.market.cross import (
    CrossExchangeConfig,
    apply_cross_exchange_env,
    fetch_secondary_ticker_overlay,
    load_cross_exchange_config,
    refresh_cross_exchange_cache,
)
from hunt_core.market.factory import create_hunt_market_plane_from_settings
from hunt_core.params.store import migrate_calibration_split
from hunt_core.runtime.cycle._cycle_confirm import _advisory_tg_enabled
from hunt_core.runtime.cycle._cycle_tick import run_tick
from hunt_core.runtime.state import LOG, OUT_PATH, SYMBOL_WATCH_MODES, new_session_state, should_stop
from hunt_core.runtime.telegram_commands import build_hunt_telegram_commands
from hunt_core.runtime.tick_io import rotate_hunt_ticks, rotate_telemetry_jsonl
# Legacy ignition/adaptive-store scanner removed; inert no-op stubs (no ticker-stream
# ignition alerts — the fusion engine detects on closed-bar ticks).
def _empty_ignition_state() -> SimpleNamespace:
    return SimpleNamespace(active={})


def format_ignition_telegram(*_a, **_k) -> str: return ""
def load_adaptive_store(*_a, **_k) -> dict: return {}
def load_ignition_state(*_a, **_k) -> SimpleNamespace:
    return _empty_ignition_state()
def mark_ignition_notified(*_a, **_k) -> None: return None
def pending_ignition_alerts(*_a, **_k) -> list: return []
def process_ticker_snapshots(_snaps, state, *_a, **_k):
    if state is None or not hasattr(state, "active"):
        state = _empty_ignition_state()
    return [], state
def save_adaptive_store(*_a, **_k) -> None: return None
def save_ignition_state(*_a, **_k) -> None: return None
from hunt_core.track.events import record_funnel_stage
from hunt_core.track.pump_history import (
    backfill_from_jsonl,
    format_history_telegram,
    load_pump_history,
    observe_prices,
    record_pump_leg,
    save_pump_history,
    stats_for,
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
            from hunt_core.market.streams import HuntCcxtStreams

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
    ignition_state = load_ignition_state()
    prescan_debounce = PrescanDebounceQueue(
        debounce_s=float(os.getenv("HUNT_PRESCAN_DEBOUNCE_S", "30") or 30),
    )
    prescan_engine = PrescanEngine()
    load_planner = HuntLoadPlanner()
    digest_scheduler = get_digest_scheduler()
    pump_store = load_pump_history()
    adaptive_store = load_adaptive_store()
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

    # Hang watchdog: if a cycle stalls (e.g. an unbounded loop in scan/levels on
    # degenerate data), faulthandler dumps every Python thread's stack — it works
    # even while the GIL is held by a tight loop — then hard-exits so the process
    # stops being a frozen zombie and can be restarted.
    faulthandler.enable()
    _wd_timeout_s = float(os.getenv("HUNT_WATCHDOG_S", "300") or 300)
    _wd_file = (OUT_PATH.parent / "hunt_watchdog.log").open("a", buffering=1)
    LOG.info("hunt_watchdog_armed", timeout_s=_wd_timeout_s)
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
                new_ignitions, ignition_state = process_ticker_snapshots(
                    ticker_raw,
                    ignition_state,
                    now=now,
                    window_s=float(IGNITION_WINDOW_S),
                    min_pct=float(active_params().ignition_min_pct),
                    min_vol_delta_usd=float(IGNITION_MIN_VOL_DELTA_USD),
                    min_qvol_usd=float(active_params().ignition_min_qvol_usd),
                    ttl_s=float(IGNITION_TTL_S),
                    adaptive=adaptive_store,
                )
                save_ignition_state(ignition_state)
                save_adaptive_store(adaptive_store)
                if not IGNITION_TELEGRAM_ENABLED:
                    for ig in ignition_state.active.values():
                        ig.notified = True
                ignition_by_sym = {
                    sym: ig.to_row() for sym, ig in ignition_state.active.items()
                }
                ex = client.exchange
                from hunt_core.market.symbol_gate import gate_symbol_dict_keys, gate_symbol_list

                ignition_by_sym = gate_symbol_dict_keys(
                    ignition_by_sym, exchange=ex, label="ignition"
                )
                for sym in list(ignition_state.active.keys()):
                    if sym not in ignition_by_sym:
                        del ignition_state.active[sym]
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
                    if prev is None or abs(_h.change_pct) > abs(prev["change_pct"]):
                        prescan_outlier_by_sym[_h.symbol] = {
                            "direction": _h.direction,
                            "change_pct": _h.change_pct,
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
                for ev in new_ignitions:
                    LOG.info(
                        "hunt_ignition",
                        symbol=ev.symbol,
                        direction=ev.direction,
                        price_delta_pct=round(ev.price_delta_pct, 2),
                        vol_delta_usd=round(ev.vol_delta_usd, 0),
                        window_s=round(ev.window_s, 1),
                    )
                    tick = ticker_by_sym.get(ev.symbol) or {}
                    ign_price = float(tick.get("last_price") or 0)
                    if ign_price > 0:
                        record_pump_leg(
                            pump_store,
                            symbol=ev.symbol,
                            kind=ev.direction,
                            source="ignition",
                            price=ign_price,
                            change_24h_pct=float(tick.get("price_change_percent") or 0),
                            now=now,
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
                if (
                    send_telegram
                    and broadcaster is not None
                    and _advisory_tg_enabled()
                    and IGNITION_TELEGRAM_ENABLED
                ):
                    for ig in pending_ignition_alerts(ignition_state):
                        hist = format_history_telegram(stats_for(pump_store, ig.symbol))
                        msg = format_ignition_telegram(ig)
                        if hist:
                            msg = f"{msg}\n<i>{html.escape(hist)}</i>"
                        result = await broadcaster.send_html(msg)
                        if result.status == "sent":
                            mark_ignition_notified(ignition_state, ig.symbol)
                            save_ignition_state(ignition_state)
                            LOG.info(
                                "hunt_ignition_telegram_sent",
                                symbol=ig.symbol,
                                direction=ig.direction,
                                message_id=result.message_id,
                            )
                        else:
                            LOG.warning(
                                "hunt_ignition_telegram_failed",
                                symbol=ig.symbol,
                                status=result.status,
                                reason=result.reason,
                            )

                if once:
                    merged = list(dict.fromkeys(s.upper() for s in cli_symbols))
                    mode_map = {
                        s: SYMBOL_WATCH_MODES.get(s, "short") for s in merged
                    }
                else:
                    symbols, mode_map = resolve_watch_universe(
                        settings,
                        static_modes=SYMBOL_WATCH_MODES,
                        ignited=ignition_by_sym,
                    )
                    merged = list(symbols)
                    for sym in cli_symbols:
                        s = sym.upper()
                        if s not in merged:
                            merged.append(s)
                        mode_map.setdefault(s, SYMBOL_WATCH_MODES.get(s, "short"))
                    # P1.6 merge: debounced prescan outliers join the ignition path.
                    prescan_merge_cap = int(
                        os.getenv("HUNT_PRESCAN_MERGE_CAP", str(MAX_PRESCAN_MERGE)) or MAX_PRESCAN_MERGE
                    )
                    prescan_to_merge = prescan_ready[: max(prescan_merge_cap, 0)]
                    if len(prescan_ready) > len(prescan_to_merge):
                        LOG.info(
                            "hunt_prescan_merge_capped",
                            ready=len(prescan_ready),
                            merged=len(prescan_to_merge),
                            cap=prescan_merge_cap,
                        )
                    for d in prescan_to_merge:
                        s = d.symbol.upper()
                        if s not in merged:
                            merged.append(s)
                        mode_map.setdefault(
                            s, "short" if d.direction == "pump" else "long"
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
                load_plan = load_planner.plan_tick(
                    active,
                    ignited=set(ignition_by_sym.keys()),
                    interval_s=float(interval_s),
                )
                LOG.info(
                    "hunt_load_plan",
                    symbols=len(active),
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
                    symbols=len(active),
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
                        active, **{k: v for k, v in tick_ctx.items() if k != "active"}
                    )
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
        if tg_cmds is not None:
            await tg_cmds.close()
        await hot_kline_loop.stop()
        try:
            await plane.aclose()
        except Exception:
            LOG.exception("hunt_plane_close_failed")


__all__ = ["run_loop"]
