"""Dashboard HTTP/WS route registration (extracted from app.py)."""

from __future__ import annotations

import asyncio
import csv
import logging
import secrets
import time
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from bot.domain.labels import labels_payload
from bot.market.rate_limit import (
    REST_WEIGHT_HARD_LIMIT,
    REST_WEIGHT_PACE_LIMIT,
    REST_WEIGHT_SOFT_LIMIT,
)
from bot.runtime.errors import DEFENSIVE_EXC

from .analytics import StrategyAnalytics
from .live_audit import audit_snapshot, build_dashboard_audit_snapshot
from .mobile_summary import build_mobile_summary
from .operator_alerts import build_live_operator_alerts
from .outcomes_insights import build_operator_weekly_kpi, build_outcomes_insights
from .user_summary import build_user_summary

if TYPE_CHECKING:
    from .app import BotDashboard

LOG = logging.getLogger("bot.dashboard.routes")


def register_routes(dashboard: BotDashboard) -> None:
    if not dashboard.app:
        return
    self = dashboard
    app = dashboard.app

    @self.app.get("/", response_class=HTMLResponse)
    async def root() -> str:
        return self._get_html_dashboard()

    @self.app.get("/api/status")
    async def status() -> dict[str, Any]:
        try:
            return await self._get_status()
        except DEFENSIVE_EXC:
            LOG.exception("dashboard api status error")
            return {"error": "status_unavailable"}

    @self.app.get("/api/metrics")
    async def metrics() -> dict[str, Any]:
        try:
            return await self._get_metrics()
        except DEFENSIVE_EXC:
            LOG.exception("dashboard api metrics error")
            return {"error": "metrics_unavailable"}

    @self.app.get("/api/health")
    async def health() -> dict[str, Any]:
        try:
            return cast("dict[str, Any]", await self.bot.health_check())
        except DEFENSIVE_EXC:
            LOG.exception("dashboard api health error")
            return {"status": "error"}

    @self.app.get("/api/analytics/report")
    async def analytics_report(days: int = 30, scope: str = "current_run") -> dict[str, Any]:
        days = max(1, min(int(days), 365))
        normalized_scope = str(scope or "current_run").strip().lower()
        if normalized_scope not in {"current_run", "rolling", "all"}:
            normalized_scope = "current_run"
        since = self._analytics_since(normalized_scope, days=days)
        since_key = since.isoformat() if since is not None else None

        # TTL Cache for analytics report (60s)
        now = time.monotonic()
        cache_key = (normalized_scope, days, since_key)
        cached = self._analytics_cache.get(cache_key)
        if cached and (now - cached[0]) < 60.0:
            return cached[1]

        reporter = StrategyAnalytics(repo=self.bot._modern_repo)
        report = await reporter.generate_report(
            days=days,
            since=since,
            scope=normalized_scope,
        )
        try:
            decision_summary = await asyncio.to_thread(
                self._get_strategy_decision_summary,
                limit_files=1,
                max_rows=50_000,
            )
        except RuntimeError as exc:
            if "shutdown" in str(exc).lower():
                LOG.debug("Dashboard endpoint called during shutdown: %s", exc)
                return {"error": "Server is shutting down"}
            raise
        merged = self._merge_strategy_catalog(report, decision_summary=decision_summary)

        self._analytics_cache[cache_key] = (now, merged)
        return merged

    @self.app.get("/api/analytics/confluence_legs")
    async def confluence_legs(max_rows: int = 100_000) -> dict[str, Any]:
        try:
            max_rows = max(1_000, min(int(max_rows), 250_000))
            return await self._live_data.confluence_legs(max_rows=max_rows)
        except RuntimeError as exc:
            if "shutdown" in str(exc).lower():
                LOG.debug("Dashboard endpoint called during shutdown: %s", exc)
                return {"error": "Server is shutting down"}
            raise
        except DEFENSIVE_EXC:
            LOG.exception("dashboard api confluence legs error")
            return {"error": "confluence_legs_unavailable"}

    @self.app.get("/api/analytics/confluence_legs_by_profile")
    async def confluence_legs_by_profile(max_rows: int = 100_000) -> dict[str, Any]:
        try:
            max_rows = max(1_000, min(int(max_rows), 250_000))
            return await self._live_data.confluence_legs_by_profile(max_rows=max_rows)
        except RuntimeError as exc:
            if "shutdown" in str(exc).lower():
                LOG.debug("Dashboard endpoint called during shutdown: %s", exc)
                return {"error": "Server is shutting down"}
            raise
        except DEFENSIVE_EXC:
            LOG.exception("dashboard api confluence legs by profile error")
            return {"error": "confluence_legs_by_profile_unavailable"}

    @self.app.get("/api/meta/labels")
    async def meta_labels() -> dict[str, dict[str, str]]:
        return labels_payload()

    @self.app.get("/api/live/overview")
    async def live_overview() -> dict[str, Any]:
        try:
            return await self._live_data.overview()
        except RuntimeError as exc:
            if "shutdown" in str(exc).lower():
                LOG.debug("Dashboard endpoint called during shutdown: %s", exc)
                return {"error": "Server is shutting down"}
            raise
        except DEFENSIVE_EXC:
            LOG.exception("dashboard live overview error")
            return {"error": "live_overview_unavailable"}

    @self.app.get("/api/live/funnel")
    async def live_funnel(max_rows: int = 100_000) -> dict[str, Any]:
        try:
            max_rows = max(1_000, min(int(max_rows), 250_000))
            return await self._live_data.funnel(max_rows=max_rows)
        except RuntimeError as exc:
            if "shutdown" in str(exc).lower():
                LOG.debug("Dashboard endpoint called during shutdown: %s", exc)
                return {"error": "Server is shutting down"}
            raise
        except DEFENSIVE_EXC:
            LOG.exception("dashboard live funnel error")
            return {"error": "live_funnel_unavailable"}

    @self.app.get("/api/live/shortlist")
    async def live_shortlist(limit: int = 80) -> dict[str, Any]:
        try:
            limit = max(1, min(int(limit), 200))
            return await self._live_data.shortlist(limit=limit)
        except RuntimeError as exc:
            if "shutdown" in str(exc).lower():
                LOG.debug("Dashboard endpoint called during shutdown: %s", exc)
                return {"error": "Server is shutting down"}
            raise
        except DEFENSIVE_EXC:
            LOG.exception("dashboard live shortlist error")
            return {"error": "live_shortlist_unavailable"}

    @self.app.get("/api/live/rejections")
    async def live_rejections(
        limit: int = 30,
        max_rows: int = 100_000,
    ) -> dict[str, Any]:
        try:
            limit = max(1, min(int(limit), 100))
            max_rows = max(1_000, min(int(max_rows), 250_000))
            return await self._live_data.rejections(limit=limit, max_rows=max_rows)
        except RuntimeError as exc:
            if "shutdown" in str(exc).lower():
                LOG.debug("Dashboard endpoint called during shutdown: %s", exc)
                return {"error": "Server is shutting down"}
            raise
        except DEFENSIVE_EXC:
            LOG.exception("dashboard live rejections error")
            return {"error": "live_rejections_unavailable"}

    @self.app.get("/api/live/decisions")
    async def live_decisions(
        limit: int = 40,
        max_rows: int = 100_000,
    ) -> dict[str, Any]:
        try:
            limit = max(1, min(int(limit), 100))
            max_rows = max(1_000, min(int(max_rows), 250_000))
            return await self._live_data.decisions(limit=limit, max_rows=max_rows)
        except RuntimeError as exc:
            if "shutdown" in str(exc).lower():
                LOG.debug("Dashboard endpoint called during shutdown: %s", exc)
                return {"error": "Server is shutting down"}
            raise
        except DEFENSIVE_EXC:
            LOG.exception("dashboard live decisions error")
            return {"error": "live_decisions_unavailable"}

    @self.app.get("/api/live/runtime")
    async def live_runtime() -> dict[str, Any]:
        try:
            return await self._live_data.runtime()
        except RuntimeError as exc:
            if "shutdown" in str(exc).lower():
                LOG.debug("Dashboard endpoint called during shutdown: %s", exc)
                return {"error": "Server is shutting down"}
            raise
        except DEFENSIVE_EXC:
            LOG.exception("dashboard live runtime error")
            return {"error": "live_runtime_unavailable"}

    @self.app.get("/api/live/delivery")
    async def live_delivery(limit: int = 25) -> dict[str, Any]:
        try:
            limit = max(1, min(int(limit), 100))
            return await self._live_data.delivery(limit=limit)
        except RuntimeError as exc:
            if "shutdown" in str(exc).lower():
                LOG.debug("Dashboard endpoint called during shutdown: %s", exc)
                return {"error": "Server is shutting down"}
            raise
        except DEFENSIVE_EXC:
            LOG.exception("dashboard live delivery error")
            return {"error": "live_delivery_unavailable"}

    @self.app.get("/api/live/telegram-preview")
    async def live_telegram_preview() -> dict[str, Any]:
        try:
            return await self._live_data.telegram_preview()
        except RuntimeError as exc:
            if "shutdown" in str(exc).lower():
                LOG.debug("Dashboard endpoint called during shutdown: %s", exc)
                return {"error": "Server is shutting down"}
            raise
        except DEFENSIVE_EXC:
            LOG.exception("dashboard live telegram preview error")
            return {"error": "live_telegram_preview_unavailable"}

        # ── WebSocket endpoints (spec: /ws/dashboard; canonical: /api/v1/ws) ──

    async def _handle_dashboard_ws(ws: WebSocket) -> None:
        if self._dashboard_token:
            provided = (
                ws.query_params.get("token", "")
                or (ws.headers.get("authorization", "") or "").removeprefix("Bearer ").strip()
            )
            if not secrets.compare_digest(provided, self._dashboard_token):
                await ws.close(code=1008, reason="unauthorized")
                return
        broadcaster = self._ws_broadcaster
        if broadcaster is None:
            await ws.close(code=1011, reason="ws_broadcaster_unavailable")
            return
        await broadcaster.connect(ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await broadcaster.disconnect(ws)

    @self.app.websocket("/api/v1/ws")
    async def dashboard_ws(ws: WebSocket) -> None:
        await _handle_dashboard_ws(ws)

        # ── API v1 endpoints ─────────────────────────────────────────────

    @self.app.get("/api/v1/status")
    async def v1_status() -> dict[str, Any]:
        return {
            "version": "2.0.0",
            "ws_clients": self._ws_broadcaster.client_count if self._ws_broadcaster else 0,
            "dashboard_online": True,
        }

    @self.app.get("/api/v1/signals/history")
    async def v1_signals_history(
        limit: int = 20,
        offset: int = 0,
        symbol: str = "",
        setup_id: str = "",
    ) -> dict[str, Any]:
        cap = max(1, min(int(limit), 100))
        off = max(0, int(offset))
        signals = await self._get_recent_signals(limit=cap + off)
        if symbol:
            signals = [s for s in signals if str(s.get("symbol", "")).upper() == symbol.upper()]
        if setup_id:
            signals = [s for s in signals if str(s.get("setup_id", "")) == setup_id]
        page = signals[off : off + cap]
        return {"data": page, "limit": cap, "offset": off, "total": len(signals)}

    @self.app.get("/api/v1/signals/active")
    async def v1_signals_active() -> list[dict[str, Any]]:
        try:
            return await self._get_active_signals()
        except DEFENSIVE_EXC:
            LOG.exception("v1 active signals error")
            return []

    @self.app.get("/api/v1/signals/live")
    async def v1_signals_live(limit: int = 20) -> list[dict[str, Any]]:
        try:
            return await self._get_live_signal_feed(max(1, min(int(limit), 100)))
        except DEFENSIVE_EXC:
            LOG.exception("v1 live signals error")
            return []

    @self.app.get("/api/v1/chart/klines")
    async def v1_chart_klines(
        symbol: str,
        interval: str = "15m",
        limit: int = 80,
    ) -> dict[str, Any]:
        try:
            return await self._get_chart_klines(
                symbol=symbol,
                interval=interval,
                limit=max(10, min(int(limit), 200)),
            )
        except DEFENSIVE_EXC:
            LOG.exception("v1 chart klines error")
            return {"error": "klines_unavailable"}

    @self.app.get("/api/v1/summary")
    async def v1_user_summary() -> dict[str, Any]:
        try:
            return await build_user_summary(self.bot, self._live_data)
        except DEFENSIVE_EXC:
            LOG.exception("v1 summary error")
            return {"error": "summary_unavailable"}

    @self.app.get("/api/v1/strategies/health")
    async def v1_strategies_health() -> list[dict[str, Any]]:
        cached = [dict(item) for item in (self._strategies_cache or [])]
        decisions = await self._live_data.decisions(limit=41, max_rows=50_000)
        zero_ids = {
            str(row.get("setup_id") or "") for row in (decisions.get("zero_signal_setups") or [])
        }
        setup_rates = {
            str(row.get("setup_id") or ""): row for row in (decisions.get("setup_reports") or [])
        }
        for item in cached:
            setup_id = str(item.get("id") or "")
            item["status"] = item.get("status", "beta")
            decision_row = setup_rates.get(setup_id) or {}
            item["zero_hit"] = setup_id in zero_ids
            item["detector_rows"] = int(decision_row.get("total") or 0)
            item["detector_signals"] = int(decision_row.get("signals") or 0)
            item["detector_signal_rate"] = float(decision_row.get("signal_rate") or 0.0)
        return cached

    @self.app.get("/api/v1/market/regime")
    async def v1_market_regime() -> dict[str, Any]:
        try:
            return self._get_market_regime()
        except DEFENSIVE_EXC:
            LOG.exception("v1 market regime error")
            return {"error": "regime_unavailable"}

        # ── Diary routes ────────────────────────────────────────────────

    @self.app.get("/api/v1/diary/trades")
    async def v1_diary_list(
        limit: int = 50,
        offset: int = 0,
        status: str = "",
        symbol: str = "",
        decision: str = "",
    ) -> list[dict[str, Any]]:
        store = await self._ensure_diary_store()
        if store is None:
            return []
        return await store.list_trades(
            limit=max(1, min(int(limit), 200)),
            offset=max(0, int(offset)),
            status=status or None,
            symbol=symbol or None,
            decision=decision or None,
        )

    @self.app.post("/api/v1/diary/trades")
    async def v1_diary_create(body: dict[str, Any]) -> dict[str, Any]:
        store = await self._ensure_diary_store()
        if store is None:
            return {"error": "diary_unavailable"}
        return await store.create_trade(body)

    @self.app.patch("/api/v1/diary/trades/{trade_id}")
    async def v1_diary_update(trade_id: str, body: dict[str, Any]) -> dict[str, Any]:
        store = await self._ensure_diary_store()
        if store is None:
            return {"error": "diary_unavailable"}
        result = await store.get_trade(trade_id)
        if result is None:
            return {"error": "trade_not_found"}
        updated = await store.update_trade(trade_id, body)
        return updated or {"error": "update_failed"}

    @self.app.get("/api/v1/diary/trades/{trade_id}")
    async def v1_diary_get(trade_id: str) -> dict[str, Any]:
        store = await self._ensure_diary_store()
        if store is None:
            return {"error": "diary_unavailable"}
        result = await store.get_trade(trade_id)
        return result or {"error": "trade_not_found"}

    @self.app.post("/api/v1/diary/trades/{trade_id}/close")
    async def v1_diary_close(trade_id: str, body: dict[str, Any]) -> dict[str, Any]:
        store = await self._ensure_diary_store()
        if store is None:
            return {"error": "diary_unavailable"}
        result = await store.close_trade(
            trade_id,
            exit_price=body.get("exit_price", 0.0),
            exit_time=body.get("exit_time", datetime.now(UTC).isoformat()),
            exit_reason=body.get("exit_reason", "manual_close"),
            pnl_percent=body.get("pnl_percent"),
            pnl_usd=body.get("pnl_usd"),
            tp_hit_level=body.get("tp_hit_level"),
            mood=body.get("mood"),
            notes=body.get("notes"),
        )
        return result or {"error": "close_failed"}

    @self.app.get("/api/v1/diary/analytics")
    async def v1_diary_analytics(days: int = 30) -> dict[str, Any]:
        store = await self._ensure_diary_store()
        if store is None:
            return {"error": "diary_unavailable"}
        return await store.get_analytics(days=max(1, min(int(days), 365)))

        # ── Strategy Correlation / Confluence ───────────────────────────

    @self.app.get("/api/v1/analytics/confluence-heatmap")
    async def v1_confluence_heatmap() -> list[dict[str, Any]]:
        decisions = await self._live_data.decisions(limit=50, max_rows=50000)
        return decisions.get("setup_reports", [])

    @self.app.get("/api/v1/analytics/outcomes")
    async def v1_analytics_outcomes(days: int = 30) -> dict[str, Any]:
        bot = self.bot
        repo = getattr(bot, "_modern_repo", None)
        if repo is None:
            return {"error": "repository_unavailable"}
        try:
            return await build_outcomes_insights(
                repo,
                days=max(1, min(int(days), 365)),
            )
        except DEFENSIVE_EXC:
            LOG.exception("v1 outcomes analytics error")
            return {"error": "outcomes_unavailable"}

    @self.app.get("/api/v1/analytics/operator-kpi")
    async def v1_analytics_operator_kpi(days: int = 7) -> dict[str, Any]:
        bot = self.bot
        repo = getattr(bot, "_modern_repo", None)
        if repo is None:
            return {"error": "repository_unavailable"}
        try:
            return await build_operator_weekly_kpi(
                repo,
                days=max(1, min(int(days), 90)),
            )
        except DEFENSIVE_EXC:
            LOG.exception("v1 operator KPI error")
            return {"error": "operator_kpi_unavailable"}

    @self.app.get("/api/v1/analytics/export")
    async def v1_analytics_export(days: int = 30) -> StreamingResponse:
        bot = self.bot
        repo = getattr(bot, "_modern_repo", None)
        if repo is None:
            return StreamingResponse(
                StringIO("error,repository_unavailable\n"), media_type="text/csv"
            )
        try:
            insights = await build_outcomes_insights(
                repo,
                days=max(1, min(int(days), 365)),
            )
            recent = insights.get("recent_stop_losses") or []
            buf = StringIO()
            writer = csv.writer(buf)
            writer.writerow(
                [
                    "symbol",
                    "direction",
                    "setup_id",
                    "result",
                    "pnl_pct",
                    "pnl_r_multiple",
                    "score",
                    "atr_pct",
                    "mae",
                    "mfe",
                    "entry_price",
                    "exit_price",
                    "sl_root_cause",
                    "sl_root_cause_label",
                ]
            )
            for row in recent:
                writer.writerow(
                    [
                        row.get("symbol", ""),
                        row.get("direction", ""),
                        row.get("setup_id", ""),
                        row.get("result", ""),
                        row.get("pnl_pct", ""),
                        row.get("pnl_r_multiple", ""),
                        row.get("score", ""),
                        row.get("atr_pct", ""),
                        row.get("mae", ""),
                        row.get("mfe", ""),
                        row.get("entry_price", ""),
                        row.get("exit_price", ""),
                        row.get("sl_root_cause", ""),
                        row.get("sl_root_cause_label", ""),
                    ]
                )
            buf.seek(0)
            now_str = datetime.now(UTC).strftime("%Y-%m-%d")
            return StreamingResponse(
                buf,
                media_type="text/csv",
                headers={"Content-Disposition": f"attachment; filename=outcomes-{now_str}.csv"},
            )
        except DEFENSIVE_EXC:
            LOG.exception("v1 analytics export error")
            return StreamingResponse(StringIO("error,export_failed\n"), media_type="text/csv")

    @self.app.get("/api/v1/mobile/summary")
    async def v1_mobile_summary() -> dict[str, Any]:
        try:
            return await build_mobile_summary(self.bot, self._live_data)
        except DEFENSIVE_EXC:
            LOG.exception("v1 mobile summary error")
            return {"error": "mobile_summary_unavailable"}

    @self.app.get("/api/v1/confluence/vetos")
    async def v1_confluence_vetos(limit: int = 50) -> list[dict[str, Any]]:
        rejections = await self._live_data.rejections(
            limit=max(1, min(limit, 100)),
            max_rows=50000,
        )
        return rejections.get("reasons", [])

        # ── Config endpoints ────────────────────────────────────────────

    @self.app.get("/api/v1/config/strategies")
    async def v1_config_strategies() -> list[dict[str, Any]]:
        return self._strategies_cache or []

    @self.app.patch("/api/v1/config/strategies")
    async def v1_config_strategies_patch(body: dict[str, Any]) -> JSONResponse:
        LOG.warning("config strategies patch not implemented (stub): %s", body)
        return JSONResponse(
            status_code=501,
            content={
                "error": "not_implemented",
                "detail": "Use config.toml to change strategy settings",
            },
        )

    @self.app.get("/api/v1/config/scoring")
    async def v1_config_scoring() -> dict[str, Any]:
        settings = getattr(self.bot, "settings", None)
        scoring = getattr(settings, "scoring", None)
        if scoring is None:
            return {"error": "scoring_config_unavailable"}
        return {
            "weights": {
                "mtf_alignment": getattr(scoring, "weight_mtf_alignment", 0.25),
                "volume_quality": getattr(scoring, "weight_volume_quality", 0.20),
                "structure_clarity": getattr(scoring, "weight_structure_clarity", 0.20),
                "risk_reward": getattr(scoring, "weight_risk_reward", 0.15),
                "crowd_position": getattr(scoring, "weight_crowd_position", 0.10),
                "oi_momentum": getattr(scoring, "weight_oi_momentum", 0.10),
            }
        }

    @self.app.patch("/api/v1/config/scoring")
    async def v1_config_scoring_patch(body: dict[str, Any]) -> JSONResponse:
        LOG.warning("config scoring patch not implemented (stub): %s", body)
        return JSONResponse(
            status_code=501,
            content={
                "error": "not_implemented",
                "detail": "Use config.toml to change scoring weights",
            },
        )

    @self.app.get("/api/v1/config/killzone")
    async def v1_config_killzone() -> dict[str, Any]:
        return {
            "london": {"start": "08:00", "end": "17:00", "utc": 0},
            "ny": {"start": "13:00", "end": "22:00", "utc": 0},
            "asia": {"start": "00:00", "end": "09:00", "utc": 0},
        }

    @self.app.patch("/api/v1/config/killzone")
    async def v1_config_killzone_patch(body: dict[str, Any]) -> JSONResponse:
        LOG.warning("config killzone patch not implemented (stub): %s", body)
        return JSONResponse(
            status_code=501,
            content={
                "error": "not_implemented",
                "detail": "Use config.toml to change killzone settings",
            },
        )

        # ── Alerts ──────────────────────────────────────────────────────

    @self.app.get("/api/v1/alerts")
    async def v1_alerts(limit: int = 50, since: str = "") -> list[dict[str, Any]]:
        cap = max(1, min(int(limit), 200))
        rows: list[dict[str, Any]] = []
        try:
            settings = getattr(self.bot, "settings", None)
            telemetry_dir = getattr(settings, "telemetry_dir", None) if settings else None
            if telemetry_dir is not None:
                runs_dir = Path(telemetry_dir) / "runs"
                if runs_dir.exists():
                    candidates = sorted(
                        runs_dir.glob("*/analysis/alerts*.jsonl"),
                        key=lambda p: p.stat().st_mtime,
                        reverse=True,
                    )
                    if candidates:
                        rows = self._read_recent_jsonl(candidates[0], limit=cap)
        except DEFENSIVE_EXC as exc:
            LOG.debug("alerts telemetry fetch error: %s", exc)
        try:
            live_rows = await build_live_operator_alerts(self.bot, self._live_data)
        except DEFENSIVE_EXC as exc:
            LOG.debug("alerts live synthesis error: %s", exc)
            live_rows = []
        merged = live_rows + [
            row
            for row in rows
            if not any(
                row.get("type") == live.get("type") and row.get("setup_id") == live.get("setup_id")
                for live in live_rows
            )
        ]
        if since:
            since_key = str(since).strip()
            merged = [
                row
                for row in merged
                if str(row.get("ts") or row.get("timestamp") or "") >= since_key
            ]
        return merged[:cap]

    @self.app.get("/api/live/ws-health")
    async def live_ws_health() -> dict[str, Any]:
        try:
            runtime = await self._live_data.runtime()
            ws_snapshot = runtime.get("ws_snapshot") if isinstance(runtime, dict) else {}
            health = await self.bot.health_check()
            return {
                "generated_at": datetime.now(UTC).isoformat(),
                "ws_connected": bool(health.get("ws_connected")),
                "health_status": health.get("status"),
                "ws_snapshot": ws_snapshot if isinstance(ws_snapshot, dict) else {},
                "latest_runtime": runtime.get("latest_runtime")
                if isinstance(runtime, dict)
                else {},
            }
        except DEFENSIVE_EXC:
            LOG.exception("dashboard live ws-health error")
            return {"error": "ws_health_unavailable"}

    @self.app.get("/api/v1/public-audit")
    async def v1_public_audit() -> dict[str, Any]:
        return await self._public_audit_manifest()

    @self.app.get("/api/live/audit")
    async def live_audit(max_rows: int = 20_000) -> dict[str, Any]:
        try:
            max_rows = max(1_000, min(int(max_rows), 50_000))

            snapshot = build_dashboard_audit_snapshot(
                overview=await self._live_data.overview(),
                funnel=await self._live_data.funnel(max_rows=max_rows),
                shortlist=await self._live_data.shortlist(),
                decisions=await self._live_data.decisions(max_rows=max_rows),
                rejections=await self._live_data.rejections(max_rows=max_rows),
                delivery=await self._live_data.delivery(),
                runtime=await self._live_data.runtime(),
                telegram=await self._live_data.telegram_preview(),
            )
            return audit_snapshot(snapshot)
        except RuntimeError as exc:
            if "shutdown" in str(exc).lower():
                LOG.debug("Dashboard endpoint called during shutdown: %s", exc)
                return {"error": "Server is shutting down"}
            raise
        except DEFENSIVE_EXC:
            LOG.exception("dashboard live audit error")
            return {"error": "live_audit_unavailable"}

    @app.get("/api/v1/market/rest-weight")
    async def rest_weight_budget() -> dict[str, Any]:
        try:
            client = getattr(self.bot, "client", None) or getattr(self.bot, "_market_data", None)
            snap: dict[str, Any] = {}
            if client is not None and hasattr(client, "state_snapshot"):
                snap = dict(client.state_snapshot())
            used = float(snap.get("rest_weight_1m") or 0.0)
            runtime_cfg = getattr(getattr(self.bot, "settings", None), "runtime", None)
            alert_pct = float(
                getattr(runtime_cfg, "dashboard_weight_alert_pct", 80.0) if runtime_cfg else 80.0
            )
            soft = float(REST_WEIGHT_SOFT_LIMIT)
            proxy_pool: dict[str, Any] = {}
            pool_fn = getattr(client, "proxy_pool_snapshot", None) if client is not None else None
            if callable(pool_fn):
                proxy_pool = dict(pool_fn())
            return {
                "used_weight_1m": used,
                "pace_limit": REST_WEIGHT_PACE_LIMIT,
                "soft_limit": soft,
                "hard_limit": REST_WEIGHT_HARD_LIMIT,
                "utilization_pct": round(used / soft * 100.0, 2) if used else 0.0,
                "alert": used >= soft * alert_pct / 100.0,
                "proxy_pool": proxy_pool,
                **snap,
            }
        except DEFENSIVE_EXC:
            LOG.exception("dashboard rest weight error")
            return {"error": "rest_weight_unavailable"}
