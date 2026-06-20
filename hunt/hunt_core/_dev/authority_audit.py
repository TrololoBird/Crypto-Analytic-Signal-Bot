"""Audit delivery authority invariants from hunt_outcome_ledger.jsonl.

Flags rows where ``delivered=true`` but fusion/playbook/mission gates disagree.
Run after live sessions or replay smoke.

  python -m hunt_core._dev.authority_audit
  python -m hunt_core._dev.authority_audit --path hunt/data/hunt_outcome_ledger.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

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


def audit(rows: list[dict]) -> tuple[list[str], dict[str, int]]:
    issues: list[str] = []
    stats = {
        "total": len(rows),
        "delivered": 0,
        "blocked": 0,
        "authority_violation": 0,
        "fusion_false_delivered": 0,
        "playbook_false_delivered": 0,
        "mission_false_delivered": 0,
    }
    for i, row in enumerate(rows, 1):
        delivered = bool(row.get("delivered")) or row.get("event") == "delivered"
        if delivered:
            stats["delivered"] += 1
        else:
            stats["blocked"] += 1

        if row.get("authority_violation"):
            stats["authority_violation"] += 1
            sym = row.get("symbol", "?")
            issues.append(f"line {i}: authority_violation {sym} blockers={row.get('blockers')}")

        if delivered and row.get("fusion_gate_open") is False:
            stats["fusion_false_delivered"] += 1
            issues.append(
                f"line {i}: delivered but fusion_gate_open=false "
                f"{row.get('symbol')}:{row.get('direction')}"
            )
        if delivered and row.get("playbook_pass_ok") is False:
            stats["playbook_false_delivered"] += 1
            issues.append(
                f"line {i}: delivered but playbook_pass_ok=false "
                f"{row.get('symbol')}:{row.get('direction')}"
            )
        if delivered and row.get("mission_pass") is False:
            stats["mission_false_delivered"] += 1
            issues.append(
                f"line {i}: delivered but mission_pass=false "
                f"{row.get('symbol')}:{row.get('direction')}"
            )
    return issues, stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit hunt outcome ledger authority invariants")
    parser.add_argument("--path", type=Path, default=LEDGER_PATH)
    args = parser.parse_args()
    rows = _load_rows(args.path)
    if not rows:
        print(f"authority_audit: no rows at {args.path}")
        return 0
    issues, stats = audit(rows)
    print(
        f"authority_audit path={args.path} total={stats['total']} "
        f"delivered={stats['delivered']} blocked={stats['blocked']} "
        f"violations={stats['authority_violation']}"
    )
    if stats["delivered"]:
        print(
            f"  fusion_false_delivered={stats['fusion_false_delivered']} "
            f"playbook_false_delivered={stats['playbook_false_delivered']} "
            f"mission_false_delivered={stats['mission_false_delivered']}"
        )
    for msg in issues[:30]:
        print(f"  FAIL {msg}", file=sys.stderr)
    if len(issues) > 30:
        print(f"  ... and {len(issues) - 30} more", file=sys.stderr)
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
