"""Offline replay of hunt tick JSONL — confirm/gate sweep without live loop.

Anti-leakage: replay reads only stored ``*_closed`` bar fields from JSONL ticks;
forming-bar highs/lows and shift(-N) features are not used on the confirm path.
Rows missing closed-bar keys are skipped in ``iter_tick_rows``.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from hunt_watch.alert_explain import evaluate_alert_gate
from hunt_watch.early_alert import (
    early_cooldown_ok,
    evaluate_early_alert,
    mark_early_sent,
)
from hunt_watch.lifecycle import HuntLifecycle, assess_hunt_lifecycle
from hunt_watch.market_regime import HuntCalibratedParams
from hunt_watch.param_store import effective_hunt_params
from hunt_watch.paths import DATA, TICK_JSONL
from hunt_watch.levels import structural_long_levels
from hunt_watch.signal_engine import confirm_dump, confirm_long

WIN_REASONS = frozenset({"tp1", "tp2", "fix_profit_tp1", "fix_profit_tp2"})
_REQUIRED_CLOSED_TFS = ("5m_closed", "15m_closed")


def _replay_cal(
    row: dict[str, Any],
    cal: HuntCalibratedParams | None,
) -> HuntCalibratedParams:
    """Live-aligned params per symbol — defaults() used confirm_min=60, live uses 72."""
    if cal is not None:
        return cal
    return effective_hunt_params(str(row.get("symbol") or "").upper())


def _parse_row_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def resolve_tick_paths(paths: list[Path] | None = None) -> list[Path]:
    """Daily archives plus live staging buffer (``dump_minute_watch.jsonl``)."""
    if paths is None:
        resolved = sorted(DATA.glob("dump_minute_watch*.jsonl"), key=lambda p: p.stat().st_mtime)
    else:
        resolved = list(paths)
    if TICK_JSONL.exists() and TICK_JSONL not in resolved:
        resolved.append(TICK_JSONL)
    return resolved


def row_has_closed_bars(row: dict[str, Any]) -> bool:
    """Anti-leakage: confirm path requires closed 5m/15m bars in stored tick."""
    tf = row.get("timeframes") or {}
    for key in _REQUIRED_CLOSED_TFS:
        block = tf.get(key)
        if not isinstance(block, dict):
            return False
        try:
            if float(block.get("close") or 0) <= 0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def load_tick_rows(
    *,
    paths: list[Path] | None = None,
    max_lines: int = 5000,
    symbols: set[str] | None = None,
    strict_closed: bool = True,
) -> list[dict[str, Any]]:
    """Load tail JSONL rows chronologically sorted; optional closed-bar filter."""
    rows = list(iter_tick_rows(paths=paths, max_lines=max_lines, symbols=symbols))
    if strict_closed:
        rows = [r for r in rows if row_has_closed_bars(r)]
    rows.sort(key=lambda r: (_parse_row_ts(str(r.get("ts") or "")) or datetime.min.replace(tzinfo=UTC)))
    return rows


@dataclass(slots=True)
class ReplayRowResult:
    symbol: str
    ts: str
    direction: str
    confirmed: bool
    gate_ok: bool
    gate_code: str
    fuel: float
    score: float
    lifecycle_phase: str


@dataclass(slots=True)
class SweepBucket:
    confirm_min: float
    n_ticks: int = 0
    n_confirmed_short: int = 0
    n_confirmed_long: int = 0
    n_gate_short: int = 0
    n_gate_long: int = 0
    block_codes_short: Counter[str] = field(default_factory=Counter)
    block_codes_long: Counter[str] = field(default_factory=Counter)


def iter_tick_rows(
    *,
    paths: list[Path] | None = None,
    max_lines: int = 5000,
    symbols: set[str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Tail-scan JSONL files newest-last within max_lines window."""
    paths = resolve_tick_paths(paths)
    lines: list[str] = []
    for path in paths:
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                lines.append(line)
                if len(lines) > max_lines:
                    lines.pop(0)
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        sym = str(row.get("symbol") or "").upper()
        if symbols and sym not in symbols:
            continue
        if sym and row.get("dump") is not None:
            yield row


def pick_recommended_floor(
    buckets: list[SweepBucket],
    *,
    min_confirm_rate: float = 0.015,
) -> SweepBucket | None:
    """Pick IS floor: maximize gate-pass without starving confirms (Q25/Q16)."""
    if not buckets:
        return None

    def _score(b: SweepBucket) -> float:
        slots = max(1, 2 * b.n_ticks)
        confirm_rate = (b.n_confirmed_short + b.n_confirmed_long) / slots
        gate_rate = (b.n_gate_short + b.n_gate_long) / slots
        if confirm_rate < min_confirm_rate:
            return gate_rate * 0.35
        return gate_rate + confirm_rate * 0.25

    return max(buckets, key=_score)


def walk_forward_sweep(
    rows: list[dict[str, Any]],
    *,
    floors: tuple[int, ...] | None = None,
    is_ratio: float = 0.70,
    min_oos_ticks: int = 400,
    min_confirm_rate: float = 0.015,
) -> dict[str, Any]:
    """Rolling IS optimize confirm_min → frozen OOS validation (Q16/Q25)."""
    from hunt_watch.param_store import walk_forward_thresholds

    wf = walk_forward_thresholds()
    if floors is None:
        raw_floors = wf.get("floors") or list(range(60, 73, 2))
        floors = tuple(int(x) for x in raw_floors)
    is_ratio = float(is_ratio if is_ratio > 0 else wf.get("is_ratio", 0.70))
    min_oos_ticks = int(min_oos_ticks or wf.get("min_oos_ticks", 400))
    min_confirm_rate = float(min_confirm_rate or wf.get("min_confirm_rate", 0.015))

    if len(rows) < min_oos_ticks + 100:
        return {
            "error": "insufficient_rows",
            "n_rows": len(rows),
            "min_required": min_oos_ticks + 100,
        }
    split = max(100, int(len(rows) * is_ratio))
    if split >= len(rows) - min_oos_ticks:
        split = len(rows) - min_oos_ticks
    is_rows, oos_rows = rows[:split], rows[split:]
    is_buckets = sweep_confirm_min(is_rows, floors=floors)
    best = pick_recommended_floor(is_buckets, min_confirm_rate=min_confirm_rate)
    if best is None:
        return {"error": "no_buckets"}

    base = HuntCalibratedParams.defaults()
    oos_cal = replace(
        base,
        confirm_min_score=best.confirm_min,
        confirm_min_score_no_div=min(72.0, best.confirm_min + 2.0),
    )
    oos_conf_short = oos_gate_short = oos_conf_long = oos_gate_long = 0
    for row in oos_rows:
        rs = replay_row(row, cal=oos_cal, direction="short")
        rl = replay_row(row, cal=oos_cal, direction="long")
        if rs.confirmed:
            oos_conf_short += 1
        if rs.gate_ok:
            oos_gate_short += 1
        if rl.confirmed:
            oos_conf_long += 1
        if rl.gate_ok:
            oos_gate_long += 1

    is_from = is_rows[0].get("ts") if is_rows else None
    is_to = is_rows[-1].get("ts") if is_rows else None
    oos_from = oos_rows[0].get("ts") if oos_rows else None
    oos_to = oos_rows[-1].get("ts") if oos_rows else None

    return {
        "is_rows": len(is_rows),
        "oos_rows": len(oos_rows),
        "is_range": [is_from, is_to],
        "oos_range": [oos_from, oos_to],
        "picked_confirm_min": best.confirm_min,
        "is_gate_short": best.n_gate_short,
        "is_gate_long": best.n_gate_long,
        "is_confirmed_short": best.n_confirmed_short,
        "is_confirmed_long": best.n_confirmed_long,
        "oos_confirmed_short": oos_conf_short,
        "oos_gate_short": oos_gate_short,
        "oos_confirmed_long": oos_conf_long,
        "oos_gate_long": oos_gate_long,
        "min_oos_outcomes_guard": int(wf.get("min_oos_outcomes", 30)),
        "autotune_allowed": False,
    }


def prep_shadow_direction_replay(
    rows: list[dict[str, Any]],
    *,
    window_hours: float = 8.0,
    direction_ok_mfe_pct: float = 2.0,
    wrong_mae_pct: float = 2.5,
) -> dict[str, Any]:
    """Offline prep-tier direction WR + paper MFE/MAE (mirrors prep_shadow_tracker)."""
    from hunt_watch.param_store import prep_shadow_thresholds

    ps = prep_shadow_thresholds()
    window_h = float(window_hours or ps.get("horizon_hours", 8.0))
    ok_mfe = float(direction_ok_mfe_pct or ps.get("direction_ok_min_mfe_pct", 2.0))
    bad_mae = float(wrong_mae_pct or ps.get("wrong_direction_mae_pct", 2.5))

    early = early_alert_simulation(rows)
    sends: list[dict[str, Any]] = early.get("all_sends") or []
    if not sends:
        return {"n": 0, "direction_wr_pct": None, "by_tier": []}

    indexed: list[tuple[datetime, dict[str, Any]]] = []
    for row in rows:
        ts = _parse_row_ts(str(row.get("ts") or ""))
        if ts is not None:
            indexed.append((ts, row))
    indexed.sort(key=lambda x: x[0])

    tier_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    labeled = 0
    wins = 0
    for send in sends:
        sym = str(send.get("symbol") or "").upper()
        direction = str(send.get("direction") or "")
        tier = str(send.get("tier") or "?")
        ts0 = _parse_row_ts(str(send.get("ts") or ""))
        entry = float(send.get("price") or 0)
        if ts0 is None or not sym or entry <= 0:
            continue
        deadline = ts0 + timedelta(hours=window_h)
        mfe = 0.0
        mae = 0.0
        for ts, row in indexed:
            if ts <= ts0:
                continue
            if ts > deadline:
                break
            if str(row.get("symbol") or "").upper() != sym:
                continue
            px = float(row.get("price") or 0)
            if px <= 0:
                continue
            if direction == "short":
                fav = (entry - px) / entry * 100.0
                adv = (px - entry) / entry * 100.0
            else:
                fav = (px - entry) / entry * 100.0
                adv = (entry - px) / entry * 100.0
            mfe = max(mfe, fav)
            mae = max(mae, adv)
        direction_ok = mfe >= ok_mfe and mae < bad_mae
        if mfe >= ok_mfe or mae >= bad_mae:
            labeled += 1
            if direction_ok:
                wins += 1
        rec = {"tier": tier, "direction": direction, "mfe_pct": round(mfe, 2), "direction_ok": direction_ok}
        tier_rows[tier].append(rec)

    by_tier: list[dict[str, Any]] = []
    for tier, items in sorted(tier_rows.items()):
        tw = sum(1 for r in items if r.get("direction_ok"))
        tn = len(items)
        by_tier.append(
            {
                "tier": tier,
                "n": tn,
                "wr_pct": round(tw / tn * 100.0, 1) if tn else None,
            }
        )

    return {
        "n": len(sends),
        "labeled": labeled,
        "direction_wr_pct": round(wins / labeled * 100.0, 1) if labeled else None,
        "window_hours": window_h,
        "by_tier": by_tier,
    }


def prep_to_confirm_funnel(
    rows: list[dict[str, Any]],
    *,
    window_hours: float = 8.0,
    cal: HuntCalibratedParams | None = None,
) -> dict[str, Any]:
    """Q20: prep/start tier → confirmed within forward window on same symbol/direction."""
    from hunt_watch.param_store import tracker_thresholds

    tr = tracker_thresholds()
    window_h = float(window_hours or tr.get("prep_confirm_window_hours", 8.0))
    early = early_alert_simulation(rows, cal=cal)
    sends: list[dict[str, Any]] = early.get("all_sends") or []
    if not sends:
        return {
            "prep_sends": 0,
            "confirmed_within_window": 0,
            "conversion_pct": None,
            "window_hours": window_h,
            "tier_breakdown": early.get("tier_hits"),
        }

    indexed: list[tuple[datetime, dict[str, Any]]] = []
    for row in rows:
        ts = _parse_row_ts(str(row.get("ts") or ""))
        if ts is not None:
            indexed.append((ts, row))
    indexed.sort(key=lambda x: x[0])

    converted = 0
    tier_conv: Counter[str] = Counter()
    for send in sends:
        sym = str(send.get("symbol") or "").upper()
        direction = str(send.get("direction") or "")
        tier = str(send.get("tier") or "")
        ts0 = _parse_row_ts(str(send.get("ts") or ""))
        if ts0 is None or not sym:
            continue
        deadline = ts0 + timedelta(hours=window_h)
        setup_key = "dump" if direction == "short" else "long"
        hit = False
        for ts, row in indexed:
            if ts <= ts0:
                continue
            if ts > deadline:
                break
            if str(row.get("symbol") or "").upper() != sym:
                continue
            setup = row.get(setup_key) or {}
            if bool(setup.get("confirmed")):
                hit = True
                break
        if hit:
            converted += 1
            tier_conv[tier] += 1

    n = len(sends)
    return {
        "prep_sends": n,
        "confirmed_within_window": converted,
        "conversion_pct": round(converted / n * 100.0, 1) if n else None,
        "window_hours": window_h,
        "tier_breakdown": early.get("tier_hits"),
        "tier_converted": [{"tier": t, "n": c} for t, c in tier_conv.most_common()],
    }


def recompute_lifecycle_row(row: dict[str, Any]) -> HuntLifecycle | None:
    """Re-run the lifecycle FSM on a stored tick — stored phase reflects the code
    at record time; this is how FSM fixes are validated against history."""
    price = float(row.get("price") or 0)
    imp = row.get("impulse") or {}
    hunt_high = float(imp.get("hunt_high") or row.get("impulse_high") or 0)
    hunt_low = float(imp.get("hunt_low") or row.get("impulse_low") or 0)
    session = row.get("session") or {}
    if price <= 0 or hunt_high <= 0 or not session:
        return None
    return assess_hunt_lifecycle(
        price=price,
        hunt_high=hunt_high,
        hunt_low=hunt_low,
        session=session,
        tf=row.get("timeframes") or {},
        market=row.get("market") or {},
        symbol=str(row.get("symbol") or "").upper(),
    )


def _recompute_long_levels(
    row: dict[str, Any],
    lifecycle: dict[str, Any],
) -> dict[str, Any] | None:
    """Re-derive long levels with current adaptive caps — stored levels_veto
    reflects record-time caps and otherwise hard-blocks confirm_long forever."""
    price = float(row.get("price") or 0)
    imp = row.get("impulse") or {}
    hunt_high = float(imp.get("hunt_high") or row.get("impulse_high") or 0)
    hunt_low = float(imp.get("hunt_low") or row.get("impulse_low") or 0)
    sess = row.get("session") or {}
    tf = row.get("timeframes") or {}
    atr15 = float((tf.get("15m") or {}).get("atr14") or 0)
    if price <= 0 or hunt_high <= 0 or atr15 <= 0:
        return None
    leg_gain = max(0.0, (hunt_high - hunt_low) / hunt_low) * 100.0 if hunt_low > 0 else 0.0
    fall = max(0.0, (hunt_high - price) / hunt_high) * 100.0
    return structural_long_levels(
        price=price,
        impulse_high=hunt_high,
        impulse_low=hunt_low,
        fib=row.get("fib") or {},
        atr15=atr15,
        local_support=float(lifecycle.get("local_support") or hunt_low),
        local_resistance=float(lifecycle.get("local_resistance") or hunt_high),
        range_pct_24h=float(sess.get("range_pct_24h") or 0),
        leg_gain_pct=leg_gain,
        fall_from_high_pct=fall,
        symbol=str(row.get("symbol") or "").upper(),
    )


_PUMP_GATE_PHASES = frozenset({"impulse_initiating", "breakout_arming"})


def gate_lifecycle_phase(
    *,
    direction: str,
    stored_phase: str,
    recomp_phase: str,
) -> str:
    """Replay gate phase: shorts keep stored label; longs prefer recomputed pump phase."""
    if stored_phase and stored_phase not in {"no_setup", "?"}:
        if direction == "short":
            return stored_phase
        if recomp_phase in _PUMP_GATE_PHASES:
            return recomp_phase
        return stored_phase
    return recomp_phase


def replay_row(
    row: dict[str, Any],
    *,
    cal: HuntCalibratedParams | None = None,
    direction: str,
    recompute_lifecycle: bool = True,
) -> ReplayRowResult:
    """Recompute confirm + alert gate for one stored tick."""
    sym = str(row.get("symbol") or "").upper()
    cal = _replay_cal(row, cal)
    tf = row.get("timeframes") or {}
    lifecycle = dict(row.get("lifecycle") or {})
    if recompute_lifecycle:
        lc = recompute_lifecycle_row(row)
        if lc is not None:
            lifecycle = {
                **lifecycle,
                "phase": lc.phase.value,
                "recommended_bias": lc.recommended_bias,
                "short_entry_ok": lc.short_entry_ok,
                "invalidate_short": lc.invalidate_short,
                "fall_from_high_pct": lc.fall_from_high_pct,
                "bounce_from_low_pct": lc.bounce_from_low_pct,
                "local_support": lc.local_support,
                "local_resistance": lc.local_resistance,
                "stored_phase": (row.get("lifecycle") or {}).get("phase"),
            }
    lc_bias = str(lifecycle.get("recommended_bias") or "")
    lc_phase = str(lifecycle.get("phase") or "")
    price = float(row.get("price") or 0)

    if direction == "short":
        setup = dict(row.get("dump") or {})
        confirmed, hard = confirm_dump(
            setup,
            tf,
            symbol=sym,
            price=price,
            market=row.get("market") or row.get("positioning"),
            cal=cal,
            lifecycle_bias=lc_bias,
        )
        setup_eval = {**setup, "confirmed": confirmed, "confirm_hard": hard}
        fuel = float(setup.get("dump_fuel") or setup.get("dump_score") or 0)
        score = float(setup.get("dump_score") or 0)
    else:
        setup = dict(row.get("long") or {})
        relv = _recompute_long_levels(row, lifecycle)
        if relv is not None:
            setup["levels_viable"] = relv.get("viable", True)
            setup["levels_veto"] = relv.get("veto") or []
            for k in ("entry_zone", "stop_loss", "tp1", "tp2", "risk_reward", "sl_dist_pct"):
                if relv.get(k) is not None:
                    setup[k] = relv[k]
        confirmed, hard = confirm_long(
            setup,
            tf,
            symbol=sym,
            price=price,
            market=row.get("market") or row.get("positioning"),
            cal=cal,
            lifecycle_bias=lc_bias,
            lifecycle_phase=lc_phase,
        )
        setup_eval = {**setup, "confirmed": confirmed, "confirm_hard": hard}
        fuel = float(setup.get("long_fuel") or setup.get("long_score") or 0)
        score = float(setup.get("long_score") or 0)

    stored_lc = dict(row.get("lifecycle") or {})
    gate_lc = dict(lifecycle)
    if stored_lc.get("fall_from_high_pct") is not None:
        gate_lc["fall_from_high_pct"] = max(
            float(gate_lc.get("fall_from_high_pct") or 0),
            float(stored_lc.get("fall_from_high_pct") or 0),
        )
    stored_phase = str(stored_lc.get("phase") or "")
    recomp_phase = str(lifecycle.get("phase") or "")
    gate_lc["phase"] = gate_lifecycle_phase(
        direction=direction,
        stored_phase=stored_phase,
        recomp_phase=recomp_phase,
    )
    gate = evaluate_alert_gate(
        setup_eval,
        direction=direction,
        symbol=sym,
        lifecycle=gate_lc,
        row=row,
    )
    return ReplayRowResult(
        symbol=sym,
        ts=str(row.get("ts") or ""),
        direction=direction,
        confirmed=confirmed,
        gate_ok=gate.ok,
        gate_code=gate.code,
        fuel=fuel,
        score=score,
        lifecycle_phase=str(lifecycle.get("phase") or ""),
    )


def sweep_confirm_min(
    rows: list[dict[str, Any]],
    *,
    floors: tuple[int, ...] = tuple(range(60, 73, 2)),
    forming_min: float = 45.0,
    adx_block: float = 34.0,
) -> list[SweepBucket]:
    """Sweep confirm_min on tail ticks — gate-pass rate per floor."""
    base = effective_hunt_params()
    buckets: list[SweepBucket] = []
    for floor in floors:
        cal = replace(
            base,
            confirm_min_score=float(floor),
            confirm_min_score_no_div=min(72.0, float(floor) + 2.0),
            forming_min_score=forming_min,
            adx_trend_block=adx_block,
        )
        b = SweepBucket(confirm_min=float(floor), n_ticks=len(rows))
        for row in rows:
            for direction in ("short", "long"):
                r = replay_row(row, cal=cal, direction=direction)
                if direction == "short":
                    if r.confirmed:
                        b.n_confirmed_short += 1
                    if r.gate_ok:
                        b.n_gate_short += 1
                    elif r.confirmed:
                        b.block_codes_short[r.gate_code] += 1
                else:
                    if r.confirmed:
                        b.n_confirmed_long += 1
                    if r.gate_ok:
                        b.n_gate_long += 1
                    elif r.confirmed:
                        b.block_codes_long[r.gate_code] += 1
        buckets.append(b)
    return buckets


def join_tracker_outcomes(
    state: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Map signal key → outcome summary for replay labeling."""
    out: dict[str, dict[str, Any]] = {}
    for key, sig in (state.get("signals") or {}).items():
        if not isinstance(sig, dict):
            continue
        reason = str(sig.get("close_reason") or "")
        out[key] = {
            "status": sig.get("status"),
            "close_reason": reason,
            "win": reason in WIN_REASONS,
            "score": sig.get("score"),
            "direction": sig.get("direction"),
        }
    return out


def phase_flip_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """stored phase vs recomputed phase — quantifies FSM fixes on history."""
    flips: Counter[tuple[str, str]] = Counter()
    samples: dict[tuple[str, str], dict[str, Any]] = {}
    n_recomputed = 0
    for row in rows:
        stored = str((row.get("lifecycle") or {}).get("phase") or "?")
        lc = recompute_lifecycle_row(row)
        if lc is None:
            continue
        n_recomputed += 1
        new = lc.phase.value
        if new != stored:
            key = (stored, new)
            flips[key] += 1
            samples.setdefault(
                key,
                {
                    "symbol": row.get("symbol"),
                    "ts": row.get("ts"),
                    "price": row.get("price"),
                    "reasons": list(lc.reasons)[:3],
                },
            )
    return {
        "n_recomputed": n_recomputed,
        "flips": [
            {"stored": s, "recomputed": n, "n": c, "sample": samples[(s, n)]}
            for (s, n), c in flips.most_common(12)
        ],
    }


def early_alert_simulation(
    rows: list[dict[str, Any]],
    *,
    cal: HuntCalibratedParams | None = None,
) -> dict[str, Any]:
    """Would-send early alerts on history with per-symbol tier cooldowns —
    offline validation of prep/imminent/start tiers (live path can't be
    backtested otherwise: sparse pre-fix rows never stored tier decisions)."""
    from datetime import datetime

    tiers: Counter[tuple[str, str]] = Counter()
    sends: list[dict[str, Any]] = []
    cooldown_state: dict[str, str] = {}
    for row in rows:
        sym = str(row.get("symbol") or "").upper()
        lifecycle = dict(row.get("lifecycle") or {})
        lc = recompute_lifecycle_row(row)
        if lc is not None:
            lifecycle.update(
                phase=lc.phase.value,
                recommended_bias=lc.recommended_bias,
                short_entry_ok=lc.short_entry_ok,
            )
        try:
            ts = datetime.fromisoformat(str(row.get("ts")))
        except ValueError, TypeError:
            continue
        for direction, key in (("short", "dump"), ("long", "long")):
            setup = row.get(key) or {}
            if not setup:
                continue
            alert = evaluate_early_alert(
                setup, direction=direction, symbol=sym, lifecycle=lifecycle, row=row
            )
            if alert.kind in ("none", "confirm"):
                continue
            tiers[(direction, alert.tier)] += 1
            if not early_cooldown_ok(sym, direction, alert.tier, cooldown_state, now=ts):
                continue
            mark_early_sent(sym, direction, alert.tier, cooldown_state, now=ts)
            sends.append(
                {
                    "ts": row.get("ts"),
                    "symbol": sym,
                    "direction": direction,
                    "tier": alert.tier,
                    "price": row.get("price"),
                    "phase": lifecycle.get("phase"),
                }
            )
    return {
        "tier_hits": [
            {"direction": d, "tier": t, "n": n} for (d, t), n in tiers.most_common()
        ],
        "would_send": len(sends),
        "all_sends": sends,
        "first_sends": sends[:10],
    }


def phase_distribution(rows: list[dict[str, Any]]) -> list[tuple[str, int]]:
    c: Counter[str] = Counter()
    for row in rows:
        phase = str((row.get("lifecycle") or {}).get("phase") or "?")
        c[phase] += 1
    return c.most_common(20)


def block_reason_mix(
    rows: list[dict[str, Any]],
    *,
    cal: HuntCalibratedParams | None = None,
) -> dict[str, Counter[str]]:
    short_c: Counter[str] = Counter()
    long_c: Counter[str] = Counter()
    for row in rows:
        row_cal = _replay_cal(row, cal)
        for direction, counter in (("short", short_c), ("long", long_c)):
            r = replay_row(row, cal=row_cal, direction=direction)
            if r.confirmed and not r.gate_ok:
                counter[r.gate_code] += 1
    return {"short": short_c, "long": long_c}


def summarize_sweep(
    buckets: list[SweepBucket],
    *,
    min_confirm_rate: float = 0.015,
) -> dict[str, Any]:
    best = pick_recommended_floor(buckets, min_confirm_rate=min_confirm_rate)
    return {
        "floors": [
            {
                "confirm_min": b.confirm_min,
                "ticks": b.n_ticks,
                "confirmed_short": b.n_confirmed_short,
                "gate_short": b.n_gate_short,
                "confirmed_long": b.n_confirmed_long,
                "gate_long": b.n_gate_long,
                "top_block_short": b.block_codes_short.most_common(5),
                "top_block_long": b.block_codes_long.most_common(5),
            }
            for b in buckets
        ],
        "recommended_floor": best.confirm_min if best else None,
    }


def run_replay_report(
    *,
    max_lines: int = 5000,
    symbols: set[str] | None = None,
    floors: tuple[int, ...] = tuple(range(60, 73, 2)),
    walk_forward: bool = True,
) -> dict[str, Any]:
    raw_n = len(list(iter_tick_rows(max_lines=max_lines, symbols=symbols)))
    rows = load_tick_rows(max_lines=max_lines, symbols=symbols, strict_closed=True)
    if not rows:
        return {"error": "no_rows", "max_lines": max_lines, "raw_rows": raw_n}

    sweep = sweep_confirm_min(rows, floors=floors)
    phases = phase_distribution(rows)
    flips = phase_flip_report(rows)
    early = early_alert_simulation(rows)
    prep_funnel = prep_to_confirm_funnel(rows)
    prep_shadow = prep_shadow_direction_replay(rows)
    blocks = block_reason_mix(rows)
    wf = walk_forward_sweep(rows, floors=floors) if walk_forward else None
    fuels_short = [
        float((r.get("dump") or {}).get("dump_fuel") or 0)
        for r in rows
        if (r.get("dump") or {}).get("dump_fuel") is not None
    ]

    return {
        "ts": rows[-1].get("ts"),
        "n_rows": len(rows),
        "raw_rows": raw_n,
        "rows_dropped_no_closed_bars": max(0, raw_n - len(rows)),
        "n_symbols": len({str(r.get("symbol") or "").upper() for r in rows}),
        "phase_distribution": phases,
        "phase_flips": flips,
        "early_alerts": early,
        "prep_funnel": prep_funnel,
        "prep_shadow_replay": prep_shadow,
        "walk_forward": wf,
        "median_dump_fuel": round(statistics.median(fuels_short), 1) if fuels_short else None,
        "block_mix": {
            "short": blocks["short"].most_common(12),
            "long": blocks["long"].most_common(12),
        },
        "sweep": summarize_sweep(sweep),
    }
