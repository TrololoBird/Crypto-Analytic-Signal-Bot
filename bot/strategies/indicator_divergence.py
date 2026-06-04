"""indicator_divergence — strategy module (bot/strategies/)."""

from __future__ import annotations

from ..setups.spec_runtime import SpecDetectorSetup
from ._common import SpecHit, _pivot_rows, as_float, with_spec_columns

__all__ = ["detect_regular_divergence"]
import math
from typing import TYPE_CHECKING, ClassVar

from ..features import _swing_points
from ..setups import _build_signal, _compute_dynamic_score, _reject
from ..setups.spec_runtime import run_setup_detection
from ._roadmap import _confirmed_context_conflict

if TYPE_CHECKING:
    import polars as pl

    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal


def detect_regular_divergence(
    frame: pl.DataFrame,
    *,
    timeframe: str = "15m",
    setup_id: str = "indicator_divergence",
    require_oversold: bool = False,
) -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 48:
        return None
    atr = as_float(work.item(-1, "spec_atr14"))
    if atr <= 0.0:
        return None
    lows = _pivot_rows(work, price_column="low", indicator_column="rsi14", pivot="low")
    if len(lows) >= 2:
        old, new = lows[-2], lows[-1]
        if (
            new["price"] < old["price"]
            and new["indicator"] > old["indicator"]
            and (not require_oversold or min(old["indicator"], new["indicator"]) < 35.0)
        ):
            strategy = setup_id
            return SpecHit(
                strategy=strategy,
                direction="long",
                entry=as_float(work.item(-1, "close")),
                stop_basis=new["price"],
                atr=atr,
                timeframe=timeframe,
                reasons=(
                    f"regular_bullish_div price_ll={new['price']:.4f}",
                    f"rsi_hl={new['indicator']:.1f}",
                ),
                rsi=as_float(work.item(-1, "rsi14"), 50.0),
            )
    highs = _pivot_rows(work, price_column="high", indicator_column="rsi14", pivot="high")
    if len(highs) >= 2 and not require_oversold:
        old, new = highs[-2], highs[-1]
        if new["price"] > old["price"] and new["indicator"] < old["indicator"]:
            return SpecHit(
                strategy=setup_id,
                direction="short",
                entry=as_float(work.item(-1, "close")),
                stop_basis=new["price"],
                atr=atr,
                timeframe=timeframe,
                reasons=(
                    f"regular_bearish_div price_hh={new['price']:.4f}",
                    f"rsi_lh={new['indicator']:.1f}",
                ),
                rsi=as_float(work.item(-1, "rsi14"), 50.0),
            )
    return None


def _tail_pair(values: list[float]) -> tuple[float, float] | None:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    if len(finite) < 2:
        return None
    return finite[-2], finite[-1]


def _swing_values(
    frame: pl.DataFrame,
    mask: pl.Series,
    *,
    price_column: str,
    indicator_column: str,
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    if indicator_column not in frame.columns:
        return None
    rows = frame.filter(mask).select([price_column, indicator_column]).drop_nulls()
    if rows.height < 2:
        return None
    price_pair = _tail_pair([as_float(value, math.nan) for value in rows[price_column].to_list()])
    indicator_pair = _tail_pair(
        [as_float(value, math.nan) for value in rows[indicator_column].to_list()]
    )
    if price_pair is None or indicator_pair is None:
        return None
    return price_pair, indicator_pair


def _detect_indicator_divergence_extended(
    prepared: PreparedSymbol,
    _settings: BotSettings,
    _defaults: dict[str, float],
    effective: dict[str, float],
    setup_id: str,
    family: str,
) -> Signal | None:
    params = effective
    work = prepared.work_15m
    if work.height < 80:
        _reject(prepared, setup_id, "insufficient_15m_bars", bars=work.height)
        return None

    required = ("high", "low", "close", "atr14", "rsi14", "macd_hist", "volume_ratio20")
    missing = [column for column in required if column not in work.columns]
    if missing:
        _reject(prepared, setup_id, "missing_feature_columns", missing_fields=missing)
        return None

    close = as_float(work.item(-1, "close"))
    atr = as_float(work.item(-1, "atr14"))
    rsi = as_float(work.item(-1, "rsi14"), 50.0)
    vol_ratio = as_float(work.item(-1, "volume_ratio20"), 1.0)
    if min(close, atr) <= 0.0:
        _reject(prepared, setup_id, "invalid_indicator_state", close=close, atr=atr)
        return None
    if vol_ratio < float(params["min_volume_ratio"]):
        _reject(
            prepared,
            setup_id,
            "volume_confirmation_missing",
            volume_ratio=vol_ratio,
            min_volume_ratio=params["min_volume_ratio"],
        )
        return None

    sh_mask, sl_mask = _swing_points(
        work,
        n=max(2, int(params["swing_lookback"])),
        include_unconfirmed_tail=True,
    )
    indicator_columns = ("rsi14", "macd_hist", "obv", "delta_ratio")
    bullish_votes: list[str] = []
    bearish_votes: list[str] = []
    low_pair: tuple[float, float] | None = None
    high_pair: tuple[float, float] | None = None

    for column in indicator_columns:
        low_values = _swing_values(
            work,
            sl_mask,
            price_column="low",
            indicator_column=column,
        )
        if low_values is not None:
            price_pair, indicator_pair = low_values
            low_pair = price_pair
            old_price, new_price = price_pair
            old_indicator, new_indicator = indicator_pair
            price_delta_pct = (new_price / old_price - 1.0) * 100.0 if old_price else 0.0
            if (
                price_delta_pct <= -float(params["min_price_delta_pct"])
                and new_indicator > old_indicator
            ):
                bullish_votes.append(column)

        high_values = _swing_values(
            work,
            sh_mask,
            price_column="high",
            indicator_column=column,
        )
        if high_values is not None:
            price_pair, indicator_pair = high_values
            high_pair = price_pair
            old_price, new_price = price_pair
            old_indicator, new_indicator = indicator_pair
            price_delta_pct = (new_price / old_price - 1.0) * 100.0 if old_price else 0.0
            if (
                price_delta_pct >= float(params["min_price_delta_pct"])
                and new_indicator < old_indicator
            ):
                bearish_votes.append(column)

    min_votes = max(1, int(params["min_indicator_votes"]))
    direction: str | None = None
    reference_level: float | None = None
    votes: list[str] = []
    if len(bullish_votes) >= min_votes and rsi <= float(params["max_rsi_long"]):
        direction = "long"
        reference_level = (
            low_pair[1] if low_pair is not None else as_float(work["low"].tail(8).min())
        )
        votes = bullish_votes
    if (
        len(bearish_votes) >= min_votes
        and len(bearish_votes) > len(votes)
        and rsi >= float(params["min_rsi_short"])
    ):
        direction = "short"
        reference_level = (
            high_pair[1] if high_pair is not None else as_float(work["high"].tail(8).max())
        )
        votes = bearish_votes

    if direction is None or reference_level is None:
        _reject(
            prepared,
            setup_id,
            "regular_divergence_missing",
            bullish_votes=len(bullish_votes),
            bearish_votes=len(bearish_votes),
            min_indicator_votes=min_votes,
        )
        return None

    # Divergence is counter-trend capable — only penalize strong 1h conflict (2+ votes).
    if _confirmed_context_conflict(prepared, direction):
        _reject(prepared, setup_id, "htf_context_conflict", direction=direction)
        return None

    sl_buffer = float(params["sl_buffer_atr"])
    min_rr = float(params["min_rr"])
    entry_price = reference_level
    if direction == "long":
        stop = entry_price - atr * sl_buffer
        risk = entry_price - stop
        tp1 = entry_price + risk * min_rr
        tp2 = entry_price + risk * max(min_rr + 0.4, 2.2)
    else:
        stop = entry_price + atr * sl_buffer
        risk = stop - entry_price
        tp1 = entry_price - risk * min_rr
        tp2 = entry_price - risk * max(min_rr + 0.4, 2.2)
    if risk <= 0.0:
        _reject(prepared, setup_id, "invalid_stop", direction=direction, stop=stop)
        return None

    score = _compute_dynamic_score(
        direction=direction,
        base_score=float(params["base_score"]),
        vol_ratio=vol_ratio,
        rsi=rsi,
        structure_clarity=min(1.0, len(votes) / float(len(indicator_columns))),
    )
    reasons = [
        f"indicator_{'convergence' if direction == 'long' else 'divergence'}_{direction}",
        f"votes={','.join(votes)}",
        f"rsi={rsi:.1f}",
        f"vol_ratio={vol_ratio:.2f}",
        f"limit_entry={entry_price:.4f}",
    ]
    return _build_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=direction,
        score=score,
        timeframe="15m",
        reasons=reasons,
        strategy_family=family,
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        price_anchor=entry_price,
        atr=atr,
    )


def detect_indicator_divergence_setup(
    prepared: PreparedSymbol,
    settings: BotSettings,
    defaults: dict[str, float],
    effective: dict[str, float],
    setup_id: str,
    family: str,
) -> Signal | None:
    spec_kwargs = None
    return run_setup_detection(
        prepared=prepared,
        settings=settings,
        setup_id=setup_id,
        family=family,
        defaults=defaults,
        effective=effective,
        spec_detect=detect_regular_divergence,
        extended_detect=_detect_indicator_divergence_extended,
        spec_kwargs=spec_kwargs,
    )


__all__ = [
    "_detect_indicator_divergence_extended",
    "detect_indicator_divergence_setup",
    "detect_regular_divergence",
]


class IndicatorDivergenceSetup(SpecDetectorSetup):
    setup_id = "indicator_divergence"
    family = "reversal"
    confirmation_profile = "divergence_reversal"
    required_context = ("futures_flow",)
    required_features = ("rsi14", "macd_hist", "obv", "delta_ratio")

    DEFAULTS: ClassVar[dict[str, float]] = {
        "base_score": 0.53,
        "swing_lookback": 6.0,
        "min_price_delta_pct": 0.35,
        "min_indicator_votes": 1.5,
        "min_volume_ratio": 0.75,
        "max_rsi_long": 55.0,
        "min_rsi_short": 45.0,
        "sl_buffer_atr": 1.1,
        "min_rr": 1.9,
    }

    detect_setup = detect_indicator_divergence_setup


__all__ = ["IndicatorDivergenceSetup"]
