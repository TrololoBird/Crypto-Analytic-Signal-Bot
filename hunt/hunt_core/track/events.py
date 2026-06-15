"""Signal events log — hunt_core canonical (append-only lifecycle + audit log)."""
from __future__ import annotations



import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hunt_core.analysis.deep_signal import resolve_trade_direction
from hunt_core.params.store import effective_hunt_params
from hunt_core.paths import DATA, SIGNAL_EVENTS
from hunt_core.scan._engine_impl import confirm_dump, confirm_long

AUDIT_LOG = DATA / "signal_audit.jsonl"

FUNNEL_STAGES: tuple[str, ...] = (
    "prescan",
    "lifecycle",
    "armed",
    "dump_initiation",
    "dump_active",
    "fuel",
    "wash",
    "tier",
    "deliver",
)

# Lifecycle phases that map to dedicated funnel telemetry stages (0a baseline).
_LIFECYCLE_FUNNEL_MAP: dict[str, str] = {
    "dump_initiating": "dump_initiation",
    "dump_active": "dump_active",
}


def append_signal_event(
    event: str,
    *,
    symbol: str,
    direction: str = "",
    detail: str = "",
    payload: dict[str, Any] | None = None,
    path: Path = SIGNAL_EVENTS,
) -> None:
    row = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event,
        "symbol": symbol.upper(),
        "direction": direction.lower() if direction else "",
        "detail": detail,
        "payload": payload or {},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def record_funnel_stage(
    stage: str,
    *,
    symbol: str,
    direction: str = "",
    detail: str = "",
    payload: dict[str, Any] | None = None,
    path: Path = SIGNAL_EVENTS,
) -> None:
    """Telemetry funnel stage → signal_events JSONL (P0 telemetry)."""
    stage_norm = stage if stage in FUNNEL_STAGES else "unknown"
    body = {"stage": stage_norm, **(payload or {})}
    append_signal_event(
        f"funnel_{stage_norm}",
        symbol=symbol,
        direction=direction,
        detail=detail,
        payload=body,
        path=path,
    )


def record_lifecycle_funnel(
    *,
    symbol: str,
    phase: str,
    prev_phase: str | None = None,
    bias: str = "",
    payload: dict[str, Any] | None = None,
    path: Path = SIGNAL_EVENTS,
) -> None:
    """Record lifecycle transition + mapped anticipation funnel stages (0a baseline)."""
    body = {"phase": phase, "prev": prev_phase, "bias": bias, **(payload or {})}
    record_funnel_stage(
        "lifecycle",
        symbol=symbol,
        detail=phase,
        payload=body,
        path=path,
    )
    mapped = _LIFECYCLE_FUNNEL_MAP.get(phase)
    if mapped:
        record_funnel_stage(
            mapped,
            symbol=symbol,
            detail=phase,
            payload=body,
            path=path,
        )


def record_phase_transition(
    *,
    symbol: str,
    direction: str,
    from_phase: str,
    to_phase: str,
    detail: str = "",
    payload: dict[str, Any] | None = None,
    path: Path = SIGNAL_EVENTS,
) -> None:
    """Append tracker FSM phase transition to signal_events JSONL."""
    body = {
        "from_phase": from_phase,
        "to_phase": to_phase,
        **(payload or {}),
    }
    append_signal_event(
        "phase_transition",
        symbol=symbol,
        direction=direction,
        detail=detail or f"{from_phase}->{to_phase}",
        payload=body,
        path=path,
    )


def _entry_mid(setup: dict[str, Any]) -> float:
    ez = setup.get("entry_zone") or [0, 0]
    lo = float(ez[0] or 0)
    hi = float(ez[1] if len(ez) > 1 else lo)
    return (lo + hi) / 2.0 if lo and hi else lo or hi


def backtest_levels_on_bars(
    bars: list[tuple[float, float, float]],
    *,
    setup: dict[str, Any],
    direction: str,
) -> dict[str, Any]:
    """bars = (high, low, close) per 5m since probe."""
    if not bars:
        return {"bars": 0}
    mid = _entry_mid(setup)
    sl = float(setup.get("stop_loss") or 0)
    tp1 = float(setup.get("tp1") or 0)
    tp2 = float(setup.get("tp2") or 0)
    hi = max(b[0] for b in bars)
    lo = min(b[1] for b in bars)
    last = bars[-1][2]
    outcome, exit_px = "open", last
    if direction == "short":
        if sl and hi >= sl:
            outcome, exit_px = "stop_hit", sl
        elif tp2 and lo <= tp2:
            outcome, exit_px = "tp2", tp2
        elif tp1 and lo <= tp1:
            outcome, exit_px = "tp1", tp1
        pnl = round(-(exit_px - mid) / mid * 100, 2) if mid else None
    else:
        if sl and lo <= sl:
            outcome, exit_px = "stop_hit", sl
        elif tp2 and hi >= tp2:
            outcome, exit_px = "tp2", tp2
        elif tp1 and hi >= tp1:
            outcome, exit_px = "tp1", tp1
        pnl = round((exit_px - mid) / mid * 100, 2) if mid else None
    return {
        "bars": len(bars),
        "hi": hi,
        "lo": lo,
        "last": last,
        "outcome": outcome,
        "pnl_if_levels": pnl,
    }


def audit_probe_row(row: dict[str, Any], *, source: str = "signal_cmd") -> dict[str, Any]:
    """Independent replay + delivery simulation for one probe snapshot."""
    issues: list[str] = []
    checks: list[str] = []
    sym = str(row.get("symbol") or "")
    lc = row.get("lifecycle") or {}
    tf = row.get("timeframes") or {}
    cal = effective_hunt_params(sym)
    bias = str(lc.get("recommended_bias") or "")

    direction, setup, fuel, dir_notes = resolve_trade_direction(row)
    if direction == "short":
        indie_conf, hard = confirm_dump(
            row.get("dump") or {},
            tf,
            symbol=sym,
            price=float(row.get("price") or 0),
            market=row.get("market") or {},
            cal=cal,
            lifecycle_bias=bias if bias in {"long", "short", "wait"} else "",
        )
        bot_conf = bool((row.get("dump") or {}).get("confirmed"))
    else:
        indie_conf, hard = confirm_long(
            row.get("long") or {},
            tf,
            symbol=sym,
            price=float(row.get("price") or 0),
            market=row.get("market") or {},
            cal=cal,
            lifecycle_bias=bias if bias in {"long", "short", "wait"} else "",
            lifecycle_phase=str(lc.get("phase") or ""),
        )
        bot_conf = bool((row.get("long") or {}).get("confirmed"))

    if bot_conf != indie_conf:
        issues.append(f"confirm_mismatch bot={bot_conf} indie={indie_conf} hard={hard}")
    else:
        checks.append(f"confirm_ok={indie_conf}")

    dq = row.get("data_quality") or {}
    missing = dq.get("fields_missing") or []
    if missing:
        issues.append(f"data_missing={missing}")
    else:
        checks.append("data_complete")

    if bias in {"short", "long"}:
        counter = "long" if bias == "short" else "short"
        alt_fuel = float(
            (row.get("long") or {}).get("long_fuel")
            if counter == "long"
            else (row.get("dump") or {}).get("dump_fuel")
            or 0
        )
        if direction == counter and alt_fuel > fuel + 15:
            issues.append(
                f"direction_vs_lifecycle bias={bias} picked={direction} "
                f"fuel={fuel} alt={alt_fuel}"
            )
        else:
            checks.append(f"direction_aligns_bias={bias}")

    if not setup.get("levels_viable"):
        veto = setup.get("levels_veto") or []
        checks.append(f"levels_veto={veto}")
    if setup.get("filter_blocks"):
        checks.append(f"filters={setup.get('filter_blocks')}")

    sess = row.get("session") or {}
    chg = abs(float(row.get("chg_24h_pct") or 0))
    rng = float(sess.get("range_pct_24h") or 0)
    if sym in {"BTCUSDT", "ETHUSDT"} and chg < cal.anomaly_min_chg_24h_pct and rng < cal.anomaly_min_range_24h_pct:
        checks.append("pinned_low_vol_anchor — meme hunt rules relaxed")

    return {
        "ts": datetime.now(UTC).isoformat(),
        "source": source,
        "symbol": sym,
        "ok": not issues,
        "issues": issues,
        "checks": checks,
        "direction": direction,
        "fuel": fuel,
        "dir_notes": dir_notes,
        "phase": setup.get("phase"),
        "levels_viable": setup.get("levels_viable"),
        "sl_dist_pct": setup.get("sl_dist_pct"),
        "lifecycle_phase": lc.get("phase"),
        "lifecycle_bias": bias,
        "indie_confirmed": indie_conf,
        "hard": hard,
    }


def append_audit_log(report: dict[str, Any], path: Path = AUDIT_LOG) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(report, default=str) + "\n")


def load_pending_symbols(path: Path | None = None) -> list[str]:
    from hunt_core.data.universe import SIGNAL_NOTIFY

    p = path or SIGNAL_NOTIFY
    if not p.exists():
        return []
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return []
    pending = payload.get("pending") or []
    return [str(x.get("symbol")).upper() for x in pending if isinstance(x, dict) and x.get("symbol")]


__all__ = [
    "AUDIT_LOG",
    "FUNNEL_STAGES",
    "append_audit_log",
    "append_signal_event",
    "audit_probe_row",
    "backtest_levels_on_bars",
    "load_pending_symbols",
    "record_funnel_stage",
    "record_lifecycle_funnel",
    "record_phase_transition",
]
