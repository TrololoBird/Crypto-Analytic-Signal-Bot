"""Seven measurement engines."""
from __future__ import annotations

from typing import Any

from hunt_core.deep.verdict_v2._helpers import (
    clamp01,
    conviction,
    coverage_ratio,
    information_value_from_z,
    pct_move,
    safe_float,
    trend_scores_from_snap,
)
from hunt_core.deep.verdict_v2.types import DataQualityReport, EngineOutput
from hunt_core.analysis.trend_engine import trend_from_snapshot
from hunt_core.shared.primitives.targets import (
    collect_downward_targets as _collect_downward_targets,
    collect_upward_targets as _collect_upward_targets,
)


def _pack(
    long: float,
    short: float,
    *,
    coverage: float,
    info: float,
    evidence: list[str],
    used: int,
    avail: int,
    up_pct: float = 0.0,
    down_pct: float = 0.0,
    base_priority: float = 0.15,
) -> EngineOutput:
    cov = coverage if avail <= 0 else coverage_ratio(used, avail)
    return EngineOutput(
        long=round(clamp01(long), 3),
        short=round(clamp01(short), 3),
        conviction=round(conviction(long, short), 3),
        blend_weight=round(base_priority * max(cov, 0.01), 4),
        coverage_quality=round(cov, 3),
        information_value=round(info, 3),
        evidence=evidence[:6],
        factors_used=used,
        factors_available=avail,
        upside_reward_pct=round(up_pct, 3),
        downside_reward_pct=round(down_pct, 3),
    )


def run_structural(row: dict[str, Any]) -> EngineOutput:
    structure = row.get("structure") if isinstance(row.get("structure"), dict) else {}
    bias = str(structure.get("structure_bias") or "wait")
    bos = str(structure.get("bos_direction") or "")
    htf = str(structure.get("htf_trend") or "neutral")
    choch = bool(structure.get("choch_detected"))
    evidence: list[str] = []
    long, short = 0.5, 0.5
    used = avail = 0
    if htf != "neutral":
        avail += 1
        used += 1
        if htf == "bull":
            long += 0.15
            evidence.append("htf_bull")
        else:
            short += 0.15
            evidence.append("htf_bear")
    if bias in {"long", "short"}:
        avail += 1
        used += 1
        if bias == "long":
            long += 0.2
            evidence.append("structure_bias_long")
        else:
            short += 0.2
            evidence.append("structure_bias_short")
    if bos == "bull":
        avail += 1
        used += 1
        long += 0.15
        evidence.append("bos_bull")
    elif bos == "bear":
        avail += 1
        used += 1
        short += 0.15
        evidence.append("bos_bear")
    if choch:
        avail += 1
        used += 1
        if htf == "bull":
            short += 0.1
            evidence.append("choch_vs_bull")
        elif htf == "bear":
            long += 0.1
            evidence.append("choch_vs_bear")
    info = clamp01(0.4 + used * 0.12)
    return _pack(long, short, coverage=coverage_ratio(used, max(avail, 1)), info=info, evidence=evidence, used=used, avail=max(avail, 4), base_priority=0.18)


def run_positioning(row: dict[str, Any]) -> EngineOutput:
    price = safe_float(row.get("price"))
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    structure = row.get("structure") if isinstance(row.get("structure"), dict) else {}
    regime = row.get("regime") if isinstance(row.get("regime"), dict) else {}
    evidence: list[str] = []
    used = avail = 0
    up_targets, up_f = _collect_upward_targets(row, price) if price > 0 else ([], [])
    down_targets, down_f = _collect_downward_targets(row, price) if price > 0 else ([], [])
    up_pct = down_pct = 0.0
    if price > 0 and up_targets:
        nearest_up = min(up_targets, key=lambda t: t - price)
        up_pct = pct_move(price, nearest_up)
        used += 1
        evidence.append(f"upside_{up_pct:.1f}%")
    if price > 0 and down_targets:
        nearest_down = max(down_targets, key=lambda t: price - t)
        down_pct = abs(pct_move(price, nearest_down))
        used += 1
        evidence.append(f"downside_{down_pct:.1f}%")
    avail += 2
    for f in up_f + down_f:
        if f not in evidence:
            evidence.append(f)
    poc = safe_float((structure.get("key_levels") or {}).get("poc") if isinstance(structure.get("key_levels"), dict) else 0)
    if poc <= 0:
        poc = safe_float(regime.get("poc_1h"))
    if poc > 0 and price > 0:
        avail += 1
        used += 1
        if price > poc * 1.002:
            evidence.append("above_poc")
        elif price < poc * 0.998:
            evidence.append("below_poc")
    pools = structure.get("liquidity_pools") if isinstance(structure.get("liquidity_pools"), dict) else {}
    if pools.get("nearest_above") or pools.get("nearest_below"):
        used += 1
        avail += 1
    long, short = 0.5, 0.5
    if up_pct or down_pct:
        total = up_pct + down_pct
        if total > 0:
            short += down_pct / total * 0.35
            long += up_pct / total * 0.35
    if "below_poc" in evidence:
        long += 0.12
    if "above_poc" in evidence:
        short += 0.08
    mps = safe_float(market.get("liq_magnet_pull_short_pct"))
    mpl = safe_float(market.get("liq_magnet_pull_long_pct"))
    if mps > mpl and mps > 0:
        short += 0.1
        evidence.append("liq_magnet_down")
        used += 1
    elif mpl > 0:
        long += 0.1
        evidence.append("liq_magnet_up")
        used += 1
    info = clamp01(0.35 + min(up_pct, down_pct) / 15.0 + used * 0.08)
    return _pack(long, short, coverage=coverage_ratio(used, max(avail, 6)), info=info, evidence=evidence, used=used, avail=max(avail, 6), up_pct=up_pct, down_pct=down_pct, base_priority=0.22)


def run_macro_trend(row: dict[str, Any]) -> EngineOutput:
    tf = row.get("timeframes") if isinstance(row.get("timeframes"), dict) else {}
    weights = {"1w": 0.35, "1d": 0.35, "4h": 0.30}
    long_acc = short_acc = w_sum = 0.0
    evidence: list[str] = []
    used = avail = 0
    for key, w in weights.items():
        snap = tf.get(key) or {}
        if not snap or snap.get("status") == "empty":
            continue
        avail += 1
        used += 1
        lg, sh = trend_scores_from_snap(snap)
        long_acc += lg * w
        short_acc += sh * w
        w_sum += w
        tr = trend_from_snapshot(snap, require_adx=False)
        if tr in {"bull", "bear"}:
            evidence.append(f"{key}_{tr}")
    if w_sum > 0:
        long, short = long_acc / w_sum, short_acc / w_sum
    else:
        long, short = 0.5, 0.5
    info = clamp01(0.45 + used * 0.12)
    return _pack(long, short, coverage=coverage_ratio(used, max(avail, 1)), info=info, evidence=evidence, used=used, avail=max(avail, 3), base_priority=0.18)


def run_derivatives(row: dict[str, Any]) -> EngineOutput:
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    evidence: list[str] = []
    long, short = 0.5, 0.5
    fz = safe_float(market.get("funding_zscore_48h"), float("nan"))
    oz = safe_float(market.get("oi_z"), float("nan"))
    if market.get("funding_zscore_48h") is not None and fz == fz:
        if fz > 0.5:
            short += min(0.25, fz / 8.0)
            evidence.append(f"funding_z={fz:.1f}")
        elif fz < -0.5:
            long += min(0.25, abs(fz) / 8.0)
            evidence.append(f"funding_z={fz:.1f}")
    if market.get("oi_z") is not None and oz == oz:
        oi_chg = safe_float(market.get("oi_chg_1h"))
        if oz > 1.0 and oi_chg > 0 and fz > 0:
            short += 0.12
            evidence.append("crowded_longs")
        elif oz > 1.0 and oi_chg < 0:
            short += 0.06
    top_gap = safe_float(market.get("top_vs_global_ls_gap"))
    if abs(top_gap) > 0.05:
        evidence.append("ls_gap")
        if top_gap > 0:
            short += 0.08
        else:
            long += 0.08
    basis = safe_float(market.get("premium_zscore_5m"), float("nan"))
    if basis == basis and abs(basis) > 0.5:
        if basis > 0:
            short += 0.06
        else:
            long += 0.06
    present = sum(
        1
        for k in ("funding_zscore_48h", "oi_z", "oi_chg_1h", "top_vs_global_ls_gap", "premium_zscore_5m", "funding_rate")
        if market.get(k) is not None
    )
    info = max(information_value_from_z(fz if fz == fz else None), information_value_from_z(oz if oz == oz else None), 0.5)
    return _pack(long, short, coverage=coverage_ratio(present, 6), info=info, evidence=evidence, used=present, avail=6, base_priority=0.15)


def run_flow(row: dict[str, Any]) -> EngineOutput:
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    evidence: list[str] = []
    long, short = 0.5, 0.5
    t5 = safe_float(market.get("taker_5m"), 1.0)
    t15 = safe_float(market.get("taker_15m"), 1.0)
    t1h = safe_float(market.get("taker_1h"), 1.0)
    accel = (t5 - t15) + (t15 - t1h) * 0.5
    if abs(accel) > 0.02:
        if accel > 0:
            long += min(0.2, accel)
            evidence.append("taker_accel_up")
        else:
            short += min(0.2, abs(accel))
            evidence.append("taker_accel_down")
    delta = safe_float(market.get("agg_trade_delta"))
    if delta != 0:
        if delta > 0:
            long += 0.08
            evidence.append("agg_delta_buy")
        else:
            short += 0.08
            evidence.append("agg_delta_sell")
    if market.get("map_cvd_divergence"):
        evidence.append("cvd_divergence")
        short += 0.06
    present = sum(1 for k in ("taker_5m", "taker_15m", "taker_1h", "agg_trade_delta") if market.get(k) is not None)
    info = clamp01(0.4 + abs(accel) * 2.0)
    return _pack(long, short, coverage=coverage_ratio(present, 4), info=info, evidence=evidence, used=present, avail=4, base_priority=0.15)


def run_execution_pressure(row: dict[str, Any]) -> EngineOutput:
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    ms = row.get("microstructure_by_direction") if isinstance(row.get("microstructure_by_direction"), dict) else {}
    evidence: list[str] = []
    long, short = 0.5, 0.5
    imb = safe_float(market.get("depth_imbalance") or market.get("map_book_imbalance_1pct"))
    if imb != 0:
        if imb > 0:
            long += min(0.15, imb)
            evidence.append("book_bid_heavy")
        else:
            short += min(0.15, abs(imb))
            evidence.append("book_ask_heavy")
    mp = safe_float(market.get("microprice_bias"))
    if mp != 0:
        if mp > 0:
            long += 0.08
        else:
            short += 0.08
        evidence.append("microprice")
    for direction, label in (("long", "ms_long"), ("short", "ms_short")):
        ctx = ms.get(direction)
        if ctx is None:
            continue
        score = safe_float(getattr(ctx, "bias_score", None) or (ctx.get("bias_score") if isinstance(ctx, dict) else 0))
        if direction == "long" and score > 0.1:
            long += score * 0.15
            evidence.append(label)
        elif direction == "short" and score > 0.1:
            short += score * 0.15
            evidence.append(label)
    present = sum(1 for k in ("depth_imbalance", "microprice_bias", "map_book_imbalance_1pct") if market.get(k) is not None) + (1 if ms else 0)
    info = clamp01(0.35 + present * 0.12)
    return _pack(long, short, coverage=coverage_ratio(present, 4), info=info, evidence=evidence, used=present, avail=4, base_priority=0.12)


def run_data_quality(row: dict[str, Any]) -> DataQualityReport:
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    maps = row.get("maps") if isinstance(row.get("maps"), dict) else {}
    tf = row.get("timeframes") if isinstance(row.get("timeframes"), dict) else {}
    groups = {
        "funding_oi": any(market.get(k) is not None for k in ("funding_rate", "oi", "oi_z")),
        "ls_ratios": market.get("ls_1h") is not None or market.get("top_ls_1h") is not None,
        "maps_liq": bool(maps.get("liquidation")),
        "maps_vp": bool(maps.get("volume_profile")),
        "orderbook": market.get("bid") is not None,
        "tf_htf": any((tf.get(k) or {}).get("status") != "empty" for k in ("1d", "4h", "1w")),
        "cross_ms": bool(row.get("cross_microstructure")),
    }
    missing = [k for k, ok in groups.items() if not ok]
    score = coverage_ratio(len(groups) - len(missing), len(groups))
    return DataQualityReport(coverage_score=round(score, 3), missing_groups=missing)


def run_all_engines(row: dict[str, Any]) -> dict[str, EngineOutput]:
    return {
        "structural": run_structural(row),
        "positioning": run_positioning(row),
        "macro_trend": run_macro_trend(row),
        "derivatives": run_derivatives(row),
        "flow": run_flow(row),
        "execution_pressure": run_execution_pressure(row),
    }
