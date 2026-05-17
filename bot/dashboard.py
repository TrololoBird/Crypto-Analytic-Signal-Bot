"""FastAPI dashboard for signal bot monitoring."""

from __future__ import annotations

import json
import logging
import webbrowser
import asyncio
from collections import Counter
from datetime import datetime, timezone
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING, cast

UTC = timezone.utc

if TYPE_CHECKING:
    from fastapi import FastAPI

try:
    from fastapi import FastAPI as _FastAPI
    from fastapi.middleware.cors import CORSMiddleware as _CORSMiddleware
    from fastapi.responses import HTMLResponse as _HTMLResponse

    HAS_FASTAPI = True
except ImportError:
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
        self._analytics_cache: dict[int, tuple[float, dict[str, Any]]] = {}
        self._decision_cache: dict[
            tuple[int, int], tuple[float, tuple[tuple[str, float], ...], dict[str, Any]]
        ] = {}

        if not self._enabled:
            LOG.warning("fastapi not installed, dashboard disabled")
            return

        app = _FastAPI(title="Signal Bot Dashboard", version="2.0.0")

        # Security: Restrict CORS origins based on configuration
        origins = ["http://127.0.0.1", "http://localhost"]
        if hasattr(self.bot, "settings"):
            origins = list(self.bot.settings.runtime.dashboard_allow_origins)

        app.add_middleware(
            _CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["GET"],  # Security: Only allow GET for dashboard API
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
        self._setup_routes()
        self._cache_strategies()

    def _setup_routes(self) -> None:
        if not self.app:
            return

        @self.app.get("/", response_class=_HTMLResponse)
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
        async def analytics_report(days: int = 30) -> dict[str, Any]:
            try:
                from .analytics import StrategyAnalytics
            except ImportError as exc:
                LOG.error("failed to import StrategyAnalytics: %s", exc)
                return {"error": "analytics_unavailable"}

            days = max(1, min(int(days), 365))

            # TTL Cache for analytics report (60s)
            now = time.monotonic()
            cached = self._analytics_cache.get(days)
            if cached and (now - cached[0]) < 60.0:
                return cached[1]

            reporter = StrategyAnalytics(repo=self.bot._modern_repo)
            report = await reporter.generate_report(days=days)
            merged = self._merge_strategy_catalog(report)

            self._analytics_cache[days] = (now, merged)
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
        except Exception as exc:
            LOG.warning("failed to cache strategies: %s", exc)
            self._strategies_cache = []

    @staticmethod
    def _runtime_strategy_status(row: dict[str, Any], catalog_status: str) -> str:
        trades = int(row.get("trades") or row.get("count") or 0)
        expectancy = float(row.get("expectancy_r") or row.get("avg_rr") or 0.0)
        win_rate = float(row.get("win_rate") or 0.0)
        if trades <= 0:
            return "unverified"
        if trades >= 5 and expectancy < 0.0:
            return "needs_rework"
        if trades >= 5 and expectancy > 0.0 and win_rate >= 45.0:
            return "validated"
        return f"observing:{catalog_status}"

    def _merge_strategy_catalog(self, report: dict[str, Any]) -> dict[str, Any]:
        """Attach every registered strategy to analytics, including zero-outcome rows."""
        catalog = self._strategies_cache or []
        by_setup = report.get("by_setup") or {}

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
                    "profit_factor": None,
                    "max_drawdown_r": 0.0,
                }

            row = by_setup[setup_id]
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
        return report

    def _get_html_dashboard(self) -> str:
        return DASHBOARD_HTML

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
        except Exception:
            pass
        try:
            get_market_context = getattr(bot._modern_repo, "get_market_context", None)
            if callable(get_market_context):
                market_context = await asyncio.wait_for(get_market_context(), timeout=1.0)
        except Exception:
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
        return self._aggregate_recent_candidates(candidates, limit=limit)

    def _read_recent_jsonl(self, path: Path | None, *, limit: int) -> list[dict[str, Any]]:
        if path is None or limit <= 0:
            return []
        rows: list[dict[str, Any]] = []
        try:
            with path.open("rb") as handle:
                try:
                    handle.seek(0, 2)
                    size = handle.tell()
                    block_size = 4096
                    data = b""
                    lines_found = 0
                    pos = size
                    while pos > 0 and lines_found <= limit:
                        pos = max(0, pos - block_size)
                        handle.seek(pos)
                        chunk = handle.read(min(block_size, size - pos))
                        data = chunk + data
                        lines_found = data.count(b"\n")

                    lines = data.decode("utf-8", errors="ignore").splitlines()
                    for line in reversed(lines):
                        if not line.strip():
                            continue
                        try:
                            rows.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
                        if len(rows) >= limit:
                            break
                except (IOError, ValueError):
                    handle.seek(0)
                    lines = handle.read().decode("utf-8", errors="ignore").splitlines()
                    for line in reversed(lines[-limit:]):
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
                price = round(float(price_raw), 3)
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
        return sorted(
            runs_dir.glob(f"*/analysis/{stem}*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[: max(1, int(limit))]

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
                "setups": {},
                "setup_reports": [],
            }

        limit_files = max(1, min(int(limit_files), 24))
        max_rows = max(1, min(int(max_rows), 1_000_000))
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
                            "reason_family": Counter(),
                        },
                    )
                    counters["status"][status] += 1
                    counters["reason"][reason] += 1
                    counters["reason_family"][reason_family] += 1
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
            setups[setup_id] = {
                "setup_id": setup_id,
                "total": total,
                "signals": signals,
                "signal_rate": round(signals / total, 6) if total else 0.0,
                "status_counts": dict(counters["status"]),
                "reason_family_counts": dict(counters["reason_family"]),
                "top_reasons": top_reasons,
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
            "generated_at": datetime.now(UTC).isoformat(),
            "status_counts": dict(status_counts),
            "reason_family_counts": dict(reason_family_counts),
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
        except Exception:
            pass

        # Get market regime safely
        regime_data = None
        try:
            regime = getattr(bot.market_regime, "_last_result", None)
            if regime:
                regime_data = regime.to_dict()
        except Exception:
            pass

        # Get engine stats safely
        engine_stats = {}
        try:
            engine = getattr(bot, "_modern_engine", None)
            if engine:
                engine_stats = engine.get_engine_stats()
        except Exception:
            pass

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
                LOG.warning("dashboard server failed to import uvicorn: %s", exc)
                return
            try:
                uvicorn.run(self.app, host=self.host, port=self.port, log_level="warning")
            except Exception as exc:
                LOG.warning("dashboard server crashed: %s", exc)

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


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Signal Bot Dashboard</title>
    <style>
        :root {
            --bg-primary: #0d1117;
            --bg-secondary: #161b22;
            --bg-tertiary: #21262d;
            --border-color: #30363d;
            --text-primary: #c9d1d9;
            --text-secondary: #8b949e;
            --accent-blue: #58a6ff;
            --accent-green: #3fb950;
            --accent-red: #f85149;
            --accent-orange: #f0883e;
            --accent-purple: #a371f7;
            --accent-yellow: #d29922;
            --font-mono: 'SF Mono', ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            --radius: 8px;
            --shadow: 0 4px 12px rgba(0,0,0,0.4);
            --space-1: 4px;
            --space-2: 8px;
            --space-3: 12px;
            --space-4: 16px;
            --space-6: 24px;
            --space-8: 32px;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
            line-height: 1.5;
        }
        .header {
            background: var(--bg-secondary);
            border-bottom: 1px solid var(--border-color);
            padding: var(--space-4) var(--space-6);
            display: flex;
            align-items: center;
            justify-content: space-between;
            position: sticky;
            top: 0;
            z-index: 100;
            backdrop-filter: blur(8px);
            -webkit-backdrop-filter: blur(8px);
        }
        .header h1 {
            font-size: 20px;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: var(--space-2);
        }
        .header .status-badge {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: var(--space-1) var(--space-3);
            border-radius: 20px;
            font-size: 12px;
            font-weight: 500;
            background: var(--bg-tertiary);
        }
        .status-badge.online { background: rgba(63,185,80,0.15); color: var(--accent-green); }
        .status-badge.offline { background: rgba(248,81,73,0.15); color: var(--accent-red); }
        .status-badge::before {
            content: '';
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: currentColor;
            animation: pulse 2s infinite;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        .nav-tabs {
            display: flex;
            gap: var(--space-2);
            background: var(--bg-tertiary);
            padding: 4px;
            border-radius: var(--radius);
        }
        .nav-tab {
            padding: var(--space-2) var(--space-4);
            border: none;
            background: transparent;
            color: var(--text-secondary);
            font-size: 14px;
            cursor: pointer;
            border-radius: 6px;
            transition: all 0.2s;
            font-weight: 500;
        }
        .nav-tab:hover { color: var(--text-primary); }
        .nav-tab:focus-visible {
            outline: 2px solid var(--accent-blue);
            outline-offset: -2px;
        }
        .nav-tab.active { background: var(--bg-secondary); color: var(--text-primary); }
        .main { padding: var(--space-6); max-width: 1400px; margin: 0 auto; }
        .tab-content {
            display: none;
            animation: fadeIn 0.2s ease-out;
        }
        .tab-content.active { display: block; }
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(4px); }
            to { opacity: 1; transform: translateY(0); }
        }
        @media (prefers-reduced-motion: reduce) {
            * {
                animation-duration: 0.01ms !important;
                animation-iteration-count: 1 !important;
                transition-duration: 0.01ms !important;
                scroll-behavior: auto !important;
            }
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: var(--space-4);
            margin-bottom: var(--space-6);
        }
        .card {
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: var(--radius);
            padding: var(--space-4);
            box-shadow: var(--shadow);
        }
        .card h2 {
            font-size: 12px;
            font-weight: 600;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: var(--space-4);
            display: flex;
            align-items: center;
            gap: var(--space-2);
        }
        .metric-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px 0;
            border-bottom: 1px solid var(--border-color);
        }
        .metric-row:last-child { border-bottom: none; }
        .metric-label { color: var(--text-secondary); font-size: 14px; }
        .metric-value {
            font-family: var(--font-mono);
            font-weight: 600;
            font-size: 14px;
        }
        .metric-value.green { color: var(--accent-green); }
        .metric-value.red { color: var(--accent-red); }
        .metric-value.blue { color: var(--accent-blue); }
        .metric-value.yellow { color: var(--accent-yellow); }
        .metric-value.purple { color: var(--accent-purple); }
        .signal-card {
            background: var(--bg-tertiary);
            border: 1px solid var(--border-color);
            border-radius: var(--radius);
            padding: var(--space-4);
            margin-bottom: var(--space-3);
            transition: transform 0.1s ease, border-color 0.2s ease;
        }
        .signal-card:hover {
            border-color: var(--accent-blue);
            transform: translateY(-1px);
        }
        .signal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
        }
        .signal-symbol { cursor: pointer;
            font-family: var(--font-mono);
            font-size: 16px;
            font-weight: 600;
        }
        .signal-direction {
            padding: 4px 10px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
        }
        .signal-direction.long { background: rgba(63,185,80,0.15); color: var(--accent-green); }
        .signal-direction.short { background: rgba(248,81,73,0.15); color: var(--accent-red); }
        .signal-details {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 12px;
            margin-bottom: 12px;
        }
        .signal-detail {
            text-align: center;
        }
        .signal-detail-label {
            font-size: 11px;
            color: var(--text-secondary);
            text-transform: uppercase;
            margin-bottom: 4px;
        }
        .signal-detail-value {
            font-family: var(--font-mono);
            font-size: 13px;
            font-weight: 500;
        }
        .signal-footer {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding-top: 12px;
            border-top: 1px solid var(--border-color);
        }
        .signal-setup {
            font-size: 12px;
            color: var(--text-secondary);
        }
        .signal-score {
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 12px;
            font-weight: 600;
        }
        .score-high { background: rgba(63,185,80,0.15); color: var(--accent-green); }
        .score-medium { background: rgba(210,153,34,0.15); color: var(--accent-yellow); }
        .score-low { background: rgba(248,81,73,0.15); color: var(--accent-red); }
        .empty-state {
            text-align: center;
            padding: var(--space-8);
            color: var(--text-secondary);
            margin-top: var(--space-2);
            writing-mode: vertical-rl;
            text-orientation: mixed;
            transform: rotate(180deg);
            height: 70px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .chart-container {
            height: 220px;
            display: flex;
            align-items: flex-end;
            justify-content: center;
            gap: var(--space-2);
            padding: var(--space-4) 0;
        }
        .chart-bar-wrapper {
            display: flex;
            flex-direction: column;
            align-items: center;
            flex: 1;
            min-width: 30px;
            max-width: 60px;
        }
        .chart-bar {
            width: 100%;
            background: var(--accent-blue);
            border-radius: 4px 4px 0 0;
            min-height: 4px;
            position: relative;
            transition: height 0.3s ease, filter 0.2s ease;
        }
        .chart-bar:hover {
            filter: brightness(1.2);
        }
        .chart-label {
            font-size: 10px;
            color: var(--text-secondary);
            margin-top: var(--space-2);
            writing-mode: vertical-rl;
            text-orientation: mixed;
            transform: rotate(180deg);
            height: 70px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .empty-state-icon {
            font-size: 48px;
            margin-bottom: 16px;
            opacity: 0.5;
        }
        .last-update {
            position: fixed;
            bottom: 16px;
            right: 16px;
            padding: 8px 16px;
            background: var(--bg-tertiary);
            border: 1px solid var(--border-color);
            border-radius: var(--radius);
            font-size: 12px;
            color: var(--text-secondary);
        }
        .toast-container {
            position: fixed;
            top: 80px;
            right: 24px;
            display: flex;
            flex-direction: column;
            gap: 12px;
            z-index: 200;
        }
        .toast {
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: var(--radius);
            padding: 16px 20px;
            box-shadow: var(--shadow);
            animation: slideIn 0.3s ease;
            min-width: 280px;
        }
        @keyframes slideIn {
            from { transform: translateX(100%); opacity: 0; }
            to { transform: translateX(0); opacity: 1; }
        }
        .toast-title { font-weight: 600; margin-bottom: 4px; }
        .toast-body { font-size: 13px; color: var(--text-secondary); }
        .keyboard-hint {
            position: fixed;
            bottom: 16px;
            left: 16px;
            font-size: 12px;
            color: var(--text-secondary);
        }
        .keyboard-hint kbd {
            background: var(--bg-tertiary);
            padding: 2px 6px;
            border-radius: 4px;
            font-family: var(--font-mono);
        }
        @media (max-width: 768px) {
            .header { flex-direction: column; gap: 12px; }
            .nav-tabs { width: 100%; overflow-x: auto; }
            .main { padding: 16px; }
            .signal-details { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <header class="header">
        <h1>
            <span>Signal Bot</span>
            <span id="status-badge" class="status-badge offline" aria-live="polite">Offline</span>
        </h1>
        <nav class="nav-tabs" role="tablist" aria-label="Dashboard sections">
            <button class="nav-tab active" role="tab" id="tab-btn-overview" aria-selected="true" aria-controls="tab-panel-overview" data-tab="overview" onclick="switchTab('overview')">Overview</button>
            <button class="nav-tab" role="tab" id="tab-btn-signals" aria-selected="false" aria-controls="tab-panel-signals" data-tab="signals" onclick="switchTab('signals')">Signals</button>
            <button class="nav-tab" role="tab" id="tab-btn-analytics" aria-selected="false" aria-controls="tab-panel-analytics" data-tab="analytics" onclick="switchTab('analytics')">Analytics</button>
            <button class="nav-tab" role="tab" id="tab-btn-settings" aria-selected="false" aria-controls="tab-panel-settings" data-tab="settings" onclick="switchTab('settings')">Settings</button>
        </nav>
    </header>
    
    <main class="main">
        <!-- Overview Tab -->
        <div id="tab-panel-overview" class="tab-content active" role="tabpanel" aria-labelledby="tab-btn-overview">
            <div class="grid">
                <div class="card">
                    <h2>Bot Status</h2>
                    <div class="metric-row">
                        <span class="metric-label">Shortlist Size</span>
                        <span id="shortlist" class="metric-value blue">-</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Open Signals</span>
                        <span id="signals" class="metric-value">-</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">WS Latency</span>
                        <span id="ws-latency" class="metric-value">-</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Market Regime</span>
                        <span id="market" class="metric-value">-</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Delivery Provider</span>
                        <span id="delivery-provider" class="metric-value">-</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Last Cycle Delivery</span>
                        <span id="last-cycle-delivery" class="metric-value">-</span>
                    </div>
                </div>
                <div class="card">
                    <h2>Market Context</h2>
                    <div class="metric-row">
                        <span class="metric-label">BTC Bias</span>
                        <span id="btc-bias" class="metric-value">-</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Market Strength</span>
                        <span id="market-strength" class="metric-value">-</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Active Strategies</span>
                        <span id="active-strategies" class="metric-value green">-</span>
                    </div>
                </div>
            </div>
            <div class="card">
                <h2>Recent Activity</h2>
                <div id="recent-activity">
                    <div class="empty-state">
                        <p>Loading recent activity...</p>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- Signals Tab -->
        <div id="tab-panel-signals" class="tab-content" role="tabpanel" aria-labelledby="tab-btn-signals" aria-hidden="true">
            <div id="active-signals-list">
                <div class="empty-state">
                    <p>Loading active signals...</p>
                </div>
            </div>
        </div>
        
        <!-- Analytics Tab -->
        <div id="tab-panel-analytics" class="tab-content" role="tabpanel" aria-labelledby="tab-btn-analytics" aria-hidden="true">
            <div class="grid">
                <div class="card">
                    <h2>Performance (30d)</h2>
                    <div class="metric-row">
                        <span class="metric-label">Total Signals</span>
                        <span id="total-signals" class="metric-value">-</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Win Rate</span>
                        <span id="win-rate" class="metric-value">-</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Avg R/R</span>
                        <span id="avg-rr" class="metric-value">-</span>
                    </div>
                </div>
                <div class="card">
                    <h2>Signal Distribution</h2>
                    <div id="signal-chart" class="chart-container">
                        <div style="text-align: center; color: var(--text-secondary);">Loading chart...</div>
                    </div>
                </div>
                <div class="card">
                    <h2>Decision Funnel</h2>
                    <div class="metric-row">
                        <span class="metric-label">Rows</span>
                        <span id="decision-rows" class="metric-value">-</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Signal Rate</span>
                        <span id="decision-signal-rate" class="metric-value">-</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Top Blocker</span>
                        <span id="decision-top-blocker" class="metric-value">-</span>
                    </div>
                </div>
            </div>
            <div class="card">
                <h2>Strategy Performance</h2>
                <div id="strategy-chart" style="min-height: 150px;">
                    <div class="empty-state">
                        <p>Strategy performance data will appear here</p>
                    </div>
                </div>
            </div>
            <div class="card">
                <h2>Strategy Decision Diagnostics</h2>
                <div id="decision-diagnostics">
                    <div class="empty-state">
                        <p>Decision diagnostics will appear here</p>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- Settings Tab -->
        <div id="tab-panel-settings" class="tab-content" role="tabpanel" aria-labelledby="tab-btn-settings" aria-hidden="true">
            <div class="card">
                <h2>Strategy Configuration</h2>
                <div id="strategy-list">
                    <p class="empty-state">Loading strategies...</p>
                </div>
            </div>
        </div>
    </main>
    
    <div class="toast-container" id="toast-container" role="status" aria-live="polite"></div>
    
    <div class="last-update" id="last-update" aria-live="polite">Last update: -</div>
    
    <div class="keyboard-hint">
        <kbd>?</kbd> for help
    </div>
    
    <script>
        // Tab switching
        function switchTab(tabId) {
            const tabs = document.querySelectorAll('.nav-tab');
            const panels = document.querySelectorAll('.tab-content');
            const selectedTab = document.querySelector(`[data-tab="${tabId}"]`);

            if (!selectedTab) return;

            tabs.forEach(t => {
                t.classList.remove('active');
                t.setAttribute('aria-selected', 'false');
            });
            panels.forEach(p => {
                p.classList.remove('active');
                p.setAttribute('aria-hidden', 'true');
            });

            selectedTab.classList.add('active');
            selectedTab.setAttribute('aria-selected', 'true');
            const activePanel = document.getElementById('tab-panel-' + tabId);
            if (activePanel) {
                activePanel.classList.add('active');
                activePanel.setAttribute('aria-hidden', 'false');
            }

            // Persist tab in URL
            window.location.hash = tabId;

            // Trigger immediate refresh for the relevant data
            if (tabId === 'overview') { fetchStatus(); fetchRecentActivity(); }
            if (tabId === 'signals') fetchSignals();
            if (tabId === 'analytics') fetchAnalytics();
            if (tabId === 'settings') fetchStrategies();
        }


        // Handle initial load and back/forward navigation
        window.addEventListener('load', () => {
            const hash = window.location.hash.replace('#', '') || 'overview';
            switchTab(hash);
        });
        window.addEventListener('hashchange', () => {
            const hash = window.location.hash.replace('#', '');
            if (hash) switchTab(hash);
        });
        
        // Keyboard shortcuts
        document.addEventListener('keydown', (e) => {
            if (e.key === '?') {
                showToast('Keyboard Shortcuts', '1-4: Switch tabs | R: Refresh | ?: Help');
            }
            if (e.key >= '1' && e.key <= '4') {
                const tabs = ['overview', 'signals', 'analytics', 'settings'];
                const tabButton = document.querySelector(`[data-tab="${tabs[e.key - 1]}"]`);
                if (tabButton) tabButton.click();
            }
            if (e.key === 'r' || e.key === 'R') {
                fetchStatus();
                fetchSignals();
                showToast('Refreshed', 'Data updated manually');
            }
        });
        
        // Toast notifications
        function showToast(title, body) {
            const container = document.getElementById('toast-container');
            if (!container) return;
            const toast = document.createElement('div');
            toast.className = 'toast';
            toast.innerHTML = `<div class="toast-title">${title}</div><div class="toast-body">${body}</div>`;
            container.appendChild(toast);
            setTimeout(() => toast.remove(), 5000);
        }
        
        // Formatters
        const finiteNumber = (v) => {
            const n = Number(v);
            return Number.isFinite(n) ? n : null;
        };
        const fmt = {
            price: (v) => {
                const n = finiteNumber(v);
                return n === null ? '-' : n.toFixed(4);
            },
            pct: (v) => {
                const n = finiteNumber(v);
                return n === null ? '-' : (n * 100).toFixed(1) + '%';
            },
            score: (v) => {
                const n = finiteNumber(v);
                if (n === null) return { text: '-', class: '' };
                if (n >= 0.75) return { text: (n * 100).toFixed(0) + '%', class: 'score-high' };
                if (n >= 0.60) return { text: (n * 100).toFixed(0) + '%', class: 'score-medium' };
                return { text: (n * 100).toFixed(0) + '%', class: 'score-low' };
            },
            timeAgo: (v) => {
                if (!v) return "-";
                const seconds = Math.floor((new Date() - new Date(v)) / 1000);
                if (seconds < 60) return "just now";
                if (seconds < 3600) return Math.floor(seconds / 60) + "m";
                if (seconds < 86400) return Math.floor(seconds / 3600) + "h";
                return Math.floor(seconds / 86400) + "d";
            }
        };


        async function copyToClipboard(text) {
            try {
                if (navigator.clipboard) {
                    await navigator.clipboard.writeText(text);
                    showToast('Copied', text + ' copied to clipboard');
                }
            } catch (err) { console.warn(err); }
        }

        function handleCopyKey(e, text) {
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                copyToClipboard(text);
            }
        }
        // Fetch status
        async function fetchStatus() {
            try {
                const res = await fetch('/api/status');
                if (!res.ok) throw new Error('Status fetch failed');
                const data = await res.json();
                
                // Update status badge
                const badge = document.getElementById('status-badge');
                if (badge) {
                    badge.textContent = data.running ? 'Online' : 'Offline';
                    badge.className = 'status-badge ' + (data.running ? 'online' : 'offline');
                }
                
                // Update metrics
                const shortlistEl = document.getElementById('shortlist');
                if (shortlistEl) shortlistEl.textContent = data.shortlist_size ?? '-';
                const signalsEl = document.getElementById('signals');
                if (signalsEl) signalsEl.textContent = data.open_signals ?? '-';
                const wsLatencyEl = document.getElementById('ws-latency');
                if (wsLatencyEl) wsLatencyEl.textContent = (data.ws_latency_ms ?? '-') + ' ms';
                
                const regimeEl = document.getElementById('market');
                if (regimeEl) {
                    regimeEl.textContent = data.market_regime || '-';
                    regimeEl.className = 'metric-value ' + (
                        data.market_regime === 'bull' ? 'green' :
                        data.market_regime === 'bear' ? 'red' : 'yellow'
                    );
                }
                
                const btcBiasEl = document.getElementById('btc-bias');
                if (btcBiasEl) document.getElementById('btc-bias').textContent = data.btc_bias || '-';
                const marketStrengthEl = document.getElementById('market-strength');
                if (marketStrengthEl) marketStrengthEl.textContent = fmt.pct(data.market_strength);
                const deliveryProviderEl = document.getElementById('delivery-provider');
                if (deliveryProviderEl) deliveryProviderEl.textContent = data.delivery_provider || '-';
                const lastCycleDeliveryEl = document.getElementById('last-cycle-delivery');
                if (lastCycleDeliveryEl) {
                    const selected = Number(data.last_cycle_selected || 0);
                    const delivered = Number(data.last_cycle_delivered || 0);
                    const statuses = data.last_cycle_delivery_status_counts || {};
                    const tail = Object.keys(statuses).length ? ` | ${JSON.stringify(statuses)}` : '';
                    lastCycleDeliveryEl.textContent = `${delivered}/${selected}${tail}`;
                }
                
                const lastUpdateEl = document.getElementById('last-update');
                if (lastUpdateEl) lastUpdateEl.textContent = 'Last update: ' + new Date().toLocaleTimeString();
            } catch (err) {
                console.error('Failed to fetch status:', err);
                const badge = document.getElementById('status-badge');
                if (badge) {
                    badge.textContent = 'Error';
                    badge.className = 'status-badge offline';
                }
            }
        }
        
        // Fetch active signals
        async function fetchSignals() {
            const container = document.getElementById('active-signals-list');
            try {
                const res = await fetch('/api/signals/active');
                if (!res.ok) throw new Error('Signals fetch failed');
                const data = await res.json();
                
                if (data.length === 0) {
                    container.innerHTML = `
                        <div class="empty-state">
                            <div class="empty-state-icon">📡</div>
                            <p>No active signals</p>
                        </div>`;
                    return;
                }
                
                container.innerHTML = data.map(s => {
                    const score = fmt.score(s.score);
                    return `
                        <div class="signal-card">
                            <div class="signal-header">
                                <span class="signal-symbol" role="button" tabindex="0" onclick="copyToClipboard('${s.symbol}')" onkeydown="handleCopyKey(event, '${s.symbol}')" title="Click to copy">${s.symbol}</span>
                                <span class="signal-direction ${s.direction.toLowerCase()}">${s.direction}</span>
                            </div>
                            <div class="signal-details">
                                <div class="signal-detail">
                                    <div class="signal-detail-label">Entry</div>
                                    <div class="signal-detail-value">${fmt.price(s.entry_price)}</div>
                                </div>
                                <div class="signal-detail">
                                    <div class="signal-detail-label">Stop</div>
                                    <div class="signal-detail-value" style="color: var(--accent-red)">${fmt.price(s.stop_price)}</div>
                                </div>
                                <div class="signal-detail">
                                    <div class="signal-detail-label">TP1</div>
                                    <div class="signal-detail-value" style="color: var(--accent-green)">${fmt.price(s.tp1_price)}</div>
                                </div>
                            </div>
                            <div class="signal-footer">
                                <span class="signal-setup">${s.setup_id} <small style="opacity: 0.5">(${fmt.timeAgo(s.timestamp)})</small></span>
                                <span class="signal-score ${score.class}">${score.text}</span>
                            </div>
                        </div>
                    `;
                }).join('');
            } catch (err) {
                console.error('Failed to fetch signals:', err);
                container.innerHTML = `<div class="empty-state"><p style="color: var(--accent-red)">Failed to load signals</p></div>`;
            }
        }

        // Fetch recent activity
        async function fetchRecentActivity() {
            const container = document.getElementById('recent-activity');
            try {
                const res = await fetch('/api/signals/recent?limit=10');
                if (!res.ok) throw new Error('Activity fetch failed');
                const data = await res.json();

                if (!Array.isArray(data) || data.length === 0) {
                    container.innerHTML = `
                        <div class="empty-state">
                            <div class="empty-state-icon">📝</div>
                            <p>No recent activity</p>
                        </div>`;
                    return;
                }

                container.innerHTML = data.map(s => {
                    const score = fmt.score(s.score);
                    const when = s.ts || s.created_at || '';
                    const timeStr = when ? ` <small style="opacity: 0.5">(${fmt.timeAgo(when)})</small>` : '';
                    const confluence = Number(s.confluence_count || 0);
                    const conflText = confluence > 1 ? ` x${confluence}` : '';
                    const source = s.source || 'candidate';
                    const status = s.delivery_status || source;
                    return `
                        <div class="metric-row">
                            <span class="metric-label">${s.symbol || '-'} ${s.setup_id || ''} ${s.direction || ''}${conflText}${timeStr}<br><small style="color: var(--text-secondary)">${status}</small></span>
                            <span class="metric-value ${score.class}" title="${when}">${score.text}</span>
                        </div>
                    `;
                }).join('');
            } catch (err) {
                console.error('Failed to fetch recent activity:', err);
                container.innerHTML = `<div class="empty-state"><p style="color: var(--accent-red)">Failed to load activity</p></div>`;
            }
        }
        
        // Fetch strategies
        async function fetchStrategies() {
            try {
                const controller = new AbortController();
                const timeoutId = setTimeout(() => controller.abort(), 3000);
                const res = await fetch('/api/strategies', { signal: controller.signal });
                clearTimeout(timeoutId);
                
                if (!res.ok) {
                    throw new Error('HTTP ' + res.status);
                }
                
                const data = await res.json();
                const container = document.getElementById('strategy-list');
                
                if (!Array.isArray(data) || data.length === 0) {
                    container.innerHTML = '<p class="empty-state">No strategies available</p>';
                    document.getElementById('active-strategies').textContent = '0/0';
                    return;
                }
                
                document.getElementById('active-strategies').textContent = data.filter(s => s.enabled).length + '/' + data.length;
                
                container.innerHTML = data.map(s => `
                    <div class="metric-row">
                        <span class="metric-label">${s.name}</span>
                        <span class="metric-value ${s.enabled ? 'green' : 'red'}">${s.enabled ? 'ON' : 'OFF'}</span>
                    </div>
                `).join('');
            } catch (err) {
                console.error('Failed to fetch strategies:', err);
                const container = document.getElementById('strategy-list');
                container.innerHTML = '<p class="empty-state">Failed to load strategies</p>';
            }
        }
        
        // Fetch analytics
        async function fetchAnalytics() {
            const signalChart = document.getElementById('signal-chart');
            const strategyChart = document.getElementById('strategy-chart');

            try {
                const controller = new AbortController();
                const timeoutId = setTimeout(() => controller.abort(), 8000);
                const res = await fetch('/api/analytics/report?days=30', { signal: controller.signal });
                clearTimeout(timeoutId);
                
                if (!res.ok) {
                    throw new Error('HTTP ' + res.status);
                }
                
                const data = await res.json();
                if (data.error) throw new Error(data.error);

                if (data.summary) {
                    document.getElementById('total-signals').textContent = data.summary.total_signals || '0';
                    document.getElementById('win-rate').textContent = data.summary.win_rate ? (data.summary.win_rate * 100).toFixed(1) + '%' : '0%';
                    document.getElementById('avg-rr').textContent = data.summary.avg_rr ? data.summary.avg_rr.toFixed(2) : '0.00';
                }
                
                // Render signal distribution chart
                renderSignalChart(data.by_setup || {});
                renderStrategyPerformance(data.setup_reports || []);
                fetchStrategyDecisions();

            } catch (err) {
                console.error('Analytics error:', err);
                const errorMsg = `<div style="text-align: center; color: var(--accent-red); width: 100%;">Failed to load analytics: ${err.message}</div>`;
                if (signalChart) signalChart.innerHTML = errorMsg;
                if (strategyChart) strategyChart.innerHTML = `<div class="empty-state"><p style="color: var(--accent-red)">Error: ${err.message}</p></div>`;
            }
        }

        async function fetchStrategyDecisions() {
            const container = document.getElementById('decision-diagnostics');
            try {
                const res = await fetch('/api/analytics/strategy-decisions?limit_files=1&max_rows=1000');
                if (!res.ok) throw new Error('HTTP ' + res.status);
                const data = await res.json();
                if (data.error) throw new Error(data.error);

                const total = data.total_rows || 0;
                const signals = (data.status_counts && data.status_counts.signal) || 0;
                const signalRate = total ? (signals / total * 100).toFixed(2) + '%' : '0.00%';
                const blockers = Object.entries(data.reason_family_counts || {})
                    .sort((a, b) => b[1] - a[1]);

                document.getElementById('decision-rows').textContent = total;
                document.getElementById('decision-signal-rate').textContent = signalRate;
                document.getElementById('decision-top-blocker').textContent = blockers.length ? blockers[0][0] : '-';

                const rows = (data.setup_reports || []).slice(0, 18);
                if (!rows.length) {
                    container.innerHTML = '<div class="empty-state"><p>No strategy decision telemetry</p></div>';
                    return;
                }
                container.innerHTML = rows.map(row => {
                    const topReason = row.top_reasons && row.top_reasons.length ? row.top_reasons[0].reason : '-';
                    const rate = ((row.signal_rate || 0) * 100).toFixed(2) + '%';
                    return `
                        <div class="metric-row">
                            <span class="metric-label">${row.setup_id}<br><small style="color: var(--text-secondary)">${topReason}</small></span>
                            <span class="metric-value">${row.signals || 0}/${row.total || 0} ${rate}</span>
                        </div>
                    `;
                }).join('');
            } catch (err) {
                console.error('Strategy decision diagnostics error:', err);
                if (container) {
                    container.innerHTML = `<div class="empty-state"><p style="color: var(--accent-red)">Failed to load diagnostics</p></div>`;
                }
            }
        }
        
        // Render signal distribution bar chart
        function renderSignalChart(bySetup) {
            const container = document.getElementById('signal-chart');
            if (!bySetup || Object.keys(bySetup).length === 0) {
                container.innerHTML = '<div class="empty-state">No data available</div>';
                return;
            }
            
            const setups = Object.entries(bySetup)
                .sort((a, b) => (b[1].count || 0) - (a[1].count || 0))
                .slice(0, 10);  // Top 10 setups
            
            const maxCount = Math.max(...setups.map(s => s[1].count || 0), 1);
            
            container.innerHTML = setups.map(([name, data]) => {
                const count = data.count || 0;
                const height = (count / maxCount * 140) || 0;
                const winRate = data.win_rate ? (data.win_rate * 100).toFixed(0) : '0';
                const label = name.replace('Setup', '').replace('Strategy', '');
                return `
                    <div class="chart-bar-wrapper">
                        <div class="chart-bar" style="height: ${height}px" title="${name}: ${count} signals, ${winRate}% win">
                        </div>
                        <div class="chart-label">${label}</div>
                    </div>
                `;
            }).join('');
        }

        function renderStrategyPerformance(setupReports) {
            const container = document.getElementById('strategy-chart');
            if (!Array.isArray(setupReports) || setupReports.length === 0) {
                container.innerHTML = '<div class="empty-state"><p>No strategy performance data</p></div>';
                return;
            }

            const rows = setupReports
                .slice()
                .sort((a, b) => {
                    const enabledDelta = Number(b.enabled !== false) - Number(a.enabled !== false);
                    if (enabledDelta !== 0) return enabledDelta;
                    return (b.trades || 0) - (a.trades || 0);
                })
                .map(row => {
                    const winRate = ((row.win_rate || 0) * 100).toFixed(1) + '%';
                    const rr = Number(row.expectancy_r || 0).toFixed(2);
                    const valueClass = row.expectancy_r > 0 ? 'green' : (row.expectancy_r < 0 ? 'red' : 'yellow');
                    const status = row.status || 'beta';
                    const suffix = row.enabled === false ? ' off' : status;
                    return `
                        <div class="metric-row">
                            <span class="metric-label">${row.setup_id}</span>
                            <span class="metric-value ${valueClass}">${row.trades || 0} / ${winRate} / ${rr}R / ${suffix}</span>
                        </div>
                    `;
                }).join('');

            container.innerHTML = rows;
        }
        
        // Initial fetch
        fetchStatus();
        fetchSignals();
        fetchRecentActivity();
        fetchStrategies();
        fetchAnalytics();
        
        // Periodic updates
        let tick = 0;
        setInterval(() => {
            tick++;
            const activeButton = document.querySelector(".nav-tab.active");
            const activeTab = activeButton ? activeButton.dataset.tab : "overview";

            // Frequent updates (every 5s)
            fetchStatus();
            if (activeTab === "signals") fetchSignals();
            if (activeTab === "overview") fetchRecentActivity();

            // Less frequent updates (every 30s)
            if (tick % 6 === 0) {
                if (activeTab === "settings") fetchStrategies();
                if (activeTab === "analytics") fetchAnalytics();
            }
        }, 5000);

        // Visibility change handling
        document.addEventListener("visibilitychange", () => {
            if (document.visibilityState === "visible") {
                fetchStatus();
                fetchSignals();
            }
        });
        
        // Welcome toast
        setTimeout(() => {
            showToast('Dashboard Ready', 'Signal Bot is running');
        }, 1000);
    </script>
</body>
</html>"""
