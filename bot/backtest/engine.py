from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import polars as pl

from .metrics import BacktestResult
from ..config import BotSettings


@dataclass(slots=True)
class _LifecycleSignal:
    setup_id: str
    direction: str
    signal_index: int
    created_at: datetime
    entry_low: float
    entry_high: float
    stop: float
    take_profit_1: float
    take_profit_2: float


class VectorizedBacktester:
    SUPPORTED_SETUPS = {
        "ema_cross",
        "momentum_breakout",
        "structure_pullback",
        "structure_break_retest",
        "wick_trap_reversal",
        "squeeze_setup",
        "ema_bounce",
        "fvg_setup",
        "order_block",
        "liquidity_sweep",
        "bos_choch",
        "hidden_divergence",
        "funding_reversal",
        "cvd_divergence",
        "session_killzone",
        "breaker_block",
        "turtle_soup",
        "vwap_trend",
        "supertrend_follow",
        "price_velocity",
        "volume_anomaly",
        "volume_climax_reversal",
        "keltner_breakout",
        "whale_walls",
        "spread_strategy",
        "depth_imbalance",
        "absorption",
        "aggression_shift",
        "liquidation_heatmap",
        "stop_hunt_detection",
        "multi_tf_trend",
        "rsi_divergence_bottom",
        "wyckoff_spring",
        "bb_squeeze",
        "atr_expansion",
        "ls_ratio_extreme",
        "oi_divergence",
        "btc_correlation",
        "altcoin_season_index",
    }

    def __init__(self, settings: BotSettings) -> None:
        self.settings = settings

    def _load_ohlcv(self, symbol: str, timeframe: str) -> pl.DataFrame:
        parquet_path = (
            self.settings.data_dir / "parquet" / f"{symbol}_{timeframe}.parquet"
        )
        if parquet_path.exists():
            return pl.read_parquet(parquet_path)
        return pl.DataFrame()

    def run(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: str = "15m",
        setup_id: str | None = None,
        initial_equity: float = 1.0,
        signals: pl.DataFrame | list[dict[str, Any]] | None = None,
    ) -> BacktestResult:
        setup = (setup_id or "ema_cross").strip().lower()
        if setup not in self.SUPPORTED_SETUPS:
            raise ValueError(
                f"unsupported setup_id={setup!r}, supported={sorted(self.SUPPORTED_SETUPS)}"
            )

        df = self._load_ohlcv(symbol, timeframe)
        if not df.is_empty() and "close_time" in df.columns:
            df = df.filter(
                (pl.col("close_time") >= start) & (pl.col("close_time") <= end)
            )

        if df.is_empty() or "close" not in df.columns:
            empty = pl.DataFrame(
                {"ts": [datetime.now(UTC)], "equity": [float(initial_equity)]}
            )
            return BacktestResult(
                total_return=0.0,
                sharpe_ratio=0.0,
                max_drawdown=0.0,
                win_rate=0.0,
                profit_factor=0.0,
                trades=pl.DataFrame(),
                equity_curve=empty,
                trade_count=0,
                expectancy=0.0,
            )

        work = self._prepare_frame(df)
        if work.is_empty():
            empty = pl.DataFrame(
                {"ts": [datetime.now(UTC)], "equity": [float(initial_equity)]}
            )
            return BacktestResult(
                0.0, 0.0, 0.0, 0.0, 0.0, pl.DataFrame(), empty, 0, 0.0
            )

        signal_rows = self._materialize_signals(work, setup_id=setup, signals=signals)
        if not signal_rows:
            empty = pl.DataFrame(
                {"ts": [datetime.now(UTC)], "equity": [float(initial_equity)]}
            )
            return BacktestResult(
                0.0, 0.0, 0.0, 0.0, 0.0, pl.DataFrame(), empty, 0, 0.0
            )

        trades, equity_curve = self._simulate_lifecycle(
            work,
            signal_rows,
            initial_equity=max(0.01, float(initial_equity)),
        )
        return BacktestResult.from_frames(trades=trades, equity_curve=equity_curve)

    def _materialize_signals(
        self,
        frame: pl.DataFrame,
        *,
        setup_id: str,
        signals: pl.DataFrame | list[dict[str, Any]] | None,
    ) -> list[_LifecycleSignal]:
        if isinstance(signals, list):
            signal_frame = pl.DataFrame(signals)
        else:
            signal_frame = signals

        if signal_frame is not None and not signal_frame.is_empty():
            return self._signals_from_explicit_rows(
                frame, signal_frame, default_setup_id=setup_id
            )
        return self._signals_from_signal_columns(frame, setup_id=setup_id)

    def _signals_from_explicit_rows(
        self,
        frame: pl.DataFrame,
        signal_frame: pl.DataFrame,
        *,
        default_setup_id: str,
    ) -> list[_LifecycleSignal]:
        ts_values = frame.get_column("close_time").to_list()
        index_by_ts = {value: index for index, value in enumerate(ts_values)}
        rows: list[_LifecycleSignal] = []
        for row in signal_frame.to_dicts():
            created_at = row.get("created_at") or row.get("ts") or row.get("close_time")
            if not isinstance(created_at, datetime):
                continue
            signal_index = index_by_ts.get(created_at)
            if signal_index is None:
                continue
            entry_low = float(
                row.get(
                    "entry_low", row.get("entry", frame.item(signal_index, "close"))
                )
            )
            entry_high = float(
                row.get(
                    "entry_high", row.get("entry", frame.item(signal_index, "close"))
                )
            )
            stop = float(row.get("stop", entry_low))
            tp1 = float(row.get("take_profit_1", row.get("tp1", entry_high)))
            tp2 = float(row.get("take_profit_2", row.get("tp2", tp1)))
            rows.append(
                _LifecycleSignal(
                    setup_id=str(row.get("setup_id") or default_setup_id),
                    direction=str(row.get("direction") or "long"),
                    signal_index=signal_index,
                    created_at=created_at,
                    entry_low=min(entry_low, entry_high),
                    entry_high=max(entry_low, entry_high),
                    stop=stop,
                    take_profit_1=tp1,
                    take_profit_2=max(tp1, tp2)
                    if str(row.get("direction") or "long") == "long"
                    else min(tp1, tp2),
                )
            )
        return rows

    def _signals_from_signal_columns(
        self, frame: pl.DataFrame, *, setup_id: str
    ) -> list[_LifecycleSignal]:
        if "signal_long" not in frame.columns and "signal_short" not in frame.columns:
            return []

        closes = frame.get_column("close").cast(pl.Float64).to_list()
        atrs = frame.get_column("atr14").cast(pl.Float64).to_list()
        close_times = frame.get_column("close_time").to_list()
        signal_long = (
            frame.get_column("signal_long").to_list()
            if "signal_long" in frame.columns
            else [0] * frame.height
        )
        signal_short = (
            frame.get_column("signal_short").to_list()
            if "signal_short" in frame.columns
            else [0] * frame.height
        )

        rows: list[_LifecycleSignal] = []
        for index, price in enumerate(closes):
            risk = max(float(atrs[index] or 0.0), float(price) * 0.003)
            if bool(signal_long[index]):
                rows.append(
                    _LifecycleSignal(
                        setup_id=setup_id,
                        direction="long",
                        signal_index=index,
                        created_at=close_times[index],
                        entry_low=float(price),
                        entry_high=float(price),
                        stop=float(price) - risk,
                        take_profit_1=float(price) + risk * 1.5,
                        take_profit_2=float(price) + risk * 2.0,
                    )
                )
            if bool(signal_short[index]):
                rows.append(
                    _LifecycleSignal(
                        setup_id=setup_id,
                        direction="short",
                        signal_index=index,
                        created_at=close_times[index],
                        entry_low=float(price),
                        entry_high=float(price),
                        stop=float(price) + risk,
                        take_profit_1=float(price) - risk * 1.5,
                        take_profit_2=float(price) - risk * 2.0,
                    )
                )
        return rows

    def _simulate_lifecycle(
        self,
        frame: pl.DataFrame,
        signals: list[_LifecycleSignal],
        *,
        initial_equity: float,
    ) -> tuple[pl.DataFrame, pl.DataFrame]:
        highs = frame.get_column("high").cast(pl.Float64).to_list()
        lows = frame.get_column("low").cast(pl.Float64).to_list()
        close_times = frame.get_column("close_time").to_list()
        pending_bars = 2
        max_holding_bars = 24
        equity = initial_equity
        equity_points: list[dict[str, Any]] = [{"ts": close_times[0], "equity": equity}]
        trades: list[dict[str, Any]] = []

        for signal in signals:
            activation_index: int | None = None
            activated = False
            tp1_hit = False
            exit_index: int | None = None
            exit_price: float | None = None
            status = "expired"

            for bar_index in range(
                signal.signal_index + 1,
                min(frame.height, signal.signal_index + pending_bars + 1),
            ):
                if (
                    lows[bar_index] <= signal.entry_high
                    and highs[bar_index] >= signal.entry_low
                ):
                    activated = True
                    activation_index = bar_index
                    break

            if activation_index is None:
                trades.append(
                    self._trade_row(
                        signal,
                        activated=False,
                        tp1_hit=False,
                        status="expired",
                        entry_ts=signal.created_at,
                        activation_ts=None,
                        exit_ts=close_times[
                            min(frame.height - 1, signal.signal_index + pending_bars)
                        ],
                        exit_price=None,
                        ret=0.0,
                        resolution_bars=pending_bars,
                    )
                )
                continue

            for bar_index in range(
                activation_index,
                min(frame.height, activation_index + max_holding_bars + 1),
            ):
                low = lows[bar_index]
                high = highs[bar_index]
                if signal.direction == "long":
                    stop_hit = low <= signal.stop
                    tp1_now = high >= signal.take_profit_1
                    tp2_now = high >= signal.take_profit_2
                else:
                    stop_hit = high >= signal.stop
                    tp1_now = low <= signal.take_profit_1
                    tp2_now = low <= signal.take_profit_2

                if stop_hit and not tp1_hit:
                    exit_index = bar_index
                    exit_price = signal.stop
                    status = "sl"
                    break
                if tp2_now:
                    tp1_hit = True
                    exit_index = bar_index
                    exit_price = signal.take_profit_2
                    status = "tp2"
                    break
                if tp1_now and not tp1_hit:
                    tp1_hit = True
                    status = "tp1"

            if exit_index is None:
                if tp1_hit:
                    exit_index = min(
                        frame.height - 1, activation_index + max_holding_bars
                    )
                    exit_price = signal.take_profit_1
                    status = "tp1"
                else:
                    exit_index = min(
                        frame.height - 1, activation_index + max_holding_bars
                    )
                    exit_price = None
                    status = "expired"

            ret = self._trade_return(status)
            equity *= 1.0 + (0.01 * ret)
            equity_points.append({"ts": close_times[exit_index], "equity": equity})
            trades.append(
                self._trade_row(
                    signal,
                    activated=activated,
                    tp1_hit=tp1_hit,
                    status=status,
                    entry_ts=signal.created_at,
                    activation_ts=close_times[activation_index],
                    exit_ts=close_times[exit_index],
                    exit_price=exit_price,
                    ret=ret,
                    resolution_bars=max(1, exit_index - signal.signal_index),
                )
            )

        return (
            pl.DataFrame(trades) if trades else pl.DataFrame(),
            pl.DataFrame(equity_points),
        )

    @staticmethod
    def _trade_row(
        signal: _LifecycleSignal,
        *,
        activated: bool,
        tp1_hit: bool,
        status: str,
        entry_ts: datetime,
        activation_ts: datetime | None,
        exit_ts: datetime,
        exit_price: float | None,
        ret: float,
        resolution_bars: int,
    ) -> dict[str, Any]:
        return {
            "setup_id": signal.setup_id,
            "direction": signal.direction,
            "entry_ts": entry_ts,
            "activation_ts": activation_ts,
            "exit_ts": exit_ts,
            "entry_low": signal.entry_low,
            "entry_high": signal.entry_high,
            "stop": signal.stop,
            "take_profit_1": signal.take_profit_1,
            "take_profit_2": signal.take_profit_2,
            "exit_price": exit_price,
            "status": status,
            "activated": activated,
            "tp1_hit": tp1_hit,
            "ret": ret,
            "resolution_bars": resolution_bars,
            "position_leverage": 1.0,
            "risk_per_trade": 0.01,
        }

    @staticmethod
    def _trade_return(status: str) -> float:
        if status == "tp2":
            return 2.0
        if status == "tp1":
            return 1.0
        if status == "sl":
            return -1.0
        return 0.0

    @staticmethod
    def _prepare_frame(df: pl.DataFrame) -> pl.DataFrame:
        frame = df.with_columns(
            [
                pl.col("close").cast(pl.Float64),
                pl.col("high").cast(pl.Float64),
                pl.col("low").cast(pl.Float64),
            ]
        )
        if "ema20" not in frame.columns:
            frame = frame.with_columns(
                [pl.col("close").ewm_mean(span=20, adjust=False).alias("ema20")]
            )
        if "ema50" not in frame.columns:
            frame = frame.with_columns(
                [pl.col("close").ewm_mean(span=50, adjust=False).alias("ema50")]
            )
        if "atr14" not in frame.columns:
            tr = pl.max_horizontal(
                [
                    (pl.col("high") - pl.col("low")).abs(),
                    (pl.col("high") - pl.col("close").shift(1)).abs(),
                    (pl.col("low") - pl.col("close").shift(1)).abs(),
                ]
            ).alias("tr")
            frame = (
                frame.with_columns([tr])
                .with_columns(
                    [
                        pl.col("tr")
                        .rolling_mean(window_size=14)
                        .fill_null(0.0)
                        .alias("atr14")
                    ]
                )
                .drop("tr")
            )
        return frame
