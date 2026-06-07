from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ._roadmap import (
    _build_atr_signal,
    _prev,
    _price_change_pct_confirmed,
    _reject,
)
from .roadmap_base import RoadmapSetup

if TYPE_CHECKING:
    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

__all__ = ["detect_btc_correlation"]


def detect_btc_correlation(
    prepared: PreparedSymbol,
    _settings: BotSettings,
    effective_params: dict[str, float],
    *,
    setup_id: str,
    family: str,
) -> Signal | None:
    params = effective_params
    if prepared.symbol == "BTCUSDT":
        _reject(prepared, setup_id, "pattern.benchmark_symbol")
        return None
    btc_bias = getattr(prepared, "btc_bias", None)
    if btc_bias is None or str(btc_bias).strip() == "":
        _reject(prepared, setup_id, "data.btc_context_missing")
        return None
    btc_phase = str(getattr(prepared, "btc_phase", "") or "").lower()
    if btc_bias not in {"uptrend", "downtrend", "bull", "bear"}:
        if btc_phase in {"markup", "accumulation"}:
            btc_bias = "bull"
        elif btc_phase in {"decline", "distribution"}:
            btc_bias = "bear"
        else:
            btc_bias = "neutral"
    work = prepared.work_15m
    # fix-sl-A: confirm momentum on last closed bar (df[-2]), not forming tail.
    vol_ratio = _prev(work, "volume_ratio20", 1.0)
    volume_penalty = vol_ratio < float(params["min_volume_ratio"])
    roc10 = _prev(work, "roc10", _price_change_pct_confirmed(work, 10))
    if abs(roc10) < float(params["min_roc10_abs_pct"]):
        _reject(prepared, setup_id, "context.momentum_too_low", roc10=roc10)
        return None
    if btc_bias in {"uptrend", "bull"} and prepared.bias_1h != "downtrend" and roc10 > 0.0:
        direction = "long"
    elif btc_bias in {"downtrend", "bear"} and prepared.bias_1h != "uptrend" and roc10 < 0.0:
        direction = "short"
    elif btc_bias == "neutral" and prepared.bias_1h == "uptrend" and roc10 > 0.0:
        direction = "long"
    elif btc_bias == "neutral" and prepared.bias_1h == "downtrend" and roc10 < 0.0:
        direction = "short"
    else:
        _reject(
            prepared,
            setup_id,
            "pattern.btc_correlation_not_aligned",
            btc_bias=btc_bias,
        )
        return None
    reasons = [
        f"btc_correlation_{direction}",
        f"btc_bias={btc_bias}",
        f"btc_phase={btc_phase or '-'}",
        f"roc10={roc10:.2f}",
    ]
    if volume_penalty:
        reasons.append(f"volume_penalty={vol_ratio:.2f}")
    # Limit order: sell into prev-bar high (resistance) for shorts, buy at prev-bar low
    # (support) for longs — EMA20 ≈ current price yields immediate market-fill.
    if direction == "long":
        entry_anchor = _prev(work, "low", 0.0) or None
    else:
        entry_anchor = _prev(work, "high", 0.0) or None
    return _build_atr_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=direction,
        params=params,
        confirmed_bar=True,
        entry_anchor=entry_anchor,
        reasons=reasons,
        family=family,
        structure_clarity=0.65 if volume_penalty else 0.75,
    )


class BTCCorrelationSetup(RoadmapSetup):
    setup_id = "btc_correlation"
    family = "multi_asset"
    confirmation_profile = "trend_follow"
    required_context = ("futures_flow",)
    DEFAULTS: ClassVar[dict[str, float]] = {
        **RoadmapSetup.DEFAULTS,
        "min_roc10_abs_pct": 0.10,
        "min_volume_ratio": 0.70,
        "sl_buffer_atr": 1.00,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return detect_btc_correlation(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["BTCCorrelationSetup"]
