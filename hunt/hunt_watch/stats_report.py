"""Telegram /stats — tracker WR, phase matrix, TG funnel, regime, confidence."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from hunt_core.telegram import TelegramBroadcaster

from hunt_watch.market_regime import active_params
from hunt_watch.param_store import effective_hunt_params
from hunt_watch.paths import DATA, MARKET_REGIME, SIGNAL_EVENTS, SIGNAL_STATE, TELEGRAM_COOLDOWN
from hunt_watch.signal_tracker import load_tracker_state
from hunt_watch.signals_report import _closed_stats, _format_tg_funnel
from hunt_watch.tracker_outcomes import (
    LEGACY_UNKNOWN,
    LOSS_REASONS,
    WIN_REASONS,
    entry_lifecycle_phase,
    outcome_kind,
)

TG_BACKTEST_PATH = DATA / "session" / "tg_backtest_report.json"


def confidence_tier(n_labeled: int) -> str:
    """Plain-text tier labels — safe for Telegram HTML (no raw '<')."""
    if n_labeled < 30:
        return "exploratory (n≤29)"
    if n_labeled < 50:
        return "early (30–49)"
    if n_labeled < 100:
        return "conservative (50–99)"
    if n_labeled < 200:
        return "calibrated (100–199)"
    return "production (n≥200)"


def bayesian_wr_ci(*, wins: int, n: int) -> str:
    """Beta(2,2) prior — 95% credible interval for win rate."""
    if n <= 0:
        return "—"
    a = 2 + wins
    b = 2 + (n - wins)
    mean = a / (a + b)
    var = (a * b) / ((a + b) ** 2 * (a + b + 1))
    sd = math.sqrt(var)
    lo = max(0.0, mean - 1.96 * sd)
    hi = min(1.0, mean + 1.96 * sd)
    return f"{mean * 100:.0f}% [{lo * 100:.0f}–{hi * 100:.0f}%]"


def _labeled_closed(signals: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sig in signals.values():
        if not isinstance(sig, dict) or sig.get("status") != "closed":
            continue
        reason = str(sig.get("close_reason") or "unknown")
        if reason == "unknown" or sig.get("pnl_pct") is None:
            continue
        out.append(sig)
    return out


def _phase_matrix(closed: list[dict[str, Any]]) -> list[str]:
    phased: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for r in closed:
        phase = entry_lifecycle_phase(r)
        direction = str(r.get("direction") or "?")
        reason = str(r.get("close_reason") or "unknown")
        pnl = r.get("pnl_pct")
        kind = outcome_kind(reason, pnl_pct=float(pnl) if pnl is not None else None)
        phased[(phase, direction)][kind].append(float(pnl) if pnl is not None else 0.0)

    lines = ["<b>Phase × direction</b> (closed):"]
    for (phase, direction), b in sorted(phased.items()):
        w, l, u = len(b["win"]), len(b["loss"]), len(b["unknown"])
        known = w + l
        wr = f"{w / known * 100:.0f}%" if known else "—"
        pnls = b["win"] + b["loss"] + b["unknown"]
        avg = sum(pnls) / len(pnls) if pnls else 0.0
        lines.append(
            f"· <code>{phase[:18]}</code> {direction} "
            f"n={w + l + u} WR {wr} avg {avg:+.1f}%"
        )
    if len(lines) == 1:
        lines.append("· нет закрытых с исходом")
    return lines


def _regime_block() -> str:
    if not MARKET_REGIME.is_file():
        return "<b>Regime:</b> нет snapshot"
    try:
        snap = json.loads(MARKET_REGIME.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "<b>Regime:</b> read error"
    regime = active_params()
    eff = effective_hunt_params()
    return (
        f"<b>Regime:</b> <code>{snap.get('regime', regime.regime)}</code> · "
        f"confirm_min <code>{eff.confirm_min_score:.0f}</code> "
        f"(cal) · adx_block <code>{eff.adx_trend_block:.0f}</code> · "
        f"n_liquid <code>{snap.get('n_liquid', '?')}</code>"
    )


def _backtest_snippet() -> str | None:
    if not TG_BACKTEST_PATH.is_file():
        return None
    try:
        rep = json.loads(TG_BACKTEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    gen = rep.get("generated_at") or rep.get("ts")
    if gen:
        try:
            dt = datetime.fromisoformat(str(gen).replace("Z", "+00:00"))
            if datetime.now(UTC) - dt > timedelta(hours=24):
                return None
        except ValueError:
            pass
    co = rep.get("confirmed_outcomes") or {}
    if isinstance(co, dict) and co:
        w = int(co.get("win", 0))
        l = int(co.get("loss", 0))
        f = int(co.get("flat", 0))
        return f"<b>TG backtest (&lt;24h):</b> confirm {w}W/{l}L/{f}F"
    summary = rep.get("summary") or rep
    confirmed = summary.get("confirmed") or summary.get("by_kind", {}).get("confirmed") or {}
    if isinstance(confirmed, dict) and confirmed:
        w = confirmed.get("win", confirmed.get("wins", 0))
        l = confirmed.get("loss", confirmed.get("losses", 0))
        f = confirmed.get("flat", 0)
        return f"<b>TG backtest (&lt;24h):</b> win {w} · loss {l} · flat {f}"
    return None


def _confirmed_events_count() -> int:
    if not SIGNAL_EVENTS.is_file():
        return 0
    n = 0
    for ln in SIGNAL_EVENTS.read_text(encoding="utf-8").splitlines()[-2000:]:
        if not ln.strip():
            continue
        try:
            ev = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if ev.get("event") == "confirmed":
            n += 1
    return n


def _score_floor_block(labeled: list[dict[str, Any]]) -> str | None:
    floor = effective_hunt_params().confirm_min_score
    below = [
        r
        for r in labeled
        if r.get("score") is not None and float(r["score"]) < floor
    ]
    if not below:
        return None
    kinds = [
        outcome_kind(
            str(r.get("close_reason") or ""),
            pnl_pct=float(r["pnl_pct"]) if r.get("pnl_pct") is not None else None,
        )
        for r in below
    ]
    bw = sum(1 for k in kinds if k == "win")
    bl = sum(1 for k in kinds if k == "loss")
    return (
        f"<b>Below confirm_min {floor:.0f}:</b> "
        f"<code>{len(below)}</code> trades · {bw}W/{bl}L "
        f"<i>(не открывались бы сегодня)</i>"
    )


def build_stats_report_text() -> str:
    state = load_tracker_state()
    signals = state.get("signals") or {}
    rows = [v for v in signals.values() if isinstance(v, dict)]
    active = [r for r in rows if r.get("status") == "active"]
    closed_all = [r for r in rows if r.get("status") == "closed"]
    labeled = _labeled_closed(signals)
    n_labeled = len(labeled)
    kinds = [
        outcome_kind(
            str(r.get("close_reason") or ""),
            pnl_pct=float(r["pnl_pct"]) if r.get("pnl_pct") is not None else None,
        )
        for r in labeled
    ]
    wins = sum(1 for k in kinds if k == "win")
    losses = sum(1 for k in kinds if k == "loss")
    legacy_n = sum(1 for r in labeled if str(r.get("close_reason")) == LEGACY_UNKNOWN)

    pnls = [float(r["pnl_pct"]) for r in labeled if r.get("pnl_pct") is not None]
    avg_pnl = sum(pnls) / len(pnls) if pnls else 0.0
    durs = sorted(float(r.get("duration_min") or 0) for r in closed_all if r.get("duration_min"))
    med_dur = durs[len(durs) // 2] if durs else 0.0

    cw, cl, cs = _closed_stats(signals)
    reset_at = state.get("baseline_reset_at")
    blocks: list[str] = [
        f"📊 <b>/stats</b> · {datetime.now(UTC).strftime('%H:%M')} UTC",
        (
            f"<b>Tracker:</b> active <code>{len(active)}</code> · "
            f"closed <code>{len(closed_all)}</code> · "
            f"labeled <code>{n_labeled}</code>"
        ),
        (
            f"<b>WR (PnL):</b> {wins}W / {losses}L"
            + (f" · legacy <code>{legacy_n}</code>" if legacy_n else "")
            + f" · avg PnL <code>{avg_pnl:+.2f}%</code> · "
            f"median dur <code>{med_dur:.0f}m</code>"
        ),
        f"<b>Confidence:</b> {confidence_tier(wins + losses)} · "
        f"Bayesian WR {bayesian_wr_ci(wins=wins, n=wins + losses)}",
        f"<b>Closed (structural):</b> win {cw} · loss {cl} · stale {cs}",
        _regime_block(),
        f"<b>signal_events confirmed:</b> <code>{_confirmed_events_count()}</code>",
    ]
    if reset_at:
        blocks.append(
            f"<i>Baseline с <code>{str(reset_at)[:16]}</code> UTC — старые outcomes в archive/</i>"
        )
    sf = _score_floor_block(labeled)
    if sf:
        blocks.append(sf)
    blocks.extend(_phase_matrix(labeled))
    from hunt_watch.phase_matrix_gate import disabled_phase_pairs

    disabled = disabled_phase_pairs()
    if disabled:
        lines = ["<b>Phase auto-off</b> (WR under 25%, n≥10):"]
        for (phase, direction), st in sorted(disabled.items()):
            lines.append(
                f"· <code>{phase[:18]}</code> {direction} "
                f"WR {st.wr * 100:.0f}% n={st.n}"
            )
        blocks.append("\n".join(lines))
    blocks.append(_format_tg_funnel(signals=signals))
    bt = _backtest_snippet()
    if bt:
        blocks.append(bt)
    blocks.append("<i>Hunt stats · read-only · не auto-trade</i>")
    return "\n\n".join(blocks)


async def deliver_stats_report(broadcaster: TelegramBroadcaster) -> None:
    from hunt_watch.symbol_probe import _watch_module as _wm

    text = build_stats_report_text()
    for part in _wm()._split_telegram(text):
        await broadcaster.send_html(part)
