"""Paper-track compressions, forming, blocked, and ignition setups per symbol.

Tracks MFE/MAE and TP1/SL outcomes for setups that never became confirm TG —
calibration input for per-symbol level stats.
"""
from __future__ import annotations



import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from hunt_core.paths import SETUP_CANDIDATES_EVENTS, SETUP_CANDIDATES_STATE
from hunt_core.features.prepare_columns import feature_vector_from_row

CloseReason = Literal[
    "horizon_expired",
    "tp1_hit",
    "stop_hit",
    "superseded",
    "direction_fail",
    "gate_blocked",
    "promoted_to_confirm",
]

HORIZON_HOURS = 8.0


def load_setup_candidates_state(path: Path = SETUP_CANDIDATES_STATE) -> dict[str, Any]:
    if not path.exists():
        return {"active": {}, "rollup": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw.setdefault("active", {})
            raw.setdefault("rollup", {})
            return raw
    except (OSError, json.JSONDecodeError):
        pass
    return {"active": {}, "rollup": {}}


def save_setup_candidates_state(state: dict[str, Any], path: Path = SETUP_CANDIDATES_STATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def _active_key(symbol: str, direction: str) -> str:
    return f"{symbol.upper()}:{direction.lower()}"


def _append_event(event: str, *, candidate: dict[str, Any], extra: dict[str, Any] | None = None) -> None:
    row = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event,
        "candidate_id": candidate.get("id"),
        "symbol": candidate.get("symbol"),
        "direction": candidate.get("direction"),
        "stage": candidate.get("stage"),
        "source": candidate.get("source"),
        "payload": {**(extra or {}), "paper_pnl_pct": candidate.get("paper_pnl_pct")},
    }
    from hunt_core.track.events import _append_jsonl_line

    _append_jsonl_line(SETUP_CANDIDATES_EVENTS, json.dumps(row, default=str) + "\n")


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


def _fallback_levels(
    price: float, *, direction: str, row: dict[str, Any]
) -> tuple[float, float, float]:
    atr = float(((row.get("timeframes") or {}).get("15m") or {}).get("atr14") or 0)
    if atr <= 0:
        atr = price * 0.02
    if direction == "short":
        return price + atr * 2.0, price - atr * 2.0, price - atr * 4.0
    return price - atr * 2.0, price + atr * 2.0, price + atr * 4.0


def _levels_from_setup(
    setup: dict[str, Any], *, direction: str, price: float, row: dict[str, Any]
) -> tuple[float, float, float]:
    sl = float(setup.get("stop_loss") or 0)
    tp1 = float(setup.get("tp1") or 0)
    tp2 = float(setup.get("tp2") or 0)
    if sl > 0 and tp1 > 0:
        return sl, tp1, tp2 if tp2 > 0 else tp1 * (0.98 if direction == "short" else 1.02)
    return _fallback_levels(price, direction=direction, row=row)


def _update_rollup(state: dict[str, Any], closed: dict[str, Any]) -> None:
    sym = str(closed.get("symbol") or "").upper()
    if not sym:
        return
    rollup = state.setdefault("rollup", {}).setdefault(sym, {
        "n_squeeze": 0,
        "n_prep": 0,
        "n_forming": 0,
        "n_blocked": 0,
        "n_confirmed": 0,
        "n_closed": 0,
        "wins": 0,
        "avg_mfe": 0.0,
        "avg_mae": 0.0,
        "avg_paper_pnl": 0.0,
        "median_tp1_dist_pct": None,
        "median_sl_dist_pct": None,
    })
    stage = str(closed.get("stage") or "")
    source = str(closed.get("source") or "")
    if "squeeze" in source or stage == "compression":
        rollup["n_squeeze"] = int(rollup.get("n_squeeze") or 0) + 1
    elif stage in {"prep", "imminent", "start"}:
        rollup["n_prep"] = int(rollup.get("n_prep") or 0) + 1
    elif stage == "forming":
        rollup["n_forming"] = int(rollup.get("n_forming") or 0) + 1
    elif stage == "blocked":
        rollup["n_blocked"] = int(rollup.get("n_blocked") or 0) + 1
    if closed.get("close_reason") == "promoted_to_confirm":
        rollup["n_confirmed"] = int(rollup.get("n_confirmed") or 0) + 1

    n = int(rollup.get("n_closed") or 0)
    pnl = float(closed.get("paper_pnl_pct") or 0)
    mfe = float(closed.get("mfe_pct") or 0)
    mae = float(closed.get("mae_pct") or 0)
    rollup["n_closed"] = n + 1
    rollup["avg_paper_pnl"] = round(
        (float(rollup.get("avg_paper_pnl") or 0) * n + pnl) / (n + 1), 3
    )
    rollup["avg_mfe"] = round((float(rollup.get("avg_mfe") or 0) * n + mfe) / (n + 1), 3)
    rollup["avg_mae"] = round((float(rollup.get("avg_mae") or 0) * n + mae) / (n + 1), 3)
    if pnl > 0 or closed.get("close_reason") in {"tp1_hit"}:
        rollup["wins"] = int(rollup.get("wins") or 0) + 1

    entry = float(closed.get("entry_price") or 0)
    tp1 = float(closed.get("tp1") or 0)
    sl = float(closed.get("stop_loss") or 0)
    str(closed.get("direction") or "short")
    if entry > 0 and tp1 > 0:
        tp1_dist = abs(entry - tp1) / entry * 100.0
        prev = rollup.get("median_tp1_dist_pct")
        rollup["median_tp1_dist_pct"] = round(
            (float(prev) + tp1_dist) / 2.0 if prev is not None else tp1_dist, 2
        )
    if entry > 0 and sl > 0:
        sl_dist = abs(entry - sl) / entry * 100.0
        prev = rollup.get("median_sl_dist_pct")
        rollup["median_sl_dist_pct"] = round(
            (float(prev) + sl_dist) / 2.0 if prev is not None else sl_dist, 2
        )


def _close_candidate(
    state: dict[str, Any],
    cand: dict[str, Any],
    *,
    reason: CloseReason,
    exit_price: float,
    now: datetime,
) -> dict[str, Any]:
    cand["status"] = "closed"
    cand["closed_at"] = now.isoformat()
    cand["close_reason"] = reason
    cand["exit_price"] = exit_price
    entry = float(cand.get("entry_price") or 0)
    direc = str(cand.get("direction") or "short")
    cand["paper_pnl_pct"] = round(_pnl_pct(direc, entry, exit_price), 3)
    cand["direction_correct"] = cand["paper_pnl_pct"] > 0.15
    _update_rollup(state, cand)
    _append_event("closed", candidate=cand, extra={"close_reason": reason})
    return cand


def _open_candidate(
    state: dict[str, Any],
    *,
    symbol: str,
    direction: str,
    stage: str,
    source: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    lifecycle: dict[str, Any],
    now: datetime,
    block_code: str = "",
    parent_id: str | None = None,
) -> dict[str, Any]:
    sym = symbol.upper()
    key = _active_key(sym, direction)
    active = state.setdefault("active", {})
    price = float(row.get("price") or 0)
    prev = active.get(key)
    if isinstance(prev, dict) and prev.get("status") == "active":
        _close_candidate(state, prev, reason="superseded", exit_price=price, now=now)
        active.pop(key, None)

    sl, tp1, tp2 = _levels_from_setup(setup, direction=direction, price=price, row=row)
    cid = f"{sym}:{direction}:{stage}:{uuid.uuid4().hex[:8]}"
    dir_l = direction.lower()
    fuel_key = "dump_fuel" if dir_l == "short" else "long_fuel"
    score_key = "dump_score" if dir_l == "short" else "long_score"
    cand: dict[str, Any] = {
        "id": cid,
        "symbol": sym,
        "direction": direction,
        "stage": stage,
        "source": source,
        "status": "active",
        "opened_at": now.isoformat(),
        "entry_price": price,
        "stop_loss": sl,
        "tp1": tp1,
        "tp2": tp2,
        "lifecycle_phase": str(lifecycle.get("phase") or ""),
        "setup_phase": str(setup.get("phase") or ""),
        "fuel": float(
            setup.get(fuel_key)
            or setup.get(score_key)
            or 0
        ),
        "block_code": block_code or None,
        "parent_candidate_id": parent_id,
        "peak_price": price,
        "trough_price": price,
        "mfe_pct": 0.0,
        "mae_pct": 0.0,
        "features_open": feature_vector_from_row(row),
    }
    active[key] = cand
    _append_event("opened", candidate=cand)
    return cand


def _tick_candidate(
    cand: dict[str, Any], *, price: float, row: dict[str, Any], now: datetime
) -> CloseReason | None:
    hi, lo = _bar_extremes(row, price)
    peak = float(cand.get("peak_price") or price)
    trough = float(cand.get("trough_price") or price)
    cand["peak_price"] = max(peak, hi, price)
    cand["trough_price"] = min(trough, lo, price)
    entry = float(cand.get("entry_price") or 0)
    direc = str(cand.get("direction") or "short")
    mfe, mae = _mfe_mae(direc, entry, cand["peak_price"], cand["trough_price"])
    cand["mfe_pct"] = mfe
    cand["mae_pct"] = mae
    cand["features_last"] = feature_vector_from_row(row)
    if mfe >= float(cand.get("peak_mfe_pct") or 0):
        cand["peak_mfe_pct"] = mfe
        cand["features_peak"] = cand["features_last"]

    sl = float(cand.get("stop_loss") or 0)
    tp1 = float(cand.get("tp1") or 0)
    if direc == "short":
        if sl > 0 and cand["peak_price"] >= sl:
            return "stop_hit"
        if tp1 > 0 and cand["trough_price"] <= tp1:
            return "tp1_hit"
    else:
        if sl > 0 and cand["trough_price"] <= sl:
            return "stop_hit"
        if tp1 > 0 and cand["peak_price"] >= tp1:
            return "tp1_hit"

    if mae >= 2.5 and mfe < 1.0:
        return "direction_fail"

    try:
        opened = datetime.fromisoformat(str(cand.get("opened_at")))
        if now - opened >= timedelta(hours=HORIZON_HOURS):
            return "horizon_expired"
    except (TypeError, ValueError):
        pass
    return None


def process_setup_candidate(
    state: dict[str, Any],
    *,
    symbol: str,
    direction: str,
    setup: dict[str, Any] | None,
    row: dict[str, Any],
    lifecycle: Any | None,
    now: datetime | None = None,
    squeeze: dict[str, Any] | None = None,
    forming: bool = False,
    blocked: bool = False,
    block_code: str = "",
    ignition: bool = False,
) -> list[dict[str, Any]]:
    """Update active candidates and open new ones from detection sources."""
    ts = now or datetime.now(UTC)
    sym = symbol.upper()
    price = float(row.get("price") or 0)
    if price <= 0:
        return []

    lc = lifecycle if isinstance(lifecycle, dict) else {}
    active = state.setdefault("active", {})
    key = _active_key(sym, direction)
    events: list[dict[str, Any]] = []

    cur = active.get(key)
    if isinstance(cur, dict) and cur.get("status") == "active":
        reason = _tick_candidate(cur, price=price, row=row, now=ts)
        if reason:
            events.append(_close_candidate(state, cur, reason=reason, exit_price=price, now=ts))
            active.pop(key, None)

    setup = setup or {}
    if blocked and block_code:
        opened = _open_candidate(
            state,
            symbol=sym,
            direction=direction,
            stage="blocked",
            source="gate_blocked",
            setup=setup,
            row=row,
            lifecycle=lc,
            now=ts,
            block_code=block_code,
        )
        events.append(opened)
        events.append(
            _close_candidate(state, opened, reason="gate_blocked", exit_price=price, now=ts)
        )
        active.pop(_active_key(sym, direction), None)
        return events

    if squeeze and not setup.get("confirmed"):
        cur = active.get(key)
        if isinstance(cur, dict) and cur.get("status") == "active" and cur.get("stage") == "compression":
            return events
        opened = _open_candidate(
            state,
            symbol=sym,
            direction=direction,
            stage="compression",
            source="lifecycle_squeeze",
            setup=setup,
            row=row,
            lifecycle=lc,
            now=ts,
        )
        events.append(opened)
        return events

    if ignition and not setup.get("confirmed"):
        cur = active.get(key)
        if isinstance(cur, dict) and cur.get("status") == "active" and cur.get("stage") == "ignition":
            return events
        opened = _open_candidate(
            state,
            symbol=sym,
            direction=direction,
            stage="ignition",
            source="ignition",
            setup=setup,
            row=row,
            lifecycle=lc,
            now=ts,
        )
        events.append(opened)
        return events

    if (
        setup.get("early_tier") == "armed"
        or setup.get("anticipation")
        or setup.get("intrabar_armed")
    ) and not setup.get("confirmed"):
        cur = active.get(key)
        if isinstance(cur, dict) and cur.get("status") == "active" and cur.get("stage") == "armed":
            return events
        opened = _open_candidate(
            state,
            symbol=sym,
            direction=direction,
            stage="armed",
            source="early_armed",
            setup=setup,
            row=row,
            lifecycle=lc,
            now=ts,
        )
        events.append(opened)
        return events

    if forming and not setup.get("confirmed"):
        cur = active.get(key)
        if isinstance(cur, dict) and cur.get("status") == "active" and cur.get("stage") == "forming":
            return events
        from hunt_core.detect.setup_fields import setup_meets_strength

        if setup_meets_strength(
            setup, direction=direction, symbol=sym, tier="forming"
        ):
            opened = _open_candidate(
                state,
                symbol=sym,
                direction=direction,
                stage="forming",
                source="forming",
                setup=setup,
                row=row,
                lifecycle=lc,
                now=ts,
            )
            events.append(opened)
    return events


def promote_to_confirm(
    state: dict[str, Any],
    *,
    symbol: str,
    direction: str,
    price: float,
    now: datetime | None = None,
) -> None:
    """Close active candidate when confirm TG ships."""
    ts = now or datetime.now(UTC)
    key = _active_key(symbol, direction)
    active = state.get("active") or {}
    cur = active.get(key)
    if isinstance(cur, dict) and cur.get("status") == "active":
        _close_candidate(
            state, cur, reason="promoted_to_confirm", exit_price=price, now=ts
        )
        active.pop(key, None)


def symbol_rollup(state: dict[str, Any], symbol: str) -> dict[str, Any]:
    return dict((state.get("rollup") or {}).get(symbol.upper()) or {})


def format_symbol_candidates_html(symbol: str, state: dict[str, Any] | None = None) -> str:
    """Short per-symbol failed-setup stats for /signal or /candidates."""
    import html as html_mod

    state = state or load_setup_candidates_state()
    r = symbol_rollup(state, symbol)
    if not r or int(r.get("n_closed") or 0) <= 0:
        return ""
    sym = html_mod.escape(symbol.replace("USDT", "-USDT"))
    n = int(r.get("n_closed") or 0)
    wins = int(r.get("wins") or 0)
    wr = round(wins / n * 100.0, 1) if n else 0.0
    lines = [
        f"📊 <b>История сетапов · {sym}</b>",
        f"Закрыто кандидатов: <code>{n}</code> · WR paper: <code>{wr}%</code>",
        f"Avg MFE <code>{r.get('avg_mfe', '—')}%</code> · "
        f"Avg MAE <code>{r.get('avg_mae', '—')}%</code> · "
        f"Avg PnL <code>{r.get('avg_paper_pnl', '—')}%</code>",
    ]
    if r.get("median_tp1_dist_pct") is not None:
        lines.append(
            f"Median TP1 dist <code>{r['median_tp1_dist_pct']}%</code> · "
            f"SL dist <code>{r.get('median_sl_dist_pct', '—')}%</code>"
        )
    sq = int(r.get("n_squeeze") or 0)
    prep = int(r.get("n_prep") or 0)
    blk = int(r.get("n_blocked") or 0)
    if sq or prep or blk:
        lines.append(
            f"Squeeze <code>{sq}</code> · prep <code>{prep}</code> · "
            f"blocked <code>{blk}</code> · → confirm <code>{r.get('n_confirmed', 0)}</code>"
        )
    return "\n".join(lines)
