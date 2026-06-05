"""Runtime health, metrics, alerts, strategy audit (single module)."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import sqlite3
import time
from collections import Counter, defaultdict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from bot.strategies import STRATEGY_CLASSES

if TYPE_CHECKING:
    from bot.engine.registry import StrategyRegistry
    from bot.runtime.bot import SignalBot
    from bot.diagnostics.analyzer_ops import PerformanceMetrics, WinRateCalculator

# --- from runtime/health.py ---
if TYPE_CHECKING:
    from bot.engine.registry import StrategyRegistry
    from bot.runtime.bot import SignalBot

    from bot.diagnostics.analyzer_ops import PerformanceMetrics, WinRateCalculator

LOG = logging.getLogger("bot.diagnostics.runtime_ops")
HEALTH_CHECK_TIMEOUT_SECONDS = 5.0  # seconds: cap each component health probe


async def bot_runtime_health_check(bot: SignalBot) -> dict[str, Any]:
    """Adapter for live diagnostics: delegate to :class:`HealthManager`."""
    from bot.runtime.health_manager import HealthManager

    return await HealthManager(bot).health_check()


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class HealthStatus(Enum):
    """Health status levels."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass
class ComponentHealth:
    """Health status of a component."""

    name: str
    status: HealthStatus
    message: str = ""
    last_check: datetime | None = None
    details: dict[str, Any] | None = None


class HealthChecker:
    """Check health of bot components and overall system."""

    # Thresholds for degradation detection
    WINRATE_DEGRADATION_THRESHOLD = 0.15  # 15% drop
    PF_DEGRADATION_THRESHOLD = 0.3  # 0.3 drop
    MIN_WINRATE = 0.35  # Minimum acceptable win rate
    MIN_PF = 1.0  # Minimum acceptable profit factor

    def __init__(
        self,
        registry: StrategyRegistry,
        calculator: WinRateCalculator,
        *,
        ws_manager: Any | None = None,
    ):
        self._registry = registry
        self._calc = calculator
        self._ws_manager = ws_manager
        self._baseline_metrics: dict[str, PerformanceMetrics] = {}
        self._last_check: datetime | None = None

    async def check_all(self) -> list[ComponentHealth]:
        """Run all health checks."""
        checks = [
            self._check_with_timeout("strategies", self._check_strategies()),
            self._check_with_timeout("performance", self._check_performance()),
            self._check_with_timeout("websocket", self._check_ws_health()),
        ]

        results = await asyncio.gather(*checks, return_exceptions=True)

        health_results = []
        for result in results:
            if isinstance(result, Exception):
                LOG.error("Health check failed: %s", result)
                continue
            health_results.append(cast("ComponentHealth", result))

        self._last_check = _utcnow_naive()
        return health_results

    async def _check_with_timeout(
        self, name: str, check: asyncio.Future[ComponentHealth] | Any
    ) -> ComponentHealth:
        try:
            return await asyncio.wait_for(check, timeout=HEALTH_CHECK_TIMEOUT_SECONDS)
        except TimeoutError:
            LOG.warning("health_check_timeout", extra={"component": name})
            return ComponentHealth(
                name=name,
                status=HealthStatus.DEGRADED,
                message="Health check timed out",
            )

    async def _check_strategies(self) -> ComponentHealth:
        """Check strategy health."""
        enabled = len(self._registry.get_enabled())
        total = len(self._registry)

        if enabled == 0:
            return ComponentHealth(
                name="strategies",
                status=HealthStatus.UNHEALTHY,
                message="No strategies enabled",
                details={"total": total, "enabled": enabled},
            )

        # Check for strategies with high error rates
        perf = self._registry.get_all_performance()
        high_error_strategies = [
            sid
            for sid, p in perf.items()
            if p["calculation_errors"] > p["signals_generated"] * 0.1  # >10% errors
        ]

        if high_error_strategies:
            return ComponentHealth(
                name="strategies",
                status=HealthStatus.DEGRADED,
                message=f"Strategies with high error rates: {high_error_strategies}",
                details={
                    "total": total,
                    "enabled": enabled,
                    "high_error_strategies": high_error_strategies,
                },
            )

        return ComponentHealth(
            name="strategies",
            status=HealthStatus.HEALTHY,
            message=f"{enabled}/{total} strategies enabled",
            details={"total": total, "enabled": enabled},
        )

    async def _check_performance(self) -> ComponentHealth:
        """Check overall performance health."""
        metrics = await self._calc.calculate_metrics(since=_utcnow_naive() - timedelta(days=7))

        # Check minimum thresholds
        if metrics.total_signals < 5:
            return ComponentHealth(
                name="performance",
                status=HealthStatus.HEALTHY,  # Not enough data
                message="Insufficient signals for performance check",
                details={"total_signals": metrics.total_signals},
            )

        issues = []

        if metrics.win_rate < self.MIN_WINRATE:
            issues.append(f"Low win rate: {metrics.win_rate * 100:.1f}%")

        if metrics.profit_factor < self.MIN_PF:
            issues.append(f"Low PF: {metrics.profit_factor:.2f}")

        # Check degradation from baseline
        if "overall" in self._baseline_metrics:
            baseline = self._baseline_metrics["overall"]

            if baseline.win_rate > 0:
                winrate_drop = baseline.win_rate - metrics.win_rate
                if winrate_drop > self.WINRATE_DEGRADATION_THRESHOLD:
                    issues.append(f"Win rate degraded by {winrate_drop * 100:.1f}%")

            if baseline.profit_factor > 0:
                pf_drop = baseline.profit_factor - metrics.profit_factor
                if pf_drop > self.PF_DEGRADATION_THRESHOLD:
                    issues.append(f"PF degraded by {pf_drop:.2f}")

        if issues:
            return ComponentHealth(
                name="performance",
                status=HealthStatus.DEGRADED,
                message="; ".join(issues),
                details=metrics.to_dict(),
            )

        # Update baseline if performance is good
        if metrics.win_rate > 0.5 and metrics.profit_factor > 1.2:
            self._baseline_metrics["overall"] = metrics

        return ComponentHealth(
            name="performance",
            status=HealthStatus.HEALTHY,
            message=f"Win rate: {metrics.win_rate * 100:.1f}%, PF: {metrics.profit_factor:.2f}",
            details=metrics.to_dict(),
        )

    async def _check_ws_health(self) -> ComponentHealth:
        """Check WebSocket health via ``FuturesWSManager.state_snapshot`` when wired."""
        manager = self._ws_manager
        if manager is None:
            return ComponentHealth(
                name="websocket",
                status=HealthStatus.DEGRADED,
                message="WebSocket manager not attached to health checker",
            )
        snapshot = manager.state_snapshot()
        connected = bool(snapshot.get("connected"))
        fresh_tickers = int(snapshot.get("fresh_tickers") or 0)
        fresh_mark = int(snapshot.get("fresh_mark_prices") or 0)
        stale_klines = int(snapshot.get("stale_kline_streams") or 0)
        if not connected:
            return ComponentHealth(
                name="websocket",
                status=HealthStatus.UNHEALTHY,
                message="WebSocket disconnected",
                details=snapshot,
            )
        if fresh_tickers == 0 or fresh_mark == 0:
            return ComponentHealth(
                name="websocket",
                status=HealthStatus.DEGRADED,
                message="WebSocket connected but market cache is empty/stale",
                details={
                    "fresh_tickers": fresh_tickers,
                    "fresh_mark_prices": fresh_mark,
                    "stale_kline_streams": stale_klines,
                },
            )
        if stale_klines > 0:
            return ComponentHealth(
                name="websocket",
                status=HealthStatus.DEGRADED,
                message=f"WebSocket connected with {stale_klines} stale kline streams",
                details={"stale_kline_streams": stale_klines},
            )
        return ComponentHealth(
            name="websocket",
            status=HealthStatus.HEALTHY,
            message="WebSocket connections active",
            details={
                "fresh_tickers": fresh_tickers,
                "fresh_mark_prices": fresh_mark,
            },
        )

    def get_overall_status(self, checks: list[ComponentHealth]) -> HealthStatus:
        """Determine overall health from component checks."""
        if any(c.status == HealthStatus.UNHEALTHY for c in checks):
            return HealthStatus.UNHEALTHY
        if any(c.status == HealthStatus.DEGRADED for c in checks):
            return HealthStatus.DEGRADED
        return HealthStatus.HEALTHY

    def format_report(self, checks: list[ComponentHealth]) -> str:
        """Format health report."""
        overall = self.get_overall_status(checks)

        lines = [
            f"Health Check - Overall: {overall.value}",
            f"Last Check: {self._last_check.isoformat() if self._last_check else 'never'}",
            "",
            "Components:",
        ]

        for check in checks:
            icon = (
                "✅"
                if check.status == HealthStatus.HEALTHY
                else "⚠️"
                if check.status == HealthStatus.DEGRADED
                else "❌"
            )
            lines.append(f"  {icon} {check.name}: {check.status.value}")
            if check.message:
                lines.append(f"     {check.message}")

        return "\n".join(lines)


# --- Market radar funnel (Tier-0 !ticker@arr ingest) ---

_STALE_INGEST_SECONDS = 120.0
_STALE_SYMBOL_RATIO_ALERT = 0.35
_MIN_SYMBOLS_FOR_ALERT = 50


def assess_radar_store(
    store: object | None,
    *,
    config: object,
    now: float | None = None,
) -> dict[str, Any]:
    """JSON-safe radar health for HealthManager, telemetry, startup_report."""
    import time as _time

    enabled = bool(getattr(config, "enabled", False))
    if store is None:
        return {"enabled": enabled, "attached": False, "status": "unavailable"}
    if not enabled:
        return {"enabled": False, "attached": True, "status": "disabled"}

    ts = float(now if now is not None else _time.monotonic())
    total = int(getattr(store, "symbol_count", 0) or 0)
    tiers = store.snapshot_summary() if hasattr(store, "snapshot_summary") else {}
    stale = recent = flagged = 0
    iter_states = getattr(store, "iter_states", None)
    states = list(iter_states()) if callable(iter_states) else []
    for state in states:
        if getattr(state, "flags", ()):
            flagged += 1
        age = ts - float(getattr(state, "last_update_ts", 0.0) or 0.0)
        if age > _STALE_INGEST_SECONDS:
            stale += 1
        else:
            recent += 1

    stale_ratio = (stale / total) if total else 0.0
    status = "healthy"
    alerts: list[str] = []
    if total < _MIN_SYMBOLS_FOR_ALERT:
        status = "degraded"
        alerts.append("low_symbol_count")
    if total >= _MIN_SYMBOLS_FOR_ALERT and stale_ratio >= _STALE_SYMBOL_RATIO_ALERT:
        status = "degraded"
        alerts.append("stale_ingest_ratio_high")
    if flagged == 0 and total >= _MIN_SYMBOLS_FOR_ALERT:
        status = "degraded"
        alerts.append("zero_screener_flags")

    return {
        "enabled": True,
        "attached": True,
        "status": status,
        "alerts": alerts,
        "symbol_count": total,
        "tiers": tiers,
        "flagged_count": flagged,
        "recent_ingest_count": recent,
        "stale_ingest_count": stale,
        "stale_ingest_ratio": round(stale_ratio, 4),
        "last_tier_cycle_age_s": round(
            max(0.0, ts - float(getattr(store, "_last_tier_cycle_ts", 0.0) or 0.0)),
            2,
        ),
    }

# --- from runtime/metrics.py ---
LOG = logging.getLogger("bot.core.diagnostics.metrics")


def _finite_metric_value(value: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    return numeric if math.isfinite(numeric) else 0.0


def safe_set(gauge: Gauge, value: float) -> None:
    gauge.set(_finite_metric_value(value))


@dataclass
class Gauge:
    """Simple gauge metric."""

    name: str
    description: str
    value: float = 0.0
    labels: dict[str, str] = field(default_factory=dict)

    def set(self, value: float) -> None:
        self.value = _finite_metric_value(value)

    def inc(self, amount: float = 1.0) -> None:
        self.value += amount

    def dec(self, amount: float = 1.0) -> None:
        self.value -= amount


@dataclass
class Counter:
    """Simple counter metric (monotonically increasing)."""

    name: str
    description: str
    value: float = 0.0
    labels: dict[str, str] = field(default_factory=dict)

    def inc(self, amount: float = 1.0) -> None:
        self.value += amount


@dataclass
class Histogram:
    """Histogram metric with buckets."""

    name: str
    description: str
    buckets: list[float] = field(default_factory=lambda: [0.1, 0.5, 1.0, 2.5, 5.0, 10.0])
    counts: dict[float, int] = field(default_factory=dict)
    sum_val: float = 0.0
    count: int = 0

    def observe(self, value: float) -> None:
        self.sum_val += value
        self.count += 1

        for bucket in self.buckets:
            if value <= bucket:
                self.counts[bucket] = self.counts.get(bucket, 0) + 1


@dataclass
class TimeSeries:
    """Time series data point."""

    timestamp: float
    value: float


class TimeSeriesGauge:
    """Gauge with time-series history."""

    def __init__(self, name: str, description: str, max_history: int = 1000):
        self.name = name
        self.description = description
        self._current: float = 0.0
        self._history: deque[TimeSeries] = deque(maxlen=max_history)

    def set(self, value: float) -> None:
        self._current = value
        self._history.append(TimeSeries(time.time(), value))

    @property
    def value(self) -> float:
        return self._current

    def get_history(self, duration_seconds: float) -> list[TimeSeries]:
        """Get history for last N seconds."""
        cutoff = time.time() - duration_seconds
        return [ts for ts in self._history if ts.timestamp >= cutoff]


class BotMetrics:
    """Collection of bot metrics."""

    def __init__(self) -> None:
        # Signal metrics
        self.signals_generated = Counter("signals_total", "Total signals generated")
        self.signals_by_strategy: dict[str, Counter] = {}
        self.signal_score = Histogram(
            "signal_score",
            "Signal confidence scores",
            buckets=[0.5, 0.6, 0.7, 0.8, 0.9],
        )

        # Performance metrics
        self.winrate_24h = Gauge("winrate_24h", "24h rolling win rate")
        self.profit_factor = Gauge("profit_factor", "Current profit factor")
        self.sharpe_ratio = Gauge("sharpe_ratio", "Sharpe ratio")

        # Strategy performance
        self.strategy_calc_time = Histogram(
            "strategy_calc_ms",
            "Strategy calculation time (ms)",
            buckets=[10, 50, 100, 250, 500, 1000],
        )
        self.strategy_errors = Counter("strategy_errors_total", "Strategy calculation errors")

        # WebSocket metrics
        self.ws_reconnects = Counter("ws_reconnects_total", "WebSocket reconnect count")
        self.ws_messages = Counter("ws_messages_total", "WebSocket messages received")
        self.ws_latency_ms = Histogram(
            "ws_latency_ms",
            "WebSocket message latency",
            buckets=[10, 50, 100, 250, 500],
        )

        # Market data metrics
        self.rest_requests = Counter("rest_requests_total", "REST API requests")
        self.rest_errors = Counter("rest_errors_total", "REST API errors")
        self.rate_limit_hits = Counter("rate_limit_hits_total", "Rate limit hits")

        # System health
        self.memory_usage_mb = Gauge("memory_usage_mb", "Memory usage in MB")
        self.active_symbols = Gauge("active_symbols", "Number of active symbols")
        self.enabled_strategies = Gauge("enabled_strategies", "Number of enabled strategies")

        # Telegram metrics
        self.telegram_sent = Counter("telegram_sent_total", "Messages sent to Telegram")
        self.telegram_errors = Counter("telegram_errors_total", "Telegram send errors")
        self.telegram_queue_size = Gauge("telegram_queue_size", "Current Telegram queue size")

    def record_signal(self, strategy_id: str, score: float) -> None:
        """Record signal generation."""
        self.signals_generated.inc()
        self.signal_score.observe(score)

        # Track by strategy
        if strategy_id not in self.signals_by_strategy:
            self.signals_by_strategy[strategy_id] = Counter(
                f"signals_strategy_{strategy_id}", f"Signals for {strategy_id}"
            )
        self.signals_by_strategy[strategy_id].inc()

    def record_strategy_calc(self, duration_ms: float, *, error: bool = False) -> None:
        """Record strategy calculation."""
        self.strategy_calc_time.observe(duration_ms)
        if error:
            self.strategy_errors.inc()

    def record_ws_reconnect(self) -> None:
        """Record WebSocket reconnect."""
        self.ws_reconnects.inc()

    def record_ws_message(self, latency_ms: float) -> None:
        """Record WebSocket message."""
        self.ws_messages.inc()
        self.ws_latency_ms.observe(latency_ms)

    def record_rest_request(self, *, error: bool = False) -> None:
        """Record REST API request."""
        self.rest_requests.inc()
        if error:
            self.rest_errors.inc()

    def record_telegram_sent(self, *, error: bool = False) -> None:
        """Record Telegram message."""
        if error:
            self.telegram_errors.inc()
        else:
            self.telegram_sent.inc()

    def update_performance(self, winrate: float, pf: float, sharpe: float) -> None:
        """Update performance metrics."""
        safe_set(self.winrate_24h, winrate)
        safe_set(self.profit_factor, pf)
        safe_set(self.sharpe_ratio, sharpe)

    def to_dict(self) -> dict[str, Any]:
        """Export all metrics as dictionary."""
        return {
            "signals_total": self.signals_generated.value,
            "winrate_24h": self.winrate_24h.value,
            "profit_factor": self.profit_factor.value,
            "sharpe_ratio": self.sharpe_ratio.value,
            "strategy_errors": self.strategy_errors.value,
            "ws_reconnects": self.ws_reconnects.value,
            "ws_messages": self.ws_messages.value,
            "rest_requests": self.rest_requests.value,
            "rest_errors": self.rest_errors.value,
            "rate_limit_hits": self.rate_limit_hits.value,
            "telegram_sent": self.telegram_sent.value,
            "telegram_errors": self.telegram_errors.value,
            "active_symbols": self.active_symbols.value,
            "enabled_strategies": self.enabled_strategies.value,
        }


class MetricsExporter:
    """Export metrics in Prometheus format."""

    def __init__(self, metrics: BotMetrics):
        self._metrics = metrics

    def export_prometheus(self) -> str:
        """Export metrics in Prometheus text format."""
        lines = []

        # Helper to format labels
        def format_labels(labels: dict[str, str]) -> str:
            if not labels:
                return ""
            pairs = [f'{k}="{v}"' for k, v in labels.items()]
            return "{" + ",".join(pairs) + "}"

        # Export all counters and gauges
        for metric in self._metrics.__dict__.values():
            if isinstance(metric, (Counter, Gauge)):
                lines.append(f"# HELP {metric.name} {metric.description}")
                lines.append(
                    f"# TYPE {metric.name} {'counter' if isinstance(metric, Counter) else 'gauge'}"
                )
                labels_str = format_labels(metric.labels)
                lines.append(f"{metric.name}{labels_str} {metric.value}")

        return "\n".join(lines)

    def export_json(self) -> dict[str, Any]:
        """Export metrics as JSON."""
        return self._metrics.to_dict()

# --- from runtime/alerts.py ---
if TYPE_CHECKING:
    from collections.abc import Callable

LOG = logging.getLogger("bot.core.diagnostics.alerts")
ALERT_THROTTLE_SECONDS = 300.0  # seconds: 5 minutes between identical alerts


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class AlertSeverity(Enum):
    """Alert severity levels."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class Alert:
    """Alert notification."""

    id: str
    severity: AlertSeverity
    component: str
    message: str
    timestamp: datetime
    details: dict[str, Any] | None = None
    acknowledged: bool = False


class AlertManager:
    """Manage alerts and notifications.

    Features:
    - Alert deduplication (same alert within cooldown period)
    - Alert throttling
    - Multiple notification channels
    - Alert history
    """

    def __init__(
        self,
        cooldown_seconds: float = ALERT_THROTTLE_SECONDS,
        max_history: int = 1000,
    ):
        self._cooldown = cooldown_seconds
        self._handlers: list[Callable[[Alert], None]] = []
        self._recent_alerts: dict[str, datetime] = {}  # Alert ID -> last sent
        self._history: deque[Alert] = deque(maxlen=max_history)
        self._alert_count = 0
        self._handler_tasks: set[asyncio.Task[None]] = set()

    def add_handler(self, handler: Callable[[Alert], None]) -> None:
        """Add notification handler."""
        self._handlers.append(handler)

    def create_alert(
        self,
        severity: AlertSeverity,
        component: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> Alert | None:
        """Create alert if not in cooldown.

        Returns None if alert is suppressed (cooldown active).
        """
        # Generate alert ID from component + message hash
        alert_id = f"{component}:{hash(message) % 10000000}"

        # Check cooldown
        now = _utcnow_naive()
        last_sent = self._recent_alerts.get(alert_id)

        if last_sent and (now - last_sent).total_seconds() < self._cooldown:
            LOG.debug("Alert suppressed (cooldown): %s", alert_id)
            return None

        # Create alert
        alert = Alert(
            id=alert_id,
            severity=severity,
            component=component,
            message=message,
            timestamp=now,
            details=details,
        )

        # Update cooldown tracker
        self._recent_alerts[alert_id] = now

        # Add to history
        self._history.append(alert)
        self._alert_count += 1

        # Send to handlers
        for handler in self._handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    task = asyncio.create_task(handler(alert))
                    self._handler_tasks.add(task)
                    task.add_done_callback(self._handler_tasks.discard)
                else:
                    handler(alert)
            except Exception:
                LOG.exception("Alert handler failed")

        LOG.info("Alert created: [%s] %s - %s", severity.value, component, message)
        return alert

    def from_health_check(self, check: ComponentHealth) -> Alert | None:
        """Create alert from health check result."""
        if check.status == HealthStatus.HEALTHY:
            return None

        severity = (
            AlertSeverity.CRITICAL
            if check.status == HealthStatus.UNHEALTHY
            else AlertSeverity.WARNING
        )

        return self.create_alert(
            severity=severity,
            component=check.name,
            message=check.message or f"Status: {check.status.value}",
            details=check.details,
        )

    def from_degradation(
        self,
        component: str,
        metric_name: str,
        old_value: float,
        new_value: float,
        threshold: float,
    ) -> Alert | None:
        """Create alert from metric degradation."""
        change = abs(old_value - new_value)

        severity = AlertSeverity.CRITICAL if change > threshold * 1.5 else AlertSeverity.WARNING

        direction = "dropped" if new_value < old_value else "increased"

        return self.create_alert(
            severity=severity,
            component=component,
            message=f"{metric_name} {direction}: {old_value:.3f} -> {new_value:.3f}",
            details={
                "metric": metric_name,
                "old_value": old_value,
                "new_value": new_value,
                "threshold": threshold,
                "change": change,
            },
        )

    def get_recent(
        self,
        since: datetime | None = None,
        severity: AlertSeverity | None = None,
    ) -> list[Alert]:
        """Get recent alerts."""
        alerts = list(self._history)

        if since:
            alerts = [a for a in alerts if a.timestamp >= since]

        if severity:
            alerts = [a for a in alerts if a.severity == severity]

        return sorted(alerts, key=lambda a: a.timestamp, reverse=True)

    def get_unacknowledged(self) -> list[Alert]:
        """Get unacknowledged alerts."""
        return [a for a in self._history if not a.acknowledged]

    def acknowledge(self, alert_id: str) -> bool:
        """Acknowledge an alert."""
        for alert in self._history:
            if alert.id == alert_id:
                alert.acknowledged = True
                return True
        return False

    def clear_history(self) -> None:
        """Clear alert history."""
        self._history.clear()
        self._recent_alerts.clear()

    def get_stats(self) -> dict[str, Any]:
        """Get alert statistics."""
        unack = len(self.get_unacknowledged())

        by_severity = {
            AlertSeverity.INFO: 0,
            AlertSeverity.WARNING: 0,
            AlertSeverity.CRITICAL: 0,
        }

        for alert in self._history:
            by_severity[alert.severity] += 1

        return {
            "total_alerts": self._alert_count,
            "in_history": len(self._history),
            "unacknowledged": unack,
            "by_severity": {k.value: v for k, v in by_severity.items()},
        }

# --- from runtime/strategy_audit.py ---
NON_TRADING_OUTCOMES: frozenset[str] = frozenset(
    {
        "expired_pending",
        "expired",
        "expired_active",
        "risk_monitor_exit",
        "smart_exit",
        "unactivated_close",
        "superseded",
    }
)
WIN_OUTCOMES: frozenset[str] = frozenset(
    {
        "tp1_hit",
        "tp2_hit",
        "tp3_hit",
        "partial_tp",
        "trailing_stop",
    }
)
LOSS_OUTCOMES: frozenset[str] = frozenset(
    {
        "stop_loss",
        "ambiguous_exit",
    }
)
SCHEDULED_SETUP_IDS: frozenset[str] = frozenset({"session_killzone"})
MARKET_CONDITION_REASONS: frozenset[str] = frozenset(
    {
        "context.outside_killzone",
        "pattern.no_keltner_breakout",
        "indicator.funding_not_extreme",
        "indicator.ls_ratio_not_extreme",
    }
)
DATA_SOURCE_REASONS: frozenset[str] = frozenset(
    {
        "data.liquidation_score_missing",
        "data.ls_ratio_missing",
        "data.funding_rate_missing",
        "data.regular_divergence_missing",
        "data.rsi_divergence_missing",
        "data.wyckoff_spring_upthrust_missing",
    }
)


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else default
    if isinstance(value, str):
        try:
            numeric = float(value)
        except ValueError:
            return default
        return numeric if math.isfinite(numeric) else default
    return default


def _as_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return default
    return default


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _parse_ts(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value:
        return None
    raw = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _counter_items_to_map(items: object) -> dict[str, int]:
    result: dict[str, int] = {}
    if not isinstance(items, list):
        return result
    for item in items:
        if isinstance(item, Mapping):
            name = str(item.get("name") or "")
            if name:
                result[name] = _as_int(item.get("count"))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            name = str(item[0] or "")
            if name:
                result[name] = _as_int(item[1])
    return result


def _first_reason_for_setup(summary: Mapping[str, Any], setup_id: str) -> str | None:
    counts = summary.get("strategy_counts")
    if isinstance(counts, Mapping):
        row = counts.get(setup_id)
        if isinstance(row, Mapping):
            top_reason = row.get("top_reject_reason") or row.get("top_blocker")
            if top_reason:
                return str(top_reason)
    reason_counts: Counter[str] = Counter()
    by_symbol = summary.get("by_symbol")
    if isinstance(by_symbol, Mapping):
        for symbol_row in by_symbol.values():
            if not isinstance(symbol_row, Mapping):
                continue
            decisions = symbol_row.get("decisions")
            if not isinstance(decisions, list):
                continue
            for decision in decisions:
                if not isinstance(decision, Mapping):
                    continue
                if str(decision.get("setup_id") or "") != setup_id:
                    continue
                reason = decision.get("reason_code") or decision.get("reason")
                if reason:
                    reason_counts[str(reason)] += 1
    if reason_counts:
        return reason_counts.most_common(1)[0][0]
    return None


@dataclass(slots=True)
class OutcomeStats:
    setup_id: str
    total: int = 0
    wins: int = 0
    losses: int = 0
    active: int = 0
    pending: int = 0
    non_trading: int = 0
    avg_r: float = 0.0
    avg_pnl_pct: float = 0.0
    max_loss_r: float = 0.0
    max_win_r: float = 0.0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    result_counts: Counter[str] = field(default_factory=Counter)

    @property
    def trading_total(self) -> int:
        return self.total - self.non_trading

    @property
    def win_rate(self) -> float:
        return self.wins / self.trading_total if self.trading_total > 0 else 0.0

    @property
    def loss_rate(self) -> float:
        return self.losses / self.trading_total if self.trading_total > 0 else 0.0

    @property
    def has_negative_edge(self) -> bool:
        return self.trading_total >= 3 and self.avg_r < 0.0 and self.win_rate < 0.30

    @property
    def should_quarantine(self) -> bool:
        return self.trading_total >= 3 and self.wins == 0 and self.avg_r <= -0.35

    def to_dict(self) -> dict[str, Any]:
        return {
            "setup_id": self.setup_id,
            "total": self.total,
            "trading_total": self.trading_total,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 4),
            "loss_rate": round(self.loss_rate, 4),
            "avg_r": round(self.avg_r, 4),
            "avg_pnl_pct": round(self.avg_pnl_pct, 4),
            "max_loss_r": round(self.max_loss_r, 4),
            "max_win_r": round(self.max_win_r, 4),
            "active": self.active,
            "pending": self.pending,
            "non_trading": self.non_trading,
            "result_counts": dict(self.result_counts),
            "should_quarantine": self.should_quarantine,
            "has_negative_edge": self.has_negative_edge,
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
        }


@dataclass(slots=True)
class DetectorStats:
    setup_id: str
    runs: int = 0
    hits: int = 0
    rejects: int = 0
    skips: int = 0
    errors: int = 0
    top_blocker: str | None = None
    reason_counts: Counter[str] = field(default_factory=Counter)

    @property
    def hit_rate(self) -> float:
        return self.hits / self.runs if self.runs > 0 else 0.0

    @property
    def reject_rate(self) -> float:
        return self.rejects / self.runs if self.runs > 0 else 0.0

    @property
    def is_scheduled_out(self) -> bool:
        return self.setup_id in SCHEDULED_SETUP_IDS and self.runs == 0

    @property
    def primary_blocker(self) -> str | None:
        if self.top_blocker:
            return self.top_blocker
        if self.reason_counts:
            return self.reason_counts.most_common(1)[0][0]
        return None

    def classify_no_hit_reason(self) -> str:
        if self.is_scheduled_out:
            return "scheduled_gate"
        reason = self.primary_blocker or ""
        if reason in MARKET_CONDITION_REASONS:
            return "market_condition"
        if reason in DATA_SOURCE_REASONS or reason.startswith("data."):
            return "missing_source_data"
        if reason.startswith("indicator."):
            return "threshold_too_strict"
        if reason.startswith("context."):
            return "context_conflict"
        if reason.startswith("pattern."):
            return "implementation_or_threshold"
        if self.errors:
            return "runtime_error"
        if self.runs == 0:
            return "not_evaluated"
        return "ambiguous"

    def to_dict(self) -> dict[str, Any]:
        return {
            "setup_id": self.setup_id,
            "runs": self.runs,
            "hits": self.hits,
            "rejects": self.rejects,
            "skips": self.skips,
            "errors": self.errors,
            "hit_rate": round(self.hit_rate, 4),
            "reject_rate": round(self.reject_rate, 4),
            "top_blocker": self.primary_blocker,
            "no_hit_reason": self.classify_no_hit_reason(),
            "reason_counts": dict(self.reason_counts),
        }


@dataclass(slots=True)
class SignalContractStats:
    checked: int = 0
    ok: int = 0
    failed: int = 0
    issue_counts: Counter[str] = field(default_factory=Counter)
    field_counts: Counter[str] = field(default_factory=Counter)

    @property
    def failure_rate(self) -> float:
        return self.failed / self.checked if self.checked > 0 else 0.0

    @property
    def passes(self) -> bool:
        return self.checked > 0 and self.failed == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "ok": self.ok,
            "failed": self.failed,
            "failure_rate": round(self.failure_rate, 4),
            "issue_counts": dict(self.issue_counts),
            "field_counts": dict(self.field_counts),
            "passes": self.passes,
        }


@dataclass(slots=True)
class ScoreStats:
    n: int = 0
    min_score: float = 0.0
    mean_score: float = 0.0
    max_score: float = 0.0
    stdev_score: float = 0.0
    high_confidence: int = 0
    buckets: dict[str, int] = field(default_factory=dict)

    @property
    def differentiated(self) -> bool:
        return self.n >= 2 and self.max_score > 0.75 and self.stdev_score > 0.06

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "min": round(self.min_score, 4),
            "mean": round(self.mean_score, 4),
            "max": round(self.max_score, 4),
            "stdev": round(self.stdev_score, 4),
            "high_confidence": self.high_confidence,
            "buckets": dict(self.buckets),
            "differentiated": self.differentiated,
        }


@dataclass(slots=True)
class StrategyAuditRow:
    setup_id: str
    family: str = "unknown"
    enabled: bool = True
    catalog_status: str = "unknown"
    detector: DetectorStats = field(default_factory=lambda: DetectorStats(setup_id=""))
    outcomes: OutcomeStats = field(default_factory=lambda: OutcomeStats(setup_id=""))
    status: str = "unknown"
    actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "setup_id": self.setup_id,
            "family": self.family,
            "enabled": self.enabled,
            "catalog_status": self.catalog_status,
            "status": self.status,
            "actions": list(self.actions),
            "detector": self.detector.to_dict(),
            "outcomes": self.outcomes.to_dict(),
        }


@dataclass(slots=True)
class StrategyAuditReport:
    generated_at: datetime
    rows: list[StrategyAuditRow]
    score: ScoreStats
    contract: SignalContractStats
    source_db: Path | None = None
    source_summary: Path | None = None
    registered_count: int = 0
    enabled_count: int = 0

    @property
    def by_setup(self) -> dict[str, StrategyAuditRow]:
        return {row.setup_id: row for row in self.rows}

    @property
    def negative_edge_setups(self) -> list[str]:
        return [row.setup_id for row in self.rows if row.outcomes.has_negative_edge]

    @property
    def quarantine_setups(self) -> list[str]:
        return [row.setup_id for row in self.rows if row.outcomes.should_quarantine]

    @property
    def zero_signal_setups(self) -> list[str]:
        return [
            row.setup_id
            for row in self.rows
            if row.enabled
            and row.detector.hits == 0
            and row.detector.runs > 0
            and row.setup_id not in SCHEDULED_SETUP_IDS
        ]

    @property
    def scheduled_setups(self) -> list[str]:
        return [row.setup_id for row in self.rows if row.detector.is_scheduled_out]

    @property
    def active_detector_setups(self) -> list[str]:
        return [row.setup_id for row in self.rows if row.detector.hits > 0]

    def status_counts(self) -> dict[str, int]:
        counter = Counter(row.status for row in self.rows)
        return dict(sorted(counter.items()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "source_db": str(self.source_db) if self.source_db else None,
            "source_summary": str(self.source_summary) if self.source_summary else None,
            "registered_count": self.registered_count,
            "enabled_count": self.enabled_count,
            "status_counts": self.status_counts(),
            "negative_edge_setups": self.negative_edge_setups,
            "quarantine_setups": self.quarantine_setups,
            "zero_signal_setups": self.zero_signal_setups,
            "scheduled_setups": self.scheduled_setups,
            "active_detector_setups": self.active_detector_setups,
            "score": self.score.to_dict(),
            "signal_contract": self.contract.to_dict(),
            "rows": [row.to_dict() for row in self.rows],
        }


def load_strategy_catalog() -> dict[str, dict[str, Any]]:
    catalog: dict[str, dict[str, Any]] = {}
    for strategy_class in STRATEGY_CLASSES:
        setup_id = getattr(strategy_class, "setup_id", strategy_class.__name__)
        family = getattr(strategy_class, "family", "unknown")
        status = getattr(strategy_class, "status", None)
        catalog[str(setup_id)] = {
            "setup_id": str(setup_id),
            "class_name": strategy_class.__name__,
            "family": str(family or "unknown"),
            "catalog_status": str(status or "unknown"),
            "enabled": True,
        }
    return catalog


def load_config_enabled_flags(path: str | Path = "config.toml") -> dict[str, bool]:
    config_path = Path(path)
    if not config_path.exists():
        return {}
    flags: dict[str, bool] = {}
    in_setups = False
    for raw_line in config_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_setups = line == "[bot.setups]"
            continue
        if not in_setups or "=" not in line:
            continue
        key, value = line.split("=", 1)
        setup_id = key.strip()
        raw_value = value.strip().split("#", 1)[0].strip().lower()
        if raw_value in {"true", "false"}:
            flags[setup_id] = raw_value == "true"
    return flags


def load_outcome_stats(
    db_path: str | Path, *, last_days: int | None = 90
) -> dict[str, OutcomeStats]:
    path = Path(db_path)
    if not path.exists():
        return {}
    since = _utc_now() - timedelta(days=last_days) if last_days is not None else None
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='signal_outcomes'"
        ).fetchone()
        if table_exists is None:
            return {}
        query = "SELECT * FROM signal_outcomes WHERE 1=1"
        params: list[Any] = []
        if since is not None:
            query += " AND COALESCE(closed_at, created_at) >= ?"
            params.append(since.isoformat())
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        setup_id = str(row["setup_id"] or "")
        if setup_id:
            grouped[setup_id].append(row)

    stats: dict[str, OutcomeStats] = {}
    for setup_id, setup_rows in grouped.items():
        item = OutcomeStats(setup_id=setup_id)
        r_values: list[float] = []
        pnl_values: list[float] = []
        for row in setup_rows:
            result = str(row["result"] or "")
            item.total += 1
            item.result_counts[result] += 1
            if result in NON_TRADING_OUTCOMES:
                item.non_trading += 1
            if result == "pending":
                item.pending += 1
            if row["activated_at"] and not row["closed_at"]:
                item.active += 1
            if result not in NON_TRADING_OUTCOMES:
                was_profitable = bool(row["was_profitable"])
                pnl_r = _as_float(row["pnl_r_multiple"])
                r_values.append(pnl_r)
                pnl_values.append(_as_float(row["pnl_pct"]))
                if was_profitable or result in WIN_OUTCOMES or pnl_r > 0.0:
                    item.wins += 1
                elif result in LOSS_OUTCOMES or pnl_r < 0.0:
                    item.losses += 1
            created = _parse_ts(row["created_at"])
            closed = _parse_ts(row["closed_at"])
            stamp = closed or created
            if stamp is not None:
                item.first_seen = stamp if item.first_seen is None else min(item.first_seen, stamp)
                item.last_seen = stamp if item.last_seen is None else max(item.last_seen, stamp)
        if r_values:
            item.avg_r = sum(r_values) / len(r_values)
            item.max_loss_r = min(r_values)
            item.max_win_r = max(r_values)
        if pnl_values:
            item.avg_pnl_pct = sum(pnl_values) / len(pnl_values)
        stats[setup_id] = item
    return stats


def load_detector_stats(summary_path: str | Path) -> dict[str, DetectorStats]:
    summary = _load_json(Path(summary_path))
    if not summary:
        return {}
    counts = summary.get("strategy_counts")
    result: dict[str, DetectorStats] = {}
    if isinstance(counts, Mapping):
        for setup_id, row in counts.items():
            if not isinstance(row, Mapping):
                continue
            item = DetectorStats(
                setup_id=str(setup_id),
                runs=_as_int(row.get("observed_results") or row.get("runs")),
                hits=_as_int(row.get("hits")),
                rejects=_as_int(row.get("rejects")),
                skips=_as_int(row.get("skips")),
                errors=_as_int(row.get("errors")),
                top_blocker=(
                    str(row.get("top_reject_reason") or row.get("top_blocker"))
                    if row.get("top_reject_reason") or row.get("top_blocker")
                    else None
                ),
            )
            result[item.setup_id] = item
    hit_map = _counter_items_to_map(summary.get("strategy_hits"))
    reject_map = _counter_items_to_map(summary.get("strategy_rejects"))
    skip_map = _counter_items_to_map(summary.get("strategy_skips"))
    error_map = _counter_items_to_map(summary.get("strategy_errors"))
    for setup_id in set(hit_map) | set(reject_map) | set(skip_map) | set(error_map):
        item = result.setdefault(setup_id, DetectorStats(setup_id=setup_id))
        item.hits = max(item.hits, hit_map.get(setup_id, 0))
        item.rejects = max(item.rejects, reject_map.get(setup_id, 0))
        item.skips = max(item.skips, skip_map.get(setup_id, 0))
        item.errors = max(item.errors, error_map.get(setup_id, 0))
        item.runs = max(item.runs, item.hits + item.rejects + item.skips + item.errors)
    reason_map = _counter_items_to_map(summary.get("strategy_reject_reasons"))
    setup_reason_counts = _setup_reason_counts_from_summary(summary)
    for setup_id, reason_counts in setup_reason_counts.items():
        item = result.setdefault(setup_id, DetectorStats(setup_id=setup_id))
        item.reason_counts.update(reason_counts)
        item.top_blocker = item.primary_blocker
    if reason_map and len(result) == 1:
        only = next(iter(result.values()))
        only.reason_counts.update(reason_map)
        only.top_blocker = only.primary_blocker
    registered = summary.get("registered_strategies")
    if isinstance(registered, list):
        for setup_id in registered:
            result.setdefault(str(setup_id), DetectorStats(setup_id=str(setup_id)))
    return result


def latest_strategy_decisions_path(
    root: str | Path = "data/bot/telemetry/runs",
) -> Path | None:
    base = Path(root)
    if not base.exists():
        return None
    candidates = sorted(
        base.glob("*/analysis/strategy_decisions.jsonl"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0.0,
        reverse=True,
    )
    return candidates[0] if candidates else None


def load_detector_stats_from_decisions(path: str | Path | None) -> dict[str, DetectorStats]:
    if path is None:
        return {}
    source = Path(path)
    if not source.exists():
        return {}
    result: dict[str, DetectorStats] = {}
    try:
        handle = source.open("r", encoding="utf-8")
    except OSError:
        return {}
    with handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, Mapping):
                continue
            setup_id = str(row.get("setup_id") or row.get("strategy") or "")
            if not setup_id:
                continue
            item = result.setdefault(setup_id, DetectorStats(setup_id=setup_id))
            status = str(row.get("status") or row.get("decision_status") or "").lower()
            item.runs += 1
            if status == "signal":
                item.hits += 1
            elif status == "skip":
                item.skips += 1
            elif status == "error":
                item.errors += 1
            else:
                item.rejects += 1
                reason = row.get("reason_code") or row.get("reason")
                if reason:
                    item.reason_counts[str(reason)] += 1
    for item in result.values():
        item.top_blocker = item.primary_blocker
    return result


def merge_detector_stats(
    primary: Mapping[str, DetectorStats],
    secondary: Mapping[str, DetectorStats],
) -> dict[str, DetectorStats]:
    result: dict[str, DetectorStats] = {
        setup_id: DetectorStats(
            setup_id=stats.setup_id,
            runs=stats.runs,
            hits=stats.hits,
            rejects=stats.rejects,
            skips=stats.skips,
            errors=stats.errors,
            top_blocker=stats.top_blocker,
            reason_counts=Counter(stats.reason_counts),
        )
        for setup_id, stats in primary.items()
    }
    for setup_id, stats in secondary.items():
        if setup_id not in result:
            result[setup_id] = DetectorStats(
                setup_id=stats.setup_id,
                runs=stats.runs,
                hits=stats.hits,
                rejects=stats.rejects,
                skips=stats.skips,
                errors=stats.errors,
                top_blocker=stats.top_blocker,
                reason_counts=Counter(stats.reason_counts),
            )
            continue
        item = result[setup_id]
        if stats.runs > item.runs:
            item.runs = stats.runs
            item.hits = stats.hits
            item.rejects = stats.rejects
            item.skips = stats.skips
            item.errors = stats.errors
        elif stats.runs == item.runs:
            item.hits = max(item.hits, stats.hits)
            item.rejects = max(item.rejects, stats.rejects)
            item.skips = max(item.skips, stats.skips)
            item.errors = max(item.errors, stats.errors)
        item.reason_counts.update(stats.reason_counts)
        item.top_blocker = item.primary_blocker
    return result


def _setup_reason_counts_from_summary(summary: Mapping[str, Any]) -> dict[str, Counter[str]]:
    result: dict[str, Counter[str]] = defaultdict(Counter)
    by_symbol = summary.get("by_symbol")
    if isinstance(by_symbol, Mapping):
        for symbol_row in by_symbol.values():
            if not isinstance(symbol_row, Mapping):
                continue
            for decision in symbol_row.get("decisions") or ():
                if not isinstance(decision, Mapping):
                    continue
                setup_id = str(decision.get("setup_id") or "")
                reason = decision.get("reason_code") or decision.get("reason")
                if setup_id and reason:
                    result[setup_id][str(reason)] += 1
    if result:
        return result
    return result


def load_score_stats(summary_path: str | Path) -> ScoreStats:
    summary = _load_json(Path(summary_path))
    row = summary.get("confluence_score")
    if not isinstance(row, Mapping):
        return ScoreStats()
    return ScoreStats(
        n=_as_int(row.get("n")),
        min_score=_as_float(row.get("min")),
        mean_score=_as_float(row.get("mean")),
        max_score=_as_float(row.get("max")),
        stdev_score=_as_float(row.get("stdev")),
        high_confidence=_as_int(row.get("high_confidence")),
        buckets={
            str(bucket): _as_int(count)
            for bucket, count in (row.get("buckets") or {}).items()
            if isinstance(row.get("buckets"), Mapping)
        },
    )


def load_signal_contract_stats(summary_path: str | Path) -> SignalContractStats:
    summary = _load_json(Path(summary_path))
    row = summary.get("signal_contract")
    if not isinstance(row, Mapping):
        return SignalContractStats()
    stats = SignalContractStats(
        checked=_as_int(row.get("checked")),
        ok=_as_int(row.get("ok")),
        failed=_as_int(row.get("failed")),
    )
    issue_counts = row.get("issue_counts")
    if isinstance(issue_counts, Mapping):
        stats.issue_counts.update({str(k): _as_int(v) for k, v in issue_counts.items()})
    field_counts = row.get("fields")
    if isinstance(field_counts, Mapping):
        stats.field_counts.update({str(k): _as_int(v) for k, v in field_counts.items()})
    return stats


def classify_strategy(
    *,
    _setup_id: str,
    enabled: bool,
    detector: DetectorStats,
    outcomes: OutcomeStats,
) -> tuple[str, list[str]]:
    actions: list[str] = []
    if not enabled:
        status = "quarantined_off"
        actions.append("config_disabled")
    elif outcomes.should_quarantine:
        status = "needs_rework"
        actions.append("quarantine_negative_edge")
    elif outcomes.has_negative_edge:
        status = "negative_edge_watch"
        actions.append("tighten_confirmations")
    elif detector.is_scheduled_out:
        status = "scheduled_gate"
        actions.append("verify_inside_schedule_window")
    elif detector.hits > 0 and outcomes.trading_total > 0 and outcomes.avg_r > 0.0:
        status = "validated_positive"
    elif detector.hits > 0 and outcomes.trading_total == 0:
        status = "detector_active_no_closed_outcomes"
        actions.append("monitor_delivery_and_outcomes")
    elif detector.hits > 0:
        status = "detector_active"
    elif detector.runs > 0:
        reason = detector.classify_no_hit_reason()
        status = f"zero_signal:{reason}"
        if reason in {"threshold_too_strict", "implementation_or_threshold", "ambiguous"}:
            actions.append("calibrate_detector_thresholds")
        elif reason == "missing_source_data":
            actions.append("fix_source_data_contract")
        elif reason == "market_condition":
            actions.append("rerun_live_check_on_larger_symbol_sample")
        elif reason == "context_conflict":
            actions.append("review_family_context_policy")
    else:
        status = "unverified"
        actions.append("run_live_check")
    return status, actions


def build_audit_report(
    *,
    db_path: str | Path = "data/bot/bot.db",
    summary_path: str | Path = "data/bot/telemetry/strategy_prompt_baseline.json",
    decisions_path: str | Path | None = None,
    config_path: str | Path = "config.toml",
    last_days: int | None = 90,
) -> StrategyAuditReport:
    catalog = load_strategy_catalog()
    enabled_flags = load_config_enabled_flags(config_path)
    outcomes = load_outcome_stats(db_path, last_days=last_days)
    summary_detectors = load_detector_stats(summary_path)
    decision_source = (
        latest_strategy_decisions_path()
        if str(decisions_path or "").lower() == "latest"
        else Path(decisions_path)
        if decisions_path
        else None
    )
    decision_detectors = load_detector_stats_from_decisions(decision_source)
    detectors = merge_detector_stats(summary_detectors, decision_detectors)
    score = load_score_stats(summary_path)
    contract = load_signal_contract_stats(summary_path)
    setup_ids = sorted(set(catalog) | set(outcomes) | set(detectors))
    rows: list[StrategyAuditRow] = []
    for setup_id in setup_ids:
        catalog_row = catalog.get(setup_id, {})
        enabled = bool(enabled_flags.get(setup_id, catalog_row.get("enabled", True)))
        detector = detectors.get(setup_id, DetectorStats(setup_id=setup_id))
        outcome = outcomes.get(setup_id, OutcomeStats(setup_id=setup_id))
        if not detector.top_blocker:
            blocker = _first_reason_for_setup(_load_json(Path(summary_path)), setup_id)
            if blocker:
                detector.top_blocker = blocker
        status, actions = classify_strategy(
            setup_id=setup_id,
            enabled=enabled,
            detector=detector,
            outcomes=outcome,
        )
        rows.append(
            StrategyAuditRow(
                setup_id=setup_id,
                family=str(catalog_row.get("family") or "unknown"),
                enabled=enabled,
                catalog_status=str(catalog_row.get("catalog_status") or "unknown"),
                detector=detector,
                outcomes=outcome,
                status=status,
                actions=actions,
            )
        )
    return StrategyAuditReport(
        generated_at=_utc_now(),
        rows=rows,
        score=score,
        contract=contract,
        source_db=Path(db_path),
        source_summary=Path(summary_path),
        registered_count=len(catalog),
        enabled_count=sum(1 for row in rows if row.enabled),
    )


def render_table(report: StrategyAuditReport, *, include_all: bool = True) -> str:
    rows = report.rows if include_all else [row for row in report.rows if row.actions]
    header = (
        "setup_id",
        "status",
        "detector",
        "trades",
        "win%",
        "avgR",
        "blocker",
        "actions",
    )
    lines = [" | ".join(header)]
    lines.append(" | ".join("---" for _ in header))
    for row in rows:
        detector = f"{row.detector.hits}/{row.detector.runs}"
        trades = str(row.outcomes.trading_total)
        win = f"{row.outcomes.win_rate * 100:.1f}"
        avg_r = f"{row.outcomes.avg_r:.2f}"
        blocker = row.detector.primary_blocker or ""
        actions = ",".join(row.actions)
        lines.append(
            " | ".join(
                (
                    row.setup_id,
                    row.status,
                    detector,
                    trades,
                    win,
                    avg_r,
                    blocker,
                    actions,
                )
            )
        )
    return "\n".join(lines)


def gate_failures(
    report: StrategyAuditReport,
    *,
    min_py_lines: int | None = None,
    py_lines_changed: int | None = None,
    require_registered: int = 38,
    require_score_differentiation: bool = True,
    require_signal_contract: bool = True,
    allow_scheduled_zero: Iterable[str] = SCHEDULED_SETUP_IDS,
) -> list[str]:
    failures: list[str] = []
    if min_py_lines is not None:
        if py_lines_changed is None:
            failures.append("python_line_count_missing")
        elif py_lines_changed < min_py_lines:
            failures.append(f"python_lines_changed<{min_py_lines}:{py_lines_changed}")
    if report.registered_count != require_registered:
        failures.append(f"registered_count:{report.registered_count}!={require_registered}")
    if require_score_differentiation and not report.score.differentiated:
        failures.append("score_distribution_not_differentiated")
    if require_signal_contract and not report.contract.passes:
        failures.append("signal_contract_not_passing")
    allowed = set(allow_scheduled_zero)
    zero_unallowed = [setup_id for setup_id in report.zero_signal_setups if setup_id not in allowed]
    if zero_unallowed:
        failures.append(f"zero_signal_setups:{','.join(zero_unallowed)}")
    return failures


def write_report_json(report: StrategyAuditReport, path: str | Path) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True), encoding="utf-8")


def summarize_actions(rows: Sequence[StrategyAuditRow]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        for action in row.actions:
            result[action].append(row.setup_id)
    return {action: sorted(set(setups)) for action, setups in sorted(result.items())}


def print_report(report: StrategyAuditReport, *, include_all: bool = True) -> None:
    print("Strategy audit")
    print(f"generated_at={report.generated_at.isoformat()}")
    print(f"registered={report.registered_count} enabled={report.enabled_count}")
    print(f"status_counts={report.status_counts()}")
    print(f"negative_edge={report.negative_edge_setups}")
    print(f"quarantine={report.quarantine_setups}")
    print(f"zero_signal={report.zero_signal_setups}")
    print(f"scheduled={report.scheduled_setups}")
    print(f"score={report.score.to_dict()}")
    print(f"signal_contract={report.contract.to_dict()}")
    print(render_table(report, include_all=include_all))

