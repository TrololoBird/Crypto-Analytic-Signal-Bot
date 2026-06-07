"""FastAPI dashboard for signal bot monitoring."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
import webbrowser
from collections import Counter, deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Thread
from typing import TYPE_CHECKING, Any, cast

import polars as pl

from bot.runtime.errors import DEFENSIVE_EXC
from bot.strategies import STRATEGY_CLASSES

from ..domain.strategies import RISK_PROFILE_BY_ID, STRATEGY_STATUS_BY_ID
from ..persistence.diary_store import DiaryStore
from .access_audit import DashboardAccessAuditor, client_ip_from_request
from .live import DashboardLiveData, _is_routing_excluded_decision_reason
from .routes_setup import register_routes
from .tracking_view import serialize_tracking_signal
from .ws_broadcast import DashboardWSBroadcaster

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_DASHBOARD_HTML = (_STATIC_DIR / "dashboard.html").read_text(encoding="utf-8")

if TYPE_CHECKING:
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

try:
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    from fastapi.staticfiles import StaticFiles

    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False
    StaticFiles = None  # type: ignore[misc, assignment]
    JSONResponse = None  # type: ignore[misc, assignment]

try:
    import uvicorn

    HAS_UVICORN = True
except ImportError:
    HAS_UVICORN = False
    uvicorn = None  # type: ignore[assignment]

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
        self._analytics_cache: dict[tuple[str, int, str | None], tuple[float, dict[str, Any]]] = {}
        self._decision_cache: dict[
            tuple[int, int], tuple[float, tuple[tuple[str, float], ...], dict[str, Any]]
        ] = {}
        self._live_data = DashboardLiveData(lambda: self.bot)
        self._diary_store: DiaryStore | None = None
        self._diary_init_lock = asyncio.Lock()
        self._ws_broadcaster: DashboardWSBroadcaster | None = None
        self._uvicorn_server: Any = None
        self._server_task: asyncio.Task[None] | None = None
        self._dashboard_token: str | None = None

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

        _open_paths = {"/api/health", "/api/status"}
        runtime_settings = getattr(self.bot, "settings", None)
        runtime_cfg = getattr(runtime_settings, "runtime", None)
        rate_limit = int(getattr(runtime_cfg, "dashboard_rate_limit_per_minute", 120) or 120)
        audit_enabled = bool(getattr(runtime_cfg, "dashboard_audit_log_enabled", True))
        telemetry_dir = getattr(runtime_settings, "telemetry_dir", None)
        audit_path = (
            Path(telemetry_dir) / "dashboard_access.jsonl" if telemetry_dir is not None else None
        )
        self._access_auditor = DashboardAccessAuditor(
            limit_per_minute=rate_limit,
            log_path=audit_path,
            enabled=audit_enabled,
        )

        @app.middleware("http")
        async def dashboard_rate_limit_and_audit(request: Any, call_next: Any) -> Any:
            path: str = request.url.path
            if path in _open_paths or path.startswith("/static/"):
                return await call_next(request)
            client_ip = client_ip_from_request(request)
            if not self._access_auditor.check_rate_limit(client_ip):
                self._access_auditor.record_access(
                    client_ip=client_ip,
                    method=str(request.method),
                    path=path,
                    status_code=429,
                    blocked=True,
                )
                if JSONResponse is not None:
                    return JSONResponse({"detail": "rate_limit_exceeded"}, status_code=429)
            response = await call_next(request)
            self._access_auditor.record_access(
                client_ip=client_ip,
                method=str(request.method),
                path=path,
                status_code=int(getattr(response, "status_code", 200) or 200),
            )
            return response

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
            static_dir = _STATIC_DIR
            if StaticFiles is not None and static_dir.exists():
                self.app.mount(
                    "/static", StaticFiles(directory=str(static_dir)), name="dashboard_static"
                )
        except DEFENSIVE_EXC:
            LOG.debug("failed to mount static files directory")

    def _setup_routes(self) -> None:
        register_routes(self)

    async def _public_audit_manifest(self) -> dict[str, Any]:
        ledger = getattr(self.bot, "public_audit", None)
        if ledger is None:
            return {"enabled": False, "files": []}
        return await asyncio.to_thread(ledger.latest_manifest)

    def _cache_strategies(self) -> None:
        """Pre-load and cache strategies at startup."""
        try:
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
        except DEFENSIVE_EXC:
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
            except DEFENSIVE_EXC:
                LOG.exception("failed to initialize diary store")
                return None
            else:
                return store

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
        regime_label = (
            regime_data.get("regime", "unknown") if isinstance(regime_data, dict) else "unknown"
        )
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

    def notify_signal_delivered(self, signal: Any) -> None:
        """Push a delivered signal to dashboard WebSocket clients."""
        if self._ws_broadcaster is None:
            return
        payload = self._signal_to_ws_payload(signal)
        if payload:
            self._ws_broadcaster.publish_signal(payload)

    def notify_tracking_changed(self, *, event_count: int = 1) -> None:
        if self._ws_broadcaster is None:
            return
        self._ws_broadcaster.publish_tracking_update({"event_count": event_count})

    @staticmethod
    def _signal_to_ws_payload(signal: Any) -> dict[str, Any] | None:
        tracking_id = getattr(signal, "tracking_id", None)
        if not tracking_id:
            return None
        score = float(getattr(signal, "score", 0.0) or 0.0)
        return {
            "signal_id": tracking_id,
            "tracking_id": tracking_id,
            "symbol": getattr(signal, "symbol", ""),
            "setup_id": getattr(signal, "setup_id", ""),
            "direction": getattr(signal, "direction", "long"),
            "timeframe": getattr(signal, "timeframe", "15m"),
            "entry_price": getattr(signal, "entry_mid", None),
            "stop_price": getattr(signal, "stop_price", None),
            "tp1_price": getattr(signal, "take_profit_1", None),
            "tp2_price": getattr(signal, "take_profit_2", None),
            "score": score,
            "confluence_score": score,
            "delivery_status": "sent",
            "ts": datetime.now(UTC).isoformat(),
        }

    async def _get_chart_klines(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int,
    ) -> dict[str, Any]:
        bot = self.bot
        sym = str(symbol or "").strip().upper()
        if not sym:
            return {"error": "symbol_required"}

        ws = getattr(bot, "_ws_manager", None) if bot is not None else None
        rows: list[dict[str, Any]] | None = None
        source = "none"
        if ws is not None:
            cache_fn = getattr(ws, "get_kline_cache", None)
            if callable(cache_fn):
                cached = cache_fn(sym, interval)
                if cached:
                    rows = cached[-limit:]
                    source = "ws_cache"

        if rows is None and bot is not None:
            client = getattr(bot, "client", None)
            fetch_fn = getattr(client, "fetch_klines_cached", None) if client is not None else None
            if callable(fetch_fn):
                try:
                    df = await asyncio.wait_for(fetch_fn(sym, interval, limit=limit), timeout=8.0)
                    rows = self._dataframe_to_kline_rows(df)
                    source = "rest"
                except (TimeoutError, *DEFENSIVE_EXC):  # type: ignore[misc]
                    rows = None

        mark_price = None
        if ws is not None:
            snap = ws.get_mark_price_snapshot(sym)
            if isinstance(snap, dict):
                try:
                    mark_price = float(snap.get("mark_price") or 0.0) or None
                except TypeError, ValueError:
                    mark_price = None

        normalized = [self._normalize_kline_row(row) for row in (rows or [])]
        normalized = [row for row in normalized if row is not None]
        return {
            "symbol": sym,
            "interval": interval,
            "source": source,
            "mark_price": mark_price,
            "klines": normalized,
        }

    @staticmethod
    def _normalize_kline_row(row: dict[str, Any]) -> dict[str, Any] | None:
        try:
            open_px = float(row.get("open") or row.get("o") or 0.0)
            high_px = float(row.get("high") or row.get("h") or 0.0)
            low_px = float(row.get("low") or row.get("l") or 0.0)
            close_px = float(row.get("close") or row.get("c") or 0.0)
        except TypeError, ValueError:
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

    @staticmethod
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
            return []
        return []

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
            except OSError, json.JSONDecodeError:
                pass
            try:
                stamp = "_".join(run_id.split("_")[:2])
                return datetime.strptime(stamp, "%Y%m%d_%H%M%S").replace(tzinfo=UTC)
            except ValueError, TypeError:
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
            parsed = datetime.fromisoformat(str(value))
        except TypeError, ValueError:
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
        except DEFENSIVE_EXC as exc:
            LOG.debug("dashboard status tracking stats unavailable: %s", exc)
        try:
            get_market_context = getattr(bot._modern_repo, "get_market_context", None)
            if callable(get_market_context):
                market_context = await asyncio.wait_for(get_market_context(), timeout=1.0)
        except DEFENSIVE_EXC as exc:
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
            signals = await asyncio.wait_for(repo.get_active_signals(), timeout=2.0)
            return [serialize_tracking_signal(sig, bot) for sig in signals]
        except TimeoutError:
            LOG.debug("timeout fetching active signals for dashboard")
            return []
        except DEFENSIVE_EXC as exc:
            LOG.debug("error fetching active signals: %s", exc)
            return []

    def _get_recent_signals(self, limit: int = 20) -> list[dict[str, Any]]:
        bot = self.bot
        if bot is None or not hasattr(bot, "settings"):
            return []

        telemetry_dir = bot.settings.telemetry_dir
        selected_file = self._latest_analysis_file(telemetry_dir, "selected.jsonl")
        delivery_file = self._latest_analysis_file(telemetry_dir, "delivery.jsonl")
        delivery_rows = self._read_recent_jsonl(delivery_file, limit=limit * 4)
        success_statuses = frozenset({"sent", "logged"})
        success_rows = [
            row
            for row in delivery_rows
            if str(row.get("delivery_status") or row.get("status") or "").strip().lower()
            in success_statuses
        ]
        if success_rows:
            out: list[dict[str, Any]] = []
            for row in success_rows[:limit]:
                enriched = dict(row)
                enriched["source"] = "delivery"
                enriched["delivery_status"] = enriched.get("delivery_status") or enriched.get(
                    "status", "unknown"
                )
                out.append(enriched)
            return out

        selected_rows = self._read_recent_jsonl(selected_file, limit=limit * 4)
        if selected_rows:
            out = []
            for row in selected_rows[:limit]:
                enriched = dict(row)
                enriched["source"] = "selected"
                enriched["delivery_status"] = "selected"
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
        except DEFENSIVE_EXC as exc:
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
            except TypeError, ValueError:
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
            except TypeError, ValueError:
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
        current_file = runs_dir / current_run_id / "analysis" / filename if current_run_id else None
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
                    if _is_routing_excluded_decision_reason(reason):
                        continue
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
        return cast("dict[str, Any]", regime.to_dict())

    async def _get_metrics(self) -> dict[str, Any]:
        bot = self.bot
        if bot is None:
            return {"error": "bot_not_found"}

        # Get signal count with timeout
        open_signals_count = 0
        try:
            stats = await asyncio.wait_for(bot._modern_repo.get_tracking_stats(), timeout=1.0)
            open_signals_count = stats.get("active", 0)
        except DEFENSIVE_EXC as exc:
            LOG.debug("dashboard metrics tracking stats unavailable: %s", exc)

        # Get market regime safely
        regime_data = None
        try:
            regime = getattr(bot.market_regime, "_last_result", None)
            if regime:
                regime_data = regime.to_dict()
        except DEFENSIVE_EXC as exc:
            LOG.debug("dashboard metrics market regime unavailable: %s", exc)

        # Get engine stats safely
        engine_stats = {}
        try:
            engine = getattr(bot, "_modern_engine", None)
            if engine:
                engine_stats = engine.get_engine_stats()
        except DEFENSIVE_EXC as exc:
            LOG.debug("dashboard metrics engine stats unavailable: %s", exc)

        return {
            "shortlist_size": len(getattr(bot, "_shortlist", [])),
            "open_signals": open_signals_count,
            "ws_streams": len(bot._ws_manager._symbols) if getattr(bot, "_ws_manager", None) else 0,
            "market_regime": regime_data,
            "engine": engine_stats,
        }

    def _latest_analysis_file(self, telemetry_dir: Path, filename: str) -> Path | None:
        current_run_id = self._current_run_id()
        if current_run_id:
            preferred = telemetry_dir / "runs" / current_run_id / "analysis" / filename
            if preferred.exists():
                return preferred
        runs_dir = telemetry_dir / "runs"
        if not runs_dir.exists():
            return None
        candidates = sorted(
            runs_dir.glob(f"*/analysis/{filename}"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        return candidates[0] if candidates else None

    async def start_server_async(
        self, *, auto_open: bool = True, delay_seconds: float = 1.5
    ) -> None:
        """Start dashboard on the bot's asyncio loop (shared REST/WS client access)."""
        if not self._enabled or not self.app:
            LOG.debug("dashboard server disabled (fastapi not installed)")
            return
        if self._server_task is not None and not self._server_task.done():
            return
        if not HAS_UVICORN or uvicorn is None:
            LOG.exception("dashboard server failed to import uvicorn")
            return
        try:
            config = uvicorn.Config(
                self.app,
                host=self.host,
                port=self.port,
                log_level="warning",
                loop="asyncio",
            )
            self._uvicorn_server = uvicorn.Server(config)
            self._server_task = asyncio.create_task(
                self._serve_dashboard(),
                name="dashboard_server",
            )
        except DEFENSIVE_EXC:
            LOG.exception("dashboard server failed to start")
            return
        LOG.info("dashboard server started on port %d", self.port)
        if auto_open:
            self._schedule_browser_open(delay_seconds)

    async def _serve_dashboard(self) -> None:
        if self._uvicorn_server is None:
            return
        try:
            await self._uvicorn_server.serve()
        except OSError as exc:
            LOG.warning(
                "dashboard server not started | host=%s port=%d errno=%s",
                self.host,
                self.port,
                getattr(exc, "errno", None),
            )
        except DEFENSIVE_EXC:
            LOG.exception("dashboard server crashed")

    def start_server(self, *, auto_open: bool = True, delay_seconds: float = 1.5) -> None:
        """Legacy thread-based start; prefer ``start_server_async`` from the bot loop."""
        if not self._enabled or not self.app:
            LOG.debug("dashboard server disabled (fastapi not installed)")
            return

        def run_server() -> None:
            if self.app is None:
                return
            if not HAS_UVICORN or uvicorn is None:
                LOG.exception("dashboard server failed to import uvicorn")
                return
            try:
                uvicorn.run(self.app, host=self.host, port=self.port, log_level="warning")
            except DEFENSIVE_EXC:
                LOG.exception("dashboard server crashed")

        thread = Thread(target=run_server, daemon=True)
        thread.start()
        LOG.info("dashboard server started on port %d (legacy thread)", self.port)

        if auto_open:
            self._schedule_browser_open(delay_seconds)

    async def stop_server_async(self) -> None:
        server = self._uvicorn_server
        task = self._server_task
        if server is not None:
            server.should_exit = True
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, timeout=10.0)
            except TimeoutError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._uvicorn_server = None
        self._server_task = None

    def _schedule_browser_open(self, delay_seconds: float) -> None:
        """Open browser after server is ready."""

        def open_browser() -> None:
            time.sleep(delay_seconds)
            url = f"http://localhost:{self.port}"
            try:
                webbrowser.open(url, new=2)  # new=2 opens in new tab
                LOG.info("opened dashboard in browser: %s", url)
            except DEFENSIVE_EXC as exc:
                LOG.debug("failed to open browser: %s", exc)
                LOG.info("dashboard available at: %s", url)

        threading.Thread(target=open_browser, daemon=True).start()
