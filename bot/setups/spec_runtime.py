"""Catalog-aligned setup detection — orchestration; logic in bot/strategies/."""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

import polars as pl

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from ..domain.strategy_catalog import CATALOG_BY_ID, catalog_default_params
from ..setups.utils import get_dynamic_params
from .base import BaseSetup
from ..strategies._common import SpecHit, build_spec_signal

SetupSignalFn = Callable[
    [PreparedSymbol, BotSettings, dict[str, float], str, str],
    Signal | None,
]

_FRAME_ATTR: dict[str, str] = {
    "15m": "work_15m",
    "1h": "work_1h",
    "4h": "work_4h",
    "1d": "work_1d",
}


def pattern_timeframe(setup_id: str) -> str:
    entry = CATALOG_BY_ID.get(setup_id)
    if entry is None:
        return "15m"
    return entry.pattern_tf


def pattern_dataframe(prepared: PreparedSymbol, setup_id: str) -> tuple[pl.DataFrame, str]:
    tf = pattern_timeframe(setup_id)
    attr = _FRAME_ATTR.get(tf, "work_15m")
    frame = getattr(prepared, attr, prepared.work_15m)
    return frame, tf


def effective_setup_params(
    setup: BaseSetup,
    prepared: PreparedSymbol,
    settings: BotSettings,
    *,
    defaults: dict[str, float] | None = None,
) -> dict[str, float]:
    base = defaults if defaults is not None else setup.get_optimizable_params(settings)
    return {
        **catalog_default_params(setup.setup_id),
        **base,
        **get_dynamic_params(prepared, setup.setup_id),
    }


SpecDetectFn = Callable[..., SpecHit | None]
ExtendedDetectFn = Callable[
    [PreparedSymbol, BotSettings, dict[str, float], dict[str, float], str, str],
    Signal | None,
]


def try_spec_signal(
    *,
    prepared: PreparedSymbol,
    settings: BotSettings,
    setup_id: str,
    family: str,
    defaults: dict[str, float],
    effective: dict[str, float],
    spec_detect: SpecDetectFn,
    spec_kwargs: dict[str, object] | None = None,
) -> Signal | None:
    work, timeframe = pattern_dataframe(prepared, setup_id)
    hit = spec_detect(work, timeframe=timeframe, **(spec_kwargs or {}))
    if hit is None:
        return None
    return build_spec_signal(
        prepared=prepared,
        settings=settings,
        setup_id=setup_id,
        family=family,
        hit=hit,
        defaults=defaults,
        params=effective,
    )


def run_setup_detection(
    *,
    prepared: PreparedSymbol,
    settings: BotSettings,
    setup_id: str,
    family: str,
    defaults: dict[str, float],
    effective: dict[str, float],
    spec_detect: SpecDetectFn,
    extended_detect: ExtendedDetectFn | None = None,
    spec_kwargs: dict[str, object] | None = None,
) -> Signal | None:
    signal = try_spec_signal(
        prepared=prepared,
        settings=settings,
        setup_id=setup_id,
        family=family,
        defaults=defaults,
        effective=effective,
        spec_detect=spec_detect,
        spec_kwargs=spec_kwargs,
    )
    if signal is not None:
        return signal
    if extended_detect is None:
        return None
    return extended_detect(prepared, settings, defaults, effective, setup_id, family)


class SpecDetectorSetup(BaseSetup):
    """Thin strategy shell — detection in ``bot/strategies/*`` module."""

    DEFAULTS: ClassVar[dict[str, float]] = {}
    detect_setup: ClassVar[SetupSignalFn | None] = None

    def get_optimizable_params(self, settings: BotSettings | None = None) -> dict[str, float]:
        from ..strategies._roadmap import _configured_params

        return _configured_params(settings, self.setup_id, dict(self.DEFAULTS))

    def effective_params(
        self, prepared: PreparedSymbol, settings: BotSettings
    ) -> tuple[dict[str, float], dict[str, float]]:
        defaults = self.get_optimizable_params(settings)
        effective = effective_setup_params(self, prepared, settings, defaults=defaults)
        return defaults, effective

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        detector = type(self).detect_setup
        if detector is None:
            raise NotImplementedError(f"{self.setup_id}: detect_setup not wired")
        defaults, effective = self.effective_params(prepared, settings)
        return detector(prepared, settings, defaults, effective, self.setup_id, self.family)


__all__ = [
    "SpecDetectorSetup",
    "SpecDetectFn",
    "ExtendedDetectFn",
    "SetupSignalFn",
    "effective_setup_params",
    "pattern_dataframe",
    "pattern_timeframe",
    "run_setup_detection",
    "try_spec_signal",
]
