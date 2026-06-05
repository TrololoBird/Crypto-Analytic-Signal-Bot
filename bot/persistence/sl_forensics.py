"""SL forensic engine — classify stop-loss outcomes with candle replay context."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import polars as pl

from bot.features.prepare_frame import _prepare_frame

ForensicType = Literal[
    "STOP_HUNT",
    "IMMEDIATE_ADVERSE",
    "THESIS_FAILED",
    "TIMING_OFF",
]

SL_RESULTS = frozenset({"stop_loss", "breakeven_stop", "trailing_stop"})

_FORENSIC_LABELS: dict[str, str] = {
    "STOP_HUNT": "TYPE 1 — Stop hunt (тезис остался после SL)",
    "IMMEDIATE_ADVERSE": "TYPE 2 — Мгновенное движение против входа",
    "THESIS_FAILED": "TYPE 3 — Тезис не реализовался",
    "TIMING_OFF": "TYPE 4 — Timing / chase / unclosed candle",
}


@dataclass(frozen=True, slots=True)
class ForensicMetrics:
    mfe: float = 0.0
    mae: float = 0.0
    active_minutes: int = 0
    post_sl_favorable_pct: float = 0.0
    post_sl_tp1_room_pct: float = 0.0
    post_sl_tp1_reached: bool = False
    entry_deviation_pct: float = 0.0
    closed_candle_valid: bool | None = None
    roc10_signal_bar: float | None = None
    roc10_prev_bar: float | None = None
    btc_roc10_at_signal: float | None = None
    btc_aligned: bool | None = None
    bars_before: int = 0
    bars_after: int = 0


@dataclass(frozen=True, slots=True)
class ForensicCase:
    tracking_id: str
    symbol: str
    setup_id: str
    direction: str
    timeframe: str
    forensic_type: ForensicType
    forensic_subtype: str | None
    label: str
    score: float | None
    atr_pct: float | None
    bias_4h: str | None
    result: str
    signal_created_at: str | None
    sl_closed_at: str | None
    entry_mid: float | None
    activation_price: float | None
    stop_price: float | None
    tp1_price: float | None
    metrics: ForensicMetrics
    recommendations: tuple[str, ...] = ()
    legacy_sl_root_cause: str | None = None

    def to_row_dict(self) -> dict[str, Any]:
        return {
            "tracking_id": self.tracking_id,
            "symbol": self.symbol,
            "setup_id": self.setup_id,
            "direction": self.direction,
            "timeframe": self.timeframe,
            "forensic_type": self.forensic_type,
            "forensic_subtype": self.forensic_subtype,
            "label": self.label,
            "sl_root_cause_legacy": self.legacy_sl_root_cause,
            "mfe": self.metrics.mfe,
            "mae": self.metrics.mae,
            "post_sl_favorable_pct": self.metrics.post_sl_favorable_pct,
            "post_sl_tp1_reached": int(self.metrics.post_sl_tp1_reached),
            "closed_candle_valid": (
                None
                if self.metrics.closed_candle_valid is None
                else int(self.metrics.closed_candle_valid)
            ),
            "entry_deviation_pct": self.metrics.entry_deviation_pct,
            "btc_correlation_at_sl": self.metrics.btc_roc10_at_signal,
            "active_minutes": self.metrics.active_minutes,
            "score": self.score,
            "atr_pct": self.atr_pct,
            "recommendations": json.dumps(list(self.recommendations), ensure_ascii=False),
            "metrics": json.dumps(asdict(self.metrics), default=str),
            "signal_created_at": self.signal_created_at,
            "sl_closed_at": self.sl_closed_at,
        }


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _roc10_at_index(work: pl.DataFrame, end_idx: int, lookback: int = 10) -> float | None:
    if work.height < 2 or "close" not in work.columns:
        return None
    end_idx = max(0, min(end_idx, work.height - 1))
    start_idx = max(0, end_idx - lookback)
    try:
        start = float(work.item(start_idx, "close"))
        end = float(work.item(end_idx, "close"))
    except (TypeError, ValueError):
        return None
    if start <= 0.0 or end <= 0.0:
        return None
    return (end / start - 1.0) * 100.0


def _direction_from_roc(roc: float | None, threshold: float = 0.0) -> str | None:
    if roc is None:
        return None
    if roc > threshold:
        return "long"
    if roc < -threshold:
        return "short"
    return None


def assess_closed_candle_validity(
    work: pl.DataFrame,
    *,
    event_dt: datetime,
    direction: str,
) -> tuple[bool | None, float | None, float | None]:
    """True when momentum on last closed bar (-2) agrees with signal direction."""
    if work.is_empty() or "close_time" not in work.columns:
        return None, None, None
    closed = work.filter(pl.col("close_time") <= pl.lit(event_dt))
    if closed.height < 3:
        return None, None, None
    signal_idx = closed.height - 1
    prev_idx = signal_idx - 1
    roc_signal = _roc10_at_index(closed, signal_idx)
    roc_prev = _roc10_at_index(closed, prev_idx)
    dir_norm = direction.lower()
    if roc_prev is None:
        return None, roc_signal, roc_prev
    prev_dir = _direction_from_roc(roc_prev, threshold=0.05)
    valid = prev_dir is None or prev_dir == dir_norm
    return valid, roc_signal, roc_prev


def measure_post_sl_from_candles(
    candles: pl.DataFrame,
    *,
    direction: str,
    exit_price: float,
    tp1: float,
    closed_at: datetime,
    window_hours: float = 4.0,
) -> tuple[float, bool, float]:
    """Max favorable % after SL and whether TP1 was touched."""
    if candles.is_empty() or exit_price <= 0.0:
        return 0.0, False, 0.0
    sign = 1.0 if direction == "long" else -1.0
    exit_px = float(exit_price)
    tp1_room = sign * (float(tp1) - exit_px) / exit_px * 100.0 if tp1 > 0.0 else 0.0
    max_fav = 0.0
    tp1_hit = False
    deadline = closed_at.timestamp() + window_hours * 3600.0
    cols = [c for c in ("close_time", "high", "low") if c in candles.columns]
    if len(cols) < 3:
        return 0.0, False, tp1_room
    for row in candles.select(cols).to_dicts():
        bar_time = _parse_dt(row.get("close_time"))
        if bar_time is None or bar_time <= closed_at:
            continue
        if bar_time.timestamp() > deadline:
            break
        high = float(row.get("high") or 0.0)
        low = float(row.get("low") or 0.0)
        probe = high if direction == "long" else low
        move = sign * (probe - exit_px) / exit_px * 100.0
        if move > max_fav:
            max_fav = move
        if tp1 > 0.0:
            if direction == "long" and high >= tp1:
                tp1_hit = True
            if direction == "short" and low <= tp1:
                tp1_hit = True
    return max_fav, tp1_hit, tp1_room


def classify_forensic_type(
    *,
    direction: str,
    mfe: float,
    mae: float,
    active_minutes: int,
    post_sl_favorable_pct: float,
    post_sl_tp1_room_pct: float,
    post_sl_tp1_reached: bool,
    closed_candle_valid: bool | None,
    entry_deviation_pct: float,
    atr_pct: float | None,
    bias_4h: str | None,
) -> tuple[ForensicType, str | None]:
    atr = float(atr_pct or 0.0)
    stale_threshold = max(0.15, 1.5 * atr) if atr > 0.0 else 1.0

    if post_sl_tp1_reached or (post_sl_favorable_pct >= 1.0 and post_sl_tp1_room_pct > 1.5):
        return "STOP_HUNT", "post_sl_recovery"

    if mfe <= 0.05:
        subtype = "quick_stop" if active_minutes <= 15 else "zero_mfe"
        if direction == "long" and str(bias_4h or "").lower() == "downtrend":
            subtype = "bear_long"
        return "IMMEDIATE_ADVERSE", subtype

    if closed_candle_valid is False or entry_deviation_pct > stale_threshold:
        subtype = "unclosed_candle" if closed_candle_valid is False else "entry_chase"
        return "TIMING_OFF", subtype

    if mfe > 0.4 and mae > 0.0 and (mfe / mae) >= 0.4:
        return "STOP_HUNT", "partial_thesis_then_stop"

    return "THESIS_FAILED", None


def _recommendations_for(
    forensic_type: ForensicType,
    subtype: str | None,
    setup_id: str,
) -> tuple[str, ...]:
    recs: list[str] = []
    if forensic_type == "STOP_HUNT":
        recs.append("Рассмотреть увеличение sl_buffer_atr или structural stop anchor для setup.")
        recs.append("Проверить post-SL window — возможен stop hunt, не снижать confluence.")
    elif forensic_type == "IMMEDIATE_ADVERSE":
        recs.append("Убедиться что entry_staleness filter активен (fix-sl-A).")
        if subtype == "quick_stop":
            recs.append("Сигнал активируется слишком поздно — confirmed bar + chase guard.")
        if subtype == "bear_long":
            recs.append("Hard block long при bias_4h=downtrend для continuation setups.")
    elif forensic_type == "TIMING_OFF":
        recs.append("Детектор должен подтверждать на closed candle (df[-2]), не forming tail.")
        if subtype == "entry_chase":
            recs.append("Снизить late_entry_chase_pct или ужесточить max_entry_deviation_atr_mult.")
    else:
        recs.append(f"Калибровать пороги {setup_id} после 50+ post-fix outcomes.")
    return tuple(recs)


def build_forensic_case(
    row: dict[str, Any],
    *,
    candles_15m: pl.DataFrame | None = None,
    _candles_1h: pl.DataFrame | None = None,
    btc_candles_15m: pl.DataFrame | None = None,
    bars_before: int = 0,
    bars_after: int = 0,
) -> ForensicCase:
    """Build a classified forensic case from DB row + optional candle replay."""
    features: dict[str, Any] = {}
    raw_feat = row.get("features")
    if isinstance(raw_feat, str):
        try:
            features = json.loads(raw_feat)
        except json.JSONDecodeError:
            features = {}
    elif isinstance(raw_feat, dict):
        features = raw_feat

    direction = str(row.get("direction") or "")
    mfe = float(row.get("mfe") or row.get("max_profit_pct") or 0.0)
    mae = float(row.get("mae") or row.get("max_loss_pct") or 0.0)
    t_entry = int(row.get("time_to_entry_min") or 0)
    t_exit = int(row.get("time_to_exit_min") or 0)
    active_minutes = max(0, t_exit - t_entry)

    created_at = _parse_dt(row.get("created_at") or row.get("signal_created_at"))
    closed_at = _parse_dt(row.get("closed_at") or row.get("sl_closed_at"))
    event_dt = _parse_dt(row.get("activated_at")) or created_at or closed_at

    entry_mid = _f(row.get("entry_mid"))
    activation_price = _f(row.get("activation_price") or row.get("entry_price"))
    stop_price = _f(row.get("stop") or row.get("exit_price"))
    tp1 = _f(row.get("take_profit_1"))

    atr_pct = _f(row.get("atr_pct")) or _f(features.get("atr_pct"))
    score = _f(row.get("score")) or _f(features.get("score"))
    bias_4h = str(row.get("bias_4h") or features.get("bias_4h") or "") or None

    post_sl_fav = _f(features.get("post_sl_favorable_pct")) or 0.0
    post_sl_room = _f(features.get("post_sl_tp1_room_pct")) or 0.0
    post_sl_tp1_reached = False

    closed_valid: bool | None = None
    roc_signal: float | None = None
    roc_prev: float | None = None
    btc_roc: float | None = None
    btc_aligned: bool | None = None

    if candles_15m is not None and not candles_15m.is_empty() and event_dt is not None:
        try:
            work = _prepare_frame(candles_15m)
        except (ValueError, TypeError, RuntimeError):
            work = candles_15m
        closed_valid, roc_signal, roc_prev = assess_closed_candle_validity(
            work, event_dt=event_dt, direction=direction
        )

    if (
        candles_15m is not None
        and not candles_15m.is_empty()
        and closed_at is not None
        and stop_price
    ):
        post_sl_fav_calc, post_sl_tp1_reached, post_sl_room_calc = measure_post_sl_from_candles(
            candles_15m,
            direction=direction,
            exit_price=stop_price,
            tp1=float(tp1 or 0.0),
            closed_at=closed_at,
        )
        if post_sl_fav <= 0.0:
            post_sl_fav = post_sl_fav_calc
        if post_sl_room <= 0.0:
            post_sl_room = post_sl_room_calc

    if btc_candles_15m is not None and event_dt is not None and not btc_candles_15m.is_empty():
        try:
            btc_work = _prepare_frame(btc_candles_15m)
            btc_closed = btc_work.filter(pl.col("close_time") <= pl.lit(event_dt))
            if btc_closed.height >= 2:
                btc_roc = _roc10_at_index(btc_closed, btc_closed.height - 1)
                btc_dir = _direction_from_roc(btc_roc, threshold=0.05)
                btc_aligned = btc_dir is None or btc_dir == direction.lower()
        except (ValueError, TypeError, RuntimeError):
            pass

    entry_deviation = 0.0
    if entry_mid and activation_price and entry_mid > 0.0:
        entry_deviation = abs(activation_price - entry_mid) / entry_mid * 100.0

    ftype, subtype = classify_forensic_type(
        direction=direction,
        mfe=mfe,
        mae=mae,
        active_minutes=active_minutes,
        post_sl_favorable_pct=post_sl_fav,
        post_sl_tp1_room_pct=post_sl_room,
        post_sl_tp1_reached=post_sl_tp1_reached,
        closed_candle_valid=closed_valid,
        entry_deviation_pct=entry_deviation,
        atr_pct=atr_pct,
        bias_4h=bias_4h,
    )

    metrics = ForensicMetrics(
        mfe=mfe,
        mae=mae,
        active_minutes=active_minutes,
        post_sl_favorable_pct=post_sl_fav,
        post_sl_tp1_room_pct=post_sl_room,
        post_sl_tp1_reached=post_sl_tp1_reached,
        entry_deviation_pct=entry_deviation,
        closed_candle_valid=closed_valid,
        roc10_signal_bar=roc_signal,
        roc10_prev_bar=roc_prev,
        btc_roc10_at_signal=btc_roc,
        btc_aligned=btc_aligned,
        bars_before=bars_before,
        bars_after=bars_after,
    )

    legacy = str(features.get("sl_root_cause") or "") or None
    setup_id = str(row.get("setup_id") or "")

    return ForensicCase(
        tracking_id=str(row.get("tracking_id") or ""),
        symbol=str(row.get("symbol") or ""),
        setup_id=setup_id,
        direction=direction,
        timeframe=str(row.get("timeframe") or "15m"),
        forensic_type=ftype,
        forensic_subtype=subtype,
        label=_FORENSIC_LABELS.get(ftype, ftype),
        score=score,
        atr_pct=atr_pct,
        bias_4h=bias_4h,
        result=str(row.get("result") or ""),
        signal_created_at=str(row.get("created_at") or "") or None,
        sl_closed_at=str(row.get("closed_at") or "") or None,
        entry_mid=entry_mid,
        activation_price=activation_price,
        stop_price=stop_price,
        tp1_price=tp1,
        metrics=metrics,
        recommendations=_recommendations_for(ftype, subtype, setup_id),
        legacy_sl_root_cause=legacy,
    )


def render_case_card(case: ForensicCase) -> str:
    m = case.metrics
    lines = [
        f"## {case.symbol} · {case.setup_id} · {case.direction.upper()}",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| tracking_id | `{case.tracking_id}` |",
        f"| result | `{case.result}` |",
        f"| **Forensic** | **{case.label}** |",
        f"| subtype | `{case.forensic_subtype or '—'}` |",
        f"| legacy sl_root_cause | `{case.legacy_sl_root_cause or '—'}` |",
        f"| score | {case.score if case.score is not None else '—'} |",
        f"| atr_pct | {case.atr_pct if case.atr_pct is not None else '—'}% |",
        f"| bias_4h | {case.bias_4h or '—'} |",
        f"| MFE / MAE | {m.mfe:.2f}% / {m.mae:.2f}% |",
        f"| active_min | {m.active_minutes} |",
        f"| post-SL favorable | {m.post_sl_favorable_pct:.2f}% |",
        f"| post-SL TP1 reached | {m.post_sl_tp1_reached} |",
        f"| closed candle valid | {m.closed_candle_valid} |",
        f"| roc10 signal/prev bar | {m.roc10_signal_bar} / {m.roc10_prev_bar} |",
        f"| entry deviation | {m.entry_deviation_pct:.2f}% |",
        f"| BTC roc10 / aligned | {m.btc_roc10_at_signal} / {m.btc_aligned} |",
        f"| candle window | -{m.bars_before} / +{m.bars_after} bars |",
        "",
        "### Recommendations",
        "",
    ]
    if case.recommendations:
        lines.extend(f"- {r}" for r in case.recommendations)
    else:
        lines.append("- —")
    lines.append("")
    return "\n".join(lines)


def render_aggregate_report(
    cases: list[ForensicCase],
    *,
    analyzed_at: str,
) -> str:
    type_counts = Counter(c.forensic_type for c in cases)
    setup_counts = Counter(c.setup_id for c in cases)
    lines = [
        "# SL Forensic Aggregate Report",
        "",
        f"**Analyzed at:** {analyzed_at}",
        f"**Cases:** {len(cases)}",
        "",
        "## Classification summary",
        "",
        "| TYPE | Count | Share |",
        "|------|------:|------:|",
    ]
    total = len(cases) or 1
    for ftype in ("STOP_HUNT", "IMMEDIATE_ADVERSE", "THESIS_FAILED", "TIMING_OFF"):
        n = type_counts.get(ftype, 0)
        lines.append(f"| {_FORENSIC_LABELS.get(ftype, ftype)} | {n} | {100 * n / total:.1f}% |")
    lines.extend(["", "## By setup", "", "| setup_id | n |", "|----------|--:|"])
    for sid, n in setup_counts.most_common():
        lines.append(f"| {sid} | {n} |")

    lines.extend(["", "## Actionable recommendations (aggregated)", ""])
    if type_counts.get("IMMEDIATE_ADVERSE", 0) >= max(1, len(cases) // 3):
        lines.append(
            "- **P1:** Доминирует IMMEDIATE_ADVERSE — проверить fix-sl-A в post-fix session."
        )
    if type_counts.get("STOP_HUNT", 0) >= 2:
        lines.append("- **P2:** STOP_HUNT cluster — review sl_buffer_atr per high-vol symbols.")
    if type_counts.get("TIMING_OFF", 0) >= 1:
        lines.append("- **P3:** TIMING_OFF — audit confirmed-bar path на orderbook strategies.")
    if not lines[-1].startswith("-"):
        lines.append("- Недостаточно кейсов для агрегированных выводов — собрать post-fix sample.")

    lines.extend(["", "## Per-case cards", ""])
    lines.extend(render_case_card(case) for case in cases)
    return "\n".join(lines)


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:
        return None
    return parsed


__all__ = [
    "SL_RESULTS",
    "ForensicCase",
    "ForensicMetrics",
    "ForensicType",
    "assess_closed_candle_validity",
    "build_forensic_case",
    "classify_forensic_type",
    "measure_post_sl_from_candles",
    "render_aggregate_report",
    "render_case_card",
]
