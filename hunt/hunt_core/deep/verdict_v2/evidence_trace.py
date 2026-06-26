"""Evidence trace — decompose direction + strength + zone origin per verdict.

This is the audit foundation for model validation. Top-level scores
(``strength=0.31``) are meaningless on their own; this trace records every
factor that fed the direction choice and every term that built the strength
score, so any signal can be decomposed offline and correlated with outcomes.

Additive, fail-silent, off the hot path's critical correctness — never raise.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from typing import Any

from hunt_core.paths import EVIDENCE_TRACE_JSONL


def evidence_trace_enabled() -> bool:
    return os.getenv("HUNT_EVIDENCE_TRACE", "1").strip().lower() in {"1", "true", "yes", "on"}


def _direction_contributors(verdict: Any) -> dict[str, Any]:
    """Per-engine net side + who actually drove the directional choice."""
    engines = getattr(verdict, "engine_outputs", None) or {}
    contributors: list[dict[str, Any]] = []
    net_long = 0.0
    net_short = 0.0
    for name, eng in sorted(engines.items()):
        long = float(getattr(eng, "long", 0.0) or 0.0)
        short = float(getattr(eng, "short", 0.0) or 0.0)
        weight = float(getattr(eng, "blend_weight", 0.0) or 0.0)
        net_long += long * weight
        net_short += short * weight
        contributors.append(
            {
                "engine": name,
                "long": round(long, 3),
                "short": round(short, 3),
                "net": round((long - short) * weight, 4),
                "weight": round(weight, 3),
                "conviction": round(float(getattr(eng, "conviction", 0.0) or 0.0), 3),
                "info_value": round(float(getattr(eng, "information_value", 0.0) or 0.0), 3),
                "evidence": list(getattr(eng, "evidence", []) or [])[:2],
            }
        )
    # Rank engines by absolute net pull so the dominant driver is obvious.
    contributors.sort(key=lambda c: abs(c["net"]), reverse=True)
    path = getattr(verdict, "expected_path", None)
    return {
        "chosen_direction": getattr(path, "direction", None),
        "path_type": getattr(path, "type", None),
        "probability_rank": round(float(getattr(path, "probability_rank", 0.0) or 0.0), 3),
        "net_long_weighted": round(net_long, 4),
        "net_short_weighted": round(net_short, 4),
        "net_bias": round(net_long - net_short, 4),
        "top_drivers": contributors[:4],
    }


def _strength_terms(verdict: Any) -> dict[str, Any]:
    """Extract strength decomposition directly from breakdown dict.

    Uses strength.breakdown (authoritative) rather than reconstructing the
    formula with hardcoded weights — avoids drift when formula changes.
    """
    path = getattr(verdict, "expected_path", None)
    topo = getattr(verdict, "horizon_topology", None)
    data = getattr(verdict, "data_quality", None)
    strength = getattr(verdict, "signal_strength", None)

    rank = float(getattr(path, "probability_rank", 0.0) or 0.0)
    topo_kind = str(getattr(topo, "kind", "") or "")
    coverage = float(getattr(data, "coverage_score", 0.0) or 0.0)
    sources = dict(getattr(data, "sources", None) or {})
    present = sum(sources.values()) if sources else 0
    total = len(sources) if sources else 0
    data_completeness = round(present / total, 3) if total > 0 else coverage

    return {
        "probability_rank": round(rank, 3),
        "topology_kind": topo_kind,
        "coverage_score": round(coverage, 3),
        "data_completeness": data_completeness,
        "data_sources": sources,
        "coverage_capped": coverage < 0.55,
        "reconcile_level": str(getattr(verdict, "reconcile_level", "coherent") or "coherent"),
        "final_score": round(float(getattr(strength, "score", 0.0) or 0.0), 3),
        "final_label": str(getattr(strength, "label", "") or ""),
        "scenario_confidence": round(float(getattr(strength, "scenario_confidence", 0.0) or 0.0), 3),
        "geometry_confidence": round(float(getattr(strength, "geometry_confidence", 0.0) or 0.0), 3),
        "capped_by_data": bool(getattr(strength, "capped_by_data", False)),
        "breakdown": dict(getattr(strength, "breakdown", None) or {}),
    }


def _zone_origin(verdict: Any, price: float) -> dict[str, Any]:
    """Where the entry/target geometry came from + how far the targets sit."""
    plan = getattr(verdict, "trade_plan", None)
    if plan is None:
        return {}
    zone = getattr(plan, "entry_zone", None) or (0.0, 0.0)

    def _pct(level: float) -> float | None:
        if price <= 0 or not level:
            return None
        return round((float(level) - price) / price * 100.0, 2)

    return {
        "entry_lo": round(float(zone[0]), 6),
        "entry_hi": round(float(zone[1]), 6),
        "entry_reference": round(float(getattr(plan, "entry_reference", 0.0) or 0.0), 6),
        "level_sources": list(getattr(plan, "level_sources", []) or []),
        # Absolute levels so the outcome resolver needs no reconstruction.
        "tp1": round(float(getattr(plan, "take_profit_1", 0.0) or 0.0), 6),
        "tp2": round(float(getattr(plan, "take_profit_2", 0.0) or 0.0), 6),
        "tp3": round(float(getattr(plan, "take_profit_3", 0.0) or 0.0), 6),
        "stop_loss": round(float(getattr(plan, "stop_loss", 0.0) or 0.0), 6),
        "tp1_pct": _pct(getattr(plan, "take_profit_1", 0.0)),
        "tp2_pct": _pct(getattr(plan, "take_profit_2", 0.0)),
        "tp3_pct": _pct(getattr(plan, "take_profit_3", 0.0)),
        "stop_pct": _pct(getattr(plan, "stop_loss", 0.0)),
    }


def append_evidence_trace(row: dict[str, Any], *, verdict: Any | None) -> None:
    if not evidence_trace_enabled() or verdict is None:
        return
    try:
        from hunt_core.data.jsonl_io import append_jsonl_lines

        price = float(row.get("price") or 0)
        dec = getattr(verdict, "signal_decision", None)
        path = getattr(verdict, "expected_path", None)
        ts = datetime.now(UTC).isoformat()
        symbol = str(row.get("symbol") or "").upper()
        action = getattr(dec, "action", None)
        playbook = str(getattr(path, "type", "") or "")
        # Stable per (symbol, minute-bucket, action) — lets the outcome resolver
        # dedup repeated emissions of the same setup across ticks.
        sig_basis = f"{symbol}|{ts[:16]}|{action}|{playbook}"
        signal_id = hashlib.sha1(sig_basis.encode()).hexdigest()[:12]
        record = {
            "ts": ts,
            "signal_id": signal_id,
            "module": "deep",
            "symbol": symbol,
            "price": price,
            "action": action,
            "playbook": playbook,
            "gates_failed": list(getattr(dec, "gates_failed", []) or []),
            "wait_category": str(getattr(dec, "wait_category", "") or ""),
            "direction": _direction_contributors(verdict),
            "strength": _strength_terms(verdict),
            "zone_origin": _zone_origin(verdict, price),
        }
        EVIDENCE_TRACE_JSONL.parent.mkdir(parents=True, exist_ok=True)
        append_jsonl_lines(
            EVIDENCE_TRACE_JSONL,
            [json.dumps(record, separators=(",", ":"), default=str)],
        )
    except Exception:
        pass


__all__ = ["append_evidence_trace", "evidence_trace_enabled"]
