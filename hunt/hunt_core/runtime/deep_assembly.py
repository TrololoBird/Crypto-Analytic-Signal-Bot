"""Module 1 Deep tick orchestrator — pinned continuous + on-demand query plane."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from hunt_core.data.universe import PINNED_SYMBOLS, save_pinned_cache
from hunt_core.shared.market import HuntCcxtClient
from hunt_core.paths import DEEP_TICKS_JSONL
from hunt_core.runtime.tick_jsonl import serialize_tick_row

LOG = structlog.get_logger("hunt.deep_assembly")

_STALE_HOURS_DEFAULT = 4.0


def deep_pinned_interval_s() -> float:
    return float(os.getenv("HUNT_DEEP_PINNED_INTERVAL", "300") or 300)


def deep_tg_on_change() -> bool:
    return os.getenv("HUNT_DEEP_TG_ON_CHANGE", "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }


def deep_tg_stale_hours() -> float:
    return float(os.getenv("HUNT_DEEP_TG_STALE_HOURS", str(_STALE_HOURS_DEFAULT)) or _STALE_HOURS_DEFAULT)


def stamp_expansion_on_row(row: dict[str, Any]) -> None:
    """Stamp ``row["expansion"]`` with the Expansion Engine opportunity (advisory).

    Independent of Verdict V2 — separate module, separate key, no merged scores. Failures
    never sink the deep tick.
    """
    try:
        from hunt_core.analysis.expansion_engine import (
            build_expansion_opportunity,
            load_expansion_config,
        )

        cfg = load_expansion_config()
        if not cfg.enabled or not cfg.lab_runtime:
            return
        opp = build_expansion_opportunity(row, cfg=cfg)
        row["expansion"] = opp.to_dict()
    except Exception as exc:  # pragma: no cover - advisory layer must never break ticks
        LOG.warning("expansion_stamp_failed", symbol=row.get("symbol"), error=repr(exc))


def append_deep_tick_jsonl(row: dict[str, Any]) -> None:
    from hunt_core.data.jsonl_io import append_jsonl_lines

    DEEP_TICKS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    append_jsonl_lines(DEEP_TICKS_JSONL, [serialize_tick_row(row)])


def deep_change_fingerprint(row: dict[str, Any]) -> str:
    """Hash material pinned deep state for change-only TG policy."""
    v2 = row.get("verdict_v2")
    dec = getattr(v2, "signal_decision", None) if v2 else None
    plan = getattr(dec, "trade_plan", None) if dec else None
    cat = getattr(v2, "catalyst", None) if v2 else None
    path = getattr(v2, "expected_path", None) if v2 else None

    action = str(getattr(dec, "action", "") or "")
    path_type = str(getattr(path, "type", "") or "")
    # Trigger level excluded — it fluctuates with price every tick causing spurious re-fires.
    # Material change = action direction or scenario type changed.
    entry = sl = 0.0
    if plan:
        try:
            # Round aggressively so minor price drift (< 0.5%) doesn't look like a change.
            entry_mid = (plan.entry_zone[0] + plan.entry_zone[1]) / 2
            entry = round(float(entry_mid), 1)
            sl = round(float(plan.stop_loss), 1)
        except (TypeError, ValueError, IndexError):
            pass

    # Legacy fallback when verdict_v2 absent
    if not action:
        pv = row.get("pinned_verdict")
        action = str(getattr(pv, "kind", "") or "")

    return json.dumps(
        {
            "action": action,
            "path": path_type,
            "entry": entry,
            "sl": sl,
        },
        sort_keys=True,
    )


def material_deep_change(
    symbol: str,
    row: dict[str, Any],
    *,
    prev: dict[str, Any] | None,
    now: datetime | None = None,
) -> bool:
    """True when verdict/phase/key levels changed or stale heartbeat exceeded."""
    if not deep_tg_on_change():
        return True
    if prev is None:
        return True
    if deep_change_fingerprint(row) != deep_change_fingerprint(prev):
        return True
    now = now or datetime.now(UTC)
    ts = row.get("ts") or prev.get("ts")
    try:
        dt = datetime.fromisoformat(str(ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        age_h = (now - dt).total_seconds() / 3600.0
        return age_h >= deep_tg_stale_hours()
    except (TypeError, ValueError):
        return False


async def assemble_deep_tick(
    symbol: str,
    client: HuntCcxtClient,
    *,
    stagger_ms: int = 200,
) -> dict[str, Any]:
    """Full deep snapshot — no hunt fusion, structure-first enrichments."""
    import asyncio

    from hunt_core.deep.build import _enrich_deep_row
    from hunt_core.domain.config import load_settings
    from hunt_core.features.prepare import _prepare_frame
    from hunt_core.features.prepare import min_required_bars
    from hunt_core.runtime.tick_assembly import snapshot_symbol
    from hunt_core.data.collect import safe_fetch

    sym = str(symbol or "").upper()
    settings = load_settings()
    minimums = min_required_bars(
        min_bars_15m=settings.filters.min_bars_15m,
        min_bars_1h=settings.filters.min_bars_1h,
        min_bars_4h=settings.filters.min_bars_4h,
    )
    owned_plane = None
    if client is None:
        from hunt_core.shared.market import create_hunt_market_plane_from_settings

        owned_plane = await create_hunt_market_plane_from_settings(settings)
        client = owned_plane.client
    if not getattr(client, "_markets_loaded", False):
        await client.load_markets()

    premium_all = await safe_fetch(client.fetch_premium_index_all(), context="premium_index_all") or {}
    await asyncio.sleep(stagger_ms / 1000.0)
    funding_info_all = await safe_fetch(client.fetch_funding_info_all(), context="funding_info_all") or {}
    await asyncio.sleep(stagger_ms / 1000.0)
    exchange_list = await safe_fetch(client.fetch_exchange_symbols(), context="exchange_symbols") or []
    exchange_by_sym = {r.symbol: r for r in exchange_list}
    await asyncio.sleep(stagger_ms / 1000.0)
    ticker_raw = await safe_fetch(client.fetch_ticker_24h(), context="ticker_24h") or []
    ticker_by_sym = {str(t.get("symbol")): t for t in ticker_raw if t.get("symbol")}

    btc_work_1h = None
    btc_work_1m = None
    btc_df = await safe_fetch(client.fetch_klines_cached("BTCUSDT", "1h", limit=500), context="btc_klines_1h")
    if btc_df is not None and not btc_df.is_empty():
        btc_work_1h = _prepare_frame(btc_df)
    btc_1m = await safe_fetch(client.fetch_klines_cached("BTCUSDT", "1m", limit=999), context="btc_klines_1m")
    if btc_1m is not None and not btc_1m.is_empty():
        btc_work_1m = _prepare_frame(btc_1m)

    old_full = os.environ.get("HUNT_FULL_PREPARE")
    os.environ["HUNT_FULL_PREPARE"] = "1"
    try:
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
            btc_work_1m=btc_work_1m,
            exchange_by_sym=exchange_by_sym,
            ticker_by_sym=ticker_by_sym,
            ws_feed=None,
            spot_companion=None,
            stagger_klines_ms=stagger_ms,
            tier="full",
            hunt_fusion=False,
        )
    finally:
        if old_full is None:
            os.environ.pop("HUNT_FULL_PREPARE", None)
        else:
            os.environ["HUNT_FULL_PREPARE"] = old_full

    if row.get("error"):
        if owned_plane is not None:
            await owned_plane.close()
        return row

    if btc_work_1h is not None:
        from hunt_core.deep.signal import btc_market_context

        row["btc_context"] = btc_market_context(btc_work_1h)

    try:
        from hunt_core.features.microstructure import build_microstructure_context
        from hunt_core.deep.signal import resolve_trade_direction

        market = dict(row.get("market") or {})
        market["symbol"] = sym
        ms_by_dir: dict[str, Any] = {}
        for direction in ("long", "short"):
            try:
                ms_by_dir[direction] = build_microstructure_context({**market, "direction": direction})
            except Exception as exc:
                LOG.warning("deep_microstructure_failed", symbol=sym, direction=direction, error=repr(exc))
        if ms_by_dir:
            row["microstructure_by_direction"] = ms_by_dir
            pick = resolve_trade_direction(row)[0]
            row["microstructure"] = ms_by_dir.get(pick) or ms_by_dir.get("long")
    except Exception as exc:
        LOG.warning("deep_microstructure_pack_failed", symbol=sym, error=repr(exc))

    row = _enrich_deep_row(row)
    row["plane"] = "deep"
    row["_deep_analysis"] = True
    row["tick_path"] = "deep_assembly"

    from hunt_core.maps.forecast import stamp_forecasts_on_row

    stamp_forecasts_on_row(row)

    stamp_expansion_on_row(row)

    try:
        save_pinned_cache(sym, row)
    except Exception as exc:
        LOG.warning("deep_pinned_cache_failed", symbol=sym, error=repr(exc))

    from hunt_core.runtime.tick_state import deep_query_store

    deep_query_store().put(sym, row)
    append_deep_tick_jsonl(row)
    try:
        from hunt_core.deep.verdict_v2.calibration import (
            CALIBRATION_JSON,
            merge_live_sample,
            write_calibration_rollup,
        )

        summary = row.get("verdict_v2_summary")
        if isinstance(summary, dict):
            if CALIBRATION_JSON.is_file():
                import json as _json

                report = _json.loads(CALIBRATION_JSON.read_text(encoding="utf-8"))
                report = merge_live_sample(report, summary, sym)
                CALIBRATION_JSON.write_text(
                    _json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            else:
                write_calibration_rollup(limit=200)
    except Exception as exc:
        LOG.debug("verdict_v2_calibration_skip", symbol=sym, error=repr(exc))
    try:
        from hunt_core.deep.verdict_v2.config import load_verdict_v2_config
        from hunt_core.deep.verdict_v2.signal_queue import refresh_pinned_signal_queue

        v2cfg = load_verdict_v2_config()
        if getattr(v2cfg, "signal_queue_enabled", True):
            row["signal_queue"] = refresh_pinned_signal_queue(sym, row, top_n=v2cfg.signal_queue_top_n)
    except Exception as exc:
        LOG.debug("verdict_v2_signal_queue_skip", symbol=sym, error=repr(exc))
    if owned_plane is not None:
        await owned_plane.close()
    return row


async def send_deep_change_telegram(
    broadcaster: Any,
    row: dict[str, Any],
    *,
    cycle_peers: list[dict[str, Any]] | None = None,
) -> bool:
    """Send deep analysis TG when material change detected."""
    from hunt_core.analysis.confluence_grid import build_confluence_grid, format_grid_telegram
    from hunt_core.deep.format_pinned_signal import format_pinned_signal
    from hunt_core.deliver._sections import format_intraday_maps_telegram

    sym = str(row.get("symbol") or "").upper()
    if row.get("error"):
        return False
    blocks: list[str] = []
    pinned_block = format_pinned_signal(row)
    if pinned_block:
        blocks.append(pinned_block)
    else:
        from hunt_core.deep.build import build_deep_report
        from hunt_core.deep.format_telegram import format_deep_analysis_telegram

        analysis = build_deep_report(row, include_watch_appendix=False)
        blocks.append(format_deep_analysis_telegram(analysis))
    grid = build_confluence_grid(row)
    if grid:
        blocks.extend(["", format_grid_telegram(grid)])
    maps_block = format_intraday_maps_telegram(row)
    if maps_block:
        blocks.extend(["", maps_block])
    from hunt_core.deep.verdict_v2.config import load_verdict_v2_config
    from hunt_core.deep.verdict_v2.delivery_policy import format_cycle_peers_footer
    from hunt_core.deep.verdict_v2.signal_queue import format_queue_telegram

    v2cfg = load_verdict_v2_config()
    if cycle_peers:
        peer_block = format_cycle_peers_footer(row, cycle_peers)
        if peer_block:
            blocks.extend(["", peer_block])
    if v2cfg.signal_queue_tg_footer:
        qblock = format_queue_telegram(row.get("signal_queue"))
        if qblock:
            blocks.extend(["", qblock])
    result = await broadcaster.send_html("\n".join(blocks), no_split=True)
    if result.status == "sent":
        LOG.info("deep_pinned_tg_sent", symbol=sym, message_id=result.message_id, plane="deep")
        return True
    LOG.warning("deep_pinned_tg_failed", symbol=sym, status=result.status, reason=result.reason)
    return False


async def deep_pinned_loop(
    client: HuntCcxtClient,
    broadcaster: Any | None,
    *,
    interval_s: float | None = None,
    send_telegram: bool = True,
) -> None:
    """Background continuous deep analysis for pinned anchors."""
    from hunt_core.runtime.state import should_stop
    from hunt_core.runtime.tick_state import deep_query_store

    import asyncio

    interval = interval_s if interval_s is not None else deep_pinned_interval_s()
    LOG.info("deep_pinned_loop_start", symbols=list(PINNED_SYMBOLS), interval_s=interval)
    while not should_stop():
        from hunt_core.deep.verdict_v2.config import load_verdict_v2_config
        from hunt_core.deep.verdict_v2.delivery_policy import (
            filter_notify_candidates,
            pick_hero_row,
            should_send_pinned_batch,
        )
        from hunt_core.deep.verdict_v2.signal_queue import load_signal_queue

        v2cfg = load_verdict_v2_config()
        prev_queue = load_signal_queue()
        cycle_changes: list[dict[str, Any]] = []
        for sym in PINNED_SYMBOLS:
            if should_stop():
                break
            try:
                prev = deep_query_store().get(sym)
                row = await assemble_deep_tick(sym, client)
                if row.get("error"):
                    LOG.warning("deep_pinned_tick_error", symbol=sym, error=row.get("error"))
                    continue
                if material_deep_change(sym, row, prev=prev):
                    cycle_changes.append(row)
                from hunt_core.analysis.expansion_engine.config import load_expansion_config
                from hunt_core.runtime.expansion_alerts import (
                    expansion_change_fingerprint,
                    expansion_cooldown_ok,
                    mark_expansion_alert_sent,
                    material_expansion_change,
                    send_expansion_change_telegram,
                )

                exp_cfg = load_expansion_config()
                if (
                    exp_cfg.lab_runtime
                    and exp_cfg.tg_pinned_alerts
                    and send_telegram
                    and broadcaster is not None
                    and material_expansion_change(sym, row, prev=prev, cfg=exp_cfg)
                    and expansion_cooldown_ok(sym, exp_cfg)
                ):
                    if await send_expansion_change_telegram(broadcaster, row):
                        exp_dict = row.get("expansion") if isinstance(row.get("expansion"), dict) else {}
                        mark_expansion_alert_sent(
                            sym,
                            fingerprint=expansion_change_fingerprint(exp_dict) if exp_dict else None,
                        )
                        try:
                            from hunt_core.analysis.expansion_engine import build_expansion_opportunity
                            from hunt_core.analysis.expansion_engine.learning import (
                                record_expansion_signal,
                            )

                            opp = build_expansion_opportunity(row, cfg=exp_cfg)
                            if opp.dominant != "neutral":
                                record_expansion_signal(opp, ts=str(row.get("ts") or ""))
                        except Exception:
                            LOG.debug("expansion_alert_record_failed", symbol=sym, exc_info=True)
            except Exception:
                LOG.exception("deep_pinned_loop_symbol_failed", symbol=sym)

        if send_telegram and broadcaster is not None and cycle_changes:
            queue = load_signal_queue()
            filtered = filter_notify_candidates(
                cycle_changes,
                queue,
                min_rank=v2cfg.signal_queue_tg_min_rank,
            )
            if filtered:
                if v2cfg.signal_queue_tg_batch:
                    hero = pick_hero_row(filtered, queue)
                    if hero:
                        hero_sym = str(hero.get("symbol") or "").upper()
                        hero_prev = deep_query_store().get(hero_sym)
                        from hunt_core.deep.arbiter import deep_cooldown_ok, mark_deep_sent
                        cooldown_h = deep_tg_stale_hours() / 8.0  # min 30min between re-fires
                        if should_send_pinned_batch(
                            hero,
                            queue=queue,
                            prev_queue=prev_queue,
                            hero_prev=hero_prev,
                            fingerprint_fn=deep_change_fingerprint,
                        ) and deep_cooldown_ok(hero_sym, hours=max(0.5, cooldown_h)):
                            if await send_deep_change_telegram(
                                broadcaster,
                                hero,
                                cycle_peers=filtered,
                            ):
                                mark_deep_sent(hero_sym)
                else:
                    from hunt_core.deep.arbiter import deep_cooldown_ok, mark_deep_sent
                    for row in filtered:
                        sym = str(row.get("symbol") or "").upper()
                        prev = deep_query_store().get(sym)
                        if material_deep_change(sym, row, prev=prev) and deep_cooldown_ok(sym, hours=0.75):
                            if await send_deep_change_telegram(broadcaster, row):
                                mark_deep_sent(sym)
        try:
            await asyncio.sleep(max(30.0, interval))
        except asyncio.CancelledError:
            break
    LOG.info("deep_pinned_loop_stop")


def expansion_review_interval_s() -> float:
    from hunt_core.analysis.expansion_engine.config import load_expansion_config

    return load_expansion_config().review_interval_s


async def expansion_outcome_review_loop(
    client: HuntCcxtClient,
    *,
    interval_s: float | None = None,
) -> None:
    """Background task — grade expansion outcome ledger at 24h/48h/72h/7d."""
    from hunt_core.analysis.expansion_engine.config import load_expansion_config
    from hunt_core.analysis.expansion_engine.learning.review import review_expansion_outcomes
    from hunt_core.runtime.state import should_stop

    import asyncio

    cfg = load_expansion_config()
    if not cfg.enabled or not cfg.review_loop:
        LOG.info("expansion_review_loop_disabled")
        return

    interval = interval_s if interval_s is not None else cfg.review_interval_s
    LOG.info("expansion_review_loop_start", interval_s=interval)
    while not should_stop():
        try:
            summary = await review_expansion_outcomes(client)
            if summary.get("graded", 0) > 0:
                LOG.info("expansion_outcomes_graded", **summary)
        except Exception:
            LOG.exception("expansion_review_loop_failed")
        try:
            await asyncio.sleep(max(300.0, interval))
        except asyncio.CancelledError:
            break
    LOG.info("expansion_review_loop_stop")


__all__ = [
    "append_deep_tick_jsonl",
    "assemble_deep_tick",
    "deep_change_fingerprint",
    "deep_pinned_interval_s",
    "expansion_outcome_review_loop",
    "expansion_review_interval_s",
    "deep_pinned_loop",
    "material_deep_change",
    "send_deep_change_telegram",
    "stamp_expansion_on_row",
]
