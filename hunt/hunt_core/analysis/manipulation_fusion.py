"""ManipulationFusionScore — multi-domain assessment for hunt archetypes."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from hunt_core.maps.oi import OiRegime, oi_regime_from_row

Archetype = Literal["predump_short", "coil_long", "ignition_long", "none"]

_PREDUMP_PHASES = frozenset({"exhaustion_at_high", "distribution", "dump_initiating"})
_COIL_PHASES = frozenset({"accumulation", "breakout_arming", "recovery"})
_IGNITION_PHASES = frozenset({"post_dump_bounce", "accumulation"})


@dataclass(frozen=True, slots=True)
class FactorHit:
    domain: str
    name: str
    value: float | str | bool
    weight: float
    source_tag: str


@dataclass(frozen=True, slots=True)
class ManipulationAssessment:
    archetype: Archetype
    score_predump: float
    score_coil: float
    score_ignition: float
    primary_score: float
    factors: tuple[FactorHit, ...] = field(default_factory=tuple)
    oi_regime: OiRegime = "unknown"
    checks: dict[str, bool] = field(default_factory=dict)
    check_sources: dict[str, str] = field(default_factory=dict)
    pass_count: int = 0
    required_n: int = 0


def _f(row: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        block = row
        if "." in key:
            parts = key.split(".")
            block = row
            for p in parts[:-1]:
                block = block.get(p) if isinstance(block, dict) else {}
            key = parts[-1]
        if isinstance(block, dict) and block.get(key) is not None:
            try:
                return float(block[key])
            except (TypeError, ValueError):
                continue
    return default


def _bool_market(row: dict[str, Any], key: str) -> bool:
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    return bool(market.get(key))


def _phase(row: dict[str, Any]) -> str:
    lc = row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else {}
    return str(lc.get("phase") or "")


def _pos_in_range(row: dict[str, Any]) -> float:
    session = row.get("session") if isinstance(row.get("session"), dict) else {}
    return float(session.get("pos_in_range") or 0.5)


def squeeze_blocks_predump_short(row: dict[str, Any]) -> bool:
    """Buildix-style squeeze checklist — blocks predump when crowded shorts + neg funding."""
    return _squeeze_blocks_predump(row)


def _squeeze_blocks_predump(row: dict[str, Any]) -> bool:
    """Buildix-style squeeze checklist — blocks predump when crowded shorts + neg funding."""
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    funding = _f(row, "market.funding_rate", "market.live_funding_rate")
    oi_regime = oi_regime_from_row(row)
    taker = _f(row, "market.taker_buy_sell_ratio", default=1.0)
    checks = 0
    if funding < -0.0002:
        checks += 1
    if oi_regime in {"squeeze", "new_money_long"}:
        checks += 1
    if taker >= 1.02:
        checks += 1
    if _bool_market(row, "map_accum_bid_absorption"):
        checks += 1
    if str(market.get("map_cvd_divergence") or "") == "bullish_div":
        checks += 1
    return checks >= 4


def _apply_check(
    checks: dict[str, bool],
    sources: dict[str, str],
    name: str,
    ok: bool,
    source: str,
) -> bool:
    checks[name] = ok
    sources[name] = source
    return ok


def _coil_breakout_checks(row: dict[str, Any], market: dict[str, Any]) -> tuple[bool, bool]:
    """5m VAH break + vol ≥1.5× median (CoinXSight breakout standard)."""
    tf = row.get("timeframes") if isinstance(row.get("timeframes"), dict) else {}
    r5 = tf.get("5m_closed") or tf.get("5m") or {}
    vah_break = False
    vol_relative = False
    close5 = r5.get("close")
    vah = r5.get("vah") or market.get("map_vah") or market.get("vah")
    if close5 is not None and vah is not None:
        try:
            vah_break = float(close5) > float(vah)
        except (TypeError, ValueError):
            pass
    vol_ratio = r5.get("vol_ratio")
    if vol_ratio is not None:
        try:
            vol_relative = float(vol_ratio) >= 1.5
        except (TypeError, ValueError):
            pass
    return vah_break, vol_relative


def evaluate_manipulation_fusion(row: dict[str, Any]) -> ManipulationAssessment:
    """Score predump / coil / ignition domains and pick primary archetype."""
    phase = _phase(row)
    pos = _pos_in_range(row)
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    oi_regime = oi_regime_from_row(row)
    price = float(row.get("price") or 0)
    leg_gain = _f(row, "lifecycle.leg_gain_pct", default=0.0)
    if leg_gain <= 0:
        session = row.get("session") if isinstance(row.get("session"), dict) else {}
        leg_gain = float(session.get("leg_gain_pct") or 0)

    factors: list[FactorHit] = []
    checks: dict[str, bool] = {}
    check_sources: dict[str, str] = {}

    # --- predump domain ---
    predump = 0.0
    if _apply_check(checks, check_sources, "distribution_phase", phase in _PREDUMP_PHASES, "wyckoff"):
        predump += 22.0
        factors.append(FactorHit("D3", "distribution_phase", phase, 22.0, "wyckoff"))
    price = float(row.get("price") or 0)
    vah = market.get("map_vah") or market.get("vah")
    above_vah = False
    if price > 0 and vah is not None:
        try:
            above_vah = price > float(vah)
        except (TypeError, ValueError):
            pass
    near_top = pos >= 0.85 or above_vah
    if _apply_check(checks, check_sources, "pos_near_high", near_top, "vp_range"):
        predump += 18.0
        factors.append(FactorHit("D3", "pos_near_high", pos, 18.0, "vp_range"))
    if _apply_check(
        checks,
        check_sources,
        "oi_distribution",
        oi_regime in {"new_money_short", "coiling"},
        "adler",
    ):
        predump += 16.0
        factors.append(FactorHit("D7", "oi_regime", oi_regime, 16.0, "adler"))
    cvd = str(market.get("map_cvd_divergence") or "")
    if _apply_check(checks, check_sources, "bear_cvd_div", cvd == "bearish_div", "markettrace"):
        predump += 14.0
        factors.append(FactorHit("D6", "bear_cvd_div", True, 14.0, "markettrace"))
    struct = row.get("structure") if isinstance(row.get("structure"), dict) else {}
    if _apply_check(
        checks,
        check_sources,
        "sweep_reclaim",
        bool(struct.get("bsl_sweep") or struct.get("support_break")),
        "smc",
    ):
        predump += 12.0
        factors.append(FactorHit("D10", "sweep_reclaim", True, 12.0, "smc"))
    squeeze_block = _squeeze_blocks_predump(row)
    _apply_check(checks, check_sources, "anti_squeeze", not squeeze_block, "buildix")
    if squeeze_block:
        predump *= 0.35
        factors.append(FactorHit("D8", "squeeze_block", True, -30.0, "buildix"))
    if leg_gain >= 40.0:
        predump += 10.0
        factors.append(FactorHit("D3", "leg_gain", leg_gain, 10.0, "session"))

    # --- coil domain ---
    coil = 0.0
    acc = float(market.get("map_vp_accumulation") or 0)
    if _apply_check(checks, check_sources, "vp_accumulation", acc >= 0.55, "coinxsight"):
        coil += 20.0
        factors.append(FactorHit("D4", "vp_accumulation", acc, 20.0, "coinxsight"))
    if _apply_check(checks, check_sources, "coil_phase", phase in _COIL_PHASES, "wyckoff"):
        coil += 18.0
        factors.append(FactorHit("D4", "coil_phase", phase, 18.0, "wyckoff"))
    contraction = market.get("map_vp_va_contraction")
    va_ok = contraction is not None and float(contraction) < 0.85
    if _apply_check(checks, check_sources, "va_contraction", va_ok, "vp"):
        coil += 12.0
        factors.append(FactorHit("D4", "va_contraction", float(contraction or 0), 12.0, "vp"))
    if _apply_check(
        checks, check_sources, "bid_absorption", _bool_market(row, "map_accum_bid_absorption"), "orderbook"
    ):
        coil += 10.0
        factors.append(FactorHit("D4", "bid_absorption", True, 10.0, "orderbook"))
    if _apply_check(checks, check_sources, "bull_cvd_div", cvd == "bullish_div", "markettrace"):
        coil += 10.0
        factors.append(FactorHit("D6", "bull_cvd_div", True, 10.0, "markettrace"))
    vah_break, vol_relative = _coil_breakout_checks(row, market)
    if _apply_check(checks, check_sources, "vah_break_5m", vah_break, "coinxsight"):
        coil += 8.0
        factors.append(FactorHit("D5", "vah_break_5m", True, 8.0, "coinxsight"))
    if _apply_check(checks, check_sources, "vol_above_median_5m", vol_relative, "coinxsight"):
        coil += 8.0
        factors.append(FactorHit("D5", "vol_breakout", True, 8.0, "coinxsight"))

    # --- ignition domain ---
    ignition = 0.0
    funding = _f(row, "market.funding_rate", "market.live_funding_rate")
    if _apply_check(checks, check_sources, "neg_funding", funding < -0.0001, "buildix"):
        ignition += 18.0
        factors.append(FactorHit("D8", "neg_funding", funding, 18.0, "buildix"))
    short_liq = market.get("liq_heatmap_nearest_short")
    if short_liq is not None and price > 0:
        try:
            sl = float(short_liq)
            liq_above = sl > price
        except (TypeError, ValueError):
            liq_above = False
    else:
        liq_above = False
    if _apply_check(checks, check_sources, "short_liq_above", liq_above, "leionion") and liq_above:
        ignition += 16.0
        factors.append(FactorHit("D9", "short_liq_magnet", float(short_liq), 16.0, "leionion"))
    if _apply_check(
        checks,
        check_sources,
        "squeeze_regime",
        oi_regime in {"squeeze", "new_money_long"},
        "adler",
    ):
        ignition += 14.0
        factors.append(FactorHit("D7", "squeeze_regime", oi_regime, 14.0, "adler"))
    if _apply_check(
        checks,
        check_sources,
        "cvd_absorption",
        cvd == "bullish_div" and phase in _IGNITION_PHASES,
        "markettrace",
    ):
        ignition += 12.0
        factors.append(FactorHit("D6", "cvd_absorption", True, 12.0, "markettrace"))
    obi = _f(row, "market.orderbook_imbalance", default=0.0)
    if _apply_check(checks, check_sources, "obi_bid", obi > 0.08, "microstructure") and obi > 0.08:
        ignition += 10.0
        factors.append(FactorHit("D1", "obi_bid", obi, 10.0, "microstructure"))

    # Smart-money display checks (not in N-of-M required sets).
    wash = float(market.get("wash_trading_index") or market.get("wash_index") or 0)
    _apply_check(checks, check_sources, "vol_oi_sane", wash < 0.85 or wash <= 0, "wash")
    taker = _f(row, "market.taker_buy_sell_ratio", default=1.0)
    _apply_check(
        checks,
        check_sources,
        "flow_aligned",
        (taker >= 1.02 and cvd == "bullish_div") or (taker <= 0.98 and cvd == "bearish_div"),
        "smart_money",
    )

    score_predump = round(min(100.0, max(0.0, predump)), 1)
    score_coil = round(min(100.0, max(0.0, coil)), 1)
    score_ignition = round(min(100.0, max(0.0, ignition)), 1)

    from hunt_core.analysis.playbook_checks import best_archetype_by_ratio

    archetype, primary, pc, req = best_archetype_by_ratio(checks)
    if archetype == "none":
        primary = 0.0

    return ManipulationAssessment(
        archetype=archetype,  # type: ignore[arg-type]
        score_predump=score_predump,
        score_coil=score_coil,
        score_ignition=score_ignition,
        primary_score=primary,
        factors=tuple(factors),
        oi_regime=oi_regime,
        checks=checks,
        check_sources=check_sources,
        pass_count=pc,
        required_n=req,
    )


def assessment_to_dict(assessment: ManipulationAssessment) -> dict[str, Any]:
    return {
        "archetype": assessment.archetype,
        "score_predump": assessment.score_predump,
        "score_coil": assessment.score_coil,
        "score_ignition": assessment.score_ignition,
        "primary_score": assessment.primary_score,
        "oi_regime": assessment.oi_regime,
        "checks": dict(assessment.checks),
        "check_sources": dict(assessment.check_sources),
        "pass_count": assessment.pass_count,
        "required_n": assessment.required_n,
        "factors": [
            {
                "domain": f.domain,
                "name": f.name,
                "value": f.value,
                "weight": f.weight,
                "source": f.source_tag,
            }
            for f in assessment.factors
        ],
    }


def stamp_fusion_on_row(row: dict[str, Any]) -> ManipulationAssessment:
    """Evaluate fusion and attach ``manipulation_fusion`` dict to row."""
    assessment = evaluate_manipulation_fusion(row)
    row["manipulation_fusion"] = assessment_to_dict(assessment)
    row["entry_archetype"] = assessment.archetype if assessment.archetype != "none" else None
    return assessment


__all__ = [
    "Archetype",
    "FactorHit",
    "ManipulationAssessment",
    "assessment_to_dict",
    "evaluate_manipulation_fusion",
    "squeeze_blocks_predump_short",
    "stamp_fusion_on_row",
]
