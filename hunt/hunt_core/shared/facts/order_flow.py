"""Order-flow synthesis from tick row + market block — strategy-neutral facts."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

from hunt_core.shared.facts.kline_flow import kline_bar_flow, resolve_flow_cvd_px

TrendDir = Literal["bull", "bear", "flat"]
AbsorptionKind = Literal["bid_absorption", "ask_absorption", "none"]


@dataclass(frozen=True, slots=True)
class OrderFlowSynthesis:
    cvd_trend: TrendDir
    cvd_note_ru: str
    absorption: AbsorptionKind
    absorption_note_ru: str
    aggressor: str
    aggressor_note_ru: str
    delta_30s: float | None
    delta_60s: float | None
    taker_5m: float | None
    depth_imbalance: float | None
    summary_ru: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "cvd_trend": self.cvd_trend,
            "cvd_note": self.cvd_note_ru,
            "absorption": self.absorption,
            "absorption_note": self.absorption_note_ru,
            "aggressor": self.aggressor,
            "aggressor_note": self.aggressor_note_ru,
            "delta_30s": self.delta_30s,
            "delta_60s": self.delta_60s,
            "taker_5m": self.taker_5m,
            "depth_imbalance": self.depth_imbalance,
            "summary": self.summary_ru,
        }


def _f_positive(value: Any) -> float | None:
    try:
        v = float(value)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _f_signed(value: Any) -> float | None:
    try:
        v = float(value)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def fmt_flow_qty(value: float | None) -> str:
    if value is None:
        return "—"
    av = abs(value)
    sign = "+" if value > 0 else ("−" if value < 0 else "")
    if av >= 1_000_000:
        return f"{sign}{av / 1_000_000:.1f}M"
    if av >= 1_000:
        return f"{sign}{av / 1_000:.1f}K"
    return f"{sign}{av:.0f}"


def _cvd_from_tf(row: dict[str, Any]) -> tuple[float | None, float | None]:
    tf = row.get("timeframes") or {}
    for key in ("1h", "15m", "5m"):
        snap = tf.get(key) or {}
        closed = tf.get(f"{key}_closed") or {}
        for block in (closed, snap):
            if not isinstance(block, dict):
                continue
            cur = _f_signed(block.get("session_cvd") or block.get("rolling_cvd_24h"))
            prev = _f_signed(block.get("session_cvd_prev") or block.get("cvd_prev"))
            if cur is not None:
                return cur, prev
    return None, None


def _cvd_from_row(row: dict[str, Any]) -> tuple[float | None, float | None]:
    cur, prev = _cvd_from_tf(row)
    if cur is not None:
        return cur, prev
    tf = row.get("timeframes") or {}
    market = row.get("market") or {}
    for interval in ("15m", "5m", "1m"):
        delta, _px = kline_bar_flow(tf, interval)
        if delta is not None:
            return delta, 0.0
        flow_delta, _flow_px, _src = resolve_flow_cvd_px(market, tf, interval=interval)
        if flow_delta is not None:
            return flow_delta, 0.0
    return None, None


def _infer_cvd_trend(cur: float | None, prev: float | None) -> tuple[TrendDir, str]:
    if cur is None:
        return "flat", "CVD недоступен"
    cur_s = fmt_flow_qty(cur)
    if prev is None:
        if cur > 0:
            return "bull", f"CVD положительный ({cur_s})"
        if cur < 0:
            return "bear", f"CVD отрицательный ({cur_s})"
        return "flat", "CVD ≈ 0"
    delta = cur - prev
    delta_s = fmt_flow_qty(delta)
    if delta > abs(cur) * 0.02 or delta > 500:
        return "bull", f"CVD растёт ({cur_s}, Δ{delta_s})"
    if delta < -abs(cur) * 0.02 or delta < -500:
        return "bear", f"CVD падает ({cur_s}, Δ{delta_s})"
    return "flat", f"CVD боковой ({cur_s})"


def _infer_absorption(
    *,
    depth_imb: float | None,
    delta_30s: float | None,
    taker_5m: float | None,
) -> tuple[AbsorptionKind, str]:
    if depth_imb is None or delta_30s is None:
        return "none", ""
    if depth_imb >= 0.12 and delta_30s > 0.5 and (taker_5m or 1.0) < 1.0:
        return "bid_absorption", "Поглощение на bid — продавцы не давят цену вниз"
    if depth_imb <= -0.12 and delta_30s < 0.5 and (taker_5m or 1.0) > 1.0:
        return "ask_absorption", "Поглощение на ask — покупатели не поднимают цену"
    return "none", ""


def _infer_aggressor(
    *,
    taker_5m: float | None,
    delta_30s: float | None,
    delta_60s: float | None,
) -> tuple[str, str]:
    if taker_5m is not None:
        if taker_5m >= 1.05:
            return "buyers", f"Агрессор: покупатели (taker {taker_5m:.2f})"
        if taker_5m <= 0.95:
            return "sellers", f"Агрессор: продавцы (taker {taker_5m:.2f})"
    if delta_60s is not None:
        if delta_60s > 0.5:
            return "buyers", f"Δ60s buy-heavy ({delta_60s * 100:.0f}% buy)"
        if delta_60s < 0.5:
            return "sellers", f"Δ60s sell-heavy ({delta_60s * 100:.0f}% buy)"
    if delta_30s is not None:
        if delta_30s > 0.5:
            return "buyers", f"Δ30s buy ({delta_30s * 100:.0f}% buy)"
        if delta_30s < 0.5:
            return "sellers", f"Δ30s sell ({delta_30s * 100:.0f}% buy)"
    return "balanced", "Агрессор сбалансирован"


def synthesize_order_flow(row: dict[str, Any]) -> OrderFlowSynthesis:
    """Build CVD / absorption / aggressor summary from tick row + market block."""
    market = row.get("market") or {}
    delta_30s = _f_signed(market.get("agg_trade_delta_30s"))
    delta_60s = _f_signed(market.get("agg_trade_delta_60s"))
    taker_5m = _f_positive(market.get("taker_5m"))
    depth_imb = _f_signed(market.get("depth_imbalance"))

    cur, prev = _cvd_from_row(row)
    cvd_trend, cvd_note = _infer_cvd_trend(cur, prev)
    absorption, abs_note = _infer_absorption(
        depth_imb=depth_imb, delta_30s=delta_30s, taker_5m=taker_5m
    )
    aggressor, aggr_note = _infer_aggressor(
        taker_5m=taker_5m, delta_30s=delta_30s, delta_60s=delta_60s
    )

    parts = [p for p in (cvd_note, abs_note, aggr_note) if p]
    if cvd_trend == "bear" and aggressor == "buyers":
        parts.append("⚠ CVD↓ vs taker buy — возможен краткий отскок")
    elif cvd_trend == "bull" and aggressor == "sellers":
        parts.append("⚠ CVD↑ vs taker sell — возможен flush")
    summary = " · ".join(parts) if parts else "Order flow нейтральный"

    return OrderFlowSynthesis(
        cvd_trend=cvd_trend,
        cvd_note_ru=cvd_note,
        absorption=absorption,
        absorption_note_ru=abs_note,
        aggressor=aggressor,
        aggressor_note_ru=aggr_note,
        delta_30s=delta_30s,
        delta_60s=delta_60s,
        taker_5m=taker_5m,
        depth_imbalance=depth_imb,
        summary_ru=summary,
    )


def cvd_from_row(row: dict[str, Any]) -> tuple[float | None, float | None]:
    """Session CVD from TF blocks; kline taker-buy delta when WS/session CVD absent."""
    return _cvd_from_row(row)


__all__ = [
    "AbsorptionKind",
    "OrderFlowSynthesis",
    "TrendDir",
    "cvd_from_row",
    "fmt_flow_qty",
    "synthesize_order_flow",
]
