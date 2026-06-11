"""Shadow tracker for prep / imminent / start — direction quality + paper PnL.

Records early-alert tiers **without Telegram** (EARLY_TELEGRAM_ENABLED may stay off).
Each open shadow tracks MFE/MAE, TP1/SL touch, confirm funnel, and closes at horizon
for calibration: which prep tier × phase predicted move correctly.

Paper PnL is hypothetical (entry at prep tick, exit at TP1 / SL / horizon).
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from hunt_watch.early_alert import (
    early_cooldown_ok,
    evaluate_early_alert,
    mark_early_sent,
)
from hunt_watch.param_store import prep_shadow_thresholds, stats_thresholds
from hunt_watch.paths import DATA, PREP_SHADOW_EVENTS, PREP_SHADOW_STATE

CloseReason = Literal[
    "horizon_expired",
    "tp1_hit",
    "stop_hit",
    "superseded",
    "direction_fail",
]


def load_prep_shadow_state(path: Path = PREP_SHADOW_STATE) -> dict[str, Any]:
    if not path.exists():
        return {"active": {}, "closed": [], "cooldowns": {}, "stats": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw.setdefault("active", {})
            raw.setdefault("closed", [])
            raw.setdefault("cooldowns", {})
            return raw
    except (OSError, json.JSONDecodeError):
        pass
    return {"active": {}, "closed": [], "cooldowns": {}, "stats": {}}


def save_prep_shadow_state(state: dict[str, Any], path: Path = PREP_SHADOW_STATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    closed = state.get("closed") or []
    if len(closed) > 500:
        state["closed"] = closed[-500:]
    path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def _append_event(event: str, *, shadow: dict[str, Any], extra: dict[str, Any] | None = None) -> None:
    row = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event,
        "shadow_id": shadow.get("id"),
        "symbol": shadow.get("symbol"),
        "direction": shadow.get("direction"),
        "tier": shadow.get("tier"),
        "payload": {**(extra or {}), "paper_pnl_pct": shadow.get("paper_pnl_pct")},
    }
    PREP_SHADOW_EVENTS.parent.mkdir(parents=True, exist_ok=True)
    with PREP_SHADOW_EVENTS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def _pnl_pct(direction: str, entry: float, exit_px: float) -> float:
    if entry <= 0 or exit_px <= 0:
        return 0.0
    if direction == "short":
        return (entry - exit_px) / entry * 100.0
    return (exit_px - entry) / entry * 100.0


def _mfe_mae(direction: str, entry: float, hi: float, lo: float) -> tuple[float, float]:
    if entry <= 0:
        return 0.0, 0.0
    if direction == "short":
        mfe = max(0.0, (entry - lo) / entry * 100.0)
        mae = max(0.0, (hi - entry) / entry * 100.0)
    else:
        mfe = max(0.0, (hi - entry) / entry * 100.0)
        mae = max(0.0, (entry - lo) / entry * 100.0)
    return round(mfe, 3), round(mae, 3)


def _bar_extremes(row: dict[str, Any], price: float) -> tuple[float, float]:
    hi = lo = price
    tf = row.get("timeframes") or {}
    for key in ("1m_closed", "5m_closed", "15m_closed"):
        candle = (tf.get(key) or {}).get("candle") or {}
        try:
            c_hi = float(candle.get("high") or 0)
            c_lo = float(candle.get("low") or 0)
        except (TypeError, ValueError):
            continue
        if c_hi > 0:
            hi = max(hi, c_hi)
        if c_lo > 0:
            lo = min(lo, c_lo)
    return hi, lo


def _active_key(symbol: str, direction: str) -> str:
    return f"{symbol.upper()}:{direction.lower()}"


def _parse_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _close_shadow(
    state: dict[str, Any],
    shadow: dict[str, Any],
    *,
    reason: CloseReason,
    exit_price: float,
    now: datetime,
) -> dict[str, Any]:
    cfg = prep_shadow_thresholds()
    direction = str(shadow.get("direction") or "")
    entry = float(shadow.get("entry_price") or 0)
    mfe = float(shadow.get("mfe_pct") or 0)
    mae = float(shadow.get("mae_pct") or 0)
    ok_min = float(cfg.get("direction_ok_min_mfe_pct", 2.0))
    fail_mae = float(cfg.get("wrong_direction_mae_pct", 2.5))

    if reason == "direction_fail":
        direction_ok = False
    elif reason in {"tp1_hit", "stop_hit"}:
        direction_ok = reason == "tp1_hit"
    else:
        direction_ok = mfe >= ok_min and mae < fail_mae * 1.5

    shadow["status"] = "closed"
    shadow["closed_at"] = now.isoformat()
    shadow["close_reason"] = reason
    shadow["exit_price"] = round(exit_price, 8)
    shadow["paper_pnl_pct"] = round(_pnl_pct(direction, entry, exit_price), 3)
    shadow["direction_correct"] = direction_ok
    shadow["duration_min"] = round(
        (now - (_parse_ts(str(shadow.get("opened_at") or "")) or now)).total_seconds() / 60.0,
        1,
    )

    active = state.setdefault("active", {})
    active.pop(_active_key(str(shadow.get("symbol") or ""), direction), None)
    closed = state.setdefault("closed", [])
    closed.append(dict(shadow))
    _append_event("closed", shadow=shadow, extra={"reason": reason, "direction_ok": direction_ok})
    return shadow


def _update_active_shadow(
    shadow: dict[str, Any],
    *,
    price: float,
    row: dict[str, Any],
    setup: dict[str, Any],
    now: datetime,
) -> CloseReason | None:
    if price <= 0:
        return None
    direction = str(shadow.get("direction") or "")
    entry = float(shadow.get("entry_price") or 0)
    hi_bar, lo_bar = _bar_extremes(row, price)
    peak = max(float(shadow.get("peak_price") or entry), price, hi_bar)
    trough = min(float(shadow.get("trough_price") or entry), price, lo_bar)
    shadow["peak_price"] = peak
    shadow["trough_price"] = trough
    mfe, mae = _mfe_mae(direction, entry, peak, trough)
    shadow["mfe_pct"] = mfe
    shadow["mae_pct"] = mae
    shadow["last_price"] = price
    shadow["last_checked_at"] = now.isoformat()

    if bool(setup.get("confirmed")):
        shadow["confirmed_later"] = True

    sl = float(shadow.get("stop_loss") or 0)
    tp1 = float(shadow.get("tp1") or 0)
    if direction == "short":
        if sl > 0 and peak >= sl:
            return "stop_hit"
        if tp1 > 0 and trough <= tp1:
            return "tp1_hit"
    else:
        if sl > 0 and trough <= sl:
            return "stop_hit"
        if tp1 > 0 and peak >= tp1:
            return "tp1_hit"

    cfg = prep_shadow_thresholds()
    fail_mae = float(cfg.get("wrong_direction_mae_pct", 2.5))
    ok_min = float(cfg.get("direction_ok_min_mfe_pct", 2.0))
    if mae >= fail_mae and mfe < ok_min * 0.5:
        return "direction_fail"

    opened = _parse_ts(str(shadow.get("opened_at") or ""))
    horizon_h = float(cfg.get("horizon_hours", stats_thresholds().get("forward_horizon_hours", 8.0)))
    if opened and now - opened >= timedelta(hours=horizon_h):
        return "horizon_expired"
    return None


def _open_shadow(
    state: dict[str, Any],
    *,
    symbol: str,
    direction: str,
    tier: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    lifecycle: dict[str, Any],
    alert_message: str,
    now: datetime,
) -> dict[str, Any]:
    cfg = prep_shadow_thresholds()
    price = float(row.get("price") or 0)
    sym = symbol.upper()
    key = _active_key(sym, direction)
    active = state.setdefault("active", {})

    prev = active.get(key)
    if prev and prev.get("status") == "active":
        _close_shadow(
            state,
            prev,
            reason="superseded",
            exit_price=price,
            now=now,
        )

    fuel_key = "dump_fuel" if direction == "short" else "long_fuel"
    shadow_id = f"{sym}:{direction}:{tier}:{uuid.uuid4().hex[:8]}"
    shadow: dict[str, Any] = {
        "id": shadow_id,
        "symbol": sym,
        "direction": direction,
        "tier": tier,
        "status": "active",
        "opened_at": now.isoformat(),
        "entry_price": price,
        "fuel": float(setup.get(fuel_key) or setup.get("dump_score" if direction == "short" else "long_score") or 0),
        "lifecycle_phase": str(lifecycle.get("phase") or ""),
        "setup_phase": str(setup.get("phase") or ""),
        "alert_message": alert_message,
        "stop_loss": setup.get("stop_loss"),
        "tp1": setup.get("tp1"),
        "tp2": setup.get("tp2"),
        "risk_reward": setup.get("risk_reward"),
        "peak_price": price,
        "trough_price": price,
        "mfe_pct": 0.0,
        "mae_pct": 0.0,
        "confirmed_later": bool(setup.get("confirmed")),
        "triggers": list(setup.get("triggers") or [])[:8],
    }
    active[key] = shadow
    _append_event("opened", shadow=shadow, extra={"message": alert_message})
    return shadow


def process_prep_shadow(
    state: dict[str, Any],
    *,
    symbol: str,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    lifecycle: Any | None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Update open shadows and maybe record a new prep/imminent/start event."""
    cfg = prep_shadow_thresholds()
    if not cfg.get("enabled", True):
        return []

    ts = now or datetime.now(UTC)
    sym = symbol.upper()
    price = float(row.get("price") or 0)
    if price <= 0 or not setup:
        return []

    lc = lifecycle if isinstance(lifecycle, dict) else {}
    closed_events: list[dict[str, Any]] = []
    key = _active_key(sym, direction)
    active = state.setdefault("active", {})
    cooldowns = state.setdefault("cooldowns", {})

    cur = active.get(key)
    if cur and cur.get("status") == "active":
        reason = _update_active_shadow(cur, price=price, row=row, setup=setup, now=ts)
        if reason:
            closed_events.append(
                _close_shadow(state, cur, reason=reason, exit_price=price, now=ts)
            )

    alert = evaluate_early_alert(
        setup,
        direction=direction,
        symbol=sym,
        lifecycle=lc,
        row=row,
    )
    if alert.kind in ("none", "confirm"):
        return closed_events
    if not early_cooldown_ok(sym, direction, alert.tier, cooldowns, now=ts):
        return closed_events

    min_tier_rank = ("prep", "imminent", "start").index(str(cfg.get("min_tier", "prep")))
    tier_rank = ("prep", "imminent", "start").index(alert.tier) if alert.tier in ("prep", "imminent", "start") else 0
    if tier_rank < min_tier_rank:
        return closed_events

    mark_early_sent(sym, direction, alert.tier, cooldowns, now=ts)
    opened = _open_shadow(
        state,
        symbol=sym,
        direction=direction,
        tier=alert.tier,
        setup=setup,
        row=row,
        lifecycle=lc,
        alert_message=alert.message,
        now=ts,
    )
    closed_events.append(opened)
    return closed_events


@dataclass(frozen=True, slots=True)
class PrepShadowSummary:
    n_closed: int
    n_active: int
    direction_wr: float | None
    avg_mfe: float | None
    avg_paper_pnl: float | None
    confirm_rate: float | None
    by_tier: dict[str, dict[str, Any]]
    by_phase: dict[str, dict[str, Any]]


def summarize_prep_shadows(
    state: dict[str, Any] | None = None,
    *,
    last_n: int = 200,
) -> PrepShadowSummary:
    state = state or load_prep_shadow_state()
    closed = list(state.get("closed") or [])[-last_n:]
    active = state.get("active") or {}

    def _bucket(rows: list[dict[str, Any]]) -> dict[str, Any]:
        if not rows:
            return {"n": 0, "wr": None, "avg_mfe": None, "avg_pnl": None}
        wins = sum(1 for r in rows if r.get("direction_correct"))
        mfe = [float(r["mfe_pct"]) for r in rows if r.get("mfe_pct") is not None]
        pnl = [float(r["paper_pnl_pct"]) for r in rows if r.get("paper_pnl_pct") is not None]
        n = len(rows)
        return {
            "n": n,
            "wr": round(wins / n * 100.0, 1) if n else None,
            "avg_mfe": round(sum(mfe) / len(mfe), 2) if mfe else None,
            "avg_pnl": round(sum(pnl) / len(pnl), 2) if pnl else None,
        }

    labeled = [r for r in closed if r.get("direction_correct") is not None]
    wins = sum(1 for r in labeled if r.get("direction_correct"))
    all_mfe = [float(r["mfe_pct"]) for r in closed if r.get("mfe_pct") is not None]
    all_pnl = [float(r["paper_pnl_pct"]) for r in closed if r.get("paper_pnl_pct") is not None]
    conf = [r for r in closed if r.get("confirmed_later")]

    by_tier: dict[str, dict[str, Any]] = {}
    tier_groups: dict[str, list] = defaultdict(list)
    for r in closed:
        tier_groups[str(r.get("tier") or "?")].append(r)
    for tier, rows in tier_groups.items():
        by_tier[tier] = _bucket(rows)

    by_phase: dict[str, dict[str, Any]] = {}
    phase_groups: dict[str, list] = defaultdict(list)
    for r in closed:
        phase_groups[str(r.get("lifecycle_phase") or "?")].append(r)
    for phase, rows in phase_groups.items():
        by_phase[phase] = _bucket(rows)

    return PrepShadowSummary(
        n_closed=len(closed),
        n_active=len(active),
        direction_wr=round(wins / len(labeled) * 100.0, 1) if labeled else None,
        avg_mfe=round(sum(all_mfe) / len(all_mfe), 2) if all_mfe else None,
        avg_paper_pnl=round(sum(all_pnl) / len(all_pnl), 2) if all_pnl else None,
        confirm_rate=round(len(conf) / len(closed) * 100.0, 1) if closed else None,
        by_tier=by_tier,
        by_phase=by_phase,
    )


def format_prep_shadow_html(summary: PrepShadowSummary | None = None) -> str:
    s = summary or summarize_prep_shadows()
    lines = [
        "<b>Prep shadow</b> — paper-трекинг prep/start без TG",
        f"Active <code>{s.n_active}</code> · closed <code>{s.n_closed}</code>",
    ]
    if s.direction_wr is not None:
        lines.append(
            f"Direction WR <code>{s.direction_wr}%</code> · "
            f"avg MFE <code>{s.avg_mfe}%</code> · paper PnL <code>{s.avg_paper_pnl}%</code>"
        )
    if s.confirm_rate is not None:
        lines.append(f"→ confirm funnel <code>{s.confirm_rate}%</code>")
    if s.by_tier:
        tier_txt = " · ".join(
            f"{t}: {b['wr']}% (n={b['n']})"
            for t, b in sorted(s.by_tier.items())
            if b.get("n")
        )
        if tier_txt:
            lines.append(f"<b>By tier:</b> {tier_txt}")
    top_phases = sorted(
        ((p, b) for p, b in s.by_phase.items() if b.get("n", 0) >= 2),
        key=lambda x: -(x[1].get("wr") or 0),
    )[:4]
    if top_phases:
        ph_txt = " · ".join(f"{p} {b['wr']}% (n={b['n']})" for p, b in top_phases)
        lines.append(f"<b>Phase:</b> {ph_txt}")
    lines.append("<i>Shadow mode — калибровка охотника, не auto-trade</i>")
    return "\n".join(lines)


def format_prep_shadow_text(summary: PrepShadowSummary | None = None) -> str:
    return format_prep_shadow_html(summary).replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", "").replace("<i>", "").replace("</i>", "")


def prep_shadow_delivery_fuel_adjustment(
    state: dict[str, Any] | None = None,
) -> tuple[float, str | None]:
    """Rolling prep-shadow WR → tighten/relax delivery min_fuel (target 70% WR)."""
    cfg = prep_shadow_thresholds()
    min_n = int(cfg.get("delivery_min_samples", 8))
    wr_floor = float(cfg.get("delivery_wr_floor_pct", 50.0))
    wr_relax = float(cfg.get("delivery_wr_relax_pct", 62.0))
    bump = float(cfg.get("delivery_fuel_bump", 3.0))
    relax = float(cfg.get("delivery_fuel_relax", 1.0))

    summary = summarize_prep_shadows(state)
    if summary.n_closed < min_n or summary.direction_wr is None:
        return 0.0, None
    wr = float(summary.direction_wr)
    if wr < wr_floor:
        return bump, f"prep shadow WR {wr:.0f}% < {wr_floor:.0f}% (n={summary.n_closed})"
    if wr >= wr_relax:
        return -relax, f"prep shadow WR {wr:.0f}% ≥ {wr_relax:.0f}% — relax"
    return 0.0, None
