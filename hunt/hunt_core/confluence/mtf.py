"""MTF family-voting confluence (P6 — extracted from deep_signal)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

@dataclass
class TFSignal:
    tf: str
    trend: Literal["bull", "bear", "neutral"]
    rsi14: float
    adx14: float
    label: str


@dataclass
class ScenarioScore:
    direction: Literal["long", "short"]
    score: float            # 0..1
    htf_count: int          # how many of 1W/1D/4H align with this direction
    htf_total: int          # how many HTF TFs had data
    entry_lo: float
    entry_hi: float
    tp1: float
    tp2: float
    stop: float
    evidence: list[str] = field(default_factory=list)


@dataclass
class MTFConfluence:
    symbol: str
    price: float
    tf_signals: dict[str, TFSignal]
    long_scenario: ScenarioScore
    short_scenario: ScenarioScore
    dominant: Literal["long", "short", "neutral"]


_DISPLAY_TFS = ["1w", "1d", "4h", "15m"]
_HTF_TFS = ["1w", "1d", "4h"]


def _trend_from_snap(snap: dict[str, Any]) -> Literal["bull", "bear", "neutral"]:
    # Prefer the canonical EMA-stack computation over the cached "trend" string.
    # The cached value may be "mixed" (neutral) even when close < ema20 < ema50
    # (post-pump dump: ema200 still below due to pre-pump history, so the full
    # 4-EMA bear stack fails despite a clear 3-EMA bearish alignment).
    from hunt_core.analysis.trend_engine import trend_from_snapshot

    recomputed = trend_from_snapshot(snap, require_adx=False)
    if recomputed in ("bull", "bear"):
        return recomputed  # type: ignore[return-value]
    # Fall back to cached label (catches old snapshots with pre-computed trend).
    t = snap.get("trend") or ""
    if t == "bull":
        return "bull"
    if t == "bear":
        return "bear"
    return "neutral"


def _tf_label(snap: dict[str, Any], trend: str) -> str:
    adx = float(snap.get("adx14") or 0)
    sup = snap.get("supertrend_dir")
    rsi = float(snap.get("rsi14") or 50)
    if trend == "bull":
        if adx >= 25:
            return "Сильный бычий тренд"
        if sup == 1:
            return "Supertrend бычий"
        return "Выше EMA50"
    if trend == "bear":
        if adx >= 25:
            return "Сильный медвежий тренд"
        if sup == -1:
            return "Supertrend медвежий"
        return "Ниже EMA50"
    if rsi > 62:
        return "Импульс восходящий"
    if rsi < 38:
        return "Импульс нисходящий"
    return "EMA переплетены"


def _rsi_edge(rsi: float, direction: str) -> float:
    """0..1 momentum edge for the given direction from RSI."""
    if direction == "long":
        return max(0.0, min(1.0, (rsi - 40.0) / 30.0))
    return max(0.0, min(1.0, (60.0 - rsi) / 30.0))


def build_mtf_confluence(
    symbol: str,
    tf: dict[str, Any],
    price: float,
    *,
    market: dict[str, Any] | None = None,
    row: dict[str, Any] | None = None,
) -> MTFConfluence:
    """
    Build MTF confluence from row['timeframes'] (already contains per-TF snapshots).

    Args:
        symbol: e.g. "BTCUSDT"
        tf: row["timeframes"] dict — keys "1w","1d","4h","15m","1h",…
        price: current mark price
    """
    tf_signals: dict[str, TFSignal] = {}
    for key in _DISPLAY_TFS:
        snap = tf.get(key) or {}
        if not snap or snap.get("status") == "empty":
            continue
        trend = _trend_from_snap(snap)
        rsi = float(snap.get("rsi14") or 50)
        adx = float(snap.get("adx14") or 0)
        tf_signals[key] = TFSignal(
            tf=key,
            trend=trend,
            rsi14=rsi,
            adx14=adx,
            label=_tf_label(snap, trend),
        )

    # ATR from best available TF for level placement
    atr = 0.0
    for k in ("4h", "1d", "1h", "15m"):
        v = float((tf.get(k) or {}).get("atr14") or 0)
        if v > 0:
            atr = v
            break
    if atr <= 0:
        atr = price * 0.01

    def _build(direction: str) -> ScenarioScore:
        # HTF score — only count TFs with a determinate trend (bull or bear).
        # A neutral TF (no EMA data in fast-tier scan) provides zero signal and
        # should not inflate htf_total: that would make family_vote_low fire when
        # the only available HTF (4H) correctly aligns with direction.
        htf_aligned = 0
        htf_total = 0
        evidence: list[str] = []
        for k in _HTF_TFS:
            sig = tf_signals.get(k)
            if sig is None or sig.trend == "neutral":
                continue
            htf_total += 1
            ok = (direction == "long" and sig.trend == "bull") or (
                direction == "short" and sig.trend == "bear"
            )
            if ok:
                htf_aligned += 1
                evidence.append(f"{k.upper()}: {sig.label}")

        htf_ratio = htf_aligned / htf_total if htf_total else 0.0

        # LTF momentum (15M, fallback 1H)
        ltf_snap = tf.get("15m") or tf.get("1h") or {}
        ltf_rsi = float(ltf_snap.get("rsi14") or 50)
        ltf_edge = _rsi_edge(ltf_rsi, direction)

        score = round(htf_ratio * 0.60 + ltf_edge * 0.40, 3)

        if direction == "long":
            entry_lo = price - 0.3 * atr
            entry_hi = price + 0.3 * atr
            tp1 = price + 2.0 * atr
            tp2 = price + 4.0 * atr
            stop = price - 1.5 * atr
        else:
            entry_lo = price - 0.3 * atr
            entry_hi = price + 0.3 * atr
            tp1 = price - 2.0 * atr
            tp2 = price - 4.0 * atr
            stop = price + 1.5 * atr

        if htf_total:
            evidence.insert(0, f"HTF {htf_aligned}/{htf_total}")

        return ScenarioScore(
            direction=direction,  # type: ignore[arg-type]
            score=score,
            htf_count=htf_aligned,
            htf_total=htf_total,
            entry_lo=round(entry_lo, 6),
            entry_hi=round(entry_hi, 6),
            tp1=round(tp1, 6),
            tp2=round(tp2, 6),
            stop=round(stop, 6),
            evidence=evidence,
        )

    long_s = _build("long")
    short_s = _build("short")

    liq_pack = None
    if row is not None:
        from hunt_core.analysis.deep_signal import (  # noqa: PLC0415
            apply_liquidity_to_mtf_scores,
            build_liquidity_scenarios,
        )

        raw_liq = row.get("liquidity_scenarios")
        if raw_liq is not None:
            liq_pack = raw_liq if hasattr(raw_liq, "scenarios") else None
        if liq_pack is None and (row.get("market") or market):
            liq_pack = build_liquidity_scenarios({**row, "market": market or row.get("market")})
    if liq_pack is not None:
        mkt = market or row.get("market") if row else market
        ls, ss, notes = apply_liquidity_to_mtf_scores(
            long_s.score, short_s.score, liq_pack, market=mkt if isinstance(mkt, dict) else None
        )
        long_s = ScenarioScore(
            direction=long_s.direction,
            score=ls,
            htf_count=long_s.htf_count,
            htf_total=long_s.htf_total,
            entry_lo=long_s.entry_lo,
            entry_hi=long_s.entry_hi,
            tp1=long_s.tp1,
            tp2=long_s.tp2,
            stop=long_s.stop,
            evidence=[*long_s.evidence, *notes],
        )
        short_s = ScenarioScore(
            direction=short_s.direction,
            score=ss,
            htf_count=short_s.htf_count,
            htf_total=short_s.htf_total,
            entry_lo=short_s.entry_lo,
            entry_hi=short_s.entry_hi,
            tp1=short_s.tp1,
            tp2=short_s.tp2,
            stop=short_s.stop,
            evidence=[*short_s.evidence, *notes],
        )

    if long_s.score >= short_s.score + 0.15:
        dominant: Literal["long", "short", "neutral"] = "long"
    elif short_s.score >= long_s.score + 0.15:
        dominant = "short"
    else:
        dominant = "neutral"

    if row is not None:
        dump = row.get("dump") if isinstance(row.get("dump"), dict) else {}
        long_setup = row.get("long") if isinstance(row.get("long"), dict) else {}
        short_ok = dump.get("levels_viable") is not False
        long_ok = long_setup.get("levels_viable") is not False
        if not short_ok or not long_ok:
            dominant = "neutral"

    return MTFConfluence(
        symbol=symbol,
        price=price,
        tf_signals=tf_signals,
        long_scenario=long_s,
        short_scenario=short_s,
        dominant=dominant,
    )


def _scenario_to_dict(sc: ScenarioScore) -> dict[str, Any]:
    return {
        "direction": sc.direction,
        "score": round(float(sc.score), 4),
        "htf_count": int(sc.htf_count),
        "htf_total": int(sc.htf_total),
        "entry_lo": float(sc.entry_lo),
        "entry_hi": float(sc.entry_hi),
        "tp1": float(sc.tp1),
        "tp2": float(sc.tp2),
        "stop": float(sc.stop),
        "evidence": list(sc.evidence),
    }


def mtf_confluence_to_dict(mtf: MTFConfluence) -> dict[str, Any]:
    """JSONL-safe MTF payload with HTF counts for ``family_vote_count`` replay."""
    return {
        "symbol": mtf.symbol,
        "price": float(mtf.price),
        "dominant": mtf.dominant,
        "long_scenario": _scenario_to_dict(mtf.long_scenario),
        "short_scenario": _scenario_to_dict(mtf.short_scenario),
        "long_htf_count": int(mtf.long_scenario.htf_count),
        "short_htf_count": int(mtf.short_scenario.htf_count),
        "long_htf_total": int(mtf.long_scenario.htf_total),
        "short_htf_total": int(mtf.short_scenario.htf_total),
    }


# Bind method on dataclass for tick_jsonl mtf_to_json_dict().
MTFConfluence.to_dict = mtf_confluence_to_dict  # type: ignore[method-assign]
