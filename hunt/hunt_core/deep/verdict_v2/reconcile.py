"""Context reconciliation — direction vs order-flow / liquidity / structure."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from hunt_core.deep.verdict_v2._helpers import safe_float
from hunt_core.deep.verdict_v2.types import EngineOutput, ExpectedPath, PatternConfidence, TradePlan

ReconcileLevel = Literal["coherent", "mild_conflict", "strong_conflict"]

_DOM_CONFLICT = 0.15
_DOM_STRONG = 0.28
_CONF_GAP_MILD = 0.35
_CONF_GAP_STRONG = 0.55
_MAGNET_INTENSITY_MIN = 0.50


@dataclass(frozen=True, slots=True)
class FactorContribution:
    factor: str
    long: float
    short: float
    weight: float
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    level: ReconcileLevel
    conflicts: tuple[str, ...] = ()
    caveats: tuple[str, ...] = ()
    strength_multiplier: float = 1.0
    factor_contributions: tuple[FactorContribution, ...] = ()


def build_factor_contributions(engines: dict[str, EngineOutput]) -> tuple[FactorContribution, ...]:
    """Per-engine blended inputs for message provenance (R1)."""
    out: list[FactorContribution] = []
    for name, eng in sorted(engines.items()):
        out.append(
            FactorContribution(
                factor=name,
                long=eng.long,
                short=eng.short,
                weight=eng.blend_weight,
                evidence=tuple(eng.evidence[:3]),
            )
        )
    return tuple(out)


def _side_bias(side: str) -> str:
    return "long" if side in {"long", "weak_long"} else "short" if side in {"short", "weak_short"} else "neutral"


def _dom_conflict(side: str, imb: float) -> tuple[str | None, bool]:
    """Return (conflict_code, is_strong)."""
    if side == "short" and imb > _DOM_CONFLICT:
        strong = imb >= _DOM_STRONG
        return "dom_buyers_vs_short", strong
    if side == "long" and imb < -_DOM_CONFLICT:
        strong = imb <= -_DOM_STRONG
        return "dom_sellers_vs_long", strong
    return None, False


def _band_conflicts(side: str, pos: EngineOutput | None) -> list[str]:
    if pos is None:
        return []
    up = pos.upside_reward_pct
    down = pos.downside_reward_pct
    if up <= 0 and down <= 0:
        return []
    total = up + down
    if total <= 0:
        return []
    up_share = up / total
    down_share = down / total
    out: list[str] = []
    if side == "short" and up_share > down_share + _CONF_GAP_MILD:
        out.append("upside_band_vs_short")
    if side == "long" and down_share > up_share + _CONF_GAP_MILD:
        out.append("downside_band_vs_long")
    return out


_SYNTHETIC_LIQ_SOURCES = frozenset(
    {"leverage_tier_estimate", "forward", "entry_anchored", "prospective"}
)


def _zone_is_realized(z: dict[str, Any]) -> bool:
    src = str(z.get("source") or "realized")
    if src in _SYNTHETIC_LIQ_SOURCES:
        return False
    if int(z.get("event_count") or 0) > 0:
        return True
    return src == "realized"


def _liq_magnet_beyond_stop(
    row: dict[str, Any],
    *,
    side: str,
    stop: float,
    price: float,
) -> str | None:
    if stop <= 0 or price <= 0:
        return None
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    maps = row.get("maps") if isinstance(row.get("maps"), dict) else {}
    liq = maps.get("liquidation") if isinstance(maps, dict) else None
    zones: list[dict[str, Any]] = []
    if isinstance(liq, dict):
        for z in liq.get("forward_zones") or []:
            if isinstance(z, dict) and _zone_is_realized(z):
                zones.append(z)
    for z in market.get("liq_density_zones") or []:
        if isinstance(z, dict) and _zone_is_realized(z):
            zones.append(z)

    if market.get("liq_synthetic_only"):
        return None

    best_px = 0.0
    best_int = 0.0
    for z in zones:
        try:
            intensity = float(z.get("intensity") or z.get("significance_pct") or 0)
            px = float(z.get("price_center") or z.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if intensity < _MAGNET_INTENSITY_MIN or px <= 0:
            continue
        if intensity > best_int:
            best_int = intensity
            best_px = px

    if best_px <= 0:
        if market.get("liq_synthetic_only"):
            return None
        if side == "short":
            magnet = safe_float(market.get("liq_heatmap_nearest_short"))
            if magnet > stop and magnet > price:
                return "liq_magnet_above_stop"
        else:
            magnet = safe_float(market.get("liq_heatmap_nearest_long"))
            if 0 < magnet < stop and magnet < price:
                return "liq_magnet_below_stop"
        return None

    if side == "short" and best_px > stop and best_px > price:
        return "liq_magnet_above_stop"
    if side == "long" and best_px < stop and best_px < price:
        return "liq_magnet_below_stop"
    return None


def _poc_conflict(row: dict[str, Any], *, side: str, patterns: PatternConfidence) -> str | None:
    structure = row.get("structure") if isinstance(row.get("structure"), dict) else {}
    regime = row.get("regime") if isinstance(row.get("regime"), dict) else {}
    price = safe_float(row.get("price"))
    poc = safe_float((structure.get("key_levels") or {}).get("poc") if isinstance(structure.get("key_levels"), dict) else 0)
    if poc <= 0:
        poc = safe_float(regime.get("poc_1h"))
    if poc <= 0 or price <= 0:
        return None
    pid = patterns.primary.id
    cites_up = pid in {"short_squeeze", "accumulation", "stop_hunt"} or "above_poc" in patterns.primary.evidence
    cites_down = pid in {"long_squeeze", "distribution", "bear_continuation", "liquidity_sweep"} or "below_poc" in patterns.primary.evidence
    if side == "short" and cites_up and price > poc:
        return "poc_cite_mismatch"
    if side == "long" and cites_down and price < poc:
        return "poc_cite_mismatch"
    return None


def _classify_level(conflicts: list[str]) -> ReconcileLevel:
    hard = {
        "liq_magnet_above_stop",
        "liq_magnet_below_stop",
        "dom_buyers_vs_short_strong",
        "dom_sellers_vs_long_strong",
    }
    if any(c in hard for c in conflicts):
        return "strong_conflict"
    if len(conflicts) >= 3:
        return "strong_conflict"
    if any(c.endswith("_vs_short") or c.endswith("_vs_long") for c in conflicts):
        gap_conflicts = [c for c in conflicts if "band" in c]
        if len(gap_conflicts) >= 1 and len(conflicts) >= 2:
            return "strong_conflict"
    if conflicts:
        return "mild_conflict"
    return "coherent"


def _caveats_for(conflicts: tuple[str, ...]) -> tuple[str, ...]:
    _RU = {
        "dom_buyers_vs_short": "стакан в пользу покупателей против шорта",
        "dom_sellers_vs_long": "стакан в пользу продавцов против лонга",
        "upside_band_vs_short": "апсайд-зона увереннее даунсайда при шорте",
        "downside_band_vs_long": "даунсайд-зона увереннее апсайда при лонге",
        "liq_magnet_above_stop": "магнит ликвидаций за стопом (вверх)",
        "liq_magnet_below_stop": "магнит ликвидаций за стопом (вниз)",
        "poc_cite_mismatch": "POC не согласуется с фактором сценария",
    }
    return tuple(_RU.get(c, c.replace("_", " ")) for c in conflicts)


def reconcile_context(
    row: dict[str, Any],
    path: ExpectedPath,
    plan: TradePlan | None,
    engines: dict[str, EngineOutput],
    patterns: PatternConfidence,
) -> ReconciliationResult:
    """Score direction-vs-context conflict; downgrade or withhold on contradiction."""
    side = _side_bias(str(path.direction))
    if side == "neutral" or path.type == "range":
        return ReconciliationResult(
            level="coherent",
            factor_contributions=build_factor_contributions(engines),
        )

    conflicts: list[str] = []
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    imb = safe_float(market.get("depth_imbalance") or market.get("map_book_imbalance_1pct"))
    dom, dom_strong = _dom_conflict(side, imb)
    if dom:
        conflicts.append(dom)
        if dom_strong:
            conflicts.append(f"{dom}_strong")

    pos = engines.get("positioning")
    conflicts.extend(_band_conflicts(side, pos))

    if plan is not None:
        liq = _liq_magnet_beyond_stop(row, side=side, stop=plan.stop_loss, price=safe_float(row.get("price")))
        if liq:
            conflicts.append(liq)

    poc = _poc_conflict(row, side=side, patterns=patterns)
    if poc:
        conflicts.append(poc)

    level = _classify_level(conflicts)
    caveats = _caveats_for(tuple(conflicts))
    mult = 1.0
    if level == "mild_conflict":
        mult = 0.78
    elif level == "strong_conflict":
        mult = 0.0
    return ReconciliationResult(
        level=level,
        conflicts=tuple(conflicts),
        caveats=caveats,
        strength_multiplier=mult,
        factor_contributions=build_factor_contributions(engines),
    )


__all__ = [
    "FactorContribution",
    "ReconciliationResult",
    "ReconcileLevel",
    "build_factor_contributions",
    "reconcile_context",
]
