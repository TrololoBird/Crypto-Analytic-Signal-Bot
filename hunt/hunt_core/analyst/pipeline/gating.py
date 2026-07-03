from __future__ import annotations

from typing import Any

from hunt_core.analyst.pipeline._helpers import safe_float_opt
from hunt_core.analyst.pipeline.config import PipelineConfig
from hunt_core.analyst.pipeline.macro import run_macro_filter
from hunt_core.analyst.pipeline.positioning import run_positioning_module
from hunt_core.analyst.pipeline.regime import (
    MarketRegime,
    RegimeParameters,
    classify_market_regime,
)
from hunt_core.analyst.pipeline.risk import run_risk_module
from hunt_core.analyst.pipeline.structure import run_structure_module
from hunt_core.analyst.pipeline.trend import run_trend_module
from hunt_core.analyst.pipeline.types import (
    FiveModuleResult,
    MacroContext,
    ModuleResult,
    RiskLevels,
)


def _count_closed_4h_candles(row: dict[str, Any]) -> int:
    prep = row.get("_prepared")
    if prep is not None:
        work = getattr(prep, "work_4h", None)
        if work is not None and hasattr(work, "height") and work.height > 0:
            return work.height

    tf = row.get("timeframes") if isinstance(row.get("timeframes"), dict) else {}
    closed = tf.get("4h_closed")
    if isinstance(closed, dict):
        return 1 if closed.get("close") else 0
    return 0


def _new_coin_guard(row: dict[str, Any], cfg: PipelineConfig) -> str | None:
    listing_age = row.get("listing_age_days")
    if listing_age is not None and listing_age < cfg.new_coin.min_age_days:
        return f"listing_age_days={listing_age}<{cfg.new_coin.min_age_days}"

    spread = row.get("spread_bps")
    if spread is not None and spread > cfg.new_coin.max_spread_pct * 100:
        return f"spread={spread:.0f}bps>{cfg.new_coin.max_spread_pct*100:.0f}bps"

    oi_value = row.get("oi_value_usd")
    if oi_value is not None and oi_value < cfg.new_coin.min_oi_usd:
        return f"oi=${oi_value/1e6:.1f}M<${cfg.new_coin.min_oi_usd/1e6:.0f}M"

    return None


def run_five_module_pipeline(
    row: dict[str, Any],
    direction: str = "long",
    *,
    cfg: PipelineConfig | None = None,
    exchange: Any = None,
) -> FiveModuleResult:
    cfg = cfg or PipelineConfig.load()

    new_coin_reason = _new_coin_guard(row, cfg)
    if new_coin_reason is not None:
        return FiveModuleResult(
            macro=ModuleResult(status="FAIL", reason=f"New coin guard: {new_coin_reason}"),
            trend=ModuleResult(status="UNKNOWN", reason="Pipeline stopped at new coin guard"),
            structure=ModuleResult(status="UNKNOWN", reason="Pipeline stopped at new coin guard"),
            positioning=ModuleResult(status="UNKNOWN", reason="Pipeline stopped at new coin guard"),
            risk=ModuleResult(status="UNKNOWN", reason="Pipeline stopped at new coin guard"),
            gating="REJECT",
            direction="wait",
            reason=f"New coin guard: {new_coin_reason}",
            macro_context=None,
        )

    candle_count = _count_closed_4h_candles(row)
    if cfg.require_closed_candle and candle_count == 0:
        return FiveModuleResult(
            macro=ModuleResult(status="CAUTION", reason="Нет закрытых 4h свечей — ждём закрытия"),
            trend=ModuleResult(status="UNKNOWN", reason="Pipeline stopped: no closed candle"),
            structure=ModuleResult(status="UNKNOWN", reason="Pipeline stopped: no closed candle"),
            positioning=ModuleResult(status="UNKNOWN", reason="Pipeline stopped: no closed candle"),
            risk=ModuleResult(status="UNKNOWN", reason="Pipeline stopped: no closed candle"),
            gating="REJECT",
            direction="wait",
            reason="No closed 4h candle — pipeline deferred",
            macro_context=None,
        )

    if candle_count < cfg.new_coin.min_candles_absolute:
        return FiveModuleResult(
            macro=ModuleResult(status="FAIL", reason=f"Недостаточно истории: {candle_count}<{cfg.new_coin.min_candles_absolute} свечей"),
            trend=ModuleResult(status="UNKNOWN", reason=f"History {candle_count}<{cfg.new_coin.min_candles_absolute}"),
            structure=ModuleResult(status="UNKNOWN", reason=f"History {candle_count}<{cfg.new_coin.min_candles_absolute}"),
            positioning=ModuleResult(status="UNKNOWN", reason=f"History {candle_count}<{cfg.new_coin.min_candles_absolute}"),
            risk=ModuleResult(status="UNKNOWN", reason=f"History {candle_count}<{cfg.new_coin.min_candles_absolute}"),
            gating="REJECT",
            direction="wait",
            reason=f"Insufficient history: {candle_count}<{cfg.new_coin.min_candles_absolute}",
        )

    insufficient_for_full = candle_count < cfg.new_coin.min_candles_4h

    btc_ctx = row.get("btc_context") or {}
    macro_data = None
    try:
        from hunt_core.analyst.pipeline.macro_data import fetch_macro_data
        macro_data = fetch_macro_data(
            api_key=cfg.macro.cmc_api_key,
            base_url=cfg.macro.cmc_base_url,
            max_age=cfg.macro.cmc_cache_ttl,
        )
    except Exception:
        pass

    regime, regime_params = classify_market_regime(
        btc_data=btc_ctx,
        macro_data=macro_data,
    )

    macro, macro_context = run_macro_filter(row, cfg.macro, direction=direction, exchange=exchange)
    if macro.status == "FAIL":
        return FiveModuleResult(
            macro=macro,
            trend=ModuleResult(status="UNKNOWN", reason="Pipeline stopped at Macro"),
            structure=ModuleResult(status="UNKNOWN", reason="Pipeline stopped at Macro"),
            positioning=ModuleResult(status="UNKNOWN", reason="Pipeline stopped at Macro"),
            risk=ModuleResult(status="UNKNOWN", reason="Pipeline stopped at Macro"),
            gating="REJECT",
            direction="wait",
            reason=f"Macro FAIL: {macro.reason}",
            regime=regime,
            macro_context=macro_context,
        )

    trend_cfg_override = None
    if regime != MarketRegime.NORMAL:
        trend_cfg_override = cfg.trend
        trend_cfg_override.ker_min_trend = regime_params.ker_min_trend
        trend_cfg_override.ker_max_caution = regime_params.ker_max_caution

    trend = run_trend_module(row, trend_cfg_override or cfg.trend, direction=direction)
    if trend.status == "FAIL":
        return FiveModuleResult(
            macro=macro,
            trend=trend,
            structure=ModuleResult(status="UNKNOWN", reason="Pipeline stopped at Trend"),
            positioning=ModuleResult(status="UNKNOWN", reason="Pipeline stopped at Trend"),
            risk=ModuleResult(status="UNKNOWN", reason="Pipeline stopped at Trend"),
            gating="REJECT",
            direction="wait",
            reason=f"Trend FAIL: {trend.reason}",
            regime=regime,
            macro_context=macro_context,
        )

    structure = run_structure_module(row, direction=direction)
    if structure.status == "FAIL":
        return FiveModuleResult(
            macro=macro,
            trend=trend,
            structure=structure,
            positioning=ModuleResult(status="UNKNOWN", reason="Pipeline stopped at Structure"),
            risk=ModuleResult(status="UNKNOWN", reason="Pipeline stopped at Structure"),
            gating="REJECT",
            direction="wait",
            reason=f"Structure FAIL: {structure.reason}",
            regime=regime,
            macro_context=macro_context,
        )

    if insufficient_for_full:
        positioning = ModuleResult(
            status="UNKNOWN",
            reason=f"Недостаточно истории ({candle_count}<{cfg.min_history_candles})",
        )
    else:
        positioning = run_positioning_module(
            row, cfg.positioning, direction=direction, exchange=exchange,
            trend_result=trend, structure_result=structure,
        )
    if positioning.status == "FAIL":
        return FiveModuleResult(
            macro=macro,
            trend=trend,
            structure=structure,
            positioning=positioning,
            risk=ModuleResult(status="UNKNOWN", reason="Pipeline stopped at Positioning"),
            gating="REJECT",
            direction="wait",
            reason=f"Positioning FAIL: {positioning.reason}",
            regime=regime,
            macro_context=macro_context,
        )

    has_caution = any(
        m.status == "CAUTION" for m in [macro, trend, structure, positioning]
    )
    has_unknown = any(
        m.status == "UNKNOWN" for m in [macro, trend, structure, positioning]
    )

    if regime_params.block_longs and direction == "long":
        return FiveModuleResult(
            macro=macro,
            trend=trend,
            structure=structure,
            positioning=positioning,
            risk=ModuleResult(status="UNKNOWN", reason=f"Regime {regime.value} blocks longs"),
            gating="REJECT",
            direction="wait",
            reason=f"Regime {regime.value}: longs blocked",
            regime=regime,
            macro_context=macro_context,
        )

    if regime_params.block_shorts and direction == "short":
        return FiveModuleResult(
            macro=macro,
            trend=trend,
            structure=structure,
            positioning=positioning,
            risk=ModuleResult(status="UNKNOWN", reason=f"Regime {regime.value} blocks shorts"),
            gating="REJECT",
            direction="wait",
            reason=f"Regime {regime.value}: shorts blocked",
            regime=regime,
            macro_context=macro_context,
        )

    if macro.status == "CAUTION":
        sizing_mod: float = 0.5
    elif has_caution:
        sizing_mod = 0.5
    else:
        sizing_mod = 1.0

    ker_val = safe_float_opt(
        trend.details.get("ker") if isinstance(trend.details, dict) else None
    )

    risk, risk_levels = run_risk_module(
        row, cfg.risk, direction,
        sizing_modifier=sizing_mod,
        regime=regime,
        regime_params=regime_params,
        ker=ker_val,
    )

    gating: str = "SIGNAL"
    if has_caution or macro.status == "CAUTION":
        gating = "CAUTION"
    if has_unknown and not has_caution and macro.status != "CAUTION":
        gating = "SIGNAL"

    reasons = []
    if macro.status != "PASS":
        reasons.append(f"Macro={macro.status}")
    if trend.status != "PASS":
        reasons.append(f"Trend={trend.status}")
    if structure.status != "PASS":
        reasons.append(f"Structure={structure.status}")
    if positioning.status != "PASS":
        reasons.append(f"Positioning={positioning.status}")

    reason_parts = [f"Gating={gating}", f"Regime={regime.value}"]
    if reasons:
        reason_parts.append(", ".join(reasons))
    if risk_levels is not None:
        reason_parts.append(f"SL={risk_levels.stop_loss}, TP1={risk_levels.tp1} ({risk_levels.rr_tp1}R)")
    reason = " | ".join(reason_parts)

    return FiveModuleResult(
        macro=macro,
        trend=trend,
        structure=structure,
        positioning=positioning,
        risk=risk,
        gating=gating,  # type: ignore[arg-type]
        direction=direction,
        reason=reason,
        regime=regime,
        macro_context=macro_context,
        risk_levels=risk_levels,
    )

