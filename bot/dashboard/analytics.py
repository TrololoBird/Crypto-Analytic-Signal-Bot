"""Strategy performance analytics built on persisted signal outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..persistence.repository.memory import MemoryRepository


@dataclass(slots=True)
class StrategyAnalytics:
    repo: MemoryRepository

    @staticmethod
    def _is_trade_outcome(row: dict[str, Any]) -> bool:
        result = str(row.get("result") or "")
        if result in {
            "expired",
            "expired_active",
            "expired_pending",
            "risk_monitor_exit",
            "smart_exit",
            "unactivated_close",
            "superseded",
        }:
            return False
        return bool(row.get("activated_at")) or result in {
            "tp1_hit",
            "tp2_hit",
            "stop_loss",
            "breakeven_stop",
            "trailing_stop",
            "ambiguous_exit",
            "emergency_exit",
        }

    async def generate_report(
        self,
        days: int = 30,
        *,
        since: datetime | None = None,
        scope: str = "rolling",
    ) -> dict[str, Any]:
        setup_rows = await self.repo.get_setup_stats(
            last_days=None if since is not None else days,
            since=since,
        )
        outcomes = await self.repo.get_signal_outcomes(
            last_days=None if since is not None else days,
            since=since,
        )
        active_rows = await self.repo.get_active_signals(include_closed=True)
        if since is not None:
            active_rows = [
                row
                for row in active_rows
                if (created_at := self._parse_dt(row.get("created_at"))) is not None
                and created_at >= since
            ]
        tracking_ids_with_outcome = {
            str(row.get("tracking_id")) for row in outcomes if row.get("tracking_id")
        }
        signal_counts: dict[str, dict[str, int]] = {}
        for row in active_rows:
            setup_id = str(row.get("setup_id") or "unknown")
            bucket = signal_counts.setdefault(
                setup_id,
                {
                    "signals_seen": 0,
                    "pending_signals": 0,
                    "active_signals": 0,
                    "closed_signals": 0,
                    "closed_missing_outcomes": 0,
                },
            )
            bucket["signals_seen"] += 1
            status = str(row.get("status") or "")
            if status == "pending":
                bucket["pending_signals"] += 1
            elif status == "active":
                bucket["active_signals"] += 1
            elif status == "closed":
                bucket["closed_signals"] += 1
                if str(row.get("tracking_id") or "") not in tracking_ids_with_outcome:
                    bucket["closed_missing_outcomes"] += 1

        by_setup: dict[str, list[dict[str, Any]]] = {}
        for row in outcomes:
            setup_id = str(row.get("setup_id") or "unknown")
            by_setup.setdefault(setup_id, []).append(row)

        setup_reports: list[dict[str, Any]] = []
        for setup in setup_rows:
            setup_id = str(setup.get("setup_id") or "unknown")
            rows = by_setup.get(setup_id, [])
            trade_rows = [row for row in rows if self._is_trade_outcome(row)]
            trades = int(setup.get("total") or 0)
            win_rate = float(setup.get("win_rate") or 0.0)
            gross_profit = sum(max(float(r.get("pnl_r_multiple") or 0.0), 0.0) for r in trade_rows)
            gross_loss = sum(
                abs(min(float(r.get("pnl_r_multiple") or 0.0), 0.0)) for r in trade_rows
            )
            profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

            curve = 0.0
            peak = 0.0
            max_drawdown = 0.0
            for r in trade_rows:
                curve += float(r.get("pnl_r_multiple") or 0.0)
                peak = max(peak, curve)
                max_drawdown = min(max_drawdown, curve - peak)

            expectancy = float(setup.get("avg_r_multiple") or 0.0)
            counts = signal_counts.get(setup_id, {})
            setup_reports.append(
                {
                    "setup_id": setup_id,
                    "trades": trades,
                    "count": trades,
                    "outcomes": len(rows),
                    "signals_seen": int(counts.get("signals_seen", trades) or 0),
                    "pending_signals": int(counts.get("pending_signals", 0) or 0),
                    "active_signals": int(counts.get("active_signals", 0) or 0),
                    "closed_signals": int(counts.get("closed_signals", 0) or 0),
                    "closed_missing_outcomes": int(counts.get("closed_missing_outcomes", 0) or 0),
                    "win_rate": round(win_rate, 4),
                    "expectancy_r": round(expectancy, 4),
                    "avg_rr": round(expectancy, 4),
                    "profit_factor": None if profit_factor is None else round(profit_factor, 4),
                    "max_drawdown_r": round(max_drawdown, 4),
                }
            )

        for setup_id, counts in signal_counts.items():
            if any(row["setup_id"] == setup_id for row in setup_reports):
                continue
            setup_reports.append(
                {
                    "setup_id": setup_id,
                    "trades": 0,
                    "count": 0,
                    "outcomes": 0,
                    "signals_seen": int(counts.get("signals_seen", 0) or 0),
                    "pending_signals": int(counts.get("pending_signals", 0) or 0),
                    "active_signals": int(counts.get("active_signals", 0) or 0),
                    "closed_signals": int(counts.get("closed_signals", 0) or 0),
                    "closed_missing_outcomes": int(counts.get("closed_missing_outcomes", 0) or 0),
                    "win_rate": 0.0,
                    "expectancy_r": 0.0,
                    "avg_rr": 0.0,
                    "profit_factor": None,
                    "max_drawdown_r": 0.0,
                }
            )

        setup_reports = sorted(setup_reports, key=lambda r: r["setup_id"])
        total_trades = sum(int(r["trades"]) for r in setup_reports)
        total_signals_seen = sum(int(r.get("signals_seen") or 0) for r in setup_reports)
        weighted_wins = sum(float(r["win_rate"]) * int(r["trades"]) for r in setup_reports)
        weighted_expectancy = sum(
            float(r["expectancy_r"]) * int(r["trades"]) for r in setup_reports
        )
        all_trade_rows = [
            row for rows in by_setup.values() for row in rows if self._is_trade_outcome(row)
        ]
        avg_mae = (
            sum(abs(float(r.get("mae") or 0.0)) for r in all_trade_rows) / len(all_trade_rows)
            if all_trade_rows
            else 0.0
        )
        avg_mfe = (
            sum(float(r.get("mfe") or 0.0) for r in all_trade_rows) / len(all_trade_rows)
            if all_trade_rows
            else 0.0
        )
        avg_r = (
            sum(float(r.get("pnl_r_multiple") or 0.0) for r in all_trade_rows) / len(all_trade_rows)
            if all_trade_rows
            else 0.0
        )

        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "scope": scope,
            "since": since.isoformat() if since is not None else None,
            "window_days": int(days),
            "summary": {
                "total_signals": total_signals_seen,
                "total_trades": total_trades,
                "win_rate": (weighted_wins / total_trades) if total_trades else 0.0,
                "avg_rr": (weighted_expectancy / total_trades) if total_trades else 0.0,
                "avg_r_multiple": round(avg_r, 4),
                "avg_mae": round(avg_mae, 4),
                "avg_mfe": round(avg_mfe, 4),
            },
            "by_setup": {str(row["setup_id"]): row for row in setup_reports},
            "setup_reports": setup_reports,
            "total_trades": total_trades,
            "total_signals": total_signals_seen,
        }

    @staticmethod
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
