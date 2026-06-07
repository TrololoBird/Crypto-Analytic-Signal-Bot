from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ..domain.strategy_catalog import catalog_default_params
from ._common import LOGGER, SpecHit, _latest_values, build_spec_signal, with_spec_columns
from ._roadmap import (
    _build_atr_signal,
    _missing_columns,
    _prev,
    _reject,
    _series_max_tail,
)
from .roadmap_base import RoadmapSetup

if TYPE_CHECKING:
    import polars as pl

    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

__all__ = ["detect_bb_squeeze_release"]


def detect_bb_squeeze_release(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 30:
        return None
    if "spec_squeeze" not in work.columns:
        LOGGER.warning("detect_bb_squeeze_release: spec_squeeze column missing")
        return None
    assert "spec_squeeze" in work.columns
    row = _latest_values(work)
    atr = row.get("spec_atr14", 0.0)
    if atr <= 0.0:
        return None
    try:
        was_squeeze = bool(work.item(-2, "spec_squeeze"))
        is_squeeze = bool(work.item(-1, "spec_squeeze"))
    except (IndexError, ValueError, TypeError):
        return None
    if not was_squeeze or is_squeeze:
        return None
    direction = "long" if row["close"] > row.get("spec_ema20", row["close"]) else "short"
    ema20 = row.get("spec_ema20", row["close"])
    return SpecHit(
        strategy="bb_squeeze",
        direction=direction,
        entry=ema20,
        stop_basis=row["low"] if direction == "long" else row["high"],
        atr=atr,
        timeframe=timeframe,
        reasons=("bb_kc_squeeze_released",),
        vol_ratio=row.get("volume_ratio20", 1.0),
        rsi=row.get("rsi14", 50.0),
    )


def detect_bb_squeeze_prepared(
    prepared: PreparedSymbol,
    settings: BotSettings,
    effective_params: dict[str, float],
    *,
    setup_id: str,
    family: str,
) -> Signal | None:
    params = effective_params
    hit = detect_bb_squeeze_release(prepared.work_15m, timeframe="15m")
    if hit is not None:
        return build_spec_signal(
            prepared=prepared,
            _settings=settings,
            setup_id=setup_id,
            family=family,
            hit=hit,
            defaults=catalog_default_params(setup_id),
            params=params,
        )

    # FIX 2026-05-21: strict spec release is last-bar only; fall through to
    # the configured squeeze memory/release window before rejecting.
    work = prepared.work_15m
    missing = _missing_columns(work, ("bb_width", "squeeze_on", "squeeze_off", "roc10"))
    if missing:
        _reject(prepared, setup_id, "missing_columns", missing_fields=missing)
        return None
    if work.height < 2:
        _reject(prepared, setup_id, "insufficient_bars")
        return None
    bb_width = _prev(work, "bb_width")
    release_lookback = int(params["squeeze_release_lookback"])
    memory_bars = int(params.get("squeeze_memory_bars", 20))
    prior = work.head(max(0, work.height - 1))
    squeeze_recent = _series_max_tail(prior, "squeeze_on", memory_bars)
    squeeze_release_recent = _series_max_tail(work, "squeeze_off", release_lookback)
    roc10 = _prev(work, "roc10")
    vol_ratio = _prev(work, "volume_ratio20", 1.0)
    if squeeze_recent <= 0.0 and bb_width > float(params["max_bb_width"]):
        _reject(prepared, setup_id, "bb_squeeze_not_active", bb_width=bb_width)
        return None
    volume_penalty = vol_ratio < float(params["min_volume_ratio"])
    if squeeze_release_recent <= 0.0 and bb_width <= float(params["max_bb_width"]):
        squeeze_release_recent = 1.0
    if squeeze_release_recent <= 0.0:
        _reject(
            prepared,
            setup_id,
            "squeeze_breakout_unconfirmed",
            squeeze_release_recent=squeeze_release_recent,
            volume_ratio=vol_ratio,
            release_lookback=release_lookback,
        )
        return None
    if abs(roc10) < float(params["min_roc10_abs_pct"]):
        _reject(prepared, setup_id, "momentum_too_low", roc10=roc10)
        return None
    direction = "long" if roc10 > 0.0 else "short"
    obv_aligned = True
    if "obv_above_ema" in work.columns:
        obv_val = float(work.item(-1, "obv_above_ema") or 0.0)
        obv_aligned = (direction == "long" and obv_val > 0.0) or (
            direction == "short" and obv_val <= 0.0
        )
    clarity = min(abs(roc10), 1.0) * (0.90 if volume_penalty else 1.0)
    if not obv_aligned:
        clarity *= 0.85
    reasons = [
        f"bb_squeeze_{direction}",
        f"bb_width={bb_width:.2f}",
        f"release_recent={squeeze_release_recent:.0f}",
    ]
    if not obv_aligned:
        reasons.append("obv_opposes_breakout")
    entry_anchor = _prev(work, "ema20", 0.0) or None
    return _build_atr_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=direction,
        params=params,
        reasons=reasons,
        family=family,
        structure_clarity=clarity,
        confirmed_bar=True,
        entry_anchor=entry_anchor,
    )


class BBSqueezeSetup(RoadmapSetup):
    setup_id = "bb_squeeze"
    family = "volatility"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)
    DEFAULTS: ClassVar[dict[str, float]] = {
        **RoadmapSetup.DEFAULTS,
        "max_bb_width": 5.0,
        "min_volume_ratio": 0.90,
        "min_roc10_abs_pct": 0.10,
        "squeeze_release_lookback": 8.0,
        "squeeze_memory_bars": 20.0,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings):
        return detect_bb_squeeze_prepared(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["BBSqueezeSetup"]
