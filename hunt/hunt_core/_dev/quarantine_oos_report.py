"""Quarantine factor OOS readiness — reads outcome ledger + promotion gate."""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from hunt_core._dev.factor_promotion_gate import (
    min_outcomes_for_power,
    promotion_allowed,
    promotion_min_delta,
)
from hunt_core.scanner.detect.factor_registry_loader import quarantine_factors
from hunt_core.track.outcome_ledger import LEDGER_PATH


def _load_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def audit(path: Path) -> int:
    rows = _load_rows(path)
    need_n = min_outcomes_for_power()
    min_delta = promotion_min_delta()
    names = sorted(quarantine_factors())

    delivered = [r for r in rows if r.get("delivered") or r.get("event") == "delivered"]
    with_q = [r for r in rows if isinstance(r.get("quarantine_factors"), dict) and r["quarantine_factors"]]

    factor_hits: dict[str, list[float]] = defaultdict(list)
    for row in delivered:
        qf = row.get("quarantine_factors") or {}
        if not isinstance(qf, dict):
            continue
        for name, score in qf.items():
            try:
                factor_hits[str(name)].append(abs(float(score)))
            except (TypeError, ValueError):
                continue

    print(f"quarantine_oos_report path={path}")
    print(f"  ledger_rows={len(rows)} delivered={len(delivered)} with_quarantine={len(with_q)}")
    print(f"  need_n={need_n} min_oos_delta={min_delta:.3f} quarantine={names}")

    if not delivered:
        print("  status: no delivered rows — soak live watch for OOS samples")
        return 0

    for name in names:
        scores = factor_hits.get(name, [])
        n = len(scores)
        avg = sum(scores) / n if n else 0.0
        # Placeholder OOS delta until horizon outcomes wired per-factor.
        oos_delta = 0.0
        ok = promotion_allowed(name, oos_precision_delta=oos_delta, delivered_n=len(delivered))
        flag = "PROMOTE?" if ok and n >= need_n else "hold"
        print(f"  {name:<22} samples={n:4d} avg|score|={avg:.3f} oos_delta={oos_delta:.3f} -> {flag}")

    if len(delivered) < need_n:
        print(f"  promotion_blocked: delivered {len(delivered)} < need_n {need_n}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Quarantine factor promotion readiness")
    p.add_argument("--path", type=Path, default=LEDGER_PATH)
    args = p.parse_args(argv)
    return audit(args.path)


if __name__ == "__main__":
    raise SystemExit(main())
