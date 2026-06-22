"""JSONL tick row prepare / hydrate — fusion lifecycle + MTF must survive replay."""
from __future__ import annotations

import json
from typing import Any

_JSONL_DROP_KEYS = frozenset({"_prepared"})


def ensure_fusion_lifecycle_fields(
    lc: dict[str, Any] | None,
    *,
    setup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Backfill ``phase_fusion`` / entry flags — JSONL rows must never carry null gates."""
    from hunt_core.scanner.detect.phase import NEUTRAL
    from hunt_core.scanner.gate._lifecycle import fusion_lifecycle_flags

    base = dict(lc) if isinstance(lc, dict) else {}
    setup_d = setup if isinstance(setup, dict) else {}
    phase = str(
        base.get("phase_fusion")
        or base.get("phase")
        or setup_d.get("phase")
        or setup_d.get("lifecycle_phase")
        or NEUTRAL
    )
    base["phase"] = phase
    base["phase_fusion"] = phase
    side = str(
        base.get("bias")
        or base.get("recommended_bias")
        or setup_d.get("direction")
        or ""
    )
    gate_open = bool(setup_d.get("confirmed")) if setup_d else bool(base.get("gate_open"))
    watch_ok = bool(base.get("watch_ok")) or phase in {"pre_pump", "pre_dump"}
    flags = fusion_lifecycle_flags(
        side=side,
        phase=phase,
        gate_open=gate_open,
        watch_ok=watch_ok,
    )
    for key, val in flags.items():
        if base.get(key) is None:
            base[key] = val
    if base.get("watch_ok") is None:
        base["watch_ok"] = watch_ok
    if base.get("cusum") is None:
        base["cusum"] = float(base.get("band") or 0.0)
    if base.get("cusum_band") is None:
        base["cusum_band"] = float(base.get("band") or 0.0)
    return base


def mtf_to_json_dict(mtf: Any | None) -> dict[str, Any] | None:
    """Serialize MTF confluence for JSONL (includes HTF counts for replay gates)."""
    if mtf is None:
        return None
    if isinstance(mtf, dict):
        return dict(mtf)
    if isinstance(mtf, str):
        return None
    to_dict = getattr(mtf, "to_dict", None)
    if callable(to_dict):
        out = to_dict()
        return out if isinstance(out, dict) else None
    return None


def resolve_row_mtf(row: dict[str, Any], *, symbol: str = "") -> Any | None:
    """Return MTF as dict or live object; recover from corrupted JSONL string mtf."""
    from hunt_core.confluence.mtf import MTFConfluence, build_mtf_confluence

    mtf = row.get("mtf")
    if isinstance(mtf, MTFConfluence):
        return mtf
    if isinstance(mtf, dict):
        return mtf
    summary = row.get("mtf_summary")
    if isinstance(summary, dict):
        return summary
    if isinstance(mtf, str):
        row.pop("mtf", None)
    sym = str(symbol or row.get("symbol") or "").upper()
    tf = row.get("timeframes") if isinstance(row.get("timeframes"), dict) else {}
    price = float(row.get("price") or row.get("last_price") or 0)
    if sym and tf and price > 0:
        return build_mtf_confluence(
            sym,
            tf,
            price,
            market=row.get("market") if isinstance(row.get("market"), dict) else None,
            row=row,
        )
    return None


def prepare_tick_row_for_jsonl(row: dict[str, Any]) -> dict[str, Any]:
    """Strip non-JSON fields and normalize lifecycle / MTF before append."""
    out: dict[str, Any] = {}
    for key, val in row.items():
        if key in _JSONL_DROP_KEYS:
            continue
        out[key] = val

    lc = ensure_fusion_lifecycle_fields(
        out.get("lifecycle") if isinstance(out.get("lifecycle"), dict) else None,
    )
    out["lifecycle"] = lc

    for setup_key in ("dump", "long"):
        setup = out.get(setup_key)
        if not isinstance(setup, dict):
            continue
        nested_lc = setup.get("lifecycle") if isinstance(setup.get("lifecycle"), dict) else None
        setup["lifecycle"] = ensure_fusion_lifecycle_fields(nested_lc or lc, setup=setup)

    mtf_json = mtf_to_json_dict(out.get("mtf"))
    if mtf_json is not None:
        out["mtf"] = mtf_json
    elif isinstance(out.get("mtf"), str):
        out.pop("mtf", None)
    if out.get("mtf_summary") is None and isinstance(out.get("mtf"), dict):
        out["mtf_summary"] = {
            "dominant": out["mtf"].get("dominant"),
            "long_htf_count": out["mtf"].get("long_htf_count"),
            "short_htf_count": out["mtf"].get("short_htf_count"),
        }

    out.setdefault("plane", "hunt")

    from hunt_core.deep.verdict_v2.serialize import strip_verdict_v2_for_jsonl

    return strip_verdict_v2_for_jsonl(out)


def hydrate_tick_row_from_jsonl(row: dict[str, Any]) -> dict[str, Any]:
    """Restore delivery-ready row from stored JSONL (lifecycle + MTF dict)."""
    out = dict(row)
    long_setup = out.get("long") if isinstance(out.get("long"), dict) else {}
    short_setup = out.get("dump") if isinstance(out.get("dump"), dict) else {}
    active_setup = long_setup if long_setup.get("confirmed") else short_setup
    out["lifecycle"] = ensure_fusion_lifecycle_fields(
        out.get("lifecycle") if isinstance(out.get("lifecycle"), dict) else None,
        setup=active_setup if active_setup else None,
    )
    for setup_key in ("dump", "long"):
        setup = out.get(setup_key)
        if isinstance(setup, dict):
            setup["lifecycle"] = ensure_fusion_lifecycle_fields(
                setup.get("lifecycle") if isinstance(setup.get("lifecycle"), dict) else out["lifecycle"],
                setup=setup,
            )
    mtf = resolve_row_mtf(out, symbol=str(out.get("symbol") or ""))
    if mtf is not None:
        mtf_json = mtf_to_json_dict(mtf)
        out["mtf"] = mtf_json if mtf_json is not None else mtf
    return out


def serialize_tick_row(row: dict[str, Any]) -> str:
    """JSONL line — normalized lifecycle/MTF, no ``default=str`` on dataclasses."""
    return json.dumps(prepare_tick_row_for_jsonl(row), default=str)


__all__ = [
    "ensure_fusion_lifecycle_fields",
    "hydrate_tick_row_from_jsonl",
    "mtf_to_json_dict",
    "prepare_tick_row_for_jsonl",
    "resolve_row_mtf",
    "serialize_tick_row",
]
