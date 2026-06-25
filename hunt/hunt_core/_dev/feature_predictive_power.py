"""Predictive-power harness for the Scanner (pre-pump / pre-dump) module.

The central question for a "find the move BEFORE the move" scanner is NOT
"are there bugs" but: do its features and score actually PRECEDE moves, or is
it labelling noise? This tool answers that empirically.

Method (no leakage — uses only state recorded in the tick, fetches future
prices separately):

  1. Read ``data/hunt_scan.jsonl`` — every scanned tick already carries
     ``manipulation_fusion`` (primary_score 0-100, archetype, weighted factors,
     sub-scores score_coil / score_ignition / score_predump) and ``price``/``ts``.
  2. For each tick, fetch forward klines [ts, ts+horizon] and measure the
     favorable excursion in the archetype's intended direction (long → max up,
     short/dump → max down).
  3. Label a "win" when the favorable move reaches ``--win-pct`` within horizon.
  4. Emit three tables: score-decile, per-factor, per-archetype.

A useful score shows MONOTONE lift: higher score bucket → higher win-rate /
bigger move. A flat table means the score has no predictive power regardless of
how elegant its formula is.

    .venv/bin/python -m hunt_core._dev.feature_predictive_power
    .venv/bin/python -m hunt_core._dev.feature_predictive_power --horizon 12h --win-pct 5

100% CCXT market plane (``fetch_klines_sync``). Public data only.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from hunt_core.market.factory import fetch_klines_sync
from hunt_core.paths import HUNT_SCAN_JSONL

_HORIZONS_H = {"3h": 3, "6h": 6, "12h": 12, "24h": 24, "48h": 48}
_RESOLVE_INTERVAL = "15m"


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _direction(archetype: str) -> str:
    a = archetype.lower()
    if "dump" in a or "short" in a or "distribution" in a:
        return "short"
    if "pump" in a or "long" in a or "coil" in a or "ignition" in a or "accum" in a:
        return "long"
    return "long"


class _Acc:
    """Win/loss + excursion accumulator."""

    __slots__ = ("n", "wins", "fav_sum", "adv_sum")

    def __init__(self) -> None:
        self.n = 0
        self.wins = 0
        self.fav_sum = 0.0
        self.adv_sum = 0.0

    def add(self, fav: float, adv: float, win: bool) -> None:
        self.n += 1
        self.wins += 1 if win else 0
        self.fav_sum += fav
        self.adv_sum += adv

    def row(self) -> tuple[int, float, float, float]:
        if self.n == 0:
            return 0, 0.0, 0.0, 0.0
        return self.n, self.wins / self.n * 100.0, self.fav_sum / self.n, self.adv_sum / self.n


def _load_ticks(min_age_h: float, min_score: float) -> list[dict[str, Any]]:
    if not HUNT_SCAN_JSONL.exists():
        return []
    now = datetime.now(UTC)
    out: list[dict[str, Any]] = []
    for line in HUNT_SCAN_JSONL.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        mf = rec.get("manipulation_fusion") if isinstance(rec.get("manipulation_fusion"), dict) else {}
        score = mf.get("primary_score")
        price = rec.get("price")
        ts = _parse_ts(str(rec.get("ts") or ""))
        if not isinstance(score, (int, float)) or not price or ts is None:
            continue
        if float(score) < min_score:
            continue
        if (now - ts).total_seconds() / 3600.0 < min_age_h:
            continue
        rec["_ts"] = ts
        rec["_score"] = float(score)
        rec["_price"] = float(price)
        rec["_mf"] = mf
        out.append(rec)
    return out


def _excursion(klines: list[list[Any]], entry: float, until_ms: int) -> tuple[float, float]:
    """(max favorable up %, max adverse down %) — both as positive magnitudes."""
    hi_max = entry
    lo_min = entry
    for k in klines:
        if int(k[0]) > until_ms:
            break
        hi_max = max(hi_max, float(k[2]))
        lo_min = min(lo_min, float(k[3]))
    up = (hi_max - entry) / entry * 100.0 if entry > 0 else 0.0
    down = (entry - lo_min) / entry * 100.0 if entry > 0 else 0.0
    return up, down


def _print_table(title: str, rows: list[tuple[str, _Acc]]) -> None:
    print(f"\n=== {title} ===")
    print(f"{'bucket':28} {'n':>4} {'win%':>6} {'avg fav%':>9} {'avg adv%':>9}")
    print("-" * 60)
    for label, acc in rows:
        n, win, fav, adv = acc.row()
        if n == 0:
            continue
        print(f"{label:28.28} {n:>4} {win:>5.0f}% {fav:>+8.2f} {adv:>+8.2f}")


def run(horizon: str, win_pct: float, min_age_h: float, min_score: float,
        proxy_url: str | None, trust_env: bool) -> None:
    hz_h = _HORIZONS_H[horizon]
    ticks = _load_ticks(min_age_h, min_score)
    if not ticks:
        print(f"No scan ticks with score>={min_score} and age>={min_age_h}h in {HUNT_SCAN_JSONL.name}.")
        print("(The scanner needs to have run long enough that ticks are older than the horizon.)")
        return

    by_decile: dict[int, _Acc] = defaultdict(_Acc)
    by_factor: dict[str, _Acc] = defaultdict(_Acc)
    by_archetype: dict[str, _Acc] = defaultdict(_Acc)
    overall = _Acc()
    resolved = 0
    klines_cache: dict[tuple[str, int], list[list[Any]]] = {}

    for rec in ticks:
        sym = str(rec.get("symbol") or "")
        mf = rec["_mf"]
        archetype = str(mf.get("archetype") or "?")
        direction = _direction(archetype)
        entry = rec["_price"]
        ts: datetime = rec["_ts"]
        since_ms = int(ts.timestamp() * 1000)
        until_ms = since_ms + hz_h * 3600 * 1000
        cache_key = (sym, since_ms // (3600 * 1000))  # 1h granularity reuse
        klines = klines_cache.get(cache_key)
        if klines is None:
            try:
                klines = fetch_klines_sync(
                    sym, _RESOLVE_INTERVAL,
                    since_ms=since_ms, until_ms=until_ms,
                    limit=1500, proxy_url=proxy_url, trust_env=trust_env,
                )
            except Exception as exc:  # noqa: BLE001 — offline tool
                print(f"  ! {sym} fetch failed: {exc}")
                continue
            klines_cache[cache_key] = klines
        if not klines:
            continue
        up, down = _excursion(klines, entry, until_ms)
        fav = up if direction == "long" else down
        adv = down if direction == "long" else up
        win = fav >= win_pct
        resolved += 1

        overall.add(fav, adv, win)
        decile = min(9, int(rec["_score"] // 10))
        by_decile[decile].add(fav, adv, win)
        by_archetype[archetype].add(fav, adv, win)
        for f in (mf.get("factors") or []):
            if isinstance(f, dict) and f.get("name"):
                by_factor[str(f["name"])].add(fav, adv, win)

    print(f"\nResolved {resolved}/{len(ticks)} ticks @ {horizon}, win = favorable move >= {win_pct:.1f}%")
    n, win, fav, adv = overall.row()
    print(f"BASELINE (all ticks): n={n} win={win:.0f}% avg_fav={fav:+.2f}% avg_adv={adv:+.2f}%")

    _print_table(
        f"Score decile (primary_score) @ {horizon}",
        [(f"{d * 10}-{d * 10 + 9}", by_decile[d]) for d in sorted(by_decile)],
    )
    _print_table(
        f"Per-archetype @ {horizon}",
        sorted(by_archetype.items(), key=lambda kv: -kv[1].row()[1]),
    )
    _print_table(
        f"Per-factor (present) @ {horizon} — ranked by win%",
        sorted(by_factor.items(), key=lambda kv: -kv[1].row()[1]),
    )
    print(
        "\nRead: a predictive score shows monotone lift across deciles; a factor "
        "beats baseline win% only if it truly precedes the move."
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Scanner feature/score predictive-power study.")
    ap.add_argument("--horizon", choices=sorted(_HORIZONS_H), default="24h")
    ap.add_argument("--win-pct", type=float, default=5.0, help="Favorable move %% that counts as a hit.")
    ap.add_argument("--min-age-h", type=float, default=None, help="Default = horizon hours.")
    ap.add_argument("--min-score", type=float, default=0.0)
    ap.add_argument("--proxy-url", default=None)
    ap.add_argument("--no-trust-env", action="store_true")
    args = ap.parse_args()
    min_age = args.min_age_h if args.min_age_h is not None else float(_HORIZONS_H[args.horizon])
    run(args.horizon, args.win_pct, min_age, args.min_score, args.proxy_url, not args.no_trust_env)


if __name__ == "__main__":
    main()
