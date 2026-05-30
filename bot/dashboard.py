"""FastAPI dashboard for signal bot monitoring."""

from __future__ import annotations

import json
import logging
import webbrowser
import asyncio
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING, cast

from .dashboard_live import DashboardLiveData
from .live_audit import audit_snapshot, build_dashboard_audit_snapshot
from .ws_dashboard import DashboardWSBroadcaster
from .diary_store import DiaryStore

UTC = timezone.utc
_DASHBOARD_HTML = (Path(__file__).parent / "static" / "dashboard.html").read_text(
    encoding="utf-8"
)

if TYPE_CHECKING:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse
    HAS_FASTAPI = True
except ImportError:
    FastAPI = None
    WebSocket = None
    WebSocketDisconnect = None
    CORSMiddleware = None
    HTMLResponse = None
    HAS_FASTAPI = False

LOG = logging.getLogger("bot.dashboard")


class BotDashboard:
    """FastAPI dashboard bound to the current bot process."""

    def __init__(self, bot: Any, port: int = 8080, host: str = "127.0.0.1") -> None:
        self.bot = bot
        self.port = port
        self.host = host
        self._enabled = HAS_FASTAPI
        self.app: FastAPI | None = None
        self._strategies_cache: list[dict[str, Any]] | None = None
        self._analytics_cache: dict[
            tuple[str, int, str | None], tuple[float, dict[str, Any]]
        ] = {}
        self._decision_cache: dict[
            tuple[int, int], tuple[float, tuple[tuple[str, float], ...], dict[str, Any]]
        ] = {}
        self._live_data = DashboardLiveData(lambda: self.bot)
        self._diary_store: DiaryStore | None = None
        self._diary_init_lock = asyncio.Lock()
        self._ws_broadcaster: DashboardWSBroadcaster | None = None

        bus = getattr(self.bot, "_bus", None)
        if bus is not None:
            self._ws_broadcaster = DashboardWSBroadcaster(bus)
            self._ws_broadcaster.subscribe_to_bus()

        if not self._enabled:
            LOG.info("fastapi not installed, dashboard disabled")
            return

        app = FastAPI(title="Signal Bot Dashboard", version="2.0.0")

        # Security: Restrict CORS origins based on configuration
        origins = ["http://127.0.0.1", "http://localhost"]
        if hasattr(self.bot, "settings"):
            origins = list(self.bot.settings.runtime.dashboard_allow_origins)

        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=["Content-Type", "Authorization"],
        )

        @app.middleware("http")
        async def add_security_headers(request: Any, call_next: Any) -> Any:
            response = await call_next(request)
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["X-XSS-Protection"] = "1; mode=block"
            return response

        self.app = app
        self._mount_static()
        self._setup_routes()
        self._cache_strategies()

    def _mount_static(self) -> None:
        if not self.app:
            return
        try:
            from fastapi.staticfiles import StaticFiles
            static_dir = Path(__file__).parent / "static"
            if static_dir.exists():
                self.app.mount("/static", StaticFiles(directory=str(static_dir)), name="dashboard_static")
        except Exception:
            LOG.debug("failed to mount static files directory")

    def _setup_routes(self) -> None:
        if not self.app:
            return

        @self.app.get("/", response_class=HTMLResponse)
        async def root() -> str:
            return self._get_html_dashboard()

        @self.app.get("/api/status")
        async def status() -> dict[str, Any]:
            try:
                return await self._get_status()
            except Exception as exc:
                LOG.error("dashboard api status error: %s", exc)
                return {"error": "status_unavailable", "detail": str(exc)}

        @self.app.get("/api/signals/active")
        async def active_signals() -> list[dict[str, Any]]:
            try:
                return await self._get_active_signals()
            except Exception as exc:
                LOG.error("dashboard api active signals error: %s", exc)
                return []

        @self.app.get("/api/signals/recent")
        async def recent_signals(limit: int = 20) -> list[dict[str, Any]]:
            try:
                limit = max(1, min(int(limit), 100))
                return self._get_recent_signals(limit)
            except Exception as exc:
                LOG.error("dashboard api recent signals error: %s", exc)
                return []

        @self.app.get("/api/market/regime")
        async def market_regime() -> dict[str, Any]:
            try:
                return self._get_market_regime()
            except Exception as exc:
                LOG.error("dashboard api market regime error: %s", exc)
                return {"error": "regime_unavailable", "detail": str(exc)}

        @self.app.get("/api/metrics")
        async def metrics() -> dict[str, Any]:
            try:
                return await self._get_metrics()
            except Exception as exc:
                LOG.error("dashboard api metrics error: %s", exc)
                return {"error": "metrics_unavailable", "detail": str(exc)}

        @self.app.get("/api/health")
        async def health() -> dict[str, Any]:
            try:
                return cast(dict[str, Any], await self.bot.health_check())
            except Exception as exc:
                LOG.error("dashboard api health error: %s", exc)
                return {"status": "error", "detail": str(exc)}

        @self.app.get("/api/analytics/report")
        async def analytics_report(days: int = 30, scope: str = "current_run") -> dict[str, Any]:
            try:
                from .analytics import StrategyAnalytics
            except ImportError as exc:
                LOG.error("failed to import StrategyAnalytics: %s", exc)
                return {"error": "analytics_unavailable"}

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

        @self.app.get("/api/analytics/strategy-decisions")
        async def strategy_decisions(
            limit_files: int = 1,
            max_rows: int = 1_000,
        ) -> dict[str, Any]:
            try:
                return await asyncio.to_thread(
                    self._get_strategy_decision_summary,
                    limit_files=limit_files,
                    max_rows=max_rows,
                )
            except RuntimeError as exc:
                if "shutdown" in str(exc).lower():
                    LOG.debug("Dashboard endpoint called during shutdown: %s", exc)
                    return {"error": "Server is shutting down"}
                raise
            except Exception as exc:
                LOG.error("dashboard api strategy decisions error: %s", exc)
                return {"error": "strategy_decisions_unavailable", "detail": str(exc)}

        @self.app.get("/api/strategies")
        async def strategies() -> list[dict[str, Any]]:
            """Return cached list of strategies with their enabled status."""
            try:
                if self._strategies_cache is not None:
                    return self._strategies_cache
                return []
            except Exception as exc:
                LOG.error("dashboard api strategies error: %s", exc)
                return []

        @self.app.get("/api/live/overview")
        async def live_overview() -> dict[str, Any]:
            try:
                return await asyncio.to_thread(self._live_data.overview)
            except RuntimeError as exc:
                if "shutdown" in str(exc).lower():
                    LOG.debug("Dashboard endpoint called during shutdown: %s", exc)
                    return {"error": "Server is shutting down"}
                raise
            except Exception as exc:
                LOG.exception("dashboard live overview error")
                return {"error": "live_overview_unavailable", "detail": str(exc)}

        @self.app.get("/api/live/funnel")
        async def live_funnel(max_rows: int = 100_000) -> dict[str, Any]:
            try:
                max_rows = max(1_000, min(int(max_rows), 250_000))
                return await asyncio.to_thread(self._live_data.funnel, max_rows=max_rows)
            except RuntimeError as exc:
                if "shutdown" in str(exc).lower():
                    LOG.debug("Dashboard endpoint called during shutdown: %s", exc)
                    return {"error": "Server is shutting down"}
                raise
            except Exception as exc:
                LOG.exception("dashboard live funnel error")
                return {"error": "live_funnel_unavailable", "detail": str(exc)}

        @self.app.get("/api/live/shortlist")
        async def live_shortlist(limit: int = 80) -> dict[str, Any]:
            try:
                limit = max(1, min(int(limit), 200))
                return await asyncio.to_thread(self._live_data.shortlist, limit=limit)
            except RuntimeError as exc:
                if "shutdown" in str(exc).lower():
                    LOG.debug("Dashboard endpoint called during shutdown: %s", exc)
                    return {"error": "Server is shutting down"}
                raise
            except Exception as exc:
                LOG.exception("dashboard live shortlist error")
                return {"error": "live_shortlist_unavailable", "detail": str(exc)}

        @self.app.get("/api/live/rejections")
        async def live_rejections(
            limit: int = 30,
            max_rows: int = 100_000,
        ) -> dict[str, Any]:
            try:
                limit = max(1, min(int(limit), 100))
                max_rows = max(1_000, min(int(max_rows), 250_000))
                return await asyncio.to_thread(
                    self._live_data.rejections,
                    limit=limit,
                    max_rows=max_rows,
                )
            except RuntimeError as exc:
                if "shutdown" in str(exc).lower():
                    LOG.debug("Dashboard endpoint called during shutdown: %s", exc)
                    return {"error": "Server is shutting down"}
                raise
            except Exception as exc:
                LOG.exception("dashboard live rejections error")
                return {"error": "live_rejections_unavailable", "detail": str(exc)}

        @self.app.get("/api/live/decisions")
        async def live_decisions(
            limit: int = 40,
            max_rows: int = 100_000,
        ) -> dict[str, Any]:
            try:
                limit = max(1, min(int(limit), 100))
                max_rows = max(1_000, min(int(max_rows), 250_000))
                return await asyncio.to_thread(
                    self._live_data.decisions,
                    limit=limit,
                    max_rows=max_rows,
                )
            except RuntimeError as exc:
                if "shutdown" in str(exc).lower():
                    LOG.debug("Dashboard endpoint called during shutdown: %s", exc)
                    return {"error": "Server is shutting down"}
                raise
            except Exception as exc:
                LOG.exception("dashboard live decisions error")
                return {"error": "live_decisions_unavailable", "detail": str(exc)}

        @self.app.get("/api/live/runtime")
        async def live_runtime() -> dict[str, Any]:
            try:
                return await asyncio.to_thread(self._live_data.runtime)
            except RuntimeError as exc:
                if "shutdown" in str(exc).lower():
                    LOG.debug("Dashboard endpoint called during shutdown: %s", exc)
                    return {"error": "Server is shutting down"}
                raise
            except Exception as exc:
                LOG.exception("dashboard live runtime error")
                return {"error": "live_runtime_unavailable", "detail": str(exc)}

        @self.app.get("/api/live/delivery")
        async def live_delivery(limit: int = 25) -> dict[str, Any]:
            try:
                limit = max(1, min(int(limit), 100))
                return await asyncio.to_thread(self._live_data.delivery, limit=limit)
            except RuntimeError as exc:
                if "shutdown" in str(exc).lower():
                    LOG.debug("Dashboard endpoint called during shutdown: %s", exc)
                    return {"error": "Server is shutting down"}
                raise
            except Exception as exc:
                LOG.exception("dashboard live delivery error")
                return {"error": "live_delivery_unavailable", "detail": str(exc)}

        @self.app.get("/api/live/telegram-preview")
        async def live_telegram_preview() -> dict[str, Any]:
            try:
                return await asyncio.to_thread(self._live_data.telegram_preview)
            except RuntimeError as exc:
                if "shutdown" in str(exc).lower():
                    LOG.debug("Dashboard endpoint called during shutdown: %s", exc)
                    return {"error": "Server is shutting down"}
                raise
            except Exception as exc:
                LOG.exception("dashboard live telegram preview error")
                return {"error": "live_telegram_preview_unavailable", "detail": str(exc)}

        # ── WebSocket endpoint ──────────────────────────────────────────
        @self.app.websocket("/api/v1/ws")
        async def dashboard_ws(ws: WebSocket) -> None:
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
            limit: int = 20, symbol: str = "", setup_id: str = "",
        ) -> list[dict[str, Any]]:
            signals = self._get_recent_signals(limit=max(1, min(int(limit), 100)))
            if symbol:
                signals = [s for s in signals if str(s.get("symbol", "")).upper() == symbol.upper()]
            if setup_id:
                signals = [s for s in signals if str(s.get("setup_id", "")) == setup_id]
            return signals

        @self.app.get("/api/v1/signals/active")
        async def v1_signals_active() -> list[dict[str, Any]]:
            try:
                return await self._get_active_signals()
            except Exception as exc:
                LOG.error("v1 active signals error: %s", exc)
                return []

        @self.app.get("/api/v1/signals/live")
        async def v1_signals_live(limit: int = 20) -> list[dict[str, Any]]:
            try:
                return self._get_live_signal_feed(max(1, min(int(limit), 100)))
            except Exception as exc:
                LOG.error("v1 live signals error: %s", exc)
                return []

        @self.app.get("/api/v1/strategies/health")
        async def v1_strategies_health() -> list[dict[str, Any]]:
            cached = self._strategies_cache or []
            for item in cached:
                item["status"] = item.get("status", "beta")
            return cached

        @self.app.get("/api/v1/market/regime")
        async def v1_market_regime() -> dict[str, Any]:
            try:
                return self._get_market_regime()
            except Exception as exc:
                LOG.error("v1 market regime error: %s", exc)
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
        @self.app.get("/api/v1/strategies/correlation")
        async def v1_strategies_correlation(limit: int = 41) -> dict[str, Any]:
            catalog = self._strategies_cache or []
            setup_ids = [s["id"] for s in catalog[:max(10, min(limit, 41))]]
            return {"strategies": setup_ids, "matrix": []}

        @self.app.get("/api/v1/analytics/confluence-heatmap")
        async def v1_confluence_heatmap() -> list[dict[str, Any]]:
            decisions = self._live_data.decisions(limit=50, max_rows=50000)
            return decisions.get("setup_reports", [])

        @self.app.post("/api/v1/confluence/simulate")
        async def v1_confluence_simulate(body: dict[str, Any]) -> dict[str, Any]:
            return {
                "simulated": True,
                "weights": body.get("weights", {}),
                "disabled_setups": body.get("disabled_setups", []),
                "note": "What-if simulation endpoint ready. Full engine integration pending.",
            }

        @self.app.get("/api/v1/confluence/distribution")
        async def v1_confluence_distribution(hours: int = 24) -> dict[str, Any]:
            return {"hours": max(1, min(hours, 168)), "buckets": []}

        @self.app.get("/api/v1/confluence/vetos")
        async def v1_confluence_vetos(limit: int = 50) -> list[dict[str, Any]]:
            rejections = self._live_data.rejections(limit=max(1, min(limit, 100)), max_rows=50000)
            return rejections.get("reasons", [])

        # ── Config endpoints ────────────────────────────────────────────
        @self.app.get("/api/v1/config/strategies")
        async def v1_config_strategies() -> list[dict[str, Any]]:
            return self._strategies_cache or []

        @self.app.patch("/api/v1/config/strategies")
        async def v1_config_strategies_patch(body: dict[str, Any]) -> dict[str, Any]:
            LOG.info("config strategies patch received: %s", body)
            return {"applied": True, "updates": body.get("updates", {})}

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
        async def v1_config_scoring_patch(body: dict[str, Any]) -> dict[str, Any]:
            LOG.info("config scoring patch received: %s", body)
            return {"applied": True, "weights": body.get("weights", {})}

        @self.app.get("/api/v1/config/killzone")
        async def v1_config_killzone() -> dict[str, Any]:
            return {
                "london": {"start": "08:00", "end": "17:00", "utc": 0},
                "ny": {"start": "13:00", "end": "22:00", "utc": 0},
                "asia": {"start": "00:00", "end": "09:00", "utc": 0},
            }

        @self.app.patch("/api/v1/config/killzone")
        async def v1_config_killzone_patch(body: dict[str, Any]) -> dict[str, Any]:
            LOG.info("config killzone patch received: %s", body)
            return {"applied": True, **body}

        # ── Alerts ──────────────────────────────────────────────────────
        @self.app.get("/api/v1/alerts")
        async def v1_alerts(limit: int = 50, since: str = "") -> list[dict[str, Any]]:
            try:
                settings = getattr(self.bot, "settings", None)
                if settings is None:
                    return []
                telemetry_dir = getattr(settings, "telemetry_dir", None)
                if telemetry_dir is None:
                    return []
                runs_dir = Path(telemetry_dir) / "runs"
                if not runs_dir.exists():
                    return []
                candidates = sorted(
                    runs_dir.glob("*/analysis/alerts*.jsonl"),
                    key=lambda p: p.stat().st_mtime, reverse=True,
                )
                if not candidates:
                    return []
                rows = self._read_recent_jsonl(candidates[0], limit=max(1, min(limit, 200)))
                return rows
            except Exception as exc:
                LOG.debug("alerts fetch error: %s", exc)
                return []

        # ── Sandbox ─────────────────────────────────────────────────────
        @self.app.post("/api/v1/sandbox/replay")
        async def v1_sandbox_replay(body: dict[str, Any]) -> dict[str, Any]:
            hours = int(body.get("hours", 24))
            return {
                "job_id": f"sim_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}",
                "status": "queued",
                "hours": max(1, min(hours, 168)),
                "disabled_setups": body.get("disabled_setups", []),
                "weights": body.get("weights", {}),
                "note": "Sandbox replay endpoint ready. Full backtest engine pending.",
            }

        @self.app.get("/api/v1/sandbox/result/{job_id}")
        async def v1_sandbox_result(job_id: str) -> dict[str, Any]:
            return {"job_id": job_id, "status": "pending", "progress": 0}

        @self.app.get("/api/live/audit")
        async def live_audit(max_rows: int = 20_000) -> dict[str, Any]:
            try:
                max_rows = max(1_000, min(int(max_rows), 50_000))

                def _build() -> dict[str, Any]:
                    snapshot = build_dashboard_audit_snapshot(
                        overview=self._live_data.overview(),
                        funnel=self._live_data.funnel(max_rows=max_rows),
                        shortlist=self._live_data.shortlist(),
                        decisions=self._live_data.decisions(max_rows=max_rows),
                        rejections=self._live_data.rejections(max_rows=max_rows),
                        delivery=self._live_data.delivery(),
                        runtime=self._live_data.runtime(),
                        telegram=self._live_data.telegram_preview(),
                    )
                    return audit_snapshot(snapshot)

                return await asyncio.to_thread(_build)
            except RuntimeError as exc:
                if "shutdown" in str(exc).lower():
                    LOG.debug("Dashboard endpoint called during shutdown: %s", exc)
                    return {"error": "Server is shutting down"}
                raise
            except Exception as exc:
                LOG.exception("dashboard live audit error")
                return {"error": "live_audit_unavailable", "detail": str(exc)}

    def _cache_strategies(self) -> None:
        """Pre-load and cache strategies at startup."""
        try:
            from .strategies import STRATEGY_CLASSES
            from .domain.strategies import RISK_PROFILE_BY_ID, STRATEGY_STATUS_BY_ID

            settings = getattr(self.bot, "settings", None)
            setups = getattr(settings, "setups", None)
            if setups is not None and hasattr(setups, "enabled_setup_ids"):
                enabled_setups = set(setups.enabled_setup_ids())
            else:
                enabled_setups = {
                    key
                    for key in dir(setups or object())
                    if not key.startswith("_") and bool(getattr(setups, key, False))
                }

            self._strategies_cache = []
            for cls in STRATEGY_CLASSES:
                setup_id = str(getattr(cls, "setup_id", "") or cls.__name__)
                self._strategies_cache.append(
                    {
                        "id": setup_id,
                        "name": cls.__name__,
                        "enabled": setup_id in enabled_setups,
                        "status": STRATEGY_STATUS_BY_ID.get(setup_id, "beta"),
                        "risk_profile": RISK_PROFILE_BY_ID.get(
                            setup_id, str(getattr(cls, "family", "generic"))
                        ),
                        "family": str(getattr(cls, "family", "generic")),
                    }
                )
            LOG.info("dashboard cached %d strategies", len(self._strategies_cache))
        except Exception:
            LOG.exception("failed to cache strategies")
            self._strategies_cache = []

    @staticmethod
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

    def _merge_strategy_catalog(
        self,
        report: dict[str, Any],
        *,
        decision_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Attach every registered strategy to analytics, including zero-outcome rows."""
        catalog = self._strategies_cache or []
        by_setup = report.get("by_setup") or {}
        decision_setups = (decision_summary or {}).get("setups") or {}

        enabled_count = 0
        for item in catalog:
            setup_id = str(item.get("id") or "")
            if not setup_id:
                continue

            is_enabled = bool(item.get("enabled"))
            if is_enabled:
                enabled_count += 1

            if setup_id not in by_setup:
                by_setup[setup_id] = {
                    "setup_id": setup_id,
                    "trades": 0,
                    "count": 0,
                    "win_rate": 0.0,
                    "expectancy_r": 0.0,
                    "avg_rr": 0.0,
                    "outcomes": 0,
                    "signals_seen": 0,
                    "pending_signals": 0,
                    "active_signals": 0,
                    "closed_signals": 0,
                    "closed_missing_outcomes": 0,
                    "profit_factor": None,
                    "max_drawdown_r": 0.0,
                }

            row = by_setup[setup_id]
            decision_row = decision_setups.get(setup_id) or {}
            row["detector_runs"] = int(decision_row.get("total") or 0)
            row["detector_hits"] = int(decision_row.get("signals") or 0)
            row["detector_signal_rate"] = float(decision_row.get("signal_rate") or 0.0)
            blockers = decision_row.get("top_blockers") or []
            row["top_detector_blocker"] = (
                blockers[0].get("reason") if blockers and isinstance(blockers[0], dict) else None
            )
            row["enabled"] = is_enabled
            catalog_status = str(item.get("status", "beta") or "beta")
            row["catalog_status"] = catalog_status
            row["status"] = self._runtime_strategy_status(row, catalog_status)
            row["risk_profile"] = item.get("risk_profile", "generic")
            row["family"] = item.get("family", "generic")

        setup_reports = sorted(
            by_setup.values(),
            key=lambda row: (
                not bool(row.get("enabled", True)),
                -int(row.get("trades") or 0),
                str(row.get("status") or ""),
                str(row.get("setup_id") or ""),
            ),
        )
        report["by_setup"] = by_setup
        report["setup_reports"] = setup_reports
        report["registered_strategies"] = len(catalog)
        report["enabled_strategies"] = enabled_count
        report["decision_summary"] = decision_summary or {}
        return report

    async def _ensure_diary_store(self) -> DiaryStore | None:
        if self._diary_store is not None:
            return self._diary_store
        async with self._diary_init_lock:
            if self._diary_store is not None:
                return self._diary_store
            try:
                settings = getattr(self.bot, "settings", None)
                if settings is None:
                    return None
                store = DiaryStore(settings.db_path)
                await store.initialize()
                self._diary_store = store
                return store
            except Exception as exc:
                LOG.error("failed to initialize diary store: %s", exc)
                return None

    @staticmethod
    def _compute_killzone() -> dict[str, bool]:
        now = datetime.now(UTC)
        hour = now.hour + now.minute / 60.0
        return {
            "london": 8 <= hour < 17,
            "ny": 13 <= hour < 22,
            "asia": 0 <= hour < 9,
        }

    @staticmethod
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

    def _get_live_signal_feed(self, limit: int = 20) -> list[dict[str, Any]]:
        signals = self._get_recent_signals(limit=limit * 2)
        killzone = self._compute_killzone()
        regime_data = self._get_market_regime()
        regime_label = regime_data.get("regime", "unknown") if isinstance(regime_data, dict) else "unknown"
        out = []
        for sig in signals[:limit]:
            score = float(sig.get("score") or sig.get("confluence_score") or 0.0)
            enriched = dict(sig)
            enriched["confluence_score"] = round(score, 1)
            enriched["confluence_color"] = self._confluence_color(score)
            enriched["killzone"] = killzone
            enriched["market_regime"] = regime_label
            enriched["active_strategies"] = [
                {"id": str(sig.get("setup_id", "unknown")), "family": "generic"},
            ]
            enriched["ttl_seconds"] = 3600
            out.append(enriched)
        return out

    def _get_html_dashboard(self) -> str:
        return _DASHBOARD_HTML

    def _current_run_id(self) -> str | None:
        telemetry = getattr(self.bot, "telemetry", None)
        run_id = getattr(telemetry, "run_id", None)
        return str(run_id) if run_id else None

    def _current_run_started_at(self) -> datetime | None:
        telemetry = getattr(self.bot, "telemetry", None)
        started_at = getattr(telemetry, "started_at", None)
        parsed = self._parse_datetime(started_at)
        if parsed is not None:
            return parsed

        run_id = self._current_run_id()
        settings = getattr(self.bot, "settings", None)
        telemetry_dir = getattr(settings, "telemetry_dir", None)
        if run_id and telemetry_dir:
            metadata_path = Path(telemetry_dir) / "runs" / run_id / "run_metadata.json"
            try:
                payload = json.loads(metadata_path.read_text(encoding="utf-8"))
                parsed = self._parse_datetime(payload.get("started_at"))
                if parsed is not None:
                    return parsed
            except (OSError, json.JSONDecodeError):
                pass
            try:
                stamp = "_".join(run_id.split("_")[:2])
                return datetime.strptime(stamp, "%Y%m%d_%H%M%S").replace(tzinfo=UTC)
            except (ValueError, TypeError):
                return None
        return None

    def _analytics_since(self, scope: str, *, days: int) -> datetime | None:
        if scope == "current_run":
            return self._current_run_started_at()
        if scope == "all":
            return None
        return datetime.now(UTC) - timedelta(days=max(1, int(days)))

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    async def _get_status(self) -> dict[str, Any]:
        bot = self.bot
        if bot is None:
            return {"error": "bot_not_found"}

        regime = getattr(bot.market_regime, "_last_result", None)

        ws_lag = 0
        if getattr(bot, "_ws_manager", None) is not None:
            stats = bot._ws_manager.get_stats()
            ws_lag = stats.get("avg_latency_overall_ms", 0) or 0

        # Quick count without full fetch
        open_signals_count = 0
        market_context: dict[str, Any] = {}
        try:
            stats = await asyncio.wait_for(bot._modern_repo.get_tracking_stats(), timeout=1.0)
            open_signals_count = stats.get("active", 0)
        except Exception as exc:
            LOG.debug("dashboard status tracking stats unavailable: %s", exc)
        try:
            get_market_context = getattr(bot._modern_repo, "get_market_context", None)
            if callable(get_market_context):
                market_context = await asyncio.wait_for(get_market_context(), timeout=1.0)
        except Exception as exc:
            LOG.debug("dashboard status market context unavailable: %s", exc)
            market_context = {}

        last_cycle = getattr(bot, "last_cycle_summary", {}) or {}
        delivery_counts = last_cycle.get("delivery_status_counts", {})
        if not isinstance(delivery_counts, dict):
            delivery_counts = {}
        selected_count = int(last_cycle.get("selected_signals", last_cycle.get("selected", 0)) or 0)
        delivered_count = int(
            last_cycle.get("delivered_signals", last_cycle.get("delivered", 0)) or 0
        )
        notifiers = getattr(getattr(bot, "settings", None), "notifiers", None)
        notifier_provider = str(getattr(notifiers, "provider", "unknown"))

        return {
            "running": not bot._shutdown.is_set() if hasattr(bot, "_shutdown") else False,
            "shortlist_size": len(getattr(bot, "_shortlist", [])),
            "open_signals": open_signals_count,
            "ws_latency_ms": ws_lag,
            "market_regime": getattr(regime, "regime", "unknown") if regime else "unknown",
            "market_strength": getattr(regime, "strength", 0.0) if regime else 0.0,
            "btc_bias": market_context.get("btc_bias", "neutral"),
            "delivery_provider": notifier_provider,
            "last_cycle_selected": selected_count,
            "last_cycle_delivered": delivered_count,
            "last_cycle_delivery_status_counts": delivery_counts,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    async def _get_active_signals(self) -> list[dict[str, Any]]:
        bot = self.bot
        if bot is None:
            return []

        repo = getattr(bot, "_modern_repo", None)
        if repo is None:
            return []

        try:
            # Use timeout to prevent blocking dashboard
            signals = await asyncio.wait_for(repo.get_active_signals(), timeout=2.0)
            return [
                {
                    "symbol": sig.get("symbol"),
                    "setup_id": sig.get("setup_id"),
                    "direction": sig.get("direction"),
                    "entry_price": sig.get("activation_price")
                    or sig.get("entry_price")
                    or sig.get("entry_mid"),
                    "stop_price": sig.get("stop_price") or sig.get("stop"),
                    "tp1_price": sig.get("tp1_price") or sig.get("take_profit_1"),
                    "tp2_price": sig.get("tp2_price") or sig.get("take_profit_2"),
                    "score": sig.get("score"),
                    "risk_reward": sig.get("risk_reward"),
                    "status": sig.get("status"),
                    "tracking_id": sig.get("tracking_id"),
                    "tracking_ref": sig.get("tracking_ref"),
                    "timestamp": sig.get("activated_at") or sig.get("created_at"),
                }
                for sig in signals
            ]
        except asyncio.TimeoutError:
            LOG.debug("timeout fetching active signals for dashboard")
            return []
        except Exception as exc:
            LOG.debug("error fetching active signals: %s", exc)
            return []

    def _get_recent_signals(self, limit: int = 20) -> list[dict[str, Any]]:
        bot = self.bot
        if bot is None or not hasattr(bot, "settings"):
            return []

        telemetry_dir = bot.settings.telemetry_dir
        selected_file = self._latest_analysis_file(telemetry_dir, "selected.jsonl")
        selected_rows = self._read_recent_jsonl(selected_file, limit=limit * 4)
        if selected_rows:
            out: list[dict[str, Any]] = []
            for row in selected_rows[:limit]:
                enriched = dict(row)
                enriched["source"] = "selected"
                enriched["delivery_status"] = "sent"
                out.append(enriched)
            return out

        delivery_file = self._latest_analysis_file(telemetry_dir, "delivery.jsonl")
        delivery_rows = self._read_recent_jsonl(delivery_file, limit=limit * 4)
        if delivery_rows:
            out = []
            for row in delivery_rows[:limit]:
                enriched = dict(row)
                enriched["source"] = "delivery"
                enriched["delivery_status"] = enriched.get("delivery_status") or enriched.get(
                    "status", "unknown"
                )
                out.append(enriched)
            return out

        candidates_file = self._latest_analysis_file(telemetry_dir, "candidates.jsonl")
        candidates = self._read_recent_jsonl(candidates_file, limit=limit * 80)
        aggregated = self._aggregate_recent_candidates(candidates, limit=limit)
        if aggregated:
            return aggregated

        decisions = self._live_data.decisions(limit=max(limit, 20), max_rows=50_000)
        rows: list[dict[str, Any]] = []
        for row in decisions.get("setup_reports", []):
            if int(row.get("signals") or 0) <= 0:
                continue
            rows.append(
                {
                    "symbol": "multi-symbol",
                    "setup_id": row.get("setup_id"),
                    "direction": "mixed",
                    "score": row.get("signal_rate"),
                    "source": "strategy_decisions",
                    "delivery_status": "raw_detector_signal",
                    "confluence_count": row.get("signals"),
                    "ts": decisions.get("generated_at"),
                }
            )
            if len(rows) >= limit:
                break
        return rows

    def _read_recent_jsonl(self, path: Path | None, *, limit: int) -> list[dict[str, Any]]:
        if path is None or limit <= 0:
            return []
        rows: list[dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                tail = deque(handle, maxlen=limit)
            for line in reversed(tail):
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except Exception as exc:
            LOG.debug("failed to read telemetry file %s: %s", path, exc)
        return rows

    def _aggregate_recent_candidates(
        self, rows: list[dict[str, Any]], *, limit: int
    ) -> list[dict[str, Any]]:
        if not rows:
            return []

        groups: dict[tuple[str, str, str, float, str], dict[str, Any]] = {}
        ordered_keys: list[tuple[str, str, str, float, str]] = []
        for row in rows:
            symbol = str(row.get("symbol") or "").strip().upper()
            direction = str(row.get("direction") or "").strip().lower()
            timeframe = str(row.get("timeframe") or "").strip().lower()
            ts_value = str(row.get("ts") or row.get("created_at") or "")
            ts_bucket = ts_value[:16] if len(ts_value) >= 16 else ts_value
            price_raw = row.get("entry_reference_price")
            if price_raw is None:
                price_raw = row.get("entry_mid")
            try:
                price = round(float(price_raw), 3) if price_raw is not None else 0.0
            except (TypeError, ValueError):
                price = 0.0
            key = (symbol, direction, timeframe, price, ts_bucket)

            grouped = groups.get(key)
            if grouped is None:
                base = dict(row)
                base["confluence_setups"] = [str(row.get("setup_id") or "")]
                base["source"] = "candidate_aggregate"
                base["delivery_status"] = "candidate"
                groups[key] = base
                ordered_keys.append(key)
                continue

            setup_id = str(row.get("setup_id") or "")
            setups = grouped.setdefault("confluence_setups", [])
            if setup_id and setup_id not in setups:
                setups.append(setup_id)
            try:
                row_score = float(row.get("score") or 0.0)
                current_score = float(grouped.get("score") or 0.0)
            except (TypeError, ValueError):
                row_score = 0.0
                current_score = 0.0
            if row_score > current_score:
                for field_name in (
                    "setup_id",
                    "score",
                    "reasons",
                    "tracking_id",
                    "tracking_ref",
                ):
                    if field_name in row:
                        grouped[field_name] = row[field_name]

        out: list[dict[str, Any]] = []
        for key in ordered_keys:
            row = groups[key]
            setups = [item for item in row.get("confluence_setups", []) if item]
            row["confluence_setups"] = setups
            row["confluence_count"] = len(setups)
            out.append(row)
            if len(out) >= limit:
                break
        return out

    def _latest_analysis_files(
        self,
        telemetry_dir: Path,
        filename: str,
        *,
        limit: int,
    ) -> list[Path]:
        runs_dir = telemetry_dir / "runs"
        if not runs_dir.exists():
            return []
        stem = filename.removesuffix(".jsonl")
        current_run_id = self._current_run_id()
        current_file = (
            runs_dir / current_run_id / "analysis" / filename if current_run_id else None
        )
        files = sorted(
            runs_dir.glob(f"*/analysis/{stem}*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if current_file is not None and current_file.exists():
            files = [current_file, *[path for path in files if path != current_file]]
        return files[: max(1, int(limit))]

    def _get_strategy_decision_summary(
        self,
        *,
        limit_files: int = 1,
        max_rows: int = 1_000,
    ) -> dict[str, Any]:
        bot = self.bot
        if bot is None or not hasattr(bot, "settings"):
            return {
                "files": [],
                "total_rows": 0,
                "status_counts": {},
                "reason_family_counts": {},
                "blocker_counts": {},
                "setups": {},
                "setup_reports": [],
            }

        limit_files = max(1, min(int(limit_files), 24))
        max_rows = max(1, min(int(max_rows), 100_000))
        files = self._latest_analysis_files(
            bot.settings.telemetry_dir,
            "strategy_decisions.jsonl",
            limit=limit_files,
        )
        fingerprint: tuple[tuple[str, float], ...] = tuple(
            (str(path), path.stat().st_mtime) for path in files if path.exists()
        )
        cache_key = (limit_files, max_rows)
        now = time.monotonic()
        cached = self._decision_cache.get(cache_key)
        if cached and (now - cached[0]) < 15.0 and cached[1] == fingerprint:
            return cached[2]

        status_counts: Counter[str] = Counter()
        reason_family_counts: Counter[str] = Counter()
        blocker_counts: Counter[str] = Counter()
        setup_counters: dict[str, dict[str, Counter[str]]] = {}
        total_rows = 0

        for path in files:
            if total_rows >= max_rows:
                break
            try:
                rows = self._read_recent_jsonl(path, limit=max_rows - total_rows)
                for row in rows:
                    setup_id = str(row.get("setup_id") or "unknown")
                    status = str(row.get("status") or "unknown")
                    reason = str(row.get("reason_code") or row.get("reason") or "unknown")
                    reason_family = str(row.get("reason_family") or reason.split(".", 1)[0])
                    counters = setup_counters.setdefault(
                        setup_id,
                        {
                            "status": Counter(),
                            "reason": Counter(),
                            "blocker": Counter(),
                            "reason_family": Counter(),
                        },
                    )
                    counters["status"][status] += 1
                    counters["reason"][reason] += 1
                    counters["reason_family"][reason_family] += 1
                    if status != "signal":
                        counters["blocker"][reason] += 1
                        blocker_counts[reason] += 1
                    status_counts[status] += 1
                    reason_family_counts[reason_family] += 1
                    total_rows += 1
            except OSError as exc:
                LOG.debug("failed to read strategy decision telemetry %s: %s", path, exc)

        setups: dict[str, dict[str, Any]] = {}
        for setup_id, counters in setup_counters.items():
            total = sum(counters["status"].values())
            signals = counters["status"].get("signal", 0)
            top_reasons = [
                {"reason": reason, "count": count}
                for reason, count in sorted(
                    counters["reason"].items(),
                    key=lambda item: (-item[1], item[0]),
                )[:5]
            ]
            top_blockers = [
                {"reason": reason, "count": count}
                for reason, count in sorted(
                    counters["blocker"].items(),
                    key=lambda item: (-item[1], item[0]),
                )[:5]
            ]
            setups[setup_id] = {
                "setup_id": setup_id,
                "total": total,
                "signals": signals,
                "signal_rate": round(signals / total, 6) if total else 0.0,
                "status_counts": dict(counters["status"]),
                "reason_family_counts": dict(counters["reason_family"]),
                "top_reasons": top_reasons,
                "top_blockers": top_blockers,
            }

        setup_reports = sorted(
            setups.values(),
            key=lambda row: (
                -int(row.get("total") or 0),
                float(row.get("signal_rate") or 0.0),
                str(row.get("setup_id") or ""),
            ),
        )
        summary = {
            "files": [str(path) for path in files],
            "file_count": len(files),
            "total_rows": total_rows,
            "max_rows": max_rows,
            "limit_files": limit_files,
            "run_id": self._current_run_id(),
            "generated_at": datetime.now(UTC).isoformat(),
            "status_counts": dict(status_counts),
            "reason_family_counts": dict(reason_family_counts),
            "blocker_counts": dict(blocker_counts),
            "setups": setups,
            "setup_reports": setup_reports,
        }
        self._decision_cache[cache_key] = (now, fingerprint, summary)
        return summary

    def _get_market_regime(self) -> dict[str, Any]:
        bot = self.bot
        if bot is None:
            return {"error": "bot_not_found"}

        regime = getattr(bot.market_regime, "_last_result", None)
        if not regime:
            return {"error": "No market data available"}
        return cast(dict[str, Any], regime.to_dict())

    async def _get_metrics(self) -> dict[str, Any]:
        bot = self.bot
        if bot is None:
            return {"error": "bot_not_found"}

        # Get signal count with timeout
        open_signals_count = 0
        try:
            stats = await asyncio.wait_for(bot._modern_repo.get_tracking_stats(), timeout=1.0)
            open_signals_count = stats.get("active", 0)
        except Exception as exc:
            LOG.debug("dashboard metrics tracking stats unavailable: %s", exc)

        # Get market regime safely
        regime_data = None
        try:
            regime = getattr(bot.market_regime, "_last_result", None)
            if regime:
                regime_data = regime.to_dict()
        except Exception as exc:
            LOG.debug("dashboard metrics market regime unavailable: %s", exc)

        # Get engine stats safely
        engine_stats = {}
        try:
            engine = getattr(bot, "_modern_engine", None)
            if engine:
                engine_stats = engine.get_engine_stats()
        except Exception as exc:
            LOG.debug("dashboard metrics engine stats unavailable: %s", exc)

        return {
            "shortlist_size": len(getattr(bot, "_shortlist", [])),
            "open_signals": open_signals_count,
            "ws_streams": len(bot._ws_manager._symbols) if getattr(bot, "_ws_manager", None) else 0,
            "market_regime": regime_data,
            "engine": engine_stats,
        }

    def _latest_analysis_file(self, telemetry_dir: Path, filename: str) -> Path | None:
        runs_dir = telemetry_dir / "runs"
        if not runs_dir.exists():
            return None
        candidates = sorted(
            runs_dir.glob(f"*/analysis/{filename}"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        return candidates[0] if candidates else None

    def start_server(self, *, auto_open: bool = True, delay_seconds: float = 1.5) -> None:
        if not self._enabled or not self.app:
            LOG.debug("dashboard server disabled (fastapi not installed)")
            return

        from threading import Thread

        def run_server() -> None:
            if self.app is None:
                return
            try:
                import uvicorn
            except Exception as exc:
                LOG.error("dashboard server failed to import uvicorn: %s", exc)
                return
            try:
                uvicorn.run(self.app, host=self.host, port=self.port, log_level="warning")
            except Exception:
                LOG.exception("dashboard server crashed")

        thread = Thread(target=run_server, daemon=True)
        thread.start()
        LOG.info("dashboard server started on port %d", self.port)

        if auto_open:
            self._schedule_browser_open(delay_seconds)

    def _schedule_browser_open(self, delay_seconds: float) -> None:
        """Open browser after server is ready."""
        import threading
        import time

        def open_browser() -> None:
            time.sleep(delay_seconds)
            url = f"http://localhost:{self.port}"
            try:
                webbrowser.open(url, new=2)  # new=2 opens in new tab
                LOG.info("opened dashboard in browser: %s", url)
            except Exception as exc:
                LOG.debug("failed to open browser: %s", exc)
                LOG.info("dashboard available at: %s", url)

        threading.Thread(target=open_browser, daemon=True).start()
