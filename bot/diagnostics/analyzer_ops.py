"""Diagnostics analyzer helpers (metrics, tracker, reporter)."""

from __future__ import annotations
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
import numpy as np
import polars as pl
from datetime import UTC, datetime
from typing import ClassVar
from bot.persistence.repository.memory import MemoryRepository
from bot.persistence.repository.schema import OutcomeRecord, SignalRecord
import json
from enum import Enum

# --- from analyzer/metrics.py ---
LOG = logging.getLogger("bot.core.analyzer.metrics")


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return default


@dataclass
class PerformanceMetrics:
    """Performance metrics for strategy or overall system."""

    # Basic counts
    total_signals: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0

    # Ratios
    win_rate: float = 0.0
    loss_rate: float = 0.0
    profit_factor: float = 0.0

    # Returns
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    max_win_pct: float = 0.0
    max_loss_pct: float = 0.0

    # Risk metrics
    avg_mae: float = 0.0  # Max adverse excursion
    avg_mfe: float = 0.0  # Max favorable excursion
    avg_risk_reward: float = 0.0

    # Time metrics
    avg_time_to_tp1_min: float | None = None
    avg_time_to_sl_min: float | None = None

    # Sharpe-like ratio (simplified)
    sharpe_ratio: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_signals": self.total_signals,
            "wins": self.wins,
            "losses": self.losses,
            "breakeven": self.breakeven,
            "win_rate": round(self.win_rate, 4),
            "loss_rate": round(self.loss_rate, 4),
            "profit_factor": round(self.profit_factor, 4),
            "avg_win_pct": round(self.avg_win_pct, 4),
            "avg_loss_pct": round(self.avg_loss_pct, 4),
            "max_win_pct": round(self.max_win_pct, 4),
            "max_loss_pct": round(self.max_loss_pct, 4),
            "avg_mae": round(self.avg_mae, 4),
            "avg_mfe": round(self.avg_mfe, 4),
            "avg_risk_reward": round(self.avg_risk_reward, 4),
            "sharpe_ratio": round(self.sharpe_ratio, 4),
        }


class WinRateCalculator:
    """Calculate win rates and performance metrics."""

    def __init__(self, repository: MemoryRepository):
        self._repo = repository

    async def calculate_metrics(
        self,
        strategy_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        min_score: float = 0.0,
    ) -> PerformanceMetrics:
        """Calculate performance metrics.

        Args:
            strategy_id: Optional filter by strategy
            since: Start date for analysis
            until: End date for analysis
            min_score: Minimum signal score to include

        Returns:
            PerformanceMetrics with calculated values
        """
        # Default to last 30 days if not specified
        if since is None:
            since = _utcnow_naive() - timedelta(days=30)

        # Get signals with outcomes as DataFrame
        df = await self._repo.get_signals_for_analysis(since, min_score)

        if df.is_empty():
            return PerformanceMetrics()
        if until is not None:
            df = df.filter(pl.col("created_at") <= until.isoformat())
            if df.is_empty():
                return PerformanceMetrics()

        # Filter by strategy if specified
        if strategy_id:
            df = df.filter(pl.col("strategy_id") == strategy_id)

        if df.is_empty():
            return PerformanceMetrics()

        metrics = PerformanceMetrics()
        metrics.total_signals = df.height

        # Count results
        results = df["result"].value_counts()
        counts_by_result = dict(
            zip(
                results["result"].to_list(),
                results["count"].to_list(),
                strict=False,
            )
        )
        metrics.wins = int(counts_by_result.get("win", 0) or 0)
        metrics.losses = int(counts_by_result.get("loss", 0) or 0)
        metrics.breakeven = int(
            sum(
                int(count or 0)
                for result_type, count in counts_by_result.items()
                if result_type not in {"win", "loss"}
            )
        )

        # Calculate rates
        closed = metrics.wins + metrics.losses + metrics.breakeven
        if closed > 0:
            metrics.win_rate = metrics.wins / closed
            metrics.loss_rate = metrics.losses / closed

        # Calculate profit factor
        wins_df = df.filter(pl.col("result") == "win")
        losses_df = df.filter(pl.col("result") == "loss")

        if not wins_df.is_empty() and not losses_df.is_empty():
            total_wins = _as_float(wins_df["pnl_24h"].sum())
            total_losses = abs(_as_float(losses_df["pnl_24h"].sum()))

            if total_losses > 0:
                metrics.profit_factor = total_wins / total_losses

        # Average win/loss
        if not wins_df.is_empty():
            metrics.avg_win_pct = _as_float(wins_df["pnl_24h"].mean())
            metrics.max_win_pct = _as_float(wins_df["pnl_24h"].max())

        if not losses_df.is_empty():
            metrics.avg_loss_pct = _as_float(losses_df["pnl_24h"].mean())
            metrics.max_loss_pct = _as_float(losses_df["pnl_24h"].min())

        # MAE/MFE
        if "max_loss_pct" in df.columns:
            metrics.avg_mae = _as_float(df["max_loss_pct"].mean())
        if "max_profit_pct" in df.columns:
            metrics.avg_mfe = _as_float(df["max_profit_pct"].mean())

        # Simplified Sharpe (24h returns / std dev)
        pnl_col = df["pnl_24h"]
        if not pnl_col.is_null().all():
            returns = pnl_col.drop_nulls()
            if len(returns) > 1:
                mean_ret = _as_float(returns.mean())
                std_ret = _as_float(returns.std())
                if std_ret > 0:
                    metrics.sharpe_ratio = mean_ret / std_ret * np.sqrt(365)  # Annualized

        return metrics

    async def calculate_by_strategy(
        self,
        since: datetime | None = None,
    ) -> dict[str, PerformanceMetrics]:
        """Calculate metrics for each strategy."""
        if since is None:
            since = _utcnow_naive() - timedelta(days=30)

        df = await self._repo.get_signals_for_analysis(since)

        if df.is_empty():
            return {}

        strategies = df["strategy_id"].unique().to_list()

        results = {}
        for strategy_id in strategies:
            metrics = await self.calculate_metrics(strategy_id=strategy_id, since=since)
            results[strategy_id] = metrics

        return results

    async def get_winrate_trend(
        self,
        strategy_id: str | None = None,
        window_days: int = 7,
        periods: int = 4,
    ) -> list[dict[str, Any]]:
        """Get win rate trend over time.

        Args:
            strategy_id: Optional strategy filter
            window_days: Days per period
            periods: Number of periods

        Returns:
            List of period metrics
        """
        end = _utcnow_naive()
        results = []

        for i in range(periods):
            period_end = end - timedelta(days=i * window_days)
            period_start = period_end - timedelta(days=window_days)

            metrics = await self.calculate_metrics(
                strategy_id=strategy_id,
                since=period_start,
                until=period_end,
            )

            results.append(
                {
                    "period_start": period_start.isoformat(),
                    "period_end": period_end.isoformat(),
                    "metrics": metrics.to_dict(),
                }
            )

        return list(reversed(results))

    def detect_degradation(
        self,
        current: PerformanceMetrics,
        baseline: PerformanceMetrics,
        winrate_threshold: float = 0.15,
        pf_threshold: float = 0.3,
    ) -> bool:
        """Detect if performance has degraded significantly.

        Returns True if degradation detected.
        """
        # Check win rate drop
        if baseline.win_rate > 0:
            winrate_drop = baseline.win_rate - current.win_rate
            if winrate_drop > winrate_threshold:
                LOG.error(
                    "Win rate degradation detected: %.2f%% -> %.2f%%",
                    baseline.win_rate * 100,
                    current.win_rate * 100,
                )
                return True

        # Check profit factor drop
        if baseline.profit_factor > 0:
            pf_drop = baseline.profit_factor - current.profit_factor
            if pf_drop > pf_threshold:
                LOG.error(
                    "Profit factor degradation: %.2f -> %.2f",
                    baseline.profit_factor,
                    current.profit_factor,
                )
                return True

        return False

# --- from analyzer/tracker.py ---
LOG = logging.getLogger("bot.core.analyzer.tracker")


@dataclass
class PriceSnapshot:
    """Price data at a point in time."""

    price: float
    timestamp: datetime
    high: float | None = None
    low: float | None = None
    volume: float | None = None


class OutcomeTracker:
    """Tracks outcomes for generated signals.

    Updates signal outcomes at 1h, 4h, 24h intervals.
    Calculates MAE/MFE and hit rates for TP/SL.
    """

    # Time checkpoints for tracking (hours)
    CHECKPOINTS: ClassVar[tuple[int, ...]] = (1, 4, 24)

    def __init__(self, repository: MemoryRepository):
        self._repo = repository

    async def update_outcomes(
        self,
        signal_id: str,
        current_price: float,
        current_high: float | None = None,
        current_low: float | None = None,
        *,
        commit: bool = True,
    ) -> OutcomeRecord | None:
        """Update outcome for a signal with current market data.

        Args:
            signal_id: Signal to update
            current_price: Current market price
            current_high: Optional high since last check
            current_low: Optional low since last check

        Returns:
            Updated OutcomeRecord or None if signal not found
        """
        # Get signal
        signal = await self._repo.get_signal(signal_id)
        if signal is None:
            LOG.error("Signal not found: %s", signal_id)
            return None

        # Get or create outcome
        outcome_id = f"outcome_{signal_id}"
        outcome = await self._repo.get_outcome(outcome_id)

        if outcome is None:
            outcome = OutcomeRecord(
                outcome_id=outcome_id,
                signal_id=signal_id,
                symbol=signal.symbol,
            )

        # Calculate time elapsed
        now = datetime.now(UTC)
        elapsed_hours = (now - signal.created_at).total_seconds() / 3600

        # Update price checkpoints
        if elapsed_hours >= 1 and outcome.price_1h is None:
            outcome.price_1h = current_price
            outcome.pnl_1h = self._calculate_pnl(signal, current_price)

        if elapsed_hours >= 4 and outcome.price_4h is None:
            outcome.price_4h = current_price
            outcome.pnl_4h = self._calculate_pnl(signal, current_price)

        if elapsed_hours >= 24 and outcome.price_24h is None:
            outcome.price_24h = current_price
            outcome.pnl_24h = self._calculate_pnl(signal, current_price)
            outcome.closed_at = now
            outcome.result = self._classify_result(outcome, signal)

        # Update MAE/MFE
        pnl_pct = self._calculate_pnl(signal, current_price)
        if pnl_pct is not None:
            outcome.mfe = max(outcome.mfe, pnl_pct) if pnl_pct > 0 else outcome.mfe
            outcome.mae = min(outcome.mae, pnl_pct) if pnl_pct < 0 else outcome.mae

            outcome.max_profit_pct = max(outcome.max_profit_pct, pnl_pct)
            outcome.max_loss_pct = min(outcome.max_loss_pct, pnl_pct)

        # Check TP/SL hits
        self._check_targets(outcome, signal, current_price, current_high, current_low)

        # Update timestamp
        outcome.updated_at = now

        # Save
        await self._repo.save_outcome(outcome, commit=commit)

        return outcome

    def _calculate_pnl(self, signal: SignalRecord, current_price: float) -> float | None:
        """Calculate PnL percentage from entry."""
        if signal.entry_price <= 0 or current_price <= 0:
            return None

        if signal.direction == "long":
            return (current_price - signal.entry_price) / signal.entry_price * 100
        # short
        return (signal.entry_price - current_price) / signal.entry_price * 100

    def _check_targets(
        self,
        outcome: OutcomeRecord,
        signal: SignalRecord,
        price: float,
        _high: float | None,
        _low: float | None,
    ) -> None:
        """Check if TP or SL levels were hit."""
        if signal.direction == "long":
            # Check TP1 hit
            if not outcome.hit_tp1 and price >= signal.take_profit_1:
                outcome.hit_tp1 = True
                if outcome.time_to_tp1_min is None:
                    elapsed = (datetime.now(UTC) - signal.created_at).total_seconds() / 60
                    outcome.time_to_tp1_min = int(elapsed)

            # Check TP2 hit
            if not outcome.hit_tp2 and price >= signal.take_profit_2:
                outcome.hit_tp2 = True
                if outcome.time_to_tp2_min is None:
                    elapsed = (datetime.now(UTC) - signal.created_at).total_seconds() / 60
                    outcome.time_to_tp2_min = int(elapsed)

            # Check SL hit
            if not outcome.hit_sl and price <= signal.stop_loss:
                outcome.hit_sl = True
                if outcome.time_to_sl_min is None:
                    elapsed = (datetime.now(UTC) - signal.created_at).total_seconds() / 60
                    outcome.time_to_sl_min = int(elapsed)

        else:  # short
            # Check TP1 hit
            if not outcome.hit_tp1 and price <= signal.take_profit_1:
                outcome.hit_tp1 = True
                if outcome.time_to_tp1_min is None:
                    elapsed = (datetime.now(UTC) - signal.created_at).total_seconds() / 60
                    outcome.time_to_tp1_min = int(elapsed)

            # Check TP2 hit
            if not outcome.hit_tp2 and price <= signal.take_profit_2:
                outcome.hit_tp2 = True
                if outcome.time_to_tp2_min is None:
                    elapsed = (datetime.now(UTC) - signal.created_at).total_seconds() / 60
                    outcome.time_to_tp2_min = int(elapsed)

            # Check SL hit
            if not outcome.hit_sl and price >= signal.stop_loss:
                outcome.hit_sl = True
                if outcome.time_to_sl_min is None:
                    elapsed = (datetime.now(UTC) - signal.created_at).total_seconds() / 60
                    outcome.time_to_sl_min = int(elapsed)

    def _classify_result(self, outcome: OutcomeRecord, _signal: SignalRecord) -> str:
        """Classify final outcome."""
        # Priority: SL hit = loss, TP hit = win
        if outcome.hit_sl:
            return "loss"

        if outcome.hit_tp1 or outcome.hit_tp2:
            return "win"

        # Check 24h PnL
        if outcome.pnl_24h is not None:
            if outcome.pnl_24h > 0.5:  # > 0.5% profit
                return "win"
            if outcome.pnl_24h < -0.5:  # > 0.5% loss
                return "loss"

        return "breakeven"

    async def get_pending_signals(self, limit: int = 100) -> list[SignalRecord]:
        """Get signals without outcomes or with open outcomes."""
        return await self._repo.get_signals_without_outcome(limit=limit)

    async def batch_update(self, prices: dict[str, PriceSnapshot]) -> list[OutcomeRecord]:
        """Batch update outcomes for multiple signals.

        Args:
            prices: Dict mapping symbol to PriceSnapshot

        Returns:
            List of updated OutcomeRecords
        """
        pending = await self.get_pending_signals(limit=200)

        updated = []
        conn = self._repo._require_conn()
        await conn.execute("BEGIN")
        try:
            for signal in pending:
                if signal.symbol in prices:
                    snapshot = prices[signal.symbol]
                    outcome = await self.update_outcomes(
                        signal.signal_id,
                        snapshot.price,
                        snapshot.high,
                        snapshot.low,
                        commit=False,
                    )
                    if outcome:
                        updated.append(outcome)
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

        LOG.info("Updated %d outcomes from batch", len(updated))
        return updated

# --- from analyzer/reporter.py ---
LOG = logging.getLogger("bot.core.analyzer.reporter")


class ReportFormat(Enum):
    """Report output format."""

    TEXT = "text"
    HTML = "html"
    MARKDOWN = "markdown"
    JSON = "json"


@dataclass
class DailyReport:
    """Daily performance report."""

    date: datetime
    overall_metrics: PerformanceMetrics
    strategy_metrics: dict[str, PerformanceMetrics]
    top_signals: list[dict[str, Any]]
    alerts: list[str]

    def to_text(self) -> str:
        """Generate text report."""
        lines = [
            f"📊 Daily Report - {self.date.strftime('%Y-%m-%d')}",
            "",
            "Overall Performance:",
            f"  Signals: {self.overall_metrics.total_signals}",
            f"  Win Rate: {self.overall_metrics.win_rate * 100:.1f}%",
            f"  Profit Factor: {self.overall_metrics.profit_factor:.2f}",
            f"  Sharpe: {self.overall_metrics.sharpe_ratio:.2f}",
            "",
            "By Strategy:",
        ]

        for strategy_id, metrics in self.strategy_metrics.items():
            lines.append(f"  {strategy_id}:")
            lines.append(f"    Signals: {metrics.total_signals}")
            lines.append(f"    Win Rate: {metrics.win_rate * 100:.1f}%")
            lines.append(f"    PF: {metrics.profit_factor:.2f}")

        if self.alerts:
            lines.extend(["", "⚠️ Alerts:", *self.alerts])

        return "\n".join(lines)

    def to_markdown(self) -> str:
        """Generate markdown report."""
        lines = [
            f"## 📊 Daily Report - {self.date.strftime('%Y-%m-%d')}",
            "",
            "### Overall Performance",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Total Signals | {self.overall_metrics.total_signals} |",
            f"| Win Rate | {self.overall_metrics.win_rate * 100:.1f}% |",
            f"| Profit Factor | {self.overall_metrics.profit_factor:.2f} |",
            f"| Sharpe Ratio | {self.overall_metrics.sharpe_ratio:.2f} |",
            f"| Avg Win | {self.overall_metrics.avg_win_pct:.2f}% |",
            f"| Avg Loss | {self.overall_metrics.avg_loss_pct:.2f}% |",
            "",
            "### By Strategy",
            "",
            "| Strategy | Signals | Win Rate | PF |",
            "|----------|---------|----------|-----|",
        ]

        for strategy_id, metrics in self.strategy_metrics.items():
            lines.append(
                f"| {strategy_id} | {metrics.total_signals} | "
                f"{metrics.win_rate * 100:.1f}% | {metrics.profit_factor:.2f} |"
            )

        if self.alerts:
            lines.extend(["", "### ⚠️ Alerts", ""])
            lines.extend(f"- {alert}" for alert in self.alerts)

        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "date": self.date.isoformat(),
            "overall": self.overall_metrics.to_dict(),
            "strategies": {k: v.to_dict() for k, v in self.strategy_metrics.items()},
            "top_signals": self.top_signals,
            "alerts": self.alerts,
        }


class DailyReporter:
    """Generate daily performance reports."""

    def __init__(
        self,
        repository: MemoryRepository,
        calculator: WinRateCalculator,
    ):
        self._repo = repository
        self._calc = calculator

    async def generate(
        self,
        date: datetime | None = None,
        _format: ReportFormat = ReportFormat.MARKDOWN,
    ) -> DailyReport | None:
        """Generate daily report.

        Args:
            date: Report date (defaults to today)
            format: Output format

        Returns:
            DailyReport with metrics and alerts
        """
        if date is None:
            date = _utcnow_naive()

        # Calculate day bounds
        day_start = date.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

        # Overall metrics for the day
        overall = await self._calc.calculate_metrics(
            since=day_start,
            until=day_end,
        )
        if overall.total_signals <= 0:
            return None  # no signals for the period, do not send an empty report

        # Per-strategy metrics
        by_strategy = await self._calc.calculate_by_strategy(since=day_start)

        # Detect degradation alerts
        alerts = await self._check_alerts(by_strategy)
        top_signals = await self._get_top_signals(day_start, day_end)

        report = DailyReport(
            date=date,
            overall_metrics=overall,
            strategy_metrics=by_strategy,
            top_signals=top_signals,
            alerts=alerts,
        )

        LOG.info("Generated daily report for %s", date.strftime("%Y-%m-%d"))
        return report

    async def _get_top_signals(
        self,
        day_start: datetime,
        day_end: datetime,
        *,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        rows = await self._repo.get_signal_outcomes(last_days=None, limit=max(limit * 4, limit))
        filtered: list[dict[str, Any]] = []
        for row in rows:
            closed_raw = row.get("closed_at") or row.get("created_at")
            try:
                closed_at = datetime.fromisoformat(str(closed_raw))
            except (TypeError, ValueError):
                continue
            if not (day_start <= closed_at < day_end):
                continue
            filtered.append(row)

        filtered.sort(
            key=lambda item: (
                float(item.get("pnl_r_multiple") or 0.0),
                float(item.get("pnl_pct") or 0.0),
            ),
            reverse=True,
        )
        return [
            {
                "symbol": str(row.get("symbol") or ""),
                "setup_id": str(row.get("setup_id") or ""),
                "direction": str(row.get("direction") or ""),
                "result": str(row.get("result") or ""),
                "pnl_r_multiple": round(float(row.get("pnl_r_multiple") or 0.0), 4),
                "pnl_pct": round(float(row.get("pnl_pct") or 0.0), 4),
                "tracking_ref": row.get("tracking_ref"),
            }
            for row in filtered[:limit]
        ]

    async def _check_alerts(
        self,
        strategy_metrics: dict[str, PerformanceMetrics],
    ) -> list[str]:
        """Check for performance degradation alerts."""
        alerts = []

        for strategy_id, metrics in strategy_metrics.items():
            # Low win rate alert
            if metrics.total_signals >= 10 and metrics.win_rate < 0.4:
                alerts.append(f"{strategy_id}: Low win rate ({metrics.win_rate * 100:.1f}%)")

            # Low profit factor alert
            if metrics.total_signals >= 10 and metrics.profit_factor < 1.0:
                alerts.append(
                    f"{strategy_id}: Negative expectancy (PF={metrics.profit_factor:.2f})"
                )

            # No signals alert
            if metrics.total_signals == 0:
                alerts.append(f"{strategy_id}: No signals generated")

        return alerts

    def format_report(
        self, report: DailyReport, format: ReportFormat = ReportFormat.MARKDOWN
    ) -> str:
        """Format report to specified output."""
        if format == ReportFormat.TEXT:
            return report.to_text()
        if format == ReportFormat.MARKDOWN:
            return report.to_markdown()
        if format == ReportFormat.JSON:
            return json.dumps(report.to_dict(), indent=2)
        msg = f"Unsupported format: {format}"
        raise ValueError(msg)
