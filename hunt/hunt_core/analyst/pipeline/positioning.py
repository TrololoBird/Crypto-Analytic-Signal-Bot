from __future__ import annotations

from typing import Any

from hunt_core.analyst.pipeline._helpers import safe_float, safe_float_opt
from hunt_core.analyst.pipeline.config import PositioningConfig
from hunt_core.analyst.pipeline.funding_history import fetch_funding_percentile
from hunt_core.analyst.pipeline.oi_rank import fetch_oi_rank, fetch_oi_value
from hunt_core.analyst.pipeline.types import ModuleResult


def run_positioning_module(
    row: dict[str, Any],
    cfg: PositioningConfig,
    direction: str = "long",
    exchange: Any = None,
    *,
    trend_result: ModuleResult | None = None,
    structure_result: ModuleResult | None = None,
) -> ModuleResult:
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    sym = str(row.get("symbol") or "").upper()

    funding_rate = safe_float(market.get("funding_rate"))
    funding_zscore = safe_float_opt(market.get("funding_zscore_48h"))

    price = safe_float(row.get("price"))
    price_chg_4h = safe_float(row.get("chg_4h_pct"))

    evidence: list[str] = []
    funding_below_abs_threshold = funding_rate is not None and abs(funding_rate) < cfg.funding_abs_threshold_8h
    if funding_rate is not None:
        evidence.append(f"funding={funding_rate:.6f}")
        if funding_below_abs_threshold:
            evidence.append(f"funding_abs<{cfg.funding_abs_threshold_8h}")
    if funding_zscore is not None:
        evidence.append(f"funding_z={funding_zscore:.2f}")

    oi_rank = None
    if exchange is not None:
        try:
            oi_rank = fetch_oi_rank(exchange, sym)
        except Exception:
            pass

    if oi_rank is not None:
        evidence.append(f"oi_rank={oi_rank}")

    if oi_rank is not None and oi_rank >= cfg.oi_rank_max_for_vp:
        return ModuleResult(
            status="UNKNOWN",
            reason=f"OI rank={oi_rank} ≥ {cfg.oi_rank_max_for_vp} — мелкая монета",
            details={"funding_zscore": funding_zscore, "oi_rank": oi_rank, "evidence": evidence},
        )

    funding_pct = None
    if exchange is not None and not funding_below_abs_threshold:
        try:
            funding_pct = fetch_funding_percentile(
                exchange, sym,
                min_points=cfg.funding_min_points,
                max_age_days=cfg.funding_history_days,
            )
        except Exception:
            pass

    if funding_below_abs_threshold:
        evidence.append("funding_pctile=skipped (abs<threshold)")
    elif funding_pct is not None:
        evidence.append(f"funding_pctile={funding_pct*100:.1f}%")

    trend_passing = trend_result is not None and trend_result.status == "PASS"
    structure_passing = structure_result is not None and structure_result.status == "PASS"

    if direction == "long":
        if funding_pct is not None and funding_pct < cfg.funding_percentile_long_min:
            if not trend_passing or not structure_passing:
                return ModuleResult(
                    status="PASS",
                    reason=f"Funding {funding_pct*100:.1f}% перцентиль — дешёво, но Trend/Structure не подтверждают",
                    details={"funding_percentile": funding_pct, "funding_zscore": funding_zscore, "evidence": evidence},
                )
            return ModuleResult(
                status="PASS",
                reason=f"Funding {funding_pct*100:.1f}% перцентиль + Trend/Structure подтверждают лонг",
                details={"funding_percentile": funding_pct, "funding_zscore": funding_zscore, "evidence": evidence},
            )
        if funding_pct is not None and funding_pct > 0.5:
            evidence.append(f"funding_pctile={funding_pct*100:.1f}% > 50% — дорого для лонга")
    else:
        if funding_pct is not None and funding_pct > cfg.funding_percentile_short_min:
            if not trend_passing or not structure_passing:
                return ModuleResult(
                    status="FAIL",
                    reason=f"Funding {funding_pct*100:.1f}% перцентиль — перегрузка лонгов, но тренд бычий (contango trap)",
                    details={"funding_percentile": funding_pct, "funding_zscore": funding_zscore, "evidence": evidence, "contango_trap": True},
                )
            return ModuleResult(
                status="PASS",
                reason=f"Funding {funding_pct*100:.1f}% перцентиль + Trend/Structure подтверждают шорт",
                details={"funding_percentile": funding_pct, "funding_zscore": funding_zscore, "evidence": evidence},
            )
        if funding_pct is not None and funding_pct < 0.5:
            evidence.append(f"funding_pctile={funding_pct*100:.1f}% < 50% — дёшево для шорта")

    oi_now = None
    if exchange is not None:
        try:
            oi_now = fetch_oi_value(exchange, sym)
        except Exception:
            pass

    oi_24h_label = market.get("oi_24h_ago")
    oi_24h: float | None = None
    if oi_24h_label is not None:
        oi_24h = safe_float_opt(oi_24h_label)
    elif exchange is not None:
        pass

    oi_delta_pct = None
    if oi_now is not None and oi_24h is not None and oi_24h > 0:
        oi_delta_pct = (oi_now - oi_24h) / oi_24h * 100.0

    price_24h_ago = safe_float(row.get("close_24h_ago"))
    price_delta_pct = None
    if price > 0 and price_24h_ago is not None and price_24h_ago > 0:
        price_delta_pct = (price - price_24h_ago) / price_24h_ago * 100.0

    if oi_delta_pct is not None and price_delta_pct is not None:
        if direction == "long" and oi_delta_pct > cfg.oi_divergence_threshold_pct and price_delta_pct < -cfg.oi_divergence_price_threshold_pct:
            return ModuleResult(
                status="FAIL",
                reason=f"OI дивергенция: OI+{oi_delta_pct:.1f}%, цена{price_delta_pct:.1f}%",
                details={"oi_delta_pct": oi_delta_pct, "price_delta_pct": price_delta_pct, "direction": direction, "evidence": evidence},
            )
        if direction == "short" and oi_delta_pct < -cfg.oi_divergence_threshold_pct and price_delta_pct > cfg.oi_divergence_price_threshold_pct:
            return ModuleResult(
                status="FAIL",
                reason=f"OI дивергенция: OI{oi_delta_pct:.1f}%, цена+{price_delta_pct:.1f}%",
                details={"oi_delta_pct": oi_delta_pct, "price_delta_pct": price_delta_pct, "direction": direction, "evidence": evidence},
            )

    if oi_delta_pct is not None:
        evidence.append(f"oi_24h={oi_delta_pct:+.1f}%")

    if funding_rate is None and "oi_rank" not in evidence:
        return ModuleResult(
            status="UNKNOWN",
            reason="Нет данных позиционирования (funding/OI)",
            details={"evidence": evidence},
        )

    return ModuleResult(
        status="PASS",
        reason="Позиционирование нейтральное",
        details={"funding_percentile": funding_pct, "funding_zscore": funding_zscore, "evidence": evidence},
    )
