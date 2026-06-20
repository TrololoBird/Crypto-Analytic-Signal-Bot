"""Shadow tracker for prep / imminent / start — direction quality + paper PnL.

Records early-alert tiers **without Telegram** (EARLY_TELEGRAM_ENABLED may stay off).
Each open shadow tracks MFE/MAE, TP1/SL touch, confirm funnel, and closes at horizon
for calibration: which prep tier × phase predicted move correctly.

Paper PnL is hypothetical (entry at prep tick, exit at TP1 / SL / horizon).
"""
from __future__ import annotations



import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import polars as pl

from hunt_core.params.store import prep_shadow_thresholds, stats_thresholds
from hunt_core.paths import PREP_SHADOW_EVENTS, PREP_SHADOW_STATE

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
        "payload": {**(extra or {}), "paper_pnl_pct": shadow.get("paper_pnl_pct", 0.0)},
    }
    from hunt_core.track.events import _append_jsonl_line

    _append_jsonl_line(PREP_SHADOW_EVENTS, json.dumps(row, default=str) + "\n")


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
    prep_shadow_thresholds()
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
        "score": float(setup.get("dump_score" if direction == "short" else "long_score") or 0),
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
        "paper_pnl_pct": 0.0,
        "confirmed_later": bool(setup.get("confirmed")),
        "triggers": list(setup.get("triggers") or [])[:8],
    }
    active[key] = shadow
    _append_event("opened", shadow=shadow, extra={"message": alert_message})
    return shadow


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
    by_fuel: dict[str, dict[str, Any]]
    by_score: dict[str, dict[str, Any]]


def summarize_prep_shadows(
    state: dict[str, Any] | None = None,
    *,
    last_n: int = 200,
) -> PrepShadowSummary:
    state = state or load_prep_shadow_state()
    closed = list(state.get("closed") or [])[-last_n:]
    active = state.get("active") or {}

    def _bucket_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
        if not rows:
            return {"n": 0, "wr": None, "avg_mfe": None, "avg_pnl": None}
        df = pl.DataFrame(rows)
        n = df.height
        wins = 0
        if "direction_correct" in df.columns:
            wins = int(df.filter(pl.col("direction_correct") == True).height)  # noqa: E712
        avg_mfe = None
        if "mfe_pct" in df.columns:
            mfe = df.filter(pl.col("mfe_pct").is_not_null())["mfe_pct"].cast(pl.Float64)
            avg_mfe = round(float(mfe.mean()), 2) if mfe.len() else None
        avg_pnl = None
        if "paper_pnl_pct" in df.columns:
            pnl = df.filter(pl.col("paper_pnl_pct").is_not_null())["paper_pnl_pct"].cast(pl.Float64)
            avg_pnl = round(float(pnl.mean()), 2) if pnl.len() else None
        return {
            "n": n,
            "wr": round(wins / n * 100.0, 1) if n else None,
            "avg_mfe": avg_mfe,
            "avg_pnl": avg_pnl,
        }

    def _group_summary(rows: list[dict[str, Any]], group_col: str) -> dict[str, dict[str, Any]]:
        if not rows:
            return {}
        df = pl.DataFrame(rows).with_columns(pl.col(group_col).cast(pl.Utf8).fill_null("?"))
        out: dict[str, dict[str, Any]] = {}
        for key in df[group_col].unique().sort().to_list():
            sub = df.filter(pl.col(group_col) == key).to_dicts()
            out[str(key)] = _bucket_rows(sub)
        return out

    labeled = [r for r in closed if r.get("direction_correct") is not None]
    wins = sum(1 for r in labeled if r.get("direction_correct"))
    all_mfe = [float(r["mfe_pct"]) for r in closed if r.get("mfe_pct") is not None]
    all_pnl = [float(r["paper_pnl_pct"]) for r in closed if r.get("paper_pnl_pct") is not None]
    conf = [r for r in closed if r.get("confirmed_later")]

    by_tier = _group_summary(closed, "tier")
    by_phase = _group_summary(
        [{**r, "lifecycle_phase": str(r.get("lifecycle_phase") or "?")} for r in closed],
        "lifecycle_phase",
    )

    fuel_rows: list[dict[str, Any]] = []
    for r in closed:
        fuel = int(r.get("fuel") or 0)
        lo = (fuel // 16) * 16
        fuel_rows.append({**r, "fuel_bucket": f"{lo}-{lo + 15}"})
    by_fuel = _group_summary(fuel_rows, "fuel_bucket")

    score_rows: list[dict[str, Any]] = []
    for r in closed:
        sc = int(float(r.get("score") or 0))
        if sc > 0:
            lo = (sc // 20) * 20
            score_rows.append({**r, "score_bucket": f"{lo}-{lo + 19}"})
    by_score = _group_summary(score_rows, "score_bucket")

    return PrepShadowSummary(
        n_closed=len(closed),
        n_active=len(active),
        direction_wr=round(wins / len(labeled) * 100.0, 1) if labeled else None,
        avg_mfe=round(sum(all_mfe) / len(all_mfe), 2) if all_mfe else None,
        avg_paper_pnl=round(sum(all_pnl) / len(all_pnl), 2) if all_pnl else None,
        confirm_rate=round(len(conf) / len(closed) * 100.0, 1) if closed else None,
        by_tier=by_tier,
        by_phase=by_phase,
        by_fuel=by_fuel,
        by_score=by_score,
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
    if s.by_fuel:
        fuel_txt = " · ".join(
            f"fuel{bkt}: {b['wr']}% (n={b['n']})"
            for bkt, b in sorted(s.by_fuel.items())
            if b.get("n", 0) >= 3
        )
        if fuel_txt:
            lines.append(f"<b>By fuel:</b> {fuel_txt}")
    if s.by_score:
        score_txt = " · ".join(
            f"sc{bkt}: {b['wr']}% (n={b['n']})"
            for bkt, b in sorted(s.by_score.items())
            if b.get("n", 0) >= 3
        )
        if score_txt:
            lines.append(f"<b>By score:</b> {score_txt}")
    lines.append("<i>Shadow mode — калибровка охотника, не auto-trade</i>")
    return "\n".join(lines)


def format_prep_shadow_text(summary: PrepShadowSummary | None = None) -> str:
    return format_prep_shadow_html(summary).replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", "").replace("<i>", "").replace("</i>", "")
