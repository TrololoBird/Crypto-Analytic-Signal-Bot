"""Calibrate universal + per-symbol hunt thresholds from outcomes, REST, tick history."""

from __future__ import annotations

import json
import statistics
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hunt_watch.adaptive_thresholds import AdaptiveStore, load_adaptive_store
from hunt_watch.market_regime import active_params
from hunt_watch.param_store import UNIVERSAL_DEFAULTS, save_calibration_payload
from hunt_watch.paths import PUMP_HISTORY, SIGNAL_STATE, TICK_JSONL

FAPI = "https://fapi.binance.com/fapi/v1/klines"
WIN = {"tp1", "tp2"}
LOSS = {"stop_hit", "bounce_invalidate", "trend_exhaustion", "reclaim_invalidation", "support_lost"}


def parse_outcome_rows(state: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, sig in (state.get("signals") or {}).items():
        if not isinstance(sig, dict):
            continue
        symbol, _, direction = key.partition(":")
        if not symbol:
            continue
        row = dict(sig)
        row["key"] = key
        row["symbol"] = symbol.upper()
        row["direction"] = direction or str(sig.get("direction") or "")
        rows.append(row)
    return rows


def _fetch_klines(symbol: str, *, interval: str = "1h", limit: int = 168) -> list[list]:
    params = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "limit": limit})
    with urllib.request.urlopen(f"{FAPI}?{params}", timeout=20) as resp:
        return json.load(resp)


def _fetch_klines_window(symbol: str, start_ms: int, *, interval: str = "5m") -> list[list]:
    out: list[list] = []
    cursor = start_ms
    for _ in range(8):
        params = urllib.parse.urlencode(
            {"symbol": symbol, "interval": interval, "startTime": cursor, "limit": 1500}
        )
        with urllib.request.urlopen(f"{FAPI}?{params}", timeout=20) as resp:
            batch = json.load(resp)
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 1500:
            break
        cursor = int(batch[-1][0]) + 1
    return out


def backfill_legacy_outcomes(state: dict[str, Any], *, now: datetime | None = None) -> int:
    """REST backfill close_reason for closed rows missing it."""
    ts = now or datetime.now(UTC)
    n = 0
    for row in parse_outcome_rows(state):
        reason_existing = str(row.get("close_reason") or "")
        if row.get("status") != "closed" or (reason_existing and reason_existing != "legacy_unknown"):
            continue
        symbol = row["symbol"]
        direction = str(row.get("direction") or "short")
        try:
            opened = datetime.fromisoformat(str(row.get("opened_at")))
        except (TypeError, ValueError):
            continue
        end_raw = row.get("closed_at")
        try:
            end = datetime.fromisoformat(str(end_raw)) if end_raw else ts
        except (TypeError, ValueError):
            end = ts
        try:
            kl = _fetch_klines_window(symbol, int(opened.timestamp() * 1000))
        except OSError:
            continue
        if not kl:
            continue
        end_ms = int(end.timestamp() * 1000)
        window = [k for k in kl if int(k[0]) <= end_ms] or kl
        hi = max(float(k[2]) for k in window)
        lo = min(float(k[3]) for k in window)
        last = float(window[-1][4])
        stop = float(row.get("stop_loss") or 0)
        tp1 = float(row.get("tp1") or 0)
        tp2 = float(row.get("tp2") or 0)
        reason, exit_px = "legacy_unknown", last
        if direction == "short":
            if stop > 0 and hi >= stop:
                reason, exit_px = "stop_hit", stop
            elif tp2 > 0 and lo <= tp2:
                reason, exit_px = "tp2", tp2
            elif tp1 > 0 and lo <= tp1:
                reason, exit_px = "tp1", tp1
        else:
            if stop > 0 and lo <= stop:
                reason, exit_px = "stop_hit", stop
            elif tp2 > 0 and hi >= tp2:
                reason, exit_px = "tp2", tp2
            elif tp1 > 0 and hi >= tp1:
                reason, exit_px = "tp1", tp1
        lo_e = float(row.get("entry_lo") or 0)
        hi_e = float(row.get("entry_hi") or 0)
        mid = (lo_e + hi_e) / 2.0 if lo_e > 0 and hi_e > 0 else (lo_e or hi_e)
        pnl = None
        if mid > 0:
            raw = (exit_px - mid) / mid * 100.0
            pnl = round(-raw if direction == "short" else raw, 2)
        sig = (state.get("signals") or {}).get(row["key"])
        if isinstance(sig, dict):
            sig["close_reason"] = reason
            sig["exit_price"] = exit_px
            if pnl is not None:
                sig["pnl_pct"] = pnl
            n += 1
    return n


def rest_symbol_profile(symbol: str) -> dict[str, float]:
    """7d 1h REST profile for per-symbol volatility gates."""
    try:
        kl = _fetch_klines(symbol, interval="1h", limit=168)
    except OSError:
        return {}
    if len(kl) < 24:
        return {}
    ranges: list[float] = []
    moves: list[float] = []
    for k in kl:
        o, h, l, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
        if o <= 0 or l <= 0:
            continue
        ranges.append((h / l - 1.0) * 100.0)
        moves.append(abs(c / o - 1.0) * 100.0)
    if not ranges:
        return {}
    med_range = statistics.median(ranges)
    p75_range = sorted(ranges)[int(len(ranges) * 0.75)]
    med_move = statistics.median(moves)
    return {
        "median_range_1h_pct": round(med_range, 3),
        "p75_range_1h_pct": round(p75_range, 3),
        "median_bar_move_pct": round(med_move, 4),
        "bars": float(len(kl)),
    }


def sample_tick_profiles(
    path: Path = TICK_JSONL,
    *,
    max_lines: int = 8000,
) -> dict[str, dict[str, Any]]:
    """Recent tick snapshots per symbol (tail scan)."""
    if not path.exists():
        return {}
    lines: list[str] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            lines.append(line)
            if len(lines) > max_lines:
                lines.pop(0)
    by_sym: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        sym = str(row.get("symbol") or "").upper()
        if sym:
            by_sym[sym].append(row)
    out: dict[str, dict[str, Any]] = {}
    for sym, rows in by_sym.items():
        chgs = [abs(float(r.get("chg_24h_pct") or 0)) for r in rows]
        ranges: list[float] = []
        for r in rows:
            dump = r.get("dump") or {}
            sl = dump.get("sl_dist_pct")
            if sl is not None:
                ranges.append(float(sl))
        out[sym] = {
            "tick_samples": len(rows),
            "median_abs_chg_24h": round(statistics.median(chgs), 2) if chgs else 0.0,
            "median_sl_dist_pct": round(statistics.median(ranges), 2) if ranges else None,
        }
    return out


def _grid_confirm_min(closed: list[dict[str, Any]]) -> float:
    """Pick confirm floor from labeled score×outcome (small-n safe)."""
    labeled = [
        r for r in closed
        if r.get("close_reason") in WIN | LOSS and r.get("score") is not None
    ]
    if len(labeled) < 5:
        return float(UNIVERSAL_DEFAULTS["gates"]["confirm_min_score"])
    best_score = 60.0
    best_metric = -1.0
    for floor in range(58, 73, 2):
        subset = [r for r in labeled if float(r["score"]) >= floor]
        if len(subset) < 3:
            continue
        wins = sum(1 for r in subset if r.get("close_reason") in WIN)
        losses = sum(1 for r in subset if r.get("close_reason") in LOSS)
        known = wins + losses
        if known < 3:
            continue
        winrate = wins / known
        avg_pnl = statistics.mean(float(r.get("pnl_pct") or 0) for r in subset)
        metric = winrate * 0.7 + (avg_pnl / 20.0) * 0.3
        if metric > best_metric:
            best_metric = metric
            best_score = float(floor)
    # Outcomes table bias: wins cluster 80+, losses 60-69
    win_scores = [float(r["score"]) for r in labeled if r.get("close_reason") in WIN]
    if win_scores:
        p25 = sorted(win_scores)[max(0, len(win_scores) // 4)]
        suggested = max(best_score, min(72.0, p25 - 8.0))
        return round(suggested, 1)
    return best_score


def calibrate_universal(
    *,
    closed: list[dict[str, Any]],
    regime: Any,
) -> dict[str, Any]:
    labeled = [r for r in closed if r.get("close_reason") in WIN | LOSS]
    stops = [r for r in labeled if r.get("close_reason") == "stop_hit" and r.get("pnl_pct") is not None]
    wins = [r for r in labeled if r.get("close_reason") in WIN and r.get("pnl_pct") is not None]

    confirm_min = _grid_confirm_min(closed)
    confirm_no_div = round(min(72.0, confirm_min + 6.0), 1)

    loss_pnls = [abs(float(r["pnl_pct"])) for r in stops]
    med_stop = statistics.median(loss_pnls) if loss_pnls else 4.0
    sl_normal = round(min(10.0, max(7.0, med_stop * 1.35)), 1)
    sl_para = round(min(15.0, sl_normal + 4.0), 1)

    adx_block = float(regime.adx_trend_block)
    if len(stops) >= 4 and len(wins) <= 2:
        adx_block = max(32.0, adx_block - 2.0)

    win_avg = statistics.mean(float(r["pnl_pct"]) for r in wins) if wins else 0.0
    min_rr = 1.0 if win_avg < 10 else 0.95

    return {
        "gates": {
            "confirm_min_score": confirm_min,
            "confirm_min_score_no_div": confirm_no_div,
            "forming_min_score": 45.0,
            "adx_trend_block": round(adx_block, 1),
            "adx_trend_min": 28.0,
            "min_risk_reward": min_rr,
        },
        "lifecycle": dict(UNIVERSAL_DEFAULTS["lifecycle"]),
        "levels": {
            **UNIVERSAL_DEFAULTS["levels"],
            "sl_max_pct_normal": sl_normal,
            "sl_max_pct_parabolic": sl_para,
        },
        "scanner": dict(UNIVERSAL_DEFAULTS["scanner"]),
    }


def calibrate_per_symbol(
    *,
    symbols: set[str],
    closed: list[dict[str, Any]],
    tick_profiles: dict[str, dict[str, Any]],
    pump_stats: dict[str, Any],
    adaptive: AdaptiveStore,
    rest_profiles: dict[str, dict[str, float]],
) -> dict[str, Any]:
    by_sym: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in closed:
        by_sym[r["symbol"]].append(r)

    per: dict[str, Any] = {}
    for sym in sorted(symbols):
        outcomes = by_sym.get(sym, [])
        pump = pump_stats.get(sym) if isinstance(pump_stats.get(sym), dict) else {}
        tick = tick_profiles.get(sym, {})
        rest = rest_profiles.get(sym, {})
        adapt = adaptive.symbols.get(sym)
        n_out = len(outcomes)
        n_pump_sig = int(pump.get("signal_short") or 0) + int(pump.get("signal_long") or 0)
        n_tick = int(tick.get("tick_samples") or 0)
        n_bars = int(rest.get("bars") or 0)
        if n_out < 1 and n_pump_sig < 2 and n_tick < 5 and n_bars < 48:
            continue

        entry: dict[str, Any] = {
            "n_outcomes": n_out,
            "n_pump_signals": n_pump_sig,
            "n_tick_samples": n_tick,
            "n_rest_bars": n_bars,
            "source": [],
        }
        gates: dict[str, float] = {}
        levels: dict[str, float] = {}
        lifecycle: dict[str, float] = {}
        scanner: dict[str, float] = {}

        labeled = [r for r in outcomes if r.get("close_reason") in WIN | LOSS]
        if labeled:
            wins = sum(1 for r in labeled if r.get("close_reason") in WIN)
            losses = sum(1 for r in labeled if r.get("close_reason") in LOSS)
            if losses >= 2 and wins == 0:
                gates["confirm_min_score"] = 72.0
                entry["source"].append("outcomes_all_loss")
            elif wins >= 2 and losses == 0:
                gates["confirm_min_score"] = 58.0
                entry["source"].append("outcomes_all_win")
            stops = [r for r in labeled if r.get("close_reason") == "stop_hit" and r.get("pnl_pct")]
            if stops:
                med = statistics.median(abs(float(r["pnl_pct"])) for r in stops)
                levels["sl_max_pct_normal"] = round(min(12.0, max(6.5, med * 1.4)), 2)
                entry["source"].append("outcome_stop_dist")

        if rest:
            p75 = float(rest.get("p75_range_1h_pct") or 0)
            med_range = float(rest.get("median_range_1h_pct") or 0)
            if p75 >= 8.0:
                scanner["hot_range_pct"] = round(max(4.0, med_range * 2.5), 2)
            if p75 >= 15.0:
                scanner["pump_extreme_pct"] = round(min(35.0, p75 * 3.0), 2)
                levels["sl_max_pct_parabolic"] = round(min(16.0, max(10.0, p75 * 0.9)), 2)
                gates["adx_trend_block"] = round(min(42.0, 34.0 + p75 * 0.15), 1)
                lifecycle["meaningful_dump_pct"] = 6.0 if p75 >= 20 else 8.0
                entry["source"].append("rest_volatility")

        if adapt and adapt.tick_n >= 6:
            import math

            sigma = math.sqrt(max(adapt.tick_var, 0.05))
            scanner["hot_range_pct"] = round(max(3.0, min(25.0, adapt.chg_mu * 0.85)), 2)
            entry["source"].append("ewma_adaptive")

        if tick.get("median_sl_dist_pct") is not None:
            levels["sl_max_pct_normal"] = round(
                min(14.0, max(7.0, float(tick["median_sl_dist_pct"]) * 1.15)),
                2,
            )
            entry["source"].append("tick_sl_dist")

        retrace = pump.get("retrace_rate_pct")
        if retrace is not None and float(retrace) >= 70.0 and n_pump_sig >= 2:
            lifecycle["premature_exhaustion_bounce_pct"] = 12.0
            entry["source"].append("pump_retrace")

        if not entry["source"]:
            continue
        if gates:
            entry["gates"] = gates
        if levels:
            entry["levels"] = levels
        if lifecycle:
            entry["lifecycle"] = lifecycle
        if scanner:
            entry["scanner"] = scanner
        per[sym] = entry
    return per


def run_full_calibration(
    *,
    fetch_rest: bool = True,
    backfill: bool = True,
    rest_symbol_limit: int = 40,
) -> dict[str, Any]:
    state = json.loads(SIGNAL_STATE.read_text(encoding="utf-8"))
    if backfill:
        filled = backfill_legacy_outcomes(state)
        if filled:
            SIGNAL_STATE.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")

    rows = parse_outcome_rows(state)
    closed = [r for r in rows if r.get("status") == "closed"]
    active = [r for r in rows if r.get("status") == "active"]
    symbols = {r["symbol"] for r in rows} | {r["symbol"] for r in active}

    pump_raw = json.loads(PUMP_HISTORY.read_text(encoding="utf-8")) if PUMP_HISTORY.exists() else {}
    pump_stats = pump_raw.get("symbols") or {}
    symbols |= {str(s).upper() for s in pump_stats if int((pump_stats[s] or {}).get("signal_short") or 0) > 0}

    tick_profiles = sample_tick_profiles()
    symbols |= set(tick_profiles)

    adaptive = load_adaptive_store()
    regime = active_params()

    rest_profiles: dict[str, dict[str, float]] = {}
    if fetch_rest:
        priority = sorted(
            symbols,
            key=lambda s: (
                -(int((pump_stats.get(s) or {}).get("signal_short") or 0)),
                -(tick_profiles.get(s, {}).get("tick_samples") or 0),
            ),
        )[:rest_symbol_limit]
        for sym in priority:
            prof = rest_symbol_profile(sym)
            if prof:
                rest_profiles[sym] = prof

    universal = calibrate_universal(closed=closed, regime=regime)
    per_symbol = calibrate_per_symbol(
        symbols=symbols,
        closed=closed,
        tick_profiles=tick_profiles,
        pump_stats=pump_stats,
        adaptive=adaptive,
        rest_profiles=rest_profiles,
    )

    labeled = [r for r in closed if r.get("close_reason") in WIN | LOSS]
    payload = {
        "computed_at": datetime.now(UTC).isoformat(),
        "data_summary": {
            "n_signals": len(rows),
            "n_closed": len(closed),
            "n_labeled": len(labeled),
            "n_legacy_unknown": sum(1 for r in closed if r.get("close_reason") == "legacy_unknown"),
            "n_per_symbol": len(per_symbol),
            "n_rest_profiles": len(rest_profiles),
            "n_tick_symbols": len(tick_profiles),
            "regime": regime.regime,
            "confidence": "low" if len(labeled) < 12 else "medium" if len(labeled) < 30 else "high",
        },
        "universal": universal,
        "per_symbol": per_symbol,
        "outcome_calibration": {
            "sl_max_pct_normal": universal["levels"]["sl_max_pct_normal"],
            "sl_max_pct_parabolic": universal["levels"]["sl_max_pct_parabolic"],
            "confirm_min_score": universal["gates"]["confirm_min_score"],
            "adx_trend_block": universal["gates"]["adx_trend_block"],
            "n_wins": sum(1 for r in labeled if r.get("close_reason") in WIN),
            "n_stops": sum(1 for r in labeled if r.get("close_reason") == "stop_hit"),
        },
    }
    save_calibration_payload(payload)
    return payload
