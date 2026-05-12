from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import polars as pl


@dataclass
class BacktestResult:
    total_return: float
    sharpe_ratio: float
    max_drawdown: float
    win_rate: float
    profit_factor: float
    trades: pl.DataFrame
    equity_curve: pl.DataFrame
    trade_count: int = 0
    expectancy: float = 0.0
    activation_rate: float = 0.0
    tp1_rate: float = 0.0
    tp2_rate: float = 0.0
    sl_rate: float = 0.0
    expired_rate: float = 0.0
    avg_resolution_bars: float = 0.0
    max_consecutive_losses: int = 0
    setup_breakdown: dict[str, dict[str, float | int]] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["trades"] = self.trades.to_dicts()
        payload["equity_curve"] = self.equity_curve.to_dicts()
        return payload

    @classmethod
    def from_frames(cls, trades: pl.DataFrame, equity_curve: pl.DataFrame) -> "BacktestResult":
        def _as_float(value: Any, default: float = 0.0) -> float:
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        equity = (
            equity_curve["equity"]
            if "equity" in equity_curve.columns
            else pl.Series("equity", [1.0], dtype=pl.Float64)
        )
        total_return = _as_float((equity.item(-1) if equity.len() else 1.0), 1.0) - 1.0
        running_max = equity.cum_max()
        drawdown = ((equity / running_max) - 1.0).min()
        max_dd = abs(_as_float(drawdown, 0.0))

        returns = (
            trades["ret"] if "ret" in trades.columns else pl.Series("ret", [], dtype=pl.Float64)
        )
        ret_std = _as_float(returns.std(), 0.0)
        mean_ret = _as_float(returns.mean(), 0.0)
        sharpe = (mean_ret / ret_std) if ret_std > 0 else 0.0
        wins = returns.filter(returns > 0)
        losses = returns.filter(returns < 0)
        win_rate = float(wins.len() / max(returns.len(), 1))
        wins_sum = _as_float(wins.sum(), 0.0)
        losses_sum = abs(_as_float(losses.sum(), 1e-9))
        profit_factor = float(wins_sum / losses_sum) if losses.len() > 0 else 0.0
        expectancy = _as_float(returns.mean(), 0.0) if returns.len() > 0 else 0.0
        activation_rate = (
            _as_float(trades.get_column("activated").mean(), 0.0)
            if "activated" in trades.columns
            else 0.0
        )
        tp1_rate = (
            _as_float(trades.get_column("tp1_hit").mean(), 0.0)
            if "tp1_hit" in trades.columns
            else 0.0
        )
        tp2_rate = (
            _as_float((trades.get_column("status") == "tp2").cast(pl.Float64).mean(), 0.0)
            if "status" in trades.columns
            else 0.0
        )
        sl_rate = (
            _as_float((trades.get_column("status") == "sl").cast(pl.Float64).mean(), 0.0)
            if "status" in trades.columns
            else 0.0
        )
        expired_rate = (
            _as_float((trades.get_column("status") == "expired").cast(pl.Float64).mean(), 0.0)
            if "status" in trades.columns
            else 0.0
        )
        avg_resolution_bars = (
            _as_float(trades.get_column("resolution_bars").mean(), 0.0)
            if "resolution_bars" in trades.columns
            else 0.0
        )
        max_consecutive_losses = 0
        current_losses = 0
        if "ret" in trades.columns:
            for value in trades.get_column("ret").to_list():
                if _as_float(value) < 0.0:
                    current_losses += 1
                    max_consecutive_losses = max(max_consecutive_losses, current_losses)
                else:
                    current_losses = 0

        setup_breakdown: dict[str, dict[str, float | int]] | None = None
        if "setup_id" in trades.columns and "status" in trades.columns and trades.height > 0:
            setup_breakdown = {}
            for row in trades.select(["setup_id", "status"]).to_dicts():
                setup_id = str(row["setup_id"])
                status = str(row["status"])
                bucket = setup_breakdown.setdefault(
                    setup_id, {"count": 0, "tp2": 0, "tp1": 0, "sl": 0, "expired": 0}
                )
                bucket["count"] = int(bucket["count"]) + 1
                if status == "tp2":
                    bucket["tp2"] = int(bucket["tp2"]) + 1
                    bucket["tp1"] = int(bucket["tp1"]) + 1
                elif status in {"tp1", "sl", "expired"}:
                    bucket[status] = int(bucket[status]) + 1
        return cls(
            total_return=total_return,
            sharpe_ratio=sharpe,
            max_drawdown=max_dd,
            win_rate=win_rate,
            profit_factor=profit_factor,
            trades=trades,
            equity_curve=equity_curve,
            trade_count=int(returns.len()),
            expectancy=expectancy,
            activation_rate=activation_rate,
            tp1_rate=tp1_rate,
            tp2_rate=tp2_rate,
            sl_rate=sl_rate,
            expired_rate=expired_rate,
            avg_resolution_bars=avg_resolution_bars,
            max_consecutive_losses=max_consecutive_losses,
            setup_breakdown=setup_breakdown,
        )
