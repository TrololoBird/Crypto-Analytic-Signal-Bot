"""Stop-loss and outcome pattern analytics for the dashboard."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..persistence.repository.memory import MemoryRepository

_WIN_RESULTS = frozenset({"tp1_hit", "tp2_hit", "tp3_hit", "breakeven_stop", "trailing_stop"})
_LOSS_RESULTS = frozenset({"stop_loss"})


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except TypeError, ValueError:
        return None
    if parsed != parsed:
        return None
    return parsed


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value))
    except TypeError, ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = round((len(ordered) - 1) * pct)
    idx = max(0, min(idx, len(ordered) - 1))
    return ordered[idx]


def _score_outcome_r_squared(rows: list[dict[str, Any]]) -> float | None:
    """answers50 Q48(e): score_at_delivery vs binary win correlation."""
    pairs: list[tuple[float, float]] = []
    for row in rows:
        score = _safe_float(row.get("score"))
        if score is None:
            feat = row.get("features") if isinstance(row.get("features"), dict) else {}
            score = _safe_float(feat.get("score"))
        if score is None:
            continue
        result = str(row.get("result") or "")
        outcome = 1.0 if result in _WIN_RESULTS else 0.0 if result in _LOSS_RESULTS else None
        if outcome is None:
            continue
        pairs.append((score, outcome))
    if len(pairs) < 5:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    var_x = sum((x - mean_x) ** 2 for x in xs)
    if var_x <= 0.0:
        return None
    cov = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    slope = cov / var_x
    intercept = mean_y - slope * mean_x
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in pairs)
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    if ss_tot <= 0.0:
        return None
    return round(1.0 - ss_res / ss_tot, 4)


def _regime_bucket(features: dict[str, Any]) -> str:
    bias = str(features.get("bias_4h") or features.get("market_regime") or "neutral").lower()
    if bias in {"downtrend", "bear", "decline", "risk_off"}:
        return "bear"
    if bias in {"uptrend", "bull", "markup", "risk_on"}:
        return "bull"
    return "neutral"


async def build_outcomes_insights(
    repo: MemoryRepository,
    *,
    days: int = 30,
) -> dict[str, Any]:
    """Summarize closed trades with emphasis on stop-loss drivers."""
    outcomes = await repo.get_signal_outcomes(last_days=days)
    active_rows = await repo.get_active_signals(include_closed=True)

    trade_outcomes = [
        row for row in outcomes if str(row.get("result") or "") in _WIN_RESULTS | _LOSS_RESULTS
    ]
    losses = [row for row in trade_outcomes if str(row.get("result") or "") == "stop_loss"]
    wins = [row for row in trade_outcomes if str(row.get("result") or "") in _WIN_RESULTS]

    closed_active = [row for row in active_rows if str(row.get("status") or "") == "closed"]
    sl_active = [row for row in closed_active if str(row.get("close_reason") or "") == "stop_loss"]

    loss_scores = [_safe_float(r.get("score")) for r in sl_active]
    win_scores = [
        _safe_float(r.get("score"))
        for r in closed_active
        if str(r.get("close_reason") or "") in {"tp1_hit", "tp2_hit", "tp3_hit"}
    ]
    loss_atr = [_safe_float(r.get("atr_pct")) for r in sl_active]
    win_atr = [
        _safe_float(r.get("atr_pct"))
        for r in closed_active
        if str(r.get("close_reason") or "") in {"tp1_hit", "tp2_hit", "tp3_hit"}
    ]
    loss_scores = [v for v in loss_scores if v is not None]
    win_scores = [v for v in win_scores if v is not None]
    loss_atr = [v for v in loss_atr if v is not None]
    win_atr = [v for v in win_atr if v is not None]

    zero_mfe = sum(1 for row in losses if (_safe_float(row.get("mfe")) or 0.0) <= 0.0)
    loss_directions = Counter(str(row.get("direction") or "unknown").lower() for row in losses)
    win_directions = Counter(str(row.get("direction") or "unknown").lower() for row in wins)
    sl_by_setup = Counter(str(row.get("setup_id") or "unknown") for row in losses)
    win_by_setup = Counter(str(row.get("setup_id") or "unknown") for row in wins)
    sl_root_causes: Counter[str] = Counter()
    for row in losses:
        feat = row.get("features") if isinstance(row.get("features"), dict) else {}
        code = str((feat or {}).get("sl_root_cause") or "unknown")
        sl_root_causes[code] += 1

    setup_matrix: list[dict[str, Any]] = []
    for setup_id in sorted(set(sl_by_setup) | set(win_by_setup)):
        sl_count = int(sl_by_setup.get(setup_id, 0))
        win_count = int(win_by_setup.get(setup_id, 0))
        total = sl_count + win_count
        setup_matrix.append(
            {
                "setup_id": setup_id,
                "stop_loss": sl_count,
                "wins": win_count,
                "total": total,
                "win_rate": round(win_count / total, 4) if total else 0.0,
            }
        )
    setup_matrix.sort(key=lambda row: (-row["stop_loss"], row["setup_id"]))

    low_score_sl = sum(1 for score in loss_scores if score < 0.62)
    high_atr_sl = sum(1 for atr in loss_atr if atr >= 1.4)

    patterns: list[dict[str, Any]] = []
    if losses:
        patterns.append(
            {
                "key": "zero_mfe",
                "label": "MFE = 0 - цена не пошла в профит до стопа",
                "count": zero_mfe,
                "share": round(zero_mfe / len(losses), 4),
            }
        )
    if losses and len(loss_directions) == 1 and "long" in loss_directions:
        patterns.append(
            {
                "key": "long_only_losses",
                "label": "Все стопы - long (возможный контр-тренд альтов)",
                "count": len(losses),
                "share": 1.0,
            }
        )
    if loss_scores and win_scores:
        avg_sl = _avg(loss_scores) or 0.0
        avg_win = _avg(win_scores) or 0.0
        if avg_sl + 0.04 < avg_win:
            patterns.append(
                {
                    "key": "score_gap",
                    "label": f"Средний score стопов ниже побед ({avg_sl:.2f} vs {avg_win:.2f})",
                    "count": low_score_sl,
                    "share": round(low_score_sl / len(loss_scores), 4) if loss_scores else 0.0,
                }
            )
    if loss_atr and win_atr:
        avg_sl_atr = _avg(loss_atr) or 0.0
        avg_win_atr = _avg(win_atr) or 0.0
        if avg_sl_atr > avg_win_atr + 0.3:
            patterns.append(
                {
                    "key": "high_atr",
                    "label": (
                        f"Стопы при более высоком ATR% ({avg_sl_atr:.2f} vs {avg_win_atr:.2f})"
                    ),
                    "count": high_atr_sl,
                    "share": round(high_atr_sl / len(loss_atr), 4) if loss_atr else 0.0,
                }
            )

    recommendations: list[str] = []
    if zero_mfe == len(losses) and losses:
        recommendations.append(
            "Все стопы закрылись без движения в профит (MFE=0) - вход сразу против позиции. "
            "Проверьте timing, bear-regime фильтр для long и ширину стопа vs ATR."
        )

    active_by_tid = {
        str(row.get("tracking_id") or ""): row for row in active_rows if row.get("tracking_id")
    }
    post_sl_thesis_room = 0
    post_sl_recovery_rows: list[dict[str, Any]] = []
    for row in losses:
        active = active_by_tid.get(str(row.get("tracking_id") or ""))
        if not active:
            continue
        tp1 = _safe_float(active.get("take_profit_1"))
        exit_p = _safe_float(row.get("exit_price"))
        entry = _safe_float(active.get("entry_mid")) or _safe_float(row.get("entry_price"))
        direction = str(row.get("direction") or "").lower()
        if not tp1 or not exit_p or not entry:
            continue
        if direction == "long":
            room_to_tp_pct = (tp1 - exit_p) / exit_p * 100.0
            thesis_intact = tp1 > entry
        else:
            room_to_tp_pct = (exit_p - tp1) / exit_p * 100.0
            thesis_intact = tp1 < entry
        if room_to_tp_pct > 1.5 and thesis_intact:
            post_sl_thesis_room += 1
        post_sl_recovery_rows.append(
            {
                "symbol": row.get("symbol"),
                "setup_id": row.get("setup_id"),
                "direction": direction,
                "room_to_tp_pct": round(room_to_tp_pct, 2),
                "thesis_intact": thesis_intact,
                "time_to_exit_min": row.get("time_to_exit_min"),
            }
        )
    if post_sl_thesis_room > 0 and losses:
        patterns.append(
            {
                "key": "post_sl_thesis_room",
                "label": (
                    "После SL до TP1 оставался запас - возможен ранний стоп / отскок после выноса"
                ),
                "count": post_sl_thesis_room,
                "share": round(post_sl_thesis_room / len(losses), 4),
            }
        )
        recommendations.append(
            "Часть стопов: тезис (TP1) оставался достижимым после выхода - типично для "
            "long в bear (stop hunt → отскок). Усилить HTF/regime gate для long и "
            "не трактовать post-SL движение как «сигнал был верным» без нового входа."
        )
    if (
        losses
        and win_directions.get("short", 0) >= 3
        and loss_directions.get("long", 0) == len(losses)
    ):
        recommendations.append(
            "Short-сетапы дают TP, long-сетапы чаще ловят стоп - возможен bearish alt regime; "
            "усилить HTF-фильтр для long или confluence gate."
        )
    if loss_scores and win_scores and (_avg(loss_scores) or 0.0) < 0.62:
        recommendations.append(
            "Поднять min score / perf gate для стратегий с частыми стопами "
            "(turtle_soup, funding_reversal, price_velocity)."
        )
    if not recommendations and not trade_outcomes:
        recommendations.append(
            "Недостаточно закрытых сделок - дождитесь накопления outcomes в SQLite."
        )

    recent_losses: list[dict[str, Any]] = []
    for row in sorted(
        losses,
        key=lambda item: _parse_dt(item.get("closed_at")) or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )[:12]:
        feat = row.get("features") if isinstance(row.get("features"), dict) else {}
        diag = feat.get("sl_diagnostics") if isinstance(feat.get("sl_diagnostics"), dict) else {}
        recent_losses.append(
            {
                "symbol": row.get("symbol"),
                "setup_id": row.get("setup_id"),
                "direction": row.get("direction"),
                "pnl_pct": _safe_float(row.get("pnl_pct")),
                "pnl_r_multiple": _safe_float(row.get("pnl_r_multiple")),
                "mae": _safe_float(row.get("mae")),
                "mfe": _safe_float(row.get("mfe")),
                "closed_at": row.get("closed_at"),
                "sl_root_cause": feat.get("sl_root_cause"),
                "sl_root_cause_label": feat.get("sl_root_cause_label") or diag.get("label"),
                "sl_reasons": diag.get("reasons") or [],
                "post_sl_favorable_pct": _safe_float(feat.get("post_sl_favorable_pct")),
            }
        )

    total_trades = len(trade_outcomes)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "window_days": int(days),
        "data_quality": {
            "outcome_rows": len(outcomes),
            "trade_outcomes": total_trades,
            "closed_active_signals": len(closed_active),
            "sufficient_for_analysis": total_trades >= 5,
        },
        "summary": {
            "wins": len(wins),
            "stop_losses": len(losses),
            "win_rate": round(len(wins) / total_trades, 4) if total_trades else 0.0,
            "avg_r_multiple": round(
                sum(_safe_float(r.get("pnl_r_multiple")) or 0.0 for r in trade_outcomes)
                / total_trades,
                4,
            )
            if total_trades
            else 0.0,
        },
        "comparisons": {
            "avg_score_stop_loss": round(_avg(loss_scores), 4) if loss_scores else None,
            "avg_score_wins": round(_avg(win_scores), 4) if win_scores else None,
            "avg_atr_pct_stop_loss": round(_avg(loss_atr), 4) if loss_atr else None,
            "avg_atr_pct_wins": round(_avg(win_atr), 4) if win_atr else None,
            "zero_mfe_stop_losses": zero_mfe,
            "post_sl_thesis_room": post_sl_thesis_room,
        },
        "direction_breakdown": {
            "losses": dict(loss_directions),
            "wins": dict(win_directions),
        },
        "by_setup": setup_matrix,
        "patterns": patterns,
        "recommendations": recommendations,
        "sl_root_causes": dict(sl_root_causes),
        "sl_root_cause_labels": {
            "immediate_adverse_entry": "MFE≈0 - сразу против",
            "bear_long_immediate_stop": "Long в bear - мгновенный стоп",
            "bear_long_countertrend": "Long vs bear/BTC↓",
            "stop_hunt_post_recovery": "Stop hunt → отскок к TP",
            "quick_stop_no_follow_through": "Быстрый стоп",
            "wide_volatility_stop": "Высокий ATR",
            "thesis_failed": "Тезис не сработал",
        },
        "recent_stop_losses": recent_losses,
        "post_sl_recovery": post_sl_recovery_rows[:12],
    }


async def build_operator_weekly_kpi(
    repo: MemoryRepository,
    *,
    days: int = 7,
) -> dict[str, Any]:
    """answers50 Q48 — five weekly operator metrics."""
    outcomes = await repo.get_signal_outcomes(last_days=days)
    trade_rows = [
        row for row in outcomes if str(row.get("result") or "") in _WIN_RESULTS | _LOSS_RESULTS
    ]
    expired_rows = [row for row in outcomes if str(row.get("result") or "").startswith("expired")]
    losses = [row for row in trade_rows if str(row.get("result") or "") == "stop_loss"]

    sl_by_regime: dict[str, dict[str, int]] = {
        "bear": {"sl": 0, "total": 0},
        "neutral": {"sl": 0, "total": 0},
        "bull": {"sl": 0, "total": 0},
    }
    for row in trade_rows:
        feat = row.get("features") if isinstance(row.get("features"), dict) else {}
        bucket = _regime_bucket(feat)
        sl_by_regime[bucket]["total"] += 1
        if str(row.get("result") or "") == "stop_loss":
            sl_by_regime[bucket]["sl"] += 1
    sl_rate_by_regime = {
        key: round(vals["sl"] / vals["total"], 4) if vals["total"] else None
        for key, vals in sl_by_regime.items()
    }

    expired_by_tf: Counter[str] = Counter()
    expired_by_setup: Counter[str] = Counter()
    for row in expired_rows:
        entry_tf = str(row.get("entry_tf") or row.get("timeframe") or "unknown")
        expired_by_tf[entry_tf] += 1
        expired_by_setup[str(row.get("setup_id") or "unknown")] += 1
    total_outcomes = len(outcomes) or 1
    expired_rate = round(len(expired_rows) / total_outcomes, 4)

    zero_mfe_sl = sum(1 for row in losses if (_safe_float(row.get("mfe")) or 0.0) <= 0.0)
    mfe_max_before_sl = [_safe_float(row.get("mfe")) or 0.0 for row in losses]

    exit_minutes = [
        float(v)
        for v in (_safe_float(row.get("time_to_exit_min")) for row in trade_rows)
        if v is not None
    ]

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "window_days": int(days),
        "sample_size": len(outcomes),
        "metrics": {
            "sl_rate_by_regime_4h": sl_rate_by_regime,
            "sl_rate_by_regime_counts": sl_by_regime,
            "expired_rate": expired_rate,
            "expired_count": len(expired_rows),
            "expired_by_entry_tf": dict(expired_by_tf),
            "expired_by_setup": dict(
                sorted(expired_by_setup.items(), key=lambda item: (-item[1], item[0]))
            ),
            "zero_mfe_before_sl": zero_mfe_sl,
            "zero_mfe_share_of_sl": round(zero_mfe_sl / len(losses), 4) if losses else None,
            "mfe_median_before_sl": round(_percentile(mfe_max_before_sl, 0.5) or 0.0, 4)
            if mfe_max_before_sl
            else None,
            "time_to_exit_min_median": round(_percentile(exit_minutes, 0.5) or 0.0, 1)
            if exit_minutes
            else None,
            "time_to_exit_min_p90": round(_percentile(exit_minutes, 0.9) or 0.0, 1)
            if exit_minutes
            else None,
            "score_vs_outcome_r_squared": _score_outcome_r_squared(trade_rows),
        },
    }
