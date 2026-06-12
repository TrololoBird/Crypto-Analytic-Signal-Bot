"""Hunter per-tick cycle — run_loop / run_tick (H-B rewrite)."""

from __future__ import annotations

import asyncio
import html
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from hunt_watch.adaptive_thresholds import load_adaptive_store, save_adaptive_store
from hunt_watch.alert_explain import evaluate_alert_gate, invalidate_detail_human
from hunt_watch.feature_latch import book_walls_from_row, feature_vector_from_row
from hunt_watch.lifecycle import promote_initial_pump_lifecycle
from hunt_watch.market_regime import (
    active_params,
    apply_snapshot,
    load_regime_file,
    refresh_market_regime,
)
from hunt_watch.dump_hunt_alert import (
    dump_hunt_skip_reason,
    format_dump_hunt_telegram,
    maybe_send_dump_hunt_telegram,
)
from hunt_watch.early_alert import (
    evaluate_early_alert,
    early_cooldown_ok,
    early_telegram_enabled,
    format_early_telegram,
    mark_early_sent,
)
from hunt_watch.ignition import (
    format_ignition_telegram,
    load_ignition_state,
    mark_ignition_notified,
    pending_ignition_alerts,
    process_ticker_snapshots,
    save_ignition_state,
)
from hunt_watch.param_store import effective_hunt_params, migrate_calibration_split
from hunt_watch.prep_shadow_tracker import (
    load_prep_shadow_state,
    process_prep_shadow,
    save_prep_shadow_state,
)
from hunt_watch.pump_history import (
    backfill_from_jsonl,
    format_history_telegram,
    load_pump_history,
    observe_prices,
    record_pump_leg,
    record_signal_outcome,
    save_pump_history,
    stats_for,
)
from hunt_watch.pump_history import record_signal_open as record_pump_signal_open
from hunt_watch.signal_engine import phase_long as _se_phase_long
from hunt_watch.signal_events import append_signal_event
from hunt_watch.signal_tracker import (
    evaluate_followups,
    latch_row_setups,
    load_tracker_state,
    mark_followups_sent,
    reconcile_signal,
    register_signal_open,
    save_tracker_state,
)
from hunt_watch.targets import PINNED_SYMBOLS, effective_watch_mode, resolve_watch_universe
from hunt_watch.telegram_commands import build_hunt_telegram_commands
from hunt_watch.tick_rotate import rotate_hunt_ticks
from hunt_watch.watchlist_ops import clear_signal_notify, load_pending_notify
from hunt_core.domain.config import load_settings
from hunt_core.errors import DEFENSIVE_EXC, defensive_exc_types
from hunt_core.features.prepare import _prepare_frame, min_required_bars
from hunt_core.telegram import TelegramBroadcaster

from hunt_core.market import HuntCcxtClient, HuntCcxtSpotCompanion, HuntCcxtStreams
from hunt_core.market.factory import create_hunt_market_plane_from_settings

from hunt_core.data.collect import snapshot_symbol
from hunt_core.data.rest_tiers import (
    SnapshotTier,
    TickBatchCache,
    refresh_tick_batch_cache,
    sort_symbols_for_tick,
)
from hunt_core.data.tick_io import append_tick_rows
from hunt_core.gate.pipeline import run_gate_pipeline
from hunt_core.runtime.settings import (
    COOLDOWN_MINUTES,
    FORMING_MIN_SCORE,
    HUNT_MIN_RISK_REWARD,
    IGNITION_MIN_PCT,
    IGNITION_MIN_QVOL_USD,
    IGNITION_MIN_VOL_DELTA_USD,
    IGNITION_TELEGRAM_ENABLED,
    IGNITION_TTL_S,
    IGNITION_WINDOW_S,
    LOG,
    OUT_PATH,
    REGIME_REFRESH_S,
    SCAN_INTERVAL_S,
    SNIPER_CONFIG,
    SQUEEZE_COOLDOWN_MINUTES,
    SQUEEZE_MIN_VOL_24H_M,
    STATE_PATH,
    STOP,
    SYMBOL_TICK_TIMEOUT_S,
    SYMBOL_WATCH_MODES,
    TICK_ROTATE_INTERVAL_S,
    TICK_ROTATE_MIN_BYTES,
    WatchMode,
)


HUNT_SNIPER_MODE = SNIPER_CONFIG.enabled
HUNT_SNIPER_LIVE_PHASES = SNIPER_CONFIG.live_phases
HUNT_SNIPER_TOP_LS_MAX = SNIPER_CONFIG.top_ls_max
HUNT_SNIPER_REQUIRE_TOP_LS = SNIPER_CONFIG.require_top_ls
HUNT_SNIPER_CHASE_TOL = SNIPER_CONFIG.chase_tol


def _format_squeeze_telegram(row: dict[str, Any]) -> str:
    from hunt_watch.deliver.telegram import format_squeeze_telegram  # noqa: PLC0415
    return format_squeeze_telegram(row)


async def _safe_fetch(coro: Any) -> Any:
    try:
        return await coro
    except DEFENSIVE_EXC:
        return None
def _should_alert(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    lifecycle: Any | None = None,
    row: dict[str, Any] | None = None,
) -> bool:
    if not isinstance(setup, dict):
        return False
    return run_gate_pipeline(
        setup=setup,
        direction=direction,
        symbol=symbol,
        lifecycle=lifecycle,
        row=row or {},
        sniper_config=SNIPER_CONFIG,
    ).ok


def _alert_block_reason(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    lifecycle: Any | None = None,
    row: dict[str, Any] | None = None,
) -> str:
    return run_gate_pipeline(
        setup=setup,
        direction=direction,
        symbol=symbol,
        lifecycle=lifecycle,
        row=row or {},
        sniper_config=SNIPER_CONFIG,
    ).message


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
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


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
        not in ("dump_active", "exhaustion_at_high", "distribution")
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
    """Reject TG when price already at/through TP1 — instant TP1 + invalidate ping-pong."""
    tp1 = float(setup.get("tp1") or 0)
    if tp1 <= 0 or price <= 0:
        return False
    if direction == "short":
        return price <= tp1
    return price >= tp1


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
) -> list[str]:
    score_key = "dump_score" if direction == "short" else "long_score"
    fuel_key = "dump_fuel" if direction == "short" else "long_fuel"
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

    fuel = _opt_num(setup.get(fuel_key)) if setup.get(fuel_key) is not None else "—"
    score = _opt_num(setup.get(score_key)) if setup.get(score_key) is not None else "—"
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
    trig_txt = html.escape(", ".join(str(t) for t in triggers[:5]))
    if len(triggers) > 5:
        trig_txt += "…"

    ez = setup.get("entry_zone") or [price, price]

    oi = pos.get("oi")
    oi_chg = pos.get("oi_chg_5m")
    fund = pos.get("funding_pct")
    taker = pos.get("taker_5m")
    ls = pos.get("ls_5m")

    if direction == "short":
        level_line = (
            f"Support <code>{_fmt_price(setup.get('support_break_level'))}</code> · liq "
            f"<code>{_fmt_price(setup.get('resistance_liq'))}</code> · impulse H "
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
        f"fuel <code>{fuel}</code> · raw <code>{score}</code>",
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
            f"OI <code>{_fmt_price(oi if oi is not None else None)}</code> · "
            f"Δ5m <code>{_opt_num(oi_chg)}</code> · "
            f"fund <code>{_opt_num(fund, digits=3)}%</code> · "
            f"taker5m <code>{_opt_num(taker)}</code> · "
            f"L/S <code>{_opt_num(ls)}</code>"
        ),
        f"Triggers: <code>{trig_txt or '—'}</code>",
    ]
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


def _format_telegram(row: dict[str, Any], *, direction: str, confirm_reasons: list[str]) -> str:
    sym = html.escape(str(row["symbol"]).replace("USDT", "-USDT"))
    setup = row["dump"] if direction == "short" else row["long"]
    fuel_key = "dump_fuel" if direction == "short" else "long_fuel"
    score_key = "dump_score" if direction == "short" else "long_score"
    price = float(row.get("price") or 0)
    tf = row.get("timeframes") or {}
    pos = row.get("market") or row.get("positioning") or {}
    lc = row.get("lifecycle") or {}
    lc_phase = str(lc.get("phase") or "—")

    badge = "🔴" if direction == "short" else "🟢"
    dir_label = "SHORT" if direction == "short" else "LONG"

    fuel_val = setup.get(fuel_key)
    score_val = setup.get(score_key)
    fuel = float(fuel_val) if fuel_val is not None else 0.0
    fuel_str = f"{fuel:.0f}" if fuel_val is not None else "—"
    score_str = f"{float(score_val):.0f}" if score_val is not None else "—"

    # Signal quality rating
    _strong_phases = frozenset({"dump_active","exhaustion_at_high","distribution","dump_confirmed",
                                 "accumulation","impulse_initiating","breakout_arming","long_confirmed"})
    if fuel >= 80 and lc_phase in _strong_phases:
        rating = "🔥 СИЛЬНЫЙ"
    elif fuel >= 65 and lc_phase in _strong_phases:
        rating = "✅ УВЕРЕННЫЙ"
    elif fuel >= 50:
        rating = "⚠️ СРЕДНИЙ"
    else:
        rating = "📊 СЛАБЫЙ"

    lifecycle_line = html.escape(_phase_human(lc_phase)) if lc_phase != "—" else "—"

    ez = setup.get("entry_zone") or [price, price]
    entry_lo = _fmt_price(ez[0])
    entry_hi = _fmt_price(ez[1])
    sl = _fmt_price(setup.get("stop_loss"))
    tp1 = setup.get("tp1")
    tp2 = setup.get("tp2")
    tp1_pct = _pct_str(price, float(tp1), direction) if tp1 else ""
    tp2_pct = _pct_str(price, float(tp2), direction) if tp2 else ""
    tp1_lbl = setup.get("tp1_label") or ""
    tp2_lbl = setup.get("tp2_label") or ""
    tp1_str = f"<code>{_fmt_price(tp1)}</code>" + (f" (<b>{tp1_pct}</b>)" if tp1_pct else "") + (f" · {tp1_lbl}" if tp1_lbl else "")
    tp2_str = f"<code>{_fmt_price(tp2)}</code>" + (f" (<b>{tp2_pct}</b>)" if tp2_pct else "") + (f" · {tp2_lbl}" if tp2_lbl else "")

    reason = _reason_human(setup, direction=direction, lc_phase=lc_phase)

    header = f"{badge} <b>ВХОД ВЗЯТ · {sym} {dir_label}</b>  {rating}"
    phase_line = f"📌 {lifecycle_line}"
    entry_line = f"📍 Вход: <code>{entry_lo}–{entry_hi}</code>  |  Стоп: <code>{sl}</code>"
    tp_line = f"🎯 TP1: {tp1_str}  |  TP2: {tp2_str}"
    reason_line = f"💡 {html.escape(reason)}"
    score_line = f"📊 Score: <code>{score_str}</code> · Fuel: <code>{fuel_str}</code>"
    footer = "<i>Signal-only · closed 5m/1m confirm · открывай сделку вручную</i>"

    hist = format_history_telegram(row.get("pump_history"))
    hist_line = f"{html.escape(hist)}\n" if hist else ""

    return f"{header}\n{phase_line}\n{entry_line}\n{tp_line}\n{reason_line}\n{score_line}\n{hist_line}\n{footer}"


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
        df = await _safe_fetch(
            client.fetch_klines_between(
                o_sym,
                "5m",
                start_time_ms=int(anchor.timestamp() * 1000),
                end_time_ms=int(now.timestamp() * 1000),
            )
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
        df = await _safe_fetch(
            client.fetch_klines_between(
                o_sym,
                "5m",
                start_time_ms=int(anchor.timestamp() * 1000),
                end_time_ms=int(now.timestamp() * 1000),
            )
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
        delta = datetime.now(UTC) - start
        total_m = int(delta.total_seconds() // 60)
        h, m = divmod(total_m, 60)
        if h > 0:
            return f"{h}ч {m}м"
        return f"{m}м"
    except Exception:
        return "—"


def _format_followup_telegram(followup: Any, row: dict[str, Any]) -> str:
    sym = html.escape(str(followup.symbol).replace("USDT", "-USDT"))
    direction = followup.direction.upper()
    price = _fmt_price(followup.price)
    lc = row.get("lifecycle") or {}
    payload = followup.payload if isinstance(followup.payload, dict) else {}
    event = followup.event

    # Use levels frozen at entry (payload), not live recalculated setup on this tick.
    sl = _fmt_price(payload.get("stop_loss"))
    tp1_lvl = _fmt_price(payload.get("tp1"))
    tp2_lvl = _fmt_price(payload.get("tp2"))
    entry_lo = payload.get("entry_lo")
    entry_hi = payload.get("entry_hi")
    entry_zone = (
        f"{_fmt_price(entry_lo)}–{_fmt_price(entry_hi)}"
        if entry_lo is not None and entry_hi is not None
        else "—"
    )
    opened_raw = str(payload.get("opened_at") or "")[:19].replace("T", " ")
    msg_id = payload.get("entry_message_id")
    entry_ref = f"Вход {entry_zone}"
    if msg_id:
        entry_ref += f" · сигнал TG <code>#{msg_id}</code>"

    reason_raw = str(payload.get("reason") or "")
    detail_human = invalidate_detail_human(str(followup.detail or ""), reason=reason_raw)

    # TP1 hit: structured update card
    if event == "fix_profit_tp1":
        fix_pct = int(payload.get("partial_fixed_pct") or 50)
        new_sl = _fmt_price(payload.get("stop_loss"))
        tp1_pct_val = payload.get("tp1")
        entry_price_est = entry_lo or 0
        if tp1_pct_val and entry_price_est:
            try:
                if direction == "SHORT":
                    tp1_pct = (float(entry_price_est) - float(tp1_pct_val)) / float(entry_price_est) * 100.0
                else:
                    tp1_pct = (float(tp1_pct_val) - float(entry_price_est)) / float(entry_price_est) * 100.0
                tp1_pct_str = f" +{tp1_pct:.1f}%"
            except Exception:
                tp1_pct_str = ""
        else:
            tp1_pct_str = ""
        return (
            f"✅ <b>TP1 достигнут{tp1_pct_str} · {sym} {direction}</b>\n"
            f"🔒 Зафиксируй <b>{fix_pct}%</b> позиции · Стоп перенесён на безубыток <code>{new_sl}</code>\n"
            f"🎯 Следующая цель: TP2 <code>{tp2_lvl}</code>\n"
            f"{entry_ref}\n"
            f"<i>Hunt follow-up · не auto-trade</i>"
        )

    # TP2 hit: close card
    if event == "fix_profit_tp2":
        duration = _duration_str(opened_raw)
        skipped = bool(payload.get("tp1_skipped"))
        extra = " (TP1 пролёт)" if skipped else ""
        return (
            f"📋 <b>Закрыт {sym} {direction}{extra}</b>\n"
            f"💰 PnL: TP2 <code>{tp2_lvl}</code> · Длит: {duration}\n"
            f"📌 Причина: Достигнут TP2\n"
            f"{entry_ref}\n"
            f"<i>Hunt follow-up · не auto-trade</i>"
        )

    # Signal closed / invalidated
    if event == "invalidate":
        duration = _duration_str(opened_raw)
        reason_label_map = {
            "stop_hit": "Стоп-лосс пробит",
            "tp1": "Закрыто по TP1",
            "tp2": "Закрыто по TP2",
            "bounce_invalidate": "Lifecycle: отскок — шорт отменён",
            "time_stall": "Нет прогресса за 8ч — тезис не сработал",
            "bias_flip": "Lifecycle сменил bias против позиции",
            "support_lost": "Потеря поддержки (лонг)",
        }
        reason_str = reason_label_map.get(reason_raw, html.escape(detail_human))
        return (
            f"📋 <b>Закрыт {sym} {direction}</b>\n"
            f"📌 Причина: {reason_str}\n"
            f"⏱ Длит: {duration}\n"
            f"{entry_ref}\n"
            f"Уровни: SL <code>{sl}</code> · TP1 <code>{tp1_lvl}</code> · TP2 <code>{tp2_lvl}</code>\n"
            f"<i>Hunt follow-up · не auto-trade</i>"
        )

    # Stop warning
    if event == "stop_warning":
        return (
            f"⚠️ <b>СТОП РЯДОМ · {sym} {direction}</b>\n"
            f"Цена <code>{price}</code> близко к SL <code>{sl}</code>\n"
            f"Реши: держать или фиксировать вручную.\n"
            f"{entry_ref}\n"
            f"<i>Hunt follow-up · не auto-trade</i>"
        )

    # Generic fallback
    badges = {"phase_change": "🔄", "avg_zone": "➕"}
    titles = {"phase_change": "PHASE CHANGE", "avg_zone": "AVG ZONE"}
    badge = badges.get(event, "📣")
    title = titles.get(event, event)
    lc_phase_now = html.escape(_phase_human(str(lc.get("phase") or "—")))
    return (
        f"{badge} <b>{title}</b>\n"
        f"{sym} · <code>{direction}</code> · цена <code>{price}</code>\n"
        f"{html.escape(detail_human)}\n"
        f"{entry_ref}\n"
        f"SL <code>{sl}</code> · TP1 <code>{tp1_lvl}</code> · TP2 <code>{tp2_lvl}</code>\n"
        f"Фаза: {lc_phase_now}\n"
        f"<i>Hunt follow-up · не auto-trade</i>"
    )


def _split_telegram(text: str, *, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    chunk = ""
    for block in text.split("\n\n"):
        candidate = f"{chunk}\n\n{block}".strip() if chunk else block
        if len(candidate) <= limit:
            chunk = candidate
            continue
        if chunk:
            parts.append(chunk)
        chunk = block
    if chunk:
        parts.append(chunk)
    return parts or [text[:limit]]


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
) -> list[dict[str, Any]]:
    state = _load_state()
    tracker_state = load_tracker_state()
    prep_shadow_state = load_prep_shadow_state()
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    notify_pending = {str(p.get("symbol")): p for p in load_pending_notify()}
    try:
        cache = batch_cache or TickBatchCache()
        need_btc = any(s != "BTCUSDT" for s in symbols)
        await refresh_tick_batch_cache(
            cache,
            client,
            safe_fetch=_safe_fetch,
            prepare_frame=_prepare_frame,
            need_btc=need_btc,
            tier=tier,
        )
        premium_all = cache.premium_all
        funding_info_all = cache.funding_info_all
        exchange_by_sym = cache.exchange_by_sym
        btc_work_1h = cache.btc_work_1h
        if ticker_by_sym is None:
            ticker_raw = await _safe_fetch(client.fetch_ticker_24h()) or []
            ticker_by_sym = {str(t.get("symbol")): t for t in ticker_raw if t.get("symbol")}
        if tier == "full" and spot_companion is not None and symbols:
            futures_mids = {
                s: float((ticker_by_sym.get(s) or {}).get("last_price") or 0) or None
                for s in symbols
            }
            try:
                spot_n = await spot_companion.refresh_symbols(
                    list(symbols), futures_mid_by_symbol=futures_mids
                )
                LOG.debug("spot_companion_refresh", symbols=len(symbols), updated=spot_n)
            except defensive_exc_types(asyncio.IncompleteReadError) as exc:
                LOG.warning("spot_companion_refresh_failed", error=repr(exc))

        ordered = sort_symbols_for_tick(
            symbols,
            ignition_by_sym=ignition_by_sym,
            last_bias=last_bias,
        )
        if tier == "fast":
            LOG.debug("watch_tick_fast_tier", symbols=len(ordered), head=list(ordered[:4]))

        for symbol in ordered:
            try:
                mode = effective_watch_mode(
                    symbol,
                    mode_map,
                    lifecycle_bias=last_bias.get(symbol),
                )
                row = await asyncio.wait_for(
                    snapshot_symbol(
                        client,
                        settings,
                        minimums,
                        symbol,
                        watch_mode=mode,
                        prev_oi=prev_oi.get(symbol),
                        premium_all=premium_all,
                        funding_info_all=funding_info_all,
                        btc_work_1h=btc_work_1h,
                        exchange_by_sym=exchange_by_sym,
                        ticker_by_sym=ticker_by_sym,
                        ws_feed=ws_feed,
                        spot_companion=spot_companion,
                        pump_stats=(
                            pump_stats_by_sym.get(symbol) if pump_stats_by_sym else None
                        ),
                        tier=tier,
                    ),
                    timeout=SYMBOL_TICK_TIMEOUT_S,
                )
                row = latch_row_setups(tracker_state, row)
                oi_val = (row.get("market") or row.get("positioning") or {}).get("oi")
                if oi_val is not None:
                    prev_oi[symbol] = float(oi_val)
                rows.append(row)
                if ignition_by_sym and symbol in ignition_by_sym:
                    row["ignited"] = True
                    row["ignition"] = ignition_by_sym[symbol]
                promote_initial_pump_lifecycle(row, symbol=symbol)
                if pump_stats_by_sym and symbol in pump_stats_by_sym:
                    row["pump_history"] = pump_stats_by_sym[symbol]
                dump = row.get("dump") or {}
                long_setup = row.get("long") or {}
                lifecycle_raw = row.get("lifecycle") or (dump.get("lifecycle") if dump else None)
                if lifecycle_raw and isinstance(lifecycle_raw, dict):
                    last_bias[symbol] = str(lifecycle_raw.get("recommended_bias") or "")
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
                for prep_dir, prep_setup in (("short", dump), ("long", long_setup)):
                    if prep_setup:
                        process_prep_shadow(
                            prep_shadow_state,
                            symbol=symbol,
                            direction=prep_dir,
                            setup=prep_setup,
                            row=row,
                            lifecycle=lifecycle_raw,
                            now=now,
                        )
                kline_events = await _reconcile_inwatch_active(
                    client, tracker_state, symbol=symbol, now=now
                )
                if kline_events:
                    mark_followups_sent(tracker_state, kline_events, now=now)
                    for fu in kline_events:
                        LOG.info(
                            "watch_followup_kline",
                            symbol=fu.symbol,
                            followup_event=fu.event,
                            detail=fu.detail,
                        )
                followups = evaluate_followups(tracker_state, row, now=now)
                for fu in followups:
                    LOG.info(
                        "watch_followup",
                        symbol=fu.symbol,
                        followup_event=fu.event,
                        detail=fu.detail,
                    )
                    # State machine runs for every signal; messages only for
                    # signals that were actually announced in Telegram.
                    announced = bool((fu.payload or {}).get("announced", True))
                    if send_telegram and broadcaster is not None and announced:
                        msg = _format_followup_telegram(fu, row)
                        result = await broadcaster.send_html(msg)
                        if result.status == "sent":
                            LOG.info(
                                "watch_followup_sent",
                                symbol=fu.symbol,
                                followup_event=fu.event,
                                message_id=result.message_id,
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
                    if (
                        send_telegram
                        and broadcaster is not None
                        and _cooldown_ok(
                            symbol,
                            "squeeze",
                            state,
                            now=now,
                            minutes=SQUEEZE_COOLDOWN_MINUTES,
                        )
                    ):
                        result = await broadcaster.send_html(_format_squeeze_telegram(row))
                        if result.status == "sent":
                            state[f"{symbol}:squeeze"] = now.isoformat()
                            LOG.info(
                                "hunt_squeeze_telegram_sent",
                                symbol=symbol,
                                message_id=result.message_id,
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
                    if nsetup and bool(nsetup.get("confirmed")):
                        if not _should_alert(
                            nsetup,
                            direction=ndir,
                            symbol=symbol,
                            lifecycle=lifecycle_raw,
                            row=row,
                        ):
                            LOG.info(
                                "signal_notify_blocked",
                                symbol=symbol,
                                direction=ndir,
                                reason=_alert_block_reason(
                                    nsetup,
                                    direction=ndir,
                                    symbol=symbol,
                                    lifecycle=lifecycle_raw,
                                    row=row,
                                ),
                            )
                            clear_signal_notify(symbol)
                        else:
                            sym_label = html.escape(symbol.replace("USDT", "-USDT"))
                            body = _format_telegram(
                                row,
                                direction=ndir,
                                confirm_reasons=list(nsetup.get("confirm_hard") or []),
                            )
                            notify_msg = f"🔔 <b>/signal confirm</b> {sym_label}\n{body}"
                            notify_result = await broadcaster.send_html(notify_msg)
                            if notify_result.status == "sent":
                                clear_signal_notify(symbol)
                                LOG.info(
                                    "signal_notify_sent",
                                    symbol=symbol,
                                    direction=ndir,
                                    message_id=notify_result.message_id,
                                )
                    elif (forming_ready or phase_ready) and ndir == "short":
                        price_now = float(row.get("price") or 0)
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
                                if sent:
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

                if followups:
                    mark_followups_sent(tracker_state, followups, now=now)
                    for fu in followups:
                        if fu.event == "invalidate":
                            append_signal_event(
                                "invalidate",
                                symbol=fu.symbol,
                                direction=str((fu.payload or {}).get("direction") or ""),
                                detail=str(fu.detail or ""),
                                payload=fu.payload or {},
                            )
                    if pump_store is not None:
                        for fu in followups:
                            if fu.event == "fix_profit_tp1":
                                record_signal_outcome(
                                    pump_store, symbol=fu.symbol, outcome="tp1", now=now
                                )
                            elif fu.event == "fix_profit_tp2":
                                record_signal_outcome(
                                    pump_store, symbol=fu.symbol, outcome="tp2", now=now
                                )
                            elif fu.event == "invalidate":
                                record_signal_outcome(
                                    pump_store, symbol=fu.symbol, outcome="invalidate", now=now
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
                            _ez = setup.get("entry_zone")
                            try:
                                _zone_lo = float(_ez[0])
                                _px = float(row["price"])
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
                            # (HMSTR 2.48 / EPIC 2.11 lost; winners <=1.91). A malformed or
                            # absent value must not silently bypass the guard — block loudly.
                            _top_ls = (row.get("market") or {}).get("top_ls_1h")
                            if _top_ls is None:
                                if HUNT_SNIPER_REQUIRE_TOP_LS:
                                    LOG.warning("sniper_block_top_ls_missing", symbol=symbol)
                                    continue
                            else:
                                try:
                                    _top_ls_f = float(_top_ls)
                                except (TypeError, ValueError):
                                    LOG.error(
                                        "sniper_block_bad_top_ls", symbol=symbol, top_ls=_top_ls,
                                    )
                                    continue
                                if _top_ls_f >= HUNT_SNIPER_TOP_LS_MAX:
                                    continue
                        if not _should_alert(
                            setup,
                            direction=direction,
                            symbol=symbol,
                            lifecycle=lifecycle_raw,
                            row=row,
                        ):
                            await _maybe_send_early_alert(
                                broadcaster,
                                symbol=symbol,
                                direction=direction,
                                setup=setup,
                                row=row,
                                lifecycle_raw=lifecycle_raw,
                                state=state,
                                mode=mode,
                                now=now,
                            )
                            lc = lifecycle_raw if isinstance(lifecycle_raw, dict) else {}
                            fuel = float(
                                setup.get("dump_fuel") or setup.get("long_fuel")
                                or setup.get("dump_score")
                                or setup.get("long_score")
                                or 0
                            )
                            if bool(setup.get("confirmed")):
                                gate = evaluate_alert_gate(
                                    setup,
                                    direction=direction,
                                    symbol=symbol,
                                    lifecycle=lifecycle_raw,
                                    row=row,
                                )
                                LOG.info(
                                    "watch_alert_blocked",
                                    symbol=symbol,
                                    direction=direction,
                                    score=setup.get("dump_score") or setup.get("long_score"),
                                    hunt_phase=lc.get("phase"),
                                    block_code=gate.code,
                                    reason=gate.message,
                                )
                                append_signal_event(
                                    "blocked",
                                    symbol=symbol,
                                    direction=direction,
                                    detail=gate.message,
                                    payload={
                                        "block_code": gate.code,
                                        "score": setup.get("dump_score")
                                        or setup.get("long_score"),
                                        "fuel": setup.get("dump_fuel")
                                        or setup.get("long_fuel"),
                                        "phase": setup.get("phase"),
                                        "lifecycle_phase": lc.get("phase"),
                                    },
                                )
                            elif fuel >= effective_hunt_params(symbol).forming_min_score:
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
                            not in ("dump_active", "exhaustion_at_high", "distribution")
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
                        price_now = float(row.get("price") or 0)
                        if _entry_past_tp1(setup, direction=direction, price=price_now):
                            LOG.info(
                                "watch_telegram_skipped_past_tp1",
                                symbol=symbol,
                                direction=direction,
                                price=price_now,
                                tp1=setup.get("tp1"),
                            )
                            continue
                        msg = _format_telegram(
                            row,
                            direction=direction,
                            confirm_reasons=setup.get("confirm_hard") or [],
                        )
                        result = await broadcaster.send_html(msg)
                        key = f"{symbol}:{direction}"
                        if result.status == "sent":
                            state[key] = now.isoformat()
                            if pump_store is not None:
                                record_pump_signal_open(
                                    pump_store,
                                    symbol=symbol,
                                    direction=direction,
                                    now=now,
                                )
                            setup_latch = {**setup, "telegram_sent": True}
                            register_signal_open(
                                tracker_state,
                                symbol=symbol,
                                direction=direction,
                                price=float(row.get("price") or 0),
                                setup=setup_latch,
                                lifecycle=lifecycle_raw
                                if isinstance(lifecycle_raw, dict)
                                else None,
                                now=now,
                                entry_message_id=result.message_id,
                                features_open=feature_vector_from_row(row),
                                book_walls=book_walls_from_row(row),
                            )
                            LOG.info(
                                "watch_telegram_sent",
                                symbol=symbol,
                                direction=direction,
                                message_id=result.message_id,
                            )
                            append_signal_event(
                                "confirmed",
                                symbol=symbol,
                                direction=direction,
                                detail="telegram_sent",
                                payload={
                                    "message_id": result.message_id,
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
            except TimeoutError:
                LOG.warning("watch_symbol_timeout", symbol=symbol, timeout_s=SYMBOL_TICK_TIMEOUT_S)
                rows.append(
                    {"ts": now.isoformat(), "symbol": symbol, "error": "symbol_tick_timeout"}
                )
            except defensive_exc_types(asyncio.IncompleteReadError) as exc:
                LOG.warning("dump_symbol_failed", symbol=symbol, error=repr(exc))
                rows.append({"ts": now.isoformat(), "symbol": symbol, "error": repr(exc)})

        # Orphan reconciliation: active signals whose symbol left the watchlist
        # would otherwise never close (PLAYUSDT held TP2 for 18h unnoticed).
        orphan_events = await _reconcile_orphan_signals(
            client, tracker_state, seen_symbols=set(symbols), now=datetime.now(UTC)
        )
        if orphan_events:
            mark_followups_sent(tracker_state, orphan_events, now=datetime.now(UTC))
            for fu in orphan_events:
                LOG.info(
                    "watch_followup_orphan",
                    symbol=fu.symbol,
                    followup_event=fu.event,
                    detail=fu.detail,
                )
                if pump_store is not None:
                    if fu.event == "fix_profit_tp1":
                        record_signal_outcome(pump_store, symbol=fu.symbol, outcome="tp1", now=now)
                    elif fu.event == "fix_profit_tp2":
                        record_signal_outcome(pump_store, symbol=fu.symbol, outcome="tp2", now=now)
                    elif fu.event == "invalidate":
                        record_signal_outcome(
                            pump_store, symbol=fu.symbol, outcome="invalidate", now=now
                        )
                announced = bool((fu.payload or {}).get("announced", True))
                if send_telegram and broadcaster is not None and announced:
                    msg = _format_followup_telegram(fu, {"symbol": fu.symbol})
                    await broadcaster.send_html(msg)
        return rows
    finally:
        _save_state(state)
        save_tracker_state(tracker_state)
        save_prep_shadow_state(prep_shadow_state)


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
    plane = await create_hunt_market_plane_from_settings(settings)
    client = plane.client
    ws_feed = plane.streams
    spot_companion = plane.spot
    ws_feed.set_symbols(list(cli_symbols))
    await ws_feed.start()
    # Persistent across ticks: kline/OI caches live in client; oi_flush/oi_build need prev tick.
    prev_oi: dict[str, float | None] = {}
    last_bias: dict[str, str] = {}
    ignition_state = load_ignition_state()
    pump_store = load_pump_history()
    adaptive_store = load_adaptive_store()
    if not pump_store.symbols and not pump_store.event_log:
        backfill_from_jsonl(pump_store)
        save_pump_history(pump_store)

    last_scan = 0.0
    last_regime = 0.0
    last_tick_rotate = time.monotonic()
    batch_cache = TickBatchCache()
    cached = load_regime_file()
    if cached is not None:
        apply_snapshot(cached)
    try:
        await refresh_market_regime(client)
        last_regime = time.monotonic()
    except Exception:
        LOG.exception("market_regime_startup_failed")

    # /signal command loop is independent of confirm-broadcast preflight.
    tg_cmds = build_hunt_telegram_commands(settings) if settings.tg_token else None
    tg_task: asyncio.Task[None] | None = None
    if tg_cmds is not None:
        tg_task = asyncio.create_task(tg_cmds.run_forever(), name="hunt_tg_commands")
        LOG.info("hunt_telegram_commands_scheduled")

    try:
        tick_ctx: dict[str, Any] | None = None
        while not STOP:
            started = time.monotonic()
            try:
                if time.monotonic() - last_regime >= REGIME_REFRESH_S:
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

                if time.monotonic() - last_scan >= SCAN_INTERVAL_S:
                    try:
                        from hunt_watch.scanner_runner import run_scan

                        summary = await run_scan(limit=30, min_score=45.0)
                        LOG.info(
                            "hunt_scan_refresh",
                            watch=summary.get("watch_count"),
                            priority=summary.get("priority_count"),
                        )
                    except defensive_exc_types(asyncio.IncompleteReadError) as exc:
                        LOG.warning("hunt_scan_refresh_failed", error=repr(exc))
                    last_scan = time.monotonic()

                settings = load_settings()
                now = datetime.now(UTC)
                ticker_raw = await _safe_fetch(client.fetch_ticker_24h()) or []
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
                if send_telegram and broadcaster is not None and IGNITION_TELEGRAM_ENABLED:
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

                symbols, mode_map = resolve_watch_universe(
                    settings,
                    static_modes=SYMBOL_WATCH_MODES,
                    ignited=ignition_by_sym,
                )
                merged: list[str] = list(symbols)
                for sym in cli_symbols:
                    s = sym.upper()
                    if s not in merged:
                        merged.append(s)
                    mode_map.setdefault(s, SYMBOL_WATCH_MODES.get(s, "short"))
                active = tuple(dict.fromkeys(merged))
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

                tick_ctx = {
                    "active": active,
                    "settings": settings,
                    "minimums": minimums,
                    "client": client,
                    "prev_oi": prev_oi,
                    "last_bias": last_bias,
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
                }
                rows = await run_tick(active, **{k: v for k, v in tick_ctx.items() if k != "active"})
                save_pump_history(pump_store)
                append_tick_rows(rows)
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
            while time.monotonic() < deadline and not STOP:
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
                            append_tick_rows(fast_rows)
                            ws_feed.consume_kline_close_triggers(set(fast_syms))
                        except Exception:
                            LOG.exception("watch_kline_fast_tick_failed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(3.0, remaining))
    finally:
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
