"""Synthesize operator alerts from live telemetry (dashboard /api/v1/alerts)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .live import DashboardLiveData

JsonDict = dict[str, Any]


def build_live_operator_alerts(bot: Any, live_data: DashboardLiveData) -> list[JsonDict]:
    """Return zero-hit, WS health, and stale-stream alerts for the operator dashboard."""
    now = datetime.now(UTC).isoformat()
    alerts: list[JsonDict] = []

    decisions = live_data.decisions(limit=41, max_rows=50_000)
    for row in decisions.get("zero_signal_setups") or []:
        setup_id = str(row.get("setup_id") or "unknown")
        blockers = row.get("top_blockers") or []
        top_blocker = blockers[0].get("key") if blockers and isinstance(blockers[0], dict) else None
        alerts.append(
            {
                "type": "zero_hit",
                "severity": "warning",
                "title": f"Zero-hit setup: {setup_id}",
                "detail": (
                    f"{int(row.get('total') or 0)} decision rows with 0 raw signals"
                    + (f"; top blocker: {top_blocker}" if top_blocker else "")
                ),
                "ts": now,
                "setup_id": setup_id,
                "source": "live_decisions",
            }
        )

    runtime = live_data.runtime()
    ws_snapshot = runtime.get("ws_snapshot") if isinstance(runtime.get("ws_snapshot"), dict) else {}
    ws_manager = getattr(bot, "_ws_manager", None)
    ws_connected = bool(getattr(ws_manager, "is_connected", lambda: False)())
    if not ws_connected:
        alerts.append(
            {
                "type": "ws_down",
                "severity": "critical",
                "title": "WebSocket disconnected",
                "detail": "Futures WS manager reports no active connection.",
                "ts": now,
                "source": "ws_manager",
            }
        )

    msg_age = ws_snapshot.get("last_message_age_seconds")
    try:
        stale_seconds = float(msg_age) if msg_age is not None else None
    except TypeError, ValueError:
        stale_seconds = None
    if stale_seconds is not None and stale_seconds > 120.0:
        alerts.append(
            {
                "type": "ws_stale",
                "severity": "warning",
                "title": "WebSocket traffic stale",
                "detail": f"No WS message for {stale_seconds:.0f}s (threshold 120s).",
                "ts": now,
                "source": "ws_snapshot",
                "last_message_age_seconds": stale_seconds,
            }
        )

    stale_klines = int(ws_snapshot.get("stale_kline_stream_count") or 0)
    if stale_klines > 0:
        alerts.append(
            {
                "type": "ws_stale_klines",
                "severity": "warning",
                "title": "Stale kline streams",
                "detail": f"{stale_klines} subscribed kline stream(s) behind freshness threshold.",
                "ts": now,
                "source": "ws_snapshot",
                "stale_kline_stream_count": stale_klines,
            }
        )

    last_cycle = (live_data.overview() or {}).get("last_cycle") or {}
    if isinstance(last_cycle, dict):
        cap_counts = last_cycle.get("delivery_status_counts") or {}
        if isinstance(cap_counts, dict):
            for status, count in cap_counts.items():
                status_key = str(status).lower()
                if "cap" in status_key and int(count or 0) > 0:
                    alerts.append(
                        {
                            "type": "delivery_cap",
                            "severity": "info",
                            "title": f"Delivery cap: {status}",
                            "detail": f"{int(count)} signal(s) blocked by {status} in last cycle.",
                            "ts": now,
                            "source": "last_cycle",
                            "delivery_status": status,
                            "count": int(count),
                        }
                    )

    return alerts
