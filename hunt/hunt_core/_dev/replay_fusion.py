"""No-lookahead replay of the fusion engine over the parquet lake (not pytest).

Run as ``python -m hunt_core._dev.replay_fusion --all``. Walks each per-symbol 15m
parquet chronologically; at every bar ``i`` it builds the trailing window from
``df[0:i+1]`` only (the same FeatureWindow the live path uses), feeds the gate the
fused-magnitude history of strictly-earlier bars, and runs ``build_detection``. When the
gate opens it records a hypothetical entry and scores the **forward** outcome from later
bars — so the harness never peeks ahead.

The lake stores closes (no intrabar high/low), so the outcome is a close-to-close,
ATR-scaled first-touch test in the predicted direction: a *hit* is the entry reaching
``±target_atr × ATR`` on the right side before the opposite side, within ``horizon``
bars. The target is the symbol's own ATR — self-calibrated, no fixed price target.

Metrics are printed, never written to a file:
- precision   = hits / signals (overall and per side)
- coverage    = signals / eligible bars (alert rate; too-loose gate shows here)
- abstain     = fraction of eligible bars with no tradable side (cold-start / thin)
- lead time   = bars from signal to target (validates PRE, not MID)
- per-factor  = mean |score| on hit vs miss signals (factor-quality audit)
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from dataclasses import dataclass, field

import polars as pl

from hunt_core.scanner.detect import build_detection
from hunt_core.scanner.detect.fusion import vol_adjusted_magnitude
from hunt_core.scanner.detect.magnitude_cache import clear_magnitude_cache
from hunt_core.scanner.detect.phase import clear_phase_sticky, phase_sticky_enabled
from hunt_core.scanner.detect.windows import DEFAULT_LOOKBACK, build_window
from hunt_core.paths import LAKE_PARQUET

DEFAULT_WARMUP = 60
DEFAULT_HORIZON = 16  # bars ahead to resolve the outcome (16 × 15m = 4h)
DEFAULT_TARGET_ATR = 1.5  # ATR multiple defining a "hit" (measurement scale, not a gate)


@dataclass
class ReplayMetrics:
    symbol: str
    bars: int = 0
    eligible: int = 0
    abstain: int = 0
    signals: int = 0
    hits: int = 0
    leads: list[int] = field(default_factory=list)
    forward_returns: list[float] = field(default_factory=list)
    random_hits: int = 0
    random_trials: int = 0
    by_side: dict[str, list[int]] = field(default_factory=lambda: {"long": [0, 0], "short": [0, 0]})
    hit_factor_abs: dict[str, list[float]] = field(default_factory=dict)
    miss_factor_abs: dict[str, list[float]] = field(default_factory=dict)
    hit_quarantine_abs: dict[str, list[float]] = field(default_factory=dict)
    miss_quarantine_abs: dict[str, list[float]] = field(default_factory=dict)

    @property
    def precision(self) -> float:
        return self.hits / self.signals if self.signals else 0.0

    @property
    def coverage(self) -> float:
        return self.signals / self.eligible if self.eligible else 0.0

    @property
    def abstain_rate(self) -> float:
        return self.abstain / self.eligible if self.eligible else 0.0

    @property
    def avg_lead(self) -> float:
        return sum(self.leads) / len(self.leads) if self.leads else 0.0


def load_symbol_lake(symbol: str, *, tf: str = "15m") -> pl.DataFrame:
    path = LAKE_PARQUET / symbol.upper() / f"{tf}.parquet"
    if not path.exists():
        return pl.DataFrame()
    df = pl.read_parquet(path)
    if df.height and "ts" in df.columns:
        df = df.sort("ts")
    return df


def _atr_at(df: pl.DataFrame, i: int, price: float) -> float | None:
    """Absolute ATR at bar i from atr14, else atr_pct, else None."""
    if "atr14" in df.columns:
        v = df["atr14"][i]
        if v is not None and float(v) > 0:
            return float(v)
    if "atr_pct" in df.columns:
        v = df["atr_pct"][i]
        if v is not None and float(v) > 0:
            return float(v) / 100.0 * price
    return None


def _forward_outcome(
    closes: list[float], i: int, side: str, entry: float, atr: float, *, horizon: int, k: float
) -> tuple[bool | None, int]:
    """First-touch outcome on forward closes; (hit?, lead_bars). None == unresolved."""
    target = entry + k * atr if side == "long" else entry - k * atr
    stop = entry - k * atr if side == "long" else entry + k * atr
    end = min(len(closes), i + 1 + horizon)
    for j in range(i + 1, end):
        c = closes[j]
        if side == "long":
            if c >= target:
                return True, j - i
            if c <= stop:
                return False, j - i
        else:
            if c <= target:
                return True, j - i
            if c >= stop:
                return False, j - i
    return None, 0


def _forward_return_pct(
    closes: list[float], i: int, side: str, *, horizon: int
) -> float | None:
    if i + horizon >= len(closes) or closes[i] <= 0:
        return None
    raw = (closes[i + horizon] / closes[i] - 1.0) * 100.0
    return raw if side == "long" else -raw


def _random_baseline_atr(
    closes: list[float],
    i: int,
    *,
    horizon: int,
    k: float,
    atr: float,
) -> bool | None:
    """Naive random-direction baseline at the same bar (50/50 long/short)."""
    import random

    side = "long" if random.random() >= 0.5 else "short"
    hit, _ = _forward_outcome(closes, i, side, closes[i], atr, horizon=horizon, k=k)
    return hit


def replay(
    symbol: str,
    *,
    warmup: int = DEFAULT_WARMUP,
    q_gate: float = 0.90,
    horizon: int = DEFAULT_HORIZON,
    target_atr: float = DEFAULT_TARGET_ATR,
    lookback: int = DEFAULT_LOOKBACK,
    walk_forward_frac: float | None = None,
) -> ReplayMetrics:
    m = ReplayMetrics(symbol=symbol.upper())
    df = load_symbol_lake(symbol)
    m.bars = df.height
    if df.height <= warmup + horizon:
        return m
    clear_magnitude_cache()
    clear_phase_sticky()
    score_from = 0
    if walk_forward_frac is not None and 0.0 < walk_forward_frac < 1.0:
        score_from = int(df.height * (1.0 - walk_forward_frac))
        warmup = max(warmup, score_from)

    closes = [float(x) for x in (df["close"] if "close" in df.columns else df["price"]).to_list()]

    mag_hist: list[float] = []
    for i in range(df.height):
        window = build_window(df.head(i + 1), symbol=symbol, lookback=lookback)
        hist = pl.Series(mag_hist, dtype=pl.Float64) if mag_hist else None
        det = build_detection(window, magnitude_history=hist, q_gate=q_gate)
        atr_pct = window.last("atr_pct")
        mag_hist.append(vol_adjusted_magnitude(det.fusion.magnitude, atr_pct))

        if i < warmup or i >= df.height - horizon:
            continue
        if walk_forward_frac is not None and i < score_from:
            continue
        m.eligible += 1
        if det.side == "none":
            m.abstain += 1

        atr = _atr_at(df, i, closes[i])
        if atr is not None:
            rnd = _random_baseline_atr(
                closes, i, horizon=horizon, k=target_atr, atr=atr
            )
            if rnd is not None:
                m.random_trials += 1
                if rnd:
                    m.random_hits += 1

        if not det.gate_open:
            continue

        if atr is None:
            continue
        hit, lead = _forward_outcome(
            closes, i, det.side, closes[i], atr, horizon=horizon, k=target_atr
        )
        if hit is None:
            continue
        m.signals += 1
        fr = _forward_return_pct(closes, i, det.side, horizon=horizon)
        if fr is not None:
            m.forward_returns.append(fr)
        self_side = m.by_side[det.side]
        self_side[1] += 1
        bucket = m.hit_factor_abs if hit else m.miss_factor_abs
        for f in det.active_factors:
            bucket.setdefault(f.name, []).append(abs(f.score))
        q_bucket = m.hit_quarantine_abs if hit else m.miss_quarantine_abs
        for f in det.quarantine_factors:
            if f.active:
                q_bucket.setdefault(f.name, []).append(abs(f.score))
        if hit:
            m.hits += 1
            self_side[0] += 1
            m.leads.append(lead)
    return m


def replay_phase_mix(
    symbol: str,
    *,
    warmup: int = DEFAULT_WARMUP,
    q_gate: float = 0.90,
    lookback: int = DEFAULT_LOOKBACK,
    sticky: bool = True,
) -> Counter[str]:
    """Count lifecycle phases over eligible bars — sticky on/off A/B (P0-C)."""
    prev = os.environ.get("HUNT_PHASE_NO_STICKY")
    if sticky:
        os.environ.pop("HUNT_PHASE_NO_STICKY", None)
    else:
        os.environ["HUNT_PHASE_NO_STICKY"] = "1"
    try:
        df = load_symbol_lake(symbol)
        if df.height <= warmup:
            return Counter()
        clear_magnitude_cache()
        clear_phase_sticky()
        phases: Counter[str] = Counter()
        mag_hist: list[float] = []
        for i in range(df.height):
            window = build_window(df.head(i + 1), symbol=symbol, lookback=lookback)
            hist = pl.Series(mag_hist, dtype=pl.Float64) if mag_hist else None
            det = build_detection(window, magnitude_history=hist, q_gate=q_gate)
            atr_pct = window.last("atr_pct")
            mag_hist.append(vol_adjusted_magnitude(det.fusion.magnitude, atr_pct))
            if i < warmup:
                continue
            phases[str(det.phase or "unknown")] += 1
        return phases
    finally:
        if prev is None:
            os.environ.pop("HUNT_PHASE_NO_STICKY", None)
        else:
            os.environ["HUNT_PHASE_NO_STICKY"] = prev


def _print_phase_ab(symbol: str, *, warmup: int, q_gate: float) -> None:
    on = replay_phase_mix(symbol, warmup=warmup, q_gate=q_gate, sticky=True)
    off = replay_phase_mix(symbol, warmup=warmup, q_gate=q_gate, sticky=False)
    total_on = sum(on.values()) or 1
    total_off = sum(off.values()) or 1
    print(f"\n== {symbol.upper()} phase A/B (sticky on vs HUNT_PHASE_NO_STICKY=1) ==")
    keys = sorted(set(on) | set(off))
    for k in keys:
        on_pct = 100.0 * on.get(k, 0) / total_on
        off_pct = 100.0 * off.get(k, 0) / total_off
        print(f"  {k:<12} sticky={on.get(k, 0):4d} ({on_pct:5.1f}%)  no_sticky={off.get(k, 0):4d} ({off_pct:5.1f}%)")
    print(f"  sticky_enabled_now={phase_sticky_enabled()}")


def _print_metrics(m: ReplayMetrics) -> None:
    print(f"\n== {m.symbol} ==")
    print(f"  bars={m.bars} eligible={m.eligible} signals={m.signals} hits={m.hits}")
    print(
        f"  precision={m.precision:.2%} coverage={m.coverage:.2%} "
        f"abstain={m.abstain_rate:.2%} avg_lead={m.avg_lead:.1f} bars"
    )
    if m.forward_returns:
        avg_fr = sum(m.forward_returns) / len(m.forward_returns)
        print(f"  avg_forward_return={avg_fr:+.2f}% (close→close, signal bars only)")
    if m.random_trials:
        rnd_prec = m.random_hits / m.random_trials
        lift = (m.precision / rnd_prec - 1.0) if rnd_prec > 0 else float("nan")
        print(
            f"  random_baseline_atr={rnd_prec:.2%} lift={lift:+.1%} "
            f"(n={m.random_trials})"
        )
    for side in ("long", "short"):
        h, n = m.by_side[side]
        if n:
            print(f"    {side}: {h}/{n} = {h / n:.2%}")
    keys = sorted(set(m.hit_factor_abs) | set(m.miss_factor_abs))
    for k in keys:
        hv = m.hit_factor_abs.get(k, [])
        mv = m.miss_factor_abs.get(k, [])
        hm = sum(hv) / len(hv) if hv else 0.0
        mm = sum(mv) / len(mv) if mv else 0.0
        print(f"    factor {k:<12} |z| hit={hm:.2f} miss={mm:.2f}")
    qkeys = sorted(set(m.hit_quarantine_abs) | set(m.miss_quarantine_abs))
    for k in qkeys:
        hv = m.hit_quarantine_abs.get(k, [])
        mv = m.miss_quarantine_abs.get(k, [])
        hm = sum(hv) / len(hv) if hv else 0.0
        mm = sum(mv) / len(mv) if mv else 0.0
        print(f"    quarantine {k:<12} |z| hit={hm:.2f} miss={mm:.2f}")


def _lake_symbols() -> list[str]:
    if not LAKE_PARQUET.exists():
        return []
    return [
        c.name
        for c in sorted(LAKE_PARQUET.iterdir())
        if c.is_dir() and not c.name.startswith("symbol=") and (c / "15m.parquet").exists()
    ]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="No-lookahead fusion-engine replay over the lake")
    p.add_argument("--symbol", default="")
    p.add_argument("--all", action="store_true", help="replay every lake symbol")
    p.add_argument("--warmup", type=int, default=DEFAULT_WARMUP)
    p.add_argument("--q-gate", type=float, default=0.90)
    p.add_argument("--horizon-bars", type=int, default=DEFAULT_HORIZON)
    p.add_argument("--target-atr", type=float, default=DEFAULT_TARGET_ATR)
    p.add_argument(
        "--walk-forward",
        type=float,
        default=0.0,
        metavar="FRAC",
        help="Score only the last FRAC of bars (e.g. 0.3 = out-of-sample tail)",
    )
    p.add_argument(
        "--train-frac",
        type=float,
        default=0.0,
        metavar="FRAC",
        help="In-sample fraction; when --walk-forward unset, holdout = 1-train_frac",
    )
    p.add_argument(
        "--phase-ab",
        action="store_true",
        help="Print MID/pre phase mix with sticky on vs HUNT_PHASE_NO_STICKY=1",
    )
    args = p.parse_args(argv)

    if args.phase_ab:
        sym = (args.symbol or "").upper()
        if not sym:
            syms = _lake_symbols()
            if not syms:
                print("no lake parquet under", LAKE_PARQUET)
                return 1
            sym = syms[0]
        _print_phase_ab(sym, warmup=args.warmup, q_gate=args.q_gate)
        return 0

    walk_forward = args.walk_forward
    if walk_forward <= 0 and args.train_frac > 0:
        walk_forward = max(0.0, min(1.0, 1.0 - args.train_frac))
    if args.train_frac > 0:
        print(
            f"replay split: train_frac={args.train_frac:.2f} "
            f"holdout_frac={1.0 - args.train_frac:.2f} walk_forward={walk_forward:.2f}"
        )

    if args.all or not args.symbol:
        symbols = _lake_symbols()
    else:
        symbols = [args.symbol.upper()]
    if not symbols:
        print("no lake parquet under", LAKE_PARQUET)
        return 1

    agg = ReplayMetrics(symbol="ALL")
    for sym in symbols:
        m = replay(
            sym,
            warmup=args.warmup,
            q_gate=args.q_gate,
            horizon=args.horizon_bars,
            target_atr=args.target_atr,
            walk_forward_frac=walk_forward if walk_forward > 0 else None,
        )
        _print_metrics(m)
        agg.eligible += m.eligible
        agg.abstain += m.abstain
        agg.signals += m.signals
        agg.hits += m.hits
        agg.leads.extend(m.leads)
    print("\n== AGGREGATE ==")
    print(
        f"  signals={agg.signals} hits={agg.hits} precision={agg.precision:.2%} "
        f"coverage={agg.coverage:.2%} abstain={agg.abstain_rate:.2%} avg_lead={agg.avg_lead:.1f}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
