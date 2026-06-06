from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ._roadmap import (
    _build_atr_signal,
    _confirmed_context_conflict,
    _finite_or_none,
    _has_l2_depth,
    _last,
    _orderbook_source,
    _prev,
    _price_change_pct_confirmed,
    _reject,
)
from .roadmap_base import RoadmapSetup

if TYPE_CHECKING:
    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

__all__ = ["detect_depth_imbalance"]


def detect_depth_imbalance(
    prepared: PreparedSymbol,
    _settings: BotSettings,
    effective_params: dict[str, float],
    *,
    setup_id: str,
    family: str,
) -> Signal | None:
    params = effective_params
    depth = _finite_or_none(prepared.depth_imbalance)
    if depth is None:
        _reject(prepared, setup_id, "pattern.depth_not_actionable")
        return None
    micro = _finite_or_none(prepared.microprice_bias)
    if micro is None:
        _reject(prepared, setup_id, "microprice_bias_missing")
        return None
    depth_source = _orderbook_source(prepared)
    source_penalty = 1.0
    if not _has_l2_depth(prepared):
        if depth_source not in {"l2_depth", "l1_book", "rest_book_l1"}:
            _reject(
                prepared,
                setup_id,
                "pattern.depth_not_actionable",
                depth_source=depth_source,
                depth_imbalance=depth,
                microprice_bias=micro,
            )
            return None
        # FIX 2026-05-21: REST/L1 public book context is weaker than fresh
        # partial-depth, but it is an explicit source and should be scored
        # as a proxy instead of hard-rejected as missing data.
        source_penalty = 0.72
    work = prepared.work_15m
    # fix-sl-A: confirmed bar (df[-2]) for orderbook momentum alignment.
    close_position = _prev(work, "close_position", 0.5)
    threshold = float(params["min_depth_imbalance"])
    micro_threshold = float(params["min_microprice_bias"])
    vol_ratio = _prev(work, "volume_ratio20", 1.0)
    volume_penalty = vol_ratio < float(params["min_volume_ratio"])
    roc10 = _prev(work, "roc10", _price_change_pct_confirmed(work, 10))
    if abs(roc10) < float(params["min_roc10_abs_pct"]):
        _reject(prepared, setup_id, "pattern.depth_not_actionable", roc10=roc10)
        return None
    long_votes = sum(
        (
            depth >= threshold,
            micro >= micro_threshold,
            close_position >= float(params["min_close_position_long"]),
            roc10 >= 0.0,
        )
    )
    short_votes = sum(
        (
            depth <= -threshold,
            micro <= -micro_threshold,
            close_position <= float(params["max_close_position_short"]),
            roc10 <= 0.0,
        )
    )
    if long_votes >= 2 and long_votes > short_votes:
        direction = "long"
    elif short_votes >= 2 and short_votes > long_votes:
        direction = "short"
    else:
        _reject(
            prepared,
            setup_id,
            "depth_not_actionable",
            depth_imbalance=depth,
            microprice_bias=micro,
            close_position=close_position,
        )
        return None
    context_penalty = _confirmed_context_conflict(prepared, direction)
    clarity = min(abs(depth), 1.0)
    clarity *= source_penalty
    if volume_penalty:
        clarity *= 0.90
    if context_penalty:
        clarity *= 0.82
    source_note = "depth_source=proxy" if source_penalty < 1.0 else "depth_source=l2_depth"
    entry_anchor = _last(work, "ema20", 0.0) or None
    return _build_atr_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=direction,
        params=params,
        confirmed_bar=True,
        entry_anchor=entry_anchor,
        reasons=[
            f"depth_imbalance_{direction}",
            f"depth={depth:.3f}",
            f"depth_source={depth_source}",
            source_note,
            f"micro={micro:.3f}",
            f"volume_ratio={vol_ratio:.2f}",
            f"roc10={roc10:.2f}",
            f"votes={long_votes if direction == 'long' else short_votes}",
        ],
        family=family,
        structure_clarity=clarity,
    )


class DepthImbalanceSetup(RoadmapSetup):
    setup_id = "depth_imbalance"
    family = "orderbook"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)
    DEFAULTS: ClassVar[dict[str, float]] = {
        **RoadmapSetup.DEFAULTS,
        "min_depth_imbalance": 0.3334,
        "min_microprice_bias": 0.05,
        "min_close_position_long": 0.52,
        "max_close_position_short": 0.48,
        "min_volume_ratio": 0.80,
        "min_roc10_abs_pct": 0.00,
        "min_rr": 1.9,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return detect_depth_imbalance(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["DepthImbalanceSetup"]
