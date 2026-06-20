"""Cross-layer audit: empty telemetry, geometry drift, stale tracker rows."""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hunt_core.paths import PREP_SHADOW_EVENTS, SIGNAL_EVENTS, SIGNAL_HISTORY, SIGNAL_STATE
from hunt_core.track.tracker import _is_signal_active, _mfe_pct, load_tracker_state

# funnel_deliver tier recorded after dispatch fix (2026-06-17 ~02:00 UTC).
_FUNNEL_TIER_FIX_CUTOFF = "2026-06-17T02:00:00"


def _audit_telemetry(events_path: Path, *, since: str) -> list[str]:
    issues: list[str] = []
    if not events_path.exists():
        return ["signal_events.jsonl missing"]
    no_code = funnel_no_tier = 0
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        ts = str(r.get("ts") or "")[:19]
        if ts < since:
            continue
        if r.get("event") == "blocked" and not (r.get("payload") or {}).get("block_code"):
            no_code += 1
        if r.get("event") == "funnel_deliver":
            p = r.get("payload") or {}
            # Only validate new-format payloads (post tier/risk_reward patch).
            if ts < _FUNNEL_TIER_FIX_CUTOFF:
                continue
            if "gate_code" in p and not p.get("delivery_tier") and not p.get("tier"):
                funnel_no_tier += 1
    if no_code:
        issues.append(f"blocked_events_missing_block_code_24h={no_code}")
    if funnel_no_tier:
        issues.append(f"funnel_deliver_missing_tier_24h={funnel_no_tier}")
    return issues


def _audit_prep_shadow(path: Path, *, since: str) -> list[str]:
    if not path.exists():
        return []
    null_open = open_total = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("event") != "opened":
            continue
        if str(r.get("ts") or "")[:19] < since:
            continue
        open_total += 1
        if (r.get("payload") or {}).get("paper_pnl_pct") is None:
            null_open += 1
    if open_total >= 5 and null_open / open_total > 0.1:
        return [f"prep_shadow_open_null_pnl_24h={null_open}/{open_total}"]
    return []


def _audit_active_geometry() -> list[str]:
    issues: list[str] = []
    st = load_tracker_state(SIGNAL_STATE)
    for key, sig in (st.get("signals") or {}).items():
        if not isinstance(sig, dict) or not _is_signal_active(sig):
            continue
        direction = str(sig.get("direction") or key.split(":")[-1])
        rr = float(sig.get("risk_reward") or 0)
        if rr <= 0:
            issues.append(f"{key}: risk_reward_missing_or_zero")
        mfe = _mfe_pct(sig, direction=direction)
        if mfe <= 0 and sig.get("telegram_sent"):
            issues.append(f"{key}: tg_active_but_mfe_zero")
    return issues


def _print_outcome_wr_summary(path: Path) -> None:
    """#49: deduped phase×direction WR from signal_history for calibration."""
    if not path.exists():
        print("  outcome_wr: signal_history.jsonl missing")
        return
    seen_ids: set[int] = set()
    buckets: dict[tuple[str, str], list[float]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        eid = rec.get("entry_message_id")
        if eid is not None:
            try:
                eid_int = int(eid)
            except (TypeError, ValueError):
                eid_int = None
            if eid_int is not None:
                if eid_int in seen_ids:
                    continue
                seen_ids.add(eid_int)
        phase = str(
            rec.get("entry_lifecycle_phase")
            or rec.get("setup_phase")
            or rec.get("phase")
            or "?"
        )
        direction = str(rec.get("direction") or "?")
        pnl = rec.get("pnl_pct")
        if pnl is None:
            continue
        try:
            buckets.setdefault((phase, direction), []).append(float(pnl))
        except (TypeError, ValueError):
            continue
    if not buckets:
        print("  outcome_wr: no labeled outcomes")
        return
    print("  outcome_wr (deduped by entry_message_id):")
    for (phase, direction), pnls in sorted(buckets.items(), key=lambda x: -len(x[1]))[:12]:
        wins = sum(1 for p in pnls if p > 0)
        print(f"    {phase}/{direction}: n={len(pnls)} wr={wins / len(pnls):.0%} avg_pnl={sum(pnls)/len(pnls):.2f}%")


def main() -> int:
    since = (datetime.now(UTC) - timedelta(hours=24)).isoformat()[:19]
    issues: list[str] = []
    issues.extend(_audit_telemetry(SIGNAL_EVENTS, since=since))
    issues.extend(_audit_active_geometry())
    issues.extend(_audit_prep_shadow(PREP_SHADOW_EVENTS, since=since))

    cut = (datetime.now(UTC) - timedelta(hours=24)).isoformat()[:19]
    recent_blocks = Counter()
    if SIGNAL_EVENTS.exists():
        for line in SIGNAL_EVENTS.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if str(r.get("ts") or "")[:19] < cut:
                continue
            if r.get("event") == "blocked":
                recent_blocks[(r.get("payload") or {}).get("block_code")] += 1

    print("check_audit top_blockers_24h:")
    for code, n in recent_blocks.most_common(8):
        print(f"  {code}: {n}")

    _print_outcome_wr_summary(SIGNAL_HISTORY)

    if issues:
        for item in issues:
            print(f"FAIL {item}", file=sys.stderr)
        return 1
    print("check_audit ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
