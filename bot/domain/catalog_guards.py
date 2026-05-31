"""HTF / volume guards aligned with STRATEGY_CATALOG (spec-driven, config-tunable)."""

from __future__ import annotations

from .schemas import PreparedSymbol


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
    from ..setups import _reject

    min_volume = float(params.get("min_volume_ratio", 0.0) or 0.0)
    if min_volume > 0.0:
        vol = prepared.volume_ratio
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
        bias = str(prepared.bias_4h or prepared.bias_1h or "neutral")
        if direction == "long" and bias == "downtrend":
            _reject(prepared, setup_id, "catalog_htf_bias_conflict", bias=bias, direction=direction)
            return False
        if direction == "short" and bias == "uptrend":
            _reject(prepared, setup_id, "catalog_htf_bias_conflict", bias=bias, direction=direction)
            return False

    return True
