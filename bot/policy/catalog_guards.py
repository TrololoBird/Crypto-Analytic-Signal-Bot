"""HTF / volume guards aligned with STRATEGY_CATALOG (spec-driven, config-tunable)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..setups import _reject
from .mtf import evaluate_mtf_gate, normalize_mtf_reject_reason

if TYPE_CHECKING:
    from engine.domain.schemas import PreparedSymbol


def catalog_allows_signal(
    prepared: PreparedSymbol,
    *,
    setup_id: str,
    direction: str,
    family: str,
    confirmation_profile: str,
    params: dict[str, float],
) -> bool:
    """Apply catalog gates before emitting a spec-built signal."""
    min_volume = float(params.get("min_volume_ratio", 0.0) or 0.0)
    if min_volume > 0.0:
        primary = getattr(prepared, "work_primary", None)
        frame = primary if primary is not None and not primary.is_empty() else prepared.work_15m
        vol = prepared.volume_ratio
        if frame is not None and not frame.is_empty() and "volume_ratio20" in frame.columns:
            raw = frame.item(-1, "volume_ratio20")
            vol = None if raw is None else float(raw)
        if vol is None or float(vol) < min_volume:
            _reject(
                prepared,
                setup_id,
                "catalog_min_volume",
                volume_ratio=vol,
                min_volume_ratio=min_volume,
            )
            return False

    min_adx = float(params.get("min_adx_1h", 0.0) or 0.0)
    if min_adx > 0.0:
        adx = prepared.adx_1h
        if adx is not None and float(adx) < min_adx:
            _reject(prepared, setup_id, "catalog_min_adx_1h", adx_1h=adx, min_adx_1h=min_adx)
            return False

    if confirmation_profile == "trend_follow" or family in {"continuation", "trend_follow"}:
        settings = getattr(prepared, "settings", None)
        strict_dq = bool(getattr(getattr(settings, "runtime", None), "strict_data_quality", True))
        mtf_ok, mtf_reason, mtf_details = evaluate_mtf_gate(
            prepared,
            direction,
            confirmation_profile=confirmation_profile,
            strict_data_quality=strict_dq,
        )
        if not mtf_ok:
            reason = normalize_mtf_reject_reason(mtf_reason)
            _reject(
                prepared,
                setup_id,
                reason,
                direction=direction,
                mtf_reason=mtf_reason,
                **mtf_details,
            )
            return False

    return True
