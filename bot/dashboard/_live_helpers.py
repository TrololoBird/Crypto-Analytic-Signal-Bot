"""Pure utility helpers extracted from live.py and app.py (no bot dependency)."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import polars as pl

from bot.domain.labels import (
    reject_reason_ru,
)
from bot.domain.limit_entry import normalize_confirmation_profile
from bot.runtime.delivery_orchestrator import DELIVERY_SUCCESS_STATUSES
from bot.runtime.errors import DEFENSIVE_EXC

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

JsonDict = dict[str, Any]

# ── Numeric ───────────────────────────────────────────────────────────────

def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default

def _percent(value: float, digits: int = 2) -> float:
    return round(float(value) * 100.0, digits)

# ── Date ──────────────────────────────────────────────────────────────────

def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

# ── Counter ───────────────────────────────────────────────────────────────

def _counter_rows(counter: Counter[str], *, limit: int = 20) -> list[JsonDict]:
    return [
        {"key": str(key), "count": int(count)}
        for key, count in counter.most_common(max(1, int(limit)))
    ]

def _labeled_counter_rows(counter: Counter[str], *, limit: int = 20) -> list[JsonDict]:
    return [
        {
            "key": str(key),
            "count": int(count),
            "label_ru": reject_reason_ru(str(key)),
        }
        for key, count in counter.most_common(max(1, int(limit)))
    ]

# ── Row extraction ────────────────────────────────────────────────────────

def _rejected_row_confirmations(row: Mapping[str, Any]) -> dict[str, bool] | None:
    confirmations = row.get("confirmations")
    if not isinstance(confirmations, dict):
        details = row.get("details")
        if isinstance(details, dict):
            nested = details.get("confirmations")
            confirmations = nested if isinstance(nested, dict) else None
    if not isinstance(confirmations, dict):
        return None
    return {str(key): bool(value) for key, value in confirmations.items()}

def _rejected_row_confirmation_profile(row: Mapping[str, Any]) -> str:
    profile = row.get("confirmation_profile")
    if not profile:
        details = row.get("details")
        if isinstance(details, dict):
            profile = details.get("confirmation_profile")
    return normalize_confirmation_profile(str(profile) if profile else None)

# ── Delivery ──────────────────────────────────────────────────────────────

def _delivery_row_status(row: Mapping[str, Any]) -> str:
    return str(row.get("delivery_status") or row.get("status") or "unknown").strip().lower()

def _delivery_success_rows(rows: Iterable[Mapping[str, Any]]) -> list[JsonDict]:
    return [dict(row) for row in rows if _delivery_row_status(row) in DELIVERY_SUCCESS_STATUSES]

def _cycle_delivered_count(row: Mapping[str, Any]) -> int:
    success = row.get("delivery_success_count")
    if success is not None:
        return _safe_int(success)
    return _safe_int(row.get("delivered_count") or row.get("delivered_signals"))

# ── Decision / Routing ────────────────────────────────────────────────────

def _is_routing_excluded_decision_reason(reason: str) -> bool:
    """True when a strategy_decision row reflects routing/asset_fit, not detector evaluation."""
    code = str(reason or "").strip().lower()
    if code == "runtime.strategy_lane_excluded":
        return True
    return code.startswith("asset_fit.")

# ── Frame / WS ────────────────────────────────────────────────────────────

def _frame_readiness_fields(bot: Any) -> dict[str, int]:
    """Expose WS frame freshness counts used by operator runtime blocks."""
    ws = getattr(bot, "_ws_manager", None)
    if ws is None or not hasattr(ws, "state_snapshot"):
        return {}
    try:
        snap = ws.state_snapshot()
    except DEFENSIVE_EXC:
        return {}
    if not isinstance(snap, dict):
        return {}
    return {
        "frames_15m_ready": _safe_int(snap.get("fresh_klines_15m")),
        "frames_1h_ready": _safe_int(snap.get("warm_symbols")),
        "frames_4h_ready": _safe_int(snap.get("warm_symbols")),
    }

def _effective_shortlist(bot: Any) -> list[Any]:
    """Match operator shortlist fallback during ws_light bootstrap."""
    live = list(getattr(bot, "_shortlist", []) or [])
    if live:
        return live
    return list(getattr(bot, "_last_live_shortlist", []) or [])

# ── Funnel ────────────────────────────────────────────────────────────────

def _compute_cycle_totals(cycles: list[JsonDict]) -> JsonDict:
    return {
        "cycles": len(cycles),
        "detector_runs": sum(_safe_int(row.get("detector_runs")) for row in cycles),
        "candidates": sum(_safe_int(row.get("candidate_count")) for row in cycles),
        "selected": sum(
            _safe_int(row.get("selected_count") or row.get("selected_signals")) for row in cycles
        ),
        "delivered": sum(_cycle_delivered_count(row) for row in cycles),
    }

def _compute_session_delta(cycles: list[JsonDict]) -> JsonDict:
    latest = cycles[0] if cycles else {}
    return {
        "candidates": _safe_int(latest.get("candidate_count")),
        "selected": _safe_int(latest.get("selected_count") or latest.get("selected_signals")),
        "delivered": _cycle_delivered_count(latest),
    }

def _build_funnel_widget(
    cycle_totals: Mapping[str, Any], session_delta: Mapping[str, Any]
) -> JsonDict:
    stages = []
    for key, label_ru in (
        ("candidates", "кандидаты"),
        ("selected", "отобрано"),
        ("delivered", "отправлено"),
    ):
        count = _safe_int(cycle_totals.get(key))
        delta = _safe_int(session_delta.get(key))
        stages.append(
            {
                "key": key,
                "label_ru": label_ru,
                "count": count,
                "session_delta": delta,
            }
        )
    return {"stages": stages}

def _unified_top_blocker(
    *,
    rejected_counter: Counter[str],
    decision_counter: Counter[str],
) -> JsonDict | None:
    merged: Counter[str] = Counter()
    merged.update(rejected_counter)
    merged.update(decision_counter)
    if not merged:
        return None
    key, total = merged.most_common(1)[0]
    rejected_count = int(rejected_counter.get(key, 0))
    decision_count = int(decision_counter.get(key, 0))
    sources: list[str] = []
    if rejected_count:
        sources.append("rejected")
    if decision_count:
        sources.append("strategy_decisions")
    return {
        "key": key,
        "count": int(total),
        "label_ru": reject_reason_ru(key),
        "rejected_count": rejected_count,
        "decision_count": decision_count,
        "sources": sources,
    }

# ── Killzone / Confluence ─────────────────────────────────────────────────

def _compute_killzone() -> dict[str, bool]:
    now = datetime.now(UTC)
    hour = now.hour + now.minute / 60.0
    return {
        "london": 8 <= hour < 17,
        "ny": 13 <= hour < 22,
        "asia": 0 <= hour < 9,
    }

def _confluence_color(score: float) -> str:
    if score >= 80:
        return "#2fd17c"
    if score >= 60:
        return "#63a5ff"
    if score >= 40:
        return "#f5bf4f"
    if score >= 20:
        return "#ff9f43"
    return "#ff5b6b"

# ── Chart ─────────────────────────────────────────────────────────────────

def _normalize_kline_row(row: dict[str, Any]) -> dict[str, Any] | None:
    try:
        open_px = float(row.get("open") or row.get("o") or 0.0)
        high_px = float(row.get("high") or row.get("h") or 0.0)
        low_px = float(row.get("low") or row.get("l") or 0.0)
        close_px = float(row.get("close") or row.get("c") or 0.0)
    except (TypeError, ValueError):
        return None
    if close_px <= 0.0:
        return None
    ts = row.get("time") or row.get("close_time") or row.get("t")
    ts_text = ts.isoformat() if hasattr(ts, "isoformat") else str(ts or "")
    return {
        "time": ts_text,
        "open": open_px,
        "high": high_px,
        "low": low_px,
        "close": close_px,
    }

def _dataframe_to_kline_rows(df: Any) -> list[dict[str, Any]]:
    if df is None:
        return []
    try:
        if isinstance(df, pl.DataFrame) and not df.is_empty():
            cols = set(df.columns)
            time_col = (
                "time" if "time" in cols else ("close_time" if "close_time" in cols else None)
            )
            if time_col is None:
                return []
            return [
                {
                    "time": row.get(time_col),
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "close": row.get("close"),
                }
                for row in df.tail(200).to_dicts()
            ]
    except DEFENSIVE_EXC:
        pass
    return []

# ── Strategy lifecycle ────────────────────────────────────────────────────

def _runtime_strategy_status(row: dict[str, Any], catalog_status: str) -> str:
    trades = int(row.get("trades") or row.get("count") or 0)
    pending = int(row.get("pending_signals") or 0)
    active = int(row.get("active_signals") or 0)
    signals_seen = int(row.get("signals_seen") or 0)
    missing_outcomes = int(row.get("closed_missing_outcomes") or 0)
    detector_runs = int(row.get("detector_runs") or 0)
    detector_hits = int(row.get("detector_hits") or 0)
    expectancy = float(row.get("expectancy_r") or row.get("avg_rr") or 0.0)
    win_rate = float(row.get("win_rate") or 0.0)
    if trades <= 0:
        if pending or active:
            return "observing:open"
        if missing_outcomes:
            return "observing:outcome_repair_needed"
        if detector_hits:
            return "observing:detector_active"
        if detector_runs:
            return "observing:market_condition"
        if signals_seen:
            return f"observing:{catalog_status}"
        return "unverified"
    if trades >= 5 and expectancy < 0.0:
        return "needs_rework"
    if trades >= 5 and expectancy > 0.0 and win_rate >= 0.45:
        return "validated"
    return f"observing:{catalog_status}"
