"""Feature vector extraction from prepared snapshots for the parquet feature lake."""
from __future__ import annotations



import json
import math
from dataclasses import asdict, dataclass, fields
from functools import lru_cache
from pathlib import Path
from typing import Any

_REGISTRY_PATH = Path(__file__).resolve().parents[1] / "domain" / "feature_registry.json"

_FRAME_SOURCES = frozenset(
    {
        "close",
        "rsi14",
        "atr14",
        "atr_pct",
        "adx14",
        "ema20",
        "ema50",
        "ema200",
        "volume_ratio20",
        "macd_hist",
        "bb_pct_b",
        "bb_width",
        "supertrend_dir",
    }
)


class FeatureExtractError(ValueError):
    """Raised when a required feature cannot be resolved from prepare outputs."""


@dataclass(slots=True)
class FeatureVector:
    """Scalar feature snapshot for one symbol × timeframe × tick."""

    symbol: str
    ts: str
    tf: str
    price: float
    close: float
    rsi14: float
    atr14: float
    adx14: float
    atr_pct: float | None = None
    ema20: float | None = None
    ema50: float | None = None
    ema200: float | None = None
    volume_ratio20: float | None = None
    macd_hist: float | None = None
    bb_pct_b: float | None = None
    bb_width: float | None = None
    supertrend_dir: float | None = None
    chg_24h_pct: float | None = None
    range_24h_pct: float | None = None
    oi: float | None = None
    oi_change_pct: float | None = None
    oi_slope_5m: float | None = None
    funding_rate: float | None = None
    ls_ratio: float | None = None
    global_ls_ratio: float | None = None
    depth_imbalance: float | None = None
    microprice_bias: float | None = None
    basis_pct: float | None = None
    premium_zscore_5m: float | None = None
    liquidation_score: float | None = None
    lifecycle_phase: str | None = None
    lifecycle_bias: str | None = None
    market_regime: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@lru_cache(maxsize=1)
def load_feature_registry() -> dict[str, Any]:
    raw = _REGISTRY_PATH.read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or "features" not in payload:
        msg = f"invalid feature registry: {_REGISTRY_PATH}"
        raise FeatureExtractError(msg)
    return payload


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(parsed) or math.isinf(parsed):
        return None
    return parsed


def _require_float(value: Any, *, field: str, symbol: str, tf: str) -> float:
    parsed = _coerce_float(value)
    if parsed is None:
        msg = f"required feature {field!r} missing for {symbol} tf={tf}"
        raise FeatureExtractError(msg)
    return parsed


def _frame_block(prepared: Any, row: dict[str, Any], tf: str) -> dict[str, Any]:
    if prepared is not None:
        attr = f"work_{tf}"
        work = getattr(prepared, attr, None)
        if work is not None and not getattr(work, "is_empty", lambda: True)():
            cols = getattr(work, "columns", [])
            out: dict[str, Any] = {}
            for name in _FRAME_SOURCES:
                if name in cols:
                    try:
                        out[name] = work.item(-1, name)
                    except Exception:
                        continue
            if out:
                return out
    snap = ((row.get("timeframes") or {}).get(tf) or {})
    return snap if isinstance(snap, dict) else {}


def _prepared_value(prepared: Any, row: dict[str, Any], attr: str) -> Any:
    if prepared is not None:
        val = getattr(prepared, attr, None)
        if val is not None:
            return val
    market = row.get("market") or row.get("positioning") or {}
    if not isinstance(market, dict):
        return None
    aliases = {
        "oi_current": ("oi",),
        "oi_change_pct": ("oi_chg_1h", "oi_change_pct"),
        "funding_rate": ("funding", "funding_rate"),
        "ls_ratio": ("ls_1h", "ls_ratio"),
        "global_ls_ratio": ("global_ls", "global_ls_ratio"),
        "depth_imbalance": ("depth", "depth_imbalance"),
        "microprice_bias": ("microprice", "microprice_bias"),
        "basis_pct": ("basis", "basis_pct"),
        "premium_zscore_5m": ("premium_zscore_5m",),
        "liquidation_score": ("liquidation_score_5m", "liquidation_score"),
    }
    for key in aliases.get(attr, (attr,)):
        if key in market and market[key] is not None:
            return market[key]
    return None


def build_feature_vector(
    prepared: Any,
    row: dict[str, Any],
    *,
    symbol: str,
    tf: str,
) -> FeatureVector:
    """Extract registry-backed features from prepare outputs; fail loud on required gaps."""
    sym = (symbol or str(row.get("symbol") or "")).upper()
    if not sym:
        raise FeatureExtractError("symbol is required for feature vector extraction")

    ts = row.get("ts")
    if not ts:
        raise FeatureExtractError(f"ts missing for feature vector extraction: {sym}")

    frame = _frame_block(prepared, row, tf)
    if frame.get("status") == "empty":
        raise FeatureExtractError(f"timeframe frame empty for {sym} tf={tf}")

    lifecycle = row.get("lifecycle") or {}
    regime = row.get("regime") or {}

    vector_kwargs: dict[str, Any] = {
        "symbol": sym,
        "ts": str(ts),
        "tf": tf,
        "price": _require_float(row.get("price"), field="price", symbol=sym, tf=tf),
        "close": _require_float(frame.get("close"), field="close", symbol=sym, tf=tf),
        "rsi14": _require_float(frame.get("rsi14"), field="rsi14", symbol=sym, tf=tf),
        "atr14": _require_float(frame.get("atr14"), field="atr14", symbol=sym, tf=tf),
        "adx14": _require_float(frame.get("adx14"), field="adx14", symbol=sym, tf=tf),
    }

    for name in _FRAME_SOURCES:
        if name in {"close", "rsi14", "atr14", "adx14"}:
            continue
        vector_kwargs[name] = _coerce_float(frame.get(name))

    vector_kwargs["chg_24h_pct"] = _coerce_float(row.get("chg_24h_pct"))
    vector_kwargs["range_24h_pct"] = _coerce_float(row.get("range_24h_pct"))
    vector_kwargs["oi"] = _coerce_float(_prepared_value(prepared, row, "oi_current"))
    vector_kwargs["oi_change_pct"] = _coerce_float(
        _prepared_value(prepared, row, "oi_change_pct")
    )
    vector_kwargs["oi_slope_5m"] = _coerce_float(_prepared_value(prepared, row, "oi_slope_5m"))
    vector_kwargs["funding_rate"] = _coerce_float(_prepared_value(prepared, row, "funding_rate"))
    vector_kwargs["ls_ratio"] = _coerce_float(_prepared_value(prepared, row, "ls_ratio"))
    vector_kwargs["global_ls_ratio"] = _coerce_float(
        _prepared_value(prepared, row, "global_ls_ratio")
    )
    vector_kwargs["depth_imbalance"] = _coerce_float(
        _prepared_value(prepared, row, "depth_imbalance")
    )
    vector_kwargs["microprice_bias"] = _coerce_float(
        _prepared_value(prepared, row, "microprice_bias")
    )
    vector_kwargs["basis_pct"] = _coerce_float(_prepared_value(prepared, row, "basis_pct"))
    vector_kwargs["premium_zscore_5m"] = _coerce_float(
        _prepared_value(prepared, row, "premium_zscore_5m")
    )
    vector_kwargs["liquidation_score"] = _coerce_float(
        _prepared_value(prepared, row, "liquidation_score")
    )
    vector_kwargs["lifecycle_phase"] = (
        str(lifecycle.get("phase")) if lifecycle.get("phase") is not None else None
    )
    vector_kwargs["lifecycle_bias"] = (
        str(lifecycle.get("recommended_bias"))
        if lifecycle.get("recommended_bias") is not None
        else None
    )
    vector_kwargs["market_regime"] = (
        str(regime.get("market_regime")) if regime.get("market_regime") is not None else None
    )

    registry = load_feature_registry().get("features") or {}
    missing_required: list[str] = []
    for field_name, meta in registry.items():
        if not isinstance(meta, dict) or not meta.get("required"):
            continue
        if field_name not in vector_kwargs or vector_kwargs[field_name] is None:
            missing_required.append(field_name)
    if missing_required:
        raise FeatureExtractError(
            f"required registry features missing for {sym} tf={tf}: {sorted(missing_required)}"
        )

    allowed = {f.name for f in fields(FeatureVector)}
    filtered = {k: v for k, v in vector_kwargs.items() if k in allowed}
    return FeatureVector(**filtered)


__all__ = [
    "FeatureExtractError",
    "FeatureVector",
    "build_feature_vector",
    "load_feature_registry",
]
