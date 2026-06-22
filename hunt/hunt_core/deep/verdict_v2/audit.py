"""Pattern audit JSONL — pre/post whitelist logging."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from hunt_core.deep.verdict_v2.types import MarketDriver, PatternCandidate, PatternConfidence
from hunt_core.paths import VERDICT_V2_PATTERN_AUDIT_JSONL


def _cand_dict(c: PatternCandidate) -> dict[str, Any]:
    return {
        "id": c.id,
        "score": c.raw_score,
        "direction": c.direction_hint,
        "evidence": c.evidence[:4],
    }


def append_pattern_audit(
    row: dict[str, Any],
    *,
    raw: list[PatternCandidate],
    filtered: list[PatternCandidate],
    resolved: PatternConfidence,
    driver: MarketDriver,
) -> None:
    try:
        from hunt_core.data.jsonl_io import append_jsonl_lines

        VERDICT_V2_PATTERN_AUDIT_JSONL.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "symbol": str(row.get("symbol") or "").upper(),
            "driver": driver.primary,
            "raw": [_cand_dict(c) for c in raw],
            "filtered": [_cand_dict(c) for c in filtered],
            "primary": resolved.primary.id,
            "alternatives": [a.id for a in resolved.alternatives],
            "spread": resolved.spread,
            "ambiguous": resolved.ambiguous,
        }
        append_jsonl_lines(VERDICT_V2_PATTERN_AUDIT_JSONL, [json.dumps(record, separators=(",", ":"))])
    except Exception:
        pass
