"""Telegram /signals — live recalc of all active tracker signals."""

from __future__ import annotations

import asyncio
import html
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from engine.telegram import TelegramBroadcaster

from hunt_watch.alert_explain import (
    collect_report_blockers,
    evaluate_alert_gate,
    evaluate_stale_advice,
    format_setup_snapshot,
    primary_block_for_report,
)
from hunt_watch.paths import SIGNAL_EVENTS, TELEGRAM_COOLDOWN
from hunt_watch.signal_tracker import load_tracker_state
from hunt_watch.symbol_probe import _watch_module, probe_symbol_signal

_PROBE_RETRIES = 3
_PROBE_RETRY_DELAY_S = 1.5


@dataclass(slots=True)
class _ReportRollup:
    n_plus: int = 0
    n_tp1: int = 0
    n_realert: int = 0
    n_stale: int = 0
    n_bias_conflict: int = 0
    n_probe_fail: int = 0


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


def _pnl_pct(sig: dict[str, Any], direction: str, price: float) -> float | None:
    lo = float(sig.get("entry_lo") or 0)
    hi = float(sig.get("entry_hi") or 0)
    mid = (lo + hi) / 2.0 if lo > 0 and hi > 0 else (lo or hi)
    if mid <= 0 or price <= 0:
        return None
    raw = (price - mid) / mid * 100.0
    return round(-raw if direction == "short" else raw, 2)


def _human_probe_error(exc: BaseException) -> str:
    name = type(exc).__name__
    text = str(exc).lower()
    if "incompleteread" in name.lower() or "timeout" in name.lower():
        return "Сбой сети Binance (proxy) — повтори /signals через 1–2 мин"
    if "connection" in text or "proxy" in text:
        return "Сеть недоступна — повтори /signals позже"
    return f"Probe failed: {name}"


async def _probe_with_retry(symbol: str) -> dict[str, Any]:
    last_exc: BaseException | None = None
    for attempt in range(_PROBE_RETRIES):
        try:
            row = await probe_symbol_signal(symbol, auto_watchlist=False, stagger_ms=120)
            if row.get("error"):
                return row
            return row
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt + 1 < _PROBE_RETRIES:
                await asyncio.sleep(_PROBE_RETRY_DELAY_S * (attempt + 1))
    return {
        "symbol": symbol,
        "error": _human_probe_error(last_exc or RuntimeError("probe_failed")),
    }


def _rollup_touch(
    rollup: _ReportRollup,
    *,
    key: str,
    sig: dict[str, Any],
    direction: str,
    row: dict[str, Any],
) -> None:
    sym = key.partition(":")[0]
    setup = (row.get("dump") if direction == "short" else row.get("long")) or {}
    lc = row.get("lifecycle") or {}
    price = float(row.get("price") or 0)
    pnl = _pnl_pct(sig, direction, price)
    if pnl is not None and pnl > 0:
        rollup.n_plus += 1
    if sig.get("tp1_hit"):
        rollup.n_tp1 += 1
    if evaluate_alert_gate(
        setup, direction=direction, symbol=sym, lifecycle=lc, row=row
    ).ok:
        rollup.n_realert += 1
    phase = str(lc.get("phase") or "")
    if phase == "no_setup":
        rollup.n_stale += 1
    bias = str(lc.get("recommended_bias") or "")
    if (direction == "short" and bias == "long") or (
        direction == "long" and bias == "short"
    ):
        rollup.n_bias_conflict += 1


def _closed_stats(signals: dict[str, Any]) -> tuple[int, int, int]:
    """Wins / losses / lifecycle_stale from closed tracker rows."""
    wins = losses = stale = 0
    win_reasons = {"tp1", "tp2", "fix_profit_tp1", "fix_profit_tp2"}
    for sig in signals.values():
        if not isinstance(sig, dict) or sig.get("status") != "closed":
            continue
        reason = str(sig.get("close_reason") or "")
        if reason in win_reasons:
            wins += 1
        elif reason == "lifecycle_stale":
            stale += 1
        else:
            losses += 1
    return wins, losses, stale


def _format_tg_funnel(*, signals: dict[str, Any]) -> str:
    """Telegram volume vs tracker — explains prep/start spam vs /signals scope."""
    tg: dict[str, str] = {}
    if TELEGRAM_COOLDOWN.is_file():
        import json

        tg = json.loads(TELEGRAM_COOLDOWN.read_text(encoding="utf-8"))

    early_n = sum(1 for k in tg if k.startswith("early:"))
    squeeze_n = sum(1 for k in tg if k.endswith(":squeeze"))
    confirm_cd = sum(
        1 for k in tg if ":" in k and not k.startswith("early") and not k.endswith(":squeeze")
    )
    tracked_tg = sum(
        1 for v in signals.values() if isinstance(v, dict) and v.get("telegram_sent")
    )
    closed_n = sum(
        1 for v in signals.values() if isinstance(v, dict) and v.get("status") == "closed"
    )

    recent_early: list[str] = []
    if SIGNAL_EVENTS.is_file():
        import json

        lines = [
            ln
            for ln in SIGNAL_EVENTS.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ][-400:]
        for ln in reversed(lines):
            ev = json.loads(ln)
            if ev.get("event") not in {"prep", "start", "imminent"}:
                continue
            sym = html.escape(str(ev.get("symbol", "?")).replace("USDT", "-USDT"))
            detail = html.escape(str(ev.get("detail") or "")[:72])
            recent_early.append(
                f"· {sym} <code>{ev.get('direction', '?')}</code> "
                f"<code>{ev.get('event')}</code> — {detail}"
            )
            if len(recent_early) >= 6:
                break

    lines = [
        "<b>TG воронка</b> (не только active tracker):",
        (
            f"early prep/start <code>{early_n}</code> · squeeze <code>{squeeze_n}</code> · "
            f"confirm cooldown <code>{confirm_cd}</code> · "
            f"tracker TG <code>{tracked_tg}</code> · closed <code>{closed_n}</code>"
        ),
        "<i>/signals ниже = только <b>active</b> latch-позиции. "
        "Prep/start в tracker не попадают.</i>",
    ]
    if recent_early:
        lines.append("<b>Последние early TG:</b>")
        lines.extend(recent_early)
    return "\n".join(lines)


def _format_summary(
    rollup: _ReportRollup,
    *,
    n_active: int,
    closed_wins: int,
    closed_losses: int,
    closed_stale: int,
) -> str:
    closed_n = closed_wins + closed_losses + closed_stale
    wr = f" · WR {closed_wins}/{closed_n}" if closed_n else ""
    return (
        f"<b>Сводка:</b> {n_active} поз · {rollup.n_plus} в плюсе · "
        f"{rollup.n_tp1} TP1 · {rollup.n_realert} re-alert · "
        f"{rollup.n_stale} stale · {rollup.n_bias_conflict} bias-конфликт"
        + (f" · {rollup.n_probe_fail} probe fail" if rollup.n_probe_fail else "")
        + wr
        + (f" · closed stale {closed_stale}" if closed_stale else "")
    )


def _format_active_block(
    *,
    key: str,
    sig: dict[str, Any],
    row: dict[str, Any],
    watch_mod: Any,
) -> str:
    sym, _, direction = key.partition(":")
    setup = (row.get("dump") if direction == "short" else row.get("long")) or {}
    lc = row.get("lifecycle") or {}
    price = float(row.get("price") or 0)
    pnl = _pnl_pct(sig, direction, price)

    primary = primary_block_for_report(
        setup, direction=direction, symbol=sym, lifecycle=lc, row=row
    )
    extra = collect_report_blockers(
        setup, direction=direction, symbol=sym, lifecycle=lc, row=row
    )
    secondary = [b for b in extra if b.code != primary.code][:2]
    advice = evaluate_stale_advice(
        symbol=sym, direction=direction, lifecycle=lc, setup=setup, sig=sig
    )

    sym_label = html.escape(sym.replace("USDT", "-USDT"))
    dir_u = direction.upper()
    opened = str(sig.get("opened_at") or "")[:19].replace("T", " ")
    latch_score = sig.get("score") or "—"
    tp1_hit = "✓" if sig.get("tp1_hit") else "—"
    tp2_hit = "✓" if sig.get("tp2_hit") else "—"
    pnl_s = f"{pnl:+.2f}%" if pnl is not None else "—"

    sl_label = _fmt_price(sig.get("stop_loss"))
    if sig.get("sl_at_breakeven"):
        sl_label = f"{sl_label} (entry BE)"

    lines = [
        f"🟢 <b>ОТКРЫТА</b> {sym_label} <code>{dir_u}</code> · PnL <code>{pnl_s}</code> · "
        f"latch <code>{latch_score}</code>",
        (
            f"Открыт <code>{opened}</code> UTC · SL <code>{sl_label}</code> · "
            f"TP1 <code>{_fmt_price(sig.get('tp1'))}</code>{tp1_hit} · "
            f"TP2 <code>{_fmt_price(sig.get('tp2'))}</code>{tp2_hit}"
        ),
    ]
    if sig.get("tp1_hit"):
        pct = sig.get("partial_fixed_pct") or 80
        lines.append(
            f"✅ <b>TP1</b> — зафиксируй <code>{pct}%</code>"
            + (" · SL на entry (безубыток)" if sig.get("sl_at_breakeven") else "")
        )
    lines.append(
        f"Сейчас <code>{watch_mod._fmt_price(price)}</code> · lc "
        f"<code>{html.escape(str(lc.get('phase') or '—'))}</code> · "
        f"bias <code>{html.escape(str(lc.get('recommended_bias') or '—'))}</code>"
    )

    realert_blockers = [
        b for b in extra if b.code not in {"not_confirmed"} or not sig.get("tp1_hit")
    ]
    if primary.ok:
        lines.append("✅ <b>Re-alert</b> сейчас прошёл бы")
    elif realert_blockers:
        lines.append(
            f"<i>Новый вход (re-alert): {html.escape(realert_blockers[0].message)}</i>"
        )
        if len(realert_blockers) > 1:
            more = "; ".join(b.message for b in realert_blockers[1:3])
            lines.append(f"<i>Ещё: {html.escape(more)}</i>")

    if advice:
        lines.append(html.escape(advice))

    if not bool(setup.get("confirmed")):
        hard = [h for h in (setup.get("confirm_hard") or []) if not str(h).startswith("veto_")]
        if hard:
            lines.append(
                "Hard partial: "
                f"<code>{html.escape(', '.join(str(h) for h in hard[:4]))}</code>"
            )

    return "\n".join(lines)


async def build_signals_report_text() -> str:
    state = load_tracker_state()
    signals = state.get("signals") or {}
    active = [
        (k, v)
        for k, v in signals.items()
        if isinstance(v, dict) and v.get("status") == "active"
    ]
    watch_mod = _watch_module()
    row_cache: dict[str, dict[str, Any]] = {}
    rollup = _ReportRollup()
    blocks: list[str] = [
        f"📋 <b>/signals</b> · {datetime.now(UTC).strftime('%H:%M')} UTC",
        _format_tg_funnel(signals=signals),
    ]
    cw, cl, cs = _closed_stats(signals)

    if not active:
        blocks.append(
            f"<b>Active tracker:</b> 0 · closed WR {cw}/{cw + cl + cs}"
            if (cw + cl + cs)
            else "<b>Active tracker:</b> 0"
        )
        blocks.append("<i>Hunt tracker · не auto-trade</i>")
        return "\n\n".join(blocks)

    blocks.append(f"<b>Active tracker</b> · {len(active)} поз.")

    for key, sig in sorted(active, key=lambda x: x[0]):
        sym = key.partition(":")[0]
        if sym not in row_cache:
            row_cache[sym] = await _probe_with_retry(sym)
        row = row_cache[sym]
        if row.get("error"):
            rollup.n_probe_fail += 1
            sym_label = html.escape(sym.replace("USDT", "-USDT"))
            blocks.append(
                f"⚠️ <b>{sym_label}</b>\n<i>{html.escape(str(row['error']))}</i>"
            )
            continue
        direction = key.partition(":")[2] or "short"
        _rollup_touch(rollup, key=key, sig=sig, direction=direction, row=row)
        blocks.append(_format_active_block(key=key, sig=sig, row=row, watch_mod=watch_mod))

    blocks.append(
        _format_summary(
            rollup,
            n_active=len(active),
            closed_wins=cw,
            closed_losses=cl,
            closed_stale=cs,
        ),
    )
    blocks.append("<i>live REST probe · latch = score при открытии</i>")
    blocks.append("<i>Hunt tracker · не auto-trade</i>")
    return "\n\n".join(blocks)


async def deliver_signals_report(broadcaster: TelegramBroadcaster) -> None:
    from hunt_watch.symbol_probe import _watch_module as _wm

    watch_mod = _wm()
    text = await build_signals_report_text()
    for part in watch_mod._split_telegram(text):
        await broadcaster.send_html(part)
