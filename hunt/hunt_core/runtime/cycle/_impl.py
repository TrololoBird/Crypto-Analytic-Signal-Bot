"""Hunter per-tick cycle — run_loop / run_tick (H-B rewrite)."""
from __future__ import annotations



import asyncio
import faulthandler
import html
import json
import os
import time
from datetime import datetime, timedelta
from typing import Any

from hunt_core.scan.predump_dump_hunt import (
    dump_hunt_skip_reason,
    format_dump_hunt_telegram,
    maybe_send_dump_hunt_telegram,
)
from hunt_core.scan.early import (
    early_cooldown_ok,
    early_telegram_enabled,
    evaluate_early_alert,
    format_early_telegram,
    format_ignition_telegram,
    format_liquidation_burst_advisory,
    liquidation_burst_from_streams,
    load_adaptive_store,
    load_ignition_state,
    mark_early_sent,
    mark_ignition_notified,
    pending_ignition_alerts,
    process_ticker_snapshots,
    save_adaptive_store,
    save_ignition_state,
)
from hunt_core.features.prepare_columns import book_walls_from_row, feature_vector_from_row
from hunt_core.regime.leg_fsm import promote_initial_pump_lifecycle, record_delivery_fsm
from hunt_core.domain.market_regime import (
    REGIME_REFRESH_S,
    active_params,
    apply_snapshot,
    load_regime_file,
    refresh_market_regime,
)
from hunt_core.params.store import effective_hunt_params, migrate_calibration_split
from hunt_core.track.prep_shadow import (
    load_prep_shadow_state,
    process_prep_shadow,
    save_prep_shadow_state,
)
from hunt_core.track.candidates import (
    load_setup_candidates_state,
    process_setup_candidate,
    promote_to_confirm,
    save_setup_candidates_state,
)
from hunt_core.track.pump_history import (
    backfill_from_jsonl,
    format_history_telegram,
    load_pump_history,
    observe_prices,
    record_pump_leg,
    record_signal_outcome,
    save_pump_history,
    stats_for,
)
from hunt_core.track.pump_history import record_signal_open as record_pump_signal_open
from hunt_core.scan.prepump import phase_long as _se_phase_long
from hunt_core.scan.routing import route_tick
from hunt_core.scan.routing import resolve_delivery_mode
from hunt_core.track.events import append_signal_event, record_funnel_stage, record_lifecycle_funnel
from hunt_core.track.tracker import (
    evaluate_followups,
    latch_row_setups,
    load_tracker_state,
    mark_close_notified,
    mark_followups_sent,
    reconcile_signal,
    register_signal_open,
)
from hunt_core.data.universe import PINNED_SYMBOLS, effective_watch_mode, resolve_watch_universe, MAX_PRESCAN_MERGE
from hunt_core.data.universe import save_pinned_cache
from hunt_core.runtime.telegram_commands import build_hunt_telegram_commands
from hunt_core.runtime.tick_io import rotate_hunt_ticks
from hunt_core.data.universe import clear_signal_notify, load_pending_notify
from hunt_core.domain.config import load_settings
from hunt_core.errors import DEFENSIVE_EXC, defensive_exc_types
from hunt_core.features.prepare import _prepare_frame, min_required_bars
from hunt_core.deliver.telegram import TelegramBroadcaster

from hunt_core.market.live_price import apply_live_price_to_row
from hunt_core.market import HuntCcxtClient, HuntCcxtSpotCompanion, HuntCcxtStreams
from hunt_core.market.cross import (
    CrossExchangeConfig,
    attach_cross_fields,
    load_cross_exchange_config,
    merge_ws_cross_into_snapshot,
    refresh_cross_exchange_cache,
    apply_cross_exchange_env,
    fetch_secondary_ticker_overlay,
)
from hunt_core.market.factory import create_hunt_market_plane_from_settings
from hunt_core.market.capacity import HuntLoadPlanner

from hunt_core.data.collect import safe_fetch
from hunt_core.runtime.tick_assembly import snapshot_symbol
from hunt_core.data.scanner import (
    PrescanDebounceQueue,
    PrescanEngine,
    apply_quality_gates,
    prescan_from_tickers,
)
from hunt_core.data.collect import (
    SnapshotTier,
    TickBatchCache,
    refresh_tick_batch_cache,
    sort_symbols_for_tick,
)
from hunt_core.data.lake import FeatureLakeWriter
from hunt_core.data.lake import (
    buffer_cooldown_state,
    buffer_tick_rows,
    buffer_tracker_state,
    flush_lake,
)
from hunt_core.features.feature_engine import FeatureExtractError, build_feature_vector
from hunt_core.deliver.digest import (
    DigestCandidate,
    advisory_digest_enabled,
    get_advisory_digest,
    get_digest_scheduler,
)
from hunt_core import clock
from hunt_core.deliver.dispatch import (
    effective_top_ls,
    evaluate_forming_gate,
    mark_unified_sent,
    unified_cooldown_ok,
)
from hunt_core.gate.delivery import delivery_hard_block
from hunt_core.runtime.state import SymbolStateStore, new_session_state
from hunt_core.runtime.state import (
    LOG,
    OUT_PATH,
    SNIPER_CONFIG,
    STATE_PATH,
    SYMBOL_WATCH_MODES,
    WatchMode,
    should_stop,
)
from hunt_core.domain.config import (
    COOLDOWN_MINUTES,
    IGNITION_MIN_VOL_DELTA_USD,
    IGNITION_TELEGRAM_ENABLED,
    IGNITION_TTL_S,
    IGNITION_WINDOW_S,
    SCAN_INTERVAL_S,
    SQUEEZE_COOLDOWN_MINUTES,
    SQUEEZE_MIN_VOL_24H_M,
    SYMBOL_TICK_TIMEOUT_S,
    TICK_ROTATE_INTERVAL_S,
    TICK_ROTATE_MIN_BYTES,
)


HUNT_SNIPER_MODE = SNIPER_CONFIG.enabled
HUNT_SNIPER_LIVE_PHASES = SNIPER_CONFIG.live_phases
HUNT_SNIPER_TOP_LS_MAX = SNIPER_CONFIG.top_ls_max
HUNT_SNIPER_REQUIRE_TOP_LS = SNIPER_CONFIG.require_top_ls
HUNT_SNIPER_CHASE_TOL = SNIPER_CONFIG.chase_tol
HUNT_SNAPSHOT_PARALLEL = max(1, int(os.getenv("HUNT_SNAPSHOT_PARALLEL", "6")))


def _overlay_ws_tickers(
    ticker_by_sym: dict[str, dict[str, Any]],
    symbols: tuple[str, ...] | list[str],
    ws_feed: HuntCcxtStreams | None,
) -> None:
    """Prefer WS last over batch REST ticker for snapshot price seed."""
    if ws_feed is None:
        return
    for sym in symbols:
        lt = ws_feed.live_ticker(sym)
        if not lt:
            continue
        last = float(lt.get("last") or 0)
        if last <= 0:
            continue
        base = dict(ticker_by_sym.get(sym) or {"symbol": sym})
        base["last_price"] = last
        ticker_by_sym[sym] = base


def _refresh_live_price(
    row: dict[str, Any],
    *,
    ws_feed: HuntCcxtStreams | None,
    symbol: str,
) -> float:
    prev = float(row.get("price") or 0)
    px = apply_live_price_to_row(row, ws_feed=ws_feed)
    delta = row.get("price_stale_delta_pct")
    if delta is not None and abs(float(delta)) >= 0.05:
        LOG.info(
            "live_price_refresh",
            symbol=symbol,
            price=px,
            prev=prev,
            delta_pct=delta,
            source=row.get("price_source"),
        )
    return px


def _advisory_tg_enabled() -> bool:
    """Advisory TG (squeeze/ignition/dump_hunt) off by default — log-only until edge proven."""
    return os.environ.get("HUNT_ADVISORY_TG", "0").strip().lower() in {"1", "true", "yes"}


def _confirm_blocked_bias_wait(
    *,
    direction: str,
    lifecycle: Any | None,
) -> bool:
    """Block confirm TG when dump_active short has bias=wait (VELVET lesson)."""
    if direction != "short" or not isinstance(lifecycle, dict):
        return False
    return (
        str(lifecycle.get("phase") or "") == "dump_active"
        and str(lifecycle.get("recommended_bias") or "") == "wait"
    )


def _phase_long(long_setup: dict[str, Any], confirmed: bool, *, symbol: str = "") -> str:
    return _se_phase_long(long_setup, confirmed, cal=effective_hunt_params(symbol))


def _load_state() -> dict[str, str]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_state(state: dict[str, str]) -> None:
    buffer_cooldown_state(state, STATE_PATH)


async def _maybe_send_liq_burst_advisory(
    broadcaster: Any,
    *,
    symbol: str,
    ws_feed: HuntCcxtStreams | None,
    state: dict[str, str],
    now: datetime,
    send_telegram: bool,
) -> bool:
    """P1.9: liquidation cascade advisory — optional TG, never confirm."""
    if not send_telegram or broadcaster is None or ws_feed is None:
        return False
    if os.getenv("HUNT_LIQ_BURST_TG", "0").strip().lower() not in {"1", "true", "yes"}:
        return False
    burst = liquidation_burst_from_streams(ws_feed, symbol)
    if burst is None:
        return False
    trade_dir = "short" if burst.direction == "dump" else "long"
    if not unified_cooldown_ok(
        state,
        symbol=symbol,
        direction=trade_dir,
        stage="early",
        now=now,
    ):
        return False
    liq_key = f"{symbol}:liq_burst"
    raw = state.get(liq_key)
    if raw:
        try:
            if now - datetime.fromisoformat(str(raw)) < timedelta(minutes=30):
                return False
        except ValueError:
            pass
    msg = format_liquidation_burst_advisory(burst)
    result = await broadcaster.send_html(msg)
    if result.status != "sent":
        LOG.warning(
            "hunt_liq_burst_telegram_failed",
            symbol=symbol,
            direction=burst.direction,
            status=result.status,
        )
        return False
    state[liq_key] = now.isoformat()
    mark_unified_sent(
        state,
        symbol=symbol,
        direction=trade_dir,
        stage="early",
        now=now,
    )
    append_signal_event(
        "liq_burst_advisory",
        symbol=symbol,
        direction=trade_dir,
        detail=burst.direction,
        payload={
            "notional_usd": burst.total_notional_usd,
            "events": burst.events,
            "score": burst.score,
        },
    )
    LOG.info(
        "hunt_liq_burst_telegram_sent",
        symbol=symbol,
        direction=burst.direction,
        notional=burst.total_notional_usd,
        events=burst.events,
    )
    return True


async def _maybe_send_early_alert(
    broadcaster: Any,
    *,
    symbol: str,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    lifecycle_raw: Any,
    state: dict[str, str],
    mode: str,
    now: datetime,
) -> bool:
    """Prep/start Telegram before full closed-bar confirm."""
    if not early_telegram_enabled(symbol):
        return False
    early = evaluate_early_alert(
        setup,
        direction=direction,
        symbol=symbol,
        lifecycle=lifecycle_raw,
        row=row,
    )
    if early.kind in ("none", "confirm"):
        return False
    lc_phase = str((lifecycle_raw or {}).get("phase") or "")
    if (
        direction == "short"
        and mode not in ("short", "both")
        and lc_phase
        not in ("dump_active", "exhaustion_at_high", "distribution", "dump_initiating")
    ):
        return False
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
        return False
    if not early_cooldown_ok(symbol, direction, early.tier, state, now=now):
        return False
    if not unified_cooldown_ok(
        state, symbol=symbol, direction=direction, stage="early", now=now
    ):
        return False
    msg = format_early_telegram(
        row,
        direction=direction,
        setup=setup,
        lifecycle=lifecycle_raw,
        alert=early,
    )
    result = await broadcaster.send_html(msg)
    if result.status != "sent":
        LOG.warning(
            "watch_early_telegram_failed",
            symbol=symbol,
            direction=direction,
            tier=early.tier,
            status=result.status,
            reason=result.reason,
        )
        return False
    mark_early_sent(symbol, direction, early.tier, state, now=now)
    mark_unified_sent(state, symbol=symbol, direction=direction, stage="early", now=now)
    event_kind = {"prep": "prep", "imminent": "imminent", "start": "start"}.get(
        early.tier, "forming_early"
    )
    LOG.info(
        "watch_early_telegram_sent",
        symbol=symbol,
        direction=direction,
        tier=early.tier,
        message_id=result.message_id,
    )
    append_signal_event(
        event_kind,
        symbol=symbol,
        direction=direction,
        detail=early.message,
        payload={
            "tier": early.tier,
            "message_id": result.message_id,
            "fuel": setup.get("dump_fuel") or setup.get("long_fuel"),
            "phase": setup.get("phase"),
            "lifecycle_phase": lc_phase,
        },
    )
    return True


def _cooldown_ok(
    symbol: str,
    direction: str,
    state: dict[str, str],
    *,
    now: datetime,
    minutes: int = COOLDOWN_MINUTES,
) -> bool:
    key = f"{symbol}:{direction}"
    raw = state.get(key) or state.get(symbol)
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return True
    return now - last >= timedelta(minutes=minutes)


def _entry_past_tp1(setup: dict[str, Any], *, direction: str, price: float) -> bool:
    """Reject TG when price already at/through TP1 (hard stale only)."""
    return (
        delivery_hard_block(
            direction=direction,
            setup=setup,
            row={"price": price},
        )
        is not None
    )


def _fmt_price(value: float | None) -> str:
    if value is None:
        return "—"
    v = float(value)
    if abs(v) >= 100:
        return f"{v:.3f}"
    if abs(v) >= 1:
        return f"{v:.4f}"
    if abs(v) >= 0.01:
        return f"{v:.5f}"
    return f"{v:.6f}"


def _phase_badge(phase: str, confirmed: bool, *, direction: str = "short") -> str:
    if confirmed:
        return "🚨"
    if direction == "long":
        return {
            "long_imminent": "🟢",
            "long_setup_forming": "🟡",
            "long_confirmed": "🚨",
            "accumulation_watch": "🔵",
            "no_long_yet": "⚪",
        }.get(phase, "⚪")
    return {
        "dump_imminent": "🔴",
        "dump_setup_forming": "🟠",
        "dump_confirmed": "🚨",
        "exhaustion_watch": "🟡",
        "no_dump_yet": "⚪",
    }.get(phase, "⚪")


def _format_setup_lines(
    row: dict[str, Any],
    setup: dict[str, Any],
    *,
    direction: str,
    tf: dict[str, Any],
    pos: dict[str, Any],
    price: float,
    suppress_context: bool = False,
) -> list[str]:
    score_key = "dump_score" if direction == "short" else "long_score"
    phase = str(setup.get("phase") or "—")
    confirmed = bool(setup.get("confirmed"))
    badge = _phase_badge(phase, confirmed, direction=direction)

    def _opt_num(val: Any, *, digits: int = 4) -> str:
        if val is None:
            return "—"
        try:
            return f"{float(val):.{digits}f}"
        except (TypeError, ValueError):
            return "—"

    def _fmt_oi(val: Any, px: float) -> str:
        if val is None:
            return "—"
        try:
            contracts = float(val)
        except (TypeError, ValueError):
            return "—"
        if contracts <= 0:
            return "—"
        if px > 0 and contracts < px * 100:
            notional = contracts * px
            if notional >= 1_000_000_000:
                return f"${notional / 1_000_000_000:.2f}B"
            if notional >= 1_000_000:
                return f"${notional / 1_000_000:.1f}M"
            return f"${notional:,.0f}"
        if contracts >= 1_000_000_000:
            return f"${contracts / 1_000_000_000:.2f}B"
        if contracts >= 1_000_000:
            return f"${contracts / 1_000_000:.1f}M"
        return _fmt_price(contracts)

    from hunt_core.deliver.dispatch import readiness_label_for_setup
    score_val = setup.get(score_key)
    readiness_line = readiness_label_for_setup(
        setup, direction=direction, row=row
    )
    score_str = f"{float(score_val):.0f}" if score_val is not None else "—"
    dir_label = "SHORT" if direction == "short" else "LONG"

    def _rsi(key: str) -> str:
        val = (tf.get(key) or {}).get("rsi14")
        return "—" if val is None else f"{val:.0f}"

    div_bits: list[str] = []
    if direction == "short":
        if (tf.get("1h") or {}).get("bearish_rsi_div"):
            div_bits.append("bear1h✓")
        if (tf.get("4h") or {}).get("bearish_rsi_div"):
            div_bits.append("bear4h✓")
    else:
        if (tf.get("1h") or {}).get("bullish_rsi_div"):
            div_bits.append("bull1h✓")
        if (tf.get("4h") or {}).get("bullish_rsi_div"):
            div_bits.append("bull4h✓")
    div_txt = " · " + " ".join(div_bits) if div_bits else ""

    triggers = setup.get("triggers") or []
    headwinds = [t for t in triggers if str(t).startswith("headwind_")]
    tailwinds = [t for t in triggers if not str(t).startswith("headwind_")]
    trig_txt = html.escape(", ".join(str(t) for t in tailwinds[:5]))
    if len(tailwinds) > 5:
        trig_txt += "…"
    headwind_txt = html.escape(", ".join(str(t) for t in headwinds[:3])) if headwinds else ""

    ez = setup.get("entry_zone") or [price, price]

    oi = pos.get("oi")
    oi_chg = pos.get("oi_chg_5m")
    fund = pos.get("funding_pct")
    taker = pos.get("taker_5m")
    ls = pos.get("ls_5m")

    if direction == "short":
        fib1272 = setup.get("fib_1272") or setup.get("resistance_liq")
        level_line = (
            f"Support <code>{_fmt_price(setup.get('support_break_level'))}</code> · "
            f"fib1272 <code>{_fmt_price(fib1272)}</code> · impulse H "
            f"<code>{_fmt_price(row.get('impulse_high'))}</code>"
        )
    else:
        level_line = (
            f"Resistance <code>{_fmt_price(setup.get('resistance_break_level'))}</code> · support "
            f"<code>{_fmt_price(setup.get('support_zone'))}</code> · impulse L "
            f"<code>{_fmt_price(row.get('impulse_low'))}</code>"
        )

    lines = [
        f"{badge} <b>{dir_label}</b> · <code>{phase}</code> · "
        f"{readiness_line} · score триггеров <code>{score_str}</code>",
        level_line,
        (
            f"Entry <code>{_fmt_price(ez[0])}-{_fmt_price(ez[1])}</code> · "
            f"SL <code>{_fmt_price(setup.get('stop_loss'))}</code> · "
            f"TP1 <code>{_fmt_price(setup.get('tp1'))}</code> · "
            f"TP2 <code>{_fmt_price(setup.get('tp2'))}</code>"
            + (
                f" · R:R <code>{setup.get('risk_reward')}</code>"
                if setup.get("risk_reward")
                else ""
            )
        ),
        (
            f"RSI 1m/5m/15m/1h/4h: "
            f"<code>{_rsi('1m')}/{_rsi('5m')}/{_rsi('15m')}/{_rsi('1h')}/{_rsi('4h')}</code>"
            f"{div_txt}"
        ),
        (
            f"OI <code>{_fmt_oi(oi, price)}</code> · "
            f"Δ5m <code>{_opt_num(oi_chg)}</code> · "
            f"fund <code>{_opt_num(fund, digits=3)}%</code> · "
            f"taker5m <code>{_opt_num(taker)}</code> · "
            f"L/S <code>{_opt_num(ls)}</code>"
        ),
        f"Triggers: <code>{trig_txt or '—'}</code>",
    ]
    if headwind_txt:
        lines.append(f"Headwinds: <code>{headwind_txt}</code>")
    regime = row.get("regime") or {}
    poc1h = regime.get("poc_1h")
    vah1h = regime.get("vah_1h")
    val1h = regime.get("val_1h")
    # Pinned deep-analysis already renders the volume profile (cross-exchange merged) —
    # skip the Binance-only copy here to avoid a duplicate with mismatched numbers.
    if poc1h is not None and not suppress_context:
        lines.append(
            f"Volume profile 1h: POC <code>{_fmt_price(float(poc1h))}</code>"
            + (f" · VAH <code>{_fmt_price(float(vah1h))}</code>" if vah1h else "")
            + (f" · VAL <code>{_fmt_price(float(val1h))}</code>" if val1h else "")
        )
    if confirmed:
        hard = setup.get("confirm_hard") or []
        lines.append(f"<b>✅ CONFIRM</b> {html.escape(', '.join(str(x) for x in hard))}")
    return lines


_PHASE_HUMAN: dict[str, str] = {
    "dump_active": "Активный дамп",
    "dump_initiating": "Начало дампа",
    "dump_imminent": "Дамп неизбежен",
    "dump_setup_forming": "Формируется шорт",
    "dump_confirmed": "Шорт подтверждён",
    "exhaustion_at_high": "Истощение на хаях",
    "exhaustion_watch": "Наблюдение за истощением",
    "distribution": "Распределение",
    "impulse_initiating": "Начало импульса",
    "breakout_arming": "Вооружение пробоя",
    "post_dump_bounce": "Отскок после дампа",
    "accumulation": "Накопление",
    "accumulation_watch": "Наблюдение за накоплением",
    "long_imminent": "Лонг неизбежен",
    "long_setup_forming": "Формируется лонг",
    "long_confirmed": "Лонг подтверждён",
    "no_setup": "Нет сетапа",
    "no_dump_yet": "Нет дампа",
    "no_long_yet": "Нет лонга",
}


def _phase_human(phase: str) -> str:
    return _PHASE_HUMAN.get(phase, phase)


def _pct_str(a: float, b: float, direction: str) -> str:
    """Percentage distance from entry to target."""
    if a <= 0 or b <= 0:
        return ""
    if direction == "short":
        pct = (a - b) / a * 100.0
    else:
        pct = (b - a) / a * 100.0
    return f"+{pct:.1f}%"


def _reason_human(setup: dict[str, Any], *, direction: str, lc_phase: str) -> str:
    """Build human-readable reason line from phase + triggers + fuel."""
    phase_txt = _phase_human(lc_phase) if lc_phase and lc_phase != "—" else _phase_human(
        str(setup.get("phase") or "")
    )
    triggers = setup.get("triggers") or []
    trig_short: list[str] = []
    for t in triggers[:3]:
        ts = str(t)
        if "volume" in ts or "vol" in ts:
            trig_short.append("аномальный объём")
        elif "support" in ts or "break" in ts:
            trig_short.append("пробой поддержки")
        elif "resistance" in ts:
            trig_short.append("пробой сопротивления")
        elif "cascade" in ts or "liq" in ts:
            trig_short.append("каскад ликвидаций")
        elif "rejection" in ts:
            trig_short.append("отбой от уровня")
        elif "rsi" in ts or "div" in ts:
            trig_short.append("RSI-дивергенция")
        elif "funding" in ts:
            trig_short.append("перегрев фандинга")
        elif "oi" in ts:
            trig_short.append("аномалия OI")
        elif "whale" in ts:
            trig_short.append("крупный продавец")
        else:
            trig_short.append(ts.replace("_", " ").split(":")[0])
    trig_txt = ", ".join(dict.fromkeys(trig_short))  # deduplicate, keep order
    if phase_txt and trig_txt:
        return f"{phase_txt} · {trig_txt}"
    return phase_txt or trig_txt or "—"


# Orphan signals (symbol no longer in watchlist) are re-checked via REST klines.
ORPHAN_RECONCILE_MINUTES = 5
INWATCH_KLINE_RECONCILE_SECONDS = 45


async def _reconcile_inwatch_active(
    client: HuntCcxtClient,
    tracker_state: dict[str, Any],
    *,
    symbol: str,
    now: datetime,
) -> list[Any]:
    """5m kline hi/lo since last_checked_at for active signals still in the watchlist."""
    events: list[Any] = []
    signals = tracker_state.get("signals") or {}
    sym_u = symbol.upper()
    for key, sig in list(signals.items()):
        if not isinstance(sig, dict) or sig.get("status") != "active":
            continue
        o_sym, _, o_dir = key.partition(":")
        if o_sym != sym_u:
            continue
        anchor_raw = sig.get("last_checked_at") or sig.get("opened_at")
        try:
            anchor = datetime.fromisoformat(str(anchor_raw))
        except (TypeError, ValueError):
            anchor = now
        if (now - anchor).total_seconds() < INWATCH_KLINE_RECONCILE_SECONDS:
            continue
        df = await safe_fetch(
            lambda: client.fetch_klines_between(
                o_sym,
                "5m",
                start_time_ms=int(anchor.timestamp() * 1000),
                end_time_ms=int(now.timestamp() * 1000),
            ),
            context="inwatch_klines",
            client=client,
        )
        if df is None or df.is_empty():
            sig["last_checked_at"] = now.isoformat()
            continue
        hi = float(df["high"].max())
        lo = float(df["low"].min())
        last_price = float(df["close"][-1])
        events.extend(
            reconcile_signal(
                tracker_state,
                symbol=o_sym,
                direction=o_dir,
                hi=hi,
                lo=lo,
                last_price=last_price,
                ts=now,
            )
        )
    return events


async def _reconcile_orphan_signals(
    client: HuntCcxtClient,
    tracker_state: dict[str, Any],
    *,
    seen_symbols: set[str],
    now: datetime,
) -> list[Any]:
    events: list[Any] = []
    signals = tracker_state.get("signals") or {}
    for key, sig in list(signals.items()):
        if not isinstance(sig, dict) or sig.get("status") != "active":
            continue
        o_sym, _, o_dir = key.partition(":")
        if not o_sym or not o_dir or o_sym in seen_symbols:
            continue
        anchor_raw = sig.get("last_checked_at") or sig.get("opened_at")
        try:
            anchor = datetime.fromisoformat(str(anchor_raw))
        except (TypeError, ValueError):
            anchor = now
        if (now - anchor).total_seconds() < ORPHAN_RECONCILE_MINUTES * 60:
            continue
        df = await safe_fetch(
            lambda: client.fetch_klines_between(
                o_sym,
                "5m",
                start_time_ms=int(anchor.timestamp() * 1000),
                end_time_ms=int(now.timestamp() * 1000),
            ),
            context="orphan_klines",
            client=client,
        )
        if df is None or df.is_empty():
            sig["last_checked_at"] = now.isoformat()
            continue
        hi = float(df["high"].max())
        lo = float(df["low"].min())
        last_price = float(df["close"][-1])
        events.extend(
            reconcile_signal(
                tracker_state,
                symbol=o_sym,
                direction=o_dir,
                hi=hi,
                lo=lo,
                last_price=last_price,
                ts=now,
            )
        )
    return events


def _duration_str(opened: str) -> str:
    """Human-readable duration from ISO opened_at to now."""
    try:
        from datetime import UTC, datetime
        start = datetime.fromisoformat(opened.replace(" ", "T"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        delta = clock.now_utc() - start
        total_m = int(delta.total_seconds() // 60)
        h, m = divmod(total_m, 60)
        if h > 0:
            return f"{h}ч {m}м"
        return f"{m}м"
    except Exception:
        return "—"


async def _deliver_followup(
    broadcaster: Any,
    fu: Any,
    row: dict[str, Any],
    tracker_state: dict[str, Any],
    *,
    now: datetime,
    send_telegram: bool,
) -> bool:
    """Send one follow-up; mark + persist immediately on success."""
    announced = bool((fu.payload or {}).get("announced", True))
    if not send_telegram or broadcaster is None or not announced:
        return False
    from hunt_core.deliver.templates import format_followup_telegram_message

    msg = format_followup_telegram_message(fu, row)
    result = await broadcaster.send_html(msg)
    if result.status != "sent":
        LOG.warning(
            "watch_followup_send_failed",
            symbol=fu.symbol,
            followup_event=fu.event,
            status=result.status,
            reason=result.reason,
        )
        return False
    mark_followups_sent(tracker_state, [fu], now=now)
    if fu.event == "invalidate":
        mark_close_notified(
            tracker_state,
            symbol=fu.symbol,
            direction=fu.direction,
            message_key=fu.message_key,
            now=now,
        )
    buffer_tracker_state(tracker_state)
    LOG.info(
        "watch_followup_sent",
        symbol=fu.symbol,
        followup_event=fu.event,
        message_id=result.message_id,
    )
    return True


def _record_followup_side_effects(
    followups: list[Any],
    *,
    sent_keys: set[str],
    now: datetime,
    pump_store: Any | None,
) -> None:
    """Append signal_events / pump_history only for follow-ups that shipped."""
    for fu in followups:
        if fu.message_key not in sent_keys:
            continue
        if fu.event == "invalidate":
            append_signal_event(
                "invalidate",
                symbol=fu.symbol,
                direction=str(fu.direction or (fu.payload or {}).get("direction") or ""),
                detail=str(fu.detail or ""),
                payload=fu.payload or {},
            )
        if pump_store is None:
            continue
        if fu.event == "fix_profit_tp1":
            record_signal_outcome(pump_store, symbol=fu.symbol, outcome="tp1", now=now)
        elif fu.event == "fix_profit_tp2":
            record_signal_outcome(pump_store, symbol=fu.symbol, outcome="tp2", now=now)
        elif fu.event == "invalidate":
            record_signal_outcome(
                pump_store, symbol=fu.symbol, outcome="invalidate", now=now
            )


def _split_telegram(text: str, *, limit: int = 3900) -> list[str]:
    from hunt_core.deliver.telegram import _split_telegram_text

    return _split_telegram_text(text, limit=limit)


async def _send_telegram_chunks(
    broadcaster: TelegramBroadcaster,
    text: str,
    *,
    log_key: str,
) -> bool:
    ok = True
    for idx, part in enumerate(_split_telegram(text)):
        result = await broadcaster.send_html(part)
        if result.status != "sent":
            LOG.warning(
                f"{log_key}_failed",
                part=idx + 1,
                status=result.status,
                reason=result.reason,
            )
            ok = False
        else:
            LOG.info(f"{log_key}_sent", part=idx + 1, message_id=result.message_id)
    return ok


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
) -> list[dict[str, Any]]:
    state = _load_state()
    tracker_state = load_tracker_state()
    prep_shadow_state = load_prep_shadow_state()
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
                    timeout=SYMBOL_TICK_TIMEOUT_S,
                )
                return sym, row
            except TimeoutError:
                LOG.warning("watch_symbol_timeout", symbol=sym, timeout_s=SYMBOL_TICK_TIMEOUT_S)
                return sym, {
                    "ts": now.isoformat(),
                    "symbol": sym,
                    "error": "symbol_tick_timeout",
                }
            except defensive_exc_types(asyncio.IncompleteReadError) as exc:
                LOG.warning("dump_symbol_failed", symbol=sym, error=repr(exc))
                return sym, {"ts": now.isoformat(), "symbol": sym, "error": repr(exc)}

        sem = asyncio.Semaphore(parallel)

        async def _bounded_snapshot(sym: str) -> tuple[str, dict[str, Any]]:
            async with sem:
                return await _snapshot_one(sym)

        snap_pairs = await asyncio.gather(*[_bounded_snapshot(s) for s in ordered])
        row_by_sym = dict(snap_pairs)
        snap_elapsed = round(time.monotonic() - tick_started, 2)
        if len(ordered) > 1:
            full_n = sum(1 for s in ordered if _tier_for(s) == "full")
            LOG.info(
                "watch_snapshot_batch",
                symbols=len(ordered),
                parallel=parallel,
                elapsed_s=snap_elapsed,
                tier=tier,
                full_symbols=full_n,
                fast_symbols=len(ordered) - full_n,
                used_weight_1m=client.used_weight_1m(),  # Binance IP budget; cap 2400/min
            )

        for symbol in ordered:
            try:
                advisory_sent_tick: set[str] = set()
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
                if symbol in PINNED_SYMBOLS and not row.get("error"):
                    try:
                        from hunt_core.analysis.pinned_deep import build_pinned_verdict
                        from hunt_core.analysis.pinned_deep import (
                            build_pinned_indicator_panel,
                            mtf_to_dict,
                            panel_to_dict,
                        )
                        from hunt_core.analysis.deep_signal import build_poc_level_scenarios

                        tf_pin = row.get("timeframes") or {}
                        px = float(row.get("price") or 0)
                        if px > 0:
                            panel = build_pinned_indicator_panel(symbol, tf_pin)
                            row["indicator_panel"] = panel
                            row["indicator_panel_summary"] = panel_to_dict(panel)
                            build_poc_level_scenarios(row)
                            row["pinned_verdict"] = build_pinned_verdict(row)
                            row["mtf_summary"] = mtf_to_dict(row.get("mtf"))
                        save_pinned_cache(symbol, row)
                    except Exception as exc:
                        LOG.warning("pinned_cache_save_failed", symbol=symbol, error=repr(exc))
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
                lifecycle_raw = row.get("lifecycle") or (dump.get("lifecycle") if dump else None)
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
                LOG.info(
                    "watch_tick",
                    symbol=symbol,
                    mode=mode,
                    price=row.get("price"),
                    hunt_phase=(lifecycle_raw or {}).get("phase"),
                    short_score=dump.get("dump_score"),
                    short_phase=dump.get("phase"),
                    short_confirmed=dump.get("confirmed"),
                    long_score=long_setup.get("long_score"),
                    long_phase=long_setup.get("phase"),
                    long_confirmed=long_setup.get("confirmed"),
                    data_missing=(row.get("data_quality") or {}).get("fields_missing"),
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
                    if cand.path == "early_advisory":
                        continue
                    cand_dir = cand.direction
                    cand_setup = cand.setup
                    if not cand_setup:
                        continue
                    process_prep_shadow(
                        prep_shadow_state,
                        symbol=symbol,
                        direction=cand_dir,
                        setup=cand_setup,
                        row=row,
                        lifecycle=lifecycle_raw,
                        now=now,
                    )
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
                    nfuel = max(
                        float((nsetup or {}).get("dump_fuel") or 0),
                        float((nsetup or {}).get("long_fuel") or 0),
                        float((nsetup or {}).get("dump_score") or 0),
                        float((nsetup or {}).get("long_score") or 0),
                    )
                    nphase = str((nsetup or {}).get("phase") or "")
                    await_phase = str(pend.get("await_phase") or "dump_confirmed")
                    min_fuel = float(pend.get("min_fuel") or 70.0)
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
                        and nfuel >= min_fuel
                        and nphase in forming_phases
                        and str(lc_dict.get("phase") or "")
                        in ("exhaustion_at_high", "distribution", "dump_active")
                    )
                    phase_ready = (
                        nsetup
                        and not bool(nsetup.get("confirmed"))
                        and nphase == await_phase
                        and nfuel >= min_fuel
                    )
                    if forming_ready or phase_ready:
                        record_funnel_stage(
                            "fuel",
                            symbol=symbol,
                            direction=ndir,
                            detail=nphase,
                            payload={"fuel": round(nfuel, 1), "min_fuel": min_fuel},
                        )
                    if nsetup and bool(nsetup.get("confirmed")):
                        from hunt_core.deliver.dispatch import evaluate_delivery

                        gate, delivery_tier = evaluate_delivery(
                            row,
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
                                "armed" if nfuel >= effective_hunt_params(symbol).confirm_min_score else
                                "prep"
                            )
                            skip = dump_hunt_skip_reason(
                                symbol=symbol,
                                tier=tier,  # type: ignore[arg-type]
                                price=price_now,
                                setup=nsetup,
                                lifecycle=lc_dict,
                                now=now,
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
                    for direction, setup in (("short", dump), ("long", long_setup)):
                        if not setup:
                            continue
                        if HUNT_SNIPER_MODE:
                            # H-A: only short fade in lifecycle `dump_active` ships live.
                            # Long stays shadow (tracked, never announced).
                            if direction != "short":
                                continue
                            _lc = lifecycle_raw if isinstance(lifecycle_raw, dict) else {}
                            _lc_phase = str(_lc.get("phase") or "")
                            if _lc_phase not in HUNT_SNIPER_LIVE_PHASES:
                                continue
                            # Respect the lifecycle's own entry permission. INXUSDT shipped a
                            # short in a transient window while the lifecycle said bias=wait /
                            # short_entry_ok=False (dump already ~15% underway) = late chase.
                            if _lc.get("short_entry_ok") is not True:
                                LOG.warning(
                                    "sniper_block_short_entry_not_ok", symbol=symbol,
                                    phase=_lc_phase, bias=_lc.get("recommended_bias"),
                                )
                                continue
                            # No-chase freshness: the entry zone is the acceptable fill band;
                            # if price already extended below it the move happened (INX: price
                            # 0.00827 under entry_zone[0]=0.008464). Missing/invalid geometry
                            # on a financial decision -> block loudly, never ship blind.
                            _px = float(row["price"])
                            from hunt_core.levels.levels import reanchor_setup_levels

                            reanchor_setup_levels(
                                setup,
                                row,
                                direction="short",
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
                            if _px < _zone_lo * (1.0 - HUNT_SNIPER_CHASE_TOL):
                                LOG.warning(
                                    "sniper_block_late_chase", symbol=symbol, price=_px,
                                    entry_zone_lo=_zone_lo,
                                    ext_pct=round((_zone_lo - _px) / _zone_lo * 100.0, 2),
                                )
                                continue
                            # HMSTR-class squeeze guard (forensics 2026-06-12): a short while
                            # top traders are heavily long fades smart money = squeeze fuel
                            # (HMSTR 2.48 / EPIC 2.11 lost; winners <=1.91). Use the 1h ratio,
                            # falling back to the 5m (fetched on every tier; ~1-2% of the 1h)
                            # so fast-tier dump candidates still get the guard.
                            # Absent = small altcoin without FAPI endpoint — allow through.
                            # Empirically all top performers (ESPORTS, BTW, BEAT) lacked this data.
                            _top_ls_f = effective_top_ls(row.get("market"))
                            if _top_ls_f is not None and _top_ls_f >= HUNT_SNIPER_TOP_LS_MAX:
                                LOG.info(
                                    "sniper_block_top_ls_squeeze",
                                    symbol=symbol,
                                    top_ls=_top_ls_f,
                                    max=HUNT_SNIPER_TOP_LS_MAX,
                                )
                                continue
                        confirmed_setup = bool(setup.get("confirmed"))
                        confirm_gate = None
                        confirm_tier: str | None = None
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
                        if confirmed_setup:
                            from hunt_core.deliver.dispatch import evaluate_delivery

                            confirm_gate, confirm_tier = evaluate_delivery(
                                row,
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
                                LOG.info(
                                    "watch_alert_blocked",
                                    symbol=symbol,
                                    direction=direction,
                                    score=setup.get("dump_score") or setup.get("long_score"),
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
                            lc = lifecycle_raw if isinstance(lifecycle_raw, dict) else {}
                            fuel = float(
                                setup.get("dump_fuel") or setup.get("long_fuel")
                                or setup.get("dump_score")
                                or setup.get("long_score")
                                or 0
                            )
                            if fuel >= effective_hunt_params(symbol).forming_min_score:
                                LOG.info(
                                    "watch_setup_forming",
                                    symbol=symbol,
                                    direction=direction,
                                    fuel=fuel,
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
                                        "fuel": fuel,
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
                            direction=direction, lifecycle=lifecycle_raw
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
                        if confirmed_setup:
                            gate, delivery_tier = confirm_gate, confirm_tier
                        else:
                            from hunt_core.deliver.dispatch import evaluate_delivery

                            gate, delivery_tier = evaluate_delivery(
                                row,
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

        # Orphan reconciliation: active signals whose symbol left the watchlist
        # would otherwise never close (PLAYUSDT held TP2 for 18h unnoticed).
        orphan_events = await _reconcile_orphan_signals(
            client, tracker_state, seen_symbols=set(symbols), now=clock.now_utc()
        )
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
        return rows
    finally:
        _save_state(state)
        buffer_tracker_state(tracker_state)
        save_prep_shadow_state(prep_shadow_state)
        save_setup_candidates_state(setup_candidates_state)
        flush_lake()


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
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    if migrate_calibration_split():
        LOG.info("hunt_calibration_migrated", path="hunt/data/hunt_calibration.json")
    try:
        rot_stats = rotate_hunt_ticks()
        if rot_stats.get("appended_lines") or rot_stats.get("archived"):
            LOG.info("hunt_tick_rotate", **rot_stats)
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
    # Persistent across ticks: kline/OI caches live in client; oi_flush/oi_build need prev tick.
    prev_oi: dict[str, float | None] = {}
    last_bias: dict[str, str] = {}
    last_lifecycle_phase: dict[str, str] = {}
    symbol_state = new_session_state()
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

    if broadcaster is not None and os.getenv("HUNT_STARTUP_TELEGRAM", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        cross_line = ", ".join(cross_cfg.exchanges) if cross_cfg.enabled else "off"
        try:
            await broadcaster.send_html(
                "🟢 <b>Hunt live</b>\n"
                f"Interval {interval_s}s · confirm-only alerts\n"
                f"Cross-intel: {cross_line}\n"
                "<i>Не auto-trade</i>"
            )
        except Exception:
            LOG.exception("watch_startup_telegram_failed")

    # /signal polling conflicts with a second getUpdates consumer — only when TG sends enabled.
    tg_cmds = (
        build_hunt_telegram_commands(settings)
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
            if not once:
                faulthandler.dump_traceback_later(
                    _wd_timeout_s, repeat=False, file=_wd_file, exit=True
                )
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
                ws_feed.set_symbols(list(active))
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
                }
                rows = await run_tick(active, **{k: v for k, v in tick_ctx.items() if k != "active"})
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
                if (
                    OUT_PATH.exists()
                    and OUT_PATH.stat().st_size >= TICK_ROTATE_MIN_BYTES
                    and time.monotonic() - last_tick_rotate >= TICK_ROTATE_INTERVAL_S
                ):
                    try:
                        rot_stats = rotate_hunt_ticks()
                        if rot_stats.get("appended_lines") or rot_stats.get("archived"):
                            LOG.info("hunt_tick_rotate_periodic", **rot_stats)
                        last_tick_rotate = time.monotonic()
                    except Exception:
                        LOG.exception("hunt_tick_rotate_periodic_failed")
                if once:
                    print(json.dumps(rows, indent=2, default=str))
                    break
            except Exception:
                LOG.exception("dump_watch_tick_error")
                if once:
                    raise
            if once:
                break
            deadline = started + max(1.0, float(interval_s))
            while time.monotonic() < deadline and not should_stop():
                pending = ws_feed.pop_kline_close_triggers()
                if pending and tick_ctx is not None:
                    ctx = tick_ctx
                    fast_syms = tuple(s for s in ctx["active"] if s in pending)
                    if fast_syms:
                        LOG.info("watch_kline_1m_trigger", symbols=list(fast_syms))
                        try:
                            fast_rows = await run_tick(
                                fast_syms,
                                **{
                                    **{k: v for k, v in ctx.items() if k not in ("active", "tier")},
                                    "tier": "fast",
                                },
                            )
                            for row in fast_rows:
                                row["tick_trigger"] = "kline_1m"
                            buffer_tick_rows(fast_rows)
                            ws_feed.consume_kline_close_triggers(set(fast_syms))
                        except Exception:
                            LOG.exception("watch_kline_fast_tick_failed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(3.0, remaining))
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
        feature_lake.close()
        if tg_task is not None:
            tg_task.cancel()
            try:
                await tg_task
            except asyncio.CancelledError:
                pass
        if tg_cmds is not None:
            await tg_cmds.close()
        await ws_feed.stop()
        await spot_companion.close()
        await client.close()


__all__ = ["run_tick", "run_loop"]
